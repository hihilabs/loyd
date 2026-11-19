"""Brain router: policy parsing, the auto classifier (against a fake ollama
/api/chat), the outcome flag on LoydAgent, and Session.ask routing."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import brain
import cli
from claude_backend import ClaudeUnavailable
from loyd_core import LoydAgent


# ---------- policy ----------

@pytest.mark.parametrize("raw,want,warns", [
    (None, "claude-first", False),
    ("", "claude-first", False),
    ("claude-first", "claude-first", False),
    ("LOCAL-FIRST ", "local-first", False),
    ("auto", "auto", False),
    ("local-only", "claude-first", True),
])
def test_policy(raw, want, warns):
    env = {} if raw is None else {"LOYD_BRAIN_POLICY": raw}
    pol, warn = brain.policy(env)
    assert pol == want
    assert bool(warn) == warns


@pytest.mark.parametrize("text,want", [
    ("YES", True), ("yes, routine", True), ("**Yes**", True),
    ("NO", False), ("no.", False), ("maybe", None), ("", None), (None, None),
])
def test_parse_yes_no(text, want):
    assert brain.parse_yes_no(text) is want


def test_classify_failures_route_to_claude():
    def boom(_):
        raise OSError("connection refused")
    assert brain.classify("q", boom)[0] == brain.RESEARCH
    assert brain.classify("q", lambda m: "hmm, it depends")[0] == brain.RESEARCH


def test_plan_paths():
    called = []

    def chat(m):
        called.append(m)
        return "YES"
    assert brain.plan("claude-first", "q", chat)[0] == "claude-first"
    assert brain.plan("local-first", "q", chat)[0] == "local-first"
    assert not called                    # only auto asks the classifier
    assert brain.plan("auto", "is plex up?", chat)[0] == "local-first"
    assert "is plex up?" in called[0][0]["content"]
    assert brain.plan("auto", "q", lambda m: "NO")[0] == "claude-first"


# ---------- fake ollama ----------

class FakeOllama:
    """Serves /api/chat from a list of canned assistant messages, recording requests."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                msg = outer.replies.pop(0) if outer.replies else {"content": ""}
                out = json.dumps({"message": {"role": "assistant", **msg}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def ollama():
    made = []

    def make(replies):
        f = FakeOllama(replies)
        made.append(f)
        return f
    yield make
    for f in made:
        f.close()


def _call(name, **args):
    return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def test_outcome_final(ollama):
    o = ollama([{"content": "all good"}])
    a = LoydAgent(ollama_url=o.url, execute=lambda n, a: "ok")
    assert a.run("q") == "all good"
    assert a.last_outcome == "final"


def test_outcome_empty(ollama):
    o = ollama([{"content": "  "}])
    a = LoydAgent(ollama_url=o.url, execute=lambda n, a: "ok")
    a.run("q")
    assert a.last_outcome == "empty"


def test_outcome_step_limit(ollama):
    o = ollama([_call("ops_disk_usage"), _call("ops_memory"), {"content": "forced"}])
    a = LoydAgent(ollama_url=o.url, execute=lambda n, a: "ok", max_iters=3)
    assert a.run("q") == "forced"
    assert a.last_outcome == "step_limit"
    assert o.requests[-1]["tools"] == []          # conclude-guard still in force


def test_outcome_error():
    a = LoydAgent(ollama_url="http://127.0.0.1:9", execute=lambda n, a: "ok", timeout=2)
    a.run("q")
    assert a.last_outcome == "error"


# ---------- Session.ask routing ----------

@pytest.fixture
def session(monkeypatch, ollama):
    def make(replies, claude_raises=None):
        o = ollama(replies)
        s = cli.Session("fake-model", o.url, "claude-test", 4)
        log = []

        class FakeClaude:
            on_event = None

            def run(self, q):
                log.append(("claude", q))
                if claude_raises:
                    raise claude_raises
                return "claude answer"
        s._claude = FakeClaude()
        real_local = s.ask_local

        def local(q):
            log.append(("local", q))
            real_local(q)
        monkeypatch.setattr(s, "ask_local", local)
        s.log, s.fake = log, o
        return s
    return make


def test_default_is_claude_first(monkeypatch, session):
    monkeypatch.delenv("LOYD_BRAIN_POLICY", raising=False)
    s = session([])
    s.ask("why is the worker slow?")
    assert s.log == [("claude", "why is the worker slow?")]
    assert s.fake.requests == []                  # no classifier call


def test_claude_first_falls_back_local(monkeypatch, session):
    monkeypatch.setenv("LOYD_BRAIN_POLICY", "claude-first")
    s = session([{"content": "local says hi"}], claude_raises=ClaudeUnavailable("offline"))
    s.ask("q")
    assert [b for b, _ in s.log] == ["claude", "local"]


def test_local_first_no_escalation_on_success(monkeypatch, session):
    monkeypatch.setenv("LOYD_BRAIN_POLICY", "local-first")
    s = session([{"content": "disk is 40% full"}])
    s.ask("disk?")
    assert [b for b, _ in s.log] == ["local"]


def test_local_first_escalates_on_step_limit(monkeypatch, session):
    monkeypatch.setenv("LOYD_BRAIN_POLICY", "local-first")
    s = session([_call("ops_disk_usage"), _call("ops_memory"), _call("ops_gpu_status"),
                 {"content": "gave up"}])
    s.ask("q")
    assert [b for b, _ in s.log] == ["local", "claude"]


def test_local_first_escalation_survives_offline(monkeypatch, session, capsys):
    monkeypatch.setenv("LOYD_BRAIN_POLICY", "local-first")
    s = session([{"content": ""}], claude_raises=ClaudeUnavailable("offline"))
    s.ask("q")
    assert [b for b, _ in s.log] == ["local", "claude"]
    assert "cloud unavailable too" in capsys.readouterr().err


def test_auto_routes_routine_local(monkeypatch, session):
    monkeypatch.setenv("LOYD_BRAIN_POLICY", "auto")
    s = session([{"content": "YES"}, {"content": "plex is up"}])
    s.ask("is the plex container running?")
    assert [b for b, _ in s.log] == ["local"]
    assert s.fake.requests[0]["tools"] == []      # classifier gets no tools


def test_auto_routes_research_to_claude(monkeypatch, session):
    monkeypatch.setenv("LOYD_BRAIN_POLICY", "auto")
    s = session([{"content": "NO"}])
    s.ask("design a backup strategy for the NAS")
    assert [b for b, _ in s.log] == ["claude"]


def test_auto_classifier_down_routes_to_claude(monkeypatch, session):
    monkeypatch.setenv("LOYD_BRAIN_POLICY", "auto")
    s = session([])
    s.agent.url = "http://127.0.0.1:9/api/chat"
    s.agent.timeout = 2
    s.ask("q")
    assert [b for b, _ in s.log] == ["claude"]
