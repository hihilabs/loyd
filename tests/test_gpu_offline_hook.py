"""GPU modes 3/3: when the cloud link drops (or comes back) and the recorded GPU mode
doesn't match, the CLI suggests a switch once — and never switches by itself. /gpu
shows the dry run and applies only after the operator's y/N."""
import pytest

import cli
import gpumode
from claude_backend import ClaudeUnavailable


@pytest.fixture()
def gpu(tmp_path, monkeypatch):
    cfg = tmp_path / "gpu-mode.env"
    cfg.write_text("VISION_CONTAINER=v\n")
    state = tmp_path / "state"
    monkeypatch.setenv("LOYD_GPU_MODE_CONFIG", str(cfg))
    monkeypatch.setenv("LOYD_GPU_MODE_STATE", str(state))
    calls = []
    monkeypatch.setattr(cli, "run_tool", lambda name, args: calls.append((name, dict(args))) or "DRY: ...")
    return dict(state=state, calls=calls)


@pytest.fixture()
def sess():
    return cli.Session("qwen", "http://127.0.0.1:9", "claude-test", 4)


def test_suggestion(gpu, monkeypatch):
    assert gpumode.suggestion(False, {}) == "offline"                  # nothing recorded = online
    assert gpumode.suggestion(False, {"mode": "online"}) == "offline"
    assert gpumode.suggestion(False, {"mode": "offline"}) is None
    assert gpumode.suggestion(True, {"mode": "offline"}) == "online"
    assert gpumode.suggestion(True, {"mode": "online"}) is None
    assert gpumode.suggestion(True, {}) is None
    gpu["state"].write_text("mode=offline\nkeep_alive=30m\n")
    assert gpumode.suggestion(True) == "online"                        # read from LOYD_GPU_MODE_STATE
    monkeypatch.delenv("LOYD_GPU_MODE_CONFIG")
    assert gpumode.suggestion(False, {}) is None                       # tool not configured: silent


def test_hint_once_per_change(gpu, sess, capsys):
    sess.gpu_hint(False)
    assert "/gpu offline" in capsys.readouterr().err
    sess.gpu_hint(False)
    assert capsys.readouterr().err == ""                               # not repeated every turn
    sess.gpu_hint(True)
    assert capsys.readouterr().err == ""                               # back online, recorded online: nothing
    sess.gpu_hint(False)
    assert "/gpu offline" in capsys.readouterr().err                   # a new outage hints again
    gpu["state"].write_text("mode=offline\n")
    sess.gpu_hint(True)
    assert "/gpu online" in capsys.readouterr().err
    assert gpu["calls"] == []                                          # never switched anything


def test_fallback_turn_suggests_but_never_switches(gpu, sess, monkeypatch, capsys):
    asked = []

    def down(q):
        raise ClaudeUnavailable("offline (can't reach api)")
    monkeypatch.setattr(sess, "ask_claude", down)
    monkeypatch.setattr(sess, "ask_local", lambda q: asked.append(q))
    sess.ask_claude_first("disk usage?")
    err = capsys.readouterr().err
    assert asked == ["disk usage?"] and "/gpu offline" in err
    assert gpu["calls"] == []

    gpu["state"].write_text("mode=offline\n")                          # someone switched; link returns
    monkeypatch.setattr(sess, "ask_claude", lambda q: None)
    sess.ask_claude_first("and now?")
    assert "/gpu online" in capsys.readouterr().err
    assert gpu["calls"] == []


def test_no_hint_without_config(gpu, sess, monkeypatch, capsys):
    monkeypatch.delenv("LOYD_GPU_MODE_CONFIG")
    sess.gpu_hint(False)
    assert capsys.readouterr().err == ""


def test_gpu_command_status_and_usage(gpu, capsys):
    cli.gpu_command("status")
    assert gpu["calls"] == [("gpu_mode", {"mode": "status"})]
    cli.gpu_command("turbo")
    assert "usage: /gpu" in capsys.readouterr().out and len(gpu["calls"]) == 1


def test_gpu_command_declined_runs_dry_only(gpu, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr(cli, "confirm_write", lambda n, a: asked.append((n, a)) or False)
    cli.gpu_command("offline")
    assert gpu["calls"] == [("gpu_mode", {"mode": "offline"})]
    assert asked == [("gpu_mode", {"mode": "offline", "apply": True})]
    assert "left as is" in capsys.readouterr().out


def test_gpu_command_confirmed_applies(gpu, monkeypatch):
    monkeypatch.setattr(cli, "confirm_write", lambda n, a: True)
    cli.gpu_command("online")
    assert gpu["calls"] == [("gpu_mode", {"mode": "online"}),
                            ("gpu_mode", {"mode": "online", "apply": True})]


def test_gpu_command_refused_dry_run_asks_nothing(gpu, monkeypatch):
    monkeypatch.setattr(cli, "run_tool", lambda n, a: gpu["calls"].append((n, a)) or "REFUSED: vision busy")
    monkeypatch.setattr(cli, "confirm_write", lambda n, a: pytest.fail("asked to confirm a refused switch"))
    cli.gpu_command("offline")
    assert len(gpu["calls"]) == 1


def test_gpu_command_not_configured(monkeypatch, capsys):
    monkeypatch.delenv("LOYD_GPU_MODE_CONFIG", raising=False)
    monkeypatch.setattr(cli, "run_tool", lambda n, a: pytest.fail("ran without config"))
    cli.gpu_command("offline")
    assert "not configured" in capsys.readouterr().out


def test_slash_gpu_dispatches(gpu, sess):
    cli.handle_slash("/gpu", sess)
    assert gpu["calls"] == [("gpu_mode", {"mode": "status"})]
