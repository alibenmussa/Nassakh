"""Tests of the ocr app: engines (fake, registry, Tesseract parsing, text-layer repair), alignment,
sanity check, fast/full OCR services, finalisation, tasks, API and the text panel partial.

No real model is ever loaded: the registry is overridden with `FakeEngine` instances that answer
per region kind (the service names its temporary crops `<variant>-<i>-<kind>.png`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.template.loader import render_to_string
from django.urls import reverse

import numpy as np
import pytest

from books.models import Book, Page
from books.services import run_stage
from core.storage import save_array
from ocr import services, tasks
from ocr.alignment import align_tokens, build_lines, word_f1
from ocr.engines import pdf_text, registry
from ocr.engines.base import OcrResult
from ocr.engines.fake import FakeEngine
from ocr.engines.qari import max_new_tokens_for, read_model_info, resolve_device
from ocr.engines.tesseract import TesseractEngine, parse_image_to_data
from ocr.models import Line, OcrRun
from processing.models import Preprocess, Region

W, H = 120, 200

TESS_BODY = "قال الامير في سنة ١٩٦٦ ان الكتاب مفيد\nوهذا سطر ثان من المتن"
TESS_FOOT = "(١) حاشية اولى"
TESS_HEADER = "عنوان الكتاب"
TESS_NUMBER = "٨"
PRIMARY_BODY = "قال الأمير في سنة ١٩٦٦ إن الكتاب مفيد وهذا سطر ثانٍ من المتن"
SECONDARY_BODY = "قال الأمير في سنة ١٩٦٦ إن الكتب مفيد وهذا سطر ثانٍ من المتن"
FOOT = "(١) حاشية أولى"


# ---------------------------------------------------------------- fixtures and helpers


@pytest.fixture
def book(db):
    return Book.objects.create(title="كتاب", status=Book.Status.OCR)


@pytest.fixture
def page(book):
    page = Page.objects.create(book=book, number=1, source_index=0, status=Page.Status.LAYOUT_DONE)
    pre = Preprocess.objects.create(page=page, output_width=W, output_height=H)
    save_array(pre.gray_image, np.full((H, W), 230, dtype=np.uint8), "gray.png")
    save_array(pre.bw_image, np.full((H, W), 255, dtype=np.uint8), "bw.png")
    pre.save()
    return page


@pytest.fixture
def user(db):
    return User.objects.create_user("editor", password="x")


def add_regions(page: Page) -> dict[str, Region]:
    kinds = [
        ("running_header", [0, 0, W, 20]),
        ("body", [0, 20, W, 120]),
        ("footnote", [0, 130, W, 190]),
        ("page_number", [0, 190, W, H]),
    ]
    return {
        kind: Region.objects.create(page=page, kind=kind, bbox=bbox, order=i)
        for i, (kind, bbox) in enumerate(kinds)
    }


def tess_lines(text: str, y0: int = 0, line_h: int = 20, width: int = W) -> list[dict]:
    """Tesseract-style lines for `text` (one visual line per text line), words laid out right to left."""
    lines = []
    for k, raw in enumerate(text.split("\n")):
        words = raw.split()
        step = width // max(len(words), 1)
        y = y0 + k * line_h
        boxes = [
            {"text": tok, "bbox": [width - (i + 1) * step, y, width - i * step, y + line_h], "conf": 90.0}
            for i, tok in enumerate(words)
        ]
        lines.append({"bbox": [0, y, width, y + line_h], "words": boxes})
    return lines


def tess_result(text: str) -> OcrResult:
    return OcrResult(text=text, duration_s=0.2, finish="n/a", extra={"lines": tess_lines(text)})


def by_kind(mapping: dict[str, str | OcrResult], default: str | OcrResult = ""):
    """Responder answering per region kind, read from the crop's file name (`<variant>-<i>-<kind>.png`)."""

    def responder(path: Path, max_new_tokens):
        return mapping.get(path.stem.split("-")[-1], default)

    return responder


def tesseract_fake() -> FakeEngine:
    return FakeEngine(
        name="tesseract",
        responder=by_kind(
            {
                "body": tess_result(TESS_BODY),
                "footnote": tess_result(TESS_FOOT),
                "running_header": tess_result(TESS_HEADER),
                "page_number": tess_result(TESS_NUMBER),
                "page": tess_result(TESS_BODY + "\n" + TESS_FOOT),
            }
        ),
    )


def vlm_fake(name: str, body: str | OcrResult, foot: str | OcrResult = FOOT) -> FakeEngine:
    engine = FakeEngine(name=name, responder=by_kind({"body": body, "footnote": foot, "page": body}))
    engine.kind = "vlm"
    engine.backend = "torch"
    return engine


def engines(primary_body=PRIMARY_BODY, secondary_body=SECONDARY_BODY) -> dict[str, FakeEngine]:
    return {
        "tesseract": tesseract_fake(),
        "qari_v03": vlm_fake("qari_v03", primary_body),
        "qari_v02": vlm_fake("qari_v02", secondary_body),
        "pdf_text": FakeEngine(name="pdf_text", text="نص الطبقة النصية ١٢٣"),
    }


# ---------------------------------------------------------------- registry and fake engine


def test_registry_builds_loads_and_caches_the_fake_engine():
    registry.unload_all()
    first = registry.get_engine("fake")
    assert isinstance(first, FakeEngine) and first.is_loaded
    assert registry.get_engine("fake") is first
    assert registry.loaded_engines() == ["fake"]
    registry.unload_all()
    assert registry.loaded_engines() == []
    assert first.unloaded == 1


def test_registry_override_serves_only_the_given_engines():
    fake = FakeEngine(text="x")
    with registry.override({"qari_v03": fake}):
        assert registry.get_engine("qari_v03") is fake
        with pytest.raises(KeyError):
            registry.get_engine("tesseract")
    registry.unload_all()
    assert isinstance(registry.get_engine("fake"), FakeEngine)
    registry.unload_all()


def test_registry_rejects_unknown_names():
    with pytest.raises(KeyError):
        registry.build_engine("nope")


def test_registry_builds_qari_engines_for_the_configured_backend(tmp_path, settings):
    settings.NASSAKH = {
        **settings.NASSAKH,
        "OCR_MODELS_DIR": tmp_path,
        "OCR_DEVICE": "cpu",
        "OCR_BACKEND": "torch",
    }
    torch_engine = registry.build_engine("qari_v03")
    assert torch_engine.backend == "torch" and torch_engine.kind == "vlm"
    assert torch_engine.model_dir == tmp_path / "qari-v0.3"
    assert registry.build_engine("qari_v02").model_dir == tmp_path / "qari-v0.2-merged"
    assert torch_engine.model_id == "NAMAA-Space/Qari-OCR-v0.3-VL-2B-Instruct"
    assert torch_engine.prompt.startswith("Below is the image of one page")
    assert not torch_engine.is_loaded and not torch_engine.is_prepared()

    settings.NASSAKH = {**settings.NASSAKH, "OCR_BACKEND": "mlx"}
    mlx_engine = registry.build_engine("qari_v02")
    assert mlx_engine.backend == "mlx"
    assert mlx_engine.model_dir == tmp_path / "mlx" / "qari-v0.2"

    # nothing prepared under the temp dir: the VLMs are not available, the rest is
    available = registry.available_engines()
    assert "qari_v03" not in available and "qari_v02" not in available
    assert {"fake", "pdf_text"} <= set(available)


