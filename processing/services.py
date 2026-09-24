"""Processing services: preprocessing a page, proposing and applying layout guides, deriving regions.

Views, API functions and Celery tasks stay thin and call these. Everything that touches the
database or storage lives here; the image maths is in `processing.pipeline`.

Coordinates: `Preprocess.crop_box` and `edge_strips_removed` are in the rotated, uncropped frame;
`line_boxes`, `footnote_rule_y`, `footnote_block_y`, `page_number_box` and `Region.bbox` are in
`gray_image` pixel space. Guide values (`header_cut`, `footnote_line`, `page_number_height`) are
ratios of the gray-image height.

Regions are derived per page (`page_layout`): footnotes and the page number come from what was
detected on that page; the book's guide lines are a manual fallback (D4) used only when the owner
set them by hand and nothing was detected, and a page override always wins.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

import numpy as np

from books.models import Book, Page
from core.images import fit_width, load_gray
from core.serializers import flag_items, region_items  # noqa: F401 - re-exported for the API
from core.storage import save_array
from processing import pipeline
from processing.models import LayoutGuides, Preprocess, Region

logger = logging.getLogger(__name__)

STAGE_PREPROCESS = "preprocess"
STAGE_LAYOUT = "layout"

DISPLAY_MAX_WIDTH = 1400
THUMB_MAX_WIDTH = 240
# Above this many pixels the manual re-run goes through the worker instead of the request.
SYNC_MAX_PIXELS = 12_000_000

MANUAL_KEYS: tuple[str, ...] = ("angle", "crop_box", "sauvola_window", "sauvola_k", "nlm_h")
GUIDE_KEYS: tuple[str, ...] = ("header_cut", "footnote_line", "page_number_zone", "page_number_height")
DEFAULT_GUIDES: dict[str, Any] = {
    "header_cut": None,
    "footnote_line": None,
    "page_number_zone": LayoutGuides.PageNumberZone.BOTTOM,
    "page_number_height": 0.06,
}
# A footnote line is proposed only when at least this fraction of the pages shows a rule.
MIN_RULE_FRACTION = 0.4
# Pixels added around a detected page number to form its region.
PAGE_NUMBER_PAD = 6

Spec = tuple[str, list[int]]


class ProcessingError(Exception):
    """A processing failure with an Arabic, actionable message (stored in `Page.error_message`)."""


# ---------------------------------------------------------------- preprocessing


def _load_original(page: Page) -> np.ndarray:
    """Read `page.original_image` as a grayscale array."""
    if not page.original_image or not page.original_image.name:
        raise ProcessingError("لا توجد صورة أصلية لهذه الصفحة. أعد استخراج صفحات الكتاب.")
    try:
        with page.original_image.open("rb") as fh:
            return load_gray(fh)
    except FileNotFoundError as exc:
        raise ProcessingError("ملف الصورة الأصلية مفقود من التخزين. أعد استخراج صفحات الكتاب.") from exc


def clean_manual_params(data: Mapping | None) -> dict:
    """Validate the manual overrides posted by the preprocess panel; keep only the given keys.

    Raises `ValidationError` with Arabic messages. Unknown keys are ignored; `None`/"" means
    "not overridden" for that key.
    """
    if not data:
        return {}
    errors: list[str] = []
    out: dict[str, Any] = {}

    def number(key: str, cast, low, high, label: str):
        value = data.get(key)
        if value is None or value == "":
            return
        try:
            v = cast(value)
        except (TypeError, ValueError):
            errors.append(f"قيمة {label} غير صالحة.")
            return
        if not (low <= v <= high):
            errors.append(f"يجب أن تكون قيمة {label} بين {low} و {high}.")
            return
        out[key] = v

    number("angle", float, -10.0, 10.0, "زاوية التدوير")
    number("sauvola_window", int, 5, 201, "نافذة Sauvola")
    number("sauvola_k", float, 0.01, 1.0, "معامل Sauvola")
    number("nlm_h", int, 0, 30, "قوة إزالة التشويش")

    box = data.get("crop_box")
    if box not in (None, "", []):
        try:
            values = [int(round(float(v))) for v in box]
            if len(values) != 4:
                raise ValueError
            if values[2] - values[0] < 8 or values[3] - values[1] < 8 or min(values) < 0:
                raise ValueError
            out["crop_box"] = values
        except (TypeError, ValueError):
            errors.append("صندوق القص غير صالح: أربعة أعداد صحيحة x0, y0, x1, y1 بحيث x0 < x1 و y0 < y1.")
    if errors:
        raise ValidationError(errors)
    return out


def stored_manual_params(page: Page) -> dict | None:
    """The manual overrides recorded on the page's `Preprocess`, or None when it is automatic.

    Tasks pass this back to `preprocess_page` so re-running the chain keeps the user's adjustments.
    """
    pre = Preprocess.objects.filter(page=page).first()
    if pre is None or not pre.is_manual:
        return None
    if pre.manual_params:
        # Only the keys the user actually overrode; the rest are re-detected on every run.
        return dict(pre.manual_params)
    return {  # rows written before `manual_params` existed: pin everything as before
        "angle": pre.angle,
        "crop_box": pre.crop_box or None,
        "sauvola_window": pre.sauvola_window,
        "sauvola_k": pre.sauvola_k,
        "nlm_h": pre.nlm_h,
    }


def _replace_stage_flags(page: Page, stage_flags: tuple[str, ...], new_flags: list[str]) -> None:
    """Drop this stage's old flags from `page.attention_flags` and add the new ones (order kept)."""
    kept = [f for f in (page.attention_flags or []) if f not in stage_flags]
    page.attention_flags = kept + [f for f in new_flags if f not in kept]


