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
from ocr.alignment import align_tokens, build_lines, merged_lines, word_f1
from ocr.engines import pdf_text, registry
from ocr.engines.base import OcrResult
from ocr.engines.fake import FakeEngine
from ocr.engines.qari import max_new_tokens_for, read_model_info, resolve_device
from ocr.engines.tesseract import TesseractEngine, parse_image_to_data
from ocr.models import Line, OcrRun
from ocr.test_numbers import FakeKraken, numbers_page, word  # noqa: F401 - numbers_page: a fixture
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
        "tess": tokens["الكتاب"]["tess"],
        "tc": 90.0,
        "why": ["disagree"],
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


def test_build_lines_flags_tokens_the_secondary_does_not_have_unless_tesseract_read_them():
    """D71: a word Qari v0.2 lacks is sure when Tesseract read the same word, else `alone`."""
    lines = build_lines("قال الأمير في سنة", "قال في سنة", tess_lines("قال الامير في سنة"))
    tok = next(t for t in lines[0]["tokens"] if t["t"] == "الأمير")
    assert tok["conf"] == "high" and tok["alt"] is None and "why" not in tok
    lines = build_lines("قال الأمير في سنة", "قال في سنة", tess_lines("قال الوزير في سنة"))
    tok = next(t for t in lines[0]["tokens"] if t["t"] == "الأمير")
    assert tok["conf"] == "low" and tok["why"] == ["alone"] and tok["tess"] == "الوزير"


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
    assert {r.region.kind for r in by_engine["qari_v02"]} == {"body", "footnote", "page_number"}
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


def test_finalize_page_replaces_every_line_of_a_page_without_review_work(page):
    add_regions(page)
    fakes = engines()
    with registry.override(fakes):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    stale = Line.objects.create(page=page, order=99, text="قديم")
    services.finalize_page(page)
    page.refresh_from_db()
    assert not Line.objects.filter(pk=stale.pk).exists()
    assert list(page.lines.order_by("order").values_list("order", flat=True)) == [0, 1, 2]
    assert page.status == Page.Status.OCR_DONE and page.reviewed_at is None


def test_a_queued_ocr_pass_keeps_the_lines_of_an_approved_page(page):
    # backend-1: review renumbers lines, so new OCR lines cannot be matched to reviewed ones by order
    from review import services as review

    add_regions(page)
    with registry.override(engines()):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    lines = list(page.lines.order_by("order"))
    review.approve_page(page, None, force=True)
    review.delete_line(lines[0])  # the garbage first line
    review.insert_line(page, lines[-1].pk, "سطر مكتوب")  # a line the models dropped, after the last one
    page.refresh_from_db()
    kept = list(page.lines.order_by("order").values_list("id", "order", "region_id", "text", "is_reviewed"))
    final_text, stamp = page.final_text, page.reviewed_at

    with pytest.raises(ValueError, match="أعد فتحها"):
        run_stage(page, "ocr")  # an approved page is refused outright
    with registry.override(engines()):
        services.run_full_ocr(page)  # ... and a pass queued before the approval changes nothing
    page.refresh_from_db()
    assert (
        list(page.lines.order_by("order").values_list("id", "order", "region_id", "text", "is_reviewed"))
        == kept
    )
    assert page.final_text == final_text
    assert final_text.count("(1) حاشية أولى") == 1 and final_text.endswith("سطر مكتوب")
    assert page.status == Page.Status.REVIEWED and page.reviewed_at == stamp
    assert page.text_state == Page.TextState.FINAL


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


def test_ocr_tasks_do_nothing_while_the_book_awaits_the_start(page):
    # D64: the gate; nothing is read before «بدء المعالجة», and the page and its runs stay as they are
    add_regions(page)
    Book.objects.filter(pk=page.book_id).update(awaits_ocr_start=True, status=Book.Status.NEEDS_GUIDES)
    before = Page.objects.filter(pk=page.pk).values("status", "text_state", "task_id", "error_message").get()
    with (
        mock.patch.object(services, "run_fast_ocr") as fast,
        mock.patch.object(services, "run_full_ocr") as full,
    ):
        assert tasks.ocr_page_fast.apply(args=[page.pk]).get() == page.pk
        assert tasks.ocr_page_full.apply(args=[page.pk]).get() == page.pk
    fast.assert_not_called()
    full.assert_not_called()
    after = Page.objects.filter(pk=page.pk).values("status", "text_state", "task_id", "error_message").get()
    assert after == before and not OcrRun.objects.filter(page=page).exists()


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
        "tess": None,
        "tc": 90.0,  # Tesseract's confidence of the word it took (D71)
        "why": ["number"],
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
    # readings popover on uncertain words: present, view-only (no form controls inside it)
    assert 'class="tok-pop"' in html and "قراءات هذه الكلمة" in html and "tokenOptions(hover.tok)" in html
    assert "showTok($event, tok)" in html and 'role="tooltip"' in html
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


def number_engines(tess_number: str, v03_number: str, v02_number: str, tess_foot=TESS_FOOT, foot=FOOT):
    """Fakes whose page-number readings (Tesseract, primary, secondary) and footnote text are given."""
    fakes = engines()
    fakes["tesseract"] = FakeEngine(
        name="tesseract",
        responder=by_kind(
            {
                "body": tess_result(TESS_BODY),
                "footnote": tess_result(tess_foot),
                "running_header": tess_result(TESS_HEADER),
                "page_number": tess_result(tess_number),
            }
        ),
    )
    for name, body, number in (
        ("qari_v03", PRIMARY_BODY, v03_number),
        ("qari_v02", SECONDARY_BODY, v02_number),
    ):
        fakes[name] = FakeEngine(
            name=name, responder=by_kind({"body": body, "footnote": foot, "page_number": number})
        )
        fakes[name].kind = "vlm"
    return fakes


def test_rerunning_only_the_fast_pass_keeps_the_final_text_and_the_voted_number(page):
    # backend-3: an `ocr_fast` re-run refreshes the provisional text only
    from review import services as review

    add_regions(page)
    with registry.override(number_engines("٢١", "٢١", "٢١")):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.printed_number == "21"
    with registry.override(number_engines("٨", "٢١", "٢١")):  # Tesseract misreads the number this time
        run_stage(page, "ocr_fast")
    page.refresh_from_db()
    assert page.status == Page.Status.OCR_DONE and page.text_state == Page.TextState.FINAL
    assert page.printed_number == "21"
    assert review.review_payload(page, None)["page"]["text_state"] == "final"
    review.approve_page(page, None, force=True)  # still approvable
    # a fast pass that was queued before the approval does not touch the approved page either
    with registry.override(number_engines("٨", "٢١", "٢١")):
        services.run_fast_ocr(page)
    page.refresh_from_db()
    assert page.status == Page.Status.REVIEWED and page.text_state == Page.TextState.FINAL
    assert page.printed_number == "21"