def test_qari_metadata_helpers(tmp_path):
    (tmp_path / "nassakh_info.json").write_text(json.dumps({"revision": "abc123"}), encoding="utf-8")
    assert read_model_info(tmp_path) == {"revision": "abc123"}
    assert read_model_info(tmp_path / "missing") == {}
    assert resolve_device("cpu") == "cpu" and resolve_device("mps") == "mps"
    assert max_new_tokens_for("page") == 3000
    assert max_new_tokens_for("body") == 2500
    assert max_new_tokens_for("footnote") == 1000
    assert max_new_tokens_for("heading") == 600


def test_fake_engine_records_calls_and_supports_errors(tmp_path):
    img = tmp_path / "a.png"
    from PIL import Image

    Image.new("L", (30, 10), 255).save(img)
    engine = FakeEngine(text="نص", lines=[{"bbox": [0, 0, 1, 1], "words": []}], output_tokens=5)
    result = engine.recognize(img, 42)
    assert result.text == "نص" and result.output_tokens == 5 and result.extra["lines"]
    assert engine.calls == [{"path": str(img), "max_new_tokens": 42, "image_size": [30, 10], "hints": None}]
    with pytest.raises(RuntimeError):
        FakeEngine(error=RuntimeError("boom")).recognize(img)


# ---------------------------------------------------------------- tesseract parsing


def _image_to_data_dict():
    rows = [
        # level, block, par, line, word, left, top, width, height, conf, text
        (5, 1, 1, 1, 1, 60, 0, 40, 20, 91, "قال"),
        (5, 1, 1, 1, 2, 10, 0, 45, 20, 88, "الأمير"),
        (5, 1, 1, 1, 3, 0, 0, 0, 0, -1, ""),  # layout row, skipped
        (5, 1, 1, 2, 1, 50, 25, 50, 20, 70, "سنة"),
        (5, 1, 1, 2, 2, 5, 25, 40, 20, 55, "١٩٦٦"),
        (5, 1, 2, 1, 1, 30, 60, 60, 20, 80, "حاشية"),
    ]
    keys = [
        "level",
        "block_num",
        "par_num",
        "line_num",
        "word_num",
        "left",
        "top",
        "width",
        "height",
        "conf",
        "text",
    ]
    return {key: [row[i] for row in rows] for i, key in enumerate(keys)}


def test_parse_image_to_data_groups_words_into_lines_with_boxes():
    text, lines = parse_image_to_data(_image_to_data_dict())
    assert text == "قال الأمير\nسنة ١٩٦٦\n\nحاشية"
    assert len(lines) == 3
    assert lines[0]["bbox"] == [10, 0, 100, 20]
    assert [w["text"] for w in lines[0]["words"]] == ["قال", "الأمير"]  # word_num order
    assert lines[1]["words"][1] == {"text": "١٩٦٦", "bbox": [5, 25, 45, 45], "conf": 55.0}


def test_parse_image_to_data_keeps_tesseract_word_order_for_latin_runs():
    # word_num order is the logical order; sorting by x would reverse "Ibn Khaldun" (F10)
    rows = [("قال", 300), ("Ibn", 150), ("Khaldun", 220), ("في", 60)]
    data = {
        "level": [5] * 4,
        "block_num": [1] * 4,
        "par_num": [1] * 4,
        "line_num": [1] * 4,
        "word_num": [1, 2, 3, 4],
        "left": [x for _, x in rows],
        "top": [0] * 4,
        "width": [60] * 4,
        "height": [20] * 4,
        "conf": [90] * 4,
        "text": [t for t, _ in rows],
    }
    text, lines = parse_image_to_data(data)
    assert text == "قال Ibn Khaldun في"
    assert [w["text"] for w in lines[0]["words"]] == ["قال", "Ibn", "Khaldun", "في"]


def test_tesseract_engine_recognize_uses_image_to_data(tmp_path, settings):
    from PIL import Image

    img = tmp_path / "bw.png"
    Image.new("L", (100, 80), 255).save(img)
    engine = TesseractEngine(langs="ara+eng", psm=4)
    with mock.patch("pytesseract.image_to_data", return_value=_image_to_data_dict()) as call:
        result = engine.recognize(img)
    assert call.call_args.kwargs["lang"] == "ara+eng"
    assert call.call_args.kwargs["config"] == "--oem 1 --psm 4"
    assert result.text.startswith("قال الأمير")
    assert len(result.extra["lines"]) == 3 and result.extra["image_size"] == [100, 80]
    assert engine.model_id == "tesseract:ara+eng" and engine.kind == "classic"


def test_tesseract_load_reports_missing_languages():
    engine = TesseractEngine(langs="ara+xyz")
    with mock.patch("pytesseract.get_languages", return_value=["ara", "eng"]):
        with pytest.raises(RuntimeError, match="xyz"):
            engine.load()


# ---------------------------------------------------------------- text layer repair


def _ch(c: str, x0: float, x1: float, y: float = 10.0) -> dict:
    return {"c": c, "bbox": (x0, y - 8, x1, y), "origin": (x0, y)}


def test_repair_chars_swaps_zero_width_alef_lam_pairs():
    # "اإلسالم" as emitted by Word: zero-width alef sitting on the lam's edge
    chars = [
        _ch("ا", 40, 40),
        _ch("ل", 36, 40),
        _ch("س", 30, 36),
        _ch("ا", 26, 26),
        _ch("ل", 22, 26),
        _ch("م", 14, 22),
    ]
    text, fixes = pdf_text.repair_chars(chars)
    assert text == "لاسلام" and fixes == 2
    # a real alef followed by lam (the article) is left alone
    text2, fixes2 = pdf_text.repair_chars([_ch("ا", 40, 44), _ch("ل", 36, 40)])
    assert text2 == "ال" and fixes2 == 0


def test_fix_misplaced_moves_a_trailing_comma_to_its_visual_position():
    # stream: "," (pen at far left = end of an RTL line), then "ك", "ت"
    chars = [_ch(",", 2, 4), _ch("ك", 46, 50), _ch("ت", 42, 46)]
    fixed = pdf_text.fix_misplaced(chars)
    assert "".join(c["c"] for c in fixed) == "كت,"
    # a comma sitting between its neighbours stays where it is
    inline = [_ch("ك", 46, 50), _ch(",", 44, 46), _ch("ت", 40, 44)]
    assert "".join(c["c"] for c in pdf_text.fix_misplaced(inline)) == "ك,ت"


