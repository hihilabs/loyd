"""OAuth flow tests for loyd_mcp — run:  python3 -m pytest -q tests/"""
import base64
import hashlib
import importlib
import os
import secrets
import sys
import time
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OP, RO = "op-" + "a" * 20, "ro-" + "b" * 20
os.environ["LOYD_MCP_TOKENS"] = f"me:o:{OP},mate:r:{RO}"
os.environ["LOYD_MCP_PUBLIC_URL"] = "https://loyd.example"

from starlette.testclient import TestClient  # noqa: E402

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
MCP_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}}}
MCP_HDRS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture()
def m():
    import loyd_mcp
    mod = importlib.reload(loyd_mcp)
    with TestClient(mod.app, base_url="http://testserver", follow_redirects=False) as c:
        mod.client = c
        yield mod


def _pkce():
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def _register(m, uris=(REDIRECT,)):
    r = m.client.post("/register", json={"redirect_uris": list(uris), "client_name": "Claude"})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _authorize(m, cid, token, challenge, readonly=False, redirect=REDIRECT, state="xyz"):
    q = {"response_type": "code", "client_id": cid, "redirect_uri": redirect, "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256"}
    r = m.client.get("/authorize", params=q)
    assert r.status_code == 200, r.text
    assert urlsplit(redirect).netloc in r.text and r.headers["x-frame-options"] == "DENY"
    from urllib.parse import urlencode
    blob = urlencode({k: q[k] for k in ("client_id", "redirect_uri", "state", "code_challenge")})
    data = {"token": token, "params": blob}
    if readonly:
        data["readonly"] = "1"
    return m.client.post("/authorize", data=data)


def _full_flow(m, token=OP, readonly=False):
    cid = _register(m)
    v, ch = _pkce()
    r = _authorize(m, cid, token, ch, readonly)
    assert r.status_code == 302
    loc = urlsplit(r.headers["location"])
    q = parse_qs(loc.query)
    assert q["state"] == ["xyz"] and q["iss"] == ["https://loyd.example"]
    t = m.client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                      "code_verifier": v, "redirect_uri": REDIRECT, "client_id": cid})
    assert t.status_code == 200, t.text
    return cid, t.json()


def _tools(m, access):
    r = m.client.post("/mcp/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                      headers={**MCP_HDRS, "Authorization": f"Bearer {access}"})
    assert r.status_code == 200, r.text
    return {t["name"] for t in r.json()["result"]["tools"]}


def test_metadata(m):
    md = m.client.get("/.well-known/oauth-authorization-server").json()
    assert md["issuer"] == "https://loyd.example"
    assert "refresh_token" in md["grant_types_supported"]
    pr = m.client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert pr["resource"] == "https://loyd.example/mcp"


def test_unauthenticated_mcp_points_to_metadata(m):
    r = m.client.post("/mcp/", json=MCP_INIT, headers=MCP_HDRS)
    assert r.status_code == 401
    assert "oauth-protected-resource/mcp" in r.headers["www-authenticate"]


def test_full_flow_operator_sees_write_tools(m):
    _, tok = _full_flow(m)
    assert tok["access_token"] != OP and OP not in tok["access_token"]   # master token never handed out
    assert "write_file" in _tools(m, tok["access_token"])


def test_readonly_checkbox_hides_write_tools(m):
    _, tok = _full_flow(m, readonly=True)
    assert "write_file" not in _tools(m, tok["access_token"])
    assert "run_shell" in _tools(m, tok["access_token"])


def test_readonly_user_stays_readonly(m):
    _, tok = _full_flow(m, token=RO)
    assert "write_file" not in _tools(m, tok["access_token"])


def test_refresh_rolls_and_keeps_scope(m):
    cid, tok = _full_flow(m, readonly=True)
    r = m.client.post("/token", data={"grant_type": "refresh_token",
                                      "refresh_token": tok["refresh_token"], "client_id": cid})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["access_token"] != tok["access_token"]
    assert "write_file" not in _tools(m, new["access_token"])


def test_refresh_bound_to_client(m):
    _, tok = _full_flow(m)
    other = _register(m)
    r = m.client.post("/token", data={"grant_type": "refresh_token",
                                      "refresh_token": tok["refresh_token"], "client_id": other})
    assert r.status_code == 400


def test_access_token_is_not_a_refresh_token(m):
    cid, tok = _full_flow(m)
    r = m.client.post("/token", data={"grant_type": "refresh_token",
                                      "refresh_token": tok["access_token"], "client_id": cid})
    assert r.status_code == 400
    r = m.client.post("/mcp/", json=MCP_INIT,
                      headers={**MCP_HDRS, "Authorization": f"Bearer {tok['refresh_token']}"})
    assert r.status_code == 401


def test_code_single_use_and_pkce(m):
    cid = _register(m)
    v, ch = _pkce()
    code = parse_qs(urlsplit(_authorize(m, cid, OP, ch).headers["location"]).query)["code"][0]
    bad = m.client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                        "code_verifier": "x" * 50, "redirect_uri": REDIRECT})
    assert bad.status_code == 400
    again = m.client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                          "code_verifier": v, "redirect_uri": REDIRECT})
    assert again.status_code == 400          # popped on first attempt


