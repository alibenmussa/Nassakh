"""The publishing engine (D42): the one interface the rest of Nassakh calls to turn a manuscript into pages.

Two layers:

- **The renderer**, `Engine.render(job) -> Rendered`: a manuscript document, a stylesheet and a scope
  (the whole book, some chapters starting at a given page, or a window of a book without chapter breaks
  starting at a block) in; a PDF, its page count, each chapter's first and last page, the page layout
  (every line's box, runs and character range, D47) and the page checks out. `WeasyPrintEngine` is the
  implementation (book model → `publishing.html` + `publishing.css` → `publishing.pdf`, D46 passes,
  `publishing.layout`); another engine (a different typesetter) only has to implement `render`.
  `ENGINE_VERSION` is part of every preview hash: bump it when the markup or the CSS change so cached
  previews are redone.
- **The preview service** the book page and the dashboard use (implemented in `publishing.preview` and
  `publishing.relayout`): `request_preview` (find the cached render for the current content or queue
  one), `preview_payload` (the API's answer), `schedule_after_edit` (after an editor save: the chapter's
  fast re-layout at once, the book render debounced, D44/D47), `request_relayout` (the fast re-layout of
  a chapter), `layout_state` (the dashboard's «الكتاب» block) and `render_book_pdf` (the export).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

ENGINE_VERSION = "nk-print-4"


@dataclass(frozen=True)
class RenderJob:
    """What to render: the whole manuscript document (a chapter is picked by `chapter_id`, several by
    `chapter_ids`), the page setup (`publishing.model.PageSetup`, a stylesheet or a dict of its fields),
    the book's title and author (used when the document's title node has none), the scope (`book`,
    `chapter`, or `window`: the chapters from the block `start_block` on) and the first page number.

    `pdf=False` lays out without writing the PDF (the fast re-layout, D47); `seed` (note id → number)
    starts the footnote passes from the numbers of the last layout; `reuse` keeps the loaded CSS and fonts
    for the next job (the layout worker)."""

    document: dict
    stylesheet: object
    title: str = ""
    author: str = ""
    scope: str = "book"
    chapter_id: str | None = None
    first_page: int = 1
    chapter_ids: tuple[str, ...] | None = None
    start_block: str | None = None
    pdf: bool = True
    seed: dict | None = None
    reuse: bool = False
    continues: bool = False  # a window that goes on from the pages before it (not the book's first text page)

    def wanted(self) -> list[str] | None:
        """The chapters the job renders (None: all of them)."""
        if self.scope == "book":
            return None
        if self.chapter_ids:
            return list(self.chapter_ids)
        return [self.chapter_id] if self.chapter_id else None


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
    layout: list[dict] = field(default_factory=list)  # the pages' layout (`publishing.layout`, D47)
    checks: list[dict] = field(default_factory=list)  # `publishing.layout.page_checks`
    numbers: dict[str, str] = field(default_factory=dict)  # footnote element id → the number shown
    misses: int = 0  # characters the layout could not place (0 for a sound layout)


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
        from editor import document as doc

        from .css import stylesheet_css
        from .fonts import number_face, resolve, tabular_digits
        from .html import render_markup
        from .layout import assign_chapters, page_checks, set_note_numbers
        from .model import book_model
        from .pdf import render_pdf

        started = time.monotonic()
        book = book_model(
            job.document, job.stylesheet, title=job.title, author=job.author, chapter_ids=job.wanted()
        )
        setup = book.setup
        fonts = resolve(setup.body_font, setup.latin_font, setup.heading_font)
        markup = render_markup(book, scope=job.scope, start_block=job.start_block)
        css = stylesheet_css(
            setup, fonts, scope=job.scope, first_page=job.first_page, continues=job.continues
        )
        seed = None
        if job.seed:
            seed = {markup.notes[key]: str(value) for key, value in job.seed.items() if key in markup.notes}
        result = render_pdf(
            markup.html,
            css,
            numbering=setup.footnote_numbering,
            cancelled=cancelled,
            texts=markup.texts,
            first_page=job.first_page,
            pdf=job.pdf,
            seed=seed,
            reuse=job.reuse,
            patch=not job.pdf and tabular_digits(number_face(fonts).files.regular),
        )
        if result.patched and result.layout:
            by_note = {
                note: result.numbers[element]
                for note, element in markup.notes.items()
                if element in result.numbers
            }
            set_note_numbers(result.layout, by_note)
        chapters = chapter_ranges(book.chapters, result.anchors, result.page_count, job.first_page)
        versions = {c.id: doc.chapter_version(c.nodes(job.document)) for c in doc.chapters_of(job.document)}
        for item in chapters:
            item["version"] = versions.get(item["id"])
        pages = result.layout or []
        assign_chapters(pages, chapters)
        return Rendered(
            pdf=result.pdf,
            page_count=result.page_count,
            chapters=chapters,
            passes=result.passes,
            duration_ms=int((time.monotonic() - started) * 1000),
            missing_fonts=fonts.missing,
            footnotes=result.footnote_pages,
            layout=pages,
            checks=page_checks(pages, chapters, fonts.missing),
            numbers=result.numbers,
            misses=result.misses,
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


def schedule_after_edit(book, chapter_id: str | None, version: int):
    """After an editor save (D44, D47): the edited chapter's fast re-layout now, the whole book once edits
    settle (debounced; a later save supersedes it). Returns the re-layout row to poll (None: nothing
    asked for)."""
    from . import preview

    return preview.schedule_after_edit(book, chapter_id, version)


def render_after_assembly(book):
    """The book render right after a whole-book assembly (D49; `preview.render_after_assembly`)."""
    from . import preview

    return preview.render_after_assembly(book)


def request_relayout(book, chapter_id: str, version: int | None = None):
    """The fast re-layout of one chapter (D47, `publishing.relayout`): the row to poll, None when the live
    layout already shows this version of the chapter."""
    from . import relayout

    return relayout.request_relayout(book, chapter_id, version)


def relayout_payload(row) -> dict:
    """The re-layout API's answer for a row (or None)."""
    from . import relayout

    return relayout.relayout_payload(row, pages=False)


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
    """Chapter id → `{first, last}` printed pages in the live layout, else in the newest finished book
    render (empty before one)."""
    from . import preview, relayout

    live = relayout.live_of(book.pk)
    render = preview.latest_done(book.pk, "book") if live is None else None
    items = live.chapters if live is not None else (render.chapters if render is not None else [])
    out: dict[str, dict] = {}
    for item in items or []:
        if isinstance(item, dict) and item.get("id"):
            out[item["id"]] = {"first": item.get("first"), "last": item.get("last")}
    return out
