"""JSON endpoints of the processing app (DRF function views, session auth).

- POST /api/pages/<id>/guides/            the page's layout override: today's replace body, a merge
  (`{merge: true, set, unset, reset, stage}`) or an undo (`{replace, stage}`) (editor)
- GET  /api/books/<id>/guides/[?from&to]  `services.book_guides_state` (grid, filmstrip, chips)
- POST /api/books/<id>/guides/            the book guides: `{set, reset_overrides, from_page?, stage}`,
  `{reset: true, stage}` or `{undo, stage}` (editor)
- POST /api/books/<id>/guides/preview/    `services.preview_book_guides` (editor; writes nothing)

Guide writes answer 400 `{errors}` (Arabic validation), 422 `{errors}` for a locked page and 409
`{detail, started: true}` when the client's `stage` is no longer the book's (§3.11). Another organisation's
book or page answers 404 (`books.access`, D102).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request
from rest_framework.response import Response

from books.access import get_book_or_404, get_page_or_404
from books.models import Page
from core.decorators import ROLE_EDITOR, has_role
from core.permissions import IsEditor
from processing import services


class ReadOrEditor(BasePermission):
    """Signed-in users read; editors (and admins) write."""

    message = IsEditor.message

    def has_permission(self, request, view) -> bool:
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return has_role(request.user, ROLE_EDITOR)


def _body(request: Request) -> dict:
    return request.data if isinstance(request.data, dict) else {}


def _conflict() -> Response:
    return Response({"detail": services.STARTED_CONFLICT, "started": True}, status=status.HTTP_409_CONFLICT)


def _stage(data: dict) -> str | None:
    stage = data.get("stage")
    return stage if isinstance(stage, str) else None


def _page(request: Request, page_id: int) -> Page:
    """A page of a book the user may access (`books.access`, D102), else 404."""
    return get_page_or_404(request.user, pk=page_id)


@api_view(["POST"])
@permission_classes([IsEditor])
def page_guides_override(request: Request, page_id: int) -> Response:
    """Change the page's layout override: merge (`merge: true`), replace (an undo: `replace`) or
    today's replace body; answers today's fields plus the `guides` block and the `undo`."""
    page = _page(request, page_id)
    data = dict(_body(request))
    stage = _stage(data)
    try:
        if data.get("merge"):
            result = services.set_page_guides(
                page, data.get("set") or {}, data.get("unset") or (), bool(data.get("reset")), stage=stage
            )
            regions, enqueued, undo = result["regions"], result["ocr_enqueued"], result["undo"]
        else:
            before = page.guides_override
            if "replace" in data:
                replace = data.get("replace")
                body = dict(replace) if isinstance(replace, dict) else {"reset": True}
            else:
                body = {k: v for k, v in data.items() if k != "stage"}
            regions, enqueued = services.set_page_guides_override(page, body, stage=stage)
            undo = {"replace": before} if services.book_awaits_start(page.book_id) else None
    except ValidationError as exc:
        return Response({"errors": list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
    except services.GuidesConflict:
        return _conflict()
    except (services.ProcessingError, ValueError) as exc:  # ValueError: refused by books.run_stage
        return Response({"errors": [str(exc)]}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    return Response(services.page_guides_answer(page, regions, enqueued, undo))


def _page_bound(raw) -> int | None:
    text = str(raw or "").strip()
    return int(text) if text.isascii() and text.isdigit() else None


@api_view(["GET", "POST"])
@permission_classes([ReadOrEditor])
def book_guides(request: Request, book_id: int) -> Response:
    """GET: every page's bands, doubts and state, the book guides, the detection line and the chip
    counts (`?from&to` narrows the pages). POST: change, reset or restore the book guides."""
    book = get_book_or_404(request.user, book_id)
    if request.method == "GET":
        first = _page_bound(request.query_params.get("from"))
        last = _page_bound(request.query_params.get("to"))
        return Response(services.book_guides_state(book, first, last))
    data = _body(request)
    stage = _stage(data)
    try:
        if "undo" in data:
            answer = services.restore_book_guides(book, data.get("undo"), request.user, stage=stage)
        elif data.get("reset"):
            answer = services.reset_book_guides(book, request.user, stage=stage)
        else:
            answer = services.apply_book_guides(
                book,
                data.get("set") or {},
                data.get("reset_overrides") or (),
                request.user,
                from_page=data.get("from_page"),
                stage=stage,
            )
    except ValidationError as exc:
        return Response({"errors": list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
    except services.GuidesConflict:
        return _conflict()
    except (services.ProcessingError, ValueError) as exc:
        return Response({"errors": [str(exc)]}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    return Response(answer)


@api_view(["POST"])
@permission_classes([IsEditor])
def book_guides_preview(request: Request, book_id: int) -> Response:
    """What a book-guides change, or `reset: true`, would do (pages changed, cut, kept, locked, re-read,
    minutes)."""
    book = get_book_or_404(request.user, book_id)
    data = _body(request)
    try:
        answer = services.preview_book_guides(
            book,
            data.get("set") or {},
            data.get("reset_overrides") or (),
            from_page=data.get("from_page"),
            stage=_stage(data),
            reset=bool(data.get("reset")),  # «إزالة الضبط العام», as `book_guides` applies it
        )
    except ValidationError as exc:
        return Response({"errors": list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
    except services.GuidesConflict:
        return _conflict()
    return Response(answer)