def test_pdf_text_engine_reads_a_generated_pdf(tmp_path):
    import pymupdf

    path = tmp_path / "doc.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=300, height=200).insert_text((40, 60), "Hello Nassakh", fontsize=14)
        doc.new_page(width=300, height=200).insert_text((40, 60), "Second page", fontsize=14)
        doc.save(path)
    engine = pdf_text.PdfTextEngine()
    first = engine.recognize(pdf_text.PdfPageRef(str(path), 0))
    assert "Hello Nassakh" in first.text and first.extra["pdf_index"] == 0
    second = engine.recognize(f"{path}#1")
    assert "Second page" in second.text
    right = engine.recognize(pdf_text.PdfPageRef(str(path), 0, half="right", split_ratio=0.5))
    assert "Hello" not in right.text  # the text sits in the left half
    with pytest.raises(IndexError):
        engine.recognize(pdf_text.PdfPageRef(str(path), 7))


# ---------------------------------------------------------------- sanity check


REF_20 = " ".join(f"كلمة{i}" for i in range(20))


@pytest.mark.parametrize(
    "text,reference,looped,expected",
    [
        (REF_20, REF_20, True, (False, "loop")),
        ("", REF_20, False, (False, "empty")),
        ("كلمة0 كلمة1", REF_20, False, (False, "too_short")),  # 2 < 20 % of 20 - slack
        (" ".join(f"كلمة{i}" for i in range(60)), REF_20, False, (False, "too_long")),
        (" ".join(f"غريب{i}" for i in range(20)), REF_20, False, (False, "low_overlap")),
        (REF_20, REF_20, False, (True, "ok")),
        ("نص بلا مرجع", "", False, (True, "no_reference")),
        # an empty reference is inconclusive for anything longer than a few words (F12)
        ("جملة كاملة متخيلة على منطقة فارغة", "", False, (False, "no_reference")),
        # diacritics and letter variants do not count as differences
        ("قَالَ الأَمِيرُ إنَّ الكِتَابَ مُفِيدٌ " * 3, "قال الامير ان الكتاب مفيد " * 3, False, (True, "ok")),
        # under 15 reference words only runaway output fails (F8): noisy Tesseract footnote
        (
            "أ ابن عبد الحكم ، فوج مصر والمغرب ، 149 . Goodchild, 148.",
            "ال ae ee CT ¥ Goodchild, 148. *",
            False,
            (True, "short_reference"),
        ),
        (" ".join(f"كلمة{i}" for i in range(14)), "غ1 غ2 غ3 غ4", False, (True, "short_reference")),
        (" ".join(f"كلمة{i}" for i in range(23)), "غ1 غ2 غ3 غ4", False, (False, "too_long")),
        (REF_20[:20], REF_20[:20], True, (False, "loop")),
    ],
)
def test_sanity_check_cases(text, reference, looped, expected):
    assert services.sanity_check(text, reference, looped) == expected


def test_word_f1_is_order_insensitive_and_lenient():
    assert word_f1("ب ا ج", "ا ب ج") == 1.0
    assert word_f1("الأمير", "الامير") == 1.0
    assert word_f1("ا ب", "ج د") == 0.0


# ---------------------------------------------------------------- alignment


def test_align_tokens_pairs_equal_substituted_inserted_and_deleted_tokens():
    a = "قال الأمير في سنة ١٩٦٦ إن الكتاب مفيد".split()
    b = "قال الامير فى سنه 1966 ان الكتب مفيد جدا".split()
    pairs = align_tokens(a, b)
    assert pairs[:8] == [(i, i) for i in range(8)]  # variants normalise away; الكتاب↔الكتب paired 1:1
    assert pairs[8] == (None, 8)  # inserted جدا
    assert align_tokens("ا ب ج".split(), "ا ج".split()) == [(0, 0), (1, None), (2, 1)]
    assert align_tokens([], ["x"]) == [(None, 0)]
    assert align_tokens([], []) == []


def test_align_tokens_fuzzy_pairs_inside_replaced_blocks():
    a = ["المكتبة", "الوطنية", "الكبيرة"]
    b = ["المكتبه", "الكبيره"]  # one word dropped, two misspelled
    pairs = align_tokens(a, b)
    assert (0, 0) in pairs and (2, 1) in pairs and (1, None) in pairs
    # unrelated tokens are not forced together in longer blocks
    pairs2 = align_tokens(["س", "شمس", "قمر"], ["كتاب", "ورقة", "حبر", "طاولة"])
    assert all(j is None for i, j in pairs2 if i is not None)
    assert sorted(j for i, j in pairs2 if i is None) == [0, 1, 2, 3]


def _lines_for_body():
    return tess_lines(TESS_BODY)


def test_build_lines_puts_tokens_on_the_matching_tesseract_lines():
    lines = build_lines(PRIMARY_BODY, SECONDARY_BODY, _lines_for_body())
    assert [line["text"] for line in lines] == [
        "قال الأمير في سنة ١٩٦٦ إن الكتاب مفيد",
        "وهذا سطر ثانٍ من المتن",
    ]
    assert [line["order"] for line in lines] == [0, 1]
    assert lines[0]["bbox"] == [0, 0, W, 20] and lines[1]["bbox"] == [0, 20, W, 40]
    tokens = {t["t"]: t for t in lines[0]["tokens"]}
    assert tokens["١٩٦٦"]["conf"] == "low" and tokens["١٩٦٦"]["digit"] is True
    assert tokens["الكتاب"] == {
        "t": "الكتاب",
        "alt": "الكتب",
        "conf": "low",
        "digit": False,
        "bbox": tokens["الكتاب"]["bbox"],
    }
    assert tokens["الكتاب"]["bbox"] is not None
    assert (
        tokens["الأمير"]["conf"] == "high" and tokens["الأمير"]["alt"] is None
    )  # الامير vs الأمير: same word
    assert lines[0]["n_low"] == 2 and lines[1]["n_low"] == 0
    assert lines[0]["n_anchored"] == 8 and lines[0]["confidence"] == 0.75


def test_build_lines_unmatched_tokens_inherit_the_previous_line():
    primary = "قال الأمير في سنة ١٩٦٦ إن الكتاب مفيد جدا وهذا سطر ثانٍ من المتن"  # جدا is not in Tesseract
    lines = build_lines(primary, None, _lines_for_body())
    assert lines[0]["text"].endswith("مفيد جدا")
    assert lines[1]["text"] == "وهذا سطر ثانٍ من المتن"
    extra = next(t for t in lines[0]["tokens"] if t["t"] == "جدا")
    assert extra["bbox"] is None and extra["conf"] == "high"  # no secondary: only digits are low
    assert sum(line["n_low"] for line in lines) == 1


def test_build_lines_leading_unmatched_tokens_take_the_first_anchored_line():
    lines = build_lines("مقدمة " + PRIMARY_BODY, None, _lines_for_body())
    assert lines[0]["text"].startswith("مقدمة قال")
    assert len(lines) == 2


