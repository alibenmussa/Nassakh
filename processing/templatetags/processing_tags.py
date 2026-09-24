"""Template tags of the processing app. Load with `{% load processing_tags %}`."""

from __future__ import annotations

from django import template
from django.urls import reverse
from django.utils.html import json_script
from django.utils.safestring import SafeString

from processing import services

register = template.Library()


@register.simple_tag
def preprocess_panel_config(page, element_id: str) -> SafeString:
    """`<script type="application/json">` with the preprocess panel's initial state for a page.

    Used by `processing/_preprocess_panel.html`, which is included from the page-detail screen
    with only `page` and `book` in its context.
    """
    config = services.preprocess_payload(page)
    config["api_url"] = reverse("api:page_preprocess", kwargs={"page_id": page.pk})
    config["guides_url"] = reverse("api:page_guides_override", kwargs={"page_id": page.pk})
    config["guides"] = services.effective_guides(page)
    config["override"] = page.guides_override
    return json_script(config, element_id)