def test_the_stored_page_number_does_not_vote_again(page):
    # backend-3: Tesseract votes with its own reading of the region, not with an earlier result
    add_regions(page)
    with registry.override(number_engines("٨", "٥", "٧")):
        services.run_fast_ocr(page)
        Page.objects.filter(pk=page.pk).update(printed_number="5")  # an earlier vote
        page.refresh_from_db()
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.printed_number == ""  # 8 / 5 / 7: no agreement (the stored 5 does not count twice)


def test_a_number_line_that_is_not_the_page_number_stays_in_the_text(page):
    # backend-4: with a page-number region, only a line repeating the voted number is dropped
    add_regions(page)
    tess_foot = "(١) انظر تاريخ الطبري ج ٢ ص\n٣٤"
    foot = "(١) انظر تاريخ الطبري ج ٢ ص\n٣٤"
    with registry.override(number_engines("٢١", "٢١", "٢١", tess_foot=tess_foot, foot=foot)):
        services.run_fast_ocr(page)
        page.refresh_from_db()
        assert page.provisional_text.endswith("ص\n٣٤") and page.printed_number == "21"
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.printed_number == "21"
    assert page.final_text.endswith("ج 2 ص\n34")
    # a line that repeats the page number read in the region is still dropped
    assert services.strip_page_number_lines("سطر\n— ٢١ —", has_region=True, known="21") == ("سطر", "21")
    assert services.strip_page_number_lines("سطر\n(١٢)", has_region=True, known="21") == ("سطر\n(١٢)", "")
    assert services.strip_page_number_lines("سطر\n(١٢)", has_region=True, known="") == ("سطر\n(١٢)", "")
    assert services.strip_page_number_lines("سطر\n(١٢)") == ("سطر", "12")  # no region: the safety net


def _detected_number_regions(page) -> None:
    from processing import services as processing

    Preprocess.objects.filter(page=page).update(
        page_number_box={"bbox": [50, 182, 70, 196], "position": "bottom"}
    )
    processing.derive_regions(page)
    assert list(page.regions.order_by("order").values_list("kind", flat=True)) == ["body", "page_number"]


def test_a_page_number_region_read_as_words_joins_the_body(page):
    # backend-2: a paragraph's one-word last line taken for the page number is not thrown away
    _detected_number_regions(page)
    with registry.override(number_engines("انتهى", "انتهى.", "انتهى")):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert Preprocess.objects.get(page=page).page_number_box is None
    assert [(r.kind, r.bbox) for r in page.regions.all()] == [("body", [0, 0, W, H])]
    assert page.status == Page.Status.OCR_DONE and page.printed_number == ""
    assert all(line.region_id is not None for line in page.lines.all())
    assert page.lines.exists()
    assert services.reads_as_words("انتهى") and not services.reads_as_words("— ٢٢ —")
    assert not services.reads_as_words("ا") and not services.reads_as_words("+")


def test_a_misread_page_number_is_not_taken_for_words(page):
    # Tesseract reads «٣١» as «اف», but the models read digits: the region stays a page number
    _detected_number_regions(page)
    with registry.override(number_engines("اف", "٣١", "٣١")):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert Preprocess.objects.get(page=page).page_number_box is not None
    assert list(page.regions.order_by("order").values_list("kind", flat=True)) == ["body", "page_number"]
    assert page.printed_number == "31"


def test_build_lines_keeps_tesseract_reading_only_when_it_differs():
    from ocr.alignment import build_lines

    tess = [
        {
            "bbox": [0, 0, 100, 20],
            "words": [
                {"text": "قال", "bbox": [70, 0, 100, 20], "conf": 90},
                {"text": "الأمبر", "bbox": [30, 0, 68, 20], "conf": 60},  # Tesseract misread of الأمير
                {"text": "1966", "bbox": [0, 0, 28, 20], "conf": 80},
            ],
        }
    ]
    lines = build_lines("قال الأمير ١٩٦٦", "قال الأمير ١٩٦٦", tess)
    toks = {t["t"]: t for t in lines[0]["tokens"]}
    assert toks["قال"]["tess"] is None  # same reading: nothing to show
    assert toks["الأمير"]["tess"] == "الأمبر"  # Tesseract's differing word travels with the token
    assert toks["١٩٦٦"]["tess"] is None  # digits compare equal after lenient normalisation


# ---------------------------------------------------------------- word chooser hook (D26) and n_unresolved


def _full_run(page):
    add_regions(page)
    with registry.override(engines()):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    return page


def test_finalize_page_counts_unresolved_words_on_the_page(page):
    page = _full_run(page)
    assert page.n_unresolved == 3 == sum(line.n_low for line in page.lines.all())


def test_word_chooser_defaults_to_the_vote_and_the_placeholder_chooses_nothing(page, settings):
    """D71: the default chooser is the vote; `choose_word` (a future classifier) is not called by it."""
    from ocr import chooser

    assert settings.NASSAKH["WORD_CHOOSER"] == "vote" and chooser.enabled()
    assert chooser.choose_word({"t": "a", "alt": "b", "conf": "low"}, {}) is None
    with mock.patch("ocr.chooser.choose_word") as choose:
        page = _full_run(page)
    choose.assert_not_called()
    settings.NASSAKH = {**settings.NASSAKH, "WORD_CHOOSER": "none"}
    assert not chooser.enabled()
    settings.NASSAKH = {**settings.NASSAKH, "WORD_CHOOSER": "placeholder"}
    services.finalize_page(page)  # enabled, but the placeholder returns None
    page.refresh_from_db()
    assert page.n_unresolved == 3
    assert all(not t.get("res") for line in page.lines.all() for t in line.tokens)


