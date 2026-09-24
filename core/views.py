"""Shared views: home redirect and login-protected media."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseNotModified
from django.shortcuts import redirect
from django.utils.http import http_date
from django.views.static import was_modified_since

from core.images import guess_content_type


@login_required
def home(request: HttpRequest) -> HttpResponse:
    """Send the signed-in user to the books list."""
    return redirect("books:list")


@login_required
def protected_media(request: HttpRequest, path: str) -> HttpResponse:
    """Serve a file from the default storage to signed-in users only.

    Derived images are rewritten in place when a stage is re-run, so the response asks the
    browser to revalidate (`no-cache`) and answers `304 Not Modified` from the file's mtime.
    """
    try:
        if not path or not default_storage.exists(path):
            raise Http404("file not found")
        modified = default_storage.get_modified_time(path)
        handle = default_storage.open(path, "rb")
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

    response = FileResponse(handle, content_type=guess_content_type(path))
    response["Last-Modified"] = http_date(mtime)
    response["Cache-Control"] = "private, no-cache"
    return response
