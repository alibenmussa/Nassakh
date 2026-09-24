"""Function-based views of the processing app: the guides screen."""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from books.models import Book
from core.decorators import ROLE_EDITOR, role_required
from processing import services


@role_required(ROLE_EDITOR)
def guides(request: HttpRequest, book_id: int) -> HttpResponse:
    """Show the reference page with draggable guide lines (GET) or apply the posted guides (POST)."""
    book = get_object_or_404(Book, pk=book_id)

    if request.method == "POST":
        try:
            services.apply_guides(book, request.POST, request.user)
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
            return redirect(request.get_full_path())
        n_pages = book.pages.filter(is_excluded=False, preprocess__isnull=False).count()
        messages.success(
            request, f"طُبّقت الأدلة على {n_pages} صفحة. يُعاد التعرّف على النص للصفحات التي تغيّرت مناطقها."
        )
        return redirect("books:detail", book.pk)

    return render(request, "processing/guides.html", services.guides_context(book, request.GET.get("page")))
