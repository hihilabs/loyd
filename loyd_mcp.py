#!/usr/bin/env python3
"""
Loyd MCP server — Loyd's tools over Streamable HTTP for any MCP client
(Claude Desktop, the Claude app, VS Code, opencode), driven by full Claude on
your subscription instead of the metered API.

Thin wrapper over tools.py (TOOL_SCHEMAS + execute_tool + WRITE_TOOLS). Acts
wherever LOYD_REMOTE points, exactly like the CLI — so run it on the public VPS
with LOYD_REMOTE=root@tokyo7 to drive Tokyo7 from your car / Steam Deck / laptop.

SECURITY — this exposes run_shell / write_file / restart_container / prune_images.
Internet-exposed without auth is catastrophic, so:
  * Bearer-token auth is REQUIRED. Configure users via LOYD_MCP_TOKENS:
        LOYD_MCP_TOKENS="alice:o:secret1,bob:r:secret2"
    each entry is  user:role:token  — role r = read-only, o = operator.
    Send it as `Authorization: Bearer <token>` (preferred) or, for clients with no
    header field (the claude.ai web modal), `?key=<token>` on the URL — a query
    token lands in access logs, so use it only where headers aren't settable.
  * Read/inspect tools are offered to any valid token; the write tools are
    offered and runnable ONLY to operators. The MCP client also prompts the
    user to approve each individual tool call — that's the second gate.
  * With NO tokens set the server refuses to bind a public interface (fail-closed)
    and 401s every request on localhost.
  * Terminate TLS in front of it (cloudflared / tailscale-serve / reverse proxy).
    Never run plain HTTP on a public interface — the bearer token is the keys.

Run:   pip install mcp uvicorn
       LOYD_MCP_TOKENS="me:o:$(openssl rand -hex 16)" python3 loyd_mcp.py
Env:   LOYD_MCP_HOST (default 127.0.0.1)  LOYD_MCP_PORT (default 8848)
       LOYD_MCP_PATH (default /mcp)        LOYD_REMOTE / LOYD_REMOTE_PORT to act on
                                           another host (no ollama here — the brain
                                           is whatever Claude client connects)
"""
import contextlib
import contextvars
import os
import sys
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tools import TOOL_SCHEMAS, WRITE_TOOLS, execute_tool, where  # noqa: E402

import anyio                                                       # noqa: E402
import mcp.types as mt                                             # noqa: E402
import uvicorn                                                     # noqa: E402
from mcp.server.lowlevel import Server                             # noqa: E402
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402
from starlette.applications import Starlette                       # noqa: E402
from starlette.responses import PlainTextResponse                  # noqa: E402
from starlette.routing import Mount                                # noqa: E402

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _load_tokens():
    """Parse LOYD_MCP_TOKENS 'user:role:token,...' -> {token: {user, operator}}."""
    users = {}
    for entry in os.getenv("LOYD_MCP_TOKENS", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            raise SystemExit(f"LOYD_MCP_TOKENS entry must be user:role:token — got {entry!r}")
        user, role, token = (p.strip() for p in parts)
        if role.lower() not in ("r", "o") or not token or not user:
            raise SystemExit(f"bad LOYD_MCP_TOKENS entry {entry!r} (role must be r or o)")
        users[token] = {"user": user, "operator": role.lower() == "o"}
    return users


TOKENS = _load_tokens()
_current = contextvars.ContextVar("loyd_mcp_current", default=None)   # per-request identity

server = Server("loyd")


def _tools_for(operator: bool):
    out = []
    for s in TOOL_SCHEMAS:
        f = s["function"]
        if f["name"] in WRITE_TOOLS and not operator:
            continue                                    # hide write tools from read-only users
        out.append(mt.Tool(
            name=f["name"], description=f.get("description", ""),
            inputSchema=f.get("parameters") or {"type": "object", "properties": {}}))
    return out


@server.list_tools()
async def list_tools():
    cur = _current.get() or {"operator": False}
    return _tools_for(cur["operator"])


@server.call_tool()
async def call_tool(name, arguments):
    cur = _current.get() or {"user": "?", "operator": False}
    if name in WRITE_TOOLS and not cur["operator"]:
        return [mt.TextContent(type="text",
                text=f"DENIED: '{name}' is an operator-only write tool; {cur['user']} is read-only.")]
    # execute_tool is blocking (subprocess/SSH) — run off the event loop
    result = await anyio.to_thread.run_sync(lambda: execute_tool(name, arguments or {}))
    return [mt.TextContent(type="text", text=result)]


_sessions = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)


async def _mcp_asgi(scope, receive, send):
    await _sessions.handle_request(scope, receive, send)


class BearerAuth:
    """ASGI wrapper: require a valid bearer token, stash identity for the handlers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":                     # let lifespan/websocket pass through
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if token not in TOKENS:
            # fallback: ?key=<token> — for the claude.ai web modal, which takes only a
            # URL (no header field). Header is preferred; a query token lands in access
            # logs, so use it only where headers aren't settable.
            qs = parse_qs(scope.get("query_string", b"").decode())
            token = (qs.get("key") or [""])[0].strip() or token
        ident = TOKENS.get(token)
        if ident is None:
            resp = PlainTextResponse("401 unauthorized: valid bearer token required\n",
                                     status_code=401, headers={"WWW-Authenticate": "Bearer"})
            return await resp(scope, receive, send)
        reset = _current.set(ident)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(reset)


@contextlib.asynccontextmanager
async def _lifespan(_app):
    async with _sessions.run():
        yield


PATH = os.getenv("LOYD_MCP_PATH", "/mcp")
app = BearerAuth(Starlette(routes=[Mount(PATH, app=_mcp_asgi)], lifespan=_lifespan))


def main():
    host = os.getenv("LOYD_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("LOYD_MCP_PORT", "8848"))
    if not TOKENS and host not in _LOCAL_HOSTS:
        raise SystemExit("REFUSING to bind a public interface with no LOYD_MCP_TOKENS set. "
                         "Set LOYD_MCP_TOKENS='user:role:token,...' or bind 127.0.0.1.")
    if not TOKENS:
        print("WARNING: no LOYD_MCP_TOKENS — every request 401s. Set tokens to use it.", file=sys.stderr)
    ops = sum(v["operator"] for v in TOKENS.values())
    print(f"Loyd MCP · acts={where()} · {len(TOKENS)} users ({ops} operator) · "
          f"listening http://{host}:{port}{PATH}", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