def preprocess_page(page: Page, manual: dict | None = None) -> Preprocess:
    """Run the preprocessing pipeline on the page's original image and store every output.

    `manual` may hold `angle`, `crop_box`, `sauvola_window`, `sauvola_k`, `nlm_h`; the pipeline
    detects the rest. Writes gray.png, bw.png, display.webp (≤ 1400 px wide) and thumb.webp
    (≤ 240 px), the line boxes, footnote rule / block, page-number box and flags, clears any
    earlier error and sets the page status to `preprocessed` (an excluded page keeps
    `excluded`). Idempotent: files are replaced in place.
    """
    gray = _load_original(page)
    overrides = {k: v for k, v in (manual or {}).items() if k in MANUAL_KEYS and v is not None}
    params = pipeline.PreprocessParams(**overrides)
    result = pipeline.run_pipeline(gray, params)

    pre, _ = Preprocess.objects.get_or_create(page=page)
    for name, value in result.model_fields().items():
        setattr(pre, name, value)
    pre.is_manual = bool(overrides)
    pre.manual_params = {k: list(v) if isinstance(v, tuple) else v for k, v in overrides.items()}

    save_array(pre.gray_image, result.gray, "gray.png")
    save_array(pre.bw_image, result.bw, "bw.png")
    save_array(pre.display_image, fit_width(result.gray, DISPLAY_MAX_WIDTH), "display.webp")
    save_array(pre.thumbnail, fit_width(result.gray, THUMB_MAX_WIDTH), "thumb.webp")
    pre.save()

    _replace_stage_flags(page, pipeline.PREPROCESS_FLAGS, result.flags)
    page.status = Page.Status.EXCLUDED if page.is_excluded else Page.Status.PREPROCESSED
    page.error_from = ""
    page.error_message = ""
    page.save(update_fields=["status", "error_from", "error_message", "attention_flags"])
    logger.info(
        "preprocessed page %s: angle=%s lines=%s rule=%s block=%s page_number=%s flags=%s manual=%s",
        page.pk,
        result.angle,
        result.n_lines,
        result.footnote_rule_y,
        result.footnote_block_y,
        result.page_number_box,
        result.flags,
        pre.is_manual,
    )
    return pre


def preprocess_is_quick(page: Page) -> bool:
    """True when the original is small enough to re-run inside a web request."""
    pixels = (page.width or 0) * (page.height or 0)
    return pixels <= SYNC_MAX_PIXELS


EXCLUDED_PAGE_ERROR = "الصفحة مستثناة؛ أعد ضمّها إلى الكتاب أولًا."


