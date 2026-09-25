"""Tests of the numbers pass (D50): Kraken reads the Arabic-Indic numbers of a finalised page.

The pure parts (`ocr.numbers`: the book's digits, the areas, matching Kraken's digit runs, the token)
and the runner's helpers run without Kraken; the pass runs with a fake engine that answers each area
from a table, and the real engine only on its error path (the project's Python has no Kraken).
"""

from __future__ import annotations

import sys

from django.test import override_settings

import numpy as np
import pytest

from books.models import Book, Page
from core.storage import save_array
from ocr import numbers as nb
from ocr.engines import kraken_runner
from ocr.engines.kraken import KrakenEngine, KrakenError
from ocr.models import Line, OcrRun
from processing.models import Preprocess

# ---------------------------------------------------------------- the book's printed digits


def tok(t, alt=None, bbox=None, **extra):
    return {
        "t": t,
        "alt": alt,
        "tess": None,
        "conf": "low",
        "digit": True,
        "bbox": bbox,
        "res": None,
        **extra,
    }


def test_a_book_printing_arabic_indic_digits_is_told_from_qaris_readings():
    # the secondary model writes them Arabic-Indic (90–100 % in such books) ...
    arabic = [tok("1328", alt="٢٢٢٩"), tok("91328هـ", alt="٩٢٢ه"), tok("28", alt="٢٣")]
    assert nb.style_of(nb.digit_counts(arabic)) == nb.ARABIC_INDIC
    # ... or the primary does often enough
    primary = [tok("١٢٣٤"), tok("2020"), tok("٥٦٧")]
    assert nb.style_of(nb.digit_counts(primary)) == nb.ARABIC_INDIC
    western = [tok("1938", alt="1938"), tok("1964م"), tok("(726هـ)", alt="(726ه)")]
    assert nb.style_of(nb.digit_counts(western)) == nb.WESTERN
    assert nb.style_of(nb.digit_counts([tok("12")])) == ""  # too few digits to tell
    words = [{"t": "كتاب", "alt": "١٢٣", "digit": False}]  # only number tokens count
    assert nb.digit_counts(words) == (0, 0, 0, 0)


# ---------------------------------------------------------------- where the numbers are


def test_areas_are_the_word_box_or_the_gap_between_boxed_neighbours():
    # right to left: token 0 is the rightmost word
    tokens = [
        {"t": "ولد", "bbox": [900, 10, 980, 40]},
        {"t": "سنة", "bbox": [800, 12, 880, 40]},
        tok("٢٧٩هـ", bbox=[700, 14, 780, 38]),
        {"t": "في", "bbox": [640, 12, 690, 40]},
        tok("١٩٦٨م"),  # no box: between «في» (x0 640) on its right and «بغداد» (x1 590) on its left
        tok("٣٠٩هـ"),  # same gap
        {"t": "بغداد", "bbox": [500, 10, 590, 40]},
        tok("٤"),  # no box, nothing boxed on its left: up to the line's edge
    ]
    areas = nb.number_areas(tokens, [100, 5, 1000, 45])
    assert [(a.bbox, a.tokens) for a in areas] == [
        ([700, 14, 780, 38], [2]),
        ([590, 5, 640, 45], [4, 5]),
        ([100, 5, 500, 45], [7]),
    ]


# ---------------------------------------------------------------- Kraken's digits for them


def chars(text, x=0):
    return [[c, float(x + i), float(x + i + 1), 0.9] for i, c in enumerate(text)]


def test_kraken_numbers_go_to_the_areas_numbers_in_order_when_the_counts_agree():
    area = [tok("(٢٢٢٢–٢٢٢٢هـ)."), tok("٢٢")]
    assert nb.assign(area, chars("(٣٢٠–٣٢٢هـ). ٣٣٢")) == [["٣٢٠", "٣٢٢"], ["٣٣٢"]]
    # stray spaces inside a number are closed up when that makes the counts agree
    assert nb.assign([tok("٢٠٠٠م.")], chars("٢٠ ٠ ٠م.")) == [["٢٠٠٠"]]
    # counts that cannot agree: Qari's reading stays
    assert nb.assign([tok("١٢")], chars("١٢ و ٣٤")) is None
    assert nb.box_text(chars("ص۲۳- ۲٤).")) == "ص۲۳- ۲٤)." and nb.box_text(chars("قال")) == ""


# ---------------------------------------------------------------- the token


