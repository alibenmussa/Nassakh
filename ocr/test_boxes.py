"""Tests of the word boxes (`ocr.alignment`, audit of book 22): Tesseract's boxes fitted to the printed
lines and clipped where they run into the word on their right, the words of an in-line gap boxed from
the Tesseract words there, punctuation and numbers matched only inside their gap, Latin-looking
misreads taken by position, weak boxes marked and left alone by the numbers pass.

Small synthetic lines: boxes are `[x0, y0, x1, y1]`, the page reads right to left.
"""

from __future__ import annotations

import numpy as np

from ocr import numbers as nb
from ocr.alignment import (
    WEAK,
    Band,
    build_lines,
    clip_overruns,
    covered_bands,
    fit_lines,
    line_pitch,
    page_bands,
    pair_marks,
    split_at_ink,
    weak_boxes,
)
from review.services import normalize_token


def w(text: str, x0: int, x1: int, y0: int = 0, y1: int = 20) -> dict:
    return {"text": text, "bbox": [x0, y0, x1, y1], "conf": 90.0}


def line(*words: dict, **extra) -> dict:
    boxes = [word["bbox"] for word in words]
    bbox = [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]
    return {"bbox": bbox, "words": list(words), **extra}


def band(y0: int, y1: int, x0: int = 0, x1: int = 400) -> dict:
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


def boxes_of(built: list[dict]) -> dict[str, list | None]:
    return {t["t"]: t["bbox"] for b in built for t in b["tokens"]}


# ---------------------------------------------------------------- 1. boxes that run into their neighbour


def test_a_box_that_runs_into_its_right_hand_neighbour_ends_where_the_neighbour_starts():
    # book 22 page 2: «أكثر» [700, 878] runs over «أن» and «مقدرين»; «تسيطر» [74, 190] into «التي»
    words = [
        w("مقدرين", 805, 878),
        w("أن", 764, 791),
        w("أكثر", 700, 878),
        w("التي", 156, 198),
        w("تسيطر", 74, 190),
    ]
    clipped = [x["bbox"][:3:2] for x in clip_overruns(words)]
    assert clipped == [[805, 878], [764, 791], [700, 764], [156, 198], [74, 156]]
    assert words[2]["bbox"] == [700, 0, 878, 20]  # the input is not changed


def test_clipping_leaves_two_boxes_of_one_word_and_punctuation_alone():
    words = [w("كلمة", 100, 200), w("كلمه", 110, 200), w("قال", 0, 60), w("،", 50, 58)]
    assert [x["bbox"] for x in clip_overruns(words)] == [x["bbox"] for x in words]


# ---------------------------------------------------------------- 3. boxes kept inside their printed line

BANDS = [band(10, 20), band(70, 80), band(130, 140)]  # a pitch of 60 px


def test_printed_lines_reach_halfway_to_their_neighbours_and_half_a_pitch_beyond_their_core():
    assert line_pitch(BANDS) == 60
    assert [(b.lo, b.hi) for b in page_bands(BANDS)] == [(-20, 45), (45, 105), (105, 170)]
    assert page_bands([band(10, 20)]) == []  # no pitch: nothing says how far a line reaches
    assert covered_bands([0, 30, 400, 100], page_bands(BANDS)) == [Band(0, 70, 400, 80, 45, 105)]


def test_word_boxes_keep_to_their_printed_line_and_a_line_over_two_bands_is_flagged():
    lines = [
        line(w("قال", 300, 380, 30, 100), w("الأمير", 200, 280, 50, 90)),  # the second printed line
        # a Tesseract line grown over the second and third printed lines: its line is the third (the
        # second has a line of its own); a word on the second band's core alone stays there
        line(w("ثم", 300, 380, 60, 150), w("انتهى", 200, 280, 72, 90), w("هنا", 100, 180, 110, 150)),
    ]
    fitted = fit_lines(lines, page_bands(BANDS))
    assert [x["bbox"][1::2] for x in fitted[0]["words"]] == [[45, 100], [50, 90]]
    assert fitted[1]["two_bands"] is True and "two_bands" not in fitted[0]
    assert [x["bbox"][1::2] for x in fitted[1]["words"]] == [[105, 150], [72, 90], [110, 150]]
    assert fitted[1]["bbox"] == [100, 105, 380, 150] and fitted[1]["core"] == [130, 140]
    built = build_lines("قال الأمير ثم انتهى هنا", None, lines, bands=BANDS)
    assert [b["two_bands"] for b in built] == [False, True]
    assert boxes_of(built)["ثم"] == [300, 105, 380, 150]


