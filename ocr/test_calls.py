"""Tests of the call pass (D83): the raised «(١)» of a footnote call read from the ink by Kraken.

The pure parts (what a page wants, the call-like ink clusters, where a cluster goes, Kraken's reading)
run on small arrays; the pass runs with a fake engine on a page drawn by hand.
"""

from __future__ import annotations

from django.utils import timezone

import numpy as np
import pytest

from books.models import Book, Page
from core.storage import save_array
from ocr import calls
from ocr.models import Line, OcrRun
from ocr.test_numbers import FakeKraken
from processing.models import Preprocess, Region

# ---------------------------------------------------------------- what a page wants


def test_a_page_wants_the_numbers_of_its_note_markers_its_body_does_not_hold():
    notes = ["(١) الأولى", "تتمة الأولى", "(٢) الثانية", "٣ - الثالثة", "ا الرابعة؟"]
    assert calls.wanted_numbers(["متن بلا علامة"], notes) == ["1", "2", "3"]
    assert calls.wanted_numbers(["متن فيه (١) و(٣)"], notes) == ["2"]
    assert calls.wanted_numbers(["متن فيه (١١)"], notes) == ["1", "2", "3"]  # «(١١)» is not (١)
    # a first note line without a marker heads the page: it wants 1
    assert calls.wanted_numbers([], ["حاشية بلا علامة", "(٢) الثانية"]) == ["1", "2"]
    assert calls.wanted_numbers([], ["حاشية بلا علامة", "(١) الأولى"]) == ["1"]
    assert calls.wanted_numbers(["متن"], []) == []
    assert calls.line_marker("١ م ه ولم ترض") == "1"  # the pass only counts markers; the linker guards years


# ---------------------------------------------------------------- the ink


def _page_image(marks: list[tuple[int, int, int, int]], size=(120, 400)) -> np.ndarray:
    """White paper with black rectangles (`x0, y0, x1, y1`)."""
    gray = np.full(size, 240, dtype=np.uint8)
    for x0, y0, x1, y1 in marks:
        gray[y0:y1, x0:x1] = 20
    return gray


# a line: core rows 60–78 (letters 30 px wide sitting on the baseline), a raised «(١)» at x 150–194
LETTERS = [(300, 50, 330, 78), (250, 60, 280, 78), (210, 40, 216, 78)]
CALL = [(188, 36, 194, 56), (170, 36, 179, 56), (150, 36, 156, 56)]  # «(», «١», «)» right to left, raised
LH = 18.0
BASELINE = (205, 72, 335, 78)


def test_the_lines_core_rows_are_where_the_ink_is_densest():
    gray = _page_image(LETTERS + CALL)
    assert 60 <= calls.core_centre(gray, [100, 30, 340, 90]) <= 78
    assert calls.core_centre(np.full((50, 50), 240, dtype=np.uint8), [0, 10, 50, 30]) == 20.0


def test_call_like_ink_forms_a_cluster_above_the_core_and_outside_the_word_boxes():
    gray = _page_image(LETTERS + CALL)
    comps = calls.components(gray, [100, 30, 340, 90])
    centre = calls.core_centre(gray, [100, 30, 340, 90])
    assert calls.call_clusters(comps, [[300, 50, 330, 78], [250, 60, 280, 78]], centre, LH) == [
        [150, 36, 194, 56]
    ]
    # inside a sure word box by more than `INSIDE`: the word's own ink
    assert calls.call_clusters(comps, [[140, 40, 200, 78]], centre, LH) == []
    # a box that ends on the cluster's ink (clipped at its neighbour) does not cover it
    assert calls.call_clusters(comps, [[194, 40, 260, 78]], centre, LH) == [[150, 36, 194, 56]]
    # sitting on the baseline (a letter), too short (a hamza), alone (one part): not a call
    low = calls.components(
        _page_image(LETTERS + [(188, 58, 194, 78), (170, 58, 179, 78), (150, 58, 156, 78)]),
        [100, 30, 340, 90],
    )
    assert calls.call_clusters(low, [], centre, LH) == []
    short = calls.components(
        _page_image(LETTERS + [(188, 50, 194, 60), (170, 50, 179, 60), (150, 50, 156, 60)]),
        [100, 30, 340, 90],
    )
    assert calls.call_clusters(short, [], centre, LH) == []
    alone = calls.components(_page_image(LETTERS + CALL[:1]), [100, 30, 340, 90])
    assert calls.call_clusters(alone, [], centre, LH) == []


