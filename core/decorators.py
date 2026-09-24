"""Role checks built on Django groups (`admin`, `editor`, `proofreader`)."""

from collections.abc import Callable
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse

ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_PROOFREADER = "proofreader"
ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_EDITOR, ROLE_PROOFREADER)
ROLE_LABELS: dict[str, str] = {ROLE_ADMIN: "مدير", ROLE_EDITOR: "محرّر", ROLE_PROOFREADER: "مراجع"}


def user_role(user) -> str | None:
    """Return the user's highest role, or None for users outside every role group.

    Superusers count as `admin`. Group order decides precedence: admin > editor > proofreader.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    if user.is_superuser:
        return ROLE_ADMIN
    names = set(user.groups.values_list("name", flat=True))
    for role in ROLES:
        if role in names:
            return role
    return None


def has_role(user, *roles: str) -> bool:
    """True when the user is a superuser, an admin, or a member of one of `roles`."""
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    names = set(user.groups.values_list("name", flat=True))
    if ROLE_ADMIN in names:
        return True
    return any(role in names for role in roles)


def role_required(*roles: str) -> Callable:
    """Restrict a view to signed-in users holding one of `roles`.

    Anonymous users are redirected to the login page; signed-in users without the role get 403.
    Superusers and members of the `admin` group always pass.
    """

    def decorator(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
        @wraps(view)
        def wrapped(request: HttpRequest, *args, **kwargs) -> HttpResponse:
            if not has_role(request.user, *roles):
                raise PermissionDenied("هذه الصفحة تتطلب صلاحية أعلى.")
            return view(request, *args, **kwargs)

        return login_required(wrapped)

    return decorator
