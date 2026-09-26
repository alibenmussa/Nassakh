"""JSON endpoints of the manuscript (DRF function views, session auth, PHASE4_SPEC §3).

GETs need a login; starting a run needs an editor (`core.permissions.IsEditor`), except the line
roles, which reviewers may set (`CanReview`). Refusals answer `{"detail": <Arabic>}`: 400 for bad
input, 403 for roles, 404 for another book's line or page (or a book without manuscript yet), 409 for
a run over a text edited on the book page without `replace_edited: true` (D49). Every endpoint that
starts a run answers 202 `services.run_payload`.

- POST /api/books/<id>/assemble/              `{footnote_numbering?, include_unreviewed?, strip_tatweel?}`
- GET  /api/books/<id>/manuscript/state/      → `services.manuscript_state`
- GET  /api/books/<id>/manuscript/            → `{document, warnings, stats, seams, version}`
- POST /api/books/<id>/manuscript/seams/      `{page, mode: "join" | "split" | "auto"}`
- POST /api/books/<id>/manuscript/roles/      `{line_ids: [...], role}` (`services.BLOCK_ROLES`: body,
  heading, subheading, verse «شعر», footnote «حاشية»)
- POST /api/books/<id>/manuscript/suggestions/ `{block_id, action: "dismiss"}`
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from books.models import Book
from core.permissions import CanReview, IsEditor

from . import services


def _data(request: Request) -> dict:
    """The request body as a dict (anything else reads as empty)."""
    return request.data if isinstance(request.data, dict) else {}


def _book(book_id: int) -> Book:
    book = Book.objects.filter(pk=book_id).first()
    if book is None:
        raise NotFound("الكتاب غير موجود.")
    return book


def _refused(exc: services.AssemblyError) -> Response:
    if isinstance(exc, services.AssemblyNotFound):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, services.AssemblyEdited):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_400_BAD_REQUEST
    return Response({"detail": str(exc), "edited": isinstance(exc, services.AssemblyEdited)}, status=code)


def _replace(data: dict) -> bool:
    """Whether the request confirms that an edited text is replaced (D49)."""
    return services._parse_bool(data.get("replace_edited")) is True


def _started(run) -> Response:
    return Response(services.run_payload(run), status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
@permission_classes([IsEditor])
def book_assemble(request: Request, book_id: int) -> Response:
    """Convert the book into its manuscript with the posted options (remembered for the book)."""
    book = _book(book_id)
    try:
        data = _data(request)
        run = services.start_assembly(book, request.user, data, replace_edited=_replace(data))
    except services.AssemblyError as exc:
        return _refused(exc)
    return _started(run)


@api_view(["GET"])
def manuscript_state(request: Request, book_id: int) -> Response:
    """Whether a manuscript exists, the latest run and whether the pages changed since."""
    return Response(services.manuscript_state(_book(book_id)))


@api_view(["GET"])
def manuscript(request: Request, book_id: int) -> Response:
    """The manuscript document with the warnings, stats and seams of the run that built it."""
    payload = services.manuscript_payload(_book(book_id))
    if payload is None:
        raise NotFound("لم يُجمَّع هذا الكتاب بعد.")
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsEditor])
def manuscript_seam(request: Request, book_id: int) -> Response:
    """Join or split at a page boundary (`auto` drops the override), then re-run."""
    book = _book(book_id)
    data = _data(request)
    try:
        run = services.set_seam_override(
            book, request.user, data.get("page"), str(data.get("mode") or ""), replace_edited=_replace(data)
        )
    except services.AssemblyError as exc:
        return _refused(exc)
    return _started(run)


@api_view(["POST"])
@permission_classes([CanReview])
def manuscript_roles(request: Request, book_id: int) -> Response:
    """Set the role of a block's lines (body / heading / subheading / verse / footnote) through review,
    then re-run."""
    book = _book(book_id)
    data = _data(request)
    try:
        lines, role = data.get("line_ids"), str(data.get("role") or "")
        run = services.set_block_roles(book, request.user, lines, role, replace_edited=_replace(data))
    except services.AssemblyError as exc:
        return _refused(exc)
    return _started(run)


@api_view(["POST"])
@permission_classes([IsEditor])
def manuscript_suggestion(request: Request, book_id: int) -> Response:
    """Dismiss a heading suggestion for good, then re-run."""
    book = _book(book_id)
    data = _data(request)
    try:
        run = services.dismiss_suggestion(
            book,
            request.user,
            str(data.get("block_id") or ""),
            str(data.get("action") or ""),
            replace_edited=_replace(data),
        )
    except services.AssemblyError as exc:
        return _refused(exc)
    return _started(run)
