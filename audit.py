"""
Loyd audit log — one JSON line per tool call: who, via what, which tool, what
args, outcome, how long. Append-only, stdlib-only, thread-safe (the MCP server
runs tools on worker threads).

  LOYD_AUDIT_LOG       path (default ~/.loyd/audit.jsonl); "off" disables.
  LOYD_AUDIT_MAX_MB    rotate to <path>.1 past this size (default 20; one old file kept).

Arguments are logged as given except write_file's content, which is replaced by
its length + sha256 (file bodies can be huge or secret). Long strings are
truncated. Commands are logged verbatim — don't paste secrets into run_shell.
"""
import hashlib
import json
import os
import threading
import time

_lock = threading.Lock()
_MAX_ARG = 500


def _path():
    p = os.getenv("LOYD_AUDIT_LOG", "~/.loyd/audit.jsonl").strip()
    return None if p.lower() in ("off", "none", "0", "") else os.path.expanduser(p)


def _scrub(name, args):
    out = {}
    for k, v in (args or {}).items():
        if name == "write_file" and k == "content":
            b = (v or "").encode()
            out[k] = {"chars": len(v or ""), "sha256": hashlib.sha256(b).hexdigest()[:16]}
        elif isinstance(v, str) and len(v) > _MAX_ARG:
            out[k] = v[:_MAX_ARG] + f"…(+{len(v) - _MAX_ARG})"
        else:
            out[k] = v
    return out


def outcome_of(result: str) -> str:
    head = (result or "")[:10].upper()
    for tag in ("DENIED", "REFUSED", "ERROR"):
        if head.startswith(tag):
            return tag.lower()
    return "ok"


def record(actor, tool, args, outcome, result_chars=0, dt=0.0, readonly=None):
    path = _path()
    if not path:
        return
    line = json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "actor": actor, "tool": tool,
        "args": _scrub(tool, args), "outcome": outcome, "readonly": readonly,
        "result_chars": result_chars, "ms": int(dt * 1000),
    }, default=str)
    try:
        with _lock:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            _rotate(path)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line + "\n")
    except OSError:
        pass   # auditing must never break a tool call


def _rotate(path):
    try:
        limit = float(os.getenv("LOYD_AUDIT_MAX_MB", "20")) * 1024 * 1024
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".1")
    except (OSError, ValueError):
        pass


def tail(n=20):
    """Last n records (for `/audit` in the CLI)."""
    path = _path()
    if not path or not os.path.exists(path):
        return []
    with open(path, errors="replace") as f:
        lines = f.readlines()[-n:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out
