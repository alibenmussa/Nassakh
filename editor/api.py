"""JSON endpoints of the editor (DRF function views, session auth, PHASE5_SPEC §3).

GETs need a login; every change needs an editor (`core.permissions.IsEditor`). Refusals answer
`{"detail": <Arabic>}`: 400 bad input (a stylesheet adds `errors: {field: message}`), 403 role, 404 no
manuscript / chapter / snapshot, 409 a chapter changed elsewhere (`{detail, id, version, content}`).

- GET  /api/books/<id>/chapters/                      → `services.chapter_summaries`
- GET  /api/books/<id>/chapters/<cid>/                → `services.chapter_document`
- PUT  /api/books/<id>/chapters/<cid>/                `{content, version}` → `services.save_chapter`
- POST /api/books/<id>/chapters/<cid>/reassemble/     `{replace_edited?}` → 202 run
                                                        (`assembly.services.run_payload`); over an edited text
                                                        without `replace_edited`: 409 `{detail, edited: true}`
- GET  /api/books/<id>/drift/                         → `{edited, pages, reasons, approvals, chapters,
                                                        chapter_pages}` (`services.drift_of`, D70, D78)
- GET  /api/books/<id>/review-changes/                → `{drift, plan, stale}` (`services.review_changes`)
- POST /api/books/<id>/review-changes/                `{pages?} | {fix}` → 202 `{plan_id, status}`; 409
                                                        `{detail, reassemble: true}` on an unedited text
- POST /api/books/<id>/review-changes/<pid>/apply/    `{choices, pages, keep_all?}` → the apply's answer; 409
                                                        `{detail, stale: true, pages}` when the plan is stale
- POST /api/books/<id>/to-footnote/                   `{block, content}` → `{content, note, removed}` (D74:
                                                        «تحويل إلى حاشية للعلامة (n)»; nothing is written)
- POST /api/books/<id>/find-replace/                  `{chapter, query, replacement, match_tashkeel,
                                                        fold_alef, whole_word, replace, version?}`
- POST /api/books/<id>/convert-digits/                `{chapter, style}` → `{changed, …}`
- GET  /api/books/<id>/snapshots/                     → list; POST `{label}` → 201 the snapshot
- POST /api/books/<id>/snapshots/<sid>/restore/       → `{version, snapshot, restored}`
- GET  /api/books/<id>/stylesheet/                    → `services.stylesheet_payload`
- PUT  /api/books/<id>/stylesheet/                    fields (any subset) [+ `chapter`] → the payload +
                                                        `preview` (the book render is queued); PATCH alike
- POST /api/books/<id>/images/                        multipart `file`, `purpose` → 201 (200: the same image
                                                        again) `services.upload_payload`; 413 / 422 / 400
                                                        `{detail, code}` (D80, `services.ImageRefused`)
- GET  /api/books/<id>/cover/                         → `publishing.cover.cover_payload` (the book page's
                                                        cover, rendered in the request when missing, D80)
- GET  /api/books/<id>/uncertain/                     → `uncertain.uncertain_words` (D47)
- POST /api/books/<id>/uncertain/accept|choose|type/  `{chapter, block, note?, start, end, word, version,
                                                        engine? | text?}` → `uncertain.resolve`

Edits answer `relayout` (the chapter's re-layout to poll, `publishing.relayout`) when one was asked for.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request
from rest_framework.response import Response

from books.models import Book
from core.decorators import ROLE_EDITOR, has_role
from core.permissions import IsEditor

from . import services, uncertain


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


def _replace(data: dict) -> bool:
    """Whether the request confirms that an edited chapter is replaced by its rebuild from review (D70)."""
    from assembly.services import _parse_bool

    return _parse_bool(data.get("replace_edited")) is True


def _refused(exc: services.EditorError) -> Response:
    if isinstance(exc, services.PlanStale):
        return Response(
            {"detail": str(exc), "stale": True, "pages": exc.pages}, status=status.HTTP_409_CONFLICT
        )
    if isinstance(exc, services.NotEdited):
        return Response({"detail": str(exc), "reassemble": True}, status=status.HTTP_409_CONFLICT)
    if isinstance(exc, services.EditorEdited):
        return Response({"detail": str(exc), "edited": True}, status=status.HTTP_409_CONFLICT)
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
    """Re-assemble one chapter from the reviewed pages (D41); the old chapter is kept as a snapshot. Over an
    edited text the request must confirm the replacement (`replace_edited`, D70), else 409."""
    from assembly.services import run_payload

    book = _book(book_id)
    try:
        run = services.reassemble_chapter(
            book, chapter_id, request.user, replace_edited=_replace(_data(request))
        )
    except services.EditorError as exc:
        return _refused(exc)
    return Response(run_payload(run), status=status.HTTP_202_ACCEPTED)


@api_view(["GET"])
def review_drift(request: Request, book_id: int) -> Response:
    """The live review drift (D70): the book page refreshes it on focus, on a return to the tab and on the
    review screen's message. Without a manuscript, no drift."""
    drift = services.drift_of(book_id)
    if drift is None:
        _book(book_id)  # 404 for a book that does not exist
        return Response(services.drift_payload(services.NO_DRIFT))
    return Response(drift)


