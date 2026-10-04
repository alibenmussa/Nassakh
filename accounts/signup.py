"""Sign-up (D106): an account of one's own on the site, an organisation or an individual, confirmed by email.

`create_account` makes, in one transaction, an inactive user (username = the email in lower case: the email is
the login, unique in any case), the organisation (`kind`; an individual's is named after the person), a
`Membership` as its admin, the user in the `editor` group (never the global `admin` group: that one is
cross-organisation), a `SignUp` row and, when `SIGNUP_PAGE_QUOTA` > 0, a `signup` grant valid
`SIGNUP_QUOTA_DAYS`. The confirmation link (`django.core.signing`, valid `EMAIL_CONFIRM_DAYS`) activates the
user once (`confirm`); asking for it again is rate-limited per address through the cache (`may_send`).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import signing
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from accounts import billing
from accounts.models import Membership, Organization, QuotaGrant, SignUp
from core.decorators import ROLE_EDITOR

log = logging.getLogger(__name__)

SALT = "nassakh.accounts.signup.confirm"
SEND_COOLDOWN_S = 60  # one confirmation email per address per minute
SENDS_PER_DAY = 5  # and five a day
DAY_FORMS: tuple[str, str, str, str] = ("يوم واحد", "يومين", "أيام", "يومًا")


class ConfirmError(Exception):
    """A confirmation link that cannot be used: `code` is `expired` or `invalid`."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def days_phrase(n: int) -> str:
    """«يوم واحد», «يومين», «3 أيام», «30 يومًا» (Western digits)."""
    n = int(n)
    if n == 1:
        return DAY_FORMS[0]
    if n == 2:
        return DAY_FORMS[1]
    return f"{n} {DAY_FORMS[2] if 3 <= n % 100 <= 10 else DAY_FORMS[3]}"


def confirm_days() -> int:
    return max(1, int(getattr(settings, "EMAIL_CONFIRM_DAYS", 3) or 3))


# ====================================================================== the account


def create_account(data: dict):
    """The new account from `SignUpForm.cleaned_data`; returns the (inactive) user."""
    email = data["email"].strip().lower()
    individual = data.get("kind") == Organization.Kind.INDIVIDUAL
    with transaction.atomic():
        user = get_user_model()._default_manager.create_user(
            username=email,
            email=email,
            password=data["password"],
            first_name=data["full_name"][:150],
            is_active=False,
        )
        organization = Organization.objects.create(
            name=(data["full_name"] if individual else data["org_name"])[:200],
            kind=Organization.Kind.INDIVIDUAL if individual else Organization.Kind.ORGANIZATION,
            org_type="" if individual else data.get("org_type") or "",
            country=(data.get("country") or "")[:100],
            website="" if individual else data.get("website") or "",
            unlimited=False,
        )
        Membership.objects.create(organization=organization, user=user, role=Membership.Role.ADMIN)
        user.groups.add(Group.objects.get_or_create(name=ROLE_EDITOR)[0])
        SignUp.objects.create(user=user)
        pages = int(getattr(settings, "SIGNUP_PAGE_QUOTA", 0) or 0)
        if pages > 0:
            billing.grant(
                organization,
                pages,
                kind=QuotaGrant.Kind.SIGNUP,
                days=int(getattr(settings, "SIGNUP_QUOTA_DAYS", 30) or 0) or None,
                note="رصيد التسجيل",
            )
    log.info("sign-up: user %s, account %s (%s)", user.pk, organization.pk, organization.kind)
    return user


def is_pending(user) -> bool:
    """True for a sign-up whose email is not confirmed yet (inactive, its `SignUp` unconfirmed)."""
    if user is None or getattr(user, "is_active", True):
        return False
    return SignUp.objects.filter(user=user, confirmed_at__isnull=True).exists()


def pending_user(email: str):
    """The sign-up waiting for confirmation with this email, else None."""
    text = (email or "").strip()
    if not text:
        return None
    return (
        get_user_model()
        ._default_manager.filter(
            email__iexact=text, is_active=False, signup__isnull=False, signup__confirmed_at__isnull=True
        )
        .order_by("id")
        .first()
    )


# ====================================================================== the link


def make_token(user) -> str:
    """The signed confirmation token of `user` (its id and email)."""
    return signing.dumps({"u": user.pk, "e": (user.email or "").lower()}, salt=SALT)


def confirm(token: str):
    """Activate the sign-up of a confirmation token: `(user, activated)`; `activated` False when it was
    confirmed before (the link is used once: it signs no one in again). Raises `ConfirmError`."""
    try:
        data = signing.loads(token, salt=SALT, max_age=timedelta(days=confirm_days()))
    except signing.SignatureExpired:
        raise ConfirmError("expired") from None
    except signing.BadSignature:
        raise ConfirmError("invalid") from None
    if not isinstance(data, dict):
        raise ConfirmError("invalid")
    user = get_user_model()._default_manager.filter(pk=data.get("u")).first()
    if user is None or (user.email or "").lower() != data.get("e"):
        raise ConfirmError("invalid")
    with transaction.atomic():
        signup = SignUp.objects.select_for_update().filter(user=user).first()
        if signup is None:
            raise ConfirmError("invalid")
        if signup.confirmed_at is not None:
            return user, False
        signup.confirmed_at = timezone.now()
        signup.save(update_fields=["confirmed_at"])
        user.is_active = True
        user.save(update_fields=["is_active"])
    log.info("sign-up confirmed: user %s", user.pk)
    return user, True


def site_url(request: HttpRequest | None) -> str:
    """The site's address for links in emails: `SITE_URL`, else the request's."""
    base = (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
    if base or request is None:
        return base
    return request.build_absolute_uri("/").rstrip("/")


def _key(prefix: str, email: str) -> str:
    return f"nassakh:signup:{prefix}:{hashlib.sha256(email.strip().lower().encode()).hexdigest()[:32]}"


def may_send(email: str) -> bool:
    """Rate limit of the confirmation email (through the cache): one per address a minute, five a day."""
    if not cache.add(_key("cooldown", email), 1, SEND_COOLDOWN_S):
        return False
    day = _key("day", email)
    cache.add(day, 0, 24 * 3600)
    try:
        sent = cache.incr(day)
    except ValueError:  # expired between the two calls
        cache.set(day, 1, 24 * 3600)
        sent = 1
    return sent <= SENDS_PER_DAY


def send_confirmation(user, request: HttpRequest | None = None) -> bool:
    """Send the confirmation email (Arabic plain text and HTML); False when it could not be sent (logged)."""
    link = site_url(request) + reverse("accounts:confirm_email", args=[make_token(user)])
    context = {"name": user.first_name or user.email, "link": link, "days": days_phrase(confirm_days())}
    subject = " ".join(render_to_string("accounts/email/confirm_subject.txt", context).split())
    message = EmailMultiAlternatives(
        subject,
        render_to_string("accounts/email/confirm.txt", context),
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )
    message.attach_alternative(render_to_string("accounts/email/confirm.html", context), "text/html")
    try:
        message.send()
    except Exception:  # noqa: BLE001 - SMTP down or refused: the page offers to send it again
        log.exception("sign-up: the confirmation email to user %s could not be sent", user.pk)
        return False
    return True


def resend(email: str, request: HttpRequest | None = None) -> bool:
    """Send the link again to a sign-up that waits for confirmation (rate-limited). The caller answers the
    same whatever happened, so the page tells no one which addresses have an account. True when an email
    went."""
    user = pending_user(email)
    if user is None or not may_send(user.email):
        return False
    return send_confirmation(user, request)
