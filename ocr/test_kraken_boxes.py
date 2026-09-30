"""Tests of the word boxes from Kraken (D92, `ocr.boxes`): Kraken's characters shaped into Tesseract's
lines (words split at spaces and punctuation, boundaries snapped to the blank columns, boxes trimmed to the
ink), the printed lines read (bands, and the Tesseract lines no band holds), the line builder taking its boxes
from Kraken and its readings from Tesseract, and the page pass: the stored run reused, Tesseract's boxes when
Kraken is off, missing or failing, or when the region's text is Tesseract's own.

A page drawn by hand: white paper, each word a black rectangle; the fake Kraken places each character inside
the inner part of its word, as the real one does (its positions are narrower than the glyphs).
"""

from __future__ import annotations

from django.conf import settings
from django.test import override_settings

import numpy as np
import pytest

from books.models import Book, Page
from core.storage import save_array
from ocr import boxes, services
from ocr.alignment import build_lines
from ocr.engines import registry
from ocr.engines.kraken import KrakenError
from ocr.models import OcrRun
from processing.models import Preprocess, Region

W, H = 400, 160
PAPER, INK = 240, 20

# two printed lines (core rows 30–44 and 90–104), words right to left: (text, x0, x1)
LINE_A = [("قال", 300, 370), ("الأمير", 180, 280), (":", 160, 166), ("نعم", 60, 140)]
LINE_B = [("هذا", 290, 370), ("سطر", 190, 270), ("ثان", 60, 170)]
CORE_A, CORE_B = (30, 44), (90, 104)
ASCENDER = (190, 22, 194, 30)  # «الأمير»'s alef, above its core
BANDS = [
    {"x0": 60, "y0": CORE_A[0], "x1": 370, "y1": CORE_A[1]},
    {"x0": 60, "y0": CORE_B[0], "x1": 370, "y1": CORE_B[1]},
]
TEXT = "قال الأمير : نعم\nهذا سطر ثان"


def draw(words_by_core: list[tuple[list[tuple[str, int, int]], tuple[int, int]]], extra=()) -> np.ndarray:
    gray = np.full((H, W), PAPER, dtype=np.uint8)
    for words, (y0, y1) in words_by_core:
        for text, x0, x1 in words:
            top = y0 + 4 if text == ":" else y0
            gray[top:y1, x0:x1] = INK
    for x0, y0, x1, y1 in extra:
        gray[y0:y1, x0:x1] = INK
    return gray


def page_image() -> np.ndarray:
    return draw([(LINE_A, CORE_A), (LINE_B, CORE_B)], [ASCENDER])


def kraken_chars(words: list[tuple[str, int, int]], conf: float = 0.9, drift: int = 0) -> list[list]:
    """Kraken's rows for a line: each word's characters spread over the inner 60 % of its ink (shifted by
    `drift`), a space between words, in reading order."""
    rows: list[list] = []
    for n, (text, x0, x1) in enumerate(words):
        if n:
            rows.append([" ", float(x1 + 2), float(x1 + 4), 0.99])
        inner0, inner1 = x0 + 0.2 * (x1 - x0) + drift, x1 - 0.2 * (x1 - x0) + drift
        step = (inner1 - inner0) / len(text)
        for k, ch in enumerate(text):  # the first character is the rightmost
            right = inner1 - k * step
            rows.append([ch, round(right - step, 1), round(right, 1), conf])
    return rows


class LineKraken:
    """Answers each crop with the line whose core it holds; counts its calls."""

    name = "kraken"
    model_id = "kraken:fake.mlmodel"
    model_revision = "test"
    backend = "cpu"

    def __init__(self, lines: dict[tuple[int, int], list[list]], fail: bool = False):
        self.lines = lines
        self.fail = fail
        self.calls: list = []

    def read(self, pages):
        self.calls.append(pages)
        if self.fail:
            raise KrakenError("Kraken crashed")
        out = []
        for page in pages:
            for line in page["lines"]:
                _x0, lo, _x1, hi = line["bbox"]
                chars = next((c for (y0, y1), c in self.lines.items() if lo <= y0 and y1 <= hi), [])
                text = "".join(str(row[0]) for row in chars)
                out.append({"id": line["id"], "text": text, "chars": chars})
        return {"ok": True, "lines": out, "elapsed": 0.2}