# ---------------------------------------------------------------- 2. and 5. the words of a gap in a line


def test_the_words_of_an_in_line_gap_take_the_unused_tesseract_words_there_by_position():
    # book 22 page 4: «فتضم المدينة نفسها Tripolis» read "فتقم Tripotis Yad Spl": the Latin run
    # (the name and two misread words) laid out left to right in Tesseract's order
    tess = [line(w("فتقم", 519, 567), w("Tripotis", 270, 355), w("Yad", 381, 436), w("Spl", 449, 506))]
    built = build_lines("فتضم المدينة نفسها Tripolis", None, tess)
    tokens = {t["t"]: t for t in built[0]["tokens"]}
    assert tokens["المدينة"]["bbox"] == [449, 0, 506, 20] and tokens["المدينة"]["tess"] == "Spl"
    assert tokens["نفسها"]["bbox"] == [381, 0, 436, 20]
    assert tokens["Tripolis"]["bbox"] == [270, 0, 355, 20]


def _inked(blocks: list[tuple[int, int]], width: int = 400, height: int = 100) -> np.ndarray:
    gray = np.full((height, width), 255, dtype=np.uint8)
    for x0, x1 in blocks:
        gray[10:30, x0:x1] = 0
    return gray


def test_a_gap_with_fewer_tesseract_words_than_words_is_split_at_its_clean_ink_gaps():
    # Tesseract read «كتابه ب» as one word: its ink is cut at the one blank far wider than the rest
    tess = [line(w("قال", 310, 350, 5, 35), w("qqqq", 20, 290, 5, 35), w("انتهى", 0, 15, 5, 35))]
    bands = [band(12, 28), band(72, 88)]
    gray = _inked([(310, 350), (200, 290), (120, 190), (20, 60), (0, 15)])
    built = build_lines("قال كتابه ب انتهى", None, tess, bands=bands, gray=gray)
    assert boxes_of(built)["كتابه"] == [120, 5, 290, 35] and boxes_of(built)["ب"] == [20, 5, 60, 35]
    # two blanks alike: no clean split, no boxes (a split by letters shifts boxes)
    gray = _inked([(310, 350), (200, 290), (130, 190), (20, 120), (0, 15)])
    built = build_lines("قال كتابه ب انتهى", None, tess, bands=bands, gray=gray)
    assert boxes_of(built)["كتابه"] is None and boxes_of(built)["ب"] is None
    # without the gray image the gap keeps no box either
    assert boxes_of(build_lines("قال كتابه ب انتهى", None, tess, bands=bands))["كتابه"] is None


def test_split_at_ink_needs_clean_gaps_and_pieces_that_fit_their_words():
    gray = _inked([(200, 290), (120, 190), (20, 60)])
    assert split_at_ink(gray, (12, 28), 0, 300, [5, 1]) == [(120, 290), (20, 60)]
    assert split_at_ink(gray, (12, 28), 0, 300, [1]) == [(20, 290)]  # one word: its ink
    assert split_at_ink(gray, (12, 28), 0, 300, [1, 5]) is None  # the pieces do not fit the words
    assert split_at_ink(gray, (12, 28), 0, 300, [2, 2, 2, 2]) is None  # too few gaps
    assert split_at_ink(np.full((40, 300), 255, dtype=np.uint8), (12, 28), 0, 300, [1, 1]) is None


