"""OCR services: fast provisional text, full dual-model OCR, selection and line building.

Flow per page (spec §6-§7):

    run_fast_ocr    Tesseract on every region (B&W crops) → OcrRuns with word boxes → provisional_text.
                    Born-digital books with `use_text_layer`: the repaired text layer becomes the
                    final text at once (Tesseract still runs for geometry) and the page is finalised.
    run_full_ocr    primary and secondary Qari on the OCR-able regions (gray crops, footnotes at 2x,
                    running header / page number skipped) → OcrRuns → finalize_page.
    finalize_page   per region, picks the text from the latest runs (primary → secondary → Tesseract
                    fallback with the `ocr_fallback` flag, D16), anchors the tokens to Tesseract's
                    lines (D12), stores Line rows, `final_text` (Western digits, D6), `ocr_done`.

All coordinates stored on runs and lines are in gray-image pixel space.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models.fields.files import FieldFile

import numpy as np
from PIL import Image

from books.models import Book, Page
from core.arabic import normalize, normalize_ws, parse_output, to_western_digits
from core.images import crop, load_gray, to_png_bytes
from processing.models import Preprocess, Region

from .alignment import build_lines, word_f1
from .engines import registry
from .engines.base import OcrEngine, OcrResult
from .engines.pdf_text import PdfPageRef
from .engines.qari import max_new_tokens_for
from .models import Line, OcrRun

log = logging.getLogger(__name__)

SKIPPED_KINDS: frozenset[str] = frozenset({Region.Kind.RUNNING_HEADER, Region.Kind.PAGE_NUMBER})
FOOTNOTE_KINDS: frozenset[str] = frozenset({Region.Kind.FOOTNOTE})
PAGE_KIND = "page"
PAGE_SCOPE = "page"
REGION_SCOPE = "region"
FLAG_FALLBACK = "ocr_fallback"
FLAG_ALIGNMENT = "alignment_poor"

# A page-number line: only digits (Western, Arabic-Indic, Persian), dashes, dots, brackets, spaces.
_PN_CHARS = r"\s0-9٠-٩۰-۹\-‐‑‒–—―ـ.·•…()\[\]{}﴾﴿<>«»"
_PAGE_NUMBER_LINE = re.compile(rf"^[{_PN_CHARS}]*[0-9٠-٩۰-۹][{_PN_CHARS}]*$")
_DIGITS = re.compile(r"[0-9٠-٩۰-۹]+")
PRINTED_NUMBER_MAX = 20

# Sanity thresholds (D16). A small absolute slack on the upper bound keeps tiny regions (a two-word
# heading read as five words) from failing on the ratio check alone.
MIN_WORD_RATIO = 0.2
MAX_WORD_RATIO = 1.8
WORD_SLACK = 3
MIN_WORD_F1 = 0.45
# Decision (Phase 2 review, extends D16): Tesseract is only a reliable yardstick on regions with some
# text. Below MIN_REF_WORDS_FOR_OVERLAP reference words (two-line footnotes of small type read from
# the 1x B&W crop) exact-token F1 is dominated by Tesseract's noise, so only a generous length check
# remains (runaway output: more than SHORT_REF_MAX_RATIO x the reference + SHORT_REF_SLACK words). An
# empty reference proves nothing: it is inconclusive (`no_reference` fails) unless the output is at
# most NO_REF_MAX_WORDS words; `select_text` still accepts the primary when the two models agree
# (word F1 >= MODELS_AGREE_F1), which is D16's own confidence signal. The loop check always applies.
MIN_REF_WORDS_FOR_OVERLAP = 15
SHORT_REF_MAX_RATIO = 3
SHORT_REF_SLACK = 10
NO_REF_MAX_WORDS = 3
MODELS_AGREE_F1 = 0.6
# Failure reasons that only mean "differs from Tesseract" (not a broken run): agreement can override.
COMPARISON_REASONS: frozenset[str] = frozenset({"no_reference", "too_short", "too_long", "low_overlap"})
# Below this share of tokens anchored to a Tesseract word the page gets the `alignment_poor` flag.
MIN_ANCHOR_RATIO = 0.5

ENGINE_LABELS: dict[str, str] = {
    "qari_v03": "Qari v0.3",
    "qari_v02": "Qari v0.2",
    "tesseract": "Tesseract",
    "pdf_text": "طبقة النص (PDF)",
    "fake": "محرّك تجريبي",
}
VARIANT_LABELS: dict[str, str] = {
    "gray": "رمادية",
    "bw": "أبيض وأسود",
    "gray_2x": "رمادية ×2",
    "pdf": "ملف PDF",
    "ingest": "نص الاستيراد",
}


class OcrError(Exception):
    """Raised with an Arabic, actionable message when a page cannot be OCR'd (stored on the page)."""