def test_the_token_takes_krakens_digits_and_keeps_qaris_brackets_and_letters():
    token = tok("(9هـ/1241م)،", alt="(943-1543هـ/199م)،", tess="(@YV4")
    assert nb.apply_reading(token, ["345", "956"])
    assert token["t"] == "(٣٤٥هـ/٩٥٦م)،"  # Kraken's digits, written Arabic-Indic; the rest is Qari's
    assert token["src"] == "kraken" and token["alt"] is None and token["tess"] is None
    assert token["qari"] == {"t": "(9هـ/1241م)،", "alt": "(943-1543هـ/199م)،", "tess": "(@YV4"}
    assert token["conf"] == "low" and token["digit"] is True  # the reviewer confirms it (D17)
    assert not nb.apply_reading(token, ["1"])  # read once
    # Qari held a fragment of the box: the token takes Kraken's reading of the whole box
    fragment = tok("١،", bbox=[1, 2, 3, 4])
    assert nb.apply_reading(fragment, whole="ص۲۳- ۲٤).") and fragment["t"] == "ص٢٣- ٢٤)."
    resolved = tok("١٢", res="typed")
    assert not nb.apply_reading(resolved, ["٣٤"]) and resolved["t"] == "١٢"  # the reviewer's word stays
    mismatch = tok("١٢ و ٣٤")
    assert not nb.apply_reading(mismatch, ["٥"]) and "src" not in mismatch


# ---------------------------------------------------------------- the runner's helpers


def test_runner_pads_lines_and_puts_character_positions_in_page_pixels():
    assert kraken_runner.padded([100, 50, 300, 90], 1000, 1000) == (88, 38, 312, 102)
    assert kraken_runner.padded([0, 0, 20, 10], 15, 8) == (0, 0, 15, 8)  # clamped to the page
    cuts = [[(10, 0), (14, 30), (12, 5)], [(20, 0), (26, 30)]]
    assert kraken_runner.char_rows("١٢", cuts, [0.9, 0.8], dx=100) == [
        ["١", 110.0, 114.0, 0.9],
        ["٢", 120.0, 126.0, 0.8],
    ]
    assert kraken_runner.char_rows("١", None, None, dx=0) == [["١", None, None, 0.0]]


# ---------------------------------------------------------------- the pass


class FakeKraken:
    """Answers each area from `table` ({bbox tuple: text}), characters one pixel wide."""

    name = "kraken"
    model_id = "kraken:fake.mlmodel"
    model_revision = "test"
    backend = "cpu"

    def __init__(self, table):
        self.table = table
        self.calls = []

    def read(self, pages):
        self.calls.append(pages)
        lines = []
        for page in pages:
            for line in page["lines"]:
                text = self.table.get(tuple(line["bbox"]), "")
                lines.append({"id": line["id"], "text": text, "chars": chars(text, line["bbox"][0])})
        return {"ok": True, "lines": lines, "elapsed": 0.05}


@pytest.fixture
def numbers_page(db):
    book = Book.objects.create(title="كتاب", status=Book.Status.OCR)
    page = Page.objects.create(
        book=book, number=1, source_index=0, status=Page.Status.OCR_DONE, text_state=Page.TextState.FINAL
    )
    pre = Preprocess.objects.create(page=page, output_width=100, output_height=100)
    save_array(pre.gray_image, np.full((100, 100), 230, dtype=np.uint8), "gray.png")
    pre.save()
    body = [
        {"t": "توفي", "bbox": [80, 10, 95, 20], "conf": "high"},
        tok("(٢٢٢٢هـ)", alt="(٢٢٢ه)", bbox=[60, 10, 75, 20]),
        {"t": "وولد", "bbox": [40, 10, 55, 20], "conf": "high"},
        tok("٢٢٢٢", alt="٢٢٢٢"),  # no box: the gap between «وولد» (x0 40) and the line's left edge
    ]
    line = Line.objects.create(page=page, order=0, bbox=[0, 8, 100, 22], text="", tokens=body, n_low=2)
    reviewed = Line.objects.create(
        page=page,
        order=1,
        bbox=[0, 30, 100, 44],
        text="١٢",
        tokens=[tok("١٢", alt="١٣", bbox=[50, 30, 60, 44])],
        is_reviewed=True,
        n_low=1,
    )
    return page, line, reviewed