def test_a_patched_word_chooser_resolves_a_word_but_keeps_it_low(page, settings):
    settings.NASSAKH = {**settings.NASSAKH, "WORD_CHOOSER": "test"}
    seen = []

    def pick(token, context):
        seen.append((token["t"], context["candidates"], context["region_kind"]))
        return token["alt"] if token.get("alt") else "not-a-candidate"

    with mock.patch("ocr.chooser.choose_word", side_effect=pick):
        page = _full_run(page)
    assert ("الكتاب", ["الكتاب", "الكتب"], "body") in seen
    line = page.lines.order_by("order").first()
    token = next(t for t in line.tokens if t.get("res"))
    assert token == {**token, "t": "الكتب", "res": "chooser", "conf": "low", "orig": "الكتاب"}
    assert line.text == "قال الأمير في سنة ١٩٦٦ إن الكتب مفيد" and line.ocr_text.endswith("الكتاب مفيد")
    assert line.n_low == 1  # only the number is left
    assert page.n_unresolved == 2 and "إن الكتب مفيد" in page.final_text


# ---------------------------------------------------------------- run placement and merged lines


def _line(words: list[tuple[str, int]], y: int, h: int = 20) -> dict:
    """A Tesseract line at `y` whose words are `(text, x0)` pairs, 10 px wide each."""
    boxes = [{"text": t, "bbox": [x, y, x + 10, y + h], "conf": 90.0} for t, x in words]
    return {"bbox": [min(x for _, x in words), y, max(x for _, x in words) + 10, y + h], "words": boxes}


def test_build_lines_moves_a_run_onto_the_garbage_that_starts_the_next_line():
    # Book 12 page 2: «وترجم التربة» end up on the next line, where Tesseract read them as "pl rr 4".
    lines = [
        _line([("امتد", 90), ("حتى", 70), ("غات", 50)], 0),
        _line([("pl", 110), ("rr", 95), ("4", 80), ("الصلصالية", 60), ("التي", 40), ("تشاهد", 20)], 30),
    ]
    built = build_lines("امتد حتى غات وترجم التربة الصلصالية التي تشاهد", None, lines)
    assert [b["text"] for b in built] == ["امتد حتى غات", "وترجم التربة الصلصالية التي تشاهد"]
    moved = built[1]["tokens"][:2]
    assert all(t["bbox"] is None for t in moved)  # two words, three garbage words: no one-to-one boxes
    assert built[1]["n_unseen"] == 0 and built[0]["n_unseen"] == 0

    # as many garbage words as words: each word takes the box (and the reading) of its garbage word
    lines[1] = _line([("pl", 110), ("rr", 95), ("الصلصالية", 60), ("التي", 40), ("تشاهد", 20)], 30)
    built = build_lines("امتد حتى غات وترجم التربة الصلصالية التي تشاهد", None, lines)
    first, second = built[1]["tokens"][:2]
    assert first["bbox"] == [110, 30, 120, 50] and first["tess"] == "pl"
    assert second["bbox"] == [95, 30, 105, 50] and second["tess"] == "rr"


def test_build_lines_gives_a_run_to_the_line_without_anchors_between_its_anchors():
    lines = [
        _line([("قال", 90), ("الأمير", 70)], 0),
        {**_line([("xq", 90), ("zzv", 70), ("kk", 50)], 30), "rescued": True},
        _line([("ثم", 90), ("انتهى", 70)], 60),
    ]
    built = build_lines("قال الأمير وهذا سطر ثالث ثم انتهى", None, lines)
    assert [b["text"] for b in built] == ["قال الأمير", "وهذا سطر ثالث", "ثم انتهى"]
    assert built[1]["rescued"] is True and built[1]["bbox"] == [50, 30, 100, 50]
    assert [t["bbox"][0] for t in built[1]["tokens"]] == [90, 70, 50]  # three words, three boxes


def test_build_lines_keeps_words_tesseract_never_saw_on_the_previous_line_and_counts_them():
    lines = [_line([("قال", 90), ("الأمير", 70)], 0), _line([("ثم", 90), ("انتهى", 70)], 30)]
    built = build_lines("قال الأمير وهذا سطر ضائع هنا . ثم انتهى", None, lines)
    assert built[0]["text"] == "قال الأمير وهذا سطر ضائع هنا ."
    assert built[0]["n_unseen"] == 4  # the full stop does not count
    assert services.trailing_unanchored(built[0]["tokens"]) == 5
    # a full stop alone between two lines ends the first one and is not "unseen"
    built = build_lines("قال الأمير . ثم انتهى", None, lines)
    assert built[0]["text"] == "قال الأمير ." and built[0]["n_unseen"] == 0
    # nor are numbers and one-letter abbreviations (Tesseract misreads them too often)
    built = build_lines("قال الأمير ١٢ ه ، ٣ م . ثم انتهى", None, lines)
    assert built[0]["n_unseen"] == 0 and services.trailing_unanchored(built[0]["tokens"]) == 6


def test_build_lines_punctuation_rides_with_its_word_to_the_next_line():
    lines = [
        _line([("بين", 90), ("الفحمي", 70)], 0),
        _line([("Gala", 110), ("ويربط", 90), ("هذا", 70)], 30),
    ]
    built = build_lines("بين الفحمي والنوبي . ويربط هذا", None, lines)
    assert [b["text"] for b in built] == ["بين الفحمي", "والنوبي . ويربط هذا"]
    assert built[1]["tokens"][0]["bbox"] == [110, 30, 120, 50] and built[1]["tokens"][1]["bbox"] is None


def test_build_lines_a_word_after_a_reference_the_primary_left_out_starts_the_next_line():
    # Book 12 page 2: Tesseract read «التاريخ (٧٢). ⏎ تقع فزان» as "التاريخ (VY).‏ ⏎ fe فزان" and Qari
    # left the reference out: «تقع» belongs to the garbage "fe" that starts the next line, not to
    # the bracketed tail of the line before; the full stop ends the line before.
    lines = [
        _line([("قبل", 110), ("التاريخ", 90), ("(VY).\u200f", 70)], 0),
        _line([("fe", 110), ("فزان", 90), ("اليوم", 70)], 30),
    ]
    built = build_lines("قبل التاريخ . تقع فزان اليوم", None, lines)
    assert [b["text"] for b in built] == ["قبل التاريخ .", "تقع فزان اليوم"]
    assert built[1]["tokens"][0]["bbox"] == [110, 30, 120, 50] and built[1]["tokens"][0]["tess"] == "fe"
    # when the primary has the reference, each takes its own garbage word
    built = build_lines("قبل التاريخ (٧٢) . تقع فزان اليوم", None, lines)
    assert [b["text"] for b in built] == ["قبل التاريخ (٧٢) .", "تقع فزان اليوم"]
    # a bracketed reference that starts the next line (a note marker) still goes there
    notes = [
        _line([("قال", 110), ("كذا", 90)], 0),
        _line([("(VY)", 110), ("انظر", 90), ("المصدر", 70)], 30),
    ]
    built = build_lines("قال كذا (١٢) انظر المصدر", None, notes)
    assert [b["text"] for b in built] == ["قال كذا", "(١٢) انظر المصدر"]


