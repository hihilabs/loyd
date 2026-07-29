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

OAUTH — added so mobile/desktop "Connectors" GUIs (which only know no-auth or
real OAuth, never a bare static token) can connect too, not just CLI/.mcp.json
clients that can set a raw Authorization header. This is deliberately NOT a
real user-account system: /authorize renders a one-field form asking for one
of the same LOYD_MCP_TOKENS values, and the "access_token" the flow hands
back to the client IS that static token, unchanged — BearerAuth downstream
needs zero new logic. OAuth here is just spec-shaped plumbing (RFC 8414
metadata, RFC 7591 dynamic client registration, RFC 9728 protected-resource
metadata, PKCE/S256 per the MCP auth spec) wrapped around a login form, not a
second credential system to keep in sync with the first.

Run:   pip install mcp uvicorn
       LOYD_MCP_TOKENS="me:o:$(openssl rand -hex 16)" python3 loyd_mcp.py
Env:   LOYD_MCP_HOST (default 127.0.0.1)  LOYD_MCP_PORT (default 8848)
       LOYD_MCP_PATH (default /mcp)        LOYD_REMOTE / LOYD_REMOTE_PORT to act on
                                           another host (no ollama here — the brain
                                           is whatever Claude client connects)
"""
import base64
import contextlib
import contextvars
import hashlib
import html
import os
import secrets
import sys
import time
from urllib.parse import parse_qs, urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tools import TOOL_SCHEMAS, WRITE_TOOLS, execute_tool, where  # noqa: E402

import anyio                                                       # noqa: E402
import mcp.types as mt                                             # noqa: E402
import uvicorn                                                     # noqa: E402
from mcp.server.lowlevel import Server                             # noqa: E402
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402
from starlette.applications import Starlette                       # noqa: E402
from starlette.requests import Request                             # noqa: E402
from starlette.responses import (HTMLResponse, JSONResponse,       # noqa: E402
                                  PlainTextResponse, RedirectResponse)
from starlette.routing import Mount, Route                         # noqa: E402

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


PATH = os.getenv("LOYD_MCP_PATH", "/mcp")


def _base_url(request_or_scope):
    """https://<host as the client sees it>, reflected from the request's own Host
    header rather than one hardcoded value — this same process is reachable
    behind several different public URLs (funnel :10000, tailnet :8543/:8444),
    and OAuth's issuer/endpoint URLs need to match whichever one the client
    actually used. Always https: this process only ever receives plain HTTP
    from the local tailscale serve/funnel proxy that already terminated TLS."""
    headers = getattr(request_or_scope, "headers", None)
    if headers is None:
        headers = {k.decode().lower(): v.decode() for k, v in request_or_scope.get("headers", [])}
    host = headers.get("host", "127.0.0.1")
    return f"https://{host}"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


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
            resource_meta = f"{_base_url(scope)}/.well-known/oauth-protected-resource{PATH}"
            resp = PlainTextResponse(
                "401 unauthorized: valid bearer token required\n", status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{resource_meta}"'})
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


# ── OAuth (RFC 8414 / 7591 / 9728 + PKCE) — a spec-shaped wrapper around the
# SAME static LOYD_MCP_TOKENS, for GUI "Connectors" clients that only speak
# no-auth or real OAuth. See the OAUTH module docstring section for why the
# "access_token" this hands back is literally just the static token. ──────────
_AUTH_CODE_TTL = 300          # seconds a code is valid for, single-use
_oauth_clients = {}           # client_id -> {"redirect_uris": [...]}
_auth_codes = {}              # code -> {token, code_challenge, redirect_uri, client_id, expires}


def _prune_expired_codes():
    now = time.time()
    for code in [c for c, v in _auth_codes.items() if v["expires"] < now]:
        _auth_codes.pop(code, None)


async def oauth_protected_resource(request: Request):
    base = _base_url(request)
    return JSONResponse({
        "resource": f"{base}{PATH}",
        "authorization_servers": [base],
    })


async def oauth_authorization_server(request: Request):
    base = _base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def oauth_register(request: Request):
    """Dynamic Client Registration (RFC 7591) — pre-auth by design, this is
    how a client learns its client_id, not how a human authenticates. No
    client_secret: these are public clients (mobile/desktop apps) using PKCE,
    same pattern every "public client, no secret" OAuth deployment uses."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    client_id = secrets.token_urlsafe(16)
    redirect_uris = body.get("redirect_uris") or []
    _oauth_clients[client_id] = {"redirect_uris": redirect_uris}
    return JSONResponse({
        "client_id": client_id,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }, status_code=201)


_AUTHORIZE_FORM = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect to Loyd</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,'Inter',sans-serif;background:#0a0f1a;min-height:100vh;
  display:flex;align-items:center;justify-content:center}}
.card{{width:100%;max-width:380px;padding:32px;background:#111827;border:1px solid #1e2d42;
  border-radius:16px;box-shadow:0 24px 60px rgba(0,0,0,.5)}}
.logo{{width:52px;height:52px;border-radius:12px;background:#7c6af7;color:#fff;
  display:flex;align-items:center;justify-content:center;font-weight:700;font-size:22px;
  margin:0 auto 18px}}
