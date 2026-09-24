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

# Sanity thresholds (D16). A small absolute slack on the upper bound keeps tiny regions (a two-word
# heading read as five words) from failing on the ratio check alone.
MIN_WORD_RATIO = 0.2
MAX_WORD_RATIO = 1.8
WORD_SLACK = 3
MIN_WORD_F1 = 0.45
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
    path = directory / f"{stem}.png"
    path.write_bytes(to_png_bytes(array))
    return path


def _offset_lines(lines: list[dict], dx: int, dy: int, scale: float = 1.0) -> list[dict]:
    """Move Tesseract boxes from crop space into gray-image space (undoing an upscale if any)."""

    def shift(bbox):
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


def _json_safe(value):
    return json.loads(json.dumps(value, default=str))


# ---------------------------------------------------------------- engine calls


def run_engine(
    page: Page,
    engine_name: str,
    target: Target,
    source,
    input_variant: str,
    max_new_tokens: int | None = None,
    scale: float = 1.0,
) -> OcrRun:
    """Call `engine_name` on `source` (image path or PdfPageRef) and store the call as an OcrRun.

    Failures are stored too (`status=error`) so the page keeps a trace of every attempt. Tesseract
    word/line boxes from `result.extra["lines"]` are moved into gray-image coordinates.
    """
    engine = registry.get_engine(engine_name)
    params: dict = {"scope": target.scope, "kind": target.kind}
    if max_new_tokens:
        params["max_new_tokens"] = max_new_tokens
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
        result = engine.recognize(source, max_new_tokens)
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
    if run.status != OcrRun.Status.OK:
        return False, "error"
    return sanity_check(run.parsed_text, reference, run.looped)


def _record_check(run: OcrRun, reference: str) -> tuple[bool, str]:
    ok, reason = _run_passes(run, reference)
    run.params["sanity"] = {"ok": ok, "reason": reason}
    run.save(update_fields=["params"])
    return ok, reason


# ---------------------------------------------------------------- sanity check (D16)


def sanity_check(text: str, reference: str, looped: bool = False) -> tuple[bool, str]:
    """Does a model output look like a reading of the same region as Tesseract's `reference`?

    Fails when the run looped, is empty, has fewer than 20 % or more than 180 % of the reference's
    words (plus a slack of a few words), or when the order-insensitive word F1 with the reference is
    below 0.45 (lenient normalisation). Returns `(ok, reason)` with reason one of
    `ok`, `no_reference`, `loop`, `empty`, `too_short`, `too_long`, `low_overlap`.
    """
    if looped:
        return False, "loop"
    words = normalize(text or "", "lenient").split()
    if not words:
        return False, "empty"
    ref = normalize(reference or "", "lenient").split()
    if not ref:
        return True, "no_reference"
    if len(words) < MIN_WORD_RATIO * len(ref):
        return False, "too_short"
    if len(words) > MAX_WORD_RATIO * len(ref) + WORD_SLACK:
        return False, "too_long"
    if word_f1(words, ref) < MIN_WORD_F1:
        return False, "low_overlap"
    return True, "ok"


# ---------------------------------------------------------------- fast OCR (default queue)


def run_fast_ocr(page: Page) -> None:
    """Tesseract on every region of the B&W image (page-level without regions); provisional text.

    Every region gets an OcrRun with word/line boxes in `params["lines"]`. `provisional_text` joins
    the body-like regions in order, then a blank line, then the footnotes; running header and page
    number are omitted. Born-digital pages with `use_text_layer` are finalised right away from the
    repaired text layer.
    """
    _, _, fast = engine_names()
    pre = _preprocess_of(page)
    bw = _load_field_image(pre.bw_image, "بالأبيض والأسود")
    targets = _targets(page, bw.shape, ocr_only=False)
    texts: list[tuple[str, str]] = []
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="nassakh-ocr-fast-") as tmp:
        tmpdir = Path(tmp)
        for i, target in enumerate(targets):
            path = _save_temp(_crop_image(bw, target.bbox), tmpdir, f"bw-{i}-{target.kind}")
            run = run_engine(page, fast, target, path, "bw")
            if run.status != OcrRun.Status.OK:
                failures.append(run.error)
                continue
            if target.kind not in SKIPPED_KINDS:
                texts.append((target.kind, run.parsed_text))
    if failures and len(failures) == len(targets):
        raise OcrError(
            "تعذّر تشغيل Tesseract على هذه الصفحة؛ تحقّق من تثبيت tesseract وحزمة اللغة العربية. "
            f"({failures[0]})"
        )

    page.provisional_text = join_region_texts(texts)
    page.text_state = Page.TextState.PROVISIONAL
    page.save(update_fields=["provisional_text", "text_state"])

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
        page.provisional_text = run.parsed_text
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
    page.provisional_text = fallback_text
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
                f"تعذّر تحميل محرّك التعرّف «{name}»؛ تحقّق من إعدادات OCR_MODELS_DIR وOCR_BACKEND. "
                f"({type(exc).__name__}: {exc})"
            ) from exc

    bw: np.ndarray | None = None
    with tempfile.TemporaryDirectory(prefix="nassakh-ocr-full-") as tmp:
        tmpdir = Path(tmp)
        for i, target in enumerate(targets):
            tess = _latest_runs(page, target).get(fast)
            if tess is None or tess.status != OcrRun.Status.OK:
                if bw is None:
                    bw = _load_field_image(pre.bw_image, "بالأبيض والأسود")
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
    passes, else Tesseract's text with `fallback=True`.
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
    source = tesseract.engine_name if tesseract is not None else ""
    return reference, None, True, f"primary:{p_reason} secondary:{s_reason}", source


def _collect_region_texts(page: Page) -> list[RegionText]:
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


def _set_flags(flags: list, updates: dict[str, bool]) -> list:
    out = [f for f in (flags or []) if f not in updates]
    out.extend(flag for flag, on in updates.items() if on)
    return out


def finalize_page(page: Page) -> None:
    """Build the Line rows and the final text of a page from its stored runs; mark it `ocr_done`.

    Unreviewed lines are replaced (reviewed ones are kept for Phase 3). `final_text` gets Western
    digits (D6) while `Line.ocr_text` and the tokens keep the raw OCR output. Sets or clears the
    `ocr_fallback` and `alignment_poor` flags, then refreshes the book status (`ready_for_review`
    once every non-excluded page is done).
    """
    region_texts = _collect_region_texts(page)
    new_lines: list[Line] = []
    main_parts: list[str] = []
    foot_parts: list[str] = []
    total_tokens = anchored = 0
    has_geometry = False
    order = 0
    for rt in region_texts:
        built = build_lines(rt.text, rt.alt_text, rt.tess_lines)
        has_geometry = has_geometry or bool(rt.tess_lines)
        texts = []
        for b in built:
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
        "error": page.error_message,
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