def test_unregistered_redirect_rejected_not_redirected(m):
    cid = _register(m)
    _, ch = _pkce()
    r = m.client.get("/authorize", params={"response_type": "code", "client_id": cid,
                                           "redirect_uri": "https://evil.example/cb",
                                           "code_challenge": ch, "code_challenge_method": "S256"})
    assert r.status_code == 400 and "location" not in r.headers


def test_tampered_client_id_rejected(m):
    cid = _register(m)
    _, ch = _pkce()
    r = m.client.get("/authorize", params={"response_type": "code", "client_id": cid[:-3] + "AAA",
                                           "redirect_uri": REDIRECT, "code_challenge": ch})
    assert r.status_code == 400


def test_register_rejects_bad_redirects(m):
    for uris in ([], ["javascript:alert(1)"], ["http://evil.example/cb"], "notalist"):
        assert m.client.post("/register", json={"redirect_uris": uris}).status_code == 400
    assert m.client.post("/register", json={"redirect_uris": ["http://127.0.0.1:9999/cb"]}).status_code == 201


def test_registration_survives_restart(m):
    cid = _register(m)
    mod = importlib.reload(m)
    assert mod._client(cid) is not None


def test_rotation_revokes(m, monkeypatch):
    _, tok = _full_flow(m)
    monkeypatch.setitem(os.environ, "LOYD_MCP_TOKENS", "me:o:rotated-" + "c" * 20)
    m.TOKENS = m._load_tokens()
    assert m.identity_for(tok["access_token"]) is None


def test_expired_access_token_rejected(m, monkeypatch):
    _, tok = _full_flow(m)
    real = time.time
    monkeypatch.setattr(m.time, "time", lambda: real() + m.ACCESS_TTL + 5)
    assert m.identity_for(tok["access_token"]) is None


def test_bruteforce_throttle(m):
    cid = _register(m)
    _, ch = _pkce()
    codes = [_authorize(m, cid, "wrong", ch).status_code for _ in range(m._FAIL_MAX + 1)]
    assert codes[:m._FAIL_MAX] == [401] * m._FAIL_MAX and codes[-1] == 429
    assert _authorize(m, cid, OP, ch).status_code == 429     # locked even with the right token


def test_static_token_header_and_query_still_work(m):
    assert "write_file" in _tools(m, OP)
    r = m.client.post(f"/mcp/?key={RO}", json=MCP_INIT, headers=MCP_HDRS)
    assert r.status_code == 200


def test_redirect_preserves_existing_query(m):
    uri = "https://app.example/cb?x=1"
    cid = _register(m, [uri])
    _, ch = _pkce()
    r = _authorize(m, cid, OP, ch, redirect=uri)
    assert r.headers["location"].startswith("https://app.example/cb?x=1&code=")


def _call(m, bearer, name, args):
    r = m.client.post("/mcp/", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": name, "arguments": args}},
                      headers={**MCP_HDRS, "Authorization": f"Bearer {bearer}"})
    assert r.status_code == 200, r.text
    return r.json()["result"]["content"][0]["text"]


def test_readonly_token_gets_readonly_shell_on_operator_server(m, monkeypatch, tmp_path):
    """Regression: with LOYD_MODE=operator, a read-only token used to get the full
    operator shell through run_shell (only the write tools were hidden)."""
    import tools
    monkeypatch.setattr(tools, "_MODE", "operator")
    f = tmp_path / "pwned"
    assert _call(m, RO, "run_shell", {"command": f"touch {f}"}).startswith("REFUSED")
    _, tok = _full_flow(m, readonly=True)             # operator token, Read-only ticked
    assert _call(m, tok["access_token"], "run_shell", {"command": f"touch {f}"}).startswith("REFUSED")
    assert not f.exists()
    _call(m, OP, "run_shell", {"command": f"touch {f}"})
    assert f.exists()


def test_healthz_is_public_and_quiet(m):
    r = m.client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_audit_tail_hidden_from_readonly_tokens(m):
    assert "audit_tail" in _tools(m, OP)
    assert "audit_tail" not in _tools(m, RO)


def test_rate_limit_per_identity(m, monkeypatch, tmp_path):
    monkeypatch.setattr(m, "RATE_PER_MIN", 3)
    monkeypatch.setattr(m, "_calls", {})
    monkeypatch.setenv("LOYD_AUDIT_LOG", str(tmp_path / "a.jsonl"))
    outs = [_call(m, OP, "read_file", {"path": "/etc/hostname"}) for _ in range(4)]
    assert not any(o.startswith("THROTTLED") for o in outs[:3]) and outs[3].startswith("THROTTLED")
    assert not _call(m, RO, "read_file", {"path": "/etc/hostname"}).startswith("THROTTLED")   # per user
    import json
    recs = [json.loads(l) for l in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert [r["outcome"] for r in recs].count("throttled") == 1
