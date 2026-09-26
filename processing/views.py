"""Function-based views of the processing app: the retired guides screen's address.

«ضبط الأدلة» is gone (D67): the layout is set in the dashboard's «التخطيط» mode. The old URL still
answers so bookmarks and older links land on the dashboard.
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_safe

from books.models import Book
from core.decorators import ROLE_EDITOR, role_required
from processing import services


@role_required(ROLE_EDITOR)
@require_safe
def guides(request: HttpRequest, book_id: int) -> HttpResponse:
    """Redirect to the dashboard: `?view=guides` once «المعالجة» started, `#sheet-N` for `?page=N`."""
    book = get_object_or_404(Book, pk=book_id)
    url = reverse("books:detail", args=[book.pk])
    if not book.awaits_ocr_start:
        url += "?view=guides"
    number = services.parse_page_number(request.GET.get("page"))
    if number:
        url += f"#sheet-{number}"
    return redirect(url)