def test_build_lines_without_geometry_uses_the_text_line_breaks():
    lines = build_lines("سطر أول ١\nسطر ثانٍ", "سطر اول ١\nسطر ثان", [])
    assert [line["text"] for line in lines] == ["سطر أول ١", "سطر ثانٍ"]
    assert all(line["bbox"] is None for line in lines)
    assert lines[0]["n_low"] == 1  # the digit
    assert build_lines("", None, []) == []


def test_build_lines_flags_tokens_the_secondary_does_not_have():
    lines = build_lines("قال الأمير في سنة", "قال في سنة", tess_lines("قال الامير في سنة"))
    tok = next(t for t in lines[0]["tokens"] if t["t"] == "الأمير")
    assert tok["conf"] == "low" and tok["alt"] is None


def test_join_region_texts_orders_body_then_footnotes_and_skips_header_and_number():
    text = services.join_region_texts(
        [
            ("running_header", "عنوان"),
            ("heading", "فصل"),
            ("body", "متن"),
            ("footnote", "حاشية"),
            ("page_number", "٨"),
        ]
    )
    assert text == "فصل\nمتن\n\nحاشية"
    assert services.join_region_texts([("body", "  ")]) == ""


@pytest.mark.parametrize(
    ("text", "expected", "number"),
    [
        ("متن سنة ١٩٦٦ هنا\nسطر 22 ثان\n— 22 —", "متن سنة ١٩٦٦ هنا\nسطر 22 ثان", "22"),
        ("٢٠\nمتن فيه ٢٠ و1966\nآخر سطر", "متن فيه ٢٠ و1966\nآخر سطر", "20"),
        ("(١٥)\n\nمتن\n\nحاشية (١)", "متن\n\nحاشية (١)", "15"),
        ("متن\n12\nآخر", "متن\n12\nآخر", ""),  # a number inside the body is left alone
        ("٢٢", "٢٢", ""),  # a single line is never emptied
    ],
)
def test_strip_page_number_lines_drops_only_a_first_or_last_number_line(text, expected, number):
    assert services.strip_page_number_lines(text) == (expected, number)


def test_page_number_digits_accepts_only_number_lines():
    assert services.page_number_digits("— ٢٢ —") == "22"
    assert services.page_number_digits("[٣٤]") == "34"
    assert services.page_number_digits("۱۲") == "12"
    assert services.page_number_digits("- - -") is None
    assert services.page_number_digits("صفحة ٢٢") is None
    assert services.printed_number_of(" ٨ ") == "8"


# ---------------------------------------------------------------- fast OCR


def test_run_fast_ocr_stores_runs_per_region_and_provisional_text(page):
    regions = add_regions(page)
    fakes = engines()
    with registry.override(fakes):
        services.run_fast_ocr(page)
    page.refresh_from_db()
    assert page.text_state == Page.TextState.PROVISIONAL
    assert page.provisional_text == TESS_BODY + "\n\n" + TESS_FOOT
    assert page.status == Page.Status.LAYOUT_DONE  # fast OCR does not finish the page
    runs = list(page.ocr_runs.order_by("id"))
    assert [r.engine_name for r in runs] == ["tesseract"] * 4
    assert {r.region_id for r in runs} == {r.pk for r in regions.values()}
    body_run = next(r for r in runs if r.region_id == regions["body"].pk)
    assert body_run.input_variant == "bw" and body_run.backend == "fake" and body_run.status == "ok"
    assert body_run.params["scope"] == "region" and body_run.params["kind"] == "body"
    # word boxes are moved into gray-image coordinates (body region starts at y=20)
    first_line = body_run.params["lines"][0]
    assert first_line["bbox"] == [0, 20, W, 40]
    assert first_line["words"][0]["text"] == "قال" and first_line["words"][0]["bbox"][1] == 20
    assert body_run.duration_ms == 200
    assert len(fakes["tesseract"].calls) == 4
    assert fakes["tesseract"].calls[1]["image_size"] == [W, 100]  # body crop


def test_run_fast_ocr_without_regions_runs_on_the_whole_page(page):
    fakes = engines()
    with registry.override(fakes):
        services.run_fast_ocr(page)
    page.refresh_from_db()
    run = page.ocr_runs.get()
    assert run.region is None and run.params["scope"] == "page"
    assert page.provisional_text == TESS_BODY + "\n" + TESS_FOOT
    assert fakes["tesseract"].calls[0]["image_size"] == [W, H]


def test_run_fast_ocr_requires_a_preprocessed_page(book):
    bare = Page.objects.create(book=book, number=2, source_index=1)
    with registry.override(engines()):
        with pytest.raises(services.OcrError):
            services.run_fast_ocr(bare)


def test_run_fast_ocr_fails_when_every_tesseract_call_fails(page):
    add_regions(page)
    broken = FakeEngine(name="tesseract", error=RuntimeError("tesseract is not installed"))
    with registry.override({"tesseract": broken}):
        with pytest.raises(services.OcrError, match="Tesseract"):
            services.run_fast_ocr(page)
    assert page.ocr_runs.filter(status="error").count() == 4


def test_run_fast_ocr_on_a_born_digital_page_finalises_from_the_text_layer(page):
    add_regions(page)
    book = page.book
    book.has_text_layer = True
    book.use_text_layer = True
    book.source_pdf.save("source.pdf", ContentFile(b"%PDF-1.4 fake"), save=True)
    fakes = engines()
    with registry.override(fakes):
        services.run_fast_ocr(page)
    page.refresh_from_db()
    assert page.text_state == Page.TextState.FINAL
    assert page.status == Page.Status.OCR_DONE
    assert page.provisional_text == "نص الطبقة النصية ١٢٣"
    assert page.final_text == "نص الطبقة النصية 123"  # D6 in final_text only
    assert page.lines.count() >= 1 and page.lines.first().ocr_text.endswith("١٢٣")
    pdf_run = page.ocr_runs.get(engine_name="pdf_text")
    assert pdf_run.input_variant == "pdf" and pdf_run.region is None
    ref = fakes["pdf_text"].calls[0]["path"]  # repr of the PdfPageRef handed to the engine
    assert f"/books/{book.pk}/source" in ref and ".pdf'" in ref and "index=0" in ref
    assert Book.objects.get(pk=book.pk).status == Book.Status.READY_FOR_REVIEW

    # the full stage then only re-finalises: no model call is made
    with registry.override(fakes):
        services.run_full_ocr(page)
    assert fakes["qari_v03"].calls == [] and fakes["qari_v02"].calls == []
    assert page.ocr_runs.filter(engine_name__in=["qari_v03", "qari_v02"]).count() == 0


