"""`/healthz`: the container health check answers without a login."""

from django.urls import reverse

import pytest


def test_healthz_answers_without_login_or_database(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert reverse("core:healthz") == "/healthz"


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_healthz_is_read_only(client, method):
    assert getattr(client, method)("/healthz").status_code == 405


def test_healthz_head_is_allowed(client):
    assert client.head("/healthz").status_code == 200
