"""claude_backend: request shape (model, caching, effort, fallbacks), thinking
blocks passed back verbatim, refusal handling."""
import io
import json

import claude_backend as cb


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