def test_the_search_reaches_left_of_the_line_box_for_a_call_after_the_last_boxed_word():
    assert calls.search_box([200, 30, 340, 90], LH) == [110, 30, 340, 90]
    assert calls.search_box([50, 30, 340, 90], LH) == [0, 30, 340, 90]


def test_a_cluster_goes_after_the_boxed_word_on_its_right_and_replaces_the_glyphs_of_its_gap():
    tokens = [
        {"t": "كلام", "bbox": [300, 50, 330, 78]},
        {"t": "ثم", "bbox": [250, 60, 280, 78]},
        {"t": "("},
        {"t": ")"},
    ]
    assert calls.place(tokens, [150, 36, 194, 56]) == (1, (2, 4))
    # nothing unboxed in the gap: a plain insertion after the word
    assert calls.place(tokens[:2], [150, 36, 194, 56]) == (1, (2, 2))
    # at the line's start (right edge)
    assert calls.place(tokens[:2], [350, 36, 394, 56]) == (-1, (0, 0))
    # a weak box that swallowed the call: the word's centre is still right of the cluster
    swallowed = [
        {"t": "كلام", "bbox": [300, 50, 330, 78]},
        {"t": "مخلد", "bbox": [140, 40, 280, 78], "bq": "weak"},
        {"t": "("},
        {"t": ")"},
    ]
    assert calls.place(swallowed, [150, 36, 194, 56]) == (1, (2, 4))


def test_a_cluster_among_unboxed_words_is_placed_by_the_side_their_ink_lies_on():
    tokens = [
        {"t": "فلحق", "bbox": [300, 50, 330, 78]},
        {"t": "في"},
        {"t": "رقادة،"},
        {"t": "وأغلق", "bbox": [40, 50, 100, 78]},
    ]
    words_right = [(280, 60, 250, 78), (240, 60, 200, 78)]  # the two words' ink, right of the cluster
    ink = [(250, 60, 280, 78), (200, 60, 240, 78)]
    assert calls.place(tokens, [150, 36, 194, 56], ink, LH) == (2, (3, 3))  # the call follows the run
    ink_left = [(140, 60, 120, 78)]
    assert calls.place(tokens, [250, 36, 294, 56], [(120, 60, 140, 78), (105, 60, 118, 78)], LH) == (
        0,
        (1, 1),
    )
    # a mark narrower than a line height beside the cluster is not a word: «(١) ،»
    assert calls.place(tokens, [150, 36, 194, 56], ink + [(120, 60, 132, 78)], LH) == (2, (3, 3))
    # words on both sides: not understood
    assert calls.place(tokens, [150, 36, 194, 56], ink + [(105, 60, 140, 78)], LH) is None
    del words_right, ink_left


# ---------------------------------------------------------------- Kraken's reading


def test_a_reading_is_accepted_when_it_is_a_wanted_number_in_brackets_or_not():
    assert calls.accept("(١)", ["1", "2"], set()) == ("1", "accepted")
    assert calls.accept("(۱)", ["1"], set()) == ("1", "accepted")  # Kraken's Persian one
    assert calls.accept("(٢", ["2"], set()) == ("2", "accepted")  # a bracket lost
    assert calls.accept("١", ["1"], set()) == ("1", "accepted")
    assert calls.accept("(١١)", ["1"], set()) == ("1", "accepted")  # the «(» stroke read as a one (D82)
    assert calls.accept("۱١)", ["1"], set(), {"1"}) == ("1", "accepted")
    assert calls.accept("(١١)", ["1"], set(), {"1", "11"}) == ("", "«11» not wanted")  # a note (١١) exists
    assert calls.accept("(٢١)", ["1"], set()) == ("", "«21» not wanted")
    assert calls.accept("(١٢)", ["1"], set()) == ("", "«12» not wanted")
    assert calls.accept("(١)", ["1"], {"1"}) == ("", "«1» duplicate")
    assert calls.accept("الرحمن", ["1"], set()) == ("", "not a call")
    assert calls.accept("", ["1"], set()) == ("", "not a call")
    assert calls.accept("(١٢٣)", ["1"], set()) == ("", "not a call")


