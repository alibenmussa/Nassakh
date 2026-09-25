"""JSON endpoint of the page preview (DRF function view, session auth, PHASE5_SPEC §3).

- GET  /api/books/<id>/preview/?scope=book|chapter&chapter=<cid>  (login) → `engine.preview_payload`:
  the pages of the newest finished render, the state of the render of the current content; a content
  never rendered (with nothing running) is queued.
- POST /api/books/<id>/preview/  `{scope, chapter}`  (editor) → 202 the payload after queueing a render
  of the current content (again after an error).

404 without a manuscript or for an unknown chapter, 400 for an unknown scope.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from books.models import Book
from editor.api import EditorOrReadOnly

from . import engine
from .preview import PreviewNotFound

SCOPES = ("book", "chapter")


def _book(book_id: int) -> Book:
    book = Book.objects.filter(pk=book_id).first()
    if book is None:
        raise NotFound("الكتاب غير موجود.")
    return book


@api_view(["GET", "POST"])
@permission_classes([EditorOrReadOnly])
def preview(request: Request, book_id: int) -> Response:
    """The book's (or a chapter's) page preview; POST asks for a render of the current content."""
    book = _book(book_id)
    source = (
        request.query_params
        if request.method == "GET"
        else (request.data if isinstance(request.data, dict) else {})
    )
    scope = str(source.get("scope") or "book")
    chapter_id = str(source.get("chapter") or "") or None
    if scope not in SCOPES or (scope == "chapter" and not chapter_id):
        return Response({"detail": "نطاق المعاينة غير معروف."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        if request.method == "GET":
            return Response(engine.preview_payload(book, scope, chapter_id))
        engine.request_preview(book, scope, chapter_id, force=True)
        return Response(
            engine.preview_payload(book, scope, chapter_id, enqueue=False), status=status.HTTP_202_ACCEPTED
        )
    except PreviewNotFound as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