def fake_kraken(**kwargs) -> LineKraken:
    return LineKraken({CORE_A: kraken_chars(LINE_A), CORE_B: kraken_chars(LINE_B)}, **kwargs)


# ---------------------------------------------------------------- the words


def texts(chars: list[list]) -> list[tuple[str, bool]]:
    return [(boxes.word_text(p.rows), p.glued) for p in boxes.split_words(chars)]


def flat(text: str) -> list[list]:
    return [[ch, float(i), float(i + 1), 0.8] for i, ch in enumerate(text)]


def test_words_split_at_spaces_and_punctuation_and_numbers_keep_their_brackets():
    assert texts(flat("عنها: قال")) == [("عنها", False), (":", False), ("قال", False)]
    assert texts(flat("[قالت](۱)")) == [("[", False), ("قالت", False), ("]", False), ("(۱)", False)]
    assert texts(flat("(١).")) == [("(١)", False), (".", False)]
    assert texts(flat("١٩٦٦م.")) == [("١٩٦٦م", False), (".", False)]  # letters stay with their digits
    assert texts(flat("«الموطأ»")) == [("«", False), ("الموطأ", False), ("»", False)]
    # a number laid out as one left-to-right run is one word (Kraken's positions inside it are permuted) ...
    assert texts(flat("(٤١٠/١")) == [("(٤١٠/١", False)]
    assert texts(flat("(274/1).")) == [("(274/1)", False), (".", False)]
    assert texts(flat("1966-1967م")) == [("1966-1967م", False)]
    # ... a dash between Arabic-Indic digits parts two runs: the dash and the digits after it may continue it
    assert texts(flat("٢٢-٣٠٨")) == [("٢٢", False), ("-", True), ("٣٠٨", True)]


def test_a_word_s_confidence_is_its_characters_mean_as_a_percentage():
    assert boxes.word_conf([["ق", 0, 1, 0.9], ["ا", 1, 2, 0.8], ["ل", 2, 3, 0.7]]) == 80.0
    assert boxes.word_conf([]) == 0.0


# ---------------------------------------------------------------- the boxes


def crop_a() -> boxes.Crop:
    return boxes.Crop(60, CORE_A[0], 370, CORE_A[1], 0, 67)


def test_kraken_s_narrow_positions_snap_to_the_blank_columns_and_trim_to_the_ink():
    gray = page_image()
    line = boxes.kraken_line(kraken_chars(LINE_A, drift=6), crop_a(), gray)
    assert [w["text"] for w in line["words"]] == ["قال", "الأمير", ":", "نعم"]
    assert [w["bbox"] for w in line["words"]] == [
        [300, 30, 370, 44],
        [180, 22, 280, 44],  # the alef's rows too
        [160, 34, 166, 44],  # the colon is a word of its own, in its own ink
        [60, 30, 140, 44],
    ]
    assert all(w["conf"] == 90.0 for w in line["words"])
    assert line["bbox"] == [60, 22, 370, 44]


def test_a_word_s_rows_leave_out_the_ascender_of_the_line_below():
    gray = draw([(LINE_A, CORE_A), (LINE_B, CORE_B)], [(100, 60, 104, 90)])  # «ثان»'s tall letter
    line = boxes.kraken_line(kraken_chars(LINE_A), crop_a(), gray)
    assert line["words"][-1]["bbox"] == [60, 30, 140, 44]