def _refuse_approved(page: Page) -> None:
    """ProcessingError for an approved page: it stays as reviewed until it is reopened.

    New preprocessing or regions would move the page out of `reviewed` and leave its reviewed lines
    on regions that no longer exist (`books.services.APPROVED_PAGE_STATUSES`).
    """
    from books.services import APPROVED_PAGE_ERROR, APPROVED_PAGE_STATUSES  # the books app owns them

    if page.status in APPROVED_PAGE_STATUSES:
        raise ProcessingError(APPROVED_PAGE_ERROR)


def rerun_preprocess(page: Page, manual: dict | None) -> tuple[dict, bool]:
    """Re-run preprocessing from the panel with `manual` overrides (None = automatic values).

    Pages that already have regions get them re-derived from the new gray image, so region boxes
    stay in its pixel space; OCR is not re-enqueued (D21). Ordinary pages run in the request and
    return `(payload, False)` with the new state; originals above `SYNC_MAX_PIXELS` are queued as
    preprocess → layout (layout only when regions exist) and return `({"task_id", "detail"}, True)`.
    Raises ProcessingError (Arabic) for an excluded or approved page, or a failed run.
    """
    if page.is_excluded:
        raise ProcessingError(EXCLUDED_PAGE_ERROR)
    _refuse_approved(page)
    rederive = page.regions.exists()
    if not preprocess_is_quick(page):
        from celery import chain

        from processing import tasks

        steps = [tasks.preprocess_page.s(page.pk, manual or {})]
        if rederive:
            steps.append(tasks.layout_page.s())
        result = chain(*steps).apply_async()
        return {"task_id": result.id, "detail": "الصورة كبيرة؛ أُرسلت المعالجة إلى العامل الخلفي."}, True

    pre = preprocess_page(page, manual=manual)
    if rederive:
        derive_regions(page)
    payload = preprocess_payload(page, pre)
    payload["regions"] = region_items(page.regions.all())
    return payload, False


def _versioned_url(field) -> str:
    """File URL with a cache-busting version so a rewritten image is reloaded by the browser."""
    if not field or not field.name:
        return ""
    try:
        stamp = int(field.storage.get_modified_time(field.name).timestamp())
    except (FileNotFoundError, NotImplementedError, OSError):
        stamp = 0
    return f"{field.url}?v={stamp}"


def preprocess_payload(page: Page, pre: Preprocess | None = None) -> dict:
    """JSON-ready description of a page's preprocessing state for the panel and the API."""
    if pre is None:
        pre = Preprocess.objects.filter(page=page).first()
    payload: dict[str, Any] = {
        "page_id": page.pk,
        "status": page.status,
        "status_label": page.get_status_display(),
        "flags": flag_items(page.attention_flags),
        "has_preprocess": pre is not None,
    }
    if pre is None:
        return payload
    bc = pre.border_crop or [0, 0, page.width or 0, page.height or 0]
    frame = (pre.auto_params or {}).get("frame") or [bc[2] - bc[0], bc[3] - bc[1]]
    payload.update(
        {
            "is_manual": pre.is_manual,
            "params": {
                "angle": pre.angle,
                "crop_box": pre.crop_box,
                "sauvola_window": pre.sauvola_window,
                "sauvola_k": pre.sauvola_k,
                "nlm_h": pre.nlm_h,
            },
            "auto_params": pre.auto_params or {},
            "frame": {"width": int(frame[0]), "height": int(frame[1])},
            "output": {"width": pre.output_width, "height": pre.output_height},
            "images": {
                "gray": _versioned_url(pre.gray_image),
                "bw": _versioned_url(pre.bw_image),
                "display": _versioned_url(pre.display_image),
                "thumb": _versioned_url(pre.thumbnail),
            },
            "stats": {
                "skew_confidence": pre.skew_confidence,
                "n_lines": pre.n_lines,
                "median_line_height": pre.median_line_height,
                "footnote_rule_y": pre.footnote_rule_y,
                "footnote_block_y": pre.footnote_block_y,
                "page_number_box": pre.page_number_box,
                "edge_strips_removed": pre.edge_strips_removed or [],
                "border_crop": pre.border_crop or [],
            },
            "line_boxes": pre.line_boxes or [],
            "updated_at": pre.updated_at.isoformat() if pre.updated_at else None,
        }
    )
    return payload


