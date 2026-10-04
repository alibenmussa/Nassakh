"""The MCP server (D108): the account's books for any MCP client — Claude Code, Claude Desktop, or another
assistant — through the official MCP Python SDK (`mcp` 2.x, `MCPServer`).

- Streamable HTTP, stateless, JSON responses, at `/mcp` (`build_app`, an ASGI app); `manage.py mcp_serve`
  runs it with uvicorn beside Django (Procfile `mcp:`); `/healthz` answers without a key.
- **Auth:** `Authorization: Bearer nsk_…`, an access key its user made on the «البحث والتحقق» page
  (`research.keys`). The SDK's token-verifier hook (`KeyVerifier`, `MCPServer(token_verifier=…)`) resolves
  the key on every request; no key, an unknown or a revoked one → 401 before any tool runs. A client that
  takes only a URL (Claude's and ChatGPT's custom connectors) connects with the secret URL `/mcp/k/<key>`:
  `KeyInPath` moves the key from the path into the header before the app sees the request, and the server
  never logs it (`RedactKeys`, `log_config`). OAuth (a sign-in from claude.ai) can replace the verifier
  later (D109) without touching the tools.
- **Tools** (read-only, closed world): `list_books`, `search`, `get_passage`, `verify_quote`, `cite`. Each
  answers a typed structured result (`research.schemas`, the tool's output schema) and, as text, a short
  Arabic summary followed by the same result as JSON (for clients that read only the text). Every tool
  calls `research.services` with the key's user, so it sees that user's books only (`books.access`).
- The Django ORM runs off the event loop (`asgiref.sync.sync_to_async`, thread-sensitive: one database
  thread, connections closed around each call as Django's request cycle does).
- Limits: `NASSAKH["MCP_RATE_LIMIT"]` calls a minute per key; every call is logged (`research.ToolCall`:
  key, tool, ms, status) for the account page and the evaluation.
- DNS-rebinding protection: the Host must be local, the public URL's host (`NASSAKH["MCP_PUBLIC_URL"]`) or
  one of `ALLOWED_HOSTS`.
"""

import copy
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Annotated, Literal
from urllib.parse import urlsplit

from django.conf import settings
from django.db import close_old_connections, connection
from django.http import Http404

from asgiref.sync import sync_to_async
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import keys, services
from .models import AccessKey, ToolCall
from .schemas import BooksResult, Citation, Passage, SearchResult, VerifyResult

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
KEY_PATH_PREFIX = f"{MCP_PATH}/{keys.KEY_SEGMENT}/"  # `/mcp/k/<key>`: the key inside the URL
SEARCH_LIMIT = 20
# a key written in a path, and a bare key anywhere (the page's shown prefix, `nsk_` + 8, is shorter and stays)
_KEY_IN_PATH = re.compile(re.escape(KEY_PATH_PREFIX) + r"[^/\s\"'?#]+")
_BARE_KEY = re.compile(re.escape(keys.KEY_PREFIX) + r"[A-Za-z0-9_\-]{20,}")
REDACTED = "…"

INSTRUCTIONS = """\
Nassakh holds printed Arabic books (heritage texts and their critical editions) that this user's
organisation digitised: the text of each page as it was read by OCR and checked against the page image.
You see only this account's books.

- Cite the printed page (`page.printed`; when it is null say «صفحة المسح N» with `page.number`) and give
  the `clip_url` so the reader can see the printed lines. Prefer the `citation` the tools return.
- When `verify_quote` answers `needs_image_check`, say «يحتاج مطابقة مع الصورة»: the page's reading is
  uncertain there, the quotation may be right. Never call it «محرّف». `not_found` means it is not in this
  account's books — never call a quotation fabricated («مختلق»).
- Keep the author's text and the editor's notes apart: `kind` = `body` is the author's (المتن), `notes` the
  editor's footnotes (حاشية المحقق). Never present a note as the author's words.
- Quote the book's words exactly as returned (diacritics included). Words marked `doubtful` may be misread.
- `search` finds passages (phrase, all words, then close matches); `get_passage` gives a passage with its
  lines and notes; `verify_quote` checks a quotation word by word; `cite` gives the citation of a passage.
"""

READ_ONLY = {
    "read_only_hint": True,
    "destructive_hint": False,
    "idempotent_hint": True,
    "open_world_hint": False,
}


def _annotations(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, **READ_ONLY)


# ====================================================================== the database, off the event loop


def _in_db_thread(fn: Callable) -> Callable:
    """`fn` with Django's connection hygiene around it (as a request has): stale or broken connections are
    closed before and after (not inside a transaction: the test suite's)."""

    def run(*args, **kwargs):
        fresh = not connection.in_atomic_block
        if fresh:
            close_old_connections()
        try:
            return fn(*args, **kwargs)
        finally:
            if fresh:
                close_old_connections()

    return run


