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

**Notes kept with their calls.** WeasyPrint drops a footnote to the next page when it steps back to honour
`widows` / `orphans` at the foot of a page, although its call stays (seen on real books: a call at the foot
of page 4, its note on page 5, room left on page 4). After the numbering passes the calls are located in the
box tree; for every note printed on a later page than its call, `widows` and `orphans` are relaxed to 1 on
the paragraph holding the call, on the paragraphs either side of that page break and on the one after each,
and the book is laid out again (at most twice).

**Layout (D47).** Given the markup's `texts`, the last pass's box tree is exported as the page layout
(`publishing.layout`); `pdf=False` stops there (the fast re-layout writes no PDF). `seed` (element id →
number) starts pass 1 with the numbers of the last layout: when no note changed page, one pass is enough.

**Exports (D61).** `on_pass(step)` is told each pass as it starts (`layout`, `footnotes`, `relax`) and
`write` before the PDF is written — the export's progress, asked where `cancelled()` is asked, so no
WeasyPrint internals are needed. `finisher` and `write_options` go to `Document.write_pdf`;
`finisher_for(output)` is the export's: the BleedBox exactly the trim plus the bleed (WeasyPrint caps it
at 10 pt from the trim), right-to-left reading and the document title in the viewer, the outline pane open
for the screen PDF, `Trapped /False` for print, and the subject, creator and dates.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .html import note_order, with_numbers

log = logging.getLogger(__name__)

MAX_PASSES = 3
MAX_RELAX_PASSES = 2


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


def call_pages(document) -> tuple[dict[str, tuple[int, str | None]], list[tuple[str | None, str | None]]]:
    """Footnote element id → (the page, 1-based, of its call, the block holding the call), and for every
    page its first and last body blocks (the blocks at its page breaks)."""
    calls: dict[str, tuple[int, str | None]] = {}
    ends: list[tuple[str | None, str | None]] = []
    for index, page in enumerate(document.pages, start=1):
        first = last = None
        stack = [(page._page_box, None)]
        while stack:
            box, block = stack.pop()
            element = getattr(box, "element", None)
            tag = getattr(box, "element_tag", "") or ""
            if element is not None and hasattr(element, "get"):
                if tag.endswith("::footnote-call"):
                    element_id = element.get("id")
                    if element_id:
                        calls.setdefault(element_id, (index, block))
                elif "::" not in tag and element.get("data-block") and element.get("data-kind") != "note":
                    block = element.get("data-block")
                    if first is None:
                        first = block
            if block is not None and getattr(box, "children", None) is None:
                last = block
            children = list(getattr(box, "children", None) or ())
            stack.extend((child, block) for child in reversed(children))  # document order
        ends.append((first, last))
    return calls, ends


def displaced_blocks(order: list[str], note_pages: dict[str, int], found) -> set[str]:
    """The blocks at the page break after every call whose note is printed on a later page: the last block
    of the call's page and the first of the next (WeasyPrint loses the note when it steps back there for
    `widows` / `orphans`), and the block holding the call."""
    calls, ends = found
    blocks: set[str] = set()
    for element_id in order:
        call = calls.get(element_id)
        note = note_pages.get(element_id)
        if not call or not note or note <= call[0]:
            continue
        page = call[0]
        if call[1]:
            blocks.add(call[1])
        if 0 < page <= len(ends) and ends[page - 1][1]:
            blocks.add(ends[page - 1][1])
        if page < len(ends) and ends[page][0]:
            blocks.add(ends[page][0])
    return blocks


def relax_css(blocks: set[str]) -> str:
    """`widows` / `orphans` 1 on these blocks and on the block after each."""
    names = [str(block).replace("\\", "\\\\").replace('"', '\\"') for block in sorted(blocks)]
    selectors = ", ".join(f'[data-block="{name}"], [data-block="{name}"] + *' for name in names)
    return f"{selectors} {{ widows: 1 !important; orphans: 1 !important; }}"


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
    write_options: dict | None = None,
    finisher: Callable | None = None,
    on_pass: Callable[[str], None] | None = None,
) -> PdfResult:
    """Lay out `html` with `css` and write the PDF (`fonts`: a WeasyPrint `FontConfiguration`, one is
    made when None). `numbering` `page` runs the D46 passes; `cancelled()` is asked between passes and
    raises `RenderCancelled` when it answers True. With `texts` the layout is exported (pages numbered
    from `first_page`); `pdf=False` writes no PDF (`PdfResult.pdf` is empty); `seed` and `reuse`: see the
    module docstring. `patch` (a layout without a PDF, digits all as wide): when the footnote numbers of
    pass 1 are wrong but as long as the right ones, no second pass is laid out — the caller sets the
    numbers in the layout (`PdfResult.patched`; the lines cannot move). `write_options`, `finisher` and
    `on_pass`: see the module docstring (exports)."""
    writing = (write_options, finisher, on_pass)
    options = (numbering, base_url, cancelled, texts, first_page, pdf, seed, patch, writing)
    if reuse and fonts is None:
        with _SHEETS_LOCK:
            return _render(html, css, None, *options, reuse=True)
    return _render(html, css, fonts, *options, reuse=False)