def test_a_number_read_without_its_spaces_is_one_word_only_when_its_ink_is_tight():
    tight = [("٢٢", 330, 370), ("-", 320, 328), ("٣٠٨", 250, 318)]
    spaced = [("٢٢", 330, 370), ("-", 300, 312), ("٣٠٨", 220, 290)]
    for words, expected in (
        (tight, [("٢٢-٣٠٨", [250, 30, 370, 44])]),
        (spaced, [("٢٢", [330, 30, 370, 44]), ("-", [300, 30, 312, 44]), ("٣٠٨", [220, 30, 290, 44])]),
    ):
        gray = draw([(words, CORE_A)])
        chars = [row for row in kraken_chars(words) if row[0] != " "]  # Kraken dropped the spaces
        line = boxes.kraken_line(chars, boxes.Crop(220, 30, 370, 44, 0, 67), gray)
        assert [(w["text"], w["bbox"]) for w in line["words"]] == expected


def test_a_mark_read_without_a_space_joins_its_word_where_the_print_glues_it():
    glued = [("القرآن", 250, 370), (".", 243, 247), ("(", 190, 196), ("قالت", 120, 186), (")", 112, 118)]
    spaced = [("عنها", 250, 370), (":", 215, 222), ("نعم", 120, 190)]
    for words, expected in (
        (glued, [("القرآن.", [243, 30, 370, 44]), ("(قالت)", [112, 30, 196, 44])]),
        (spaced, [("عنها", [250, 30, 370, 44]), (":", [215, 34, 222, 44]), ("نعم", [120, 30, 190, 44])]),
    ):
        gray = draw([(words, CORE_A)])
        chars = kraken_chars(words)
        chars = [row for n, row in enumerate(chars) if row[0] != " " or n == 0]  # Kraken read no space
        if words is glued:  # ... but for the one between the two words
            chars.insert(next(n for n, row in enumerate(chars) if row[0] == "("), [" ", 200.0, 202.0, 0.99])
        line = boxes.kraken_line(chars, boxes.Crop(100, 30, 370, 44, 0, 67), gray)
        assert [(w["text"], w["bbox"]) for w in line["words"]] == expected


def test_an_ornament_s_lone_letters_are_no_line():
    gray = page_image()
    assert boxes.kraken_line([["چ", 200.0, 201.0, 0.94]], crop_a(), gray) is None
    assert boxes.kraken_line(flat("ر ن"), crop_a(), gray) is None
    assert boxes.kraken_line([], crop_a(), gray) is None
    assert boxes.kraken_line([["*", 200.0, 204.0, 0.9]], crop_a(), gray) is not None  # a separator stays


def test_a_line_s_first_and_last_words_stop_short_of_the_page_s_rule():
    gray = draw([(LINE_A, CORE_A)], [(45, 0, 48, H), (385, 0, 388, H)])  # rules 12 and 15 px away
    words = boxes.kraken_line(kraken_chars(LINE_A), crop_a(), gray)["words"]
    assert words[0]["bbox"][2] == 370 and words[-1]["bbox"][0] == 60
    assert words[0]["bbox"][1] == 30 and words[-1]["bbox"][1] == 30  # the rules' rows are not the words'


# ---------------------------------------------------------------- the lines read


def tess_line(words: list[tuple[str, int, int]], core: tuple[int, int], shift: int = 0, **extra) -> dict:
    out = [
        {"text": t, "bbox": [x0 + shift, core[0] - 4, x1 + shift, core[1] + 4], "conf": 90.0}
        for t, x0, x1 in words
    ]
    return {
        "bbox": [min(w["bbox"][0] for w in out), core[0] - 4, max(w["bbox"][2] for w in out), core[1] + 4],
        "words": out,
        **extra,
    }


