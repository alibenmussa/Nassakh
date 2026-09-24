"""JSON endpoints of the review screen (DRF function views, session auth, CSRF as in Phase 2).

GETs need a login; POSTs need a reviewer role (`core.permissions.CanReview`). Refused actions
answer 400 `{"message": <Arabic>}`; an approval blocked by unresolved words answers 409
`{"unresolved", "message"}`. Line actions may carry what the client saw: `t` (and `t_next` for a
merge), the word at `index`, and `v`, the line's version from its payload, for whole-line actions.
When the line no longer matches (changed in another tab, or an earlier queued action failed) the
answer is 409 `{"message", "line"}` with the line as it is now; without them nothing is checked.

- GET  /api/pages/<id>/review/      → `services.review_payload`
- POST /api/lines/<id>/resolve/     `{index, choice, text?, t?}` → `{line, counts, page}`
- POST /api/lines/<id>/edit/        `{text, v?}` → `{line, counts}`
- POST /api/lines/<id>/delete/      `{v?}` → `{deleted_id, counts}`
- POST /api/lines/<id>/merge/       `{index, t?, t_next?}` → `{line, counts}`
- POST /api/lines/<id>/delete-word/ `{index, t?}` → `{line, counts}` | `{deleted_id, counts}`
- POST /api/lines/<id>/role/        `{role, v?}` → `{line}`
- POST /api/pages/<id>/lines/       `{after, text}` → `{line, lines, counts}`
- POST /api/pages/<id>/undo/        → review payload
- POST /api/pages/<id>/approve/     `{force}` → `{status, next_review_url, ...}` | 409
- POST /api/pages/<id>/reopen/      → review payload
- GET  /api/books/<id>/filmstrip/   → `{"pages": services.filmstrip}`
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from rest_framework.response import Response

from books.models import Book, Page
from core.permissions import CanReview
from ocr.models import Line

from . import services

TRUE_VALUES = frozenset({True, "1", "true", "True", "yes", "on"})


def _data(request: Request) -> dict:
    """The request body as a dict (anything else reads as empty)."""
    return request.data if isinstance(request.data, dict) else {}


def _error(exc: services.ReviewError) -> Response:
    """400 / 409 answer of a refused review action."""
    if isinstance(exc, services.ReviewBlocked):
        return Response({"unresolved": exc.unresolved, "message": str(exc)}, status=status.HTTP_409_CONFLICT)
    if isinstance(exc, services.ReviewConflict):
        return Response(
            {"message": str(exc), "line": services.line_item(exc.line)}, status=status.HTTP_409_CONFLICT
        )
    return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def _seen(data: dict, key: str) -> str | None:
    """What the client saw (`t`, `t_next`, `v`) as a string; None when it did not send it."""
    value = data.get(key)
    return None if value is None or isinstance(value, dict | list) else str(value)


def _line(line_id: int) -> Line:
    return get_object_or_404(Line.objects.select_related("page"), pk=line_id)


def _page(page_id: int) -> Page:
    return get_object_or_404(Page.objects.select_related("book"), pk=page_id)


@api_view(["GET"])
def page_review(request: Request, page_id: int) -> Response:
    """The review payload of one page."""
    return Response(services.review_payload(_page(page_id), request.user))


@api_view(["POST"])
@permission_classes([CanReview])
def line_resolve(request: Request, line_id: int) -> Response:
    """Resolve one uncertain word with a reading (`primary | secondary | tess | typed`)."""
    line = _line(line_id)
    data = _data(request)
    try:
        line = services.resolve_token(
            line,
            data.get("index"),
            str(data.get("choice") or ""),
            data.get("text"),
            request.user,
            expected=_seen(data, "t"),
        )
    except services.ReviewError as exc:
        return _error(exc)
    page = _page(line.page_id)
    return Response(
        {
            "line": services.line_item(line),
            "counts": services.mutation_counts(page, line),
            "page": services.page_item(page),
        }
    )


@api_view(["POST"])
@permission_classes([CanReview])
def line_edit(request: Request, line_id: int) -> Response:
    """Replace the text of a whole line."""
    line = _line(line_id)
    data = _data(request)
    try:
        line = services.edit_line(line, str(data.get("text") or ""), request.user, version=_seen(data, "v"))
    except services.ReviewError as exc:
        return _error(exc)
    return Response(
        {"line": services.line_item(line), "counts": services.mutation_counts(_page(line.page_id), line)}
    )


@api_view(["POST"])
@permission_classes([CanReview])
def line_role(request: Request, line_id: int) -> Response:
    """Mark a line as body text, a main heading or a subheading (`{role}`)."""
    line = _line(line_id)
    data = _data(request)
    try:
        line = services.set_line_role(
            line, str(data.get("role") or ""), request.user, version=_seen(data, "v")
        )
    except services.ReviewError as exc:
        return _error(exc)
    return Response({"line": services.line_item(line)})


@api_view(["POST"])
@permission_classes([CanReview])
def line_merge(request: Request, line_id: int) -> Response:
    """Join word `index` with the word after it into one word (a name the models split in two)."""
    line = _line(line_id)
    data = _data(request)
    try:
        line = services.merge_tokens(
            line,
            data.get("index"),
            request.user,
            expected=_seen(data, "t"),
            expected_next=_seen(data, "t_next"),
        )
    except services.ReviewError as exc:
        return _error(exc)
    return Response(
        {"line": services.line_item(line), "counts": services.mutation_counts(_page(line.page_id), line)}
    )


@api_view(["POST"])
@permission_classes([CanReview])
def line_delete_word(request: Request, line_id: int) -> Response:
    """Remove one stray word; removing a line's only word deletes the line (`deleted_id`)."""
    line = _line(line_id)
    page_id = line.page_id
    data = _data(request)
    try:
        result = services.delete_token(line, data.get("index"), request.user, expected=_seen(data, "t"))
    except services.ReviewError as exc:
        return _error(exc)
    page = _page(page_id)
    if result["line"] is None:
        return Response({"deleted_id": result["deleted_line_id"], "counts": services.mutation_counts(page)})
    return Response(
        {"line": services.line_item(result["line"]), "counts": services.mutation_counts(page, result["line"])}
    )


