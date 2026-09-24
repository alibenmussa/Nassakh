"""Template context shared by every page: the signed-in user's role for the navigation."""

from django.http import HttpRequest

from core.decorators import ROLE_LABELS, user_role


def nav(request: HttpRequest) -> dict:
    """Expose `is_admin`, `role` and `role_label` to templates (all falsy for anonymous users)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"is_admin": False, "role": None, "role_label": ""}
    role = user_role(user)
    return {"is_admin": role == "admin", "role": role, "role_label": ROLE_LABELS.get(role, "")}
