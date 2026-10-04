"""The email is the login (D106): on every save a user's email is stored in lower case and the username
follows it, whoever saves the user (sign-up, Django admin, `createsuperuser`, the shell). A user saved
without an email but with an email as username gets that email. A user with neither cannot sign in until an
email is set."""

from __future__ import annotations

from django.conf import settings
from django.db.models.signals import pre_save
from django.dispatch import receiver

from accounts.backends import normalize_email


@receiver(pre_save, sender=settings.AUTH_USER_MODEL)
def email_is_the_login(sender, instance, **kwargs) -> None:
    email = normalize_email(instance.email)
    if not email and "@" in (instance.username or ""):
        email = normalize_email(instance.username)
    if email:
        instance.email = email
        instance.username = email