def test_merged_lines_reports_unseen_words_and_overlong_lines():
    def line(n: int, unseen: int = 0, width: int = 100, read: float = 1.0) -> dict:
        tess = max(1, n - unseen)
        return {
            "tokens": [{"t": "كلمة"}] * n,
            "n_unseen": unseen,
            "bbox": [0, 0, width, 20],
            "tess_words": tess,
            "tess_matched": round(read * tess),
        }

    assert merged_lines([line(9), line(10), line(9), line(11)]) == []
    assert merged_lines([line(9), line(12, unseen=3), line(9)]) == [1]  # three words nobody saw
    assert merged_lines([line(9), line(12, unseen=3, read=0.25), line(9)]) == []  # a line read as garbage
    many = [line(9), line(10), line(9), line(10)]
    assert merged_lines([*many, line(19, unseen=1)]) == [4]  # 19 words where 100 px hold about 9.5
    assert merged_lines([*many, line(19)]) == []  # ... but Tesseract saw every one of them
    assert merged_lines([*many, line(19, unseen=1, width=200)]) == []  # a line twice as wide
    assert merged_lines([line(9), line(19, unseen=1)]) == []  # too few lines for a density


# ---------------------------------------------------------------- line rescue (Tesseract stage)

BAND_A, BAND_B, BAND_C = (30, 45), (60, 75), (90, 105)  # printed lines (ink rows) of the body region
TESS_A, TESS_B, TESS_C = "قال الأمير في سنة", "وهذا سطر ثان من المتن", "ثم انتهى الكلام هنا"
QARI_ABC = "قال الأمير في سنة وهذا سطر ثانٍ من المتن ثم انتهى الكلام هنا"


def _inked_page(page: Page, detected: tuple[tuple[int, int], ...] = (BAND_A, BAND_B, BAND_C)) -> Region:
    """Three printed lines in the B&W image, the detector's core bands for `detected`, one body region."""
    bw = np.full((H, W), 255, dtype=np.uint8)
    for y0, y1 in (BAND_A, BAND_B, BAND_C):
        bw[y0:y1, 10:110] = 0
    pre = page.preprocess
    save_array(pre.bw_image, bw, "bw.png")
    pre.line_boxes = [{"x0": 10, "y0": y0 + 5, "x1": 110, "y1": y0 + 10} for y0, _ in detected]
    pre.median_line_height = 5
    pre.save()
    return Region.objects.create(page=page, kind="body", bbox=[0, 20, W, 120], order=0)


def _tess_body(texts: list[tuple[str, tuple[int, int]]]) -> OcrResult:
    """Tesseract's answer for the body crop (origin y=20): one line per `(text, band)`."""
    lines = []
    for text, (y0, y1) in texts:
        lines += tess_lines(text, y0=y0 - 22, line_h=y1 - y0 + 4)
    return OcrResult(
        text="\n".join(t for t, _ in texts), duration_s=0.1, finish="n/a", extra={"lines": lines}
    )


def _psm7(text: str, conf: float = 90.0) -> OcrResult:
    """A single-line reading of a rescue crop (padded 20 px, rows 57-78: the text sits at y 23-38)."""
    words = text.split()
    step = 100 // max(len(words), 1)
    boxes = [
        {"text": t, "bbox": [130 - (i + 1) * step, 23, 130 - i * step, 38], "conf": conf}
        for i, t in enumerate(words)
    ]
    return OcrResult(
        text=text,
        duration_s=0.05,
        finish="n/a",
        extra={"lines": [{"bbox": [30, 23, 130, 38], "words": boxes}]},
    )


def _rescue_engines(tesseract_found, rescue_reading) -> dict[str, FakeEngine]:
    """Fakes for one body region: Tesseract finds `tesseract_found`; a rescue crop reads `rescue_reading`."""

    def tesseract(path: Path, cap):
        return rescue_reading if path.stem.startswith("rescue") else tesseract_found

    fakes = engines(primary_body=QARI_ABC, secondary_body=QARI_ABC)
    fakes["tesseract"] = FakeEngine(name="tesseract", responder=tesseract)
    return fakes


def _rescue_calls(engine: FakeEngine) -> list[dict]:
    return [c for c in engine.calls if Path(c["path"]).stem.startswith("rescue")]


def test_a_line_tesseract_missed_is_rescued_into_its_own_line(page):
    region = _inked_page(page)
    fakes = _rescue_engines(_tess_body([(TESS_A, BAND_A), (TESS_C, BAND_C)]), _psm7(TESS_B))
    with registry.override(fakes):
        services.run_fast_ocr(page)
        run = page.ocr_runs.get(engine_name="tesseract", region=region)
        assert [line.get("rescued", False) for line in run.params["lines"]] == [False, True, False]
        assert run.params["rescue"] == {"tried": 1, "added": 1}
        assert run.parsed_text == "\n".join([TESS_A, TESS_B, TESS_C]) == page.provisional_text
        calls = _rescue_calls(fakes["tesseract"])
        assert len(calls) == 1 and calls[0]["hints"] == {"psm": 7}
        assert calls[0]["image_size"] == [W + 40, 21 + 40]  # rows 57-78 of the region, padded
        rescued = run.params["lines"][1]
        assert rescued["bbox"][1] == 57 + 23 - 20 and rescued["bbox"][3] == 57 + 38 - 20  # gray-image rows
        services.run_full_ocr(page)
    page.refresh_from_db()
    lines = list(page.lines.order_by("order"))
    assert [line.text for line in lines] == [
        "قال الأمير في سنة",
        "وهذا سطر ثانٍ من المتن",
        "ثم انتهى الكلام هنا",
    ]
    assert lines[1].bbox == rescued["bbox"] and all(t["bbox"] for t in lines[1].tokens)
    assert "lines_merged" not in page.attention_flags


def test_a_short_line_the_detector_missed_is_found_in_the_gap(page):
    _inked_page(page, detected=(BAND_A, BAND_C))  # the projection detector saw only two lines
    fakes = _rescue_engines(_tess_body([(TESS_A, BAND_A), (TESS_C, BAND_C)]), _psm7(TESS_B))
    with registry.override(fakes):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.lines.count() == 3 and len(_rescue_calls(fakes["tesseract"])) == 1