@dataclass
class Target:
    """What one engine call looks at: a region, or the whole page when the page has no regions."""

    region: Region | None
    bbox: list[int]

    @property
    def kind(self) -> str:
        return self.region.kind if self.region is not None else PAGE_KIND

    @property
    def scope(self) -> str:
        return REGION_SCOPE if self.region is not None else PAGE_SCOPE

    @property
    def origin(self) -> tuple[int, int]:
        return int(self.bbox[0]), int(self.bbox[1])


@dataclass
class RegionText:
    """The text chosen for one target and what it is built from."""

    target: Target
    text: str
    alt_text: str | None
    tess_lines: list[dict]
    fallback: bool
    reason: str
    source: str


# ---------------------------------------------------------------- settings and inputs


def nassakh() -> dict:
    """`settings.NASSAKH`, read at call time so `override_settings` works in tests."""
    return settings.NASSAKH


def engine_names() -> tuple[str, str, str]:
    """(primary, secondary, fast) engine names from settings."""
    cfg = nassakh()
    return str(cfg["OCR_PRIMARY"]), str(cfg["OCR_SECONDARY"]), str(cfg.get("OCR_FAST", "tesseract"))


def uses_text_layer(page: Page) -> bool:
    """True for pages of born-digital books whose owner kept `use_text_layer` on."""
    book = page.book
    return bool(book.has_text_layer) and bool(book.use_text_layer)


def _preprocess_of(page: Page) -> Preprocess:
    """The page's Preprocess row; OcrError (Arabic) when the page was never preprocessed."""
    try:
        return page.preprocess
    except ObjectDoesNotExist:
        raise OcrError("لا توجد معالجة أولية لهذه الصفحة؛ شغّل مرحلة المعالجة الأولية أولًا.") from None


def _load_field_image(field: FieldFile, label: str) -> np.ndarray:
    """Read a stored image field as a 2-D uint8 array, with an Arabic error when it is missing."""
    if not field or not field.name:
        raise OcrError(f"الصورة {label} غير متوفرة؛ أعد تشغيل المعالجة الأولية.")
    try:
        with field.open("rb") as fh:
            return load_gray(fh)
    except FileNotFoundError:
        raise OcrError(f"ملف الصورة {label} مفقود من التخزين؛ أعد تشغيل المعالجة الأولية.") from None


def _targets(page: Page, shape: tuple[int, ...], ocr_only: bool) -> list[Target]:
    """Regions in order (OCR-able ones only when `ocr_only`), or one page-level target without regions."""
    h, w = int(shape[0]), int(shape[1])
    regions = list(page.regions.order_by("order", "id"))
    if not regions:
        return [Target(None, [0, 0, w, h])]
    if ocr_only:
        regions = [r for r in regions if r.kind not in SKIPPED_KINDS]
    return [Target(r, [int(round(float(v))) for v in r.bbox]) for r in regions]


def _page_shape(page: Page) -> tuple[int, int]:
    """(height, width) of the gray image from the Preprocess row (0, 0 when unknown)."""
    try:
        pre = page.preprocess
    except ObjectDoesNotExist:
        return 0, 0
    return int(pre.output_height), int(pre.output_width)


# ---------------------------------------------------------------- images


PAGE_NUMBER_UPSCALE = 4  # Lanczos factor for the page-number crop before Tesseract
PAGE_NUMBER_PAD = 8  # pixels of white kept around the digits (gray-image space)
PAGE_NUMBER_PSM = 7  # Tesseract "single text line"


def _pad_bbox(bbox: list[int], pad: int, shape: tuple[int, ...]) -> list[int]:
    """Grow `bbox` by `pad` pixels on every side, clamped to an image of `shape` (h, w, ...)."""
    h, w = int(shape[0]), int(shape[1])
    x0, y0, x1, y1 = (int(v) for v in bbox)
    return [max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)]


def _crop_image(array: np.ndarray, bbox: list[int], upscale: int = 1) -> np.ndarray:
    """Crop `bbox` out of `array`; upscale with Lanczos when `upscale > 1` (footnotes, D11)."""
    part = crop(array, bbox)
    if part.size == 0:
        raise OcrError("منطقة فارغة خارج حدود الصورة؛ راجع أدلة التخطيط لهذه الصفحة.")
    if upscale > 1:
        img = Image.fromarray(part)
        img = img.resize((img.width * upscale, img.height * upscale), Image.LANCZOS)
        part = np.asarray(img, dtype=np.uint8)
    return part


def _save_temp(array: np.ndarray, directory: Path, stem: str) -> Path:
    """Write `array` as `<directory>/<stem>.png` for an engine that reads files; returns the path."""
    path = directory / f"{stem}.png"
    path.write_bytes(to_png_bytes(array))
    return path


