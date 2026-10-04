"""Login / logout views, role groups and the seed command."""

from io import StringIO

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

import pytest

from accounts.services import ensure_groups

LOGIN_URL = "/accounts/login/"


@pytest.fixture
def user(db):
    return User.objects.create_user("ali", email="Ali@Example.org", password="secret-pass")


@pytest.mark.django_db
def test_login_page_renders_the_card(client):
    response = client.get(reverse("accounts:login"))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'lang="ar" dir="rtl"' in body
    assert "نسّاخ" in body
    assert "البريد الإلكتروني" in body and "كلمة المرور" in body
    assert 'name="username"' in body and 'type="email"' in body and 'type="password"' in body


def test_login_with_wrong_password_shows_an_arabic_error(client, user):
    response = client.post(reverse("accounts:login"), {"username": "ali@example.org", "password": "wrong"})
    assert response.status_code == 200
    assert "البريد الإلكتروني أو كلمة المرور غير صحيحة" in response.content.decode()
    assert "_auth_user_id" not in client.session


def test_login_with_missing_fields_shows_field_errors(client, user):
    response = client.post(reverse("accounts:login"), {"username": "", "password": ""})
    assert response.status_code == 200
    body = response.content.decode()
    assert "أدخل بريدك الإلكتروني." in body and "أدخل كلمة المرور." in body


def test_login_redirects_to_next_when_it_is_local(client, user):
    response = client.post(
        reverse("accounts:login") + "?next=/admin/",
        {"username": "ALI@example.org", "password": "secret-pass"},
    )
    assert response.status_code == 302
    assert response["Location"] == "/admin/"
    assert client.session["_auth_user_id"] == str(user.pk)


def test_login_ignores_an_external_next(client, user):
    with override_settings(LOGIN_REDIRECT_URL="/admin/"):
        response = client.post(
            reverse("accounts:login"),
            {"username": "ALI@example.org", "password": "secret-pass", "next": "https://evil.example/"},
        )
    assert response.status_code == 302
    assert response["Location"] == "/admin/"


def test_login_redirects_to_login_redirect_url_by_default(client, user):
    with override_settings(LOGIN_REDIRECT_URL="/admin/"):
        form = {"username": "ALI@example.org", "password": "secret-pass"}
        response = client.post(reverse("accounts:login"), form)
    assert response.status_code == 302 and response["Location"] == "/admin/"


def test_authenticated_user_is_redirected_away_from_login(client, user):
    client.force_login(user)
    with override_settings(LOGIN_REDIRECT_URL="/admin/"):
        response = client.get(reverse("accounts:login"))
    assert response.status_code == 302 and response["Location"] == "/admin/"


def test_protected_page_redirects_to_login_with_next(client):
    response = client.get(reverse("core:home"))
    assert response.status_code == 302
    assert response["Location"] == f"{LOGIN_URL}?next=/"


def test_logout_requires_post_and_returns_to_login(client, user):
    client.force_login(user)
    assert client.get(reverse("accounts:logout")).status_code == 405
    response = client.post(reverse("accounts:logout"))
    assert response.status_code == 302
    assert response["Location"] == LOGIN_URL
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_role_groups_exist_after_migrations():
    assert set(Group.objects.values_list("name", flat=True)) >= {"admin", "editor", "proofreader"}


@pytest.mark.django_db
def test_ensure_groups_and_seed_command_are_idempotent():
    before = Group.objects.count()
    ensure_groups()
    out = StringIO()
    call_command("seed_groups", stdout=out)
    assert Group.objects.count() == before
    assert "admin, editor, proofreader" in out.getvalue()
