"""Template context shared by every page: the signed-in user's role and organisation for the navigation."""

from django.http import HttpRequest

from core.decorators import ROLE_LABELS, user_role


def nav(request: HttpRequest) -> dict:
    """Expose `is_admin`, `role`, `role_label`, `nav_organization` (the organisation the user works in,
    D98, D102: the sidebar's «المؤسسة»; none without a membership) and `nav_quota` (its page quota, D106:
    the sidebar's «الرصيد», an `accounts.billing.Summary`; None without an organisation) to templates (all
    falsy for anonymous users)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {
            "is_admin": False,
            "role": None,
            "role_label": "",
            "nav_organization": None,
            "nav_quota": None,
        }
    from accounts import billing
    from accounts.services import current_organization

    role = user_role(user)
    organization = current_organization(request)
    return {
        "is_admin": role == "admin",
        "role": role,
        "role_label": ROLE_LABELS.get(role, ""),
        "nav_organization": organization,
        "nav_quota": billing.nav_summary(organization) if organization is not None else None,
    }