def _offset_lines(lines: list[dict], dx: int, dy: int, scale: float = 1.0) -> list[dict]:
    """Move Tesseract boxes from crop space into gray-image space (undoing an upscale if any)."""

    def shift(bbox: list[int] | None) -> list[int] | None:
        if not bbox:
            return bbox
        x0, y0, x1, y1 = bbox
        return [
            int(round(x0 / scale + dx)),
            int(round(y0 / scale + dy)),
            int(round(x1 / scale + dx)),
            int(round(y1 / scale + dy)),
        ]

    out = []
    for line in lines:
        words = [{**w, "bbox": shift(w.get("bbox"))} for w in line.get("words", [])]
        out.append({**line, "bbox": shift(line.get("bbox")), "words": words})
    return out


def _json_safe(value: object) -> object:
    """Round-trip through JSON so numpy scalars, Paths and the like become plain JSON types."""
    return json.loads(json.dumps(value, default=str))


# ---------------------------------------------------------------- engine calls


def run_engine(
    page: Page,
    engine_name: str,
    target: Target,
    source: Path | PdfPageRef,
    input_variant: str,
    max_new_tokens: int | None = None,
    scale: float = 1.0,
    hints: dict | None = None,
) -> OcrRun:
    """Call `engine_name` on `source` (image path or PdfPageRef) and store the call as an OcrRun.

    Failures are stored too (`status=error`) so the page keeps a trace of every attempt. Tesseract
    word/line boxes from `result.extra["lines"]` are moved into gray-image coordinates.
    """
    engine = registry.get_engine(engine_name)
    params: dict = {"scope": target.scope, "kind": target.kind}
    if max_new_tokens:
        params["max_new_tokens"] = max_new_tokens
    if hints:
        params["hints"] = dict(hints)
    generation_params = getattr(engine, "generation_params", None)
    if callable(generation_params):
        params.update(generation_params(max_new_tokens or getattr(engine, "default_max_new_tokens", 0)))
    run = OcrRun(
        page=page,
        region=target.region,
        engine_name=engine_name[:40],
        model_id=(engine.model_id or "")[:200],
        model_revision=(engine.model_revision or "")[:64],
        backend=(engine.backend or "")[:20],
        prompt=engine.prompt or "",
        input_variant=input_variant[:20],
        params=params,
    )
    try:
        result = engine.recognize(source, max_new_tokens, hints=hints)
    except Exception as exc:  # noqa: BLE001 - recorded on the run, the page decides what to do
        log.exception("%s failed on page %s (%s)", engine_name, page.pk, target.kind)
        run.status = OcrRun.Status.ERROR
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
        run.save()
        return run
    _fill_run(run, result, engine, target.origin, scale)
    run.save()
    return run


def _fill_run(
    run: OcrRun, result: OcrResult, engine: OcrEngine, origin: tuple[int, int], scale: float
) -> None:
    """Copy an engine result onto `run`: parsed text, loop detection, timing and boxes (gray space)."""
    hit_cap = result.finish == "length"
    if engine.kind == "vlm":
        parsed, looped = parse_output(result.text or "", hit_cap=hit_cap)
        looped = looped or result.looped or hit_cap
    else:
        parsed, looped = normalize_ws(result.text or ""), bool(result.looped)
    run.raw_output = result.text or ""
    run.parsed_text = parsed
    run.duration_ms = max(0, int(round(result.duration_s * 1000)))
    run.output_tokens = result.output_tokens
    run.finish = (result.finish or "")[:20]
    run.looped = looped
    extra = dict(result.extra or {})
    lines = extra.pop("lines", None)
    if lines is not None:
        run.params["lines"] = _offset_lines(lines, origin[0], origin[1], scale)
    run.params.update(_json_safe(extra))


def _run_passes(run: OcrRun, reference: str) -> tuple[bool, str]:
    """`sanity_check` of a stored run against `reference`; a failed engine call is `error`."""
    if run.status != OcrRun.Status.OK:
        return False, "error"
    return sanity_check(run.parsed_text, reference, run.looped)


def _record_check(run: OcrRun, reference: str) -> tuple[bool, str]:
    """`_run_passes` and store its verdict on the run (`params["sanity"]`, shown in the runs list)."""
    ok, reason = _run_passes(run, reference)
    run.params["sanity"] = {"ok": ok, "reason": reason}
    run.save(update_fields=["params"])
    return ok, reason


# ---------------------------------------------------------------- sanity check (D16)


