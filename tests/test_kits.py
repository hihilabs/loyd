"""Kit framework: load-time validation, param checks + quoting, verified gating,
write tools gated like core writes, shipped kits valid."""
import json
import os

import pytest

import kits
import tools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _kit(tmp_path, cmds, name="demo"):
    (tmp_path / f"{name}.json").write_text(json.dumps({"name": name, "description": "d", "commands": cmds}))
    return tmp_path


def _install(tmp_path, cmds, runner=None, name="demo"):
    schemas, impl, writes = [{"function": {"name": "run_shell"}}], {}, set()
    seen = []
    runner = runner or (lambda line, t: seen.append((line, t)) or "ran")
    kits.install(schemas, impl, writes, runner, names=[name], directory=_kit(tmp_path, cmds, name))
    return schemas, impl, writes, seen


GOOD = {"name": "logs", "mode": "read", "verified": True, "description": "tail logs",
        "params": {"name": {"type": "string", "pattern": "^[a-z0-9-]+$"},
                   "lines": {"type": "integer", "min": 1, "max": 500, "default": 50}},
        "run": "docker logs --tail {lines} {name} --format '{{.Names}}'"}


def test_render_validates_and_quotes(tmp_path):
    schemas, impl, writes, seen = _install(tmp_path, [GOOD])
    assert impl["demo_logs"]({"name": "web-1"}, False) == "ran"
    assert seen[-1] == ("docker logs --tail 50 web-1 --format '{{.Names}}'", 30)
    assert impl["demo_logs"]({"name": "web; rm -rf /"}, False).startswith("ERROR")
    assert impl["demo_logs"]({"name": "web", "lines": 9999}, False).startswith("ERROR")
    assert impl["demo_logs"]({"name": "web", "evil": "x"}, False).startswith("ERROR")
    assert impl["demo_logs"]({}, False).startswith("ERROR")          # required param
    assert len(seen) == 1                                            # nothing bad ran
    s = next(x for x in schemas if x["function"]["name"] == "demo_logs")["function"]
    assert s["parameters"]["required"] == ["name"]
    assert s["parameters"]["properties"]["lines"]["maximum"] == 500


def test_values_are_shell_quoted(tmp_path):
    cmd = dict(GOOD, params={"q": {"type": "string", "pattern": "^[a-z ]+$"}}, run="grep {q} f")
    _, impl, _, seen = _install(tmp_path, [cmd])
    impl["demo_logs"]({"q": "two words"}, False)
    assert seen[-1][0] == "grep 'two words' f"


@pytest.mark.parametrize("bad, msg", [
    (dict(GOOD, params={"x": {"type": "string"}}, run="echo {x}"), "pattern"),
    (dict(GOOD, params={"x": {"type": "string", "pattern": "[a-z]+"}}, run="echo {x}"), "anchored"),
    (dict(GOOD, params={}, run="echo {x}"), "not a declared param"),
    (dict(GOOD, mode="maybe"), "mode"),
    (dict(GOOD, timeout=5000), "timeout"),
    (dict(GOOD, name="Bad Name"), "command name"),
    (dict(GOOD, params={"n": {"type": "integer", "min": 1}}), "min"),
])
def test_bad_kits_fail_loudly(tmp_path, bad, msg):
    with pytest.raises(kits.KitError, match=msg):
        _install(tmp_path, [bad])


def test_core_tool_collision_rejected(tmp_path):
    with pytest.raises(kits.KitError, match="collides"):
        _install(tmp_path, [dict(GOOD, name="shell")], name="run")


def test_unverified_hidden_and_refused(tmp_path):
    schemas, impl, writes, seen = _install(tmp_path, [dict(GOOD, verified=False)])
    assert "demo_logs" not in [s["function"]["name"] for s in schemas]
    assert impl["demo_logs"]({"name": "web"}, False).startswith("REFUSED")
    assert not seen


def test_write_commands_are_write_tools(tmp_path):
    _, _, writes, _ = _install(tmp_path, [dict(GOOD, mode="write")])
    assert "demo_logs" in writes


def test_readonly_caller_denied_kit_write(monkeypatch, tmp_path):
    schemas, impl, writes = list(tools.TOOL_SCHEMAS), dict(tools._IMPL), set(tools.WRITE_TOOLS)
    kits.install(schemas, impl, writes, lambda l, t: "ran", names=["demo"],
                 directory=_kit(tmp_path, [dict(GOOD, mode="write", run="docker restart {name}")]))
    monkeypatch.setattr(tools, "_IMPL", impl)
    monkeypatch.setattr(tools, "WRITE_TOOLS", writes)
    assert tools.execute_tool("demo_logs", {"name": "web"}, readonly=True).startswith("DENIED")
    assert tools.execute_tool("demo_logs", {"name": "web"}) == "ran"


@pytest.mark.parametrize("name", ["ops", "hihilabs"])
def test_shipped_kits_load(name):
    kit = kits.load_kit(os.path.join(ROOT, "kits", f"{name}.json"),
                        reserved={s["function"]["name"] for s in tools.TOOL_SCHEMAS})
    assert kit["commands"]


def test_hihilabs_kit_is_unverified_until_a_human_checks():
    kit = kits.load_kit(os.path.join(ROOT, "kits", "hihilabs.json"))
    assert not any(c.get("verified") for c in kit["commands"])


def test_ops_kit_renders_through_real_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(tools, "_exec", lambda line, **kw: calls.append((line, kw)) or
                        type("P", (), {"stdout": "ok", "stderr": "", "returncode": 0})())
    schemas, impl, writes = [], {}, set()
    kits.install(schemas, impl, writes, tools._run_kit_command, names=["ops"])
    assert impl["ops_container_logs"]({"name": "camagent-worker-gpu1", "lines": 20}, False) == "ok"
    assert calls[-1] == ("docker logs --tail 20 camagent-worker-gpu1 2>&1", {"timeout": 60})
    impl["ops_containers"]({}, False)
    assert "{{.Names}}" in calls[-1][0]
