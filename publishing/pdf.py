"""HTML + CSS → PDF with WeasyPrint (D42), footnotes numbered per page by two passes (D46).

WeasyPrint's footnote counter runs through the document (`counter-reset: footnote` on `@page` does
nothing), so per-page numbering is done here: pass 1 lays the book out and learns on which page every
footnote lands (each footnote element has an id, and a laid-out page lists the ids it holds in
`Page.anchors`); pass 2 sets each footnote's `data-n` to its rank on that page (the call and the marker
show `attr(data-n)`); a third pass runs only when a page assignment moved in pass 2 (a wider number can
push a call onto the next line). Per-chapter and per-book numbering need one pass.

One `FontConfiguration` serves the `CSS` object and every `render` of a job (WeasyPrint needs the same
instance for `@font-face` rules to apply). With `reuse`, the pair is kept for the next job with the same
CSS (the re-layout worker saves the ~0.1 s the CSS and its fonts take to load; one job at a time uses it).

**Layout (D47).** Given the markup's `texts`, the last pass's box tree is exported as the page layout
(`publishing.layout`); `pdf=False` stops there (the fast re-layout writes no PDF). `seed` (element id →
number) starts pass 1 with the numbers of the last layout: when no note changed page, one pass is enough.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from collections import OrderedDict
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
    layout: list[dict] | None = None  # the pages' layout (`publishing.layout`) when `texts` were given
    misses: int = 0  # characters the layout could not place in their block's text (0 when sound)
    patched: bool = False  # the footnote numbers are to be set in the layout (`numbers`), see `patch`


_SHEETS: OrderedDict[str, tuple] = OrderedDict()
_SHEETS_KEPT = 4
_SHEETS_LOCK = threading.RLock()
atexit.register(_SHEETS.clear)  # the kept font configurations go before the interpreter tears down


def _sheet(css: str, fonts, reuse: bool):
    """`(CSS, FontConfiguration)` for `css`: a new pair, or with `reuse` the kept one for the same CSS."""
    from weasyprint import CSS
    from weasyprint.text.fonts import FontConfiguration

    if not reuse or fonts is not None:
        font_config = fonts or FontConfiguration()
        return CSS(string=css, font_config=font_config), font_config
    found = _SHEETS.get(css)
    if found is not None:
        _SHEETS.move_to_end(css)
        return found
    font_config = FontConfiguration()
    found = (CSS(string=css, font_config=font_config), font_config)
    _SHEETS[css] = found
    while len(_SHEETS) > _SHEETS_KEPT:
        _SHEETS.popitem(last=False)
    return found


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
    texts: dict[str, str] | None = None,
    first_page: int = 1,
    pdf: bool = True,
    seed: dict[str, str] | None = None,
    reuse: bool = False,
    patch: bool = False,
) -> PdfResult:
    """Lay out `html` with `css` and write the PDF (`fonts`: a WeasyPrint `FontConfiguration`, one is
    made when None). `numbering` `page` runs the D46 passes; `cancelled()` is asked between passes and
    raises `RenderCancelled` when it answers True. With `texts` the layout is exported (pages numbered
    from `first_page`); `pdf=False` writes no PDF (`PdfResult.pdf` is empty); `seed` and `reuse`: see the
    module docstring. `patch` (a layout without a PDF, digits all as wide): when the footnote numbers of
    pass 1 are wrong but as long as the right ones, no second pass is laid out — the caller sets the
    numbers in the layout (`PdfResult.patched`; the lines cannot move)."""
    options = (numbering, base_url, cancelled, texts, first_page, pdf, seed, patch)
    if reuse and fonts is None:
        with _SHEETS_LOCK:
            return _render(html, css, None, *options, reuse=True)
    return _render(html, css, fonts, *options, reuse=False)


def _render(
    html, css, fonts, numbering, base_url, cancelled, texts, first_page, pdf, seed, patch, *, reuse
) -> PdfResult:
    from weasyprint import HTML

    started = time.monotonic()
    sheet, font_config = _sheet(css, fonts, reuse)

    def layout(markup: str):
        if cancelled is not None and cancelled():
            raise RenderCancelled
        return HTML(string=markup, base_url=base_url).render(stylesheets=[sheet], font_config=font_config)

    order = note_order(html)
    numbers: dict[str, str] = {}
    seeded = numbering == "page" and bool(seed) and bool(order)
    if seeded:
        numbers = {element_id: str(seed.get(element_id) or "1") for element_id in order}
        html_first = with_numbers(html, numbers)
    else:
        html_first = html
    document = layout(html_first)
    anchors = first_pages(document)
    passes = 1
    note_pages = {element_id: anchors[element_id] for element_id in order if element_id in anchors}
    patched = False
    if numbering == "page" and order:
        wanted = per_page_numbers(order, note_pages)
        shown = numbers if seeded else {element_id: "1" for element_id in order}
        if wanted == {element_id: shown[element_id] for element_id in wanted}:
            numbers = wanted  # every number shown in pass 1 is already right: no second pass
        elif patch and not pdf and all(len(value) == len(shown[key]) for key, value in wanted.items()):
            numbers = wanted  # as long as the numbers shown, digits all as wide: set in the layout instead
            patched = True
        while passes < MAX_PASSES and wanted != numbers:
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
    pages = misses = None
    if texts is not None:
        from .layout import extract_layout

        pages, misses = extract_layout(document, texts, first_page=first_page)
    data = document.write_pdf() if pdf else b""
    return PdfResult(
        pdf=data,
        page_count=len(document.pages),
        anchors=anchors,
        footnote_pages=note_pages,
        numbers=numbers,
        passes=passes,
        duration_ms=int((time.monotonic() - started) * 1000),
        layout=pages,
        misses=misses or 0,
        patched=patched,
    )
