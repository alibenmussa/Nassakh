"""HTML + CSS → PDF with WeasyPrint (D42), footnotes numbered per page by two passes (D46).

WeasyPrint's footnote counter runs through the document (`counter-reset: footnote` on `@page` does
nothing), so per-page numbering is done here: pass 1 lays the book out and learns on which page every
footnote lands (each footnote element has an id, and a laid-out page lists the ids it holds in
`Page.anchors`); pass 2 sets each footnote's `data-n` to its rank on that page (the call and the marker
show `attr(data-n)`); a third pass runs only when a page assignment moved in pass 2 (a wider number can
push a call onto the next line). Per-chapter and per-book numbering need one pass.

One `FontConfiguration` serves the `CSS` object and every `render` of a job (WeasyPrint needs the same
instance for `@font-face` rules to apply).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .html import note_order, with_numbers

log = logging.getLogger(__name__)

MAX_PASSES = 3


class RenderCancelled(Exception):  # noqa: N818 - a signal, not an error
    """The job was cancelled between two passes (a newer render replaced it)."""


@dataclass
class PdfResult:
    """A rendered PDF: its bytes, page count, the first page (1-based) of every element id, the page of
    every footnote element, the numbers shown and how many layout passes it took."""

    pdf: bytes
    page_count: int
    anchors: dict[str, int] = field(default_factory=dict)
    footnote_pages: dict[str, int] = field(default_factory=dict)
    numbers: dict[str, str] = field(default_factory=dict)
    passes: int = 1
    duration_ms: int = 0


def first_pages(document) -> dict[str, int]:
    """Element id → the first page (1-based) it appears on, from a laid-out WeasyPrint document."""
    out: dict[str, int] = {}
    for index, page in enumerate(document.pages, start=1):
        for anchor in page.anchors:
            out.setdefault(anchor, index)
    return out


def per_page_numbers(order: list[str], pages: dict[str, int]) -> dict[str, str]:
    """Each footnote's rank on its page (`order` is the document order of the footnote ids)."""
    counts: dict[int, int] = {}
    numbers: dict[str, str] = {}
    for element_id in order:
        page = pages.get(element_id)
        if page is None:
            continue
        counts[page] = counts.get(page, 0) + 1
        numbers[element_id] = str(counts[page])
    return numbers


def render_pdf(
    html: str,
    css: str,
    fonts=None,
    *,
    numbering: str = "page",
    base_url: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> PdfResult:
    """Lay out `html` with `css` and write the PDF (`fonts`: a WeasyPrint `FontConfiguration`, one is
    made when None). `numbering` `page` runs the D46 passes; `cancelled()` is asked between passes and
    raises `RenderCancelled` when it answers True."""
    from weasyprint import CSS, HTML
    from weasyprint.text.fonts import FontConfiguration

    started = time.monotonic()
    font_config = fonts or FontConfiguration()
    sheet = CSS(string=css, font_config=font_config)

    def layout(markup: str):
        if cancelled is not None and cancelled():
            raise RenderCancelled
        return HTML(string=markup, base_url=base_url).render(stylesheets=[sheet], font_config=font_config)

    document = layout(html)
    anchors = first_pages(document)
    passes = 1
    order = note_order(html)
    numbers: dict[str, str] = {}
    note_pages = {element_id: anchors[element_id] for element_id in order if element_id in anchors}
    if numbering == "page" and order:
        wanted = per_page_numbers(order, note_pages)
        while passes < MAX_PASSES:
            document = layout(with_numbers(html, wanted))
            passes += 1
            numbers = wanted
            anchors = first_pages(document)
            moved = {element_id: anchors[element_id] for element_id in order if element_id in anchors}
            if moved == note_pages:
                break
            note_pages = moved
            wanted = per_page_numbers(order, note_pages)
            if wanted == numbers:
                break  # some notes moved, but every rank shown is still right
        if wanted != numbers:  # pragma: no cover - a note still moving after the third pass
            log.warning("footnote numbers may be off on some pages after %s passes", passes)
    if cancelled is not None and cancelled():
        raise RenderCancelled
    pdf = document.write_pdf()
    return PdfResult(
        pdf=pdf,
        page_count=len(document.pages),
        anchors=anchors,
        footnote_pages=note_pages,
        numbers=numbers,
        passes=passes,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