async def _db(fn: Callable, *args, **kwargs):
    return await sync_to_async(_in_db_thread(fn), thread_sensitive=True)(*args, **kwargs)


# ====================================================================== auth


class KeyVerifier:
    """The SDK's token verifier (`mcp.server.auth.provider.TokenVerifier`): a Bearer token is an access key
    (`research.keys.resolve_key`); the access token names the key and its user."""

    async def verify_token(self, token: str) -> AccessToken | None:
        key = await _db(keys.resolve_key, token)
        if key is None:
            return None
        return AccessToken(
            token=key.prefix,
            client_id=f"nassakh-key-{key.pk}",
            scopes=["research"],
            subject=str(key.user_id),
            claims={"key_id": key.pk, "user_id": key.user_id},
        )


class KeyInPath:
    """ASGI wrapper around the server's app: a request to `/mcp/k/<key>` reaches the app as a request to
    `/mcp` carrying `Authorization: Bearer <key>` (an Authorization header the request brought is replaced),
    so a client that takes only a URL connects with the secret URL and meets the same verifier, 401s, rate
    limit and call log. The scope is rewritten in place: the server's access log, which reads the scope's
    path when the response starts, sees `/mcp`, never the key."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http":
            path = str(scope.get("path") or "")
            if path.startswith(KEY_PATH_PREFIX):
                key, _slash, rest = path[len(KEY_PATH_PREFIX) :].partition("/")
                scope["path"] = MCP_PATH + (f"/{rest}" if rest else "")
                scope["raw_path"] = scope["path"].encode("ascii", "ignore")
                headers = [
                    (name, value)
                    for name, value in scope.get("headers") or []
                    if name.lower() != b"authorization"
                ]
                headers.append((b"authorization", f"Bearer {key}".encode()))
                scope["headers"] = headers
        await self.app(scope, receive, send)


def redact(value):
    """`value` with any access key blanked: in a path (`/mcp/k/nsk_…` → `/mcp/k/…`) or on its own."""
    if not isinstance(value, str):
        return value
    return _BARE_KEY.sub(keys.KEY_PREFIX + REDACTED, _KEY_IN_PATH.sub(KEY_PATH_PREFIX + REDACTED, value))


class RedactKeys(logging.Filter):
    """A logging filter that blanks access keys in a record's message and arguments (`redact`); on uvicorn's
    handlers (`log_config`) it keeps a key out of the access log and of any error line."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.msg)
        args = record.args
        if isinstance(args, dict):
            record.args = {name: redact(value) for name, value in args.items()}
        elif isinstance(args, tuple):
            record.args = tuple(redact(value) for value in args)
        return True


def log_config() -> dict:
    """uvicorn's logging config with `RedactKeys` on every handler (`manage.py mcp_serve` passes it)."""
    import uvicorn.config

    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_keys"] = {"()": f"{__name__}.RedactKeys"}
    for handler in config.get("handlers", {}).values():
        handler.setdefault("filters", []).append("redact_keys")
    return config


def _key_id() -> int:
    token = get_access_token()
    claims = (token.claims or {}) if token is not None else {}
    if "key_id" not in claims:
        raise ToolError("Not authenticated: send an access key as `Authorization: Bearer nsk_…`.")
    return int(claims["key_id"])


# ====================================================================== running a tool


def _run_tool(key_id: int, tool: str, work: Callable[[object], BaseModel]) -> BaseModel:
    """Run one tool for the key's user: refuse a revoked key or one over the rate limit, log the call."""
    started = time.monotonic()

    def ms() -> int:
        return int((time.monotonic() - started) * 1000)

    key = AccessKey.objects.select_related("user").filter(pk=key_id, revoked_at__isnull=True).first()
    if key is None:
        raise ToolError("This access key was revoked. «أُلغي هذا المفتاح.»")
    if keys.rate_limited(key):
        keys.log_call(key, tool, 0, ToolCall.Status.LIMITED)
        limit = keys.rate_limit()
        raise ToolError(
            f"Rate limit: at most {limit} calls a minute per key; wait a minute. "
            f"«تجاوزت {limit} استدعاءً في الدقيقة لهذا المفتاح؛ انتظر دقيقة.»"
        )
    try:
        result = work(key.user)
    except Http404 as exc:
        keys.log_call(key, tool, ms(), ToolCall.Status.ERROR, str(exc))
        raise ToolError(f"Not found: {exc}") from exc
    except ToolError as exc:
        keys.log_call(key, tool, ms(), ToolCall.Status.ERROR, str(exc))
        raise
    except Exception as exc:
        keys.log_call(key, tool, ms(), ToolCall.Status.ERROR, type(exc).__name__)
        raise
    keys.log_call(key, tool, ms(), ToolCall.Status.OK)
    return result


