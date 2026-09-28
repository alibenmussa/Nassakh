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
CALL = [(188, 44, 194, 64), (170, 44, 179, 64), (150, 44, 156, 64)]  # «(», «١», «)» right to left
LH = 18.0


def test_the_lines_core_rows_are_where_the_ink_is_densest():
    gray = _page_image(LETTERS + CALL)
    assert 60 <= calls.core_centre(gray, [100, 30, 340, 90]) <= 78
    assert calls.core_centre(np.full((50, 50), 240, dtype=np.uint8), [0, 10, 50, 30]) == 20.0


def test_call_like_ink_forms_a_cluster_above_the_core_and_outside_the_word_boxes():
    gray = _page_image(LETTERS + CALL)
    comps = calls.components(gray, [100, 30, 340, 90])
    centre = calls.core_centre(gray, [100, 30, 340, 90])
    assert calls.call_clusters(comps, [[300, 50, 330, 78], [250, 60, 280, 78]], centre, LH) == [
        [150, 44, 194, 64]
    ]
    # inside a sure word box by more than `INSIDE`: the word's own ink
    assert calls.call_clusters(comps, [[140, 40, 200, 78]], centre, LH) == []
    # a box that ends on the cluster's ink (clipped at its neighbour) does not cover it
    assert calls.call_clusters(comps, [[194, 40, 260, 78]], centre, LH) == [[150, 44, 194, 64]]
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
    assert calls.place(tokens, [150, 44, 194, 64]) == (1, (2, 4))
    # nothing unboxed in the gap: a plain insertion after the word
    assert calls.place(tokens[:2], [150, 44, 194, 64]) == (1, (2, 2))
    # at the line's start (right edge)
    assert calls.place(tokens[:2], [350, 44, 394, 64]) == (-1, (0, 0))
    # a weak box that swallowed the call: the word's centre is still right of the cluster
    swallowed = [
        {"t": "كلام", "bbox": [300, 50, 330, 78]},
        {"t": "مخلد", "bbox": [140, 40, 280, 78], "bq": "weak"},
        {"t": "("},
        {"t": ")"},
    ]
    assert calls.place(swallowed, [150, 44, 194, 64]) == (1, (2, 4))


def test_a_cluster_among_unboxed_words_is_placed_by_the_side_their_ink_lies_on():
    tokens = [
        {"t": "فلحق", "bbox": [300, 50, 330, 78]},
        {"t": "في"},
        {"t": "رقادة،"},
        {"t": "وأغلق", "bbox": [40, 50, 100, 78]},
    ]
    words_right = [(280, 60, 250, 78), (240, 60, 200, 78)]  # the two words' ink, right of the cluster
    ink = [(250, 60, 280, 78), (200, 60, 240, 78)]
    assert calls.place(tokens, [150, 44, 194, 64], ink, LH) == (2, (3, 3))  # the call follows the run
    ink_left = [(140, 60, 120, 78)]
    assert calls.place(tokens, [250, 44, 294, 64], [(120, 60, 140, 78), (105, 60, 118, 78)], LH) == (
        0,
        (1, 1),
    )
    # a mark narrower than a line height beside the cluster is not a word: «(١) ،»
    assert calls.place(tokens, [150, 44, 194, 64], ink + [(120, 60, 132, 78)], LH) == (2, (3, 3))
    # words on both sides: not understood
    assert calls.place(tokens, [150, 44, 194, 64], ink + [(105, 60, 140, 78)], LH) is None
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
    calls.insert_call(tokens, 0, (1, 3), "1", [150, 44, 194, 64])
    assert [t["t"] for t in tokens] == ["كلام", "(١)", "بعده"]
    call = tokens[1]
    assert (
        call["call"] is True and call["src"] == "kraken" and call["conf"] == "low" and call["digit"] is True
    )
    assert call["bbox"] == [150, 44, 194, 64] and call["qari"]["t"] == "( )" and call["alt"] == "( )"
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
    save_array(pre.gray_image, _page_image(LETTERS + CALL, size=(200, 400)), "gray.png")
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
    engine = FakeKraken({(150, 44, 194, 64): "(١)"})
    done = calls.read_page_calls(page, engine=engine, style="arabic_indic")
    assert done.wanted == ["1"] and done.accepted == 1 and done.applied == 1 and done.rejected == 0
    requests = engine.calls[0][0]["lines"]
    assert [(r["bbox"], r["scale"]) for r in requests] == [([150, 44, 194, 64], k) for k in calls.SCALES]
    line.refresh_from_db()
    assert line.text == "كلام ثم (١)"
    call = line.tokens[2]
    assert call["call"] is True and call["src"] == "kraken" and call["bbox"] == [150, 44, 194, 64]
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
    engine = FakeKraken({(150, 44, 194, 64): "(١)"})
    done = calls.read_page_calls(page, engine=engine, style="arabic_indic", dry_run=True)
    assert done.accepted == 1 and done.applied == 0
    assert [c.as_dict()["status"] for c in done.candidates] == ["accepted"]
    line.refresh_from_db()
    assert line.text == "كلام ثم ( )" and not OcrRun.objects.filter(page=page).exists()


def test_a_reading_that_is_not_the_wanted_call_changes_nothing(calls_page):
    page, line, _note = calls_page
    engine = FakeKraken({(150, 44, 194, 64): "(٢)"})
    done = calls.read_page_calls(page, engine=engine, style="arabic_indic")
    assert done.accepted == 0 and done.applied == 0
    assert [c.status for c in done.candidates] == ["«2» not wanted"]
    line.refresh_from_db()
    assert line.text == "كلام ثم ( )"


def test_the_pass_leaves_approved_pages_western_books_and_reviewed_lines_alone(calls_page):
    page, line, _note = calls_page
    engine = FakeKraken({(150, 44, 194, 64): "(١)"})
    assert calls.read_page_calls(page, engine=engine, style="western").skipped == "not arabic-indic"
    Line.objects.filter(pk=line.pk).update(is_reviewed=True)
    done = calls.read_page_calls(page, engine=engine, style="arabic_indic")
    assert done.candidates == [] and not engine.calls
    Line.objects.filter(pk=line.pk).update(is_reviewed=False)
    page.reviewed_at = timezone.now()
    assert calls.read_page_calls(page, engine=engine, style="arabic_indic").skipped == "approved"