def test_latin_looking_words_between_lines_are_taken_by_position_when_the_words_are_arabic():
    # book 22 page 2: «علم الأنساب» start the next line, read "GLI ple" and laid out left to right
    tess = [
        line(w("قال", 300, 380), w("الأمير", 200, 280)),
        line(w("GLI", 250, 330, 30, 50), w("ple", 340, 380, 30, 50), w("من", 150, 200, 30, 50)),
    ]
    built = build_lines("قال الأمير علم الأنساب من", None, tess)
    assert boxes_of(built)["علم"] == [340, 30, 380, 50] and boxes_of(built)["الأنساب"] == [250, 30, 330, 50]
    # Latin words keep Tesseract's (left to right) order
    tess[1] = line(w("Lqqq", 250, 330, 30, 50), w("Mzzz", 340, 380, 30, 50), w("من", 150, 200, 30, 50))
    built = build_lines("قال الأمير Leptis Magna من", None, tess)
    assert boxes_of(built)["Leptis"] == [250, 30, 330, 50]


# ---------------------------------------------------------------- 4. punctuation and numbers in their gap


def test_a_mark_never_takes_another_mark_elsewhere_nor_a_word_its_place():
    # «،» against a ":" on the next line: before, both normalised to nothing and «الأمير» lost its match
    tess = [
        line(w("قال", 300, 380), w("الأمير", 200, 280)),
        line(w(":", 390, 395, 30, 50), w("ثم", 300, 380, 30, 50)),
    ]
    built = build_lines("قال ، الأمير ثم", None, tess)
    assert boxes_of(built)["الأمير"] == [200, 0, 280, 20]
    assert boxes_of(built)["،"] is None
    assert [b["text"] for b in built] == ["قال ، الأمير", "ثم"]


def test_a_number_takes_the_mark_tesseract_read_for_it_at_the_start_of_its_line():
    tess = [
        line(w("قال", 300, 380), w("الأمير", 200, 280)),
        line(w("(')", 385, 398, 30, 50), w("ثم", 300, 380, 30, 50), w("انتهى", 200, 280, 30, 50)),
    ]
    built = build_lines("قال الأمير (٢) ثم انتهى", None, tess)
    assert [b["text"] for b in built] == ["قال الأمير", "(٢) ثم انتهى"]
    assert boxes_of(built)["(٢)"] == [385, 30, 398, 50]


def test_a_number_at_the_end_of_a_region_is_not_a_page_number_further_down():
    tess = [line(w("قال", 300, 380), w("الأمير", 200, 280)), line(w("٨", 190, 200, 60, 80))]
    built = build_lines("قال الأمير ١٢٣", None, tess)
    assert [b["text"] for b in built] == ["قال الأمير ١٢٣"]
    # ... but a number that reads the same is found on a line of its own
    built = build_lines("قال الأمير ٨", None, tess)
    assert [b["text"] for b in built] == ["قال الأمير", "٨"]


def test_a_closing_bracket_after_the_last_word_of_a_line_stays_on_that_line():
    # book 22 page 4: «الهجري )» ends a line; the next one starts with a bracket, mirrored by Tesseract
    tess = [
        line(w("قال", 300, 380), w("الهجري)", 200, 280)),
        line(w("(", 385, 398, 30, 50), w("وبرنيق", 300, 380, 30, 50), w("هنا", 200, 280, 30, 50)),
    ]
    built = build_lines("قال الهجري ) وبرنيق هنا", None, tess)
    assert [b["text"] for b in built] == ["قال الهجري )", "وبرنيق هنا"]
    # an opening one there starts the next line
    built = build_lines("قال الهجري ( وبرنيق هنا", None, tess)
    assert [b["text"] for b in built] == ["قال الهجري", "( وبرنيق هنا"]


def test_a_mark_without_a_box_takes_the_unused_word_between_its_neighbours():
    # book 22 page 2: «،» read as "ع." (a letter), between «الناس» and «أخلاط»
    tess = [line(w("الناس", 300, 380), w("ع.", 280, 292), w("أخلاط", 200, 275), w("من", 150, 190))]
    built = build_lines("الناس ، أخلاط من", None, tess)
    assert boxes_of(built)["،"] == [280, 0, 292, 20]


def test_marks_pair_in_order_preferring_readings_and_kinds_that_agree():
    assert pair_marks([".", "،"], ["»", "."]) == [(0, 0), (1, 1)]  # the most pairs win
    assert pair_marks(["."], ["»", "."]) == [(0, 1)]  # then the reading that agrees
    assert pair_marks(["(٢)"], [".", "(')"]) == [(0, 1)]  # the shared bracket wins
    assert pair_marks(["•"], ["١٩", "©"]) == [(0, 1)]  # a mark for a mark
    assert pair_marks(["،"], [",", ","]) == [(0, 0)]  # of two alike, the earlier
    assert pair_marks([], ["."]) == [] and pair_marks(["."], []) == []