@api_view(["POST"])
@permission_classes([CanReview])
def line_delete(request: Request, line_id: int) -> Response:
    """Delete a line (undo brings it back)."""
    line = _line(line_id)
    page_id = line.page_id
    try:
        deleted = services.delete_line(line, request.user, version=_seen(_data(request), "v"))
    except services.ReviewError as exc:
        return _error(exc)
    return Response({"deleted_id": deleted, "counts": services.mutation_counts(_page(page_id))})


@api_view(["POST"])
@permission_classes([CanReview])
def page_lines(request: Request, page_id: int) -> Response:
    """Insert a typed line below `after` (a line id; null inserts at the top)."""
    page = _page(page_id)
    data = _data(request)
    try:
        line = services.insert_line(page, data.get("after"), str(data.get("text") or ""), request.user)
    except services.ReviewError as exc:
        return _error(exc)
    page = _page(page_id)
    order = list(page.lines.order_by("order", "id").values("id", "order"))
    return Response(
        {"line": services.line_item(line), "lines": order, "counts": services.mutation_counts(page, line)},
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([CanReview])
def page_undo(request: Request, page_id: int) -> Response:
    """Undo the newest review action of the page; answers the new review payload."""
    try:
        payload = services.undo_last(_page(page_id), request.user)
    except services.ReviewError as exc:
        return _error(exc)
    return Response(payload)


@api_view(["POST"])
@permission_classes([CanReview])
def page_approve(request: Request, page_id: int) -> Response:
    """Approve a page (`force: true` approves with unresolved words left)."""
    raw = _data(request).get("force")
    force = isinstance(raw, bool | int | float | str) and raw in TRUE_VALUES  # a list or dict is not hashable
    try:
        result = services.approve_page(_page(page_id), request.user, force=force)
    except services.ReviewError as exc:
        return _error(exc)
    return Response(result)


@api_view(["POST"])
@permission_classes([CanReview])
def page_reopen(request: Request, page_id: int) -> Response:
    """Take an approved page back to review; answers the new review payload."""
    try:
        services.reopen_page(_page(page_id), request.user)
    except services.ReviewError as exc:
        return _error(exc)
    return Response(services.review_payload(_page(page_id), request.user))


@api_view(["GET"])
def book_filmstrip(request: Request, book_id: int) -> Response:
    """Thumbnails and review state of every non-excluded page of a book."""
    book = get_object_or_404(Book, pk=book_id)
    return Response({"book_id": book.pk, "pages": services.filmstrip(book)})
