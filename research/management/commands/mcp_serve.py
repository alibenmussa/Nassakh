"""`manage.py mcp_serve [--host H] [--port P]`: run the MCP server (D108, `research.mcp_server`) with
uvicorn — Streamable HTTP at `/mcp`, stateless, JSON responses, access keys as Bearer tokens.

It runs beside Django (`make mcp`, Procfile `mcp:`); in production the same domain proxies `/mcp` to it.
Host and port default to `NASSAKH["MCP_HOST"]` / `NASSAKH["MCP_PORT"]` (env `MCP_HOST`, `MCP_PORT`).
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the MCP server (Streamable HTTP at /mcp) with uvicorn."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--host", default=None, help="interface to bind (default: MCP_HOST, 127.0.0.1)")
        parser.add_argument("--port", type=int, default=None, help="port (default: MCP_PORT, 8001)")
        parser.add_argument("--log-level", default="info", help="uvicorn log level (default: info)")

    def handle(self, *args, host=None, port=None, log_level="info", **options) -> None:
        import uvicorn

        from research.mcp_server import MCP_PATH, build_app, public_url

        host = host or settings.NASSAKH.get("MCP_HOST", "127.0.0.1")
        port = int(port or settings.NASSAKH.get("MCP_PORT", 8001))
        self.stdout.write(f"MCP server on http://{host}:{port}{MCP_PATH} (public URL: {public_url()})")
        uvicorn.run(build_app(), host=host, port=port, log_level=log_level, proxy_headers=True)
