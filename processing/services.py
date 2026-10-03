"""Processing services: preprocessing a page, proposing and applying layout guides, deriving regions.

Views, API functions and Celery tasks stay thin and call these. Everything that touches the
database or storage lives here; the image maths is in `processing.pipeline`.

Coordinates: `Preprocess.crop_box` and `edge_strips_removed` are in the rotated, uncropped frame;
`line_boxes`, `footnote_rule_y`, `footnote_block_y`, `page_number_box` and `Region.bbox` are in
`gray_image` pixel space. Guide values (`header_cut`, `footnote_line`, `page_number_height`) are
ratios of the gray-image height.

Regions are derived per page (`page_layout` → `resolve_layout`): footnotes and the page number come
from what was detected on that page; the book's guide lines are a manual fallback (D4) used only
when the owner set them by hand and nothing was detected, and a page override always wins.

«التخطيط» (D64, D67): while a book awaits «بدء المعالجة» no `Region` row exists; the dashboard,
the page detail and the APIs draw the bands `page_bands` computes, which are exactly what
`layout_page` writes at the start. Guide edits then only save values; once «المعالجة» started they
re-derive and re-read the changed pages, leaving approved pages and pages with review work alone.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

import numpy as np

from books import runs
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
# The first manual change to a book whose guides are automatic starts from these (D67 §3.10): the
# stored proposal (`footnote_line`, a bottom zone) must not become live with it.
INERT_GUIDES: dict[str, Any] = {
    "header_cut": None,
    "footnote_line": None,
    "page_number_zone": LayoutGuides.PageNumberZone.NONE,
    "page_number_height": 0.06,
}
# A footnote line is proposed only when at least this fraction of the pages shows a rule.
MIN_RULE_FRACTION = 0.4
# Pixels added around a detected page number to form its region.
PAGE_NUMBER_PAD = 6

Spec = tuple[str, list[int]]


class ProcessingError(Exception):
    """A processing failure with an Arabic, actionable message (stored in `Page.error_message`)."""


class GuidesConflict(Exception):
    """A guides write that names a stage the book is no longer in (another tab started «المعالجة»)."""


# Guide edits on a locked page (only once «المعالجة» started) and the stage conflict (§3.11).
APPROVED_GUIDES_ERROR = "الصفحة معتمدة؛ أعد فتحها من شاشة المراجعة أولًا."
REVIEW_WORK_ERROR = "في هذه الصفحة تصحيحات مراجعة، فلا تتغيّر مناطقها."
STARTED_CONFLICT = "بدأت المعالجة في نافذة أخرى؛ حدّث الصفحة."
GUIDES_STAGES: tuple[str, ...] = ("layout", "ocr")


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


def guides_values(guides: LayoutGuides | None) -> dict:
    """The stored guide values of a `LayoutGuides` row as a plain dict (`DEFAULT_GUIDES` without one)."""
    values = dict(DEFAULT_GUIDES)
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


def is_manual(guides: LayoutGuides | None) -> bool:
    """True when the owner set the book guides by hand (only then do they apply as a fallback)."""
    return guides is not None and guides.source == LayoutGuides.Source.MANUAL


def book_guides_dict(book: Book) -> dict:
    """The book-level guide values as a plain dict (defaults when the book has no guides yet)."""
    return guides_values(LayoutGuides.objects.filter(book=book).first())


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


# ---------------------------------------------------------------- the running-head cut, fitted per page (owner 18)

# `(top, offset)` in page widths: the first text row's top on the page the book cut was set on, and the cut's
# distance below it. Widths, not heights: the pages of a book share the scan's scale, while their heights follow
# the crop (a shorter page, a deeper top margin).
HeaderAnchor = tuple[float, float]
HEADER_TOP_TOLERANCE = 1.5  # a page's first row may sit this many anchor offsets from the anchor's own top
HEADER_MAX = 0.5  # = `clean_guides`' bound: a fitted cut never goes lower than half the page


def _text_rows(pre: Preprocess) -> list[tuple[float, float]]:
    """The page's text rows top to bottom: `_line_boxes` merged where they overlap vertically, `(y0, y1)` px."""
    rows: list[list[float]] = []
    for line in sorted(_line_boxes(pre), key=lambda ln: ln["y0"]):
        if rows and line["y0"] <= rows[-1][1]:
            rows[-1][1] = max(rows[-1][1], line["y1"])
        else:
            rows.append([line["y0"], line["y1"]])
    return [(y0, y1) for y0, y1 in rows]


def _row_cut(rows: list[tuple[float, float]], y: float) -> int | None:
    """The index of the row a cut at `y` runs through (more than `LINE_CUT_MARGIN` px inside it), else None."""
    m = LINE_CUT_MARGIN
    return next((i for i, (y0, y1) in enumerate(rows) if y0 + m < y < y1 - m), None)


def _gap_of(rows: list[tuple[float, float]], y: float) -> int | None:
    """The gap between rows a cut at `y` lies in (0: above the first row), None when it runs through a row."""
    if _row_cut(rows, y) is not None:
        return None
    return sum(1 for y0, y1 in rows if (y0 + y1) / 2 < y)


def _out_of_rows(rows: list[tuple[float, float]], y: float, h: float) -> float:
    """A cut moved out of the row it runs through, to the middle of the nearer gap (above or below the row)."""
    i = _row_cut(rows, y)
    if i is None:
        return y
    y0, y1 = rows[i]
    if y - y0 < y1 - y:
        return ((rows[i - 1][1] if i else 0.0) + y0) / 2
    return (y1 + (rows[i + 1][0] if i + 1 < len(rows) else h)) / 2


def header_anchor(ratio: float | None, pre: Preprocess | None) -> HeaderAnchor | None:
    """Where the book's running-head cut `ratio` sits on `pre`'s page, as a `HeaderAnchor`.

    None when that page cannot anchor it: no prepared image or no text, no row above the cut (the band would
    hold no running head there) or a cut through a line.
    """
    if ratio is None or not _has_pre(pre):
        return None
    rows = _text_rows(pre)
    if not rows:
        return None
    w, h = float(pre.output_width), float(pre.output_height)
    cut = float(ratio) * h
    if rows[0][1] > cut + LINE_CUT_MARGIN or _row_cut(rows, cut) is not None:
        return None
    return (rows[0][0] / w, (cut - rows[0][0]) / w)


