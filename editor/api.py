"""JSON endpoints of the editor (DRF function views, session auth, PHASE5_SPEC §3).

GETs need a login; every change needs an editor (`core.permissions.IsEditor`). Refusals answer
`{"detail": <Arabic>}`: 400 bad input (a stylesheet adds `errors: {field: message}`), 403 role, 404 no
manuscript / chapter / snapshot, 409 a chapter changed elsewhere (`{detail, id, version, content}`).

- GET  /api/books/<id>/chapters/                      → `services.chapter_summaries`
- GET  /api/books/<id>/chapters/<cid>/                → `services.chapter_document`
- PUT  /api/books/<id>/chapters/<cid>/                `{content, version}` → `services.save_chapter`
- POST /api/books/<id>/chapters/<cid>/reassemble/     → 202 run (`assembly.services.run_payload`)
- POST /api/books/<id>/find-replace/                  `{chapter, query, replacement, match_tashkeel,
                                                        fold_alef, whole_word, replace, version?}`
- POST /api/books/<id>/convert-digits/                `{chapter, style}` → `{changed, …}`
- GET  /api/books/<id>/snapshots/                     → list; POST `{label}` → 201 the snapshot
- POST /api/books/<id>/snapshots/<sid>/restore/       → `{version, snapshot, restored}`
- GET  /api/books/<id>/stylesheet/                    → `services.stylesheet_payload`
- PUT  /api/books/<id>/stylesheet/                    fields (any subset) [+ `chapter`] → the payload +
                                                        `preview` (the book render is queued)
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request
from rest_framework.response import Response

from books.models import Book
from core.decorators import ROLE_EDITOR, has_role
from core.permissions import IsEditor

from . import services


class EditorOrReadOnly(BasePermission):
    """Reads for signed-in users, changes for editors (and admins)."""

    message = IsEditor.message

    def has_permission(self, request, view) -> bool:
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return has_role(request.user, ROLE_EDITOR)


def _data(request: Request) -> dict:
    return request.data if isinstance(request.data, dict) else {}


def _book(book_id: int) -> Book:
    book = Book.objects.filter(pk=book_id).first()
    if book is None:
        raise NotFound("الكتاب غير موجود.")
    return book


def _refused(exc: services.EditorError) -> Response:
    if isinstance(exc, services.ChapterConflict):
        return Response(
            {"detail": str(exc), "id": exc.chapter_id, "version": exc.version, "content": exc.content},
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, services.StyleSheetError):
        return Response({"detail": str(exc), "errors": exc.errors}, status=status.HTTP_400_BAD_REQUEST)
    code = (
        status.HTTP_404_NOT_FOUND if isinstance(exc, services.EditorNotFound) else status.HTTP_400_BAD_REQUEST
    )
    return Response({"detail": str(exc)}, status=code)


@api_view(["GET"])
def chapters(request: Request, book_id: int) -> Response:
    """The chapters of the manuscript with their versions, word counts, pages and drift."""
    try:
        return Response(services.chapter_summaries(_book(book_id)))
    except services.EditorError as exc:
        return _refused(exc)


@api_view(["GET", "PUT"])
@permission_classes([EditorOrReadOnly])
def chapter(request: Request, book_id: int, chapter_id: str) -> Response:
    """One chapter for the editor (GET), or its autosave (PUT, version-checked)."""
    book = _book(book_id)
    try:
        if request.method == "GET":
            return Response(services.chapter_document(book, chapter_id))
        data = _data(request)
        version = data.get("version")
        return Response(
            services.save_chapter(
                book, chapter_id, data.get("content"), str(version) if version else "", request.user
            )
        )
    except services.EditorError as exc:
        return _refused(exc)


@api_view(["POST"])
@permission_classes([IsEditor])
def chapter_reassemble(request: Request, book_id: int, chapter_id: str) -> Response:
    """Re-assemble one chapter from the reviewed pages (D41); the old chapter is kept as a snapshot."""
    from assembly.services import run_payload

    book = _book(book_id)
    try:
        run = services.reassemble_chapter(book, chapter_id, request.user)
    except services.EditorError as exc:
        return _refused(exc)
    return Response(run_payload(run), status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
@permission_classes([IsEditor])
def find_replace(request: Request, book_id: int) -> Response:
    """Find, or replace all, in one chapter or the whole book."""
    book = _book(book_id)
    data = _data(request)
    try:
        result = services.find_replace(
            book,
            data.get("chapter") or None,
            data.get("query"),
            data.get("replacement") or "",
            data,
            replace=data.get("replace", False),
            version=data.get("version"),
            user=request.user,
        )
    except services.EditorError as exc:
        return _refused(exc)
    return Response(result)


@api_view(["POST"])
@permission_classes([IsEditor])
def convert_digits(request: Request, book_id: int) -> Response:
    """Convert the digits of one chapter (or of the book) to Western or Arabic-Indic."""
    book = _book(book_id)
    data = _data(request)
    try:
        result = services.convert_digits(
            book, data.get("chapter") or None, str(data.get("style") or ""), request.user
        )
    except services.EditorError as exc:
        return _refused(exc)
    return Response(result)


@api_view(["GET", "POST"])
@permission_classes([EditorOrReadOnly])
def snapshots(request: Request, book_id: int) -> Response:
    """The manuscript's snapshots (GET), or a new one taken now (POST `{label}`)."""
    book = _book(book_id)
    try:
        if request.method == "GET":
            return Response(services.snapshots(book))
        created = services.snapshot(book, str(_data(request).get("label") or ""), "manual", request.user)
    except services.EditorError as exc:
        return _refused(exc)
    return Response(created, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsEditor])
def snapshot_restore(request: Request, book_id: int, snapshot_id: int) -> Response:
    """Put a snapshot back (the current text is kept as a snapshot first)."""
    book = _book(book_id)
    try:
        return Response(services.restore(book, snapshot_id, request.user))
    except services.EditorError as exc:
        return _refused(exc)


@api_view(["GET", "PUT"])
@permission_classes([EditorOrReadOnly])
def stylesheet(request: Request, book_id: int) -> Response:
    """The book's stylesheet with its options (GET); PUT saves the posted fields and queues the renders."""
    from publishing import engine
    from publishing.preview import PreviewNotFound

    book = _book(book_id)
    if request.method == "GET":
        return Response(services.stylesheet_payload(book))
    data = _data(request)
    try:
        services.update_stylesheet(book, data, request.user)
    except services.EditorError as exc:
        return _refused(exc)
    chapter_id = str(data.get("chapter") or request.query_params.get("chapter") or "") or None
    engine.request_preview(book, "book")
    if chapter_id:
        engine.request_preview(book, "chapter", chapter_id)
    payload = services.stylesheet_payload(book)
    try:
        payload["preview"] = engine.preview_payload(book, "book", enqueue=False)
    except PreviewNotFound:
        payload["preview"] = None
    return Response(payload)
