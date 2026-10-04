"""«البحث والتحقق» (D107): the page, the JSON endpoints and the access keys (D108), scoped to the user's
books."""

from __future__ import annotations

import json

from django.test import Client
from django.urls import reverse

import pytest

from research import index, keys
from research.models import AccessKey

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def indexed(library):
    for book in (library["book"], library["other"], library["secret"]):
        index.reindex_book(book.pk)
    return library


def _client(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _post(client: Client, name: str, body: dict, *args):
    return client.post(reverse(f"api:{name}", args=args), json.dumps(body), content_type="application/json")


def test_the_page_lists_the_users_books_and_links_from_the_sidebar(reader, library):
    response = _client(reader).get(reverse("research:home"))
    body = response.content.decode()
    assert response.status_code == 200
    assert 'href="/research/"' in body and "البحث والتحقق" in body
    config = json.loads(body.split('id="research-config" type="application/json">')[1].split("</script>")[0])
    assert {b["title"] for b in config["books"]} == {"مختصر صحيح البخاري", "كتاب آخر"}  # never «كتاب سري»
    assert config["mcp_url"].endswith("/mcp") and config["public_local"] is True  # the default: localhost
    assert config["urls"]["example"] == reverse("api:research_example")
    assert "كتاب سري" not in body
    # the three parts and the one-click example
    assert "ربط مساعد" in body and "جرّب مثالًا" in body and "لن يظهر مرة أخرى" in body


def test_the_example_is_a_sentence_of_the_accounts_text_that_verifies_exact(reader, stranger, library):
    client = _client(reader)
    for _ in range(5):  # drawn at random: every draw must hold
        example = client.get(reverse("api:research_example")).json()
        assert example["book"]["id"] in {library["book"].pk, library["other"].pk}
        assert len(example["quote"].split()) == 7
        checked = _post(client, "research_verify", {"quote": example["quote"]}).json()
        assert checked["status"] == "exact", example
        assert not any(word.endswith("~") for word in example["quote"].split())  # never a doubtful reading
    # the other account draws from its own book only
    theirs = _client(stranger).get(reverse("api:research_example")).json()
    assert theirs["book"]["id"] == library["secret"].pk


def test_no_text_means_no_example(db):
    from django.contrib.auth.models import User

    lonely = User.objects.create_user("lonely", password="pass-1234")
    assert _client(lonely).get(reverse("api:research_example")).json() == {"quote": None, "book": None}


def test_the_page_needs_a_session(client):
    assert client.get(reverse("research:home")).status_code == 302


def test_search_api(reader, library):
    response = _post(_client(reader), "research_search", {"query": "إنما الأعمال بالنيات", "kind": "body"})
    data = response.json()
    assert response.status_code == 200 and data["total"] == 1
    hit = data["hits"][0]
    assert hit["book"]["id"] == library["book"].pk and hit["page"]["printed"] == "11"
    assert hit["clip_url"].startswith("http://testserver/research/clip/")  # absolute: this request's host
    assert _post(_client(reader), "research_search", {"query": " "}).status_code == 400


def test_verify_api(reader):
    client = _client(reader)
    exact = _post(client, "research_verify", {"quote": "إنما الأعمال بالنيات وإنما لكل امرئ ما نوى"}).json()
    assert exact["status"] == "exact" and exact["citation"].endswith("ص 11")
    note = _post(
        client,
        "research_verify",
        {"quote": "هذا الحديث رواه البخاري في أول صحيحه", "attributed_to": "author"},
    ).json()
    assert note["attribution"] == "note_not_author"
    assert _post(client, "research_verify", {"quote": ""}).status_code == 400


def test_the_apis_never_answer_from_another_organisations_books(reader, stranger, library):
    client = _client(reader)
    secret = library["secret"].pk
    searched = _post(client, "research_search", {"query": "سر مكتوم", "book_ids": [secret]}).json()
    assert searched["total"] == 0
    refused = _post(client, "research_verify", {"quote": "سر مكتوم لا يراه غير أهله", "book_id": secret})
    assert refused.status_code == 404
    hit = _post(client, "research_search", {"query": "إنما الأعمال بالنيات"}).json()["hits"][0]
    assert _client(stranger).get(reverse("api:research_passage", args=[hit["passage_id"]])).status_code == 404
    assert _client(reader).get(reverse("api:research_passage", args=[hit["passage_id"]])).status_code == 200
    books = _client(stranger).get(reverse("api:research_books")).json()["books"]
    assert [b["title"] for b in books] == ["كتاب سري"]


def test_access_keys_are_made_shown_once_listed_and_revoked(reader, stranger):
    client = _client(reader)
    made = _post(client, "research_keys", {"name": "حاسوب المكتب"})
    assert made.status_code == 201
    secret = made.json()["secret"]
    assert secret.startswith("nsk_") and len(secret) > 40
    assert made.json()["secret_url"] == f"{made.json()['mcp_url']}/k/{secret}"  # for the URL-only clients
    assert made.json()["public_local"] is True
    row = AccessKey.objects.get(user=reader)
    assert row.key_hash == keys.hash_key(secret) and secret not in json.dumps(made.json()["key"])
    listed = client.get(reverse("api:research_keys")).json()
    assert [k["name"] for k in listed["keys"]] == ["حاسوب المكتب"]
    assert "secret" not in json.dumps(listed["keys"]) and "public_local" in listed
    assert keys.resolve_key(secret).pk == row.pk
    # another user cannot revoke it
    assert _post(_client(stranger), "research_key_revoke", {}, row.pk).status_code == 404
    revoked = _post(client, "research_key_revoke", {}, row.pk)
    assert revoked.status_code == 200 and revoked.json()["key"]["active"] is False
    assert keys.resolve_key(secret) is None
    assert _post(client, "research_keys", {"name": ""}).status_code == 400


def test_the_public_url_decides_the_secret_url_and_whether_it_is_local(settings):
    settings.NASSAKH = {**settings.NASSAKH, "MCP_PUBLIC_URL": "https://nassakh.example/mcp/"}
    assert keys.public_url() == "https://nassakh.example/mcp"
    assert keys.secret_url("nsk_abc") == "https://nassakh.example/mcp/k/nsk_abc"
    assert keys.public_is_local() is False
    settings.NASSAKH = {**settings.NASSAKH, "MCP_PUBLIC_URL": "http://127.0.0.1:8001/mcp"}
    assert keys.public_is_local() is True


def test_a_key_of_a_deactivated_user_stops_working(reader):
    key, secret = keys.make_key(reader, "x")
    reader.is_active = False
    reader.save(update_fields=["is_active"])
    assert keys.resolve_key(secret) is None
    assert keys.resolve_key("nsk_unknown") is None and keys.resolve_key("") is None