def test_the_lines_read_are_the_bands_and_the_tesseract_lines_no_band_holds():
    gray = draw([(LINE_A, CORE_A), (LINE_B, CORE_B), ([("انتهى", 300, 370)], (130, 144))])
    lines = [
        tess_line(LINE_A, CORE_A),
        tess_line(LINE_B, CORE_B),
        tess_line([("انتهى", 300, 370)], (130, 144)),
    ]
    crops = boxes.region_crops(BANDS, lines, [0, 0, W, H], gray)
    assert [(c.y0, c.y1, c.lo, c.hi, c.line, c.rows) for c in crops] == [
        (
            30,
            44,
            5,
            67,
            False,
            (26, 48),
        ),  # half a pitch (50 px) above the core at most, halfway to the next line
        (90, 104, 67, 117, False, (86, 108)),
        (130, 144, 117, 169, True, (117, 169)),  # the short line the band detector missed, from its own ink
    ]
    assert [(c.x0, c.x1) for c in crops] == [(60, 370), (60, 370), (300, 370)]
    # a band holding two Tesseract lines, far taller than the others, is those lines
    tall = [BANDS[0], {"x0": 60, "y0": 90, "x1": 370, "y1": 144}, {"x0": 60, "y0": 170, "x1": 370, "y1": 184}]
    gray = draw([(LINE_A, CORE_A), (LINE_B, CORE_B), ([("انتهى", 300, 370)], (130, 144))])
    crops = boxes.region_crops(tall, lines, [0, 0, W, 200], gray)
    assert [(c.y0, c.y1) for c in crops] == [(30, 44), (90, 104), (130, 144), (170, 184)]
    # the region's columns bound the line's
    assert {(c.x0, c.x1) for c in boxes.region_crops(BANDS, [], [100, 0, 300, H], gray)} == {(100, 300)}


def test_a_printed_rule_is_no_line_and_the_line_it_held_is_read_on_its_own():
    rule = {"x0": 60, "y0": 120, "x1": 370, "y1": 123}  # a footnote rule the band detector took for a line
    gray = draw(
        [(LINE_A, CORE_A), (LINE_B, CORE_B), ([("حاشية", 250, 370)], (140, 154))], [(60, 120, 370, 123)]
    )
    note = tess_line([("حاشية", 250, 370)], (140, 154))
    note["bbox"][1] = 118  # Tesseract's box reaches over the rule: it is fitted to the rule's band
    crops = boxes.region_crops([*BANDS, rule], [tess_line(LINE_A, CORE_A), note], [0, 0, W, 200], gray)
    assert boxes.is_rule(rule, gray, W) and not boxes.is_rule(BANDS[0], gray, W)
    word = {"x0": 300, "y0": 140, "x1": 370, "y1": 154}  # a line of one word, its baseline inked end to end
    assert not boxes.is_rule(word, gray, W)
    assert [(c.y0, c.y1, c.line) for c in crops] == [(30, 44, False), (90, 104, False), (140, 154, True)]


def test_a_tesseract_line_on_a_band_s_printed_line_is_not_read_as_a_line_of_its_own():
    missed = [("انتهى", 300, 370), ("هنا", 200, 280)]
    gray = draw([(LINE_A, CORE_A), (LINE_B, CORE_B), (missed, (140, 154))], [(250, 72, 262, 84)])
    call = tess_line([("(1)", 250, 262)], (72, 84))  # a raised call above line B's core: Tesseract's own line
    halves = [tess_line([("انتهى", 300, 370)], (140, 154)), tess_line([("هنا", 200, 280)], (140, 154))]
    lines = [tess_line(LINE_A, CORE_A), call, tess_line(LINE_B, CORE_B), *halves]
    crops = boxes.region_crops(BANDS, lines, [0, 0, W, 200], gray)
    # the call is line B's; the two halves of the missed last line are one line
    assert [(c.y0, c.y1, c.x0, c.x1, c.line) for c in crops] == [
        (30, 44, 60, 370, False),
        (90, 104, 60, 370, False),
        (140, 154, 200, 370, True),
    ]