def sanity_check(text: str, reference: str, looped: bool = False) -> tuple[bool, str]:
    """Does a model output look like a reading of the same region as Tesseract's `reference`?

    Fails when the run looped or is empty. With a reference of at least
    `MIN_REF_WORDS_FOR_OVERLAP` words it fails when it has fewer than 20 % or more than 180 % of
    the reference's words (plus a slack of a few words), or when the order-insensitive word F1
    with the reference is below 0.45 (lenient normalisation). A shorter reference only guards
    against runaway output (`short_reference` when it passes); an empty one is inconclusive
    (`no_reference` passes only for at most `NO_REF_MAX_WORDS` words). Returns `(ok, reason)` with
    reason one of `ok`, `short_reference`, `no_reference`, `loop`, `empty`, `too_short`,
    `too_long`, `low_overlap`.
    """
    if looped:
        return False, "loop"
    words = normalize(text or "", "lenient").split()
    if not words:
        return False, "empty"
    ref = normalize(reference or "", "lenient").split()
    if not ref:
        return len(words) <= NO_REF_MAX_WORDS, "no_reference"
    if len(ref) < MIN_REF_WORDS_FOR_OVERLAP:
        if len(words) > SHORT_REF_MAX_RATIO * len(ref) + SHORT_REF_SLACK:
            return False, "too_long"
        return True, "short_reference"
    if len(words) < MIN_WORD_RATIO * len(ref):
        return False, "too_short"
    if len(words) > MAX_WORD_RATIO * len(ref) + WORD_SLACK:
        return False, "too_long"
    if word_f1(words, ref) < MIN_WORD_F1:
        return False, "low_overlap"
    return True, "ok"


# ---------------------------------------------------------------- fast OCR (default queue)

TESSERACT_HEADLINE = "تعذّر تشغيل Tesseract على هذه الصفحة؛ تحقّق من تثبيت tesseract وحزمة اللغة العربية."
TESSERACT_HINT = "brew install tesseract tesseract-lang"


def _load_fast_engine(name: str) -> None:
    """Load the fast engine now so a missing install fails with the actionable Arabic headline."""
    try:
        registry.get_engine(name)
    except Exception as exc:  # noqa: BLE001 - configuration problem: stop with an Arabic message
        raise OcrError(f"{TESSERACT_HEADLINE}\n{TESSERACT_HINT}\n{type(exc).__name__}: {exc}") from exc


def run_fast_ocr(page: Page) -> None:
    """Tesseract on every region of the B&W image (page-level without regions); provisional text.

    Every region gets an OcrRun with word/line boxes in `params["lines"]`. `provisional_text` joins
    the body-like regions in order, then a blank line, then the footnotes; running header and page
    number are omitted, and a leading / trailing line that is only a page number is dropped. The
    page-number region's digits (else the dropped line's) are stored in `printed_number`.
    Born-digital pages with `use_text_layer` are finalised right away from the repaired text layer.
    """
    _, _, fast = engine_names()
    pre = _preprocess_of(page)
    bw = _load_field_image(pre.bw_image, "بالأبيض والأسود")
    _load_fast_engine(fast)
    targets = _targets(page, bw.shape, ocr_only=False)
    texts: list[tuple[str, str]] = []
    failures: list[str] = []
    printed = ""
    with tempfile.TemporaryDirectory(prefix="nassakh-ocr-fast-") as tmp:
        tmpdir = Path(tmp)
        for i, target in enumerate(targets):
            if target.kind == Region.Kind.PAGE_NUMBER:
                # A printed page number is a few digits ~30 px tall: Tesseract needs the crop padded,
                # enlarged and read as a single line to return anything at all.
                target = Target(target.region, _pad_bbox(target.bbox, PAGE_NUMBER_PAD, bw.shape))
                crop_img = _crop_image(bw, target.bbox, upscale=PAGE_NUMBER_UPSCALE)
                path = _save_temp(crop_img, tmpdir, f"bw-{i}-{target.kind}")
                run = run_engine(
                    page,
                    fast,
                    target,
                    path,
                    f"bw_{PAGE_NUMBER_UPSCALE}x",
                    scale=PAGE_NUMBER_UPSCALE,
                    hints={"psm": PAGE_NUMBER_PSM},
                )
            else:
                path = _save_temp(_crop_image(bw, target.bbox), tmpdir, f"bw-{i}-{target.kind}")
                run = run_engine(page, fast, target, path, "bw")
            if run.status != OcrRun.Status.OK:
                failures.append(run.error)
                continue
            if target.kind == Region.Kind.PAGE_NUMBER:
                printed = printed or printed_number_of(run.parsed_text)
            elif target.kind not in SKIPPED_KINDS:
                texts.append((target.kind, run.parsed_text))
    if failures and len(failures) == len(targets):
        raise OcrError(f"{TESSERACT_HEADLINE}\n{failures[0]}")

    provisional, stripped = strip_page_number_lines(join_region_texts(texts))
    page.provisional_text = provisional
    page.printed_number = printed or stripped
    page.text_state = Page.TextState.PROVISIONAL
    page.save(update_fields=["provisional_text", "printed_number", "text_state"])

    if uses_text_layer(page):
        if _run_text_layer(page) is not None:
            finalize_page(page)