# ---------------------------------------------------------------- guides


@dataclass(slots=True)
class GuideProposal:
    """What the preprocessed pages of a book suggest for the footnote line."""

    n_pages: int
    n_with_rule: int
    footnote_line: float | None
    confidence: float
    reference_page: Page | None

    @property
    def percent(self) -> int:
        return int(round(100 * self.confidence))


def _rule_stats(book: Book) -> GuideProposal:
    """Median footnote-rule ratio over the book's preprocessed, non-excluded pages."""
    rows = list(
        Preprocess.objects.filter(page__book=book, page__is_excluded=False, output_height__gt=0)
        .select_related("page")
        .order_by("page__number")
    )
    with_rule = [r for r in rows if r.footnote_rule_y is not None]
    confidence = len(with_rule) / len(rows) if rows else 0.0
    ratios = [r.footnote_rule_y / r.output_height for r in with_rule]
    median = float(np.median(ratios)) if ratios else None
    proposed = round(median, 4) if median is not None and confidence >= MIN_RULE_FRACTION else None

    reference: Page | None = None
    if with_rule and median is not None:
        closest = min(zip(ratios, with_rule, strict=True), key=lambda t: abs(t[0] - median))
        reference = closest[1].page
    elif rows:
        reference = rows[0].page
    return GuideProposal(len(rows), len(with_rule), proposed, round(confidence, 3), reference)


def guides_stats(book: Book) -> GuideProposal:
    """Detection statistics for the guides screen (how many pages show a rule, proposed ratio)."""
    return _rule_stats(book)


def propose_guides(book: Book) -> tuple[LayoutGuides, float]:
    """Create or refresh the book's automatic guides from the preprocessed pages (display only).

    `footnote_line` is the median `footnote_rule_y / output_height` when at least 40% of the pages
    show a rule, else None; `header_cut` stays None (running-header detection is manual in Phase
    2). The proposal is shown on the guides screen as a starting point; automatic guides are never
    applied to a page (`page_layout`), so processing never waits for them. Guides the user already
    set by hand are left alone. Returns `(guides, confidence)` where confidence is the fraction of
    pages with a rule.
    """
    stats = _rule_stats(book)
    guides, created = LayoutGuides.objects.get_or_create(book=book)
    if created or guides.source == LayoutGuides.Source.AUTO:
        guides.footnote_line = stats.footnote_line
        guides.header_cut = None
        guides.page_number_zone = LayoutGuides.PageNumberZone.BOTTOM
        guides.reference_page = stats.reference_page
        guides.source = LayoutGuides.Source.AUTO
        guides.save()
    logger.info(
        "guides proposal for book %s: footnote_line=%s confidence=%s (%s/%s pages with a rule)",
        book.pk,
        stats.footnote_line,
        stats.confidence,
        stats.n_with_rule,
        stats.n_pages,
    )
    return guides, stats.confidence


def book_guides_dict(book: Book) -> dict:
    """The book-level guide values as a plain dict (defaults when the book has no guides yet)."""
    values = dict(DEFAULT_GUIDES)
    guides = LayoutGuides.objects.filter(book=book).first()
    if guides is not None:
        values.update(
            {
                "header_cut": guides.header_cut,
                "footnote_line": guides.footnote_line,
                "page_number_zone": guides.page_number_zone,
                "page_number_height": guides.page_number_height,
            }
        )
    return values


def effective_guides(page: Page) -> dict:
    """Book guides overlaid with the page's `guides_override` (a present key wins, even when null)."""
    values = book_guides_dict(page.book)
    override = page.guides_override or {}
    for key in GUIDE_KEYS:
        if key in override:
            values[key] = override[key]
    return values