def test_the_best_formed_reading_of_the_scales_wins():
    assert calls.reading_score("(١)", "1") == 3 and calls.reading_score("(١", "1") == 2
    assert calls.reading_score("١", "1") == 1 and calls.reading_score("۱١)", "1") == 1
    assert calls.reading_score("كلام", "1") == 0
    assert calls.best_reading({1: "۱١)", 2: "(۱)"}, ["1"]) == ("(۱)", "1", "accepted", 3)
    assert calls.best_reading({1: "(١)", 2: "(١١"}, ["1"]) == ("(١)", "1", "accepted", 3)
    assert calls.best_reading({1: "(٢)", 2: "(٦)"}, ["1"]) == ("(٦)", "", "«6» not wanted", 0)
    assert calls.best_reading({1: "", 2: "الرحمن"}, ["1"]) == ("الرحمن", "", "not a call", 0)


def test_the_call_token_and_its_write_back():
    tokens = [
        {"t": "كلام", "bbox": [300, 50, 330, 78]},
        {"t": "("},
        {"t": ")"},
        {"t": "بعده", "bbox": [40, 50, 100, 78]},
    ]
    calls.insert_call(tokens, 0, (1, 3), "1", [150, 36, 194, 56])
    assert [t["t"] for t in tokens] == ["كلام", "(١)", "بعده"]
    call = tokens[1]
    assert (
        call["call"] is True and call["src"] == "kraken" and call["conf"] == "low" and call["digit"] is True
    )
    assert call["bbox"] == [150, 36, 194, 56] and call["qari"]["t"] == "( )" and call["alt"] == "( )"
    misread = [{"t": "(”)", "bbox": [207, 1037, 251, 1060], "conf": "low", "bq": "weak"}]
    calls.apply_token(misread, 0, "1")
    assert (
        misread[0]["t"] == "(١)"
        and misread[0]["bbox"] == [207, 1037, 251, 1060]
        and misread[0]["bq"] == "weak"
    )
    assert misread[0]["qari"]["t"] == "(”)"
    wrong_box = [{"t": "(١١)", "bbox": [182, 540, 267, 596], "conf": "low"}]
    calls.apply_token(wrong_box, 0, "1", [100, 540, 144, 563])  # the ink's box, not the token's
    assert wrong_box[0]["t"] == "(١)" and wrong_box[0]["bbox"] == [100, 540, 144, 563]


def test_which_boxed_tokens_are_read_again():
    markers = {"1", "2"}
    assert calls.is_misread_token({"t": "(ا)", "bbox": [1, 2, 3, 4]}, ["1"], markers)
    assert calls.is_misread_token({"t": "()", "bbox": [1, 2, 3, 4]}, ["1"], markers)
    assert calls.is_misread_token({"t": "(١١)", "bbox": [1, 2, 3, 4]}, ["1"], markers)
    assert not calls.is_misread_token({"t": "(٢)", "bbox": [1, 2, 3, 4]}, ["1"], markers)  # a note's call
    assert not calls.is_misread_token({"t": "(ا)"}, ["1"], markers)  # no box
    assert not calls.is_misread_token({"t": "(ا)", "bbox": [1, 2, 3, 4], "res": "typed"}, ["1"], markers)
    assert not calls.is_misread_token({"t": "(١)", "bbox": [1, 2, 3, 4], "call": True}, ["1"], markers)
    assert not calls.is_misread_token({"t": "(كفير)", "bbox": [1, 2, 3, 4]}, ["1"], markers)


# ---------------------------------------------------------------- the pass


@pytest.fixture
def calls_page(db):
    book = Book.objects.create(title="كتاب", status=Book.Status.OCR)
    page = Page.objects.create(
        book=book, number=1, source_index=0, status=Page.Status.OCR_DONE, text_state=Page.TextState.FINAL
    )
    pre = Preprocess.objects.create(page=page, output_width=400, output_height=200, median_line_height=LH)
    # a thin baseline under the letters: the line's dense core, as in print (D87 raises the call above it)
    save_array(pre.gray_image, _page_image(LETTERS + CALL + [BASELINE], size=(200, 400)), "gray.png")
    pre.save()
    foot = Region.objects.create(page=page, kind=Region.Kind.FOOTNOTE, bbox=[0, 120, 400, 200], order=1)
    body = [
        {
            "t": "كلام",
            "alt": None,
            "tess": None,
            "conf": "high",
            "digit": False,
            "bbox": [300, 50, 330, 78],
            "res": None,
        },
        {
            "t": "ثم",
            "alt": None,
            "tess": None,
            "conf": "high",
            "digit": False,
            "bbox": [250, 60, 280, 78],
            "res": None,
        },
        {"t": "(", "alt": None, "tess": None, "conf": "high", "digit": False, "bbox": None, "res": None},
        {"t": ")", "alt": None, "tess": None, "conf": "high", "digit": False, "bbox": None, "res": None},
    ]
    line = Line.objects.create(page=page, order=0, bbox=[100, 30, 340, 90], text="كلام ثم ( )", tokens=body)
    note = Line.objects.create(
        page=page,
        order=1,
        region=foot,
        bbox=[0, 130, 400, 160],
        text="(١) حاشية",
        tokens=[
            {"t": "(١)", "conf": "low", "digit": True, "bbox": [360, 130, 400, 160]},
            {"t": "حاشية", "conf": "high"},
        ],
    )
    return page, line, note


