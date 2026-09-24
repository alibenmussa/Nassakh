"""Template tags of the ocr app. Load with `{% load ocr_tags %}`."""

from __future__ import annotations

from django import template

from ocr import services

register = template.Library()


@register.simple_tag
def text_panel_data(page) -> dict:
    """Initial payload of the text panel (same shape as the `/text/` and `/runs/` endpoints).

    The partial embeds it with `json_script` so the panel paints without a first request; the
    Alpine component then polls while the page is still processing.
    """
    if page is None or page.pk is None:
        return {"lines": [], "runs": []}
    data = services.page_text_payload(page)
    data["runs"] = services.page_runs_payload(page)
    return data
