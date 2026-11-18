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

OAUTH — so mobile/desktop/web "Connectors" GUIs (which only know no-auth or
real OAuth, never a bare static token) can connect, not just CLI/.mcp.json
clients that set a raw Authorization header. Still NOT a user-account system:
/authorize asks for one of the same LOYD_MCP_TOKENS values. What it hands the
client is a short-lived access token + rolling refresh token HMAC-signed with a
key derived from that static token — so the master token never leaves the box,
a connection can be scoped read-only at consent, and rotating the static token
revokes every grant minted from it. Fully stateless (signed client_ids, signed
tokens): restarts don't log connectors out. Spec coverage: RFC 8414 metadata,
RFC 7591 dynamic registration, RFC 9728 protected-resource metadata, RFC 6749
code + refresh grants, PKCE/S256 (RFC 7636), iss in the redirect (RFC 9207).

Run:   pip install mcp uvicorn
       LOYD_MCP_TOKENS="me:o:$(openssl rand -hex 16)" python3 loyd_mcp.py
Env:   LOYD_MCP_HOST (default 127.0.0.1)  LOYD_MCP_PORT (default 8848)
       LOYD_MCP_PUBLIC_URL (pin OAuth issuer, e.g. https://loyd.hihilabs.xyz)
       LOYD_MCP_ACCESS_TTL (3600) LOYD_MCP_REFRESH_TTL (2592000)
       LOYD_MCP_OAUTH_SECRET (optional; signs client_ids, else derived from tokens)
       LOYD_MCP_PATH (default /mcp)        LOYD_REMOTE / LOYD_REMOTE_PORT to act on
                                           another host (no ollama here — the brain
                                           is whatever Claude client connects)
"""
import base64
import contextlib
import contextvars
import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Full Claude on the other end can digest far more than the local 7B the CLI caps
# for — raise the trim limits unless the operator pinned them.
os.environ.setdefault("LOYD_MAX_OUTPUT_CHARS", "20000")
os.environ.setdefault("LOYD_MAX_OUTPUT_LINES", "400")
from tools import TOOL_SCHEMAS, WRITE_TOOLS, execute_tool, where  # noqa: E402

import anyio                                                       # noqa: E402
import mcp.types as mt                                             # noqa: E402
import uvicorn                                                     # noqa: E402
from mcp.server.lowlevel import Server                             # noqa: E402
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402
from starlette.applications import Starlette                       # noqa: E402
from starlette.middleware import Middleware                        # noqa: E402
from starlette.middleware.cors import CORSMiddleware                # noqa: E402
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
    # readonly=True makes execute_tool deny write tools AND run run_shell/read_file
    # under read-only rules even when the server itself runs LOYD_MODE=operator —
    # without it a read-only token got the operator shell. Every call is audited.
    actor, ro = f"mcp:{cur['user']}", not cur["operator"]
    # execute_tool is blocking (subprocess/SSH) — run off the event loop
    result = await anyio.to_thread.run_sync(
        lambda: execute_tool(name, arguments or {}, actor=actor, readonly=ro))
    return [mt.TextContent(type="text", text=result)]


_sessions = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)


async def _mcp_asgi(scope, receive, send):
    await _sessions.handle_request(scope, receive, send)


PATH = os.getenv("LOYD_MCP_PATH", "/mcp")
# Pin the public origin (e.g. https://loyd.hihilabs.xyz) instead of reflecting the
# Host header — recommended now that there's one public door; OAuth issuer/endpoint
# URLs must match it exactly.
PUBLIC_URL = os.getenv("LOYD_MCP_PUBLIC_URL", "").rstrip("/")


def _base_url(request_or_scope):
    """https://<host as the client sees it>, reflected from the request's own Host
    header rather than one hardcoded value — this same process is reachable
    behind several different public URLs (funnel :10000, tailnet :8543/:8444),
    and OAuth's issuer/endpoint URLs need to match whichever one the client
    actually used. Always https: this process only ever receives plain HTTP
    from the local tailscale serve/funnel proxy that already terminated TLS."""
    if PUBLIC_URL:
        return PUBLIC_URL
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
        ident = identity_for(token)
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


# ── OAuth (RFC 8414 / 7591 / 9728 / 6749 refresh + PKCE) — a spec-shaped layer
# over the SAME static LOYD_MCP_TOKENS, for GUI "Connectors" clients that only
# speak no-auth or real OAuth.
#
# Everything here is STATELESS, so a restart (or `/update`) never logs a
# connector out and there's no DB to keep:
#   * client_id      = signed blob carrying the registered redirect_uris
#                      (server key, see _server_key) — /authorize and /token
#                      verify the signature, so redirect_uri can't be swapped.
#   * access/refresh = signed blob {user, kind, read-only?, client, exp}, HMAC'd
#                      with a key derived from THAT USER'S static token. The
#                      static token itself never leaves the box; rotating it in
#                      LOYD_MCP_TOKENS instantly revokes every OAuth grant
#                      minted from it. Nothing to keep in sync.
# The one bit of state is auth codes (5-min, single-use) — losing them on
# restart just means "click Authorize again".
_AUTH_CODE_TTL = 300
ACCESS_TTL = int(os.getenv("LOYD_MCP_ACCESS_TTL", "3600"))               # 1h
REFRESH_TTL = int(os.getenv("LOYD_MCP_REFRESH_TTL", str(30 * 86400)))    # 30d, rolling
_FAIL_WINDOW, _FAIL_MAX = 600, 8      # /authorize: ≤8 bad tokens per IP per 10 min
_auth_codes = {}                      # code -> {token, ro, code_challenge, redirect_uri, client_id, expires}
_auth_failures = {}                   # ip -> [timestamps]
_TOKEN_PREFIX, _CLIENT_PREFIX = "loyd1.", "lc1."


def _server_key() -> bytes:
    """Signs client_ids. LOYD_MCP_OAUTH_SECRET if set, else derived from the
    token set — stable across restarts, and changes (invalidating registrations,
    which clients transparently redo) only when the tokens themselves change."""
    secret = os.getenv("LOYD_MCP_OAUTH_SECRET", "")
    material = secret or "|".join(sorted(TOKENS))
    return hashlib.sha256(b"loyd-oauth-client-v1:" + material.encode()).digest()


def _user_key(static_token: str) -> bytes:
    return hashlib.sha256(b"loyd-oauth-token-v1:" + static_token.encode()).digest()


def _sign(prefix: str, payload: dict, key: bytes) -> str:
    body = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = _b64url(hmac.new(key, (prefix + body).encode(), hashlib.sha256).digest())
    return f"{prefix}{body}.{mac}"


def _unsign(prefix: str, blob: str, key_for):
    """Return the payload if `blob` is a validly-signed `prefix` blob, else None.
    key_for(payload) -> key or None (lets the key depend on who it claims to be)."""
    if not blob or not blob.startswith(prefix) or blob.count(".") < 2:
        return None
    body, _, mac = blob[len(prefix):].rpartition(".")
    try:
        payload = json.loads(_b64dec(body))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    key = key_for(payload)
    if key is None:
        return None
    good = _b64url(hmac.new(key, (prefix + body).encode(), hashlib.sha256).digest())
    return payload if hmac.compare_digest(good, mac) else None


def _b64dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _static_tokens_for(user: str):
    return [t for t, v in TOKENS.items() if v["user"] == user]


def _mint(static_token: str, kind: str, ro: bool, cfp: str, ttl: int) -> str:
    user = TOKENS[static_token]["user"]
    return _sign(_TOKEN_PREFIX, {
        "u": user, "k": kind, "ro": bool(ro), "c": cfp,
        "exp": int(time.time()) + ttl, "n": secrets.token_urlsafe(6),
    }, _user_key(static_token))


def _client_fingerprint(client_id: str) -> str:
    return hashlib.sha256(client_id.encode()).hexdigest()[:16]


def _verify_minted(blob: str, kind: str):
    """-> (static_token, payload) for a live, correctly-signed token of `kind`, else None.
    Tries each static token belonging to the claimed user, so a user with
    several tokens works and a rotated-away token no longer verifies."""
    hit = []

    def key_for(p):
        for st in _static_tokens_for(str(p.get("u", ""))):
            if _unsign(_TOKEN_PREFIX, blob, lambda _p, k=_user_key(st): k) is not None:
                hit.append(st)
                return _user_key(st)
        return None

    p = _unsign(_TOKEN_PREFIX, blob, key_for)
    if p is None or p.get("k") != kind or int(p.get("exp", 0)) < time.time():
        return None
    return hit[0], p


def identity_for(token: str):
    """Bearer token -> {user, operator} or None. Accepts a static LOYD_MCP_TOKENS
    value (CLI / config-file clients) or a live OAuth access token."""
    if token in TOKENS:
        return TOKENS[token]
    v = _verify_minted(token, "a")
    if v is None:
        return None
    st, p = v
    base = TOKENS[st]
    return {"user": base["user"], "operator": base["operator"] and not p.get("ro")}


def _redirect_ok(uri: str) -> bool:
    """https anywhere, http only to loopback (native apps, RFC 8252), or a
    private-use app scheme (com.example.app:/cb). Never javascript:/data:/etc."""
    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    if u.fragment:
        return False
    if u.scheme == "https":
        return bool(u.netloc)
    if u.scheme == "http":
        return (u.hostname or "") in _LOCAL_HOSTS
    return "." in u.scheme and u.scheme not in ("javascript", "data", "file", "vbscript")


def _client(client_id: str):
    return _unsign(_CLIENT_PREFIX, client_id, lambda _p: _server_key())


def _client_ip(request: Request) -> str:
    # This binds 127.0.0.1 behind nginx/Cloudflare, so the forwarded headers are
    # the real client; direct hits fall back to the socket peer.
    h = request.headers
    return (h.get("cf-connecting-ip") or h.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


def _throttled(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _auth_failures.get(ip, []) if now - t < _FAIL_WINDOW]
    _auth_failures[ip] = recent
    return len(recent) >= _FAIL_MAX


def _prune_expired_codes():
    now = time.time()
    for code in [c for c, v in _auth_codes.items() if v["expires"] < now]:
        _auth_codes.pop(code, None)


def _with_query(uri: str, params: dict) -> str:
    sep = "&" if urlsplit(uri).query else "?"
    return f"{uri}{sep}{urlencode(params)}"


_NO_FRAME = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'",
             "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


async def oauth_protected_resource(request: Request):
    base = _base_url(request)
    return JSONResponse({
        "resource": f"{base}{PATH}",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["loyd"],
    })


async def oauth_authorization_server(request: Request):
    base = _base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["loyd"],
        "authorization_response_iss_parameter_supported": True,
    })


async def oauth_register(request: Request):
    """Dynamic Client Registration (RFC 7591) — pre-auth by design: this is how a
    client learns its client_id, not how a human authenticates. Public clients
    (no secret) + PKCE. The client_id is a signed record of the redirect_uris,
    so nothing is stored and a restart doesn't orphan the registration."""
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_client_metadata",
                             "error_description": "JSON object body required"}, status_code=400)
    uris = body.get("redirect_uris")
    if (not isinstance(uris, list) or not uris or len(uris) > 10
            or not all(isinstance(u, str) and len(u) < 512 and _redirect_ok(u) for u in uris)):
        return JSONResponse({"error": "invalid_redirect_uri",
                             "error_description": "1-10 redirect_uris required: https, loopback http, "
                                                  "or a private-use app scheme"}, status_code=400)
    name = str(body.get("client_name") or "")[:64]
    client_id = _sign(_CLIENT_PREFIX, {"r": uris, "n": name, "iat": int(time.time()), "j": secrets.token_urlsafe(6)}, _server_key())
    out = {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    if name:
        out["client_name"] = name
    return JSONResponse(out, status_code=201)


_AUTHORIZE_FORM = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect to Loyd</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,'Inter',sans-serif;background:#0a0f1a;min-height:100vh;
  display:flex;align-items:center;justify-content:center;padding:16px}}
