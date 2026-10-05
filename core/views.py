"""Shared views: home redirect, the health check and the media of the books a user may access."""

import posixpath

from django.contrib.auth.decorators import login_required
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseNotModified,
    JsonResponse,
)
from django.shortcuts import redirect
from django.utils.http import http_date
from django.views.decorators.http import require_safe
from django.views.static import was_modified_since

from core.images import guess_content_type


@login_required
def home(request: HttpRequest) -> HttpResponse:
    """Send the signed-in user to the books list."""
    return redirect("books:list")


@require_safe
def healthz(request: HttpRequest) -> JsonResponse:
    """The container health check (docker-compose.yml): no login, no database, nothing to compute."""
    return JsonResponse({"ok": True})


def _book_of_path(normal: str) -> int | None:
    """The book id of a normalised media path `books/<id>/…`, None for any other path."""
    parts = normal.split("/")
    if len(parts) >= 3 and parts[0] == "books" and parts[1].isascii() and parts[1].isdigit():
        return int(parts[1])
    return None


@login_required
def protected_media(request: HttpRequest, path: str) -> HttpResponse:
    """Serve a book's file from the default storage to the signed-in users who may access the book.

    Only `books/<id>/…` is served (the scans, the derived images, the covers, the previews, the layouts,
    the exports, the uploaded images), and only when the user may access book `<id>` (`books.access`,
    D102: another organisation's book answers 404 like a missing file). The path is normalised first and
    the normalised path is the one opened, so `books/1/../2/…` is book 2's. An organisation's files
    (`orgs/`: its licensed fonts, D98) are never served here, only to its members through
    `accounts:font_file`; nothing else lives under MEDIA_ROOT.

    Derived images are rewritten in place when a stage is re-run, so the response asks the
    browser to revalidate (`no-cache`) and answers `304 Not Modified` from the file's mtime.
    """
    from books.access import may_access

    normal = posixpath.normpath(str(path or "").replace("\\", "/")).lstrip("/")
    book_id = _book_of_path(normal)
    if book_id is None or not may_access(request.user, book_id):
        raise Http404("file not found")
    try:
        if not default_storage.exists(normal):
            raise Http404("file not found")
        modified = default_storage.get_modified_time(normal)
        handle = default_storage.open(normal, "rb")
    except (
        SuspiciousFileOperation,
        FileNotFoundError,
        IsADirectoryError,
        PermissionError,
        NotImplementedError,
    ):
        raise Http404("file not found") from None

    mtime = modified.timestamp()
    if not was_modified_since(request.META.get("HTTP_IF_MODIFIED_SINCE"), mtime):
        handle.close()
        return HttpResponseNotModified()

    response = FileResponse(handle, content_type=guess_content_type(normal))
    response["Last-Modified"] = http_date(mtime)
    response["Cache-Control"] = "private, no-cache"
    return response
