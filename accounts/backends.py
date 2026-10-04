"""Sign-in by email (D106).

The email is the login, in any case: a user's username is their email in lower case (`accounts.signals`
keeps the two the same on every save, migration 0006 converted the accounts made before), so a sign-in looks
the lower-cased email up as the username. A username that is not an email signs no one in.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


def normalize_email(text: str | None) -> str:
    """The form an email is stored and looked up in: trimmed, lower case ("" for nothing)."""
    return (text or "").strip().lower()


class EmailBackend(ModelBackend):
    """Django's `ModelBackend` (password, active check, permissions), looked up by the lower-cased email."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        email = normalize_email(username or kwargs.get(get_user_model().USERNAME_FIELD))
        if "@" not in email or password is None:
            get_user_model()().set_password(password or "")  # the same time as a wrong password
            return None
        return super().authenticate(request, username=email, password=password)


def find_user(login: str):
    """The user an email (any case) names, else None."""
    email = normalize_email(login)
    if "@" not in email:
        return None
    return get_user_model()._default_manager.filter(username=email).first()