def clean_guides(data: Mapping | None, partial: bool = False) -> dict:
    """Validate guide values from a form or JSON body.

    Full mode (the guides screen) returns every key with defaults for missing ones. Partial mode
    (a page override) returns only the keys present in `data`; an explicit null is kept so a page
    can switch a book-level line off. Raises `ValidationError` with Arabic messages.
    """
    data = data or {}
    errors: list[str] = []
    out: dict[str, Any] = {}

    def ratio(key: str, label: str, low: float, high: float):
        present = key in data
        value = data.get(key)
        if value in (None, ""):
            if present or not partial:
                out[key] = None
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            errors.append(f"قيمة {label} غير صالحة.")
            return
        if not (low <= v <= high):
            errors.append(f"يجب أن تكون قيمة {label} نسبة بين {low} و {high} من ارتفاع الصفحة.")
            return
        out[key] = round(v, 4)

    ratio("header_cut", "حدّ الترويسة", 0.0, 0.5)
    ratio("footnote_line", "خط الحاشية", 0.2, 1.0)

    if "page_number_zone" in data or not partial:
        zone = data.get("page_number_zone") or DEFAULT_GUIDES["page_number_zone"]
        if zone not in LayoutGuides.PageNumberZone.values:
            errors.append("موضع رقم الصفحة غير صالح.")
        else:
            out["page_number_zone"] = zone

    if "page_number_height" in data or not partial:
        raw = data.get("page_number_height")
        if raw in (None, ""):
            out["page_number_height"] = DEFAULT_GUIDES["page_number_height"]
        else:
            try:
                v = float(raw)
                if not (0.01 <= v <= 0.25):
                    raise ValueError
                out["page_number_height"] = round(v, 4)
            except (TypeError, ValueError):
                errors.append("يجب أن يكون ارتفاع منطقة رقم الصفحة نسبة بين 0.01 و 0.25.")

    header, footnote = out.get("header_cut"), out.get("footnote_line")
    if header is not None and footnote is not None and header >= footnote:
        errors.append("يجب أن يكون حدّ الترويسة أعلى من خط الحاشية.")

    if "reference_page" in data and data.get("reference_page") not in (None, ""):
        try:
            out["reference_page"] = int(data["reference_page"])
        except (TypeError, ValueError):
            errors.append("الصفحة المرجعية غير صالحة.")

    if errors:
        raise ValidationError(errors)
    return out


# ---------------------------------------------------------------- regions


def guide_regions(
    guides: Mapping,
    width: int,
    height: int,
    footnote_y: int | None = None,
    page_number_box: list[int] | None = None,
) -> list[Spec]:
    """Region specs `(kind, [x0, y0, x1, y1])` for a page of `width × height`.

    `guides` holds ratios (`header_cut`, `footnote_line`, `page_number_zone`, `page_number_height`).
    `footnote_y` (pixels), when given, replaces the `footnote_line` ratio; `page_number_box`
    (pixels), when given, replaces the page-number zone: the region is that box expanded by
    `PAGE_NUMBER_PAD` and the body / footnote stop above (bottom number) or start below (top
    number) it. Reading order top to bottom: page number (top), running header above
    `header_cut`, body, footnote, page number (bottom). Empty slices are dropped; a footnote top
    that is not strictly between the header and the page number is ignored.
    """
    w, h = int(width), int(height)
    if w <= 0 or h <= 0:
        return []
    header = guides.get("header_cut")
    footnote = guides.get("footnote_line")
    zone = guides.get("page_number_zone") or "none"
    header_y = int(round(header * h)) if header is not None else 0
    if footnote_y is not None:
        foot_y: int | None = int(footnote_y)
    else:
        foot_y = int(round(footnote * h)) if footnote is not None else None

    pn_top: list[int] | None = None
    pn_bottom: list[int] | None = None
    top, bottom = 0, h
    if page_number_box:
        pad = PAGE_NUMBER_PAD
        x0, y0, x1, y1 = (int(v) for v in page_number_box[:4])
        box = [max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)]
        if (y0 + y1) / 2 < h / 2:
            pn_top, top = box, box[3]
        else:
            pn_bottom, bottom = box, box[1]
    elif zone in ("top", "bottom"):
        pn_height = guides.get("page_number_height") or DEFAULT_GUIDES["page_number_height"]
        pn = int(round(pn_height * h))
        if pn > 0 and zone == "top":
            pn_top, top = [0, 0, w, pn], pn
        elif pn > 0:
            pn_bottom, bottom = [0, h - pn, w, h], h - pn

    specs: list[Spec] = []
    if pn_top is not None:
        specs.append((Region.Kind.PAGE_NUMBER, pn_top))
    if header_y > top:
        specs.append((Region.Kind.RUNNING_HEADER, [0, top, w, header_y]))
        top = header_y
    has_footnote = foot_y is not None and top < foot_y < bottom
    body_bottom = foot_y if has_footnote else bottom
    if body_bottom > top:
        specs.append((Region.Kind.BODY, [0, top, w, body_bottom]))
    if has_footnote:
        specs.append((Region.Kind.FOOTNOTE, [0, foot_y, w, bottom]))
    if pn_bottom is not None:
        specs.append((Region.Kind.PAGE_NUMBER, pn_bottom))
    return [(str(kind), box) for kind, box in specs if box[3] > box[1] and box[2] > box[0]]