def test_a_noisy_reading_adds_nothing_and_the_page_is_flagged_merged(page):
    region = _inked_page(page)
    fakes = _rescue_engines(_tess_body([(TESS_A, BAND_A), (TESS_C, BAND_C)]), _psm7("ee TT", conf=30))
    with registry.override(fakes):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    run = page.ocr_runs.get(engine_name="tesseract", region=region)
    assert run.params["rescue"] == {"tried": 1, "added": 0} and len(run.params["lines"]) == 2
    page.refresh_from_db()
    lines = list(page.lines.order_by("order"))
    assert lines[0].text == "قال الأمير في سنة وهذا سطر ثانٍ من المتن"  # still two printed lines in one
    assert "lines_merged" in page.attention_flags
    from core.templatetags.nassakh import FLAG_LABELS

    assert FLAG_LABELS["lines_merged"] == "سطران مطبوعان في سطر واحد"


def test_nothing_changes_on_a_page_where_tesseract_found_every_line(page):
    region = _inked_page(page)
    found = _tess_body([(TESS_A, BAND_A), (TESS_B, BAND_B), (TESS_C, BAND_C)])
    fakes = _rescue_engines(found, _psm7("لا يُقرأ"))
    with registry.override(fakes):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    assert _rescue_calls(fakes["tesseract"]) == []
    run = page.ocr_runs.get(engine_name="tesseract", region=region)
    assert run.params["lines"] == services._offset_lines(found.extra["lines"], 0, 20)
    assert run.params["rescue"] == {"tried": 0, "added": 0}
    assert run.parsed_text == "\n".join([TESS_A, TESS_B, TESS_C])
    page.refresh_from_db()
    assert page.lines.count() == 3 and "lines_merged" not in page.attention_flags


def test_rescue_is_idempotent_on_a_stored_run(page, tmp_path):
    region = _inked_page(page)
    fakes = _rescue_engines(_tess_body([(TESS_A, BAND_A), (TESS_C, BAND_C)]), _psm7(TESS_B))
    with registry.override(fakes):
        services.run_fast_ocr(page)
        run = page.ocr_runs.get(engine_name="tesseract", region=region)
        target = services.Target(region, region.bbox)
        bw = services._load_field_image(page.preprocess.bw_image, "bw")
        assert services.rescue_lines(page.preprocess, bw, [(target, run)], "tesseract", tmp_path) == 1
    run.refresh_from_db()
    assert sum(1 for line in run.params["lines"] if line.get("rescued")) == 1
    assert run.parsed_text.count(TESS_B) == 1


def test_a_failing_rescue_read_never_fails_the_fast_pass(page):
    region = _inked_page(page)
    found = _tess_body([(TESS_A, BAND_A), (TESS_C, BAND_C)])

    def tesseract(path: Path, cap):
        if path.stem.startswith("rescue"):
            raise RuntimeError("tesseract crashed")
        return found

    fakes = engines()
    fakes["tesseract"] = FakeEngine(name="tesseract", responder=tesseract)
    with registry.override(fakes):
        services.run_fast_ocr(page)
    run = page.ocr_runs.get(engine_name="tesseract", region=region)
    assert run.status == "ok" and run.params["rescue"] == {"tried": 1, "added": 0}
    page.refresh_from_db()
    assert page.provisional_text == TESS_A + "\n" + TESS_C


def test_reading_order_rebuilds_a_line_tesseract_laid_out_left_to_right():
    from ocr.engines.tesseract import reading_order

    def words(*items):
        return [{"text": t, "bbox": [x0, 0, x1, 20]} for t, x0, x1 in items]

    # Book 13 page 3: «إلى مكان آخر في ليبيا ، حيث أنشأوا مدينة» came back as the left run first
    laid_out = words(
        ("حيث", 300, 340), ("مدينة", 200, 280), ("LS", 400, 440), ("إلى", 660, 700), ("في", 500, 530)
    )
    assert [w["text"] for w in reading_order(laid_out)] == ["إلى", "في", "LS", "حيث", "مدينة"]
    # a right-to-left line with a Latin run keeps Tesseract's order (the run stays left to right)
    fine = words(("قال", 300, 360), ("Ibn", 150, 210), ("Khaldun", 220, 280), ("في", 60, 120))
    assert reading_order(fine) == fine
    # overlapping boxes are not an inversion
    close = words(("لتلك", 740, 810), ("المياه", 700, 882), ("الجوفية", 500, 580))
    assert reading_order(close) == close
    # a line laid out left to right with a Latin run: the run itself stays left to right
    mixed = words(("من", 100, 140), ("Never", 300, 360), ("Split", 370, 420), ("الهدف", 600, 660))
    assert [w["text"] for w in reading_order(mixed)] == ["الهدف", "Never", "Split", "من"]


def test_reads_as_text_rejects_specks_and_accepts_words():
    assert services.reads_as_text([{"text": "وهذا", "conf": 91}, {"text": ".", "conf": 80}])
    assert not services.reads_as_text([{"text": "ee", "conf": 34}, {"text": "ل", "conf": 79}])
    assert not services.reads_as_text([{"text": "TT", "conf": 32}, {"text": "ig", "conf": 40}])
    assert not services.reads_as_text([{"text": "ees", "conf": 70}])  # a lone Latin word is a smudge
    assert services.reads_as_text([{"text": "Goodchild,", "conf": 88}, {"text": "148:", "conf": 80}])


# ---------------------------------------------------------------- manage.py rebuild_lines


def _old_page(page: Page) -> tuple[Region, dict[str, FakeEngine]]:
    """A page OCR'd before the rescue existed: Tesseract missed line B and nothing read it again."""
    region = _inked_page(page)
    fakes = _rescue_engines(_tess_body([(TESS_A, BAND_A), (TESS_C, BAND_C)]), _psm7(""))
    with registry.override(fakes):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.lines.count() == 2 and page.status == Page.Status.OCR_DONE
    fakes["tesseract"].responder = lambda path, cap: (
        _psm7(TESS_B) if path.stem.startswith("rescue") else pytest.fail("only rescue crops are read")
    )
    return region, fakes


def _rebuild(fakes, *args: str) -> str:
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    with registry.override(fakes):
        call_command("rebuild_lines", *args, stdout=out)
    return out.getvalue()


