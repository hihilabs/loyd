"""claude_backend: request shape (model, caching, effort, fallbacks), thinking
blocks passed back verbatim, refusal handling."""
import io
import json

import pytest

import claude_backend as cb


@pytest.fixture(autouse=True)
def _no_network_probe(monkeypatch):
    # request-shape tests must not depend on real network; offline tests _reset() this
    monkeypatch.setenv("LOYD_OFFLINE", "0")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake(responses, seen):
    def urlopen(req, timeout=None):
        seen.append({"body": json.loads(req.data), "headers": dict(req.header_items())})
        return _Resp(json.dumps(responses.pop(0)).encode())
    return urlopen


def _agent(**kw):
    return cb.ClaudeAgent(api_key="k", tool_schemas=[{"type": "function", "function": {
        "name": "run_shell", "description": "d",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}],
        execute=lambda n, a: "Tokyo7", **kw)


def test_request_shape_and_thinking_roundtrip(monkeypatch):
    seen = []
    thinking = {"type": "thinking", "thinking": "", "signature": "sig"}
    monkeypatch.setattr(cb.urllib.request, "urlopen", _fake([
        {"stop_reason": "tool_use", "content": [thinking, {"type": "tool_use", "id": "t1",
                                                           "name": "run_shell", "input": {"command": "hostname"}}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "It's Tokyo7."}]},
    ], seen))
    assert _agent().run("which host?") == "It's Tokyo7."
    b, h = seen[0]["body"], {k.lower(): v for k, v in seen[0]["headers"].items()}
    assert b["model"] == "claude-opus-5-5"
    assert b["cache_control"] == {"type": "ephemeral"}
    assert b["output_config"] == {"effort": "high"}
    assert b["fallbacks"] == "default" and h["anthropic-beta"] == cb.FALLBACK_BETA
    assert "thinking" not in b and "temperature" not in b
    assert b["max_tokens"] >= 16000
    # second request carries the thinking block back unchanged
    assert seen[1]["body"]["messages"][1]["content"][0] == thinking
    assert seen[1]["body"]["messages"][2]["content"][0]["tool_use_id"] == "t1"


def test_fallbacks_can_be_disabled(monkeypatch):
    seen = []
    monkeypatch.setenv("LOYD_CLAUDE_FALLBACKS", "off")
    monkeypatch.setattr(cb.urllib.request, "urlopen", _fake([
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}], seen))
    _agent().run("hi")
    assert "fallbacks" not in seen[0]["body"]
    assert "anthropic-beta" not in {k.lower() for k in seen[0]["headers"]}


def test_refusal_is_reported_not_blank(monkeypatch):
    monkeypatch.setattr(cb.urllib.request, "urlopen", _fake([
        {"stop_reason": "refusal", "content": [], "stop_details": {"type": "refusal", "category": "cyber"}}], []))
    out = _agent().run("x")
    assert "declined" in out and "cyber" in out


def _reset(monkeypatch):
    monkeypatch.setattr(cb, "_probe", {"at": 0.0, "online": True, "why": ""})
    monkeypatch.delenv("LOYD_OFFLINE", raising=False)


def test_offline_probe_cached_and_skips_request(monkeypatch):
    _reset(monkeypatch)
    calls, sent = [], []

    def boom(addr, timeout=None):
        calls.append(addr)
        raise OSError("network unreachable")
    monkeypatch.setattr(cb.socket, "create_connection", boom)
    monkeypatch.setattr(cb.urllib.request, "urlopen", lambda *a, **k: sent.append(1))
    for _ in range(3):
        with pytest.raises(cb.ClaudeUnavailable, match="offline"):
            _agent().run("hi")
    assert len(calls) == 1 and not sent          # probed once, never tried the API


def test_env_overrides(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(cb.socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed")))
    monkeypatch.setenv("LOYD_OFFLINE", "1")
    assert cb.online_status() == (False, "LOYD_OFFLINE=1")
    monkeypatch.setenv("LOYD_OFFLINE", "0")
    assert cb.online_status()[0] is True


def test_network_error_mid_request_marks_offline(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(cb.socket, "create_connection", lambda *a, **k: type("C", (), {"close": lambda s: None})())

    def drop(*a, **k):
        raise cb.urllib.error.URLError("connection reset")
    monkeypatch.setattr(cb.urllib.request, "urlopen", drop)
    with pytest.raises(cb.ClaudeUnavailable):
        _agent().run("hi")
    assert cb.online_status() [0] is False                 # remembered: next turn goes local at once


def test_back_online_after_cache_expires(monkeypatch):
    _reset(monkeypatch)
    cb.mark_offline("blip")
    monkeypatch.setattr(cb.socket, "create_connection", lambda *a, **k: type("C", (), {"close": lambda s: None})())
    assert cb.online_status()[0] is False
    monkeypatch.setitem(cb._probe, "at", cb._probe["at"] - cb.OFFLINE_CACHE_S - 1)
    assert cb.online_status()[0] is True
