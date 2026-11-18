"""
Loyd trace recorder — full-session traces for evaluation and (local-model)
fine-tuning data, opt-in with LOYD_TRACE_DIR.

One JSON line per answered question in <dir>/traces-YYYYMMDD.jsonl:
  {id, ts, brain, model, actor, task, steps:[{tool,args,result,declined}], final,
   outcome, trainable}

The rule that keeps us inside Anthropic's terms: **brain "claude" is never
trainable**, here and again in scripts/export_training.py (which re-checks the
brain field, so an edited file can't leak Claude output). Local-model traces are
trainable only after a person rates them: `/good` or `/bad` in the CLI appends
{"review": <id>, "verdict": ...}. Export takes trainable + reviewed-good only.
"""
import json
import os
import time
import uuid

MAX_RESULT = 4000
BRAINS = ("claude", "local", "human")


def trace_dir():
    d = os.getenv("LOYD_TRACE_DIR", "").strip()
    return os.path.expanduser(d) if d else None


def _path(d):
    return os.path.join(d, time.strftime("traces-%Y%m%d.jsonl"))


def _append(rec):
    d = trace_dir()
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        fd = os.open(_path(d), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass        # tracing must never break an answer


class Tracer:
    def __init__(self, brain, model, task, actor="cli"):
        if brain not in BRAINS:
            raise ValueError(f"brain must be one of {BRAINS}")
        self.rec = {"id": uuid.uuid4().hex[:12], "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "brain": brain, "model": model, "actor": actor, "task": task,
                    "steps": [], "final": None, "outcome": None,
                    "trainable": brain != "claude"}
        self._pending = None
        self.written = False

    def on_event(self, kind, data):
        if kind == "tool_call":
            self._pending = {"tool": data.get("name"), "args": data.get("args") or {}}
        elif kind in ("tool_result", "tool_declined") and self._pending:
            step = dict(self._pending)
            if kind == "tool_declined":
                step["declined"] = True
            else:
                r = data.get("result") or ""
                step["result"] = r if len(r) <= MAX_RESULT else r[:MAX_RESULT] + "…[truncated]"
            self.rec["steps"].append(step)
            self._pending = None
        elif kind == "final":
            self.finish(data.get("text", ""), "answered")
        elif kind == "error":
            self.finish(data.get("text", ""), "error")

    def finish(self, final, outcome):
        if self.written:
            return
        self.rec.update(final=final, outcome=outcome)
        if outcome != "answered":
            self.rec["trainable"] = False
        if self.rec["brain"] == "claude":
            self.rec["trainable"] = False           # never, whatever happened above
        _append(self.rec)
        self.written = True

    def abort(self, reason):
        """The run stopped without an answer (e.g. the cloud dropped mid-task)."""
        if self.rec["steps"] and not self.written:
            self.finish(reason, "aborted")


def review(trace_id, verdict):
    if verdict not in ("good", "bad"):
        raise ValueError("verdict must be good or bad")
    _append({"review": trace_id, "verdict": verdict, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")})


def chain(*hooks):
    def on_event(kind, data):
        for h in hooks:
            if h:
                h(kind, data)
    return on_event
