"""gpu_mode tool (gpumode.py): operator-only, dry run first, apply only after the
dry run passed and only with the operator's y/N; the local brain sends the recorded
keep_alive in `online`. Runs the real scripts/gpu_mode.sh against fake docker /
nvidia-smi / curl on PATH."""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import pytest

import cli
import gpumode
import loyd_core
import tools


@pytest.fixture()
def box(tmp_path, monkeypatch):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    calls = tmp_path / "calls.log"
    state = tmp_path / "gpu-mode.state"
    (fakebin / "docker").write_text(
        f'#!/bin/bash\necho docker "$@" >> {calls}\n[ "$1" = inspect ] && echo true\nexit 0\n')
    (fakebin / "nvidia-smi").write_text(f'#!/bin/bash\ncat {tmp_path}/util 2>/dev/null || echo 0\n')
    (fakebin / "curl").write_text(f'#!/bin/bash\necho curl "$@" >> {calls}\n'
                                  f'[ -e {tmp_path}/curlfail ] && exit 7\nexit 0\n')
    for f in fakebin.iterdir():
        f.chmod(0o755)
    cfg = tmp_path / "gpu-mode.env"
    cfg.write_text("VISION_CONTAINER=vision-gpu1\nVISION_GPU=1\nOLLAMA_URL=http://ollama:11435\n"
                   f"OFFLINE_MODEL=big:32b\nSTATE_FILE={state}\n")
    monkeypatch.setenv("PATH", f"{fakebin}:{os.environ['PATH']}")
    monkeypatch.setenv("LOYD_GPU_MODE_CONFIG", str(cfg))
    monkeypatch.setattr(tools, "_REMOTE", "")
    # install into the live registries (as tools.py does at import with the env set), undo after
    schemas, impl, ops = tools.TOOL_SCHEMAS, tools._IMPL, tools.OPERATOR_TOOLS
    n = len(schemas)
    assert gpumode.install(schemas, impl, ops, tools._exec, tools._cap)
    yield dict(calls=calls, state=state, util=tmp_path / "util", curlfail=tmp_path / "curlfail")
    del schemas[n:]
    impl.pop("gpu_mode", None)
    ops.discard("gpu_mode")


def curl_calls(box):
    return [ln for ln in (box["calls"].read_text().splitlines() if box["calls"].exists() else [])
            if ln.startswith("curl")]


def test_not_installed_without_config(monkeypatch):
    monkeypatch.delenv("LOYD_GPU_MODE_CONFIG", raising=False)
    s, i, o = [], {}, set()
    assert gpumode.install(s, i, o, tools._exec, tools._cap) is False
    assert not s and not i and not o
    assert gpumode.gpu_mode(tools._exec, tools._cap, "status").startswith("ERROR: gpu_mode is not configured")


def test_status_and_dry_run_change_nothing(box):
    out = tools.execute_tool("gpu_mode", {"mode": "status"})
    assert "recorded mode:" in out and "vision:" in out
    out = tools.execute_tool("gpu_mode", {"mode": "offline"})
    assert "DRY: load big:32b" in out and "nothing changed" in out
    assert not [c for c in curl_calls(box) if "/api/generate" in c]
    assert not box["state"].exists()


def test_apply_runs_dry_first_then_applies(box):
    out = tools.execute_tool("gpu_mode", {"mode": "offline", "apply": True})
    assert "load big:32b on http://ollama:11435" in out and "DRY" not in out
    gen = [c for c in curl_calls(box) if "/api/generate" in c]
    assert len(gen) == 1 and '"keep_alive":"30m"' in gen[0]
    assert "mode=offline" in box["state"].read_text()
    out = tools.execute_tool("gpu_mode", {"mode": "online", "apply": "true"})
    assert "mode=online" in box["state"].read_text() and "keep_alive=2m" in box["state"].read_text()


def test_refused_while_vision_busy_never_applies(box):
    box["util"].write_text("90\n")
    out = tools.execute_tool("gpu_mode", {"mode": "offline", "apply": True})
    assert out.startswith("REFUSED:") and "never paused" in out
    assert not curl_calls(box) and not box["state"].exists()
    c = box["calls"].read_text()
    assert "docker stop" not in c and "docker pause" not in c


def test_failed_apply_is_an_error(box):
    box["curlfail"].write_text("")
    out = tools.execute_tool("gpu_mode", {"mode": "online", "apply": True})
    assert out.startswith("ERROR (exit 4)") and "failed: unload big:32b" in out


def test_bad_mode():
    assert gpumode.gpu_mode(tools._exec, tools._cap, "turbo").startswith("ERROR: mode must be")


def test_operator_only(box, monkeypatch):
    out = tools.execute_tool("gpu_mode", {"mode": "status"}, actor="mcp:mate", readonly=True)
    assert out.startswith("DENIED") and "operator-only" in out
    monkeypatch.setenv("LOYD_MCP_TOKENS", "me:o:" + "o" * 32 + ",mate:r:" + "r" * 32)
    import loyd_mcp
    assert "gpu_mode" in [t.name for t in loyd_mcp._tools_for(True)]
    assert "gpu_mode" not in [t.name for t in loyd_mcp._tools_for(False)]


def test_confirm_only_on_apply():
    assert not cli.should_confirm("gpu_mode", {"mode": "status"})
    assert not cli.should_confirm("gpu_mode", {"mode": "offline"})
    assert not cli.should_confirm("gpu_mode", {"mode": "offline", "apply": False})
    assert cli.should_confirm("gpu_mode", {"mode": "offline", "apply": True})
    assert cli.should_confirm("gpu_mode", {"mode": "online", "apply": "true"})
    assert cli.should_confirm("write_file", {"path": "/x", "content": ""})


def test_declined_apply_never_runs(box):
    ran = []
    agent = loyd_core.LoydAgent(execute=lambda n, a: ran.append(n) or "ran",
                                confirm=lambda n, a: False, should_confirm=cli.should_confirm)
    replies = iter([{"tool_calls": [{"function": {"name": "gpu_mode",
                                                  "arguments": {"mode": "offline", "apply": True}}}]},
                    {"content": "ok, left it"}])
    agent._chat = lambda messages, tools=None: (next(replies), 0.0)
    assert agent.run("go offline") == "ok, left it"
    assert ran == []


def test_keep_alive_from_state(tmp_path):
    st = tmp_path / "state"
    assert gpumode.keep_alive(str(st)) is None                       # no file
    st.write_text("mode=online\nkeep_alive=2m\nmodel=\nsince=x\n")
    assert gpumode.keep_alive(str(st)) == "2m"
    st.write_text("mode=offline\nkeep_alive=30m\nmodel=big:32b\n")
    assert gpumode.keep_alive(str(st)) is None                       # big model's keep-alive isn't ours


def test_local_brain_sends_keep_alive_online(tmp_path, monkeypatch):
    bodies = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            out = json.dumps({"message": {"role": "assistant", "content": "hi"}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        st = tmp_path / "state"
        monkeypatch.setenv("LOYD_GPU_MODE_STATE", str(st))
        agent = loyd_core.LoydAgent(ollama_url=url)
        assert agent.run("q") == "hi" and "keep_alive" not in bodies[-1]
        st.write_text("mode=online\nkeep_alive=2m\n")
        agent.run("q")
        assert bodies[-1]["keep_alive"] == "2m"
        st.write_text("mode=offline\nkeep_alive=30m\n")
        agent.run("q")
        assert "keep_alive" not in bodies[-1]
    finally:
        srv.shutdown()