def test_the_line_builder_takes_kraken_s_boxes_and_tesseract_s_readings():
    gray = page_image()
    tesseract = [
        tess_line([("قال", 300, 370), ("الامير", 180, 280), ("بعم", 60, 140)], CORE_A, shift=12),
        tess_line(LINE_B, CORE_B, shift=12),
    ]
    tesseract[0]["words"][2]["conf"] = 88.0
    kraken = [boxes.kraken_line(kraken_chars(LINE_A), crop_a(), gray)]
    kraken.append(boxes.kraken_line(kraken_chars(LINE_B), boxes.Crop(60, 90, 370, 104, 67, 134), gray))
    built = build_lines(TEXT, TEXT, tesseract, BANDS, gray, box_lines=kraken)
    today = build_lines(TEXT, TEXT, tesseract, BANDS, gray)
    assert [b["text"] for b in built] == [b["text"] for b in today] == ["قال الأمير : نعم", "هذا سطر ثان"]
    first = built[0]["tokens"]
    assert [t["bbox"] for t in first] == [
        [300, 30, 370, 44],
        [180, 22, 280, 44],
        [160, 34, 166, 44],
        [60, 30, 140, 44],
    ]
    assert today[0]["tokens"][0]["bbox"] == [312, 26, 382, 48]  # Tesseract's own, shifted
    # the readings are Tesseract's: «نعم» read «بعم» at 88, the colon Tesseract never read
    assert first[3]["tess"] == "بعم" and first[3]["tc"] == 88.0
    assert "tc" not in first[2]
    assert [{k: v for k, v in t.items() if k != "bbox"} for t in first] == [
        {k: v for k, v in t.items() if k != "bbox"} for t in today[0]["tokens"]
    ]
    assert built[0]["n_anchored"] == 4 and today[0]["n_anchored"] == 3  # the colon is Kraken's word too


# ---------------------------------------------------------------- the page


@pytest.fixture
def drawn_page(db):
    book = Book.objects.create(title="كتاب", status=Book.Status.OCR)
    page = Page.objects.create(book=book, number=1, source_index=0, status=Page.Status.LAYOUT_DONE)
    pre = Preprocess.objects.create(page=page, output_width=W, output_height=H, line_boxes=BANDS)
    save_array(pre.gray_image, page_image(), "gray.png")
    save_array(pre.bw_image, np.full((H, W), 255, dtype=np.uint8), "bw.png")
    pre.save()
    region = Region.objects.create(page=page, kind="body", bbox=[0, 0, W, H], order=0)
    common = {"page": page, "region": region, "params": {"scope": "region", "kind": "body"}}
    tesseract = [tess_line(LINE_A, CORE_A, shift=12), tess_line(LINE_B, CORE_B, shift=12)]
    OcrRun.objects.create(
        **{**common, "params": {**common["params"], "lines": tesseract}},
        engine_name="tesseract",
        input_variant="bw",
        parsed_text="قال الأمير : نعم\nهذا سطر ثان",
    )
    for name in ("qari_v03", "qari_v02"):
        OcrRun.objects.create(**common, engine_name=name, input_variant="gray", parsed_text=TEXT)
    return page


def kraken_on(on: bool = True):
    return override_settings(NASSAKH={**settings.NASSAKH, "KRAKEN_BOXES": on})


def token_boxes(page: Page) -> list[list | None]:
    return [t["bbox"] for line in page.lines.order_by("order") for t in line.tokens]


KRAKEN_BOXES = [[300, 30, 370, 44], [180, 22, 280, 44], [160, 34, 166, 44], [60, 30, 140, 44]]
KRAKEN_BOXES += [[290, 90, 370, 104], [190, 90, 270, 104], [60, 90, 170, 104]]