def test_run_fast_ocr_born_digital_falls_back_to_the_ingest_text_layer(page):
    book = page.book
    book.has_text_layer = True
    book.use_text_layer = True
    book.save()
    page.text_layer_text = "نص من الاستيراد\nسطر ٢"
    page.save()
    fakes = engines()  # no source_pdf on the book: the pdf_text engine cannot run
    fakes["tesseract"] = FakeEngine(
        name="tesseract", responder=by_kind({"page": tess_result("نص من الاستيراد\nسطر ٢")})
    )
    with registry.override(fakes):
        services.run_fast_ocr(page)
    page.refresh_from_db()
    assert fakes["pdf_text"].calls == []
    run = page.ocr_runs.get(engine_name="pdf_text")
    assert run.input_variant == "ingest" and run.parsed_text == "نص من الاستيراد\nسطر ٢"
    assert page.text_state == Page.TextState.FINAL and page.final_text == "نص من الاستيراد\nسطر 2"
    lines = list(page.lines.order_by("order"))
    assert [line.text for line in lines] == ["نص من الاستيراد", "سطر ٢"]
    assert lines[0].bbox == [0, 0, W, 20] and lines[1].bbox == [
        0,
        20,
        W,
        40,
    ]  # anchored to Tesseract geometry


# ---------------------------------------------------------------- full OCR and finalisation


def test_run_full_ocr_builds_lines_final_text_and_statuses(page):
    regions = add_regions(page)
    fakes = engines()
    with registry.override(fakes):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()

    assert page.status == Page.Status.OCR_DONE
    assert page.text_state == Page.TextState.FINAL
    assert page.final_text == "قال الأمير في سنة 1966 إن الكتاب مفيد\nوهذا سطر ثانٍ من المتن\n\n(1) حاشية أولى"
    assert "ocr_fallback" not in page.attention_flags
    assert Book.objects.get(pk=page.book_id).status == Book.Status.READY_FOR_REVIEW

    lines = list(page.lines.order_by("order"))
    assert [line.text for line in lines] == [
        "قال الأمير في سنة ١٩٦٦ إن الكتاب مفيد",
        "وهذا سطر ثانٍ من المتن",
        "(١) حاشية أولى",
    ]
    assert [line.order for line in lines] == [0, 1, 2]
    assert lines[0].region_id == regions["body"].pk and lines[2].region_id == regions["footnote"].pk
    assert lines[0].ocr_text == lines[0].text  # raw digits kept on the line
    assert lines[0].bbox == [0, 20, W, 40]  # gray-image coordinates
    assert lines[2].bbox == [0, 130, W, 150]
    low = [t["t"] for line in lines for t in line.tokens if t["conf"] == "low"]
    assert low == ["١٩٦٦", "الكتاب", "(١)"]
    assert next(t for t in lines[0].tokens if t["t"] == "الكتاب")["alt"] == "الكتب"
    assert lines[0].n_low == 2 and lines[0].confidence == 0.75

    # runs: 4 tesseract (fast) + primary and secondary on body and footnote only
    by_engine = {}
    for run in page.ocr_runs.all():
        by_engine.setdefault(run.engine_name, []).append(run)
    assert len(by_engine["tesseract"]) == 4
    assert {r.region.kind for r in by_engine["qari_v03"]} == {"body", "footnote", "page_number"}
    assert {r.region.kind for r in by_engine["qari_v02"]} == {"body", "footnote"}
    foot_run = next(r for r in by_engine["qari_v03"] if r.region.kind == "footnote")
    body_run = next(r for r in by_engine["qari_v03"] if r.region.kind == "body")
    assert foot_run.input_variant == "gray_2x" and body_run.input_variant == "gray"
    assert foot_run.params["max_new_tokens"] == 1000 and body_run.params["max_new_tokens"] == 2500
    assert body_run.params["sanity"] == {"ok": True, "reason": "short_reference"}  # 12-word reference
    assert body_run.prompt.startswith("Below is the image") is False  # fake engines have no prompt
    assert body_run.backend == "torch" and body_run.model_id == "fake"
    # footnote crops are upscaled 2x with Lanczos before the models see them
    foot_call = next(c for c in fakes["qari_v03"].calls if c["path"].endswith("footnote.png"))
    assert foot_call["image_size"] == [W * 2, 60 * 2] and foot_call["max_new_tokens"] == 1000
    body_call = next(c for c in fakes["qari_v03"].calls if c["path"].endswith("body.png"))
    assert body_call["image_size"] == [W, 100]


def test_run_full_ocr_falls_back_to_tesseract_when_both_models_fail(page):
    add_regions(page)
    looping = OcrResult(text=PRIMARY_BODY, duration_s=1.0, output_tokens=2500, finish="length")
    runaway = " ".join(f"كلمة{i}" for i in range(60))
    fakes = engines(primary_body=looping, secondary_body=runaway)
    with registry.override(fakes):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert "ocr_fallback" in page.attention_flags
    assert page.status == Page.Status.OCR_DONE
    # body from Tesseract (Western digits in final_text), footnote from the models (they agreed there)
    assert page.final_text == "قال الامير في سنة 1966 ان الكتاب مفيد\nوهذا سطر ثان من المتن\n\n(1) حاشية أولى"
    body_primary = page.ocr_runs.get(engine_name="qari_v03", region__kind="body")
    assert body_primary.looped is True and body_primary.params["sanity"]["reason"] == "loop"
    body_secondary = page.ocr_runs.get(engine_name="qari_v02", region__kind="body")
    assert body_secondary.params["sanity"]["reason"] == "too_long"
    body_lines = page.lines.filter(region__kind="body").order_by("order")
    assert [line.text for line in body_lines] == TESS_BODY.split("\n")
    # only digits are low in fallback text (no alternatives)
    assert [t["t"] for line in body_lines for t in line.tokens if t["conf"] == "low"] == ["١٩٦٦"]


def test_run_full_ocr_uses_the_secondary_when_the_primary_fails(page):
    add_regions(page)
    fakes = engines(primary_body="")  # empty primary output
    with registry.override(fakes):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert "ocr_fallback" not in page.attention_flags
    assert page.final_text.startswith("قال الأمير في سنة 1966 إن الكتب مفيد")
    body_tokens = [t for line in page.lines.filter(region__kind="body") for t in line.tokens]
    assert all(t["alt"] is None for t in body_tokens)  # a failed primary gives no alternatives


def test_run_full_ocr_runs_tesseract_itself_when_the_fast_stage_did_not(page):
    add_regions(page)
    fakes = engines()
    with registry.override(fakes):
        services.run_full_ocr(page)  # no fast OCR first
    page.refresh_from_db()
    assert page.status == Page.Status.OCR_DONE
    assert page.ocr_runs.filter(engine_name="tesseract").count() == 2  # body and footnote references
    assert page.lines.count() == 3


def test_run_full_ocr_reports_an_engine_that_cannot_load(page):
    add_regions(page)

    class Broken(FakeEngine):
        def load(self):
            raise FileNotFoundError("weights missing")

    fakes = engines()
    broken = Broken(name="qari_v03")
    with registry.override(fakes):
        services.run_fast_ocr(page)
    with mock.patch.object(
        registry, "get_engine", side_effect=lambda n: fakes[n] if n != "qari_v03" else broken.load()
    ):
        with pytest.raises(services.OcrError, match="qari_v03"):
            services.run_full_ocr(page)