@dataclass(slots=True)
class PageLayout:
    """Where a page's footnotes and page number come from, resolved per page (see `page_layout`)."""

    guides: dict
    footnote_y: int | None
    footnote_source: str
    page_number_box: list[int] | None
    page_number_source: str


def page_layout(page: Page, pre: Preprocess) -> PageLayout:
    """Resolve the footnote top and the page-number region of one page.

    Footnote top, first match wins: the page override's `footnote_line` (an explicit null means
    "no footnotes on this page"), the detected rule, the detected smaller-type block, the book's
    `footnote_line` only when the book guides are manual, else none (the body runs to the bottom).
    An automatically proposed book line is never applied to a page.

    Page number: the page override's zone (`none` switches it off), else the detected box, else
    the book's zone only when the book guides are manual, else none. The running-header cut comes
    from the book guides and the page override as before.
    """
    book_guides = LayoutGuides.objects.filter(book_id=page.book_id).first()
    manual_book = book_guides is not None and book_guides.source == LayoutGuides.Source.MANUAL
    values = effective_guides(page)
    override = page.guides_override or {}
    h = int(pre.output_height or 0)

    footnote_y: int | None = None
    if "footnote_line" in override:
        ratio = override.get("footnote_line")
        footnote_y = int(round(ratio * h)) if ratio is not None else None
        footnote_source = "override" if ratio is not None else "none"
    elif pre.footnote_rule_y is not None:
        footnote_y, footnote_source = int(pre.footnote_rule_y), "rule"
    elif pre.footnote_block_y is not None:
        footnote_y, footnote_source = int(pre.footnote_block_y), "block"
    elif manual_book and book_guides.footnote_line is not None:
        footnote_y, footnote_source = int(round(book_guides.footnote_line * h)), "book"
    else:
        footnote_source = "none"

    detected = (pre.page_number_box or {}).get("bbox") if isinstance(pre.page_number_box, dict) else None
    box: list[int] | None = None
    if "page_number_zone" in override:
        zone = override.get("page_number_zone") or "none"
        page_number_source = "override" if zone != "none" else "none"
    elif detected:
        zone, box, page_number_source = "none", [int(v) for v in detected], "detected"
    elif manual_book and values.get("page_number_zone") in ("top", "bottom"):
        zone, page_number_source = values["page_number_zone"], "book"
    else:
        zone, page_number_source = "none", "none"

    resolved = dict(values)
    resolved["footnote_line"] = None
    resolved["page_number_zone"] = zone
    return PageLayout(resolved, footnote_y, footnote_source, box, page_number_source)


def page_region_specs(page: Page, pre: Preprocess) -> list[Spec]:
    """Region specs of a page from `page_layout` (gray-image coordinates)."""
    layout = page_layout(page, pre)
    return guide_regions(
        layout.guides,
        pre.output_width,
        pre.output_height,
        footnote_y=layout.footnote_y,
        page_number_box=layout.page_number_box,
    )