# ---------------------------------------------------------------- 6. weak boxes


def tok(t: str, bbox: list | None = None, **extra) -> dict:
    return {"t": t, "alt": None, "tess": None, "conf": "high", "digit": False, "bbox": bbox, **extra}


def test_boxes_that_overlap_are_too_tall_or_far_too_wide_are_weak():
    line_ = [tok("قال", [300, 0, 340, 20]), tok("الأمير", [230, 0, 315, 20]), tok("في", [200, 0, 220, 20])]
    assert weak_boxes(line_) == [True, True, False]  # a third of «قال» under «الأمير»
    assert weak_boxes([tok("قال", [300, 0, 340, 90])], pitch=60) == [True]
    assert weak_boxes([tok("قال", [300, 0, 340, 70])], pitch=60) == [False]
    words = [tok("كتب", [x, 0, x + 30, 20]) for x in (400, 360, 320, 280)]
    assert weak_boxes([*words, tok("في", [0, 0, 250, 20])]) == [False] * 4 + [True]
    assert weak_boxes([*words, tok("في", [210, 0, 250, 20])]) == [False] * 5
    assert weak_boxes([tok("في", [0, 0, 250, 20])]) == [False]  # too few words to know the letters' width
    assert weak_boxes([tok("،", [100, 0, 110, 20]), tok("قال", [90, 0, 140, 20])]) == [False, False]


def test_build_lines_marks_a_box_that_holds_two_printed_words_weak():
    tess = [
        line(
            *(w(t, 400 - 40 * k, 430 - 40 * k) for k, t in enumerate(["قال", "كتب", "عند", "بعد"])),
            w("xx", 0, 240),
        )
    ]
    built = build_lines("قال كتب عند بعد في", None, tess)
    tokens = {t["t"]: t for t in built[0]["tokens"]}
    assert tokens["في"]["bq"] == WEAK and "bq" not in tokens["قال"]


def test_the_numbers_pass_does_not_use_a_weak_box_as_an_area_or_a_gap_edge():
    tokens = [
        tok("قال", [300, 0, 340, 20]),
        tok("١٢", [200, 0, 290, 20], digit=True, bq=WEAK),
        tok("سنة", [150, 0, 190, 20], bq=WEAK),
        tok("٣", None, digit=True),
        tok("ثم", [50, 0, 90, 20]),
    ]
    areas = nb.number_areas(tokens, [0, 0, 400, 20])
    assert [(a.bbox, a.tokens) for a in areas] == [([90, 0, 300, 20], [1, 3])]
    assert nb.word_box(tokens[0]) == [300, 0, 340, 20] and nb.word_box(tokens[2]) is None
    letters = [
        tok("قال", [300, 0, 340, 20]),
        tok("ا", [200, 0, 220, 20], bq=WEAK),
        tok("ثم", [50, 0, 90, 20]),
    ]
    assert nb.letter_area(letters, 1, [0, 0, 400, 20]) == ([90, 0, 300, 20], "", False)
    date = [
        tok("قال", [300, 0, 340, 20]),
        tok("(هـ", [250, 0, 290, 20], bq=WEAK),
        tok("–", None),
        tok("م)", None),
    ]
    assert nb.date_area(date, 1, 3, [0, 0, 400, 20]) == [0, 0, 300, 20]
    merged = nb.replace_date(
        [tok("(هـ", [250, 0, 290, 20], bq=WEAK), tok("–", [240, 0, 248, 20]), tok("م)", [200, 0, 238, 20])],
        0,
        2,
        "(٨٤٧–٨٦١م)",
    )
    assert merged["bbox"] == [200, 0, 290, 20] and merged["bq"] == WEAK


def test_review_keeps_the_weak_mark_on_a_token():
    assert normalize_token({"t": "قال", "bbox": [0, 0, 10, 10], "bq": WEAK})["bq"] == WEAK
