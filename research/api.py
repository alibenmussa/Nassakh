"""JSON endpoints of «البحث والتحقق» (D107, D108): DRF function views, session auth, signed-in users.

- POST /api/research/search              `{query, kind?, book_ids?, limit?, offset?}` → `SearchResult`
- POST /api/research/verify              `{quote, book_id?, attributed_to?}` → `VerifyResult`
- GET  /api/research/books               → `BooksResult` (the account's books)
- GET  /api/research/passages/<id>/      `?context=` → `Passage`
- GET  /api/research/keys                → `{keys, mcp_url}` (the user's keys, never the keys themselves)
- POST /api/research/keys                `{name}` → 201 `{key, secret, mcp_url}` (`secret`: shown this once)
- POST /api/research/keys/<id>/revoke    → `{key}`

The same services as the MCP tools (`research.services`), scoped to the user's books (`books.access`): a
book of another organisation answers 404 like a missing one. Links in the answers are absolute (this
request's host). Refusals answer `{"detail": <Arabic>}`.
"""

from __future__ import annotations

from django.http import Http404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from . import keys, services

QUERY_MAX = 500
QUOTE_MAX = 4000


def _data(request: Request) -> dict:
    return request.data if isinstance(request.data, dict) else {}


def _base(request: Request) -> str:
    return request.build_absolute_uri("/").rstrip("/")


def _int(value, message: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError({"detail": message}) from None


def _book_ids(value) -> list[int] | None:
    if value in (None, "", []):
        return None
    if not isinstance(value, list):
        value = [value]
    return [_int(item, "رقم الكتاب غير صالح.") for item in value if item not in (None, "")] or None


@api_view(["POST"])
def search(request: Request) -> Response:
    """Search the account's books (`services.search`)."""
    data = _data(request)
    query = str(data.get("query") or "").strip()
    if not query:
        return Response({"detail": "اكتب كلمة للبحث."}, status=status.HTTP_400_BAD_REQUEST)
    if len(query) > QUERY_MAX:
        return Response({"detail": "نص البحث أطول من المسموح."}, status=status.HTTP_400_BAD_REQUEST)
    result = services.search(
        request.user,
        query,
        book_ids=_book_ids(data.get("book_ids")),
        kind=str(data.get("kind") or "all"),
        limit=_int(data.get("limit"), "العدد غير صالح.") or 20,
        offset=_int(data.get("offset"), "العدد غير صالح.") or 0,
        base_url=_base(request),
    )
    return Response(result.model_dump(mode="json"))


@api_view(["POST"])
def verify(request: Request) -> Response:
    """Check a quotation (`services.verify_quote`)."""
    data = _data(request)
    quote = str(data.get("quote") or "").strip()
    if not quote:
        return Response({"detail": "الصق النص المنقول أولًا."}, status=status.HTTP_400_BAD_REQUEST)
    if len(quote) > QUOTE_MAX:
        return Response({"detail": "النص أطول من المسموح."}, status=status.HTTP_400_BAD_REQUEST)
    result = services.verify_quote(
        request.user,
        quote,
        book_id=_int(data.get("book_id"), "رقم الكتاب غير صالح."),
        attributed_to=str(data.get("attributed_to") or "unknown"),
        base_url=_base(request),
    )
    return Response(result.model_dump(mode="json"))


@api_view(["GET"])
def books(request: Request) -> Response:
    """The account's books (`services.list_books`)."""
    return Response(services.list_books(request.user).model_dump(mode="json"))


@api_view(["GET"])
def passage(request: Request, passage_id: str) -> Response:
    """A passage with its context (`services.get_passage`)."""
    context = _int(request.query_params.get("context"), "العدد غير صالح.")
    result = services.get_passage(
        request.user, passage_id, context=2 if context is None else context, base_url=_base(request)
    )
    return Response(result.model_dump(mode="json"))


@api_view(["GET", "POST"])
def access_keys(request: Request) -> Response:
    """The user's access keys; POST makes one (its secret is in this answer only)."""
    if request.method == "POST":
        name = str(_data(request).get("name") or "").strip()
        if not name:
            return Response({"detail": "سمِّ المفتاح أولًا."}, status=status.HTTP_400_BAD_REQUEST)
        key, secret = keys.make_key(request.user, name)
        payload = {"key": keys.key_item(key), "secret": secret, "mcp_url": keys.public_url()}
        return Response(payload, status=status.HTTP_201_CREATED)
    return Response({"keys": keys.keys_of(request.user), "mcp_url": keys.public_url()})


@api_view(["POST"])
def revoke_key(request: Request, key_id: int) -> Response:
    """Revoke one of the user's keys (another user's answers 404)."""
    try:
        key = keys.revoke_key(request.user, key_id)
    except Http404 as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    return Response({"key": keys.key_item(key)})