def _derive_regions(page: Page) -> tuple[list[Region], bool]:
    """Recompute the guide-derived regions of a page. Returns (regions, changed).

    Regions with `source=manual`/`auto` are kept. When the new geometry equals the stored one,
    nothing is rewritten and `changed` is False; the status still moves to `layout_done` when
    the page had not reached it yet.
    """
    pre = Preprocess.objects.filter(page=page).first()
    if pre is None or not pre.output_height:
        raise ProcessingError("لم تُعالَج الصفحة بعد؛ شغّل المعالجة الأولية قبل تخطيط الصفحة.")
    specs = page_region_specs(page, pre)
    existing = list(page.regions.filter(source=Region.Source.GUIDES).order_by("order", "pk"))
    changed = [(r.kind, [int(v) for v in r.bbox]) for r in existing] != specs

    if changed:
        with transaction.atomic():
            page.regions.filter(source=Region.Source.GUIDES).delete()
            regions = Region.objects.bulk_create(
                [
                    Region(page=page, kind=kind, bbox=box, order=i, source=Region.Source.GUIDES)
                    for i, (kind, box) in enumerate(specs)
                ]
            )
    else:
        regions = existing

    # A page that had not reached layout yet (or failed in it) moves on even when the geometry is
    # unchanged; an error from a later stage is only cleared when the regions changed, because
    # only then is that stage run again.
    needs_status = page.status in (Page.Status.UPLOADED, Page.Status.PREPROCESSED) or (
        page.status == Page.Status.ERROR and page.error_from in ("", STAGE_LAYOUT)
    )
    if (changed or needs_status) and not page.is_excluded:  # an excluded page stays `excluded`
        page.status = Page.Status.LAYOUT_DONE
        page.error_from = ""
        page.error_message = ""
        page.save(update_fields=["status", "error_from", "error_message"])
    return list(regions), changed


def derive_regions(page: Page) -> list[Region]:
    """Regions of a page from its own detection and the manual guides (`page_layout`); `layout_done`."""
    regions, _changed = _derive_regions(page)
    return regions


def drop_detected_page_number(page: Page) -> bool:
    """Forget a detected page number that OCR read as words, and re-derive the page's regions.

    A short last line of text (a paragraph ending in one word) can pass the shape test of
    `pipeline.detect_page_number`; as a page-number region its words would never reach the text.
    Only a detected box is dropped: a page override or the manual book zone is the owner's choice.
    The stored box is cleared (`Preprocess.page_number_box`) so the words join the body region.
    Returns True when the regions changed.
    """
    pre = Preprocess.objects.filter(page=page).first()
    if pre is None or not pre.output_height or not pre.page_number_box:
        return False
    if page_layout(page, pre).page_number_source != "detected":
        return False
    logger.info("page %s: detected page number %s reads as text; dropped", page.pk, pre.page_number_box)
    pre.page_number_box = None
    pre.save(update_fields=["page_number_box"])
    _regions, changed = _derive_regions(page)
    return changed


def _run_stage(page: Page, stage: str) -> None:
    """Hand a page to `books.services.run_stage` (imported lazily; the books app owns the chain)."""
    from books import services as book_services

    book_services.run_stage(page, stage)


def apply_guides(book: Book, data: Mapping, user=None) -> LayoutGuides:
    """Save manual guides for the book, re-derive every non-excluded page and re-OCR what changed.

    Pages that have not been preprocessed yet are skipped (their layout task will pick the new
    guides up), and so are approved pages (they keep their regions and reviewed lines until they
    are reopened). Pages whose regions changed are handed to `books.services.run_stage(page, "ocr")`.
    """
    from books.services import APPROVED_PAGE_STATUSES  # the books app owns the page statuses

    clean = clean_guides(data)
    guides, _ = LayoutGuides.objects.get_or_create(book=book)
    guides.header_cut = clean["header_cut"]
    guides.footnote_line = clean["footnote_line"]
    guides.page_number_zone = clean["page_number_zone"]
    guides.page_number_height = clean["page_number_height"]
    guides.source = LayoutGuides.Source.MANUAL
    if clean.get("reference_page"):
        guides.reference_page = book.pages.filter(pk=clean["reference_page"]).first() or guides.reference_page
    guides.save()

    changed_pages: list[Page] = []
    pages = (
        book.pages.filter(is_excluded=False, preprocess__isnull=False)
        .exclude(status__in=APPROVED_PAGE_STATUSES)
        .select_related("book")
        .order_by("number")
    )
    for page in pages:
        _regions, changed = _derive_regions(page)
        if changed:
            changed_pages.append(page)
    for page in changed_pages:
        _run_stage(page, "ocr")
    book.refresh_status()
    logger.info(
        "guides applied to book %s by %s: %s pages re-derived, %s changed",
        book.pk,
        getattr(user, "pk", None),
        len(pages),
        len(changed_pages),
    )
    return guides


