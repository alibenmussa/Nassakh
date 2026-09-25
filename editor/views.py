"""Editor pages (PHASE5_SPEC §4–§5). Placeholders: the UI build replaces the templates.

- `editor:edit`   → `/books/<id>/editor/[?chapter=<cid>]`, the chapter editor (`bookEditor`)
- `editor:layout` → `/books/<id>/layout/[?chapter=<cid>]`, the stylesheet and the page preview (`bookLayout`)

Both render with `config` (`editor.services.page_config`), the Alpine component's start, as JSON.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render

from books.models import Book

from . import services


def _context(request: HttpRequest, book_id: int, page: str) -> dict:
    book = get_object_or_404(Book, pk=book_id)
    chapter_id = request.GET.get("chapter") or None
    config = services.page_config(book, request.user, chapter_id, page)
    return {"book": book, "config": config, "exists": config["exists"], "can_edit": config["canEdit"]}


@login_required
def edit(request: HttpRequest, book_id: int) -> HttpResponse:
    """The chapter editor (an empty state before the first assembly)."""
    return render(request, "editor/edit.html", _context(request, book_id, "editor"))


def _layout_initial(book: Book, config: dict) -> dict:
    """The layout page's first paint (`bookLayout`): the stylesheet payload, the book's cached preview and,
    when a chapter was asked for, that chapter's. Nothing is queued here (the component's first GET does),
    so opening the page never starts a render by itself."""
    from publishing import engine
    from publishing.preview import PreviewNotFound

    out: dict = {"stylesheet": services.stylesheet_payload(book), "preview": None, "chapterPreview": None}
    if not config["exists"]:
        return out
    try:
        out["preview"] = engine.preview_payload(book, "book", enqueue=False)
    except PreviewNotFound:
        return out
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
    context = _context(request, book_id, "layout")
    config = context["config"]
    config["initial"] = _layout_initial(context["book"], config)
    context["initial"] = config["initial"]
    context["first_page"] = _first_page(config["initial"], config.get("requestedChapter"))
    context["preview_pages"] = (config["initial"].get("preview") or {}).get("pages") or []
    return render(request, "editor/layout.html", context)