.card{{width:100%;max-width:380px;padding:32px;background:#111827;border:1px solid #1e2d42;
  border-radius:16px;box-shadow:0 24px 60px rgba(0,0,0,.5)}}
.logo{{width:52px;height:52px;border-radius:12px;background:#7c6af7;color:#fff;
  display:flex;align-items:center;justify-content:center;font-weight:700;font-size:22px;
  margin:0 auto 18px}}
h1{{color:#e6edf3;font-size:16px;text-align:center;margin-bottom:4px}}
p{{color:#8b949e;font-size:12px;text-align:center;margin-bottom:22px;line-height:1.5}}
p b{{color:#e6edf3}}
label{{display:block;font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;
  letter-spacing:.05em;margin-bottom:6px}}
input[type=password]{{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:8px;
  padding:11px 14px;color:#e6edf3;font-size:14px;font-family:inherit;outline:none}}
input[type=password]:focus{{border-color:#7c6af7}}
.ro{{display:flex;gap:8px;align-items:center;margin-top:14px;color:#8b949e;font-size:12px;
  text-transform:none;letter-spacing:0;font-weight:400}}
button{{width:100%;margin-top:16px;background:#7c6af7;color:#fff;border:none;
  border-radius:8px;padding:12px;font-size:14px;font-weight:600;cursor:pointer}}
button:hover{{background:#8f80f9}}
.err{{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);color:#f85149;
  border-radius:8px;padding:9px 12px;font-size:12px;margin-bottom:16px}}
</style></head><body>
<div class="card">
  <div class="logo">L</div>
  <h1>Connect to Loyd</h1>
  <p><b>{client_desc}</b> wants access to Loyd's tools.<br>
     After you authorize, you'll be sent to <b>{redirect_host}</b>.<br>
     Only continue if you started this connection.</p>
  {error_html}
  <form method="post">
    <label for="token">Loyd Access Token</label>
    <input type="password" id="token" name="token" autofocus autocomplete="off" required>
    <label class="ro"><input type="checkbox" name="readonly" value="1"> Read-only (no write tools; read-only shell)</label>
    <input type="hidden" name="params" value="{params}">
    <button type="submit">Authorize</button>
  </form>
</div>
</body></html>"""


def _render_form(client, redirect_uri, params_blob, error="", status=200):
    host = urlsplit(redirect_uri).netloc or urlsplit(redirect_uri).scheme
    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    return HTMLResponse(_AUTHORIZE_FORM.format(
        client_desc=html.escape(client.get("n") or "An app"),
        redirect_host=html.escape(host), error_html=err, params=html.escape(params_blob)),
        status_code=status, headers=_NO_FRAME)


def _check_authorize_params(p):
    """-> (client, error_response). Per RFC 6749 §4.1.2.1, a bad client_id or
    redirect_uri is shown to the user, NEVER redirected (that's the open-redirect hole)."""
    client = _client(p.get("client_id", ""))
    if client is None:
        return None, PlainTextResponse("invalid_client: unknown or tampered client_id — "
                                       "remove and re-add the connector", status_code=400)
    if p.get("redirect_uri", "") not in client["r"]:
        return None, PlainTextResponse("invalid_request: redirect_uri not registered for this client",
                                       status_code=400)
    if (p.get("response_type", "code") != "code" or not p.get("code_challenge")
            or p.get("code_challenge_method", "S256") != "S256"):
        return None, PlainTextResponse("invalid_request: response_type=code and an S256 "
                                       "code_challenge are required", status_code=400)
    return client, None


async def oauth_authorize(request: Request):
    """GET renders the consent form; POST checks the pasted token (same TOKENS
    map BearerAuth uses), then mints a 5-min single-use code bound to that
    token, this client, its redirect_uri, and the PKCE challenge. POST takes its
    params only from the form's hidden field, re-validated from scratch."""
    if request.method == "GET":
        q = dict(request.query_params)
        client, err = _check_authorize_params(q)
        if err:
            return err
        blob = urlencode({k: q.get(k, "") for k in
                          ("client_id", "redirect_uri", "state", "code_challenge")})
        return _render_form(client, q["redirect_uri"], blob)

    form = await request.form()
    blob = form.get("params") or ""
    saved = {k: v[0] for k, v in parse_qs(blob).items()}
    client, err = _check_authorize_params(saved)
    if err:
        return err

    ip = _client_ip(request)
    if _throttled(ip):
        return _render_form(client, saved["redirect_uri"], blob,
                            "Too many wrong tokens — wait 10 minutes and try again.", 429)
    token = (form.get("token") or "").strip()
    if token not in TOKENS:
        _auth_failures.setdefault(ip, []).append(time.time())
        return _render_form(client, saved["redirect_uri"], blob,
                            "That token wasn't recognized — check it and try again.", 401)

    _prune_expired_codes()
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "token": token,
        "ro": form.get("readonly") == "1",
        "code_challenge": saved["code_challenge"],
        "redirect_uri": saved["redirect_uri"],
        "client_id": saved["client_id"],
        "expires": time.time() + _AUTH_CODE_TTL,
    }
    params = {"code": code}
    if saved.get("state"):
        params["state"] = saved["state"]
    params["iss"] = _base_url(request)                      # RFC 9207 mix-up defense
    return RedirectResponse(_with_query(saved["redirect_uri"], params), status_code=302,
                            headers={"Cache-Control": "no-store"})


def _token_error(error, desc="", status=400):
    body = {"error": error}
    if desc:
        body["error_description"] = desc
    return JSONResponse(body, status_code=status,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _token_response(static_token, ro, cfp):
    return JSONResponse({
        "access_token": _mint(static_token, "a", ro, cfp, ACCESS_TTL),
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "refresh_token": _mint(static_token, "r", ro, cfp, REFRESH_TTL),
        "scope": "loyd",
    }, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


async def oauth_token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")
    client_id = form.get("client_id", "")

    if grant_type == "authorization_code":
        code = form.get("code", "")
        verifier = form.get("code_verifier", "")
        _prune_expired_codes()
        entry = _auth_codes.pop(code, None)   # single-use: pop, not get
        if entry is None or entry["expires"] < time.time():
            return _token_error("invalid_grant", "code invalid, used, or expired")
        if client_id and client_id != entry["client_id"]:
            return _token_error("invalid_grant", "client_id mismatch")
        if entry["redirect_uri"] != form.get("redirect_uri", entry["redirect_uri"]):
            return _token_error("invalid_grant", "redirect_uri mismatch")
        if not (43 <= len(verifier) <= 128):
            return _token_error("invalid_grant", "code_verifier must be 43-128 chars")
        expected = _b64url(hashlib.sha256(verifier.encode()).digest())
        if not hmac.compare_digest(expected, entry["code_challenge"]):
            return _token_error("invalid_grant", "PKCE verification failed")
        if entry["token"] not in TOKENS:                 # rotated away mid-flow
            return _token_error("invalid_grant", "token no longer valid")
        return _token_response(entry["token"], entry["ro"], _client_fingerprint(entry["client_id"]))

    if grant_type == "refresh_token":
        v = _verify_minted(form.get("refresh_token", ""), "r")
        if v is None:
            return _token_error("invalid_grant", "refresh_token invalid, expired, or revoked")
        st, p = v
        if client_id and _client_fingerprint(client_id) != p.get("c"):
            return _token_error("invalid_grant", "refresh_token was issued to another client")
        # rolling refresh: a fresh pair each time, bound to the same client,
        # and a read-only grant stays read-only
        return _token_response(st, p.get("ro"), p.get("c", ""))

    return _token_error("unsupported_grant_type")


app = Starlette(
    routes=[
        Route(f"/.well-known/oauth-protected-resource{PATH}", oauth_protected_resource),
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
        Route("/.well-known/oauth-authorization-server", oauth_authorization_server),
        Route("/register", oauth_register, methods=["POST"]),
        Route("/authorize", oauth_authorize, methods=["GET", "POST"]),
        Route("/token", oauth_token, methods=["POST"]),
        Mount(PATH, app=BearerAuth(_mcp_asgi)),
    ],
    lifespan=_lifespan,
    # A Connectors GUI likely does registration/discovery from a browser-based
    # renderer, unlike every curl/http.client test used to verify this — those
    # don't enforce CORS at all, so a preflight-blocked fetch there would look
    # identical to success in this codebase's own testing but fail silently in
    # the real client. No auth/security tradeoff here: CORS only gates whether
    # browser JS may READ a response, never whether the request reaches the
    # server — BearerAuth is the actual access-control boundary, unaffected.
    middleware=[Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])],
)


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