h1{{color:#e6edf3;font-size:16px;text-align:center;margin-bottom:4px}}
p{{color:#8b949e;font-size:12px;text-align:center;margin-bottom:22px}}
label{{display:block;font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;
  letter-spacing:.05em;margin-bottom:6px}}
input{{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:8px;
  padding:11px 14px;color:#e6edf3;font-size:14px;font-family:inherit;outline:none}}
input:focus{{border-color:#7c6af7}}
button{{width:100%;margin-top:16px;background:#7c6af7;color:#fff;border:none;
  border-radius:8px;padding:12px;font-size:14px;font-weight:600;cursor:pointer}}
button:hover{{background:#8f80f9}}
.err{{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);color:#f85149;
  border-radius:8px;padding:9px 12px;font-size:12px;margin-bottom:16px}}
</style></head><body>
<div class="card">
  <div class="logo">L</div>
  <h1>Connect to Loyd</h1>
  <p>{client_desc} is requesting access. Paste one of your Loyd tokens below.</p>
  {error_html}
  <form method="post">
    <label for="token">Loyd Access Token</label>
    <input type="password" id="token" name="token" autofocus autocomplete="off" required>
    <input type="hidden" name="params" value="{params}">
    <button type="submit">Authorize</button>
  </form>
</div>
</body></html>"""


async def oauth_authorize(request: Request):
    """GET renders the form; POST validates the token against TOKENS (the
    SAME check BearerAuth does) and, on success, mints a short-lived
    single-use code bound to that token + this client's PKCE challenge.
    GET's params come from the URL query string; POST's come from the
    'params' hidden field the form round-trips — POST never re-reads the
    query string, which is empty on a same-origin form POST anyway."""
    if request.method == "GET":
        q = request.query_params
        response_type = q.get("response_type", "code")
        client_id = q.get("client_id", "")
        redirect_uri = q.get("redirect_uri", "")
        state = q.get("state", "")
        code_challenge = q.get("code_challenge", "")
        code_challenge_method = q.get("code_challenge_method", "S256")

        if response_type != "code" or not redirect_uri or code_challenge_method != "S256" or not code_challenge:
            return PlainTextResponse(
                "invalid_request: response_type=code, redirect_uri, and S256 code_challenge are required",
                status_code=400)

        params_blob = urlencode({
            "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
            "code_challenge": code_challenge,
        })
        client_desc = html.escape(client_id[:24] or "An app")
        return HTMLResponse(_AUTHORIZE_FORM.format(
            client_desc=client_desc, error_html="", params=html.escape(params_blob)))

    form = await request.form()
    token = (form.get("token") or "").strip()
    saved = dict(parse_qs(form.get("params") or ""))
    saved = {k: v[0] for k, v in saved.items()}

    if token not in TOKENS:
        client_desc = html.escape(saved.get("client_id", "")[:24] or "An app")
        err = '<div class="err">That token wasn\'t recognized — check it and try again.</div>'
        return HTMLResponse(
            _AUTHORIZE_FORM.format(client_desc=client_desc, error_html=err,
                                    params=html.escape(form.get("params") or "")),
            status_code=401)

    _prune_expired_codes()
    code = secrets.token_urlsafe(24)
    _auth_codes[code] = {
        "token": token,
        "code_challenge": saved.get("code_challenge", ""),
        "redirect_uri": saved.get("redirect_uri", ""),
        "client_id": saved.get("client_id", ""),
        "expires": time.time() + _AUTH_CODE_TTL,
    }
    redirect = f"{saved.get('redirect_uri')}?{urlencode({'code': code, 'state': saved.get('state', '')})}"
    return RedirectResponse(redirect, status_code=302)


async def oauth_token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    code = form.get("code", "")
    verifier = form.get("code_verifier", "")
    redirect_uri = form.get("redirect_uri", "")

    _prune_expired_codes()
    entry = _auth_codes.pop(code, None)   # single-use: pop, not get
    if entry is None or entry["expires"] < time.time():
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if entry["redirect_uri"] != redirect_uri:
        return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"},
                             status_code=400)
    expected_challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    if not secrets.compare_digest(expected_challenge, entry["code_challenge"]):
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"},
                             status_code=400)

    return JSONResponse({
        "access_token": entry["token"],     # literally the static LOYD_MCP_TOKENS value
        "token_type": "Bearer",
        "expires_in": 31536000,             # cosmetic — really lives/dies with the static token
    })


app = Starlette(routes=[
    Route(f"/.well-known/oauth-protected-resource{PATH}", oauth_protected_resource),
    Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
    Route("/.well-known/oauth-authorization-server", oauth_authorization_server),
    Route("/register", oauth_register, methods=["POST"]),
    Route("/authorize", oauth_authorize, methods=["GET", "POST"]),
    Route("/token", oauth_token, methods=["POST"]),
    Mount(PATH, app=BearerAuth(_mcp_asgi)),
], lifespan=_lifespan)


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
    # LOYD_MCP_LOG_LEVEL=info surfaces per-request access logs — invaluable when a
    # client (e.g. a hosted connector) says "can't reach" and you need to know
    # whether its request arrives at all, and with what status.
    uvicorn.run(app, host=host, port=port,
                log_level=os.getenv("LOYD_MCP_LOG_LEVEL", "warning"))


if __name__ == "__main__":
    main()
