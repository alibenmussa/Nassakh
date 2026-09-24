"""Manuscript pages (PHASE4_SPEC §4.2).

- `assembly:manuscript` → the manuscript view of a book: the document server-rendered by
  `assembly.render`, the side panel's outline, warnings and stats, and the Alpine component's config
- `assembly:document`   → the same fragment on its own, swapped in place by the view after a re-run
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import render as render_template
from django.urls import reverse

from books.models import Book
from books.services import page_url_templates
from core.decorators import ROLE_EDITOR, ROLE_PROOFREADER, has_role

from . import render, services


def _config(book: Book, state: dict, can_edit: bool, can_review: bool, counts_text: str) -> dict:
    """What `manuscriptView` (static/src/js/manuscript.js) starts from."""
    urls = services.manuscript_urls(book)
    urls.update(
        sheets=reverse("api:book_sheets", args=[book.pk]),
        review=page_url_templates(book)["review"],  # `__n__` for the page number
        dashboard=reverse("books:detail", args=[book.pk]),
    )
    return {
        "bookId": book.pk,
        "title": book.title,
        "state": state,
        "urls": urls,
        "canEdit": can_edit,
        "canReview": can_review,
        "pageCount": book.pages.filter(is_excluded=False).count(),
        "countsText": counts_text,
    }


@login_required
def manuscript(request: HttpRequest, book_id: int) -> HttpResponse:
    """The manuscript view: never assembled, assembling, ready (fresh or stale) or failed."""
    book = get_object_or_404(Book, pk=book_id)
    state = services.manuscript_state(book)
    payload = services.manuscript_payload(book) if state["exists"] else None
    fragment = render.fragment_context(payload)
    can_edit = has_role(request.user, ROLE_EDITOR)
    can_review = has_role(request.user, ROLE_PROOFREADER, ROLE_EDITOR)
    context = {
        "book": book,
        "state": state,
        "can_edit": can_edit,
        "can_review": can_review,
        "assembling": bool(state.get("active")),
        "failed": bool(state.get("run") and state["run"].get("status") == "error"),
        "config": _config(book, state, can_edit, can_review, fragment.get("counts_text", "")),
        **fragment,
    }
    return render_template(request, "assembly/manuscript.html", context)


@login_required
def document(request: HttpRequest, book_id: int) -> HttpResponse:
    """The rendered document fragment (article, outline, warnings, stats and the meta JSON)."""
    book = get_object_or_404(Book, pk=book_id)
    payload = services.manuscript_payload(book)
    if payload is None:
        return HttpResponse("", status=404)
    context = {"book": book, **render.fragment_context(payload)}
    return render_template(request, "assembly/_document.html", context)
