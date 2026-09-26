"""Review screens: one page (`review:page`) and «the next page needing review» (`review:next`).

Both take review's origin (D76, PHASE7_SPEC §5.3): `?from=book|manuscript|export`, with `at` (the book page's
page) and `block` (a block id); `services.parse_origin` validates it, the payload's `nav.back` leads there and
every review URL carries it. Anything else in them is ignored.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from books.models import Book, Page

from . import services


def origin_of(request: HttpRequest) -> dict | None:
    """Review's origin from the query string (`services.parse_origin`)."""
    return services.parse_origin(request.GET.get("from"), request.GET.get("at"), request.GET.get("block"))


@login_required
def review_page(request: HttpRequest, book_id: int, number: int) -> HttpResponse:
    """Review screen of one page; the Alpine component reads `config` (`review_payload`)."""
    from books.services import StageFacts, book_stages

    page = get_object_or_404(Page.objects.select_related("book"), book_id=book_id, number=number)
    facts = StageFacts(page.book)
    config = services.review_payload(page, request.user, origin_of(request), facts)
    context = {
        "book": page.book,
        "page": page,
        "prev_url": config["nav"]["prev_url"],
        "next_url": config["nav"]["next_url"],
        "back": config["nav"]["back"],
        "stage_steps": book_stages(page.book, "review", facts),  # the stage bar (D76)
        "config": config,
    }
    return render(request, "review/review.html", context)


def _to_page(book_id: int, number: int, origin: dict | None) -> HttpResponse:
    return HttpResponseRedirect(services.with_origin(reverse("review:page", args=[book_id, number]), origin))


@login_required
def review_next(request: HttpRequest, book_id: int) -> HttpResponse:
    """Redirect to the next page waiting for review (after `?after=<n>`), or back to the dashboard.

    When the page the reviewer came from is the only one still waiting, they stay on it. The origin comes
    along.
    """
    book = get_object_or_404(Book, pk=book_id)
    origin = origin_of(request)
    raw = request.GET.get("after", "")
    after = int(raw) if raw.isascii() and raw.isdigit() else None
    page = services.next_page_to_review(book, after_number=after)
    if page is None:
        current = services.pending_page(book, after)
        if current is not None:
            messages.info(request, "هذه آخر صفحة بانتظار المراجعة")
            return _to_page(book.pk, current.number, origin)
        messages.info(request, "لا صفحات بانتظار المراجعة")
        return redirect("books:detail", book.pk)
    return _to_page(book.pk, page.number, origin)
