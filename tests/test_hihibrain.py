"""hihilabs brain tools passed through Loyd (hihibrain.py): discovery, calls, failure modes, wiring."""
import importlib
import json
import os
import sys

import hihibrain

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_brain_door.py")]


def test_unset_means_no_tools(monkeypatch):
    monkeypatch.delenv("LOYD_BRAIN_CMD", raising=False)
    assert hihibrain.discover() == []
    assert hihibrain.call("project_brief", {"project_id": 50}).startswith("ERROR: hihilabs brain tools are not configured")


def test_discover_takes_hihilabs_own_schemas_and_only_allowed_tools():
    got = hihibrain.discover(FAKE)
    assert [t["function"]["name"] for t in got] == ["project_brief", "log_handoff"]      # arm_paid_api never offered
    f = got[0]["function"]
    assert f["description"].startswith("[hihilabs] ") and f["parameters"]["required"] == ["project_id"]


def test_call_passes_arguments_and_returns_the_text():
    out = json.loads(hihibrain.call("project_brief", {"project_id": 50}, FAKE))
    assert out == {"tool": "project_brief", "args": {"project_id": 50}}
    assert hihibrain.call("arm_paid_api", {}, FAKE) == "ERROR: 'arm_paid_api' is not a hihilabs brain tool."
    assert hihibrain.call("log_handoff", {"boom": True}, FAKE) == "ERROR: project_id is required"


def test_a_dead_or_silent_door_is_an_error_string_never_an_exception(monkeypatch):
    assert hihibrain.call("project_brief", {}, ["/nonexistent/door"]).startswith("ERROR: could not start")
    assert hihibrain.call("project_brief", {}, [sys.executable, "-c", "print('not json')"]).startswith("ERROR: hihilabs gave no answer")
    monkeypatch.setenv("LOYD_BRAIN_TIMEOUT", "5")
    assert "did not answer within 5 s" in hihibrain.call("project_brief", {}, [sys.executable, "-c", "import time; time.sleep(30)"])
    assert hihibrain.discover(["/nonexistent/door"]) == []                              # Loyd still starts


def test_tools_installs_them_for_read_only_users_too_and_audits(monkeypatch, tmp_path):
    monkeypatch.setenv("LOYD_BRAIN_CMD", " ".join(FAKE))
    monkeypatch.setenv("LOYD_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    for m in ("tools", "audit"):
        sys.modules.pop(m, None)
    tools = importlib.import_module("tools")
    try:
        assert tools.BRAIN_TOOLS == ["project_brief", "log_handoff"]
        assert not (set(tools.BRAIN_TOOLS) & (tools.WRITE_TOOLS | tools.OPERATOR_TOOLS))   # notes are for everyone
        out = tools.execute_tool("log_handoff", {"project_id": 50, "title": "x"}, actor="mcp:geoff", readonly=True)
        assert json.loads(out)["tool"] == "log_handoff"
        assert "read_file" in tools._IMPL                                                   # Loyd's own tools untouched
    finally:
        monkeypatch.delenv("LOYD_BRAIN_CMD", raising=False)
        sys.modules.pop("tools", None)