def set_page_guides_override(page: Page, data: Mapping | None) -> tuple[list[Region], bool]:
    """Store a per-page guide override (or clear it when `data` is empty/`reset`) and re-derive.

    Returns `(regions, ocr_enqueued)`; OCR is re-run through the books chain when the regions
    changed. Raises ProcessingError for an excluded or approved page.
    """
    if page.is_excluded:
        raise ProcessingError(EXCLUDED_PAGE_ERROR)
    _refuse_approved(page)
    data = dict(data or {})
    reset = data.pop("reset", False)
    clean = {} if reset else clean_guides(data, partial=True)
    clean.pop("reference_page", None)
    page.guides_override = clean or None
    page.save(update_fields=["guides_override"])
    regions, changed = _derive_regions(page)
    if changed:
        _run_stage(page, "ocr")
    return regions, changed


# ---------------------------------------------------------------- guides screen


def ratio_percent(ratio: float | None) -> str:
    """Ratio → percentage string with one decimal and Western digits ('' for None)."""
    return "" if ratio is None else f"{ratio * 100:.1f}"


def _parse_page_number(requested: str | None) -> int | None:
    """`?page=` value as a page number, None when it is not a plain decimal number."""
    try:
        return int(requested) if requested and requested.strip().isdecimal() else None
    except ValueError:
        return None


def guides_reference_page(book: Book, guides: LayoutGuides | None, requested: str | None) -> Page | None:
    """The page shown on the guides screen: `?page=<number>`, else the stored reference, else the first."""
    candidates = book.pages.filter(is_excluded=False, preprocess__isnull=False).select_related("preprocess")
    number = _parse_page_number(requested)
    if number is not None:
        page = candidates.filter(number=number).first()
        if page is not None:
            return page
    if guides is not None and guides.reference_page_id:
        page = candidates.filter(pk=guides.reference_page_id).first()
        if page is not None:
            return page
    return candidates.order_by("number").first()


def guides_context(book: Book, requested_page: str | None) -> dict:
    """Everything the guides screen renders: guide values, detection stats and the reference page.

    `config` feeds the Alpine component (guide ratios, the proposal, the reference page's detected
    footnote top (rule, else block), line boxes and output size); `pages` lists every preprocessed
    page with whether a footnote rule, a footnote block and a page number were detected on it.
    """
    guides = LayoutGuides.objects.filter(book=book).first()
    stats = guides_stats(book)
    reference = guides_reference_page(book, guides, requested_page)
    values = book_guides_dict(book)

    pre: Preprocess | None = reference.preprocess if reference is not None else None
    detected_rule = None
    if pre is not None and pre.output_height:
        detected_y = pre.footnote_rule_y if pre.footnote_rule_y is not None else pre.footnote_block_y
        if detected_y is not None:
            detected_rule = round(detected_y / pre.output_height, 4)

    pages = [
        {
            "number": n,
            "has_rule": rule is not None,
            "has_block": block is not None,
            "has_number": bool(number),
        }
        for n, rule, block, number in Preprocess.objects.filter(
            page__book=book, page__is_excluded=False, output_height__gt=0
        )
        .order_by("page__number")
        .values_list("page__number", "footnote_rule_y", "footnote_block_y", "page_number_box")
    ]
    config = {
        "header_cut": values["header_cut"],
        "footnote_line": values["footnote_line"],
        "page_number_zone": values["page_number_zone"],
        "page_number_height": values["page_number_height"],
        "proposal": stats.footnote_line,
        "detected_rule": detected_rule,
        "line_boxes": pre.line_boxes if pre is not None else [],
        "output": {"width": pre.output_width, "height": pre.output_height} if pre is not None else None,
    }
    return {
        "book": book,
        "guides": guides,
        "config": config,
        "stats": stats,
        "proposal_percent": ratio_percent(stats.footnote_line),
        "reference": reference,
        "reference_image": pre.display_image.url if pre is not None and pre.display_image else "",
        "pages": pages,
        "n_with_block": sum(1 for p in pages if p["has_block"] and not p["has_rule"]),
        "n_with_number": sum(1 for p in pages if p["has_number"]),
        "zones": LayoutGuides.PageNumberZone.choices,
    }
