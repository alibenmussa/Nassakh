"""The MCP server end to end (D108): the official SDK's client over Streamable HTTP against the ASGI app — the
key check (401), the five tools with their structured results and Arabic summaries, the scoping by the key's
user, the rate limit and the call log; the key inside the URL (`/mcp/k/<key>`) for the clients that take only
a URL, and that the key never reaches a log line.

The async client runs under `async_to_sync`, so the server's ORM calls (`sync_to_async`, thread-sensitive)
come back to this thread and see the test's data."""

from __future__ import annotations

import logging

from django.utils import timezone

import httpx2
import pytest
from asgiref.sync import async_to_sync
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from research import index, keys
from research.mcp_server import MCP_PATH, KeyInPath, RedactKeys, build_app, build_server, log_config, redact
from research.models import ToolCall

pytestmark = pytest.mark.django_db

BASE = "http://localhost"
TOOLS = {"list_books", "search", "get_passage", "verify_quote", "cite"}


@pytest.fixture(autouse=True)
def indexed(library):
    for book in (library["book"], library["other"], library["secret"]):
        index.reindex_book(book.pk)
    return library


@pytest.fixture
def secret_key(reader) -> str:
    return keys.make_key(reader, "test")[1]


def _session(key: str | None, work, mode: str = "auto", path: str = MCP_PATH):
    """Run `work(client)` against a fresh server at `path`, with `key` as the Bearer token (none when the key
    is in the path); returns its result."""

    async def run():
        server = build_server()
        app = build_app(server)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        async with server.session_manager.run():
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url=BASE, headers=headers) as http:
                stream = streamable_http_client(f"{BASE}{path}", http_client=http)
                async with Client(stream, mode=mode) as client:
                    return await work(client)

    return async_to_sync(run)()


def _raw_post(headers: dict, path: str = MCP_PATH) -> httpx2.Response:
    async def run():
        server = build_server()
        app = build_app(server)
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as http:
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                accept = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
                return await http.post(path, json=body, headers={**accept, **headers})

    return async_to_sync(run)()


def test_no_key_an_unknown_key_or_a_revoked_one_answers_401(reader, secret_key):
    assert _raw_post({}).status_code == 401
    assert _raw_post({"Authorization": "Bearer nsk_not-a-key"}).status_code == 401
    assert _raw_post({"Authorization": f"Bearer {secret_key}"}).status_code == 200
    key = keys.resolve_key(secret_key)
    keys.revoke_key(reader, key.pk)
    response = _raw_post({"Authorization": f"Bearer {secret_key}"})
    assert response.status_code == 401 and "Bearer" in response.headers["www-authenticate"]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_the_tools_are_listed_read_only_with_output_schemas(secret_key, mode):
    async def work(client):
        return (await client.list_tools()).tools, client

    tools, _client = _session(secret_key, work, mode=mode)
    assert {tool.name for tool in tools} == TOOLS
    for tool in tools:
        assert tool.annotations.read_only_hint is True and tool.annotations.open_world_hint is False
        assert tool.output_schema and tool.output_schema.get("type") == "object"
        assert tool.description
    search = next(tool for tool in tools if tool.name == "search")
    assert search.input_schema["properties"]["limit"]["maximum"] == 20


def test_every_tool_answers_structured_content_and_an_arabic_summary(secret_key, library):
    async def work(client):
        out = {"list_books": await client.call_tool("list_books", {})}
        out["search"] = await client.call_tool("search", {"query": "إنما الأعمال بالنيات", "kind": "body"})
        passage_id = out["search"].structured_content["hits"][0]["passage_id"]
        out["get_passage"] = await client.call_tool("get_passage", {"passage_id": passage_id, "context": 1})
        out["verify_quote"] = await client.call_tool(
            "verify_quote", {"quote": "هذا الحديث رواه البخاري في أول صحيحه", "attributed_to": "author"}
        )
        out["cite"] = await client.call_tool("cite", {"passage_id": passage_id})
        whole_page = {"book_id": library["book"].pk, "printed_page": "12"}
        out["page"] = await client.call_tool("get_passage", whole_page)
        return out

    out = _session(secret_key, work)
    for name, result in out.items():
        assert not result.is_error, (name, result.content)
        assert result.structured_content, name
        assert result.content[0].type == "text" and result.content[0].text, name
    books = out["list_books"].structured_content["books"]
    assert {b["title"] for b in books} == {"مختصر صحيح البخاري", "كتاب آخر"}  # never the other account's
    hit = out["search"].structured_content["hits"][0]
    assert hit["page"]["printed"] == "11" and hit["kind"] == "body"
    assert hit["clip_url"].startswith("http://localhost:8000/research/clip/")  # SITE_URL (here its default)
    assert "نتيجة واحدة" in out["search"].content[0].text
    verdict = out["verify_quote"].structured_content
    assert verdict["status"] == "exact" and verdict["attribution"] == "note_not_author"
    assert "هذا من حاشية المحقق لا من متن المؤلف." in out["verify_quote"].content[0].text
    assert out["cite"].structured_content["citation"].endswith("ص 11")
    assert out["page"].structured_content["page"]["number"] == 2
    assert out["get_passage"].structured_content["notes"]  # the notes apart from the body
    logged = list(ToolCall.objects.values_list("tool", "status"))
    assert len(logged) == 6 and {status for _tool, status in logged} == {"ok"}