def test_the_pass_reads_the_call_from_the_ink_writes_the_token_and_records_the_run(calls_page):
    page, line, _note = calls_page
    engine = FakeKraken({(150, 36, 194, 56): "(١)"})
    done = calls.read_page_calls(page, engine=engine, style="arabic_indic")
    assert done.wanted == ["1"] and done.accepted == 1 and done.applied == 1 and done.rejected == 0
    requests = engine.calls[0][0]["lines"]
    assert [(r["bbox"], r["scale"]) for r in requests] == [([150, 36, 194, 56], k) for k in calls.SCALES]
    line.refresh_from_db()
    assert line.text == "كلام ثم (١)"
    call = line.tokens[2]
    assert call["call"] is True and call["src"] == "kraken" and call["bbox"] == [150, 36, 194, 56]
    page.refresh_from_db()
    assert "(1)" in page.final_text  # Western digits in the page text
    run = OcrRun.objects.get(page=page, engine_name="kraken")
    assert run.params["pass"] == "calls" and run.params["accepted"] == 1 and run.params["wanted"] == ["1"]
    assert run.input_variant == "gray_1x+2x"
    # read again: the call is there, nothing is wanted
    again = calls.read_page_calls(page, engine=engine, style="arabic_indic")
    assert again.wanted == [] and again.candidates == []


def test_a_dry_run_reads_and_reports_but_writes_nothing(calls_page):
    page, line, _note = calls_page
    engine = FakeKraken({(150, 36, 194, 56): "(١)"})
    done = calls.read_page_calls(page, engine=engine, style="arabic_indic", dry_run=True)
    assert done.accepted == 1 and done.applied == 0
    assert [c.as_dict()["status"] for c in done.candidates] == ["accepted"]
    line.refresh_from_db()
    assert line.text == "كلام ثم ( )" and not OcrRun.objects.filter(page=page).exists()


def test_the_one_call_shape_of_a_page_takes_its_one_wanted_number_whatever_kraken_read(calls_page):
    """D87: one note, one call-shaped ink: the order says which (book 31: «(١)» read «٤», «٢٤», «٥٤»)."""
    page, line, _note = calls_page
    engine = FakeKraken({(150, 36, 194, 56): "(٢)"})
    done = calls.read_page_calls(page, engine=engine, style="arabic_indic")
    assert done.accepted == 1 and done.applied == 1
    assert [c.status for c in done.candidates] == ["accepted by order"]
    line.refresh_from_db()
    assert line.text == "كلام ثم (١)"


def test_the_pass_leaves_approved_pages_western_books_and_reviewed_lines_alone(calls_page):
    page, line, _note = calls_page
    engine = FakeKraken({(150, 36, 194, 56): "(١)"})
    Line.objects.filter(pk=line.pk).update(is_reviewed=True)
    done = calls.read_page_calls(page, engine=engine, style="arabic_indic")
    assert done.candidates == [] and not engine.calls
    Line.objects.filter(pk=line.pk).update(is_reviewed=False)
    page.reviewed_at = timezone.now()
    assert calls.read_page_calls(page, engine=engine, style="arabic_indic").skipped == "approved"


def test_a_western_book_numbers_its_call_by_order_without_kraken(calls_page):
    """D87: Kraken's digits model reads Arabic-Indic digits; a book printing «(1)» has its call shapes
    numbered by order and written in its own digits."""
    page, line, _note = calls_page
    engine = FakeKraken({(150, 36, 194, 56): "(١)"})
    done = calls.read_page_calls(page, engine=engine, style="western")
    assert not engine.calls and done.accepted == 1 and done.applied == 1
    line.refresh_from_db()
    assert line.text == "كلام ثم (1)" and line.tokens[2]["call"] is True


# ---------------------------------------------------------------- D87: numbers by order and count


class _Line:
    def __init__(self, order):
        self.order, self.pk, self.tokens = order, order + 1, []