def test_run_full_ocr_page_level_without_regions_uses_the_page_cap(page):
    fakes = engines()
    with registry.override(fakes):
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.status == Page.Status.OCR_DONE
    primary = page.ocr_runs.get(engine_name="qari_v03")
    assert primary.region is None and primary.params["scope"] == "page"
    assert primary.params["max_new_tokens"] == 3000


def test_fast_ocr_drops_a_trailing_page_number_line_and_stores_it(page):
    # no page-number region (nothing detected): the number reached the footnote text
    Region.objects.create(page=page, kind="body", bbox=[0, 0, W, 120], order=0)
    Region.objects.create(page=page, kind="footnote", bbox=[0, 130, W, H], order=1)
    fakes = engines()
    fakes["tesseract"] = FakeEngine(
        name="tesseract",
        responder=by_kind({"body": tess_result(TESS_BODY), "footnote": tess_result(TESS_FOOT + "\n— 22 —")}),
    )
    with registry.override(fakes):
        services.run_fast_ocr(page)
    page.refresh_from_db()
    assert page.provisional_text == TESS_BODY + "\n\n" + TESS_FOOT  # body digits ١٩٦٦ untouched
    assert page.printed_number == "22"


def test_full_ocr_drops_a_leading_page_number_line_from_lines_and_final_text(page):
    Region.objects.create(page=page, kind="body", bbox=[0, 0, W, 120], order=0)
    fakes = engines(primary_body="٢٠\n" + PRIMARY_BODY, secondary_body="٢٠\n" + SECONDARY_BODY)
    fakes["tesseract"] = FakeEngine(
        name="tesseract", responder=by_kind({"body": tess_result("٢٠\n" + TESS_BODY)})
    )
    with registry.override(fakes):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.printed_number == "20"
    assert page.provisional_text == TESS_BODY
    assert not page.final_text.startswith("20")
    assert "1966" in page.final_text  # body digits kept (converted to Western as usual)
    texts = list(page.lines.order_by("order").values_list("text", flat=True))
    assert texts and all(services.page_number_digits(t) is None for t in texts)
    assert "١٩٦٦" in texts[0]
    assert list(page.lines.order_by("order").values_list("order", flat=True)) == list(range(len(texts)))


def test_finalize_page_replaces_unreviewed_lines_and_keeps_reviewed_ones(page):
    add_regions(page)
    fakes = engines()
    with registry.override(fakes):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    first = page.lines.order_by("order").first()
    first.is_reviewed = True
    first.text = "نص مُراجَع"
    first.save()
    stale = Line.objects.create(page=page, order=99, text="قديم")
    services.finalize_page(page)
    page.refresh_from_db()
    assert Line.objects.filter(pk=first.pk, text="نص مُراجَع").exists()
    assert not Line.objects.filter(pk=stale.pk).exists()
    # the reviewed line takes the place of the new line at its order: no duplicate (F54)
    assert page.lines.count() == 3
    assert list(page.lines.order_by("order").values_list("order", flat=True)) == [0, 1, 2]
    assert page.lines.get(order=0).is_reviewed and "نص مُراجَع" in page.final_text


def test_finalize_page_uses_the_latest_run_per_engine(page):
    regions = add_regions(page)
    body = services.Target(regions["body"], regions["body"].bbox)
    common = dict(page=page, region=regions["body"], engine_name="qari_v03", params={"scope": "region"})
    OcrRun.objects.create(**common, parsed_text="قديم")
    newer = OcrRun.objects.create(**common, parsed_text="جديد")
    latest = services._latest_runs(page, body)
    assert latest["qari_v03"].pk == newer.pk


def test_finalize_page_flags_poor_alignment(page):
    add_regions(page)
    # Tesseract sees completely different words: nothing anchors
    unrelated = "\n".join(" ".join(f"غ{i}{k}" for i in range(6)) for k in range(2))
    fakes = engines()
    fakes["tesseract"] = FakeEngine(name="tesseract", responder=by_kind({}, tess_result(unrelated)))
    fakes["qari_v03"] = vlm_fake(
        "qari_v03", " ".join(f"ك{i}" for i in range(12)), " ".join(f"ح{i}" for i in range(6))
    )
    fakes["qari_v02"] = vlm_fake(
        "qari_v02", " ".join(f"ك{i}" for i in range(12)), " ".join(f"ح{i}" for i in range(6))
    )
    with registry.override(fakes):
        services.run_fast_ocr(page)
        with mock.patch.object(services, "sanity_check", return_value=(True, "ok")):
            services.run_full_ocr(page)
    page.refresh_from_db()
    assert "alignment_poor" in page.attention_flags
    assert page.status == Page.Status.OCR_DONE


def test_book_becomes_ready_for_review_only_when_all_pages_are_done(page):
    book = page.book
    second = Page.objects.create(book=book, number=2, source_index=1, status=Page.Status.LAYOUT_DONE)
    Page.objects.create(book=book, number=3, source_index=2, is_excluded=True, status=Page.Status.EXCLUDED)
    fakes = engines()
    with registry.override(fakes):
        services.run_full_ocr(page)
    assert Book.objects.get(pk=book.pk).status == Book.Status.OCR
    pre = Preprocess.objects.create(page=second, output_width=W, output_height=H)
    save_array(pre.gray_image, np.full((H, W), 230, dtype=np.uint8), "gray.png")
    save_array(pre.bw_image, np.full((H, W), 255, dtype=np.uint8), "bw.png")
    pre.save()
    with registry.override(fakes):
        services.run_full_ocr(second)
    assert Book.objects.get(pk=book.pk).status == Book.Status.READY_FOR_REVIEW


def test_select_text_prefers_primary_then_secondary_then_tesseract():
    tess = OcrRun(engine_name="tesseract", parsed_text=REF_20)
    good = OcrRun(engine_name="qari_v03", parsed_text=REF_20)
    also = OcrRun(engine_name="qari_v02", parsed_text=REF_20.replace("كلمة3", "كلمه٣"))
    bad = OcrRun(engine_name="qari_v02", parsed_text="x", looped=True)
    failed = OcrRun(engine_name="qari_v03", status="error", error="boom")

    text, alt, fallback, reason, source = services.select_text(good, also, tess)
    assert (text, fallback, source) == (REF_20, False, "qari_v03") and alt == also.parsed_text
    text, alt, fallback, _, source = services.select_text(good, bad, tess)
    assert (alt, fallback, source) == (None, False, "qari_v03")
    text, alt, fallback, reason, source = services.select_text(failed, also, tess)
    assert (text, fallback, source) == (also.parsed_text, False, "qari_v02") and reason == "primary:error"
    text, alt, fallback, reason, source = services.select_text(failed, bad, tess)
    assert (text, fallback, source) == (REF_20, True, "tesseract") and "loop" in reason
    text, alt, fallback, _, source = services.select_text(None, None, None)
    assert (text, fallback, source) == ("", True, "")