@api_view(["GET", "POST"])
@permission_classes([EditorOrReadOnly])
def review_changes(request: Request, book_id: int) -> Response:
    """«تغييرات المراجعة» (D78): the drift and the newest plan (GET); POST starts a plan of the drift pages
    (`pages` to limit it, `fix` for a «تصحيح في كل الكتاب» batch) and answers 202 `{plan_id, status}`."""
    book = _book(book_id)
    if request.method == "GET":
        return Response(services.review_changes(book))
    data = _data(request)
    try:
        plan = services.plan_review_changes(book, request.user, data.get("pages"), data.get("fix"))
    except services.EditorError as exc:
        return _refused(exc)
    return Response({"plan_id": plan.pk, "status": plan.status}, status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
@permission_classes([IsEditor])
def review_changes_apply(request: Request, book_id: int, plan_id: int) -> Response:
    """Take a plan's changes (`{choices, pages}`), or keep the book's text for them (`keep_all`)."""
    from assembly.services import _parse_bool

    book = _book(book_id)
    data = _data(request)
    try:
        result = services.apply_review_changes(
            book,
            plan_id,
            data.get("choices"),
            data.get("pages"),
            request.user,
            keep_all=_parse_bool(data.get("keep_all")) is True,
        )
    except services.EditorError as exc:
        return _refused(exc)
    return Response(result)


@api_view(["POST"])
@permission_classes([IsEditor])
def to_footnote(request: Request, book_id: int) -> Response:
    """«تحويل إلى حاشية للعلامة (n)»: the chapter the book page holds with the paragraph `block` turned into
    the footnote of its call. Nothing is saved here; the page puts the answer in place (its undo) and
    saves."""
    from . import document as doc

    _book(book_id)
    data = _data(request)
    try:
        nodes = doc.clean_nodes(data.get("content"))
        new_nodes, note = doc.paragraph_to_footnote(nodes, str(data.get("block") or ""))
    except doc.DocumentError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(
        {"content": {"type": "doc", "content": new_nodes}, "note": note, "removed": str(data.get("block"))}
    )


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


@api_view(["GET", "PUT", "PATCH"])
@permission_classes([EditorOrReadOnly])
def stylesheet(request: Request, book_id: int) -> Response:
    """The book's stylesheet with its options (GET); PUT (or PATCH: the same subset) saves the posted fields
    and queues the renders (a change of the cover alone keeps the pages' hashes: nothing is rendered)."""
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


@api_view(["POST"])
@permission_classes([IsEditor])
@parser_classes([MultiPartParser, FormParser])
def book_images(request: Request, book_id: int) -> Response:
    """Upload an image of the book (D80: the cover's picture): checked, normalised and stored by content."""
    book = _book(book_id)
    try:
        row, created = services.upload_image(
            book, request.FILES.get("file"), request.data.get("purpose") or None, request.user
        )
    except services.ImageRefused as exc:
        return Response({"detail": str(exc), "code": exc.code}, status=exc.status)
    code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return Response(services.upload_payload(row), status=code)


@api_view(["GET"])
def cover(request: Request, book_id: int) -> Response:
    """The book page's cover (D80): `{mode, hash, image_1x, image_2x, width, height}`."""
    from publishing.cover import cover_payload

    return Response(cover_payload(_book(book_id)))


@api_view(["GET"])
def uncertain_words(request: Request, book_id: int) -> Response:
    """Every uncertain word left in the manuscript, with its page, context and readings (D47)."""
    try:
        return Response(uncertain.uncertain_words(_book(book_id)))
    except services.EditorError as exc:
        return _refused(exc)


def _resolve(request: Request, book_id: int, action: str) -> Response:
    book = _book(book_id)
    try:
        return Response(uncertain.resolve(book, action, _data(request), request.user))
    except services.EditorError as exc:
        return _refused(exc)


@api_view(["POST"])
@permission_classes([IsEditor])
def uncertain_accept(request: Request, book_id: int) -> Response:
    """Keep an uncertain word as it is (its mark goes)."""
    return _resolve(request, book_id, "accept")


@api_view(["POST"])
@permission_classes([IsEditor])
def uncertain_choose(request: Request, book_id: int) -> Response:
    """Replace an uncertain word by one of its readings (`engine`: primary, secondary or tess)."""
    return _resolve(request, book_id, "choose")


@api_view(["POST"])
@permission_classes([IsEditor])
def uncertain_type(request: Request, book_id: int) -> Response:
    """Replace an uncertain word by the typed `text`."""
    return _resolve(request, book_id, "type")