def fit_header_cut(ratio: float, pre: Preprocess, anchor: HeaderAnchor | None = None) -> float:
    """The book's running-head cut on one page, from the page's own geometry: a ratio of its height (owner 18).

    One book ratio fell at the same x % of pages whose heights and top margins differ, so it cut above the
    running head of one page and through the first body line of another. Here, with an `anchor` (the page the
    cut was set on, `header_anchor`) and a first text row near the anchor's own (within `HEADER_TOP_TOLERANCE`
    offsets), the cut sits the same distance below this page's first row, moved out of a line it would cut,
    kept above half the page and with text left below it; it replaces the plain ratio only when the two fall in
    different gaps between the lines, so a page where the ratio already works never moves (no re-reading). Any
    other page keeps the ratio, moved out of a line it would cut. A page without detected lines keeps the ratio.
    """
    h, w = float(pre.output_height or 0), float(pre.output_width or 0)
    rows = _text_rows(pre) if h > 0 and w > 0 else []
    if not rows:
        return ratio
    plain = float(ratio) * h
    if anchor is not None:
        top_w, offset_w = anchor
        if abs(rows[0][0] / w - top_w) <= HEADER_TOP_TOLERANCE * offset_w:
            fitted = _out_of_rows(rows, rows[0][0] + offset_w * w, h)
            left_below = any((y0 + y1) / 2 > fitted for y0, y1 in rows)
            if fitted <= HEADER_MAX * h and left_below and _gap_of(rows, fitted) != _gap_of(rows, plain):
                return round(fitted) / h  # on a whole pixel: the band `guide_regions` draws, to the pixel
    if _row_cut(rows, plain) is None:
        return ratio
    return round(min(_out_of_rows(rows, plain, h), HEADER_MAX * h)) / h


