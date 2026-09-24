"""Plain-dict serialisers shared by the books and processing apps (screens and JSON APIs)."""

from __future__ import annotations

from collections.abc import Iterable

from core.templatetags.nassakh import FLAG_LABELS


def flag_items(flags: Iterable[str] | None) -> list[dict]:
    """Attention flags as `[{"code", "label"}]` with Arabic labels (unknown codes shown as they are)."""
    return [{"code": str(flag), "label": FLAG_LABELS.get(str(flag), str(flag))} for flag in flags or []]


def region_items(regions: Iterable) -> list[dict]:
    """Regions as JSON-ready dicts (`id`, `kind`, `label`, `bbox`, `order`, `source`), bbox in gray pixels."""
    return [
        {
            "id": region.pk,
            "kind": region.kind,
            "label": region.get_kind_display(),
            "bbox": region.bbox,
            "order": region.order,
            "source": region.source,
        }
        for region in regions
    ]