# ---------------------------------------------------------------- tasks (Celery eager)


def test_ocr_tasks_run_the_chain_eagerly_and_return_the_page_id(page):
    add_regions(page)
    with registry.override(engines()):
        assert tasks.ocr_page_fast.apply(args=[page.pk]).get() == page.pk
        page.refresh_from_db()
        assert page.text_state == Page.TextState.PROVISIONAL
        assert tasks.ocr_page_full.apply(args=[page.pk]).get() == page.pk
    page.refresh_from_db()
    assert page.status == Page.Status.OCR_DONE and page.text_state == Page.TextState.FINAL


def test_ocr_tasks_skip_excluded_and_missing_pages(page):
    page.is_excluded = True
    page.save()
    with mock.patch.object(services, "run_fast_ocr") as run:
        assert tasks.ocr_page_fast.apply(args=[page.pk]).get() == page.pk
        assert tasks.ocr_page_full.apply(args=[999_999]).get() == 999_999
    run.assert_not_called()


def test_ocr_task_marks_the_page_error_on_unexpected_failures(page):
    with mock.patch.object(services, "run_full_ocr", side_effect=ValueError("bad tensor")):
        assert tasks.ocr_page_full.apply(args=[page.pk]).get() == page.pk
    page.refresh_from_db()
    assert page.status == Page.Status.ERROR and page.error_from == "ocr_full"
    assert page.error_message.startswith(tasks.HEADLINE_FULL)
    assert "ValueError: bad tensor" in page.error_message


def test_ocr_task_stores_ocr_errors_as_the_message(page):
    with mock.patch.object(services, "run_fast_ocr", side_effect=services.OcrError("رسالة عربية واضحة")):
        tasks.ocr_page_fast.apply(args=[page.pk]).get()
    page.refresh_from_db()
    assert (page.status, page.error_from, page.error_message) == (
        Page.Status.ERROR,
        "ocr_fast",
        "رسالة عربية واضحة",
    )


def test_ocr_task_retries_os_errors_then_marks_the_page(page):
    for task in (tasks.ocr_page_fast, tasks.ocr_page_full):
        assert task.autoretry_for == (OSError,) and task.max_retries == 2
    with mock.patch.object(services, "run_full_ocr", side_effect=OSError("disk gone")):
        # While retries remain the OSError is handed to Celery's autoretry. In eager mode with
        # `task_eager_propagates` that surfaces as `Retry`; a worker re-queues the task instead.
        with pytest.raises(Retry):
            tasks.ocr_page_full.apply(args=[page.pk])
        page.refresh_from_db()
        assert page.status != Page.Status.ERROR
        # last attempt (retries == max_retries): the page is marked and the task returns normally
        stub = SimpleNamespace(request=SimpleNamespace(id="task-abc", retries=2), max_retries=2)
        assert (
            tasks._run_stage(stub, page.pk, "ocr_full", services.run_full_ocr, tasks.HEADLINE_FULL) == page.pk
        )
    page.refresh_from_db()
    assert page.status == Page.Status.ERROR and page.error_from == "ocr_full"
    assert page.error_message.startswith(tasks.HEADLINE_FULL) and "OSError: disk gone" in page.error_message
    assert page.task_id == "task-abc"


def test_ocr_task_recovers_a_page_from_error(page):
    add_regions(page)
    page.set_error("ocr_full", "فشل سابق")
    with registry.override(engines()):
        run_stage(page, "ocr_full")  # the retry path clears the error, then the task runs
    page.refresh_from_db()
    assert page.status == Page.Status.OCR_DONE and page.error_from == "" and page.error_message == ""


def test_ocr_tasks_skip_a_page_that_failed_earlier_in_the_chain(page):
    # F2: a failure upstream keeps its message; OCR does not overwrite it nor run on stale inputs
    add_regions(page)
    page.set_error("preprocess", "فشلت المعالجة الأولية.")
    with registry.override(engines()):
        tasks.ocr_page_fast.apply(args=[page.pk]).get()
        tasks.ocr_page_full.apply(args=[page.pk]).get()
    page.refresh_from_db()
    assert (page.status, page.error_from, page.error_message) == (
        "error",
        "preprocess",
        "فشلت المعالجة الأولية.",
    )
    assert not page.ocr_runs.exists()


def test_run_full_ocr_puts_the_page_in_error_when_every_model_call_crashes(page):
    # F11: an engine crash on every call is not a finished page with Tesseract text
    add_regions(page)
    fakes = engines()
    with registry.override(fakes):
        services.run_fast_ocr(page)
        for name in ("qari_v03", "qari_v02"):
            fakes[name].recognize = mock.Mock(side_effect=RuntimeError("MPS backend out of memory"))
        tasks.ocr_page_full.apply(args=[page.pk]).get()
    page.refresh_from_db()
    assert page.status == Page.Status.ERROR and page.error_from == "ocr_full"
    headline, detail = page.error_message.split("\n", 1)
    assert "GPU" in headline and "RuntimeError" not in headline and "out of memory" in detail


def test_run_fast_ocr_reports_a_missing_tesseract_with_the_install_hint(page):
    # F13/F25: the actionable headline, the technical part on the following lines
    add_regions(page)
    fakes = {k: v for k, v in engines().items() if k != "tesseract"}
    with registry.override(fakes):
        tasks.ocr_page_fast.apply(args=[page.pk]).get()
    page.refresh_from_db()
    headline = page.error_message.splitlines()[0]
    assert page.error_from == "ocr_fast" and headline == services.TESSERACT_HEADLINE


def test_select_text_accepts_the_primary_when_both_models_agree_against_tesseract():
    # F8 (2): both Qari readings differ from a garbage reference but agree with each other
    tess = OcrRun(engine_name="tesseract", parsed_text=" ".join(f"غ{i}" for i in range(20)))
    reading = " ".join(f"كلمة{i}" for i in range(20))
    primary = OcrRun(engine_name="qari_v03", parsed_text=reading)
    secondary = OcrRun(engine_name="qari_v02", parsed_text=reading + " زائدة")
    text, alt, fallback, reason, source = services.select_text(primary, secondary, tess)
    assert (text, fallback, reason, source) == (reading, False, "models_agree", "qari_v03")
    assert alt == secondary.parsed_text
    looping = OcrRun(engine_name="qari_v03", parsed_text=reading, looped=True)
    assert services.select_text(looping, secondary, tess)[2] is True  # a loop is never rescued


def test_warm_up_engines_loads_the_configured_engines():
    fakes = engines()
    with registry.override(fakes):
        assert tasks.warm_up_engines.apply().get() == ["qari_v03", "qari_v02", "tesseract"]
    with registry.override({"tesseract": fakes["tesseract"]}):
        assert tasks.warm_up_engines.apply().get() == ["tesseract"]  # missing engines are reported, not fatal


# ---------------------------------------------------------------- API