def test_rebuild_lines_rescues_and_rebuilds_without_calling_a_model(page):
    region, fakes = _old_page(page)
    model_calls = len(fakes["qari_v03"].calls) + len(fakes["qari_v02"].calls)
    out = _rebuild(fakes, "--book", str(page.book_id))
    assert len(fakes["qari_v03"].calls) + len(fakes["qari_v02"].calls) == model_calls
    page.refresh_from_db()
    assert [line.text for line in page.lines.order_by("order")] == [
        "قال الأمير في سنة",
        "وهذا سطر ثانٍ من المتن",
        "ثم انتهى الكلام هنا",
    ]
    assert page.status == Page.Status.OCR_DONE and "lines_merged" not in page.attention_flags
    run = page.ocr_runs.get(engine_name="tesseract", region=region)
    assert run.params["rescue"] == {"tried": 1, "added": 1}
    row = next(line for line in out.splitlines() if line.split()[:2] == [str(page.book_id), "1"])
    assert row.split() == [str(page.book_id), "1", "2", "->", "3", "1", "->", "0", "1", "0"]


def test_rebuild_lines_dry_run_writes_nothing(page):
    region, fakes = _old_page(page)
    lines_before = list(page.lines.order_by("order").values_list("id", "text", "tokens"))
    run_before = page.ocr_runs.get(engine_name="tesseract", region=region)
    flags = list(page.attention_flags)
    out = _rebuild(fakes, "--book", str(page.book_id), "--page", "1", "--dry-run")
    assert "dry run" in out and "1 line(s) rescued" in out
    page.refresh_from_db()
    assert list(page.lines.order_by("order").values_list("id", "text", "tokens")) == lines_before
    run_after = page.ocr_runs.get(pk=run_before.pk)
    assert run_after.params == run_before.params and run_after.parsed_text == run_before.parsed_text
    assert page.attention_flags == flags and "lines_merged" in flags


def test_rebuild_lines_lists_and_skips_reviewed_edited_and_unfinished_pages(page, book):
    from review.models import LineRevision

    _, fakes = _old_page(page)
    made = []
    for number, damage in ((2, "revision"), (3, "manual"), (4, "reviewed"), (5, "status")):
        other = Page.objects.create(
            book=book, number=number, source_index=number, status=Page.Status.OCR_DONE
        )
        line = Line.objects.create(page=other, order=0, text="سطر", tokens=[{"t": "سطر", "bbox": None}])
        if damage == "revision":
            LineRevision.objects.create(page=other, line=line, action="edit", undone=True)
        elif damage == "manual":
            Line.objects.filter(pk=line.pk).update(is_manual=True)
        elif damage == "reviewed":
            Line.objects.filter(pk=line.pk).update(is_reviewed=True)
        else:
            Page.objects.filter(pk=other.pk).update(status=Page.Status.LAYOUT_DONE)
        made.append(other)
    out = _rebuild(fakes, "--book", str(book.pk))
    assert "skipped 4 page(s)" in out
    for number, reason in ((2, "has review revisions"), (3, "has manual lines"), (4, "has reviewed lines")):
        assert f"book {book.pk} page {number}: {reason}" in out
    assert f"book {book.pk} page 5: status layout_done" in out
    for other in made:
        assert list(other.lines.values_list("text", flat=True)) == ["سطر"]
    page.refresh_from_db()
    assert page.lines.count() == 3  # the untouched page was rebuilt


def test_rebuild_lines_page_needs_a_book():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="--page needs --book"):
        _rebuild({}, "--page", "3")


# ---------------------------------------------------------------- 7b: trust (D71–D73)


def test_select_text_offers_the_clean_start_of_a_looped_run_as_a_partial_second_reading():
    tess = OcrRun(engine_name="tesseract", parsed_text=REF_20)
    good = OcrRun(engine_name="qari_v03", parsed_text=REF_20)
    raw = "كلمة0 كلمة1 كلمه2 كلمة3 " + "واخذ عن جماعة من الفضلاء " * 8
    looped = OcrRun(engine_name="qari_v02", raw_output=raw, parsed_text=raw, looped=True)
    chosen = services.select_reading(good, looped, tess)
    assert chosen.alt_partial and chosen.text == REF_20 and not chosen.fallback
    assert chosen.alt.startswith("كلمة0 كلمة1 كلمه2 كلمة3") and chosen.alt.count("الفضلاء") <= 1
    assert services.select_text(good, looped, tess)[1] == chosen.alt
    before = services.select_reading(good, looped, tess, partial=False)  # the rule before 7b
    assert before.alt is None and not before.alt_partial
    # the primary looped, the secondary passed: no second reading (test_a_looped_primary_gives_…)


# the secondary read «إن الكتب مفيد» where the primary skipped it (Tesseract has it: a group), and
# «الكريم جدا» at the end, which Tesseract does not have (a suggestion)
SKIPPING_PRIMARY = "قال الأمير في سنة ١٩٦٦ وهذا سطر ثانٍ من المتن"
READING_SECONDARY = SECONDARY_BODY + " الكريم جدا"


def test_finalize_page_writes_the_reading_the_groups_and_the_gaps(page):
    from ocr.models import TextGap

    add_regions(page)
    with registry.override(engines(primary_body=SKIPPING_PRIMARY, secondary_body=READING_SECONDARY)):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.reading == {"readers": "two", "partial": False, "groups": 1, "gaps": 1}
    assert "missing_text" in page.attention_flags and "single_reader" not in page.attention_flags
    body = list(page.lines.filter(region__kind="body").order_by("order"))
    group = [t for line in body for t in line.tokens if t.get("ins") == 1]
    assert [t["t"] for t in group] == ["إن", "الكتب", "مفيد"]
    assert all(t["why"] == ["missing"] and t["conf"] == "low" and t.get("res") is None for t in group)
    assert "إن الكتب مفيد" in body[0].text  # in the text, marked (Tesseract's boxes place it)
    gap = TextGap.objects.get(page=page)
    assert (gap.text, gap.status, gap.kind, gap.source) == ("الكريم جدا", "open", "words", "secondary")
    assert gap.line_id == body[-1].pk and gap.after_t == "المتن" and gap.support < 0.7
    assert gap.index == len(body[-1].tokens) - 1
    items = services.page_open_items(page)
    assert (items.groups, items.gaps) == (1, 1) and page.n_unresolved == items.total
    assert sum(line.n_low for line in page.lines.all()) == items.words + items.groups  # no gaps in n_low


def test_a_group_over_two_lines_counts_once_and_the_counts_are_pure():
    tokens_a = [{"t": "أ", "conf": "low", "ins": 1}, {"t": "ب", "conf": "low", "why": ["disagree"]}]
    tokens_b = [{"t": "ج", "conf": "low", "ins": 1}, {"t": "د", "conf": "low", "ins": 2, "res": "secondary"}]
    assert services.count_unresolved(tokens_a) == 2 and services.count_unresolved(tokens_b) == 1
    items = services.open_items([tokens_a, tokens_b], open_gaps=2)
    assert (items.words, items.groups, items.gaps, items.total) == (1, 1, 2, 4)
    assert services.group_of({"ins": True}) is None and services.group_of({"ins": 3}) == 3


