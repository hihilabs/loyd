"""Trace recorder + export: the Claude-never-trainable rule, review gating, CLI wiring."""
import json
import os
import subprocess
import sys

import pytest

import cli
import loydtrace as trace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORT = os.path.join(ROOT, "scripts", "export_training.py")


def _run(brain, events, task="is plex up?"):
    t = trace.Tracer(brain, "m", task)
    for k, d in events:
        t.on_event(k, d)
    return t


GOOD_RUN = [("tool_call", {"name": "get_container_status", "args": {"name": "plex"},
                           "source": "native", "dt": 0.1}),
            ("tool_result", {"name": "get_container_status", "result": '{"running":true}', "repeat": False}),
            ("final", {"text": "Plex is running.", "dt": 0.1})]


def _records(d):
    out = []
    for p in sorted(d.glob("traces-*.jsonl")):
        out += [json.loads(l) for l in p.read_text().splitlines()]
    return out


def test_off_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("LOYD_TRACE_DIR", raising=False)
    _run("local", GOOD_RUN)
    assert not list(tmp_path.iterdir())


def test_records_steps_and_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("LOYD_TRACE_DIR", str(tmp_path))
    _run("local", GOOD_RUN)
    _run("claude", GOOD_RUN)
    _run("local", [("error", {"text": "model down"})])
    local, claude, err = _records(tmp_path)
    assert local["steps"] == [{"tool": "get_container_status", "args": {"name": "plex"},
                               "result": '{"running":true}'}]
    assert local["trainable"] is True and local["final"] == "Plex is running."
    assert claude["trainable"] is False                     # never, even for a perfect run
    assert err["trainable"] is False and err["outcome"] == "error"
    assert oct((next(tmp_path.glob("traces-*"))).stat().st_mode)[-3:] == "600"


def test_export_only_reviewed_local_and_never_claude(monkeypatch, tmp_path):
    monkeypatch.setenv("LOYD_TRACE_DIR", str(tmp_path))
    good = _run("local", GOOD_RUN, "good one")
    unreviewed = _run("local", GOOD_RUN, "unreviewed")
    bad = _run("local", GOOD_RUN, "rated bad")
    claude = _run("claude", GOOD_RUN, "claude answer")
    trace.review(good.rec["id"], "good")
    trace.review(bad.rec["id"], "bad")
    trace.review(claude.rec["id"], "good")
    # Someone edits the file to mark the Claude trace trainable — it must still not leak.
    f = next(tmp_path.glob("traces-*"))
    f.write_text(f.read_text().replace(
        f'"id": "{claude.rec["id"]}"', f'"id": "{claude.rec["id"]}"').replace(
        '"trainable": false', '"trainable": true'))
    out = tmp_path / "train.jsonl"
    subprocess.run([sys.executable, EXPORT, str(tmp_path), str(out)], check=True, capture_output=True)
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert [r["source_trace"] for r in rows] == [good.rec["id"]]
    msgs = rows[0]["messages"]
    assert msgs[0] == {"role": "user", "content": "good one"}
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "get_container_status"
    assert msgs[-1] == {"role": "assistant", "content": "Plex is running."}
    assert "claude answer" not in out.read_text() and "unreviewed" not in out.read_text()


def test_abort_writes_only_when_work_happened(monkeypatch, tmp_path):
    monkeypatch.setenv("LOYD_TRACE_DIR", str(tmp_path))
    trace.Tracer("claude", "m", "x").abort("cloud dropped")             # nothing happened
    t = _run("local", GOOD_RUN[:2])
    t.abort("cloud dropped")
    recs = _records(tmp_path)
    assert len(recs) == 1 and recs[0]["outcome"] == "aborted" and recs[0]["trainable"] is False


def test_cli_traces_and_rates(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOYD_TRACE_DIR", str(tmp_path))
    sess = cli.Session("qwen", "http://127.0.0.1:9", "claude-opus-5-5", 4)

    def fake_run(q):
        for k, d in GOOD_RUN:
            sess.agent.on_event(k, d)
        return "Plex is running."
    monkeypatch.setattr(sess.agent, "run", fake_run)
    sess.ask_local("is plex up?")
    assert sess.agent.on_event is sess.printer                          # hook restored
    cli.handle_slash("/good", sess)
    recs = _records(tmp_path)
    assert recs[0]["brain"] == "local" and recs[1] == {**recs[1], "review": recs[0]["id"], "verdict": "good"}
    assert "rated" in capsys.readouterr().out
