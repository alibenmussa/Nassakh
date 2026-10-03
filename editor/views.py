"""Editor pages (PHASE5_SPEC §5, §9 D47).

- `editor:layout` → `/books/<id>/layout/[?chapter=<cid>][&mode=edit][&tab=<tab>][&block=<id>]`, the book
  page (`bookLayout`): live pages, preview and edit, the one side panel (`?tab=find&q=&r=&fix=` fills in its
  find & replace, D79)
- `editor:edit`   → `/books/<id>/editor/[?chapter=<cid>]`, the old editor address: 302 to the book page on
  that chapter in edit mode

The book page renders with `config` (`editor.services.page_config`), the Alpine component's start, as
JSON. `editor_context` builds the old editor page's context (its template stays until the UI merge).
Another organisation's book answers 404 (`books.access`, D102).
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse

from books.access import get_book_or_404
from books.models import Book

from . import services

INITIAL_LAYOUT_PAGES = 4


def _context(request: HttpRequest, book_id: int, page: str) -> dict:
    book = get_book_or_404(request.user, book_id)
    chapter_id = request.GET.get("chapter") or None
    config = services.page_config(
        book,
        request.user,
        chapter_id,
        page,
        request.GET.get("mode"),
        tab=request.GET.get("tab"),
        block=request.GET.get("block"),
        find={
            key: request.GET.get(key)
            for key in ("q", "r", "fix", "match_tashkeel", "fold_alef", "whole_word")
        },
    )
    return {"book": book, "config": config, "exists": config["exists"], "can_edit": config["canEdit"]}


def editor_context(request: HttpRequest, book_id: int) -> dict:
    """The old editor page's context (`editor/edit.html`), kept for its template until the UI merge."""
    return _context(request, book_id, "editor")


@login_required
def edit(request: HttpRequest, book_id: int) -> HttpResponse:
    """The old editor address (D47): the book page on the chapter, in edit mode."""
    get_book_or_404(request.user, book_id)
    query = {"mode": "edit"}
    if request.GET.get("chapter"):
        query = {"chapter": request.GET["chapter"], **query}
    return HttpResponseRedirect(f"{reverse('editor:layout', args=[book_id])}?{urlencode(query)}")


def _layout_initial(book: Book, config: dict) -> dict:
    """The book page's first paint (`bookLayout`): the stylesheet payload, the book's cached preview, when a
    chapter was asked for that chapter's, and the first live pages (`layout`: the requested chapter's first
    pages, else the book's). Nothing is queued here (the component's first GET does), so opening the page
    never starts a render by itself."""
    from publishing import engine, relayout
    from publishing.preview import PreviewNotFound

    out: dict = {
        "stylesheet": services.stylesheet_payload(book),
        "preview": None,
        "chapterPreview": None,
        "layout": None,
        # D80: the cover's cached render, so its sheet paints at once (never rendered here: `pending` asks the
        # component to fetch api:cover, which renders it)
        "cover": services.cached_cover(book),
    }
    if not config["exists"]:
        return out
    try:
        out["preview"] = engine.preview_payload(book, "book", enqueue=False)
    except PreviewNotFound:
        return out
    start = None
    requested = config.get("requestedChapter")
    for item in (out["preview"].get("layout") or {}).get("chapters") or []:
        if item.get("id") == requested and isinstance(item.get("first"), int):
            start = item["first"]
    try:
        # the first live pages (the requested chapter's), so the stage paints without waiting for a fetch
        out["layout"] = relayout.layout_payload(
            book, first=start, last=(start or 1) + INITIAL_LAYOUT_PAGES - 1
        )
    except relayout.LayoutNotFound:
        out["layout"] = None
    chapter_id = config.get("requestedChapter")
    if chapter_id and any(chapter["id"] == chapter_id for chapter in config["chapters"]):
        try:
            out["chapterPreview"] = engine.preview_payload(book, "chapter", chapter_id, enqueue=False)
        except PreviewNotFound:
            pass
    return out


def _first_page(initial: dict, chapter_id: str | None) -> dict | None:
    """The page the server paints in the stage: the requested chapter's first page, else the first page."""
    preview = initial.get("preview") or {}
    pages = preview.get("pages") or []
    if chapter_id:
        for chapter in preview.get("chapters") or []:
            if chapter.get("id") == chapter_id:
                for page in pages:
                    if page["n"] == chapter.get("first"):
                        return page
        chapter_preview = initial.get("chapterPreview") or {}
        if chapter_preview.get("pages"):
            return chapter_preview["pages"][0]
    return pages[0] if pages else None


@login_required
def layout(request: HttpRequest, book_id: int) -> HttpResponse:
    """The stylesheet panel and the page preview (an empty state before the first assembly)."""
    from books.services import book_stages

    context = _context(request, book_id, "layout")
    config = context["config"]
    context["stage_steps"] = book_stages(context["book"], "book")  # the stage bar (D76)
    config["initial"] = _layout_initial(context["book"], config)
    context["initial"] = config["initial"]
    context["first_page"] = _first_page(config["initial"], config.get("requestedChapter"))
    context["preview_pages"] = (config["initial"].get("preview") or {}).get("pages") or []
    return render(request, "editor/layout.html", context)