def _cand(order, x, source="ink", number="", status="not a call"):
    return calls.Candidate(_Line(order), [x, 10, x + 20, 30], source, number=number, status=status)


def test_calls_are_numbered_by_order_between_the_fixed_ones():
    # a page wanting 1 and 3 whose «(٢)» is in the text: one shape before it, one after
    first, last = _cand(0, 300), _cand(2, 100)
    assert calls.number_calls([first, last], ["1", "3"], [((1, -200.0), 2)]) == 2
    assert (first.number, last.number) == ("1", "3") and first.status == "accepted by order"
    # two shapes where one number is wanted: which one cannot be told
    a, b = _cand(0, 300), _cand(0, 100)
    assert calls.number_calls([a, b], ["1"], []) == 0 and not a.number and not b.number
    # a token (no ink shape) is never numbered by order; an unplaced shape neither
    token, unplaced = _cand(0, 300, source="token"), _cand(1, 300, status="unplaced words in the gap")
    assert calls.number_calls([token], ["1"], []) == 0 and calls.number_calls([unplaced], ["1"], []) == 0


def test_a_read_call_out_of_order_with_the_text_loses_its_number():
    # Kraken read «(٣)» before the text's «(٢)»: it cannot be 3; by order it is 1
    early = _cand(0, 300, number="3", status="accepted")
    assert calls.number_calls([early], ["1", "3"], [((1, -200.0), 2)]) == 1
    assert early.number == "1" and early.status == "accepted by order"


def test_the_gap_keeps_its_marks_around_the_call():
    """D87: «الجنة » (” .» becomes «الجنة » (١) .»; a «( )» pair is the call; a lone «)» it follows."""
    tokens = [{"t": "الجنة", "bbox": [300, 10, 350, 30]}, {"t": "»"}, {"t": "(”"}, {"t": "."}]
    calls.insert_call(tokens, 0, (1, 4), "1", [250, 10, 280, 25])
    assert [t["t"] for t in tokens] == ["الجنة", "»", "(١)", "."]
    tokens = [{"t": "فيه", "bbox": [300, 10, 350, 30]}, {"t": ")"}, {"t": ":"}]
    calls.insert_call(tokens, 0, (1, 3), "2", [250, 10, 280, 25])
    assert [t["t"] for t in tokens] == ["فيه", ")", "(٢)", ":"]
    tokens = [{"t": "ثم", "bbox": [300, 10, 350, 30]}, {"t": "("}, {"t": ")"}]
    calls.insert_call(tokens, 0, (1, 3), "1", [250, 10, 280, 25], style="western")
    assert [t["t"] for t in tokens] == ["ثم", "(1)"]


def test_a_bracketed_call_is_found_by_its_shape_sized_by_the_line_pitch():
    """Book 31 p. 50: brackets 24–25 px, 2.0× the thin core band (the D83 filter's limit 1.6), 0.24 of the
    line pitch; raised: they end above the core's top, where letters end in the core."""
    core_top = 469.0
    comps = [(235, 428, 244, 453), (258, 431, 266, 450), (280, 428, 289, 452)]  # «(» «١» «)»
    letters = [(300, 440, 314, 485), (320, 443, 327, 478)]  # a text bracket and an alef: not raised
    (box,) = calls.bracket_calls(comps + letters, [], core_top, 101.5)
    assert box == [235, 428, 289, 453]
    # D83's clusters, scaled on the 12 px core band, refused them
    assert calls.call_clusters(comps, [], (469 + 478) / 2, 12.0) == []
    # inside a word box too: Tesseract boxes «الشافعي(٢)» as one word when the models drop the call (b. 34)
    assert calls.bracket_calls(comps, [[230, 420, 295, 460]], core_top, 101.5) == [[235, 428, 289, 453]]
    # a vowel mark beside a bracket does not join the call and spoil its ends (book 34 p. 6: «المنعُ(٨)»)
    damma = (295, 433, 306, 447)  # 14 px: below `JOIN_HEIGHTS` of the 25 px brackets
    assert calls.bracket_calls([*comps, damma], [], core_top, 101.5) == [[235, 428, 289, 453]]