def test_the_page_s_words_take_kraken_s_boxes_and_its_run_is_kept_for_the_next_build(drawn_page):
    kraken = fake_kraken()
    numbers = OcrRun.objects.create(page=drawn_page, engine_name="kraken", params={"areas": 2, "applied": 1})
    with kraken_on(), registry.override({"kraken": kraken}):
        services.finalize_page(drawn_page)
        assert token_boxes(drawn_page) == KRAKEN_BOXES
        assert len(kraken.calls) == 1
        numbers.delete()  # the numbers pass's run is no boxes run
        run = drawn_page.ocr_runs.get(engine_name="kraken")
        assert (
            run.params["pass"] == "boxes" and run.params["scope"] == "region" and run.input_variant == "gray"
        )
        assert run.region.kind == "body" and len(run.params["crops"]) == 2 and len(run.params["lines"]) == 2
        assert run.parsed_text == "قال الأمير : نعم\nهذا سطر ثان"
        # the models' runs are what the selection reads; the boxes run is not one of them
        target = services.Target(run.region, run.region.bbox)
        assert services._latest_runs(drawn_page, target)["qari_v03"].engine_name == "qari_v03"
        # built again (a rebuild): the stored run serves, Kraken is not called
        services.finalize_page(drawn_page)
        drawn_page.refresh_from_db()
        result = services.rebuild_page_lines(drawn_page)
        assert result.saved and token_boxes(drawn_page) == KRAKEN_BOXES
        assert len(kraken.calls) == 1 and drawn_page.ocr_runs.filter(engine_name="kraken").count() == 1
        # a dry run reads nothing either and writes nothing
        assert [
            t["bbox"]
            for line in services.rebuild_page_lines(drawn_page, save=False).lines
            for t in line.tokens
        ] == (KRAKEN_BOXES)
    tokens = drawn_page.lines.order_by("order").first().tokens
    assert all(t.get("tess") is None and t.get("tc") == 90.0 for t in tokens if t["t"] != ":")


def test_a_stored_run_that_read_other_lines_is_read_again(drawn_page):
    kraken = fake_kraken()
    with kraken_on(), registry.override({"kraken": kraken}):
        services.finalize_page(drawn_page)
        pre = drawn_page.preprocess
        pre.line_boxes = [BANDS[0], {**BANDS[1], "y1": 105}]  # prepared again: other lines
        pre.save()
        services.finalize_page(drawn_page)
    assert len(kraken.calls) == 2 and drawn_page.ocr_runs.filter(engine_name="kraken").count() == 2


def tesseract_boxes(page: Page) -> list[list | None]:
    """The boxes the page's words take from Tesseract (the setting off), for comparison."""
    with kraken_on(False):
        return [t["bbox"] for line in services.compose_page(page).lines for t in line.tokens]


def test_without_kraken_the_words_keep_tesseract_s_boxes(drawn_page):
    expected = tesseract_boxes(drawn_page)
    assert expected[0] == [312, 26, 382, 48] and expected != KRAKEN_BOXES
    kraken = fake_kraken()
    with kraken_on(False), registry.override({"kraken": kraken}):  # the setting off
        services.finalize_page(drawn_page)
    assert token_boxes(drawn_page) == expected and not kraken.calls
    with kraken_on(), registry.override({}):  # not set up: the registry has no Kraken
        services.finalize_page(drawn_page)
    assert token_boxes(drawn_page) == expected
    failing = fake_kraken(fail=True)
    with kraken_on(), registry.override({"kraken": failing}):  # Kraken crashed: logged, the page goes on
        services.finalize_page(drawn_page)
    drawn_page.refresh_from_db()
    assert token_boxes(drawn_page) == expected and len(failing.calls) == 1
    assert drawn_page.status == Page.Status.OCR_DONE
    assert not drawn_page.ocr_runs.filter(engine_name="kraken").exists()


def test_a_region_whose_text_is_tesseract_s_keeps_tesseract_s_boxes(drawn_page):
    drawn_page.ocr_runs.filter(engine_name__startswith="qari").update(status=OcrRun.Status.ERROR)
    expected = tesseract_boxes(drawn_page)
    kraken = fake_kraken()
    with kraken_on(), registry.override({"kraken": kraken}):
        services.finalize_page(drawn_page)
    assert token_boxes(drawn_page) == expected and not kraken.calls
    assert "ocr_fallback" in Page.objects.get(pk=drawn_page.pk).attention_flags