def header_reference(pages, ratio: float | None, preferred: int | None = None) -> int | None:
    """The page a book running-head cut `ratio` is anchored on (stored as `LayoutGuides.reference_page`).

    `preferred` (the page the value came from) when it can anchor it, else the page with the book's usual
    geometry: among the non-excluded pages the ratio already fits, the one whose first text row sits at the
    median height (in page widths). None when no page can anchor it. Reads the loaded pages (no query).
    """
    if ratio is None:
        return None
    anchors: dict[int, HeaderAnchor] = {}
    for page in pages:
        if page.is_excluded:
            continue
        anchor = header_anchor(ratio, _pre_of(page))
        if anchor is not None:
            anchors[page.pk] = anchor
    if preferred in anchors:
        return preferred
    if not anchors:
        return None
    tops = sorted(anchor[0] for anchor in anchors.values())
    median = tops[len(tops) // 2]
    return min(anchors, key=lambda pk: (abs(anchors[pk][0] - median), pk))


def guides_anchor(guides: LayoutGuides | None, pres: Mapping[int, Preprocess | None] | None = None) -> HeaderAnchor | None:
    """The `HeaderAnchor` of the book guides: their `header_cut` on their `reference_page`.

    `pres` maps page ids to their loaded `Preprocess` (the bulk paths, no query); else the reference page's
    Preprocess comes from `select_related("reference_page__preprocess")` when the caller loaded it, or one query.
    """
    if guides is None or guides.header_cut is None or not guides.reference_page_id:
        return None
    if pres is not None:
        pre = pres.get(guides.reference_page_id)
    elif LayoutGuides.reference_page.is_cached(guides):
        pre = _pre_of(guides.reference_page) if guides.reference_page is not None else None
    else:
        pre = Preprocess.objects.filter(page_id=guides.reference_page_id).first()
    return header_anchor(guides.header_cut, pre)


def _book_guides(book_id: int) -> LayoutGuides | None:
    """The book's guides with their reference page's Preprocess (one query), for `guides_anchor`."""
    return LayoutGuides.objects.select_related("reference_page__preprocess").filter(book_id=book_id).first()


@dataclass(slots=True)
class PageLayout:
    """Where a page's footnotes and page number come from, resolved per page (see `resolve_layout`)."""

    guides: dict
    footnote_y: int | None
    footnote_source: str
    page_number_box: list[int] | None
    page_number_source: str
    header_source: str = "book"


def resolve_layout(
    book_values: Mapping,
    manual_book: bool,
    override: Mapping | None,
    pre: Preprocess,
    anchor: HeaderAnchor | None = None,
) -> PageLayout:
    """Resolve the footnote top and the page-number region of one page (pure: no query).

    `book_values` are the stored book guides (`guides_values`), `manual_book` whether the owner set
    them, `override` the page's `guides_override`. Footnote top, first match wins: the override's
    `footnote_line` (an explicit null means "no footnotes on this page"), the detected rule, the
    detected smaller-type block, the book's `footnote_line` only when the book guides are manual,
    else none (the body runs to the bottom). An automatically proposed book line is never applied.

    Page number: the override's zone (`none` switches it off), else the detected box, else the
    book's zone only when the book guides are manual, else none. The running-header cut comes from
    the override when it has the key (as the owner set it on the page), else from the book guides, fitted
    to the page's own geometry with the guides' `anchor` (`fit_header_cut`, owner 18).
    """
    override = override or {}
    values = dict(book_values)
    for key in GUIDE_KEYS:
        if key in override:
            values[key] = override[key]
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
    elif manual_book and book_values.get("footnote_line") is not None:
        footnote_y, footnote_source = int(round(book_values["footnote_line"] * h)), "book"
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
    header_source = "override" if "header_cut" in override else "book"
    if header_source == "book" and resolved.get("header_cut") is not None and h:
        resolved["header_cut"] = fit_header_cut(resolved["header_cut"], pre, anchor)
    return PageLayout(resolved, footnote_y, footnote_source, box, page_number_source, header_source)


def page_layout(page: Page, pre: Preprocess) -> PageLayout:
    """`resolve_layout` for a stored page: loads the book guides with their anchor (one query) and the page override."""
    guides = _book_guides(page.book_id)
    return resolve_layout(guides_values(guides), is_manual(guides), page.guides_override, pre, guides_anchor(guides))


def layout_specs(layout: PageLayout, pre: Preprocess) -> list[Spec]:
    """Region specs of a resolved layout on the page's prepared image (gray-image pixels)."""
    return guide_regions(
        layout.guides,
        pre.output_width,
        pre.output_height,
        footnote_y=layout.footnote_y,
        page_number_box=layout.page_number_box,
    )


def page_bands(
    pre: Preprocess,
    book_values: Mapping,
    manual_book: bool,
    override: Mapping | None,
    anchor: HeaderAnchor | None = None,
) -> list[Spec]:
    """The bands of a page: `guide_regions(resolve_layout(…))`, the specs `layout_page` writes (pure)."""
    return layout_specs(resolve_layout(book_values, manual_book, override, pre, anchor), pre)


def page_region_specs(page: Page, pre: Preprocess) -> list[Spec]:
    """Region specs of a stored page: `page_bands` with its book guides, their anchor and the page override."""
    guides = _book_guides(page.book_id)
    return page_bands(pre, guides_values(guides), is_manual(guides), page.guides_override, guides_anchor(guides))


# ---------------------------------------------------------------- pages worth a look (D67)

LAYOUT_DOUBT_LABELS: dict[str, str] = {
    "footnote_from_type": "حاشية من حجم الخط",
    "line_cut": "خط يقطع سطرًا",
    "text_hidden": "سطر من المتن خارج المتن",
    "no_lines": "لا أسطر في الصفحة",
}
LINE_CUT_MARGIN = 2  # px: a guide line this close to a line box's edge does not cut it
HIDDEN_MIN_WIDTH = 0.5  # share of the text-block width a hidden line must have
HIDDEN_MIN_SHARE = 0.6  # share of its height (and half its width) inside the band
_CUT_EDGE = {Region.Kind.RUNNING_HEADER: 3, Region.Kind.FOOTNOTE: 1}  # the bbox edge that is a guide line
_HIDING_KINDS = (Region.Kind.RUNNING_HEADER, Region.Kind.PAGE_NUMBER)


def _line_boxes(pre: Preprocess) -> list[dict]:
    """The page's detected text-line boxes (malformed entries skipped).

    A box thinner than a thick rule (`pipeline.NEAR_RULE_THICKNESS` × the median line height) is a
    rule or a stroke that `detect_lines` kept, not a line of text: a footnote rule lies inside its own
    box, which must not read as «خط يقطع سطرًا» (book 25 after D68).
    """
    min_h = pipeline.NEAR_RULE_THICKNESS * float(pre.median_line_height or 0)
    out = []
    for box in pre.line_boxes or []:
        try:
            line = {k: float(box[k]) for k in ("x0", "y0", "x1", "y1")}
        except (KeyError, TypeError, ValueError):
            continue
        if line["y1"] - line["y0"] >= min_h:
            out.append(line)
    return out


def layout_doubts(
    pre: Preprocess, layout: PageLayout, override: Mapping | None, specs: list[Spec]
) -> list[str]:
    """The reasons a page is worth a look (live, nothing stored), in a fixed order (pure).

    - `footnote_from_type`: the footnote start comes from the smaller-type block (an explicit footnote
      choice in the override makes it go away);
    - `line_cut`: the running-head cut or the footnote start lies inside a detected line box, more
      than `LINE_CUT_MARGIN` px from its edges;
    - `text_hidden`: a line box at least half the text-block width lies, by 60 % of its height and half
      its width, inside a running-head or page-number band;
    - `no_lines`: preprocessing found no line on the page.
    """
    codes: list[str] = []
    if layout.footnote_source == "block" and "footnote_line" not in (override or {}):
        codes.append("footnote_from_type")
    lines = _line_boxes(pre)
    cuts = [box[_CUT_EDGE[kind]] for kind, box in specs if kind in _CUT_EDGE]
    m = LINE_CUT_MARGIN
    if any(ln["y0"] + m < y < ln["y1"] - m for y in cuts for ln in lines):
        codes.append("line_cut")
    if lines:
        block_w = max(ln["x1"] for ln in lines) - min(ln["x0"] for ln in lines)
        bands = [box for kind, box in specs if kind in _HIDING_KINDS]
        for ln in lines:
            lw, lh = ln["x1"] - ln["x0"], ln["y1"] - ln["y0"]
            if lw < HIDDEN_MIN_WIDTH * block_w or lh <= 0:
                continue
            if any(
                min(ln["y1"], b[3]) - max(ln["y0"], b[1]) >= HIDDEN_MIN_SHARE * lh
                and min(ln["x1"], b[2]) - max(ln["x0"], b[0]) >= 0.5 * lw
                for b in bands
            ):
                codes.append("text_hidden")
                break
    if not pre.n_lines:
        codes.append("no_lines")
    return codes


def doubt_items(codes: list[str]) -> list[dict]:
    """Doubt codes as `[{"code", "label"}]` with their Arabic labels."""
    return [{"code": code, "label": LAYOUT_DOUBT_LABELS.get(code, code)} for code in codes]


# ---------------------------------------------------------------- the «التخطيط» payloads (§3.11)

BAND_KINDS: tuple[str, ...] = (
    Region.Kind.RUNNING_HEADER,
    Region.Kind.BODY,
    Region.Kind.FOOTNOTE,
    Region.Kind.PAGE_NUMBER,
)
BAND_COMPACT: dict[str, str] = {"running_header": "h", "body": "b", "footnote": "f", "page_number": "p"}
LOCK_APPROVED = "approved"
LOCK_REVIEW = "review"


def _r4(value: float) -> float:
    return round(float(value), 4)


def band_sources(layout: PageLayout) -> dict[str, str]:
    """`{kind: source}` of a resolved layout's bands (body `auto`; the others as resolved)."""
    return {
        Region.Kind.RUNNING_HEADER: layout.header_source,
        Region.Kind.BODY: "auto",
        Region.Kind.FOOTNOTE: layout.footnote_source,
        Region.Kind.PAGE_NUMBER: layout.page_number_source,
    }


def _rows_specs(rows: list[Region]) -> list[Spec]:
    """Stored guide regions as specs, in their order."""
    ordered = sorted(rows, key=lambda r: (r.order, r.pk or 0))
    return [(str(r.kind), [int(round(float(v))) for v in r.bbox]) for r in ordered]


def page_lock(page: Page, awaits_start: bool, review_pages: set[int] | frozenset[int]) -> str:
    """'' | 'approved' | 'review' for a page; always '' while the book awaits «بدء المعالجة».

    `review_pages` holds the ids of the book's pages with a reviewed line (one query for many pages).
    """
    from books.services import APPROVED_PAGE_STATUSES  # the books app owns the page statuses

    if awaits_start:
        return ""
    if page.status in APPROVED_PAGE_STATUSES:
        return LOCK_APPROVED
    if page.reviewed_at is not None or page.pk in review_pages:
        return LOCK_REVIEW
    return ""


def review_work_pages(book_id: int, page_ids: list[int] | None = None) -> set[int]:
    """Ids of the book's pages with at least one reviewed line (one query)."""
    from ocr.models import Line  # other app: lazy import

    rows = Line.objects.filter(page__book_id=book_id, is_reviewed=True)
    if page_ids is not None:
        rows = rows.filter(page_id__in=page_ids)
    return set(rows.values_list("page_id", flat=True).distinct())


def _image_url(pre: Preprocess) -> str | None:
    """Versioned URL of the prepared display image (the gray image as fallback), None without one."""
    return _versioned_url(pre.display_image) or _versioned_url(pre.gray_image) or None


def _page_view(pre: Preprocess, book_values, manual_book, override, rows: list[Region] | None, anchor=None):
    """`(specs, sources, layout, derived)`: the stored guide rows once derived, else the computed bands."""
    layout = resolve_layout(book_values, manual_book, override, pre, anchor)
    guide_rows = [r for r in rows or [] if r.source == Region.Source.GUIDES]
    if guide_rows:
        return _rows_specs(guide_rows), band_sources(layout), layout, True
    return layout_specs(layout, pre), band_sources(layout), layout, False


def _has_pre(pre: Preprocess | None) -> bool:
    return pre is not None and bool(pre.output_height) and bool(pre.output_width)


def page_guides_payload(
    page: Page,
    pre: Preprocess | None,
    book_values: Mapping,
    manual_book: bool,
    rows: list[Region] | None = None,
    locked: str | None = None,
    anchor: HeaderAnchor | None = None,
) -> dict:
    """The `guides` block of a sheet item (§3.11): bands, guide lines, rows, override, doubts, image.

    The bands are the page's stored guide regions once derived (`derived: true`), else the bands
    `layout_page` would write. Every value is a ratio of the prepared image (4 decimals). `rows`
    are the page's `Region` rows when the caller loaded them (None: queried here); `locked` is the
    page's `page_lock` when known (None: computed here, one query); `anchor` the book guides'
    `guides_anchor` (the book cut fitted to the page, owner 18). Excluded pages carry no doubt.
    """
    override = dict(page.guides_override or {})
    if locked is None:
        awaits = Book.objects.filter(pk=page.book_id).values_list("awaits_ocr_start", flat=True).first()
        locked = page_lock(page, bool(awaits), review_work_pages(page.book_id, [page.pk]))
    if not _has_pre(pre):
        return {
            "bands": [],
            "lines": {"header": None, "footnote": None},
            "rows": [],
            "override": override,
            "doubts": [],
            "image_url": None,
            "derived": False,
            "locked": locked,
        }
    if rows is None:
        rows = list(page.regions.all())
    w, h = int(pre.output_width), int(pre.output_height)
    specs, sources, layout, derived = _page_view(pre, book_values, manual_book, override, rows, anchor)
    bands = [
        {
            "kind": kind,
            "bbox": [_r4(box[0] / w), _r4(box[1] / h), _r4(box[2] / w), _r4(box[3] / h)],
            "source": sources.get(kind, "auto"),
        }
        for kind, box in specs
    ]
    header = next((b for b in bands if b["kind"] == Region.Kind.RUNNING_HEADER), None)
    footnote = next((b for b in bands if b["kind"] == Region.Kind.FOOTNOTE), None)
    doubts = [] if page.is_excluded else layout_doubts(pre, layout, override, specs)
    return {
        "bands": bands,
        "lines": {
            "header": {"y": header["bbox"][3], "source": header["source"]} if header else None,
            "footnote": {"y": footnote["bbox"][1], "source": footnote["source"]} if footnote else None,
        },
        "rows": [[_r4(ln["y0"] / h), _r4(ln["y1"] / h)] for ln in _line_boxes(pre)],
        "override": override,
        "doubts": doubt_items(doubts),
        "image_url": _image_url(pre),
        "derived": derived,
        "locked": locked,
    }


def _compact_bands(specs: list[Spec], w: int, h: int) -> list[list]:
    """Bands as `[kind, y0, y1]` (the page number also `x0, x1`), ratios to 4 decimals."""
    out: list[list] = []
    for kind, (x0, y0, x1, y1) in specs:
        entry: list = [BAND_COMPACT.get(kind, kind), _r4(y0 / h), _r4(y1 / h)]
        if kind == Region.Kind.PAGE_NUMBER:
            entry += [_r4(x0 / w), _r4(x1 / w)]
        out.append(entry)
    return out


def compact_entry(
    page: Page,
    pre: Preprocess | None,
    book_values,
    manual_book,
    rows: list[Region] | None,
    locked: str,
    anchor: HeaderAnchor | None = None,
) -> dict:
    """One page of `api:book_guides` (about 60 B): id, number, bands, doubt count, override, lock, status."""
    override = page.guides_override or {}
    bands: list[list] = []
    n_doubts = 0
    if _has_pre(pre):
        specs, _sources, layout, _derived = _page_view(pre, book_values, manual_book, override, rows, anchor)
        bands = _compact_bands(specs, int(pre.output_width), int(pre.output_height))
        if not page.is_excluded:
            n_doubts = len(layout_doubts(pre, layout, override, specs))
    return {
        "id": page.pk,
        "n": page.number,
        "b": bands,
        "d": n_doubts,
        "o": bool(override),
        "l": locked,
        "s": page.status,
        "x": page.is_excluded,
    }


def book_guides_view(guides: LayoutGuides | None) -> dict:
    """The book guides as the side panel shows them: automatic guides read as the inert values."""
    if not is_manual(guides):
        header = guides.header_cut if guides is not None else None
        return {"source": LayoutGuides.Source.AUTO, **INERT_GUIDES, "header_cut": header}
    return {"source": LayoutGuides.Source.MANUAL, **guides_values(guides)}


def _book_brief(guides: LayoutGuides | None, awaits_start: bool) -> dict:
    """The `book` part of the guides payloads."""
    view = book_guides_view(guides)
    return {
        "source": view["source"],
        "header_cut": view["header_cut"],
        "footnote_line": view["footnote_line"],
        "awaits_ocr_start": bool(awaits_start),
    }


_ONE_PAGE_OF = ("صفحة واحدة", "صفحتين", "صفحات", "صفحة")


def _summary_of(rows: list[tuple[int, int | None, int | None, Any]]) -> dict:
    """`guides_summary` from `(output_height, rule_y, block_y, page_number_box)` of the prepared pages."""
    from assembly.render import ar_count  # other app: lazy import

    n = len(rows)
    ratios = [rule / height for height, rule, _block, _pn in rows if rule is not None]
    rule = len(ratios)
    block = sum(1 for _h, r, b, _pn in rows if r is None and b is not None)
    number = sum(1 for *_rest, pn in rows if pn)
    median = _r4(statistics.median(ratios)) if ratios and rule / n >= MIN_RULE_FRACTION else None
    parts = []
    if rule:
        parts.append(f"خط حاشية في {rule}")
    if block:
        parts.append(f"حاشية بخط أصغر في {block}")
    if number:
        parts.append(f"رقم صفحة في {number}")
    if not n:
        text = ""
    elif parts:
        parts[0] += f" من {ar_count(n, _ONE_PAGE_OF)}"
        text = "اكتُشف " + "، و".join(parts) + "."
    else:
        text = "لم يُكتشف خط حاشية ولا رقم صفحة."
    return {"pages": n, "rule": rule, "block": block, "number": number, "median_rule": median, "text": text}


def guides_summary(book: Book) -> dict:
    """The detection line of the side panel: `{pages, rule, block, number, median_rule, text}` (one query).

    Over the book's prepared, non-excluded pages: how many show a footnote rule, a smaller-type
    block (without a rule) and a page number; `median_rule` is the median rule ratio when at least
    `MIN_RULE_FRACTION` of the pages show one (the proposal), else null. Parts at 0 are left out.
    """
    rows = list(
        Preprocess.objects.filter(page__book=book, page__is_excluded=False, output_height__gt=0).values_list(
            "output_height", "footnote_rule_y", "footnote_block_y", "page_number_box"
        )
    )
    return _summary_of(rows)


def _load_book_pages(book: Book, awaits: bool):
    """`(pages, regions_of, review_pages, guides)` for every page of the book in ≤ 4 queries."""
    guides = LayoutGuides.objects.filter(book=book).first()
    pages = list(
        book.pages.select_related("preprocess")
        .defer(
            "text_layer_text",
            "provisional_text",
            "final_text",
            "preprocess__auto_params",
            "preprocess__edge_strips_removed",
        )
        .order_by("number")
    )
    regions_of: dict[int, list[Region]] = {page.pk: [] for page in pages}
    for region in Region.objects.filter(page__book=book).order_by("page_id", "order", "id"):
        regions_of.setdefault(region.page_id, []).append(region)
    review_pages = set() if awaits else review_work_pages(book.pk)
    return pages, regions_of, review_pages, guides


def _pre_of(page: Page) -> Preprocess | None:
    try:
        return page.preprocess
    except Preprocess.DoesNotExist:
        return None


def _pres_of(pages) -> dict[int, Preprocess | None]:
    """`{page id: Preprocess}` of loaded pages, for `guides_anchor` (no query)."""
    return {page.pk: _pre_of(page) for page in pages}


def book_guides_state(book: Book, first: int | None = None, last: int | None = None) -> dict:
    """`GET api:book_guides`: the book guides, the detection line, the chip counts and one compact entry
    per page (`first..last` when given; the counts always cover the whole book). ≤ 4 queries whatever
    the page count."""
    awaits = bool(book.awaits_ocr_start)
    pages, regions_of, review_pages, guides = _load_book_pages(book, awaits)
    values, manual = guides_values(guides), is_manual(guides)
    anchor = guides_anchor(guides, _pres_of(pages))
    entries = [
        compact_entry(
            page,
            _pre_of(page),
            values,
            manual,
            regions_of.get(page.pk),
            page_lock(page, awaits, review_pages),
            anchor,
        )
        for page in pages
    ]
    included = [e for e in entries if not e["x"]]
    counts = {
        "all": len(included),
        "doubt": sum(1 for e in included if e["d"] or e["s"] == Page.Status.ERROR),
        "override": sum(1 for e in included if e["o"]),
        "error": sum(1 for e in included if e["s"] == Page.Status.ERROR),
    }
    prepared = [
        (pre.output_height, pre.footnote_rule_y, pre.footnote_block_y, pre.page_number_box)
        for page in pages
        if not page.is_excluded and (pre := _pre_of(page)) is not None and pre.output_height
    ]
    if first is not None or last is not None:
        lo, hi = first or 1, last or 10**9
        entries = [e for e in entries if lo <= e["n"] <= hi]
    return {
        "book": _book_brief(guides, awaits),
        "stats": _summary_of(prepared),
        "counts": counts,
        "pages": entries,
    }


def _derive_regions(page: Page) -> tuple[list[Region], bool]:
    """Recompute the guide-derived regions of a page. Returns (regions, changed).

    Regions with `source=manual`/`auto` are kept. When the new geometry equals the stored one,
    nothing is rewritten and `changed` is False; the status still moves to `layout_done` when
    the page had not reached it yet.
    """
    pre = Preprocess.objects.filter(page=page).first()
    if pre is None or not pre.output_height:
        raise ProcessingError("لم تُجهَّز الصفحة بعد؛ شغّل «تجهيز الصفحات» قبل تحديد مناطقها.")
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


# ---------------------------------------------------------------- guide edits and the stage rule (§3.10)


def book_awaits_start(book_id: int) -> bool:
    """The book's `awaits_ocr_start`, read fresh (a click on «بدء المعالجة» may have landed meanwhile)."""
    return bool(Book.objects.filter(pk=book_id).values_list("awaits_ocr_start", flat=True).first())


def refresh_waiting_book(book_id: int) -> None:
    """Re-derive the status of a book in «التخطيط» after one of its pages was prepared (or failed).

    Runs under the book's row lock so two tasks finishing together cannot write a stale status: the
    second one counts after the first has committed. A book that has not started (`uploaded`) or
    whose «المعالجة» started meanwhile is left alone.
    """
    with transaction.atomic():
        book = Book.objects.select_for_update().filter(pk=book_id).first()
        if book is None or not book.awaits_ocr_start or book.status == Book.Status.UPLOADED:
            return
        book.refresh_status()


def check_stage(book_id: int, stage: str | None) -> bool:
    """The book's fresh `awaits_ocr_start`; GuidesConflict when the client's `stage` is no longer the book's.

    `stage` is `layout` («التخطيط») or `ocr` («المعالجة»); any other value (or none) is not checked.
    """
    awaits = book_awaits_start(book_id)
    if stage in GUIDES_STAGES and (stage == "layout") != awaits:
        raise GuidesConflict(STARTED_CONFLICT)
    return awaits


def _stored_guides(guides: LayoutGuides | None) -> dict | None:
    """A `LayoutGuides` row as an undo value (`source`, the four values and the `reference_page` the running-head
    cut is anchored on, owner 18), None without a row."""
    if guides is None:
        return None
    return {"source": guides.source, **guides_values(guides), "reference_page": guides.reference_page_id}


@dataclass(slots=True)
class _GuidesPlan:
    """A book-guides change: the stored values after it (None: no row), their source, the page
    overrides it rewrites (`{page id: override | None}`), the keys it sets and the page its running-head cut
    is anchored on (`header_reference`; None: the stored one stays)."""

    values: dict | None
    source: str
    overrides: dict[int, dict | None]
    set_keys: frozenset[str]
    reference_page: int | None = None

    @property
    def manual(self) -> bool:
        return self.values is not None and self.source == LayoutGuides.Source.MANUAL


def _clean_keys(keys) -> list[str]:
    """The override keys a request names (unknown keys refused, Arabic ValidationError)."""
    keys = list(keys or [])
    unknown = [k for k in keys if k not in GUIDE_KEYS]
    if unknown:
        raise ValidationError(["مفتاح تخطيط غير معروف: " + "، ".join(str(k) for k in unknown)])
    return keys


def _override_changes(pages: list[Page], drop_all: list[str], from_page: int | None, set_keys) -> dict:
    """`{page id: new override | None}` for the pages whose override loses `drop_all` (every page) or
    the set keys (`from_page`, whose value now is the book value)."""
    out: dict[int, dict | None] = {}
    for page in pages:
        drop = set(drop_all)
        if from_page is not None and page.pk == from_page:
            drop |= set(set_keys)
        current = page.guides_override or {}
        if drop & set(current):
            kept = {k: v for k, v in current.items() if k not in drop}
            out[page.pk] = kept or None
    return out


def _plan_change(book: Book, guides, pages, changes, reset_overrides, from_page) -> _GuidesPlan:
    """The plan of a partial change: the inert start for automatic guides, then only the given keys."""
    clean = clean_guides(changes if isinstance(changes, Mapping) else {}, partial=True)
    clean.pop("reference_page", None)
    values = guides_values(guides) if is_manual(guides) else dict(INERT_GUIDES)
    values.update(clean)
    if values["header_cut"] is not None and values["footnote_line"] is not None:
        if values["header_cut"] >= values["footnote_line"]:
            raise ValidationError(["يجب أن يكون حدّ الترويسة أعلى من خط الحاشية."])
    source_page = None
    if from_page not in (None, ""):
        try:
            source_page = int(from_page)
        except (TypeError, ValueError):
            raise ValidationError(["الصفحة غير صالحة."]) from None
    keys = frozenset(clean)
    overrides = _override_changes(pages, _clean_keys(reset_overrides), source_page, keys)
    # a new running-head cut is anchored on the page it came from, else on the book's usual page (owner 18)
    reference = header_reference(pages, clean["header_cut"], source_page) if clean.get("header_cut") is not None else None
    return _GuidesPlan(values, LayoutGuides.Source.MANUAL, overrides, keys, reference)


def _plan_undo(pages, undo) -> _GuidesPlan:
    """The plan that restores an `undo` payload exactly (the book row, then each page's override)."""
    if not isinstance(undo, Mapping):
        raise ValidationError(["بيانات التراجع غير صالحة."])
    stored = undo.get("book")
    values: dict | None = None
    source = LayoutGuides.Source.AUTO
    reference: int | None = None
    if stored is not None:
        if not isinstance(stored, Mapping) or stored.get("source") not in LayoutGuides.Source.values:
            raise ValidationError(["بيانات التراجع غير صالحة."])
        source = stored["source"]
        values = clean_guides(stored)
        reference = values.pop("reference_page", None)
    known = {page.pk for page in pages}
    overrides: dict[int, dict | None] = {}
    raw = undo.get("overrides") or {}
    if not isinstance(raw, Mapping):
        raise ValidationError(["بيانات التراجع غير صالحة."])
    for key, value in raw.items():
        try:
            page_id = int(key)
        except (TypeError, ValueError):
            raise ValidationError(["بيانات التراجع غير صالحة."]) from None
        if page_id not in known:
            continue
        clean = clean_guides(value, partial=True) if isinstance(value, Mapping) else {}
        clean.pop("reference_page", None)
        overrides[page_id] = clean or None
    return _GuidesPlan(values, source, overrides, frozenset(), reference if reference in known else None)


def _plan_reset(book: Book) -> _GuidesPlan:
    """«إزالة الضبط العام»: back to the automatic guides (a fresh proposal, inert as ever)."""
    stats = _rule_stats(book)
    values = {
        "header_cut": None,
        "footnote_line": stats.footnote_line,
        "page_number_zone": LayoutGuides.PageNumberZone.BOTTOM,
        "page_number_height": DEFAULT_GUIDES["page_number_height"],
    }
    return _GuidesPlan(values, LayoutGuides.Source.AUTO, {}, frozenset())


def _new_override(page: Page, plan: _GuidesPlan) -> dict:
    if page.pk in plan.overrides:
        return dict(plan.overrides[page.pk] or {})
    return dict(page.guides_override or {})


def _plan_anchor(plan: _GuidesPlan, guides: LayoutGuides | None, pres: Mapping) -> HeaderAnchor | None:
    """The `HeaderAnchor` of a plan's running-head cut: on its new reference page, else on the stored one."""
    if plan.values is None or plan.values.get("header_cut") is None:
        return None
    reference = plan.reference_page if plan.reference_page is not None else getattr(guides, "reference_page_id", None)
    return header_anchor(plan.values["header_cut"], pres.get(reference)) if reference else None


def _evaluate(plan: _GuidesPlan, awaits: bool, pages, regions_of, review_pages, guides) -> list[dict]:
    """Per prepared, non-excluded page: would its bands change, is it locked, would a line cut a line
    or a band hide text afterwards, does its own override keep a set key, and where its running-head cut
    lands (`header`, a ratio of its height; None without one)."""
    old_values, old_manual = guides_values(guides), is_manual(guides)
    new_values = plan.values if plan.values is not None else guides_values(None)
    pres = _pres_of(pages)
    old_anchor, new_anchor = guides_anchor(guides, pres), _plan_anchor(plan, guides, pres)
    out = []
    for page in pages:
        pre = _pre_of(page)
        if page.is_excluded or not _has_pre(pre):
            continue
        override = _new_override(page, plan)
        layout = resolve_layout(new_values, plan.manual, override, pre, new_anchor)
        after = layout_specs(layout, pre)
        if awaits:
            before = page_bands(pre, old_values, old_manual, page.guides_override, old_anchor)
        else:
            before = _rows_specs([r for r in regions_of.get(page.pk, []) if r.source == Region.Source.GUIDES])
        doubts = set(layout_doubts(pre, layout, override, after))
        out.append(
            {
                "page": page,
                "changed": after != before,
                "locked": page_lock(page, awaits, review_pages),
                "cut": bool(doubts & {"line_cut", "text_hidden"}),
                "kept": bool(plan.set_keys & set(override)),
                "header": layout.guides.get("header_cut") if layout.header_source == "book" else None,
            }
        )
    return out


def _minutes(book: Book, pages: int) -> int:
    """Model time of re-reading `pages` pages (the books app's per-page Qari estimate)."""
    from books.services import qari_seconds  # the books app owns the estimate

    return math.ceil(pages * qari_seconds(book) / 60) if pages else 0


def preview_book_guides(
    book: Book,
    changes: Mapping,
    reset_overrides=(),
    *,
    from_page=None,
    stage: str | None = None,
    reset: bool = False,
) -> dict:
    """What a book-guides change would do, without writing anything (§3.12 «معاينة»).

    `{changed, pages, cut, kept_overrides, locked, reocr, minutes, header_at}`: the pages whose bands change
    (in «المعالجة» against their stored regions, locked pages apart), those that would then show
    `line_cut` or `text_hidden`, those whose own override keeps a set key, the approved / review-work
    pages that stay (only in «المعالجة»), how many pages are re-read and the model time (null in
    «التخطيط»), and `{page id: ratio}` of the pages where the book's running-head cut, fitted to the page
    (`fit_header_cut`, owner 18), lands elsewhere than the plain ratio (the side panel draws its draft there).
    With `reset` it previews «إزالة الضبط العام» (`reset_book_guides`'s plan; `changes` are ignored). A
    constant number of queries whatever the page count.
    """
    awaits = check_stage(book.pk, stage)
    pages, regions_of, review_pages, guides = _load_book_pages(book, awaits)
    if reset:
        plan = _plan_reset(book)
    else:
        plan = _plan_change(book, guides, pages, changes, reset_overrides, from_page)
    rows = _evaluate(plan, awaits, pages, regions_of, review_pages, guides)
    changed = [r for r in rows if r["changed"] and not r["locked"]]
    reocr = 0 if awaits else len(changed)
    ratio = (plan.values or {}).get("header_cut")
    return {
        "changed": len(changed),
        "pages": [r["page"].number for r in changed],
        "cut": [r["page"].number for r in changed if r["cut"]],
        "kept_overrides": [r["page"].number for r in rows if r["kept"]],
        "locked": [r["page"].number for r in rows if r["changed"] and r["locked"]],
        "reocr": reocr,
        "minutes": None if awaits else _minutes(book, reocr),
        "header_at": {
            str(r["page"].pk): _r4(r["header"])
            for r in rows
            if ratio is not None and r["header"] is not None and _r4(r["header"]) != _r4(ratio)
        },
    }


def _commit(book: Book, plan: _GuidesPlan, awaits: bool, loaded, user=None) -> dict:
    """Write a guides plan and answer as `api:book_guides` does (§3.11).

    In «التخطيط» only values are saved: `changed` lists the pages whose computed bands move, and
    `undo` restores the book row and every rewritten override exactly. In «المعالجة» the plan skips
    locked pages; every other prepared page is re-derived and those whose regions changed are
    re-read (`books.services.run_stage(page, "ocr")`); there is no undo. In «المعالجة» nothing is written
    while a run holds a page of the book (`_refuse_running_book`).
    """
    if not awaits:
        _refuse_running_book(book)
    pages, regions_of, review_pages, guides = loaded
    by_id = {page.pk: page for page in pages}
    if not awaits:  # locked pages keep their override (their regions stay as reviewed)
        plan.overrides = {
            pk: value
            for pk, value in plan.overrides.items()
            if pk in by_id and not page_lock(by_id[pk], False, review_pages)
        }
    evaluated = _evaluate(plan, awaits, pages, regions_of, review_pages, guides) if awaits else []
    undo = {
        "book": _stored_guides(guides),
        "overrides": {str(pk): by_id[pk].guides_override for pk in plan.overrides if pk in by_id},
    }

    with transaction.atomic():
        if plan.values is None:
            LayoutGuides.objects.filter(book=book).delete()
            guides = None
        else:
            guides, _ = LayoutGuides.objects.get_or_create(book=book)
            for key in GUIDE_KEYS:
                setattr(guides, key, plan.values[key])
            guides.source = plan.source
            if plan.reference_page and plan.reference_page in by_id:  # a page of this book (no query)
                guides.reference_page = by_id[plan.reference_page]
            guides.save()
        for pk, value in plan.overrides.items():
            if pk in by_id:
                Page.objects.filter(pk=pk).update(guides_override=value)
                by_id[pk].guides_override = value
    values, manual = guides_values(guides), is_manual(guides)

    if awaits:
        changed_ids = [r["page"].pk for r in evaluated if r["changed"]]
        reocr = 0
    else:
        changed_pages: list[Page] = []
        for page in pages:
            if page.is_excluded or not _has_pre(_pre_of(page)) or page_lock(page, False, review_pages):
                continue
            _regions, changed = _derive_regions(page)
            if changed:
                changed_pages.append(page)
        for page in changed_pages:
            _run_stage(page, "ocr")
        book.refresh_status()
        changed_ids = [page.pk for page in changed_pages]
        reocr = len(changed_pages)
    touched = set(changed_ids) | set(plan.overrides)
    if touched and not awaits:
        fresh: dict[int, list[Region]] = {pk: [] for pk in touched}
        for region in Region.objects.filter(page_id__in=touched).order_by("page_id", "order", "id"):
            fresh[region.page_id].append(region)
        regions_of = {**regions_of, **fresh}
        for page in Page.objects.filter(pk__in=touched).only("id", "status"):
            by_id[page.pk].status = page.status
    anchor = guides_anchor(guides, _pres_of(pages))
    entries = [
        compact_entry(
            page,
            _pre_of(page),
            values,
            manual,
            regions_of.get(page.pk),
            page_lock(page, awaits, review_pages),
            anchor,
        )
        for page in pages
        if page.pk in touched
    ]
    logger.info(
        "book guides of book %s saved by %s (%s): %s pages changed, %s re-read",
        book.pk,
        getattr(user, "pk", None),
        "layout" if awaits else "ocr",
        len(changed_ids),
        reocr,
    )
    return {
        "changed": sorted(by_id[pk].number for pk in changed_ids),
        "reocr": reocr,
        "undo": undo if awaits else None,
        "book": _book_brief(guides, awaits),
        "pages": entries,
    }


def apply_book_guides(
    book: Book, changes: Mapping, reset_overrides=(), user=None, *, from_page=None, stage: str | None = None
) -> dict:
    """Save a partial change of the book guides (the side panel's «تطبيق على كل الصفحات»).

    The first manual change of automatic guides starts from `INERT_GUIDES`, then only the given keys
    change. `reset_overrides` removes those keys from every page's override; `from_page` drops the
    set keys from that page's override (its value is now the book value). In «التخطيط» nothing is
    derived or queued and the answer carries its `undo`; in «المعالجة» today's loop re-derives the
    unlocked pages and re-reads those whose regions changed. Raises ValidationError (Arabic) and
    GuidesConflict (a stale `stage`).
    """
    awaits = check_stage(book.pk, stage)
    loaded = _load_book_pages(book, awaits)
    plan = _plan_change(book, loaded[3], loaded[0], changes, reset_overrides, from_page)
    return _commit(book, plan, awaits, loaded, user)


def restore_book_guides(book: Book, undo: Mapping, user=None, *, stage: str | None = None) -> dict:
    """Post back an `undo` payload of `apply_book_guides` (or of this function): restores the book row
    and the listed overrides exactly; the answer carries the inverse again."""
    awaits = check_stage(book.pk, stage)
    loaded = _load_book_pages(book, awaits)
    return _commit(book, _plan_undo(loaded[0], undo), awaits, loaded, user)


def reset_book_guides(book: Book, user=None, *, stage: str | None = None) -> dict:
    """«إزالة الضبط العام»: the book goes back to its automatic guides (which never apply to a page)."""
    awaits = check_stage(book.pk, stage)
    loaded = _load_book_pages(book, awaits)
    return _commit(book, _plan_reset(book), awaits, loaded, user)


def apply_guides(book: Book, data: Mapping, user=None) -> LayoutGuides:
    """Save the full set of manual guides (the retired guides form's four keys).

    In «التخطيط» the values are only saved. Once «المعالجة» started, every prepared, non-excluded
    page is re-derived and those whose regions changed are re-read; approved pages and pages with
    review work keep their regions (`ocr.services._has_review_work`: their footnote lines would lose
    their region). Returns the saved `LayoutGuides`.
    """
    clean = clean_guides(data)
    awaits = book_awaits_start(book.pk)
    loaded = _load_book_pages(book, awaits)
    values = {key: clean[key] for key in GUIDE_KEYS}
    reference = clean.get("reference_page")
    if values["header_cut"] is not None:  # the running-head cut's anchor (owner 18)
        reference = header_reference(loaded[0], values["header_cut"], reference) or reference
    plan = _GuidesPlan(values, LayoutGuides.Source.MANUAL, {}, frozenset(values), reference)
    _commit(book, plan, awaits, loaded, user)
    return LayoutGuides.objects.get(book=book)


def _refuse_locked(page: Page, awaits: bool) -> None:
    """ProcessingError (Arabic) for a page whose regions must not change: excluded, or — once
    «المعالجة» started — approved, carrying review work, or held by a run (`books.runs`, item 29: a change
    there would re-read the page while it is still being read)."""
    if page.is_excluded:
        raise ProcessingError(EXCLUDED_PAGE_ERROR)
    lock = page_lock(page, awaits, review_work_pages(page.book_id, [page.pk]) if not awaits else set())
    if lock == LOCK_APPROVED:
        raise ProcessingError(APPROVED_GUIDES_ERROR)
    if lock == LOCK_REVIEW:
        raise ProcessingError(REVIEW_WORK_ERROR)
    if not awaits and runs.is_active(page.pk):
        raise ProcessingError(runs.PAGE_RUN_ACTIVE_ERROR)


def _refuse_running_book(book: Book) -> None:
    """ProcessingError (Arabic) while a run holds any page of a started book: the book guides re-read the
    pages they change, which must wait until no page is being read (`books.runs`, item 29)."""
    busy = runs.active_count(book.pages.all())
    if busy:
        raise ProcessingError(runs.book_run_active_message(busy))


def _save_override(page: Page, override: dict | None, awaits: bool) -> tuple[list[Region], bool]:
    """Store a page override; in «المعالجة» re-derive the page and re-read it when its regions changed."""
    page.guides_override = override or None
    page.save(update_fields=["guides_override"])
    if awaits:
        return [], False
    regions, changed = _derive_regions(page)
    if changed:
        _run_stage(page, "ocr")
    return regions, changed


def set_page_guides(
    page: Page, set_: Mapping, unset=(), reset: bool = False, *, stage: str | None = None
) -> dict:
    """Merge a change into the page's override (a drag, «+ ترويسة», «إزالة من هذه الصفحة», «التلقائي»).

    `set_` keys are validated by `clean_guides(partial=True)` (a null keeps its meaning: "none on this
    page"), `unset` keys are removed, `reset` clears the override before `set_` applies (a started
    book's pending «التلقائي» followed by a drag saves both at once). In «التخطيط» only the value is
    saved; in «المعالجة» approved pages and pages with review work are refused (422) and the page is
    re-derived and re-read when its regions changed. Returns `{regions, ocr_enqueued, undo}`, the undo
    (`{"replace": <override before>}`) only in «التخطيط».
    """
    awaits = check_stage(page.book_id, stage)
    _refuse_locked(page, awaits)
    before = page.guides_override
    clean = clean_guides(set_ if isinstance(set_, Mapping) else {}, partial=True)
    clean.pop("reference_page", None)
    if reset:
        override: dict = {}
    else:
        drop = set(_clean_keys(unset))
        override = {k: v for k, v in (before or {}).items() if k not in drop}
    override.update(clean)
    header, footnote = override.get("header_cut"), override.get("footnote_line")
    if header is not None and footnote is not None and header >= footnote:
        raise ValidationError(["يجب أن يكون حدّ الترويسة أعلى من خط الحاشية."])
    regions, changed = _save_override(page, override, awaits)
    return {"regions": regions, "ocr_enqueued": changed, "undo": {"replace": before} if awaits else None}


def set_page_guides_override(
    page: Page, data: Mapping | None, *, stage: str | None = None
) -> tuple[list[Region], bool]:
    """Replace the page's override (or clear it when `data` is empty / `reset`); the undo body as well.

    Returns `(regions, ocr_enqueued)`. In «التخطيط» only the value is saved (no regions yet); in
    «المعالجة» the page is re-derived and re-read when its regions changed. Raises ProcessingError
    for an excluded page, and once «المعالجة» started for an approved page or one with review work.
    """
    awaits = check_stage(page.book_id, stage)
    _refuse_locked(page, awaits)
    data = dict(data or {})
    reset = data.pop("reset", False)
    clean = {} if reset else clean_guides(data, partial=True)
    clean.pop("reference_page", None)
    return _save_override(page, clean, awaits)


def page_guides_answer(page: Page, regions: list[Region], enqueued: bool, undo: dict | None) -> dict:
    """The answer of `api:page_guides_override`: today's fields plus the `guides` block and the undo."""
    page.refresh_from_db(fields=["status", "guides_override", "reviewed_at", "is_excluded"])
    guides = _book_guides(page.book_id)
    pre = Preprocess.objects.filter(page=page).first()
    rows = list(page.regions.all())
    awaits = book_awaits_start(page.book_id)
    locked = page_lock(page, awaits, review_work_pages(page.book_id, [page.pk]) if not awaits else set())
    effective = guides_values(guides)
    for key in GUIDE_KEYS:
        if key in (page.guides_override or {}):
            effective[key] = page.guides_override[key]
    return {
        "page_id": page.pk,
        "status": page.status,
        "status_label": page.get_status_display(),
        "effective": effective,
        "override": page.guides_override,
        "regions": region_items(rows),
        "ocr_enqueued": enqueued,
        "guides": page_guides_payload(
            page, pre, guides_values(guides), is_manual(guides), rows, locked, guides_anchor(guides)
        ),
        "undo": undo,
    }


def parse_page_number(requested: str | None) -> int | None:
    """`?page=` value as a page number, None when it is not a plain decimal number."""
    try:
        return int(requested) if requested and requested.strip().isdecimal() else None
    except ValueError:
        return None
