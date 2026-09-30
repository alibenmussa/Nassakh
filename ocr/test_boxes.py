"""Tests of the word boxes (`ocr.alignment`, audit of book 22): Tesseract's boxes fitted to the printed
lines and clipped where they run into the word on their right, the words of an in-line gap boxed from
the Tesseract words there, punctuation and numbers matched only inside their gap, Latin-looking
misreads taken by position, weak boxes marked and read by the numbers pass only as a last resort; and
the regressions of that change found in review (page numbers, line-end dates, misread marks, bands).

Small synthetic lines: boxes are `[x0, y0, x1, y1]`, the page reads right to left.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from books.models import Book, Page
from ocr import numbers as nb
from ocr.alignment import (
    WEAK,
    Band,
    align_tokens,
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
from ocr.models import Line
from review import services as review
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


def texts(built: list[dict]) -> list[str]:
    return [b["text"] for b in built]


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


def test_a_line_keeps_to_the_band_its_words_cover_most_not_a_thin_one():
    # a thin band (a rule, a speck row) under the first line: every word covers all of its one row
    bands = [band(10, 20), band(26, 27), band(70, 80)]
    fitted = fit_lines([line(w("قال", 300, 380, 15, 28), w("في", 200, 280, 15, 28))], page_bands(bands))
    assert fitted[0]["core"] == [10, 20]
    # book 21 page 5: a fragment Tesseract made a line of its own sits on the band the line's words
    # are on; the line box stays there, not on the band its box grew over
    lines = [
        line(w("0", 390, 398, 12, 18)),
        line(w("قال", 300, 380, 5, 25), w("الأمير", 200, 280, 5, 25), w("ثم", 100, 180, 5, 85)),
    ]
    fitted = fit_lines(lines, page_bands([band(10, 20), band(70, 80)]))
    assert fitted[1]["core"] == [10, 20] and fitted[1]["bbox"][1::2] == [5, 45]


def test_a_malformed_band_is_left_out():
    tess = [line(w("قال", 300, 380), w("الأمير", 200, 280))]
    broken = [band(0, 20), {"x0": 0, "y0": 60, "x1": 400}, {"x0": "?", "y0": 0, "x1": 1, "y1": 5}]
    assert texts(build_lines("قال الأمير", None, tess, bands=broken)) == ["قال الأمير"]


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
    # boxes found by their place in the line do not count as anchored (the page's alignment flags)
    assert built[0]["n_anchored"] == 2


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


def test_latin_words_split_at_the_ink_take_their_pieces_left_to_right():
    # «Leptis Magna» between Arabic words, read by Tesseract as one word: «Leptis» is the left piece
    tess = [line(w("قال", 310, 350, 5, 35), w("Lqqq", 20, 290, 5, 35), w("انتهى", 0, 15, 5, 35))]
    gray = _inked([(310, 350), (200, 290), (20, 150), (0, 15)])
    built = build_lines("قال Leptis Magna انتهى", None, tess, bands=[band(12, 28), band(72, 88)], gray=gray)
    assert boxes_of(built)["Leptis"] == [20, 5, 150, 35] and boxes_of(built)["Magna"] == [200, 5, 290, 35]


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


def test_the_last_number_of_a_region_takes_the_page_number_line_below():
    # books 10-18: the page number «١٨» read "\\A" on the region's last line, a line of its own; D63 kept
    # it on the last text line, where it leaked into the text
    tess = [line(w("قال", 300, 380), w("الأمير", 200, 280)), line(w("\\A", 190, 205, 60, 80))]
    assert texts(build_lines("قال الأمير ١٨", None, tess)) == ["قال الأمير", "١٨"]
    # a page number at the top of a region
    tess = [line(w("Ye", 190, 205)), line(w("قال", 300, 380, 60, 80), w("الأمير", 200, 280, 60, 80))]
    assert texts(build_lines("٢٤ قال الأمير", None, tess)) == ["٢٤", "قال الأمير"]


def test_a_number_at_the_end_of_a_region_is_not_a_page_number_much_shorter_than_it():
    tess = [line(w("قال", 300, 380), w("الأمير", 200, 280)), line(w("٨", 190, 200, 60, 80))]
    assert texts(build_lines("قال الأمير ١٢٣", None, tess)) == ["قال الأمير ١٢٣"]
    # ... but a number that reads the same is found on a line of its own
    assert texts(build_lines("قال الأمير ٨", None, tess)) == ["قال الأمير", "٨"]


def test_a_number_tesseract_set_apart_on_the_same_printed_line_stays_on_it():
    # book 21 page 11: the footnote mark «(١)» is a Tesseract line of its own on the first line's band
    tess = [
        line(w("0", 390, 398, 8, 22)),
        line(w("قال", 300, 380, 5, 25), w("الأمير", 200, 280, 5, 25)),
        line(w("ثم", 300, 380, 65, 85)),
    ]
    built = build_lines("(١) قال الأمير ثم", None, tess, bands=[band(10, 20), band(70, 80)])
    assert texts(built) == ["(١) قال الأمير", "ثم"]


def test_a_date_that_ends_a_line_is_not_pulled_onto_the_next_one():
    # book 19 page 34: «… ٢٠٢١م.» ends a line and the footnote mark «٢» (read "°") starts the next;
    # D63 gave that mark to the first token of the gap, which took the date along
    tess = [
        line(w("قال", 300, 380), w("الأمير", 200, 280), w("xq", 100, 180)),
        line(w("°", 390, 398, 30, 40), w("يقول", 300, 380, 30, 50), w("السعودي", 200, 280, 30, 50)),
    ]
    built = build_lines("قال الأمير ه ٢٠٢١م. ٢ يقول السعودي", None, tess)
    assert texts(built) == ["قال الأمير ه ٢٠٢١م.", "٢ يقول السعودي"]
    assert boxes_of(built)["٢"] == [390, 30, 398, 40]


def test_a_number_that_reads_the_same_needs_no_room_after_it():
    tess = [
        line(w("قال", 300, 380), w("الأمير", 200, 280)),
        line(w("xyz", 300, 380, 30, 50), w("214", 200, 280, 30, 50)),
    ]
    assert texts(build_lines("قال الأمير ٢١٤ ص", None, tess)) == ["قال الأمير", "٢١٤ ص"]


def test_a_mark_on_a_line_between_two_anchors_does_not_split_a_run_away_from_it():
    # book 19 page 39: «(ج١، ص٢٤٠).» on a line of its own, read "Ge Ng) +¥8("; D63 gave «.22).» its
    # last word and left «(ج» on the line before
    tess = [
        line(w("المقدس»", 300, 380)),
        line(w("Ge", 300, 340, 30, 50), w("Ng)", 350, 400, 30, 50), w("+¥8(", 200, 260, 30, 50)),
        line(w("ولما", 300, 380, 60, 80), w("كان", 200, 280, 60, 80)),
    ]
    built = build_lines("المقدس» (ج ا، ص .22). ولما كان", None, tess)
    assert texts(built) == ["المقدس»", "(ج ا، ص .22).", "ولما كان"]


def test_a_mark_misread_as_another_keeps_the_two_readings_in_step():
    # book 22 page 6: «.» read "-", «وأيّاً» read "I,": D63 matched no mark, lost «وأيّاً» and moved it up
    tess = [
        line(w("الفتتح", 300, 380), w("gl", 200, 280), w("-", 180, 190)),
        line(w("I,", 330, 380, 30, 50), w("كان", 250, 320, 30, 50), w("الأمر", 150, 240, 30, 50)),
    ]
    built = build_lines("الفتح العربي . وأيّاً كان الأمر", None, tess)
    assert texts(built) == ["الفتح العربي .", "وأيّاً كان الأمر"]


def test_marks_matched_to_marks_do_not_take_the_match_of_a_word():
    # book 20 page 3: with every mark equal to every other the marks around «الأمير» match first; the
    # gap is aligned again with marks of different kinds apart
    tess = [
        line(w("قال", 300, 380), w("الأمير", 200, 280), w(":", 190, 195), w("؛", 180, 185)),
        line(w("ثم", 300, 380, 30, 50)),
    ]
    assert boxes_of(build_lines("قال . ، الأمير ثم", None, tess))["الأمير"] == [200, 0, 280, 20]


def test_an_abbreviation_and_its_number_share_the_word_tesseract_read_for_both():
    # book 19: «(ج١، ص١٠٦).» is two words to Tesseract, four tokens to Qari; the number takes the box
    # (the numbers pass reads it), the abbreviation none; D63 gave «١،» the page's box
    tess = [line(w("قال", 400, 480), w("Ne)", 300, 380), w("(Vga", 150, 290))]
    boxes = boxes_of(build_lines("قال (ج ١، ص ١٠٦).", None, tess))
    assert boxes["١،"] == [300, 0, 380, 20] and boxes["١٠٦)."] == [150, 0, 290, 20]
    assert boxes["(ج"] is None and boxes["ص"] is None


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
    assert pair_marks(["ه", "٢٠٢١م.", "٢"], ["°"]) == [(0, 0)]
    assert pair_marks(["ه", "٢٠٢١م.", "٢"], ["°"], allowed=lambda x, y: x == 2) == [(2, 0)]


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


def test_the_numbers_pass_reads_a_weak_box_only_when_the_numbers_gap_gave_it_nothing():
    # a weak box is no area and no gap edge (D63): the gap is read; a number it gave nothing is then
    # read in its own weak box, which is mostly wide (Qari read «٢٢» for «٢٠١٢»), when that box holds
    # as many numbers as the token (no whole-box reading: the box may be a neighbour's)
    tokens = [
        tok("قال", [300, 0, 340, 20]),
        tok("١٢", [200, 0, 290, 20], digit=True, bq=WEAK),
        tok("سنة", [150, 0, 190, 20], bq=WEAK),
        tok("٣", None, digit=True),
        tok("ثم", [50, 0, 90, 20]),
    ]
    areas = nb.number_areas(tokens, [0, 0, 400, 20])
    assert [(a.bbox, a.tokens) for a in areas] == [([90, 0, 300, 20], [1, 3])]
    assert nb.word_box(tokens[0]) == [300, 0, 340, 20] and nb.word_box(tokens[1]) is None
    held = SimpleNamespace(pk=7, tokens=tokens, bbox=[0, 0, 400, 20])
    requests, index = nb.page_weak_boxes(None, [held])
    assert requests == [{"id": "7:weak1", "bbox": [200, 0, 290, 20]}] and index == [(held, 1)]
    one = [["٢", 280, 288, 0.9], ["٠", 260, 268, 0.9], ["١", 240, 248, 0.9], ["٢", 220, 228, 0.9]]
    token = dict(tokens[1])
    assert nb.read_weak_box(token, one) and token["t"] == "٢٠١٢" and token["src"] == "kraken"
    assert not nb.read_weak_box(token, one)  # read already
    two = [*one[:2], ["،", 250, 256, 0.9], *one[2:]]
    assert not nb.read_weak_box(dict(tokens[1]), two)  # two numbers for one token: left as it is
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


def test_a_review_edit_that_keeps_a_box_keeps_its_weak_mark():
    old = [tok("قال", [60, 0, 100, 20], bq=WEAK), tok("الأمير", [0, 0, 50, 20])]
    edited = review.retokenize(old, "قالت الأمير")
    assert edited[0]["t"] == "قالت" and edited[0]["bbox"] == [60, 0, 100, 20] and edited[0]["bq"] == WEAK
    assert "bq" not in edited[1]
    assert "bq" not in review.typed_token("قال", None, WEAK)  # no box, no mark


@pytest.mark.django_db
def test_merging_two_words_keeps_the_weak_mark_of_either_box():
    book = Book.objects.create(title="كتاب", status=Book.Status.READY_FOR_REVIEW)
    page = Page.objects.create(
        book=book, number=1, source_index=0, status=Page.Status.OCR_DONE, text_state=Page.TextState.FINAL
    )
    tokens = [tok("هير", [60, 0, 100, 20], bq=WEAK), tok("ودوت", [30, 0, 60, 20]), tok("قال", [0, 0, 30, 20])]
    line_ = Line.objects.create(page=page, order=0, bbox=[0, 0, 100, 20], text="هير ودوت قال", tokens=tokens)
    merged = review.merge_tokens(line_, 0)
    assert merged.tokens[0]["t"] == "هيرودوت" and merged.tokens[0]["bbox"] == [30, 0, 100, 20]
    assert merged.tokens[0]["bq"] == WEAK
    clean = Line.objects.create(page=page, order=1, bbox=[0, 30, 100, 50], text="قال ثم", tokens=tokens[1:])
    assert "bq" not in review.merge_tokens(clean, 0).tokens[0]


# ---------------------------------------------------------------- 8. the first lines before the first anchor


def test_the_first_lines_before_the_first_anchor_take_the_empty_lines_and_bands_above_it():
    """Book 29 p. 20 (full-book test, 2026-09-28): Tesseract read the chapter number «– ٤ –» as garbage and
    the heading not at all, so the primary's first two lines had no anchor and were glued onto the first
    body line. The primary's own line breaks name them; the garbage line and the band no Tesseract line
    covers above the first anchored line are their places, bottom-aligned."""
    bands = [band(10, 20, 150, 250), band(70, 80, 100, 300), band(130, 140), band(190, 200)]
    tess = [
        line(w("—$-—-", 150, 250, 5, 25)),  # the chapter number as Tesseract saw it
        line(w("ابن", 320, 380, 125, 145), w("جفنة", 240, 300, 125, 145), w("بن", 180, 220, 125, 145)),
        line(w("صحابي", 300, 380, 185, 205), w("حضر", 200, 280, 185, 205)),
    ]
    built = build_lines("– ٤ –\nمعاوية بن حديج\nابن جفنة بن\nصحابي حضر", None, tess, bands=bands)
    assert texts(built) == ["– ٤ –", "معاوية بن حديج", "ابن جفنة بن", "صحابي حضر"]
    assert built[1]["bbox"] == [100, 45, 300, 105]  # the band's rows halfway to its neighbours
    assert built[0]["n_anchored"] == 0 and built[1]["n_unseen"] == 3  # Tesseract never saw them
    # no empty line or band above the first anchor: the leading tokens stay on the first line, as before
    plain = [tess[1], tess[2]]
    built = build_lines("– ٤ –\nابن جفنة بن\nصحابي حضر", None, plain, bands=bands[2:])
    assert texts(built) == ["– ٤ – ابن جفنة بن", "صحابي حضر"]
    # book 29 p. 243: the primary wrote «– ١٢١ – علي عشقر» on one line; the number has a place of its own
    # (Tesseract's «-191-»), the name the empty band below it, the body goes on as read
    named = [
        line(w("-191-", 470, 670, 30, 85)),
        line(w("من", 900, 950, 560, 600), w("الآستانة", 800, 880, 560, 600), w("واليا", 700, 780, 560, 600)),
    ]
    heading = [band(40, 60, 470, 670), band(312, 321, 486, 675), band(565, 575, 40, 1037)]
    built = build_lines("– ١٢١ – علي عشقر\nعين من الآستانة واليا", None, named, bands=heading)
    assert texts(built) == ["– ١٢١ –", "علي عشقر", "عين من الآستانة واليا"]
    # more leading lines than places: the topmost join the top place
    built = build_lines("سطر أول\n– ٤ –\nمعاوية بن حديج\nابن جفنة بن\nصحابي حضر", None, tess, bands=bands)
    assert texts(built) == ["سطر أول – ٤ –", "معاوية بن حديج", "ابن جفنة بن", "صحابي حضر"]


# ------------------------------------------------------ 9. the word pairing keeps the most words in order


def test_a_word_equal_to_a_later_word_is_not_paired_across_the_words_between():
    """Book 29 p. 243: «علي» (the name) equals «على» (the preposition) leniently; SequenceMatcher paired
    them across «من الآستانة», which then could not pair in order. The longest common subsequence keeps
    those pairs and leaves the name unpaired."""
    a = "علي عشقر عين من الآستانة واليا على طرابلس بعد حسن".split()
    b = "oe من الآستانة Ul, على طرايلس بعد حسن".split()
    pairs = dict(p for p in align_tokens(a, b) if p[0] is not None)
    assert pairs[3] == 1 and pairs[4] == 2 and pairs[6] == 4 and pairs[8] == 6 and pairs[9] == 7
    assert pairs[0] is None  # «علي» is not «على»
    # an ordinary line pairs as before (SequenceMatcher's blocks), a misread word one for one
    a = "قال الأمير في سنة ثم انتهى".split()
    b = "قال الامبر في سنة ثم انتهى".split()
    assert align_tokens(a, b) == [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 5)]
    assert align_tokens(["كلمة"], []) == [(0, None)] and align_tokens([], ["كلمة"]) == [(None, 0)]


# ---------------------------------------------------------------- D91: words of an unread line start


def test_words_left_on_the_line_above_go_to_the_unread_start_of_the_next_line(monkeypatch):
    """Book 31 p. 50: Tesseract read only the left half of a printed line; the model's words of its right
    half, with no Tesseract word to anchor them, stayed at the end of the line above. The next line's band
    has that ink unread at its start: the words go there from the model's own line break, boxed on the ink
    where it splits cleanly, and the line's box grows over it (D91)."""
    gray = np.full((120, 400), 255, dtype=np.uint8)
    for rows, blocks in (
        ((10, 30), [(320, 380), (240, 310), (180, 230), (100, 170), (20, 90)]),
        ((50, 70), [(300, 380), (200, 290), (120, 180), (20, 110)]),
        ((90, 110), [(300, 380), (200, 290)]),
    ):
        for x0, x1 in blocks:
            gray[rows[0] : rows[1], x0:x1] = 0
    tess = [
        line(
            w("قال", 320, 380, 5, 35),
            w("الأمير", 240, 310, 5, 35),
            w("في", 180, 230, 5, 35),
            w("سنة", 100, 170, 5, 35),
            w("كذا", 20, 90, 5, 35),
        ),
        line(w("وهذا", 120, 180, 45, 75), w("الكلام", 20, 110, 45, 75)),  # the right half never read
        line(w("انتهى", 300, 380, 85, 115), w("هنا", 200, 290, 85, 115)),
    ]
    bands = [band(12, 28), band(52, 68), band(92, 108)]
    text = "قال الأمير في سنة كذا\nعشرين سنة وهذا الكلام\nانتهى هنا"
    built = build_lines(text, None, tess, bands=bands, gray=gray)
    assert texts(built) == ["قال الأمير في سنة كذا", "عشرين سنة وهذا الكلام", "انتهى هنا"]
    assert built[1]["bbox"][2] == 380  # the line's box covers its whole ink
    assert boxes_of(built)["عشرين"] == [300, 45, 380, 75]
    # the rule off: the words stay at the end of the line above (as before D91)
    import ocr.alignment as alignment

    monkeypatch.setattr(alignment, "UNREAD_MIN_LETTERS", 1e9)
    assert texts(build_lines(text, None, tess, bands=bands, gray=gray))[0].endswith("كذا عشرين سنة")
