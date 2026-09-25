"""JSON endpoints of the page preview and the live pages (DRF function views, session auth, PHASE5_SPEC
§3, §9.1).

- GET  /api/books/<id>/preview/?scope=book|chapter&chapter=<cid>  (login) → `engine.preview_payload`:
  the pages of the newest finished render, the state of the render of the current content (with
  `layout_url`, the live layout's summary and the page checks); a content never rendered (with nothing
  running) is queued.
- POST /api/books/<id>/preview/  `{scope, chapter}`  (editor) → 202 the payload after queueing a render
  of the current content (again after an error).
- GET  /api/books/<id>/preview/layout/?from=&to=[&scope=chapter&chapter=<cid> | &render=<rid>]  (login)
  → `relayout.layout_payload`: a page range of the live layout (of a chapter's newest render, of one
  render); 404 before the first layout.
- POST /api/books/<id>/chapters/<cid>/relayout/  `{version?}`  (editor) → the fast re-layout of the
  chapter: 202 `relayout.relayout_payload` to poll (200 when done at once or when nothing had to move).
- GET  /api/books/<id>/relayout/<rid>/?wait=<seconds ≤ 5>  (login) → the re-layout's state; with `wait`
  the answer waits until it is done (a long poll).

404 without a manuscript or for an unknown chapter, 400 for an unknown scope or a bad page range.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from books.models import Book
from editor.api import EditorOrReadOnly

from . import engine, relayout
from .models import PreviewRender
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


def _int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except ValueError:
        raise ValueError from None


@api_view(["GET"])
def preview_layout(request: Request, book_id: int) -> Response:
    """A page range of the book's live layout (or of a chapter's render, or of one render)."""
    book = _book(book_id)
    params = request.query_params
    scope = str(params.get("scope") or "book")
    chapter_id = str(params.get("chapter") or "") or None
    if scope not in SCOPES or (scope == "chapter" and not chapter_id):
        return Response({"detail": "نطاق المعاينة غير معروف."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        first, last, render_id = _int(params.get("from")), _int(params.get("to")), _int(params.get("render"))
    except ValueError:
        return Response({"detail": "نطاق الصفحات غير صالح."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        return Response(
            relayout.layout_payload(book, scope, chapter_id, first=first, last=last, render_id=render_id)
        )
    except relayout.LayoutNotFound as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)


@api_view(["POST"])
@permission_classes([EditorOrReadOnly])
def relayout_chapter(request: Request, book_id: int, chapter_id: str) -> Response:
    """Ask for the fast re-layout of a chapter (after an edit); poll the answer's `url`."""
    book = _book(book_id)
    data = request.data if isinstance(request.data, dict) else {}
    try:
        version = _int(data.get("version"))
    except ValueError:
        version = None
    try:
        row = relayout.request_relayout(book, chapter_id, version)
    except relayout.LayoutNotFound as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    payload = relayout.relayout_payload(row)
    active = row is not None and row.status in (PreviewRender.Status.QUEUED, PreviewRender.Status.RUNNING)
    return Response(payload, status=status.HTTP_202_ACCEPTED if active else status.HTTP_200_OK)


@api_view(["GET"])
def relayout_status(request: Request, book_id: int, render_id: int) -> Response:
    """The state of a re-layout (its result and new pages once done); `?wait=` long-polls."""
    row = PreviewRender.objects.filter(pk=render_id, book_id=book_id, kind=PreviewRender.Kind.LAYOUT).first()
    if row is None:
        raise NotFound("إعادة الترتيب غير موجودة.")
    try:
        wait = float(request.query_params.get("wait") or 0)
    except ValueError:
        wait = 0.0
    if wait > 0:
        row = relayout.wait_for(row, wait)
    return Response(relayout.relayout_payload(row))