def test_a_key_sees_only_its_users_books(stranger, library):
    secret = keys.make_key(stranger, "theirs")[1]

    async def work(client):
        quote = "إنما الأعمال بالنيات وإنما لكل امرئ ما نوى"
        found = await client.call_tool("verify_quote", {"quote": quote})
        refused = await client.call_tool("get_passage", {"book_id": library["book"].pk, "page": 1})
        books = await client.call_tool("list_books", {})
        return found, refused, books

    found, refused, books = _session(secret, work)
    assert found.structured_content["book"]["id"] == library["secret"].pk
    assert refused.is_error and "الكتاب غير موجود" in refused.content[0].text
    assert [b["title"] for b in books.structured_content["books"]] == ["كتاب سري"]
    assert ToolCall.objects.filter(tool="get_passage", status="error").count() == 1


def test_the_rate_limit_refuses_calls_over_the_minute(reader, secret_key, settings):
    settings.NASSAKH = {**settings.NASSAKH, "MCP_RATE_LIMIT": 2}
    key = keys.resolve_key(secret_key)
    for _ in range(2):
        keys.log_call(key, "search", 5, ToolCall.Status.OK)

    async def work(client):
        return await client.call_tool("list_books", {})

    result = _session(secret_key, work)
    assert result.is_error and "Rate limit" in result.content[0].text
    assert ToolCall.objects.filter(status="limited").count() == 1
    ToolCall.objects.update(created_at=timezone.now() - timezone.timedelta(minutes=2))
    assert not _session(secret_key, work).is_error  # a minute later


# ---------------------------------------------------------------------- the key inside the URL


def test_the_secret_url_connects_a_client_that_sends_no_header(reader, secret_key):
    path = f"/mcp/k/{secret_key}"
    assert _raw_post({}, path).status_code == 200

    async def work(client):
        return await client.call_tool("list_books", {})

    result = _session(None, work, path=path)
    assert not result.is_error and {b["title"] for b in result.structured_content["books"]} == {
        "مختصر صحيح البخاري",
        "كتاب آخر",
    }
    assert ToolCall.objects.filter(tool="list_books", status="ok", key__user=reader).count() == 1
    # the key in the URL stands; a header the client brings is replaced
    assert _raw_post({"Authorization": "Bearer nsk_wrong"}, path).status_code == 200
    # a trailing slash is the same address
    assert _raw_post({}, path + "/").status_code == 200


def test_a_revoked_or_unknown_keys_url_answers_401_like_the_header(reader, secret_key):
    assert _raw_post({}, "/mcp/k/nsk_not-a-key").status_code == 401
    assert _raw_post({}, "/mcp/k/").status_code == 401
    keys.revoke_key(reader, keys.resolve_key(secret_key).pk)
    response = _raw_post({}, f"/mcp/k/{secret_key}")
    assert response.status_code == 401 and "Bearer" in response.headers["www-authenticate"]


def test_the_wrapper_rewrites_the_scope_in_place_so_the_access_log_sees_mcp():
    seen = {}

    async def spy(scope, receive, send):
        seen.update(path=scope["path"], raw=scope["raw_path"], headers=dict(scope["headers"]))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/k/nsk_secret-key-value",
        "raw_path": b"/mcp/k/nsk_secret-key-value",
        "query_string": b"",
        "headers": [(b"host", b"localhost"), (b"authorization", b"Bearer nsk_old")],
    }

    async def send(message):
        pass

    async def run():
        await KeyInPath(spy)(scope, None, send)

    async_to_sync(run)()
    assert seen["path"] == "/mcp" and seen["raw"] == b"/mcp"
    assert seen["headers"][b"authorization"] == b"Bearer nsk_secret-key-value"
    assert scope["path"] == "/mcp"  # the very dict the server logs from
    assert "nsk_" not in scope["path"] and scope["raw_path"] == b"/mcp"


def test_the_key_never_appears_in_a_log_record(reader, secret_key, caplog):
    caplog.set_level(logging.DEBUG)
    assert _raw_post({}, f"/mcp/k/{secret_key}").status_code == 200
    ours = [r for r in caplog.records if not r.name.startswith("http")]  # the test's own client logs its URL
    assert ours, "nothing was logged at all; the check below would be vacuous"
    for record in ours:
        assert secret_key not in record.getMessage() and secret_key not in str(record.args)
    # uvicorn's access line, as its handler would see it with the filter `log_config` installs
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:50000", "POST", f"/mcp/k/{secret_key}", "1.1", 200),
        None,
    )
    assert RedactKeys().filter(record) is True
    message = record.getMessage()
    assert secret_key not in message and '"POST /mcp/k/… HTTP/1.1" 200' in message
    assert redact(f"token {secret_key} in a line") == "token nsk_… in a line"
    assert redact("prefix nsk_abcdefgh… stays") == "prefix nsk_abcdefgh… stays"  # the page's shown prefix
    config = log_config()
    assert config["filters"]["redact_keys"]["()"] == "research.mcp_server.RedactKeys"
    assert all("redact_keys" in handler["filters"] for handler in config["handlers"].values())


def test_the_health_route_needs_no_key():
    async def run():
        server = build_server()
        app = build_app(server)
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as http:
                return await http.get("/healthz")

    response = async_to_sync(run)()
    assert response.status_code == 200 and response.json()["ok"] is True
