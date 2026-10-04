"""D106: sign-up, email confirmation, sending the link again, and signing in by email or username.

Mail goes to Django's in-memory outbox (`mailoutbox`); the rate limit lives in the cache, cleared per test.
"""

from __future__ import annotations

import re
import time
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import signing
from django.core.cache import cache
from django.urls import reverse

import pytest

from accounts import signup as signups
from accounts.models import Membership, Organization, QuotaGrant, SignUp

pytestmark = pytest.mark.django_db

PASSWORD = "Mukhtasar-1966!"
ORG_FORM = {
    "full_name": "  علي   بن موسى ",
    "email": "Ali@Example.ORG",
    "password": PASSWORD,
    "kind": "organization",
    "org_name": "دار التراث",
    "org_type": "publisher",
    "country": "ليبيا",
    "website": "daraltourath.example",
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _link(message) -> str:
    match = re.search(r"https?://\S+/accounts/confirm/\S+?/", message.body)
    assert match, message.body
    return match.group(0)


def _path(link: str) -> str:
    return "/" + link.split("://", 1)[1].split("/", 1)[1]


# ====================================================================== the form


def test_the_login_page_links_to_the_sign_up(client):
    body = client.get(reverse("accounts:login")).content.decode()
    assert reverse("accounts:signup") in body and "أنشئ حسابًا" in body
    body = client.get(reverse("accounts:signup")).content.decode()
    assert "حساب جديد" in body and 'name="kind"' in body and 'name="org_type"' in body
    assert "og-kind" in body and "باحث أو محقّق" in body  # the two kinds, as cards


def test_an_organisation_signs_up_inactive_with_a_confirmation_email(client, mailoutbox):
    response = client.post(reverse("accounts:signup"), ORG_FORM)
    assert response.status_code == 302 and response["Location"] == reverse("accounts:signup_sent")
    user = User.objects.get(email="ali@example.org")
    assert (user.username, user.first_name, user.is_active) == ("ali@example.org", "علي بن موسى", False)
    assert user.check_password(PASSWORD)
    membership = Membership.objects.select_related("organization").get(user=user)
    org = membership.organization
    assert membership.role == Membership.Role.ADMIN
    assert (org.name, org.kind, org.org_type, org.country, org.website, org.unlimited) == (
        "دار التراث",
        "organization",
        "publisher",
        "ليبيا",
        "https://daraltourath.example",
        False,
    )
    assert set(user.groups.values_list("name", flat=True)) == {"editor"}  # never the global admin group
    assert SignUp.objects.get(user=user).confirmed_at is None
    assert not QuotaGrant.objects.filter(organization=org).exists()  # SIGNUP_PAGE_QUOTA defaults to 0
    assert len(mailoutbox) == 1
    message = mailoutbox[0]
    assert message.to == ["ali@example.org"] and message.subject == "فعّل حسابك في نسّاخ"
    assert "مرحبًا علي بن موسى" in message.body and "3 أيام" in message.body
    html = message.alternatives[0]
    assert html.mimetype == "text/html" and _link(message) in html.content and 'dir="rtl"' in html.content
    body = client.get(response["Location"]).content.decode()
    assert "ali@example.org" in body and "أرسل الرابط مجددًا" in body


def test_an_individual_account_is_named_after_its_person(client, settings, mailoutbox):
    settings.SIGNUP_PAGE_QUOTA = 25
    settings.SIGNUP_QUOTA_DAYS = 30
    data = {**ORG_FORM, "kind": "individual", "email": "reader@example.org", "org_name": "", "org_type": ""}
    assert client.post(reverse("accounts:signup"), data).status_code == 302
    org = Organization.objects.get(memberships__user__email="reader@example.org")
    assert (org.name, org.kind, org.org_type, org.website) == ("علي بن موسى", "individual", "", "")
    grant = QuotaGrant.objects.get(organization=org)
    assert (grant.kind, grant.pages, grant.remaining) == ("signup", 25, 25)
    assert 29 <= (grant.expires_at - grant.starts_at).days <= 30


def test_the_form_refuses_a_taken_email_a_weak_password_and_a_missing_organisation(client, mailoutbox):
    User.objects.create_user("ALI@example.org", email="", password="x")  # a username equal to the email
    body = client.post(reverse("accounts:signup"), {**ORG_FORM, "email": "ali@EXAMPLE.org"}).content.decode()
    assert "هذا البريد مسجّل من قبل" in body
    User.objects.create_user("old", email="Taken@Example.org", password="x")
    body = client.post(
        reverse("accounts:signup"), {**ORG_FORM, "email": "taken@example.org"}
    ).content.decode()
    assert "هذا البريد مسجّل من قبل" in body
    body = client.post(
        reverse("accounts:signup"), {**ORG_FORM, "email": "new@example.org", "password": "12345678"}
    ).content.decode()
    assert 'name="password"' in body and "field-error" in body
    body = client.post(
        reverse("accounts:signup"), {**ORG_FORM, "email": "new@example.org", "org_name": " ", "org_type": ""}
    ).content.decode()
    assert "أدخل اسم المؤسسة." in body and "اختر نوع المؤسسة." in body
    assert not User.objects.filter(email="new@example.org").exists() and not mailoutbox


# ====================================================================== the link


def test_the_link_activates_and_signs_in_once(client, mailoutbox):
    client.post(reverse("accounts:signup"), ORG_FORM)
    path = _path(_link(mailoutbox[0]))
    response = client.get(path)
    assert response.status_code == 302 and response["Location"] == reverse("books:list")
    user = User.objects.get(email="ali@example.org")
    assert user.is_active and SignUp.objects.get(user=user).confirmed_at is not None
    assert client.session["_auth_user_id"] == str(user.pk)
    # used again from another browser: no sign-in, the login page says the account is active
    other = client.__class__()
    response = other.get(path)
    assert response.status_code == 302 and response["Location"] == reverse("accounts:login")
    assert "_auth_user_id" not in other.session
    assert "حسابك مفعّل من قبل" in other.get(reverse("accounts:login")).content.decode()


def test_an_expired_or_broken_link_offers_a_new_one(client, settings, mailoutbox):
    client.post(reverse("accounts:signup"), ORG_FORM)
    token = _path(_link(mailoutbox[0])).rstrip("/").rsplit("/", 1)[1]
    with patch.object(signing, "time") as clock:  # the token was signed now; read it 4 days later
        clock.time.return_value = time.time() + 4 * 24 * 3600
        response = client.get(reverse("accounts:confirm_email", args=[token]))
    assert response.status_code == 400 and "انتهت صلاحية الرابط" in response.content.decode()
    response = client.get(reverse("accounts:confirm_email", args=[token[:-3] + "abc"]))
    assert response.status_code == 400 and "الرابط غير صالح" in response.content.decode()
    assert not User.objects.get(email="ali@example.org").is_active


# ====================================================================== the login and the resend


def test_the_login_says_an_account_awaits_confirmation_only_with_its_password(client, mailoutbox):
    client.post(reverse("accounts:signup"), ORG_FORM)
    body = client.post(
        reverse("accounts:login"), {"username": "ali@example.org", "password": "wrong"}
    ).content.decode()
    assert "غير صحيحة" in body and "لم يُفعَّل حسابك بعد" not in body
    body = client.post(
        reverse("accounts:login"), {"username": "ALI@example.org", "password": PASSWORD}
    ).content.decode()
    assert "لم يُفعَّل حسابك بعد" in body and "data-resend" in body and 'value="ali@example.org"' in body
    assert "_auth_user_id" not in client.session


def test_the_link_is_sent_again_at_most_once_a_minute(client, mailoutbox):
    client.post(reverse("accounts:signup"), ORG_FORM)
    assert len(mailoutbox) == 1
    response = client.post(reverse("accounts:signup_resend"), {"email": "ali@example.org"})
    assert response.status_code == 302 and len(mailoutbox) == 1  # within the minute of the first
    cache.clear()
    client.post(reverse("accounts:signup_resend"), {"email": "Ali@Example.org"})
    assert len(mailoutbox) == 2 and _link(mailoutbox[1])
    body = client.get(reverse("accounts:login")).content.decode()
    assert signups_answer() in body
    # an address without a pending sign-up: the same answer, no email
    cache.clear()
    client.post(reverse("accounts:signup_resend"), {"email": "nobody@example.org"})
    assert len(mailoutbox) == 2


def signups_answer() -> str:
    from accounts.views import RESEND_ANSWER

    return RESEND_ANSWER


def test_five_links_a_day_at_most():
    sent = 0
    for _ in range(8):
        cache.delete(signups._key("cooldown", "a@example.org"))
        sent += signups.may_send("a@example.org")
    assert sent == signups.SENDS_PER_DAY


def test_a_user_an_admin_deactivated_is_not_offered_a_link(client, mailoutbox):
    User.objects.create_user("idle", email="idle@example.org", password=PASSWORD, is_active=False)
    body = client.post(
        reverse("accounts:login"), {"username": "idle@example.org", "password": PASSWORD}
    ).content.decode()
    assert "لم يُفعَّل حسابك بعد" not in body and "غير صحيحة" in body
    client.post(reverse("accounts:signup_resend"), {"email": "idle@example.org"})
    assert not mailoutbox


def test_sign_in_by_email_in_any_case_never_by_a_username(client, mailoutbox):
    client.post(reverse("accounts:signup"), ORG_FORM)
    client.get(_path(_link(mailoutbox[0])))
    client.post(reverse("accounts:logout"))
    response = client.post(reverse("accounts:login"), {"username": "Ali@Example.Org", "password": PASSWORD})
    assert response.status_code == 302 and "_auth_user_id" in client.session
    client.post(reverse("accounts:logout"))
    # any user's login is their email in lower case, whatever username they were given
    old = User.objects.create_user("hamza", email="Hamza@Example.org", password=PASSWORD)
    assert (old.username, old.email) == ("hamza@example.org", "hamza@example.org")
    response = client.post(reverse("accounts:login"), {"username": "hamza", "password": PASSWORD})
    assert response.status_code == 200 and "_auth_user_id" not in client.session
    form = {"username": " HAMZA@example.org ", "password": PASSWORD}
    response = client.post(reverse("accounts:login"), form)
    assert response.status_code == 302 and client.session["_auth_user_id"] == str(old.pk)
    client.post(reverse("accounts:logout"))
    # a user without an email cannot sign in
    User.objects.create_user("nomail", password=PASSWORD)
    response = client.post(reverse("accounts:login"), {"username": "nomail", "password": PASSWORD})
    assert response.status_code == 200 and "_auth_user_id" not in client.session


def test_every_save_keeps_the_username_equal_to_the_lower_cased_email(db):
    user = User.objects.create_user("x", email="  Mixed@Case.ORG ", password=PASSWORD)
    assert (user.username, user.email) == ("mixed@case.org", "mixed@case.org")
    user.email = "New@Case.org"
    user.save()
    user.refresh_from_db()
    assert user.username == user.email == "new@case.org"
    by_username = User.objects.create_user("Only@Username.org", password=PASSWORD)
    assert by_username.username == by_username.email == "only@username.org"
