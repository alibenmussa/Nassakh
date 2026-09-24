"""Account helpers: role groups and safe redirects."""

from __future__ import annotations

from django.contrib.auth.models import Group
from django.http import HttpRequest
from django.utils.http import url_has_allowed_host_and_scheme

from core.decorators import ROLES, user_role

__all__ = ["ROLES", "ensure_groups", "safe_next_url", "user_role"]


def ensure_groups() -> list[Group]:
    """Create the role groups `admin`, `editor`, `proofreader` if missing. Idempotent."""
    return [Group.objects.get_or_create(name=name)[0] for name in ROLES]


def safe_next_url(request: HttpRequest) -> str:
    """Return the `next` parameter (POST first, then GET) when it points at this host, else ''."""
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return ""
