"""Review screens: one page (`review:page`) and «the next page needing review» (`review:next`)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from books.models import Book, Page

from . import services


@login_required
def review_page(request: HttpRequest, book_id: int, number: int) -> HttpResponse:
    """Review screen of one page; the Alpine component reads `config` (`review_payload`)."""
    page = get_object_or_404(Page.objects.select_related("book"), book_id=book_id, number=number)
    config = services.review_payload(page, request.user)
    context = {
        "book": page.book,
        "page": page,
        "prev_url": config["nav"]["prev_url"],
        "next_url": config["nav"]["next_url"],
        "config": config,
    }
    return render(request, "review/review.html", context)


@login_required
def review_next(request: HttpRequest, book_id: int) -> HttpResponse:
    """Redirect to the next page waiting for review (after `?after=<n>`), or back to the dashboard."""
    book = get_object_or_404(Book, pk=book_id)
    raw = request.GET.get("after", "")
    after = int(raw) if raw.isascii() and raw.isdigit() else None
    page = services.next_page_to_review(book, after_number=after)
    if page is None:
        messages.info(request, "لا صفحات بانتظار المراجعة")
        return redirect("books:detail", book.pk)
    return redirect("review:page", book.pk, page.number)
