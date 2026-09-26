"""JSON endpoints of the books app (DRF function views, session auth, login required).

- GET /api/books/<id>/progress/[?compact=1] → `services.book_progress` plus the page tiles (dashboard
  polling; `compact` gives the small tiles of `services.page_tile(compact=True)`)
- GET /api/pages/<id>/status/    → `services.page_status` (page detail / text panel polling)
- GET /api/books/<id>/text/      → `services.book_text` (copy the clean text of the whole book)
- GET /api/books/<id>/sheets/?from=<n>&to=<n>[&guides=1] → `services.book_sheets` (stacked view, ≤ 40
  pages; `guides=1` adds each page's «التخطيط» block and skips the lines)
"""

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from books import services
from books.models import Book, Page


@api_view(["GET"])
def book_progress(request: Request, book_id: int) -> Response:
    """Counts per status, percentage, active flag, attention count, book error and one entry per page."""
    book = get_object_or_404(Book, pk=book_id)
    compact = request.query_params.get("compact") in ("1", "true")
    return Response({**services.book_progress(book), "pages": services.page_tiles(book, compact=compact)})


@api_view(["GET"])
def page_status(request: Request, page_id: int) -> Response:
    """Status, text state, provisional/final text, flags and error of one page."""
    page = get_object_or_404(Page.objects.select_related("book"), pk=page_id)
    return Response(services.page_status(page))


@api_view(["GET"])
def book_text(request: Request, book_id: int) -> Response:
    """Clean text of every non-excluded page in order (`{"text", "pages"}`)."""
    book = get_object_or_404(Book, pk=book_id)
    return Response(services.book_text(book))


@api_view(["GET"])
def book_sheets(request: Request, book_id: int) -> Response:
    """Pages `from..to` (at most 40) with images, line boxes and text for the stacked-sheets view."""
    book = get_object_or_404(Book, pk=book_id)
    try:
        first, last = services.sheets_range(request.query_params.get("from"), request.query_params.get("to"))
    except services.SheetsRangeError as exc:
        return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    guides = request.query_params.get("guides") in ("1", "true")
    return Response(services.book_sheets(book, first, last, guides=guides))