def test_the_pass_reads_the_numbers_of_unreviewed_lines_and_records_the_run(numbers_page):
    page, line, reviewed = numbers_page
    engine = FakeKraken({(60, 10, 75, 20): "(٣٣٤هـ)", (0, 8, 40, 22): "٣٢٢"})
    done = nb.read_page_numbers(page, engine=engine, style=nb.ARABIC_INDIC)
    assert (done.areas, done.applied) == (2, 2)
    line.refresh_from_db()
    assert [t["t"] for t in line.tokens] == ["توفي", "(٣٣٤هـ)", "وولد", "٣٢٢"]
    assert line.text == "توفي (٣٣٤هـ) وولد ٣٢٢" and line.n_low == 2  # still to confirm
    assert all(t.get("src") == "kraken" for t in line.tokens if t.get("digit"))
    reviewed.refresh_from_db()
    assert reviewed.tokens[0]["t"] == "١٢" and "src" not in reviewed.tokens[0]  # reviewed lines stay
    page.refresh_from_db()
    assert "(334هـ)" in page.final_text  # the page text follows (Western digits, D6)
    run = OcrRun.objects.get(page=page, engine_name="kraken")
    assert run.params == {"areas": 2, "applied": 2, "style": nb.ARABIC_INDIC}
    # a second pass finds nothing left to read
    again = nb.read_page_numbers(page, engine=engine, style=nb.ARABIC_INDIC)
    assert (again.areas, again.applied) == (0, 0) and len(engine.calls) == 1


def test_the_pass_leaves_western_books_and_approved_pages_alone(numbers_page):
    page, line, _reviewed = numbers_page
    engine = FakeKraken({})
    assert nb.read_page_numbers(page, engine=engine, style=nb.WESTERN).skipped == "not arabic-indic"
    from django.utils import timezone

    page.reviewed_at = timezone.now()
    page.save(update_fields=["reviewed_at"])
    assert nb.read_page_numbers(page, engine=engine, style=nb.ARABIC_INDIC).skipped == "approved"
    assert engine.calls == [] and not OcrRun.objects.filter(engine_name="kraken").exists()


def test_the_book_style_counts_qaris_readings_of_numbers_kraken_already_read(numbers_page):
    page, line, _reviewed = numbers_page
    # the style is Qari's: a number Kraken read counts with the readings kept under `qari`
    line.tokens = [tok("٣٣٤", src="kraken", qari={"t": "1328", "alt": "1328", "tess": None})] * 3
    line.save(update_fields=["tokens"])
    Line.objects.filter(pk=_reviewed.pk).delete()
    assert nb.book_style(page.book) == nb.WESTERN


# ---------------------------------------------------------------- scheduling and the engine


def test_scheduling_follows_the_setting_and_needs_kraken(
    numbers_page, django_capture_on_commit_callbacks, monkeypatch
):
    from django.conf import settings

    from ocr import tasks

    page = numbers_page[0]
    queued = []
    monkeypatch.setattr(tasks.read_numbers, "delay", lambda page_id: queued.append(page_id))
    with django_capture_on_commit_callbacks(execute=True):
        nb.schedule(page)  # tests: the pass is off
    assert queued == []
    with override_settings(NASSAKH={**settings.NASSAKH, "NUMBERS_PASS": True}):
        monkeypatch.setattr(KrakenEngine, "is_prepared", lambda self: True)
        with django_capture_on_commit_callbacks(execute=True):
            nb.schedule(page)
    assert queued == [page.pk]


def test_the_engine_reports_a_missing_setup_and_a_runner_failure(tmp_path):
    missing = KrakenEngine(python=tmp_path / "none", model=tmp_path / "none.mlmodel")
    assert not missing.is_prepared()
    with pytest.raises(KrakenError, match="make kraken"):
        missing.read([])
    model = tmp_path / "model.mlmodel"
    model.write_bytes(b"x")
    # the project's own Python has no Kraken: the runner answers ok: false and the engine raises
    engine = KrakenEngine(python=sys.executable, model=model)
    with pytest.raises(KrakenError, match="kraken"):
        engine.read([{"image": str(model), "lines": []}])


def test_review_payload_and_book_page_readings_carry_krakens_label():
    from editor.uncertain import _readings
    from review.services import normalize_token

    assert "src" not in normalize_token({"t": "١٢"})  # stored tokens stay as they were
    token = tok("(٣٣٤هـ)", src="kraken")
    assert normalize_token(token)["src"] == "kraken"
    labels = {"primary": "Qari v0.3", "secondary": "Qari v0.2", "tess": "Tesseract"}
    assert _readings(token, "(٣٣٤هـ)", labels) == [
        {"engine": "primary", "label": "Kraken", "text": "(٣٣٤هـ)", "current": True}
    ]