def _run_text_layer(page: Page) -> OcrRun | None:
    """Store a `pdf_text` run for a born-digital page (falls back to the ingest text layer).

    Returns None when both sources are empty.
    """
    book = page.book
    h, w = _page_shape(page)
    target = Target(None, [0, 0, w, h])
    run: OcrRun | None = None
    with tempfile.TemporaryDirectory(prefix="nassakh-ocr-pdf-") as tmp:
        pdf_path = _local_pdf_path(book, Path(tmp))
        if pdf_path is not None:
            ref = PdfPageRef(
                pdf_path=str(pdf_path),
                index=int(page.source_index),
                half=page.source_half,
                split_ratio=float(page.split_ratio_override or book.split_ratio or 0.5),
            )
            run = run_engine(page, "pdf_text", target, ref, "pdf")
    if run is not None and run.status == OcrRun.Status.OK and run.parsed_text.strip():
        page.provisional_text, _number = strip_page_number_lines(run.parsed_text)
        page.save(update_fields=["provisional_text"])
        return run
    fallback_text = normalize_ws(page.text_layer_text or "")
    if not fallback_text:
        return None
    run = OcrRun.objects.create(
        page=page,
        region=None,
        engine_name="pdf_text",
        model_id="pymupdf:get_text",
        backend="pdf",
        input_variant="ingest",
        raw_output=page.text_layer_text,
        parsed_text=fallback_text,
        params={"scope": PAGE_SCOPE, "kind": PAGE_KIND, "source": "text_layer_text"},
        finish="n/a",
    )
    page.provisional_text, _number = strip_page_number_lines(fallback_text)
    page.save(update_fields=["provisional_text"])
    return run


