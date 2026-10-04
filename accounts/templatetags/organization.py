"""Template tags of the organisation (D98): its faces shown in their own face."""

from __future__ import annotations

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def org_font_faces(owner) -> str:
    """A `<style data-org-fonts>` with the `@font-face` rules of the active faces of an organisation (or of
    a book's organisation): the font menus show each face in itself (`nk-org-<pk>`,
    `publishing.fonts.org_sample_css`). Empty without one."""
    from publishing.fonts import org_sample_css

    organization = getattr(owner, "organization", owner) if owner is not None else None
    if organization is None or not getattr(organization, "pk", None):
        return ""
    css = org_sample_css(organization)
    # the rules carry only `nk-org-<pk>` families and reversed URLs of content-named files
    return mark_safe(f"<style data-org-fonts>{css}</style>") if css else ""


@register.filter
def ar_pages(n) -> str:
    """«صفحة واحدة», «صفحتان», «5 صفحات», «300 صفحة» (D106: the page quota's numbers in words)."""
    from accounts.billing import pages_phrase

    try:
        return pages_phrase(int(n))
    except (TypeError, ValueError):
        return ""


@register.filter
def ar_days(n) -> str:
    """«يوم واحد», «يومين», «7 أيام», «365 يومًا»."""
    from accounts.signup import days_phrase

    try:
        return days_phrase(int(n))
    except (TypeError, ValueError):
        return ""
