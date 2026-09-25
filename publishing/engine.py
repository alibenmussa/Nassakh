"""The publishing engine (D42): the one interface the rest of Nassakh calls to turn a manuscript into pages.

Two layers:

- **The renderer**, `Engine.render(job) -> Rendered`: a manuscript document, a stylesheet and a scope
  (the whole book, or one chapter starting at a given page) in; a PDF, its page count and each chapter's
  first and last page out. `WeasyPrintEngine` is the implementation (book model → `publishing.html` +
  `publishing.css` → `publishing.pdf`, D46 passes); another engine (a different typesetter) only has to
  implement `render`. `ENGINE_VERSION` is part of every preview hash: bump it when the markup or the CSS
  change so cached previews are redone.
- **The preview service** the editor, the layout page and the dashboard use (implemented in
  `publishing.preview`): `request_preview` (find the cached render for the current content or queue
  one), `preview_payload` (the API's answer), `schedule_after_edit` (the debounced renders after an
  editor save, D44), `layout_state` (the dashboard's «الكتاب» block) and `render_book_pdf` (the export).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

ENGINE_VERSION = "nk-print-2"


@dataclass(frozen=True)
class RenderJob:
    """What to render: the whole manuscript document (a chapter is picked by `chapter_id`), the page
    setup (`publishing.model.PageSetup`, a stylesheet or a dict of its fields), the book's title and
    author (used when the document's title node has none), the scope and the first page number."""

    document: dict
    stylesheet: object
    title: str = ""
    author: str = ""
    scope: str = "book"
    chapter_id: str | None = None
    first_page: int = 1


@dataclass
class Rendered:
    """A finished render: the PDF, its page count, `[{id, title, first, last}]` per chapter (printed page
    numbers), the layout passes, the time taken and the faces that were missing (Amiri stood in)."""

    pdf: bytes
    page_count: int
    chapters: list[dict] = field(default_factory=list)
    passes: int = 1
    duration_ms: int = 0
    missing_fonts: list[dict] = field(default_factory=list)
    footnotes: dict[str, int] = field(default_factory=dict)  # footnote element id → page (1-based index)


class Engine(Protocol):
    """A page renderer."""

    name: str
    version: str

    def render(self, job: RenderJob, cancelled: Callable[[], bool] | None = None) -> Rendered: ...


def chapter_ranges(chapters, anchors: dict[str, int], page_count: int, first_page: int = 1) -> list[dict]:
    """`[{id, title, first, last}]`: each chapter from the page of its `ch-<id>` anchor to the page before
    the next chapter (the last one to the end), as printed numbers (`first_page` is page 1's number).
    A section of a book without headings runs on (no page break before it), so the one before it ends
    on the page where it starts."""
    from .html import chapter_anchor

    starts: list[tuple[object, int]] = []
    for chapter in chapters:
        page = anchors.get(chapter_anchor(chapter))
        if page is not None:
            starts.append((chapter, page))
    out: list[dict] = []
    for i, (chapter, page) in enumerate(starts):
        if i + 1 < len(starts):
            following, next_page = starts[i + 1]
            last = next_page if getattr(following, "kind", "") == "section" else next_page - 1
        else:
            last = page_count
        out.append(
            {
                "id": chapter.id,
                "title": chapter.title,
                "first": page + first_page - 1,
                "last": max(page, last) + first_page - 1,
            }
        )
    return out


class WeasyPrintEngine:
    """The WeasyPrint renderer (verified on this Mac: HarfBuzz shaping, footnotes at the page foot,
    running headers, page numbers, recto openings, embedded and subset fonts; ~0.1 s per chapter)."""

    name = "weasyprint"

    @property
    def version(self) -> str:
        import weasyprint

        return f"{ENGINE_VERSION}/weasyprint-{weasyprint.__version__}"

    def render(self, job: RenderJob, cancelled: Callable[[], bool] | None = None) -> Rendered:
        from .css import stylesheet_css
        from .fonts import resolve
        from .html import render_html
        from .model import book_model
        from .pdf import render_pdf

        started = time.monotonic()
        wanted = [job.chapter_id] if job.scope == "chapter" and job.chapter_id else None
        book = book_model(
            job.document, job.stylesheet, title=job.title, author=job.author, chapter_ids=wanted
        )
        setup = book.setup
        fonts = resolve(setup.body_font, setup.latin_font, setup.heading_font)
        html = render_html(book, scope=job.scope)
        css = stylesheet_css(setup, fonts, scope=job.scope, first_page=job.first_page)
        result = render_pdf(html, css, numbering=setup.footnote_numbering, cancelled=cancelled)
        return Rendered(
            pdf=result.pdf,
            page_count=result.page_count,
            chapters=chapter_ranges(book.chapters, result.anchors, result.page_count, job.first_page),
            passes=result.passes,
            duration_ms=int((time.monotonic() - started) * 1000),
            missing_fonts=fonts.missing,
            footnotes=result.footnote_pages,
        )


def get_engine() -> Engine:
    """The configured renderer (WeasyPrint; D42 keeps the choice behind this function)."""
    return WeasyPrintEngine()


# ====================================================================== the preview service


def request_preview(book, scope: str = "book", chapter_id: str | None = None, *, force: bool = False):
    """The render for the book's current content: a cached one, the one on its way, or a new one queued
    (a running render of an older content of the same scope is cancelled and revoked). None without a
    manuscript (or without that chapter). `force` renders again after an error."""
    from . import preview

    return preview.request_preview(book, scope, chapter_id, force=force)


def preview_payload(
    book, scope: str = "book", chapter_id: str | None = None, *, enqueue: bool = True
) -> dict:
    """`{status, hash, page_count, chapters, pages: [{n, url, url2x, chapter}], stale, …}` (the API)."""
    from . import preview

    return preview.preview_payload(book, scope, chapter_id, enqueue=enqueue)


def schedule_after_edit(book, chapter_id: str | None, version: int) -> None:
    """After an editor save (D44): render the edited chapter once edits settle (a few seconds), then the
    whole book (debounced); a later save supersedes both."""
    from . import preview

    preview.schedule_after_edit(book, chapter_id, version)


def layout_state(book, manuscript_exists: bool = True) -> dict:
    """The dashboard's «الكتاب» block: `{trim, trim_label, page_count, rendering, stale}`."""
    from . import preview

    return preview.layout_state(book, manuscript_exists)


def render_book_pdf(book) -> Rendered | None:
    """The whole book as a PDF, rendered now (the export; Phase 6). None without a manuscript."""
    from . import preview

    job = preview.job_for(book, "book", None)
    return get_engine().render(job) if job is not None else None


def chapter_pages(book) -> dict[str, dict]:
    """Chapter id → `{first, last}` printed pages in the newest finished book render (empty before one)."""
    from . import preview

    render = preview.latest_done(book.pk, "book")
    out: dict[str, dict] = {}
    for item in (render.chapters if render is not None else []) or []:
        if isinstance(item, dict) and item.get("id"):
            out[item["id"]] = {"first": item.get("first"), "last": item.get("last")}
    return out