def test_a_page_one_model_read_says_so_and_is_flagged(page):
    add_regions(page)
    with registry.override(engines(secondary_body="")):  # the secondary read nothing
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.reading["readers"] == "one" and page.reading["gaps"] == 0
    assert "single_reader" in page.attention_flags
    assert services.readers_of(page.reading) == "one" and services.readers_of({}) == ""


def test_a_page_read_from_tesseract_alone_reads_tesseract(page):
    add_regions(page)
    looping = OcrResult(text=PRIMARY_BODY, duration_s=1.0, output_tokens=2500, finish="length")
    runaway = " ".join(f"كلمة{i}" for i in range(60))
    with registry.override(engines(primary_body=looping, secondary_body=runaway)):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.reading["readers"] == "tesseract" and "ocr_fallback" in page.attention_flags


def test_a_new_ocr_pass_replaces_the_gaps_but_a_page_with_review_work_keeps_them(page):
    from ocr.models import TextGap

    add_regions(page)
    fakes = engines(primary_body=SKIPPING_PRIMARY, secondary_body=READING_SECONDARY)
    with registry.override(fakes):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    first = TextGap.objects.get(page=page).pk
    services.finalize_page(page)
    assert list(TextGap.objects.filter(page=page).values_list("text", flat=True)) == ["الكريم جدا"]
    assert TextGap.objects.get(page=page).pk != first  # replaced with the lines
    page.lines.filter(order=0).update(is_reviewed=True)
    kept = TextGap.objects.get(page=page)
    services.finalize_page(page)
    assert list(TextGap.objects.filter(page=page)) == [kept]


def test_line_kind_follows_the_role_then_the_region():
    assert services.line_kind("footnote", "body") == "footnote"
    assert services.line_kind("body", "footnote") == "footnote"
    assert services.line_kind(None, "footnote") == "footnote"
    for role in ("main", "heading", "subheading", "verse"):
        assert services.line_kind(role, "footnote") == "body"
    assert services.line_kind("body", "body") == "body" and services.line_kind("body", None) == "body"


def test_rebuild_lines_recomputes_reasons_and_reading_and_schedules_the_numbers_pass(page):
    _, fakes = _old_page(page)
    Page.objects.filter(pk=page.pk).update(reading={})  # read before 7b
    for line in page.lines.all():  # tokens stored before 7b: no reasons
        line.tokens = [{k: v for k, v in token.items() if k not in ("why", "tc")} for token in line.tokens]
        line.save(update_fields=["tokens"])
    calls = len(fakes["qari_v03"].calls) + len(fakes["qari_v02"].calls)
    with mock.patch("ocr.numbers.schedule") as schedule:
        _rebuild(fakes, str(page.book_id))
    assert len(fakes["qari_v03"].calls) + len(fakes["qari_v02"].calls) == calls  # no model call
    page.refresh_from_db()
    assert page.reading["readers"] == "two"
    assert any("tc" in token for line in page.lines.all() for token in line.tokens)  # recomputed
    schedule.assert_called()


def test_rebuild_lines_refuses_a_book_whose_manuscript_was_edited(page):
    from django.core.management.base import CommandError

    from editor.models import Manuscript

    _, fakes = _old_page(page)
    Manuscript.objects.create(book=page.book, origin=Manuscript.Origin.EDITOR)
    before = list(page.lines.values_list("id", flat=True))
    with pytest.raises(CommandError, match="--include-edited"):
        _rebuild(fakes, str(page.book_id))
    out = _rebuild(fakes)  # every book: the edited one is listed and left alone
    assert "skipping book(s) with an edited manuscript" in out
    assert list(page.lines.values_list("id", flat=True)) == before
    _rebuild(fakes, str(page.book_id), "--include-edited")
    assert list(page.lines.values_list("id", flat=True)) != before


def test_rebuild_lines_report_writes_nothing_and_measures_the_flags(page, user):
    from review import services as review

    page = _full_run(page)
    line = page.lines.filter(region__kind="body").order_by("order").first()
    index = next(i for i, t in enumerate(line.tokens) if t["t"] == "الكتاب")
    review.resolve_token(line, index, "typed", "الكتابة", user)  # a flagged word the reviewer changed
    review.approve_page(page, user, force=True)
    lines = list(page.lines.order_by("order").values_list("id", "tokens"))
    out = _rebuild(engines(), "--report", str(page.book_id))
    assert list(page.lines.order_by("order").values_list("id", "tokens")) == lines
    assert "nothing is written" in out and f"book {page.book_id}: 1 approved page(s)" in out
    today = next(row for row in out.splitlines() if row.strip().startswith("today"))
    assert "100.0% caught (1 of 1 errors" in today
    rows = __import__("ocr.report", fromlist=["book_rows"]).book_rows(page.book_id)[0]
    assert sum(row.changed for row in rows) == 1


def test_finalize_page_checks_a_year_against_its_value_in_words(page):
    body = "توفي سنة ( ٢٤٢ ) ثلاث واربعين ومايتين وولي ابنه"
    add_regions(page)
    with registry.override(engines(primary_body=body, secondary_body=body)):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    token = next(t for line in page.lines.all() for t in line.tokens if t["t"] == "٢٤٢")
    assert token["why"] == ["number", "year"] and token.get("res") is None
    assert token["sug"] == {"t": "٢٤٣", "src": "words", "label": "من الحروف", "words": "ثلاث واربعين ومايتين"}


def test_the_numbers_pass_checks_the_years_again_after_kraken(page):
    from ocr import numbers

    add_regions(page)
    region = page.regions.get(kind="body")
    line = Line.objects.create(
        page=page,
        order=0,
        region=region,
        tokens=[
            {"t": "٢٤٣", "conf": "low", "why": ["number", "year"], "sug": {"t": "٢٤٢", "src": "words"}},
            {"t": "اثنتين", "conf": "high"},
        ],
    )
    following = Line.objects.create(
        page=page, order=1, region=region, tokens=[{"t": "واربعين", "conf": "high"}, {"t": "ومايتين"}]
    )
    lines = [line, following]
    line.tokens[0]["t"] = "٢٤٢"  # as Kraken read it
    assert numbers.check_years(lines) == 1
    assert line.tokens[0]["res"] == "words" and "sug" not in line.tokens[0]


