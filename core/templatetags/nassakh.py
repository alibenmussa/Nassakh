"""Template tags and filters shared by all Nassakh screens. Load with `{% load nassakh %}`."""

from __future__ import annotations

from django import template

register = template.Library()

# Status → colour token (DESIGN.md §2): in progress = accent, done = success, pending = neutral,
# error = danger, attention = warning. Page and Book statuses share one map; unknown values are neutral.
STATUS_COLOURS: dict[str, str] = {
    # Page
    "uploaded": "neutral",
    "preprocessed": "accent",
    "layout_done": "accent",
    "ocr_done": "success",
    "reviewed": "success",
    "assembled": "success",
    "error": "danger",
    "excluded": "neutral",
    # Book
    "processing": "accent",
    "needs_guides": "warning",
    "ocr": "accent",
    "ready_for_review": "success",
    "reviewing": "accent",
    # Text state
    "none": "neutral",
    "provisional": "accent",
    "final": "success",
}

# Attention flags raised by the pipeline → Arabic labels.
FLAG_LABELS: dict[str, str] = {
    "large_skew": "انحراف كبير",
    "deskew_low_confidence": "ثقة منخفضة في تصحيح الانحراف",
    "no_lines_detected": "لم تُكتشف أسطر",
    "edge_strip_removed": "أُزيل شريط من حافة الصفحة",
    "ocr_fallback": "استُخدم نص Tesseract الاحتياطي",
    "alignment_poor": "ربط الكلمات بالأسطر ضعيف",
    "lines_merged": "سطران مطبوعان في سطر واحد",
}


@register.filter
def status_dot(status: str) -> str:
    """CSS modifier class for `.dot` matching a Page/Book status, e.g. `dot-accent`."""
    return f"dot-{STATUS_COLOURS.get(str(status), 'neutral')}"


@register.filter
def percent(a, b) -> int:
    """Integer percentage of `a` out of `b` (0 when `b` is zero or values are missing)."""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return 0
    if b <= 0:
        return 0
    return int(round(100 * a / b))


percent_of = percent


@register.inclusion_tag("partials/_progress.html")
def progress_bar(value=None, total=None, percent=None, thin=False, label="", state="") -> dict:
    """Render `partials/_progress.html` from `value`/`total` or an explicit `percent` (0-100).

    `state` picks the fill colour (success | danger | warning | neutral; default accent).
    """
    if percent is None:
        percent = percent_of(value, total)
    return {"percent": percent, "thin": thin, "label": label, "state": state}


@register.filter
def ar_status(obj) -> str:
    """Arabic label of an object's `status` (Page or Book); falls back to `str(obj)`."""
    display = getattr(obj, "get_status_display", None)
    if callable(display):
        return display()
    return str(obj) if obj is not None else ""


@register.filter
def ar_flag(flag: str) -> str:
    """Arabic label for an attention flag; unknown flags are shown as they are."""
    return FLAG_LABELS.get(str(flag), str(flag))


def _stage_labels() -> dict[str, str]:
    # Imported lazily: books.services imports this module (FLAG_LABELS, status_dot).
    from books.services import STAGE_LABELS

    return dict(STAGE_LABELS)


@register.filter
def stage_label(stage: str) -> str:
    """Arabic label of a pipeline stage key (e.g. `ocr_full`); unknown keys are shown as they are."""
    return _stage_labels().get(str(stage), str(stage or ""))


@register.simple_tag
def stage_labels() -> dict[str, str]:
    """`{stage key: Arabic label}` for the retry buttons that Alpine components render."""
    return _stage_labels()


@register.simple_tag
def optional_url(name: str, *args) -> str:
    """`reverse(name, args)` or "" when the route does not exist (lets a template feature-detect it)."""
    from django.urls import NoReverseMatch, reverse

    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""
