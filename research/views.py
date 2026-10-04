"""«البحث والتحقق» (D107) and the page clips.

- `research:home`: the page — search, check a quotation, connect an AI assistant (access keys, D108). Every
  member; a user without an organisation sees no book (`books.access`) and is told so.
- `research:clip`: a page clip (`research.clips`) behind its signed token, served without a session (an AI
  client shows the link); a bad token answers 404, an expired one 410.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse

from books.access import has_organization

from . import clips, keys, services


@login_required
def research_page(request: HttpRequest) -> HttpResponse:
    """The page; its Alpine component reads `config`."""
    books = services.list_books(request.user).books
    config = {
        "urls": {
            "search": reverse("api:research_search"),
            "verify": reverse("api:research_verify"),
            "keys": reverse("api:research_keys"),
            "revoke": reverse("api:research_key_revoke", args=[0]).replace("/0/", "/__id__/", 1),
        },
        "mcp_url": keys.public_url(),
        "books": [{"id": book.id, "title": book.title} for book in books],
        "rate_limit": keys.rate_limit(),
        "clip_days": clips.link_days(),
    }
    context = {
        "books": books,
        "config": config,
        "has_books": bool(books),
        "member": has_organization(request.user),
    }
    return render(request, "research/research.html", context)


def clip(request: HttpRequest, token: str) -> HttpResponse:
    """The WebP clip a signed token names (no session needed)."""
    try:
        payload = clips.read_token(token)
    except signing.SignatureExpired:
        return HttpResponse("انتهت صلاحية رابط الصورة.", status=410, content_type="text/plain; charset=utf-8")
    except signing.BadSignature:
        raise Http404("الصورة غير موجودة.") from None
    try:
        data = clips.clip_bytes(payload)
    except clips.ClipError:
        raise Http404("الصورة غير موجودة.") from None
    response = HttpResponse(data, content_type="image/webp")
    response["Cache-Control"] = "private, max-age=3600"
    response["X-Robots-Tag"] = "noindex"
    return response