# ---------------------------------------------------------------- 7 review: rebuild_lines while review works


def _review_during_the_rescue(monkeypatch, act) -> None:
    """`rescue_lines` runs as stored, then the reviewer acts (`act()`) while it would still be reading."""
    real = services.rescue_lines

    def rescue(*args, **kwargs):
        added = real(*args, **kwargs)
        act()
        return added

    monkeypatch.setattr(services, "rescue_lines", rescue)


def test_a_resolve_made_during_the_rescue_stops_the_rebuild(page, monkeypatch):
    from review import services as review
    from review.models import LineRevision

    region, fakes = _old_page(page)
    run = page.ocr_runs.get(engine_name="tesseract", region=region)
    assert services.rebuild_skip_reason(page) == ""
    first = page.lines.order_by("order").first()
    _review_during_the_rescue(monkeypatch, lambda: review.resolve_token(first, 0, "typed", text="كتب"))
    with registry.override(fakes), pytest.raises(services.OcrError, match="has review revisions"):
        services.rebuild_page_lines(page, save=True)
    revision = LineRevision.objects.get(page=page)
    assert not revision.undone  # the resolution stays and can still be undone
    assert page.lines.count() == 2 and page.lines.order_by("order").first().tokens[0]["t"] == "كتب"
    fresh = page.ocr_runs.get(pk=run.pk)
    assert fresh.params == run.params and fresh.parsed_text == run.parsed_text  # the rescue was not saved


def test_an_approval_made_during_the_rescue_stays(page, monkeypatch):
    from review import services as review

    _, fakes = _old_page(page)
    _review_during_the_rescue(monkeypatch, lambda: review.approve_page(page, None, force=True))
    with registry.override(fakes), pytest.raises(services.OcrError, match="status reviewed"):
        services.rebuild_page_lines(page, save=True)
    page.refresh_from_db()
    assert page.status == Page.Status.REVIEWED and page.reviewed_at is not None
    assert page.lines.count() == 2


def test_keeping_reviewed_lines_reads_the_approval_from_the_database(page):
    # a pass that read the page before it was approved still finalises it as approved
    add_regions(page)
    with registry.override(engines()):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    stale = Page.objects.get(pk=page.pk)
    page.lines.update(is_reviewed=True)
    Page.objects.filter(pk=page.pk).update(status=Page.Status.REVIEWED, reviewed_at="2026-09-27T10:00:00Z")
    assert stale.reviewed_at is None
    services.finalize_page(stale)
    page.refresh_from_db()
    assert page.status == Page.Status.REVIEWED and page.reviewed_at is not None


# ---------------------------------------------------------------- 7 review: which model a second reading is


def test_a_looped_primary_gives_the_secondarys_text_no_second_reading():
    # review names `t` Qari v0.3 and `alt` Qari v0.2 on every page: v0.3's clean start offered as the second
    # reading of v0.2's text would be named the wrong way round, so the text stands alone (one reader)
    tess = OcrRun(engine_name="tesseract", parsed_text=REF_20)
    raw = "كلمة0 كلمة1 كلمه2 كلمة3 " + "واخذ عن جماعة من الفضلاء " * 8
    chosen = services.select_reading(
        OcrRun(engine_name="qari_v03", raw_output=raw, parsed_text=raw, looped=True),
        OcrRun(engine_name="qari_v02", parsed_text=REF_20),
        tess,
    )
    assert chosen.source == "qari_v02" and chosen.text == REF_20
    assert chosen.alt is None and not chosen.alt_partial and chosen.reason == "primary:loop"


# ---------------------------------------------------------------- 7 review: the numbers pass moves the gaps


def test_the_numbers_pass_moves_a_lines_suggestions_with_its_words(numbers_page):  # noqa: F811
    from ocr import numbers as nb
    from ocr.models import TextGap

    page, line, _reviewed = numbers_page
    line.tokens = [
        word("المنصور", [80, 10, 95, 20]),
        word("(ع"),
        word("ه"),
        word("–"),
        word("ه"),
        word("م)"),
        word("بأيدي", [60, 10, 75, 20]),
        word("(ه)", [45, 10, 55, 20]),
        word("(هـ)", [30, 10, 40, 20]),
    ]
    line.save(update_fields=["tokens"])
    after_date = TextGap.objects.create(page=page, line=line, index=5, after_t="م)", text="رحمه الله")
    after_letter = TextGap.objects.create(page=page, line=line, index=7, after_t="(ه)", text="تعالى")
    last = TextGap.objects.create(page=page, line=line, index=8, after_t="(هـ)", text="كذا")
    engine = FakeKraken(
        {
            (0, 8, 100, 22): "المنصور (٧٥٤-٧٧٥م) بأيدي (٥) (هـ)",
            (45, 10, 55, 20): "(٥)",
            (30, 10, 40, 20): "(هـ)",
        }
    )
    done = nb.read_page_numbers(page, engine=engine, style=nb.ARABIC_INDIC)
    assert (done.letters, done.dates) == (1, 1)
    line.refresh_from_db()
    assert [t["t"] for t in line.tokens] == ["المنصور", "(٧٥٤–٧٧٥م)", "بأيدي", "(٥)", "(هـ)"]
    # the date's five tokens became one: what came after it stays after it; a rewritten word is followed
    places = {gap.pk: (gap.index, gap.after_t) for gap in TextGap.objects.filter(line=line)}
    assert places == {after_date.pk: (1, "(٧٥٤–٧٧٥م)"), after_letter.pk: (3, "(٥)"), last.pk: (4, "(هـ)")}


def test_a_repeated_unit_is_read_once_before_the_sanity_check():
    """A model that wrote a footnote line twice (book 29 p. 112) passes the check on the text read once,
    and that text is the region's; the run keeps its raw text."""
    head = "(1) كنديته أبو زكرا ، وهو مؤسس الدولة الخفصية ، عين أم كيراً على"
    rest = "افريقية من قبل الموحدين في رجب سنة واستقل بافريقية عنهم"
    raw = f"{head} {head} {rest}"
    primary = OcrRun(engine_name="qari_v03", parsed_text=raw)
    secondary = OcrRun(engine_name="qari_v02", parsed_text=f"{head} {rest}")
    tess = OcrRun(engine_name="tesseract", parsed_text=f"{head} {rest}")
    chosen = services.select_reading(primary, secondary, tess)
    assert chosen.source == "qari_v03" and chosen.text == f"{head} {rest}" and chosen.alt == f"{head} {rest}"
    assert primary.parsed_text == raw