def test_page_text_and_runs_api_require_login_and_return_lines(client, page, user):
    add_regions(page)
    with registry.override(engines()):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    text_url = reverse("api:page_text", args=[page.pk])
    runs_url = reverse("api:page_runs", args=[page.pk])
    assert text_url == f"/api/pages/{page.pk}/text/" and runs_url == f"/api/pages/{page.pk}/runs/"
    assert client.get(text_url).status_code in (401, 403)

    client.force_login(user)
    data = client.get(text_url).json()
    assert data["page_id"] == page.pk and data["text_state"] == "final" and data["status"] == "ocr_done"
    assert data["final_text"].startswith("قال الأمير في سنة 1966")
    assert len(data["lines"]) == 3 and data["n_low"] == 3
    line = data["lines"][0]
    assert set(line) == {
        "id",
        "order",
        "region_id",
        "bbox",
        "text",
        "tokens",
        "confidence",
        "n_low",
        "is_reviewed",
    }
    assert line["tokens"][4] == {
        "t": "١٩٦٦",
        "alt": None,
        "conf": "low",
        "digit": True,
        "bbox": line["tokens"][4]["bbox"],
    }

    runs = client.get(runs_url).json()["runs"]
    assert len(runs) == 10  # 4 tesseract + 2x2 model runs + 2 page-number reads
    assert runs[0]["created_at"] >= runs[-1]["created_at"]  # newest first
    sample = next(r for r in runs if r["engine"] == "qari_v03" and r["region_kind"] == "footnote")
    assert sample["variant"] == "gray_2x" and sample["variant_label"] == "رمادية ×2"
    assert sample["engine_label"] == "Qari v0.3" and sample["region_label"] == "حاشية"
    assert sample["seconds"] == 0.0 and sample["looped"] is False and sample["check"] == "short_reference"
    assert client.get(reverse("api:page_text", args=[999_999])).status_code == 404


# ---------------------------------------------------------------- text panel partial


def _panel_payload(html: str) -> dict:
    match = re.search(r'<script type="application/json">(.*?)</script>', html, re.S)
    assert match, "the partial should embed its initial JSON payload"
    return json.loads(match.group(1))


def test_text_panel_renders_provisional_state_and_polling_config(page, book):
    add_regions(page)
    with registry.override(engines()):
        services.run_fast_ocr(page)
    html = render_to_string("ocr/_text_panel.html", {"page": page, "book": book})
    assert "نص مبدئي (Tesseract)" in html
    assert "text-text-3" in html and "tok-low" in html
    assert f"statusUrl: '/api/pages/{page.pk}/status/'" in html
    assert (
        f"textUrl: '/api/pages/{page.pk}/text/'" in html and f"runsUrl: '/api/pages/{page.pk}/runs/'" in html
    )
    assert 'x-data="textPanel(' in html
    assert "تشغيلات المحرّكات" in html
    payload = _panel_payload(html)
    assert payload["text_state"] == "provisional" and payload["status"] == "layout_done"
    assert payload["provisional_text"] == TESS_BODY + "\n\n" + TESS_FOOT
    assert payload["lines"] == [] and len(payload["runs"]) == 4


def test_text_panel_embeds_final_lines(page, book):
    add_regions(page)
    with registry.override(engines()):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    payload = _panel_payload(render_to_string("ocr/_text_panel.html", {"page": page, "book": book}))
    assert payload["text_state"] == "final" and len(payload["lines"]) == 3 and payload["n_low"] == 3
    assert payload["lines"][0]["tokens"][6]["alt"] == "الكتب"


def test_text_panel_renders_nothing_without_a_page():
    html = render_to_string("ocr/_text_panel.html", {"page": None, "book": None})
    assert html.strip() == ""


def test_run_fast_ocr_reads_the_page_number_padded_upscaled_single_line(page):
    """The page-number crop is padded, enlarged 4x and read with Tesseract psm 7; digits -> printed_number."""
    regions = add_regions(page)
    fakes = engines()
    with registry.override(fakes):
        services.run_fast_ocr(page)
    page.refresh_from_db()
    assert page.printed_number == "8"
    run = page.ocr_runs.get(region=regions["page_number"])
    assert run.input_variant == f"bw_{services.PAGE_NUMBER_UPSCALE}x"
    assert run.params["hints"] == {"psm": services.PAGE_NUMBER_PSM}
    call = next(c for c in fakes["tesseract"].calls if c["hints"] == {"psm": services.PAGE_NUMBER_PSM})
    x0, y0, x1, y1 = regions["page_number"].bbox
    pad, up = services.PAGE_NUMBER_PAD, services.PAGE_NUMBER_UPSCALE
    expected = [(min(W, x1 + pad) - max(0, x0 - pad)) * up, (min(H, y1 + pad) - max(0, y0 - pad)) * up]
    assert call["image_size"] == expected
    # every other region is still read at 1x with the engine's default mode
    assert all(c["hints"] is None for c in fakes["tesseract"].calls if c is not call)


def test_run_full_ocr_reads_the_printed_number_with_the_primary_model(page):
    """Both models read the padded 4x page-number crop with a tiny cap; agreeing digits win over Tesseract."""
    regions = add_regions(page)
    fakes = engines()
    fakes["qari_v03"] = FakeEngine(
        name="qari_v03",
        responder=by_kind(
            {"body": PRIMARY_BODY, "footnote": FOOT, "page": PRIMARY_BODY, "page_number": "— ٢٢ —"}
        ),
    )
    fakes["qari_v03"].kind = "vlm"
    fakes["qari_v02"] = FakeEngine(
        name="qari_v02",
        responder=by_kind(
            {"body": SECONDARY_BODY, "footnote": FOOT, "page": SECONDARY_BODY, "page_number": "22"}
        ),
    )
    fakes["qari_v02"].kind = "vlm"
    with registry.override(fakes):
        services.run_fast_ocr(page)
        page.refresh_from_db()
        assert page.printed_number == "8"  # Tesseract's guess from the fast pass
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.printed_number == "22"  # both models agree; Tesseract is outvoted
    run = page.ocr_runs.get(region=regions["page_number"], engine_name="qari_v03")
    assert run.input_variant == f"gray_{services.PAGE_NUMBER_UPSCALE}x"
    assert run.params["max_new_tokens"] == services.PAGE_NUMBER_VLM_TOKENS
    assert (
        "22" not in page.final_text.splitlines()[-1] or "٢٢" not in page.final_text
    )  # number is metadata only


def test_printed_number_is_cleared_when_the_three_readers_disagree(page):
    add_regions(page)
    fakes = engines()
    for name, digit in (("qari_v03", "٧"), ("qari_v02", "٦")):
        fakes[name] = FakeEngine(
            name=name,
            responder=by_kind(
                {"body": PRIMARY_BODY, "footnote": FOOT, "page": PRIMARY_BODY, "page_number": digit}
            ),
        )
        fakes[name].kind = "vlm"
    with registry.override(fakes):
        services.run_fast_ocr(page)  # Tesseract says ٨
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.printed_number == ""  # 8 / 7 / 6: no two voters agree -> unknown, not wrong
