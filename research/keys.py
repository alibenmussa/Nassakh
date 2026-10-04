"""Access keys of the MCP server and its call log (D108).

A key is `nsk_` + 32 random bytes (URL-safe base64), shown once by `make_key`; the database keeps its
SHA-256 and its first characters. `resolve_key` turns a presented key into its `AccessKey` (None for an
unknown or revoked key, or a deactivated user): the MCP server's token verifier calls it on every request,
so a revoked key stops at once. Each tool call is logged (`log_call`) and a key may make
`NASSAKH["MCP_RATE_LIMIT"]` calls a minute (`rate_limited`, counted from the log, so it holds across server
processes). Keys are made and revoked by their user on the «البحث والتحقق» page (`research.api`).

A key is presented as `Authorization: Bearer nsk_…`, or — for the clients that take only a URL (Claude's
and ChatGPT's custom connectors) — inside the secret connection URL `<public>/mcp/k/<key>` (`secret_url`;
`research.mcp_server.KeyInPath` moves it into the header).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db.models import Count, Q
from django.http import Http404
from django.utils import timezone

from .models import AccessKey, ToolCall

KEY_PREFIX = "nsk_"
KEY_BYTES = 32
SHOWN_PREFIX = 12  # «nsk_» and the first 8 characters, to tell keys apart
NAME_MAX = 80
TOUCH_EVERY = timedelta(minutes=1)  # `last_used_at` is written at most this often
KEY_NOT_FOUND = "المفتاح غير موجود."
KEY_SEGMENT = "k"  # `/mcp/k/<key>`: the key as a path segment, for URL-only clients
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_key(user, name: str) -> tuple[AccessKey, str]:
    """A new key of `user` named `name`: the row and the key itself (never stored, shown once)."""
    raw = KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)
    clean = " ".join(str(name or "").split())[:NAME_MAX] or "مفتاح"
    key = AccessKey.objects.create(user=user, name=clean, prefix=raw[:SHOWN_PREFIX], key_hash=hash_key(raw))
    return key, raw


def resolve_key(raw: str | None) -> AccessKey | None:
    """The live key `raw` is (with its user), None otherwise; notes when it was last used."""
    raw = str(raw or "").strip()
    if not raw.startswith(KEY_PREFIX) or len(raw) > 200:
        return None
    key = (
        AccessKey.objects.select_related("user")
        .filter(key_hash=hash_key(raw), revoked_at__isnull=True, user__is_active=True)
        .first()
    )
    if key is None:
        return None
    now = timezone.now()
    if key.last_used_at is None or now - key.last_used_at >= TOUCH_EVERY:
        AccessKey.objects.filter(pk=key.pk).update(last_used_at=now)
        key.last_used_at = now
    return key


def revoke_key(user, key_id: int) -> AccessKey:
    """Revoke one of the user's keys (another user's key answers 404 like a missing one)."""
    key = AccessKey.objects.filter(pk=key_id, user=user).first()
    if key is None:
        raise Http404(KEY_NOT_FOUND)
    if key.revoked_at is None:
        key.revoked_at = timezone.now()
        key.save(update_fields=["revoked_at"])
    return key


def site_url() -> str:
    """The site's public address, the start of the clip and review links an MCP client gets (`SITE_URL`;
    the local Django server when it is empty)."""
    return str(getattr(settings, "SITE_URL", "") or "http://localhost:8000").rstrip("/")


def public_url() -> str:
    """The MCP endpoint clients connect to: `NASSAKH["MCP_PUBLIC_URL"]`, else the local server's `/mcp`."""
    value = settings.NASSAKH.get("MCP_PUBLIC_URL") or ""
    return str(value or f"http://localhost:{settings.NASSAKH.get('MCP_PORT', 8001)}/mcp").rstrip("/")


def secret_url(raw: str) -> str:
    """The connection URL that carries `raw` itself (`<public>/mcp/k/<key>`), for a client that takes only a
    URL; it is as secret as the key, and revoking the key voids it."""
    return f"{public_url()}/{KEY_SEGMENT}/{raw}"


def public_is_local() -> bool:
    """True when the public URL names this machine (localhost, 127.0.0.1…): a web client's servers cannot
    reach it until the site is deployed; a client on this machine can."""
    host = (urlsplit(public_url()).hostname or "").lower()
    return host in LOCAL_HOSTS


def rate_limit() -> int:
    return int(settings.NASSAKH.get("MCP_RATE_LIMIT", 60))


def rate_limited(key: AccessKey) -> bool:
    """True when the key made `rate_limit()` calls (refused ones not counted) in the last minute."""
    since = timezone.now() - timedelta(minutes=1)
    calls = ToolCall.objects.filter(key=key, created_at__gte=since).exclude(status=ToolCall.Status.LIMITED)
    return calls.count() >= rate_limit()


def log_call(key: AccessKey | None, tool: str, ms: int, status: str, detail: str = "") -> ToolCall:
    return ToolCall.objects.create(
        key=key,
        user_id=key.user_id if key is not None else None,
        tool=tool[:40],
        ms=max(0, int(ms)),
        status=status,
        detail=str(detail or "")[:200],
    )


def key_item(key: AccessKey, calls: int = 0) -> dict:
    """A key as the page lists it (never the key itself)."""
    return {
        "id": key.pk,
        "name": key.name,
        "prefix": key.prefix,
        "created_at": key.created_at.isoformat() if key.created_at else None,
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
        "active": key.revoked_at is None,
        "calls": calls,
    }


def keys_of(user) -> list[dict]:
    """The user's keys, newest first, each with its calls of the last 30 days."""
    since = timezone.now() - timedelta(days=30)
    keys = AccessKey.objects.filter(user=user).annotate(
        recent=Count("calls", filter=Q(calls__created_at__gte=since))
    )
    return [key_item(key, key.recent) for key in keys]