def _local_pdf_path(book, tmpdir: Path) -> Path | None:
    """A filesystem path to the book's PDF (copied to `tmpdir` for non-local storages)."""
    field = book.source_pdf
    if not field or not field.name:
        return None
    try:
        return Path(field.path)
    except (NotImplementedError, AttributeError):
        pass
    try:
        target = tmpdir / "source.pdf"
        with field.open("rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        return target
    except FileNotFoundError:
        return None


def _latest_text_layer_run(page: Page) -> OcrRun | None:
    """Newest successful, non-empty page-level `pdf_text` run of the page, if any."""
    return (
        page.ocr_runs.filter(engine_name="pdf_text", region__isnull=True, status=OcrRun.Status.OK)
        .exclude(parsed_text="")
        .order_by("-created_at", "-id")
        .first()
    )


# ---------------------------------------------------------------- full OCR (gpu queue)


def run_full_ocr(page: Page) -> None:
    """Primary and secondary Qari on every OCR-able region, Tesseract as the reference; then finalise.

    Body, heading, poetry and other regions are sent as gray crops; footnotes are upscaled 2x
    (`gray_2x`); running header and page number are skipped. The primary output is checked against
    Tesseract with `sanity_check`; the secondary runs in every case (as the candidate when the
    primary failed, as the source of alternatives when it passed). `finalize_page` then selects.
    Born-digital pages that already have a text-layer run are only finalised.
    """
    primary, secondary, fast = engine_names()
    if uses_text_layer(page) and _latest_text_layer_run(page) is not None:
        finalize_page(page)
        return

    pre = _preprocess_of(page)
    gray = _load_field_image(pre.gray_image, "الرمادية")
    targets = _targets(page, gray.shape, ocr_only=True)
    upscale = int(nassakh().get("FOOTNOTE_UPSCALE", 2) or 1)
    for name in (primary, secondary):
        try:
            registry.get_engine(name)
        except Exception as exc:  # noqa: BLE001 - configuration problem: stop with an Arabic message
            raise OcrError(
                f"تعذّر تحميل محرّك التعرّف «{name}»؛ تحقّق من إعدادات OCR_MODELS_DIR وOCR_BACKEND.\n"
                f"{type(exc).__name__}: {exc}"
            ) from exc

    bw: np.ndarray | None = None
    model_errors: list[str] = []
    n_model_runs = 0
    with tempfile.TemporaryDirectory(prefix="nassakh-ocr-full-") as tmp:
        tmpdir = Path(tmp)
        for i, target in enumerate(targets):
            tess = _latest_runs(page, target).get(fast)
            if tess is None or tess.status != OcrRun.Status.OK:
                if bw is None:
                    bw = _load_field_image(pre.bw_image, "بالأبيض والأسود")
                    _load_fast_engine(fast)
                bw_path = _save_temp(_crop_image(bw, target.bbox), tmpdir, f"bw-{i}-{target.kind}")
                tess = run_engine(page, fast, target, bw_path, "bw")
            reference = tess.parsed_text if tess.status == OcrRun.Status.OK else ""

            scale = upscale if target.kind in FOOTNOTE_KINDS and upscale > 1 else 1
            variant = f"gray_{scale}x" if scale > 1 else "gray"
            image_path = _save_temp(
                _crop_image(gray, target.bbox, scale), tmpdir, f"{variant}-{i}-{target.kind}"
            )
            cap = max_new_tokens_for(target.kind)

            primary_run = run_engine(page, primary, target, image_path, variant, cap, scale)
            ok, reason = _record_check(primary_run, reference)
            if not ok:
                log.info("page %s %s: primary %s failed sanity (%s)", page.pk, target.kind, primary, reason)
            secondary_run = run_engine(page, secondary, target, image_path, variant, cap, scale)
            _record_check(secondary_run, reference)
            n_model_runs += 2
            model_errors += [r.error for r in (primary_run, secondary_run) if r.status == OcrRun.Status.ERROR]
    if n_model_runs and len(model_errors) == n_model_runs:
        # Every model call crashed (out of memory, broken weights...): this is an engine failure,
        # not a page to finalise from Tesseract; the page goes to `error` and can be retried.
        raise OcrError(
            "تعذّر تشغيل نماذج التعرّف على هذه الصفحة؛ راجع سجل عامل GPU ثم أعد تشغيل المرحلة.\n"
            f"{model_errors[0]}"
        )
    finalize_page(page)


# ---------------------------------------------------------------- selection and finalisation


def _latest_runs(page: Page, target: Target) -> dict[str, OcrRun]:
    """Most recent run per engine name for `target` (page-level runs are those with `scope=page`)."""
    if target.region is not None:
        qs = page.ocr_runs.filter(region=target.region)
    else:
        qs = page.ocr_runs.filter(region__isnull=True, params__scope=PAGE_SCOPE)
    latest: dict[str, OcrRun] = {}
    for run in qs.order_by("-created_at", "-id"):
        latest.setdefault(run.engine_name, run)
    return latest


def select_text(
    primary: OcrRun | None, secondary: OcrRun | None, tesseract: OcrRun | None
) -> tuple[str, str | None, bool, str, str]:
    """Pick the text of one region from its latest runs (D16).

    Returns `(text, alt_text, fallback, reason, source)`: the primary when it passes the sanity
    check (alternatives from the secondary when that passes too), else the secondary when it
    passes, else the primary when both only differ from Tesseract (`COMPARISON_REASONS`) but agree
    with each other (`models_agree`), else Tesseract's text with `fallback=True`.
    """
    reference = (
        tesseract.parsed_text if tesseract is not None and tesseract.status == OcrRun.Status.OK else ""
    )

    def passes(run: OcrRun | None) -> tuple[bool, str]:
        if run is None:
            return False, "missing"
        return _run_passes(run, reference)

    p_ok, p_reason = passes(primary)
    s_ok, s_reason = passes(secondary)
    if p_ok:
        alt = secondary.parsed_text if s_ok else None
        return primary.parsed_text, alt, False, p_reason, primary.engine_name
    if s_ok:
        return secondary.parsed_text, None, False, f"primary:{p_reason}", secondary.engine_name
    if (
        p_reason in COMPARISON_REASONS
        and s_reason in COMPARISON_REASONS
        and word_f1(
            normalize(primary.parsed_text, "lenient").split(),
            normalize(secondary.parsed_text, "lenient").split(),
        )
        >= MODELS_AGREE_F1
    ):
        # Both differ from Tesseract only, and agree with each other: trust the models (D16).
        return primary.parsed_text, secondary.parsed_text, False, "models_agree", primary.engine_name
    source = tesseract.engine_name if tesseract is not None else ""
    return reference, None, True, f"primary:{p_reason} secondary:{s_reason}", source


def _collect_region_texts(page: Page) -> list[RegionText]:
    """Selected text of every OCR-able target (or the text-layer page) with its Tesseract lines."""
    primary, secondary, fast = engine_names()
    h, w = _page_shape(page)
    if uses_text_layer(page):
        pdf_run = _latest_text_layer_run(page)
        if pdf_run is not None:
            tess_lines: list[dict] = []
            for target in _targets(page, (h, w), ocr_only=False):
                tess = _latest_runs(page, target).get(fast)
                if tess is not None and tess.status == OcrRun.Status.OK:
                    tess_lines.extend(tess.params.get("lines") or [])
            page_target = Target(None, [0, 0, w, h])
            return [
                RegionText(
                    page_target, pdf_run.parsed_text, None, tess_lines, False, "text_layer", "pdf_text"
                )
            ]

    out: list[RegionText] = []
    for target in _targets(page, (h, w), ocr_only=True):
        runs = _latest_runs(page, target)
        tess = runs.get(fast)
        text, alt, fallback, reason, source = select_text(runs.get(primary), runs.get(secondary), tess)
        lines = (
            list(tess.params.get("lines") or [])
            if tess is not None and tess.status == OcrRun.Status.OK
            else []
        )
        out.append(RegionText(target, text, alt, lines, fallback, reason, source))
    return out


def join_region_texts(items: list[tuple[str, str]]) -> str:
    """Join `(kind, text)` pairs: body-like kinds in order, then a blank line, then the footnotes."""
    main = [t.strip() for k, t in items if k not in FOOTNOTE_KINDS and k not in SKIPPED_KINDS and t.strip()]
    foot = [t.strip() for k, t in items if k in FOOTNOTE_KINDS and t.strip()]
    parts = []
    if main:
        parts.append("\n".join(main))
    if foot:
        parts.append("\n".join(foot))
    return "\n\n".join(parts)


def page_number_digits(line: str) -> str | None:
    """Western digits of `line` when it is only a page number («— ٢٢ —», «(20)», «٢٠»), else None."""
    text = (line or "").strip()
    if not text or not _PAGE_NUMBER_LINE.match(text):
        return None
    digits = to_western_digits("".join(_DIGITS.findall(text)))
    return digits[:PRINTED_NUMBER_MAX] or None


def page_number_edges(lines: list[str]) -> tuple[set[int], str]:
    """Indices of the first / last non-empty lines that are only a page number, and the number.

    `digits` is the Western number of a dropped line (the last one when both are numbers), ''
    when none. A page with a single line is never emptied. Nothing between the first and the last
    non-empty line is looked at.
    """
    filled = [i for i, line in enumerate(lines) if line.strip()]
    if len(filled) < 2:
        return set(), ""
    first = page_number_digits(lines[filled[0]])
    last = page_number_digits(lines[filled[-1]])
    drop = ({filled[0]} if first else set()) | ({filled[-1]} if last else set())
    return drop, last or first or ""


def strip_page_number_lines(text: str) -> tuple[str, str]:
    """`text` without a leading / trailing page-number line, and that number ('' when none).

    The safety net for page numbers that reached the text (page order comes from the scan order;
    the printed number is kept as metadata only). Only the first and last non-empty lines are
    candidates; the body is left exactly as it is.
    """
    lines = (text or "").split("\n")
    drop, digits = page_number_edges(lines)
    if not drop:
        return text, ""
    kept = "\n".join(line for i, line in enumerate(lines) if i not in drop)
    return re.sub(r"\n{3,}", "\n\n", kept).strip("\n"), digits


def printed_number_of(text: str) -> str:
    """Western digits of a page-number region's OCR text ('' when it holds no number)."""
    digits = page_number_digits(" ".join((text or "").split()))
    return digits or ""


def _set_flags(flags: list, updates: dict[str, bool]) -> list:
    """`flags` with every key of `updates` removed, then re-added (at the end) where it is True."""
    out = [f for f in (flags or []) if f not in updates]
    out.extend(flag for flag, on in updates.items() if on)
    return out


def finalize_page(page: Page) -> None:
    """Build the Line rows and the final text of a page from its stored runs; mark it `ocr_done`.

    Unreviewed lines are replaced; a reviewed line (Phase 3) is kept in place of the new line at
    its `order` and its text goes into `final_text`. `final_text` gets Western
    digits (D6) while `Line.ocr_text` and the tokens keep the raw OCR output. Sets or clears the
    `ocr_fallback` and `alignment_poor` flags, then refreshes the book status (`ready_for_review`
    once every non-excluded page is done). A first or last line of the page that is only a page
    number is dropped (no Line row, not in the text) and its digits go to `printed_number`.
    """
    region_texts = _collect_region_texts(page)
    reviewed = {line.order: line for line in page.lines.filter(is_reviewed=True)}
    new_lines: list[Line] = []
    main_parts: list[str] = []
    foot_parts: list[str] = []
    total_tokens = anchored = 0
    has_geometry = False
    order = 0
    built_per_region = [build_lines(rt.text, rt.alt_text, rt.tess_lines) for rt in region_texts]
    # Safety net: a first / last line of the page that is only a page number is dropped from the
    # lines and the text; its number is kept as metadata (`printed_number`).
    flat = [(r, k) for r, built in enumerate(built_per_region) for k in range(len(built))]
    drop, printed = page_number_edges([built_per_region[r][k]["text"] for r, k in flat])
    dropped = {flat[i] for i in drop}
    for r, (rt, built) in enumerate(zip(region_texts, built_per_region, strict=True)):
        has_geometry = has_geometry or bool(rt.tess_lines)
        texts = []
        for k, b in enumerate(built):
            if (r, k) in dropped:
                continue
            kept = reviewed.get(order)
            if kept is not None:  # a reviewed line wins over the new OCR line at its position
                order += 1
                texts.append(kept.text)
                continue
            new_lines.append(
                Line(
                    page=page,
                    order=order,
                    region=rt.target.region,
                    bbox=b["bbox"],
                    text=b["text"],
                    ocr_text=b["text"],
                    tokens=b["tokens"],
                    confidence=b["confidence"],
                    n_low=b["n_low"],
                )
            )
            order += 1
            total_tokens += len(b["tokens"])
            anchored += b["n_anchored"]
            texts.append(b["text"])
        (foot_parts if rt.target.kind in FOOTNOTE_KINDS else main_parts).append("\n".join(texts))
    final_text = join_region_texts(
        [(PAGE_KIND, "\n".join(p for p in main_parts if p))] + [(Region.Kind.FOOTNOTE, p) for p in foot_parts]
    )
    fallback = any(rt.fallback for rt in region_texts)
    poor = has_geometry and total_tokens >= 10 and anchored / total_tokens < MIN_ANCHOR_RATIO

    with transaction.atomic():
        page.lines.filter(is_reviewed=False).delete()
        Line.objects.bulk_create(new_lines)
        page.final_text = to_western_digits(final_text)
        page.text_state = Page.TextState.FINAL
        page.attention_flags = _set_flags(
            page.attention_flags, {FLAG_FALLBACK: fallback, FLAG_ALIGNMENT: poor}
        )
        fields = ["final_text", "text_state", "attention_flags"]
        if printed:
            page.printed_number = printed
            fields.append("printed_number")
        if not page.is_excluded:
            page.status = Page.Status.OCR_DONE
            page.error_from = ""
            page.error_message = ""
            fields += ["status", "error_from", "error_message"]
        page.save(update_fields=fields)
    # Re-read the book: the instance on `page` may predate a long model run.
    book = Book.objects.filter(pk=page.book_id).first()
    if book is not None:
        book.refresh_status()
    log.info(
        "page %s finalised: %d lines, %d tokens (%d low), fallback=%s, sources=%s",
        page.pk,
        len(new_lines),
        total_tokens,
        sum(line.n_low for line in new_lines),
        fallback,
        sorted({rt.source for rt in region_texts if rt.source}),
    )


# ---------------------------------------------------------------- payloads for the API / panel


def _error_headline(message: str | None) -> str:
    """First line of an error message: the Arabic headline (the rest is technical detail)."""
    lines = (message or "").splitlines()
    return lines[0] if lines else ""


def page_text_payload(page: Page) -> dict:
    """State and lines of a page for `/api/pages/<id>/text/` and the text panel."""
    lines = [
        {
            "id": line.pk,
            "order": line.order,
            "region_id": line.region_id,
            "bbox": line.bbox,
            "text": line.text,
            "tokens": line.tokens,
            "confidence": line.confidence,
            "n_low": line.n_low,
            "is_reviewed": line.is_reviewed,
        }
        for line in page.lines.order_by("order", "id")
    ]
    from books.services import ACTIVE_BOOK_STATUSES, ACTIVE_PAGE_STATUSES  # the books app owns "active"

    return {
        "page_id": page.pk,
        "status": page.status,
        "active": page.status in ACTIVE_PAGE_STATUSES and page.book.status in ACTIVE_BOOK_STATUSES,
        "text_state": page.text_state,
        "provisional_text": page.provisional_text,
        "final_text": page.final_text,
        "flags": list(page.attention_flags or []),
        "error": _error_headline(page.error_message),
        "error_detail": "\n".join((page.error_message or "").splitlines()[1:]).strip(),
        "lines": lines,
        "n_low": sum(line["n_low"] for line in lines),
    }


def page_runs_payload(page: Page, limit: int = 60) -> list[dict]:
    """Engine runs of a page, newest first, for `/api/pages/<id>/runs/` and the runs list."""
    runs = page.ocr_runs.select_related("region").order_by("-created_at", "-id")[:limit]
    out = []
    for run in runs:
        region = run.region
        sanity = (run.params or {}).get("sanity") or {}
        out.append(
            {
                "id": run.pk,
                "engine": run.engine_name,
                "engine_label": ENGINE_LABELS.get(run.engine_name, run.engine_name),
                "backend": run.backend,
                "variant": run.input_variant,
                "variant_label": VARIANT_LABELS.get(run.input_variant, run.input_variant),
                "region_id": run.region_id,
                "region_kind": region.kind if region else "",
                "region_label": region.get_kind_display() if region else "الصفحة كاملة",
                "duration_ms": run.duration_ms,
                "seconds": round(run.duration_ms / 1000, 1),
                "looped": run.looped,
                "status": run.status,
                "error": (run.error or "").splitlines()[0][:200] if run.error else "",
                "finish": run.finish,
                "output_tokens": run.output_tokens,
                "check": sanity.get("reason", ""),
                "check_ok": sanity.get("ok"),
                "created_at": run.created_at.isoformat() if run.created_at else None,
            }
        )
    return out