def _render(
    html, css, fonts, numbering, base_url, cancelled, texts, first_page, pdf, seed, patch, writing, *, reuse
) -> PdfResult:
    from weasyprint import HTML

    write_options, finisher, on_pass = writing
    started = time.monotonic()
    sheet, font_config = _sheet(css, fonts, reuse)

    extra: list = []  # the relaxed widows / orphans around a displaced note, when needed

    def layout(markup: str, step: str):
        if on_pass is not None:
            on_pass(step)
        if cancelled is not None and cancelled():
            raise RenderCancelled
        document = HTML(string=markup, base_url=base_url)
        return document.render(stylesheets=[sheet, *extra], font_config=font_config)

    order = note_order(html)
    numbers: dict[str, str] = {}
    seeded = numbering == "page" and bool(seed) and bool(order)
    if seeded:
        numbers = {element_id: str(seed.get(element_id) or "1") for element_id in order}
        html_first = with_numbers(html, numbers)
    else:
        html_first = html
    document = layout(html_first, "layout")
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
            document = layout(with_numbers(html, wanted), "footnotes")
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
    if order:
        from weasyprint import CSS

        relaxed: set[str] = set()
        for _ in range(MAX_RELAX_PASSES):
            blocks = displaced_blocks(order, note_pages, call_pages(document)) - relaxed
            if not blocks:
                break
            relaxed |= blocks
            extra[:] = [CSS(string=relax_css(relaxed), font_config=font_config)]
            markup = with_numbers(html, numbers) if numbering == "page" and numbers else html
            document = layout(markup, "relax")
            passes += 1
            anchors = first_pages(document)
            note_pages = {element_id: anchors[element_id] for element_id in order if element_id in anchors}
            if numbering == "page":
                wanted = per_page_numbers(order, note_pages)
                if wanted != numbers:
                    document = layout(with_numbers(html, wanted), "footnotes")
                    passes += 1
                    numbers = wanted
                    anchors = first_pages(document)
                    note_pages = {key: anchors[key] for key in order if key in anchors}
                patched = False
    if cancelled is not None and cancelled():
        raise RenderCancelled
    pages = misses = None
    if texts is not None:
        from .layout import extract_layout

        pages, misses = extract_layout(document, texts, first_page=first_page)
    data = b""
    if pdf:
        if on_pass is not None:
            on_pass("write")
        data = document.write_pdf(finisher=finisher, **(write_options or {}))
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


# ====================================================================== the exports' finisher (D61)

MM_PT = 72 / 25.4


def pdf_date(value: datetime) -> str:
    """A PDF date (`D:20260926100211+02'00'`; `Z` for UTC, a naive time is taken as UTC)."""
    stamp = value.strftime("D:%Y%m%d%H%M%S")
    offset = value.utcoffset()
    if offset is None or not offset:
        return stamp + "Z"
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{stamp}{sign}{minutes // 60:02d}'{minutes % 60:02d}'"


def page_objects(pdf) -> list:
    """The page dictionaries of a `pydyf.PDF`, in order."""
    return [pdf.objects[number] for number in pdf.pages["Kids"][::3]]


def finisher_for(output) -> Callable:
    """The `write_pdf` finisher of a PDF export (`output`: `publishing.engine.PdfOutput`): see the module
    docstring."""
    import pydyf

    metadata = output.metadata
    bleed = float(output.bleed_mm) * MM_PT if output.is_print else 0.0

    def finish(document, pdf) -> None:
        for page in page_objects(pdf):
            left, top, right, bottom = (float(value) for value in page["TrimBox"])
            page["BleedBox"] = pydyf.Array([left - bleed, top - bleed, right + bleed, bottom + bleed])
        preferences = {"Direction": "/R2L", "DisplayDocTitle": "true"}
        pdf.catalog["ViewerPreferences"] = pydyf.Dictionary(preferences)
        if metadata.lang:
            pdf.catalog["Lang"] = pydyf.String(metadata.lang)
        if output.is_print:
            pdf.info["Trapped"] = "/False"
        else:
            pdf.catalog["PageMode"] = "/UseOutlines"
        if metadata.subject:
            pdf.info["Subject"] = pydyf.String(metadata.subject)
        if metadata.creator:
            pdf.info["Creator"] = pydyf.String(metadata.creator)
        if metadata.created is not None:
            stamp = pydyf.String(pdf_date(metadata.created))
            pdf.info["CreationDate"] = stamp
            pdf.info["ModDate"] = stamp

    return finish
