"""Publishing pages (PHASE6_SPEC §6.5, D59): plain Django views (signed-out visitors go to the login page).

- `publishing:export` → `/books/<id>/export/`, the export page «الإخراج»: `templates/publishing/export.html`
  with `config`, the §3.2 object (`publishing.exports.page_payload`), the Alpine component's start.
- `publishing:export_download` → `/books/<id>/exports/<eid>/download/[?inline=1]`: a finished export's file
  under its Arabic name (`Content-Disposition` with `filename*=utf-8''…`, RFC 5987), `Cache-Control:
  private, no-cache`; 404 «الملف غير جاهز.» when the export is not done, its file is gone, or it belongs
  to another book.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render

from books.models import Book

from . import exports
from .exporters import FORMATS
from .models import Export


@login_required
def export_page(request: HttpRequest, book_id: int) -> HttpResponse:
    """The export page: readiness, one block per format, the history."""
    book = get_object_or_404(Book, pk=book_id)
    config = exports.page_payload(book, request.user)
    return render(request, "publishing/export.html", {"book": book, "config": config})


@login_required
def export_download(request: HttpRequest, book_id: int, export_id: int) -> FileResponse:
    """A finished export's file (an attachment, or shown in the browser with `?inline=1`)."""
    row = Export.objects.filter(pk=export_id, book_id=book_id, status=Export.Status.DONE).first()
    if row is None or not row.file or not default_storage.exists(row.file.name):
        raise Http404(exports.NOT_READY)
    info = FORMATS.get(row.format)
    inline = request.GET.get("inline") in ("1", "true")
    response = FileResponse(
        default_storage.open(row.file.name, "rb"),
        as_attachment=not inline,
        filename=row.filename or f"export-{row.pk}{info.extension if info else ''}",
        content_type=info.media_type if info is not None else None,
    )
    response["Cache-Control"] = "private, no-cache"
    return response