async def _call(tool: str, work: Callable[[object], BaseModel], summary: Callable) -> CallToolResult:
    """The tool's answer: its structured result and, as text, the Arabic summary then the same JSON."""
    result = await _db(_run_tool, _key_id(), tool, work)
    data = result.model_dump(mode="json")
    return CallToolResult(
        content=[
            TextContent(type="text", text=summary(result)),
            TextContent(type="text", text=json.dumps(data, ensure_ascii=False)),
        ],
        structured_content=data,
    )


# ====================================================================== Arabic summaries


def _where(page, kind: str | None = None) -> str:
    text = f"ص {page.printed}" if page.printed else f"صفحة المسح {page.number}"
    if kind:
        text += f" ({'المتن' if kind == 'body' else 'الحاشية'})"
    return text


def _books_summary(result: BooksResult) -> str:
    lines = [result.message]
    for book in result.books:
        extra = f"، ص {book.printed_range}" if book.printed_range else ""
        lines.append(
            f"• [{book.id}] «{book.title}» — {book.author or 'مؤلف غير مذكور'}: {book.pages} صفحة{extra}، "
            f"رُوجع {round(book.reviewed_share * 100)}٪ من أسطره."
        )
    return "\n".join(lines)


def _search_summary(result: SearchResult) -> str:
    lines = [result.message]
    for n, hit in enumerate(result.hits, start=result.offset + 1):
        lines.append(
            f"{n}. «{hit.match}» — «{hit.book.title}»، {_where(hit.page, hit.kind)} [{hit.passage_id}]"
        )
    return "\n".join(lines)


def _passage_summary(result: Passage) -> str:
    states = {"reviewed": "مراجَع", "partly_reviewed": "مراجَع جزئيًا", "unreviewed": "غير مراجَع بعد"}
    where = _where(result.page, result.kind)
    text = f"«{result.book.title}»، {where} — {states[result.review_state]}:\n{result.text}"
    if result.doubtful_words:
        text += f"\nكلمات قراءتها غير محسومة: {'، '.join(result.doubtful_words)}"
    return text + f"\nالإحالة: {result.citation}"


def _verify_summary(result: VerifyResult) -> str:
    lines = [result.message]
    if result.attribution_message:
        lines.append(result.attribution_message)
    if result.book is not None and result.page is not None:
        lines.append(f"الموضع: «{result.book.title}»، {_where(result.page, result.kind)}.")
    for change in result.changes[:10]:
        lines.append(f"- {change.type}: في النص «{change.quote}» / في الكتاب «{change.source}»")
    if result.citation:
        lines.append(f"الإحالة: {result.citation}")
    return "\n".join(lines)


def _cite_summary(result: Citation) -> str:
    return result.citation


# ====================================================================== the server


site_url = keys.site_url
public_url = keys.public_url