def test_a_word_with_the_calls_reading_glued_to_it_gives_it_to_the_call():
    """Book 34 p. 6: «المنع””؛» — the models wrote the raised «(٨)» as two quote strokes on the word."""
    tokens = [{"t": "والراجح", "bbox": [265, 10, 400, 30]}, {"t": "المنع””؛"}]
    assert calls.place(tokens, [118, 5, 160, 25], [], 13.0) == (1, (2, 2))
    calls.insert_call(tokens, 1, (2, 2), "8", [118, 5, 160, 25])
    assert [t["t"] for t in tokens] == ["والراجح", "المنع", "(٨)", "؛"]
    assert tokens[2]["qari"]["t"] == "””"


def test_what_the_models_write_for_a_call_is_parsed_with_the_texts_own_closers_and_marks():
    """D87: the forms a raised «(٢)» takes in the models' text (books 34, 35), glued to a word or alone;
    the text's own closers, marks and numbers stay the text's."""
    assert calls.call_reading('المخالفة"٢"،') == ("المخالفة", "", '"٢"', "،")
    assert calls.call_reading("الخبيثَ»٤»") == ("الخبيثَ", "»", "٤»", "")  # the quote ends, then the call
    assert calls.call_reading('به"٢٢».') == ("به", "", '"٢٢»', ".")
    assert calls.call_reading('الأصول»"ا".') == ("الأصول", "»", '"ا"', ".")
    assert calls.call_reading('"الموطأ""') == ('"الموطأ', '"', '"', "")  # the title's quote, then the call
    for text in ["(يؤذيهما)", '"الموطأ"', "«الموطأ»", "قالا»", "ص٢٣)", "كتاب١١", "سنة٣٩٣", "٢٣]", "فيه)"]:
        assert calls.call_reading(text) is None, text
    assert calls.is_reading('"٣"') and calls.is_reading("(”") and calls.is_reading("(١١)")
    assert not calls.is_reading("»") and not calls.is_reading(".") and not calls.is_reading("٢٣]")
    assert calls.split_reading('"٣"،') == ("", '"٣"', "،") and calls.split_reading("»٤»") == ("»", "٤»", "")


def test_the_call_takes_the_place_of_its_reading_and_leaves_the_texts_marks():
    """Book 34 p. 1: «المخالفة"٢"،», «"٣"،» and «»٤»» were the models' readings of the calls: each becomes
    the call, the comma and the closing quote stay (D87)."""
    tokens = [
        {"t": "مفهوم", "bbox": [1100, 10, 1200, 30]},
        {"t": 'المخالفة"٢"،', "bbox": [892, 10, 1086, 30]},
    ]
    calls.insert_call(tokens, 1, (2, 2), "2", [911, 5, 952, 25])
    assert [t["t"] for t in tokens] == ["مفهوم", "المخالفة", "(٢)", "،"]
    assert tokens[2]["qari"]["t"] == '"٢"'
    tokens = [{"t": "وطوله", "bbox": [742, 10, 788, 30]}, {"t": '"٣"،', "bbox": [645, 10, 742, 30]}]
    calls.apply_token(tokens, 1, "3", [645, 5, 686, 25])
    assert [t["t"] for t in tokens] == ["وطوله", "(٣)", "،"]
    tokens = [{"t": "الخبيثَ", "bbox": [150, 10, 272, 30]}, {"t": "»٤»", "bbox": [94, 10, 150, 30]}]
    calls.apply_token(tokens, 1, "5", [94, 5, 136, 25])
    assert [t["t"] for t in tokens] == ["الخبيثَ", "»", "(٥)"]


def test_as_many_call_shapes_as_notes_are_numbered_by_order_whatever_kraken_read():
    """Book 35 p. 2: five shapes, five notes; Kraken read «(٢)» as «۶» (a «٤» to it) and «(٣)» as «٢»
    (D87). With a note number lost the counts differ: the readings that agree with the order keep their
    numbers and the shapes between them stay unnumbered when they outnumber the numbers left."""

    def shapes():
        out = []
        for k, reading in enumerate(["(١)", "(۶)", "(٢)", "(٤)", "(٥)"]):
            cand = _cand(k, 300)
            cand.readings = {1: reading, 2: reading}
            out.append(cand)
        return out

    page = shapes()
    assert calls.number_calls(page, ["1", "2", "3", "4", "5"], []) == 5
    assert [c.number for c in page] == ["1", "2", "3", "4", "5"]
    assert [c.status for c in page] == [
        "accepted",
        "accepted by order",
        "accepted by order",
        "accepted",
        "accepted",
    ]
    lost = shapes()
    assert calls.number_calls(lost, ["1", "3", "4", "5"], []) == 3
    assert [c.number for c in lost] == ["1", "", "", "4", "5"]
