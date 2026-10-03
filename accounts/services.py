"""Account helpers: role groups, safe redirects, and the organisation a user or a book belongs to (D98)."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import Group
from django.http import HttpRequest
from django.utils.http import url_has_allowed_host_and_scheme

from core.decorators import ROLE_ADMIN, ROLE_EDITOR, ROLES, has_role, user_role

__all__ = [
    "ROLES",
    "book_organization",
    "can_edit_books",
    "default_organization",
    "ensure_groups",
    "is_member",
    "is_org_admin",
    "organization_for",
    "safe_next_url",
    "user_role",
]


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


# ====================================================================== the organisation (D98)


def default_organization():
    """The first organisation, created (named `NASSAKH["ORGANIZATION_NAME"]`) when there is none."""
    from .models import Organization

    organization = Organization.objects.order_by("id").first()
    if organization is None:
        name = settings.NASSAKH.get("ORGANIZATION_NAME") or "المؤسسة"
        organization = Organization.objects.create(name=name)
    return organization


def _membership(user):
    if not getattr(user, "is_authenticated", False):
        return None
    from .models import Membership

    try:
        return user.membership
    except Membership.DoesNotExist:
        return None


def _only_organization():
    """The organisation when there is exactly one (a user without a membership belongs to it), else None."""
    from .models import Organization

    rows = list(Organization.objects.order_by("id")[:2])
    return rows[0] if len(rows) == 1 else None


def organization_for(user):
    """The user's organisation: their membership's; without one, the only organisation there is (one
    publisher, the common case), or the first one for a superuser; None for anonymous users and for a user
    without a membership among several organisations."""
    if not getattr(user, "is_authenticated", False):
        return None
    membership = _membership(user)
    if membership is not None:
        return membership.organization
    only = _only_organization()
    if only is not None:
        return only
    if user.is_superuser:
        return default_organization()
    return None


def is_member(user, organization) -> bool:
    """True when the user belongs to `organization` (superusers belong to every one)."""
    if organization is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    own = organization_for(user)
    return own is not None and own.pk == organization.pk


def is_org_admin(user, organization) -> bool:
    """True for the organisation's admins: a superuser, a member whose membership is `admin`, or a member
    of the global `admin` role group (the owner's admins administer their organisation)."""
    if not is_member(user, organization):
        return False
    if user.is_superuser:
        return True
    membership = _membership(user)
    if membership is not None and membership.role == membership.Role.ADMIN:
        return True
    return user_role(user) == ROLE_ADMIN


def can_edit_books(user, organization) -> bool:
    """True when the user may apply and save the organisation's templates: an editor (or admin) of the app
    who belongs to it."""
    return is_member(user, organization) and has_role(user, ROLE_EDITOR)


def book_organization(book):
    """The organisation of a book (None for a book that has none)."""
    if book is None or not getattr(book, "organization_id", None):
        return None
    return book.organization