def build_server() -> MCPServer:
    """The MCP server with its five tools and the key verifier."""
    server = MCPServer(
        name="nassakh",
        title="Nassakh — search and quotation checking",
        description="Search printed Arabic books, check quotations word by word, cite the printed page.",
        instructions=INSTRUCTIONS,
        website_url=site_url(),
        version="1.0",
        token_verifier=KeyVerifier(),
        auth=AuthSettings(issuer_url=site_url(), resource_server_url=None),
    )

    @server.tool(
        name="list_books",
        title="List the books",
        description=(
            "List the books of this account: id, title, author, editor (المحقق), publisher, edition, number "
            "of pages, the printed page range and the share of the text a reviewer checked. Use the ids to "
            "narrow `search` or `verify_quote`."
        ),
        annotations=_annotations("List the books"),
    )
    async def list_books() -> Annotated[CallToolResult, BooksResult]:
        return await _call("list_books", services.list_books, _books_summary)

    @server.tool(
        name="search",
        title="Search the books",
        description=(
            "Search the Arabic text of the account's books. The query is normalised (diacritics, hamza "
            "forms, ta marbuta, alef maqsura, digits), then matched as a phrase, then as all words on a "
            "page, then approximately. Each hit gives the book, the printed page, whether it is the "
            "author's text (`body`) or the editor's notes (`notes`), the words around it, a passage_id, a "
            "clip link showing the printed lines and a ready citation."
        ),
        annotations=_annotations("Search the books"),
    )
    async def search(
        query: Annotated[
            str, Field(description="Arabic words or a phrase to find.", min_length=1, max_length=500)
        ],
        book_ids: Annotated[
            list[int] | None, Field(description="Only these books (ids from list_books); all when omitted.")
        ] = None,
        kind: Annotated[
            Literal["body", "notes", "all"],
            Field(description="`body`: the author's text; `notes`: the editor's footnotes; `all`: both."),
        ] = "all",
        limit: Annotated[int, Field(ge=1, le=SEARCH_LIMIT, description="Hits per call (at most 20).")] = 10,
        offset: Annotated[int, Field(ge=0, description="Skip this many hits (to page through).")] = 0,
    ) -> Annotated[CallToolResult, SearchResult]:
        return await _call(
            "search",
            lambda user: services.search(user, query, book_ids, kind, limit, offset),
            _search_summary,
        )

    @server.tool(
        name="get_passage",
        title="Read a passage",
        description=(
            "Read a passage with its context: by passage_id (from search or verify_quote), or a whole page "
            "by book_id and page (its place in the scan) or printed_page. Returns the author's text and the "
            "editor's notes apart, each line with its box on the page image, the doubtful words, the review "
            "state, clip links and the citation."
        ),
        annotations=_annotations("Read a passage"),
    )
    async def get_passage(
        passage_id: Annotated[
            str | None, Field(description="A passage_id from search or verify_quote.")
        ] = None,
        book_id: Annotated[
            int | None, Field(description="With page or printed_page: read a whole page.")
        ] = None,
        page: Annotated[
            int | None, Field(ge=1, description="The page's place in the scan (1 = first).")
        ] = None,
        printed_page: Annotated[str | None, Field(description="The page number printed on the page.")] = None,
        context: Annotated[int, Field(ge=0, le=10, description="Lines of context around the passage.")] = 2,
    ) -> Annotated[CallToolResult, Passage]:
        return await _call(
            "get_passage",
            lambda user: services.get_passage(user, passage_id, book_id, page, printed_page, context),
            _passage_summary,
        )

    @server.tool(
        name="verify_quote",
        title="Check a quotation",
        description=(
            "Check an Arabic quotation against the account's books, word by word. Status: `exact` (with "
            "`diacritics_differ` when only the vowels differ), `differs` (each change listed with the book's "
            "words), `needs_image_check` (the only differences fall on words the OCR read with doubt: check "
            "the clip, the quotation may be right), `not_found`, `too_short` (under 3 words). Give "
            "`attributed_to` = `author` when the quotation is presented as the author's words: a match in "
            "the editor's notes is then flagged (`note_not_author`). With `book_id`, a match found only in "
            "another book is flagged (`other_book`)."
        ),
        annotations=_annotations("Check a quotation"),
    )
    async def verify_quote(
        quote: Annotated[str, Field(description="The quotation, in Arabic.", min_length=1, max_length=4000)],
        book_id: Annotated[
            int | None, Field(description="The book it is said to come from (optional).")
        ] = None,
        attributed_to: Annotated[
            Literal["author", "editor", "unknown"],
            Field(description="Whose words the quotation is presented as."),
        ] = "unknown",
    ) -> Annotated[CallToolResult, VerifyResult]:
        return await _call(
            "verify_quote",
            lambda user: services.verify_quote(user, quote, book_id, attributed_to),
            _verify_summary,
        )

    @server.tool(
        name="cite",
        title="Cite a passage",
        description=(
            "The citation of a passage in Arabic: "
            "«المؤلف، العنوان، تحقيق: المحقق، الناشر، الطبعة، السنة، ص N», and its parts (the printed "
            "page; the place in the scan when the printed number is unknown)."
        ),
        annotations=_annotations("Cite a passage"),
    )
    async def cite(
        passage_id: Annotated[
            str, Field(description="A passage_id from search, get_passage or verify_quote.")
        ],
    ) -> Annotated[CallToolResult, Citation]:
        return await _call("cite", lambda user: services.cite(user, passage_id), _cite_summary)

    @server.custom_route("/healthz", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "mcp": MCP_PATH})

    return server


def transport_security() -> TransportSecuritySettings:
    """Hosts the server answers (DNS-rebinding protection): local ones, the public URL's, `ALLOWED_HOSTS`
    (`*` there turns the check off)."""
    allowed = list(settings.ALLOWED_HOSTS or [])
    if "*" in allowed:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = {"127.0.0.1", "localhost", "[::1]", "127.0.0.1:*", "localhost:*", "[::1]:*"}
    origins = {"http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"}
    public = urlsplit(public_url())
    names = [name.lstrip(".") for name in allowed if name]
    if public.hostname:
        names.append(public.hostname)
    for name in names:
        hosts |= {name, f"{name}:*"}
        origins |= {f"https://{name}", f"http://{name}", f"https://{name}:*", f"http://{name}:*"}
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=sorted(hosts), allowed_origins=sorted(origins)
    )


def build_app(server: MCPServer | None = None):
    """The ASGI app of `server` (a new `build_server()` when None): Streamable HTTP at `/mcp` (and, through
    `KeyInPath`, at `/mcp/k/<key>`), stateless, JSON responses, Bearer keys required. Its lifespan runs the
    server's session manager."""
    app = (server or build_server()).streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=transport_security(),
    )
    return KeyInPath(app)
