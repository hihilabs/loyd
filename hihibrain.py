"""
hihilabs brain tools for Loyd — all roads go through hihi (2026-11-20).

The claude.ai connector gave a session a shell on Tokyo7 and no way to read or
write hihilabs: no project brief, no "save this decision", no handoff. A plan
written in a claude.ai session on 2026-11-18 sat outside hihi for two days.

This passes hihilabs' own brain tools through, unchanged: project_brief,
create_project_memory, create_project_note, list_projects, get_project,
task_brief, log_handoff, list_open_gaps, search_history. Nothing is redefined
here. The tool list and schemas are read from hihilabs at start-up, and every
call is handed to the same door the VS Code sessions use (hihilabs
scripts/brain_door.sh, verb `mcp`), which keeps its own allowlist, its own audit
log and its "never arm a paid API" rule.

  LOYD_BRAIN_CMD   the command that runs that door, speaking MCP over stdio.
                   Unset = no brain tools (the default; the CLI stays as it was).
                   On the VPS, beside hihilabs:
                     /usr/sbin/runuser -u hihilabs.xyz -- /bin/bash /var/www/vhosts/hihilabs.xyz/app/scripts/brain_door.sh
                   From another machine, through the brain key:
                     ssh -F /dev/null -i ~/.ssh/hihilabs_brain -o IdentitiesOnly=yes -o BatchMode=yes root@<vps public ip> mcp
  LOYD_BRAIN_TIMEOUT   seconds per call (default 60)

One short-lived process per call: start, initialize, call, read the answer, then end.
Slower than a held connection by a Django boot, and it cannot leak or wedge.
These are notes, not ops: read-only tokens get them too (leaving a note is the
point), and they act on hihilabs, never on LOYD_REMOTE. Stdlib only.
"""
import json
import os
import shlex
import subprocess
import threading

ALLOWED = ("project_brief", "create_project_memory", "create_project_note", "list_projects",
           "get_project", "task_brief", "log_handoff", "list_open_gaps", "search_history")
_PROTOCOL = "2024-11-05"


def command():
    raw = os.getenv("LOYD_BRAIN_CMD", "").strip()
    return shlex.split(raw) if raw else []


def _timeout():
    try:
        return max(5, int(os.getenv("LOYD_BRAIN_TIMEOUT", "60")))
    except ValueError:
        return 60


def _rpc(requests, want_id, cmd=None):
    """Run the door once: send the handshake and `requests`, return the result of `want_id`.
    Raises RuntimeError with a plain reason on any failure."""
    cmd = cmd or command()
    if not cmd:
        raise RuntimeError("hihilabs brain tools are not configured (LOYD_BRAIN_CMD is unset)")
    lines = [{"jsonrpc": "2.0", "id": 0, "method": "initialize",
              "params": {"protocolVersion": _PROTOCOL, "capabilities": {},
                         "clientInfo": {"name": "loyd-hihibrain", "version": "1"}}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"}] + list(requests)
    env = dict(os.environ, SSH_ORIGINAL_COMMAND="mcp")      # the door's verb when run locally
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env)
    except OSError as e:
        raise RuntimeError(f"could not start the hihilabs door: {e}")
    # stdin stays open until the answer is read: hihilabs' server drops a call that is
    # still running when its input ends. A reader thread makes the wait interruptible.
    found, err_tail = {}, []

    def read_out():
        for line in p.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == want_id:
                found["msg"] = msg
                return

    def read_err():
        for line in p.stderr:
            if line.strip():
                err_tail[:] = [line.strip()]

    t_out = threading.Thread(target=read_out, daemon=True)
    t_err = threading.Thread(target=read_err, daemon=True)
    t_out.start(); t_err.start()
    try:
        try:
            p.stdin.write("".join(json.dumps(m) + "\n" for m in lines))
            p.stdin.flush()
        except (BrokenPipeError, OSError):
            pass                                            # it died early; the reader reports why
        t_out.join(_timeout())
        timed_out = t_out.is_alive()
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(3)
    msg = found.get("msg")
    if msg is None:
        if timed_out:
            raise RuntimeError(f"hihilabs did not answer within {_timeout()} s")
        tail = err_tail[0] if err_tail else "no output"
        raise RuntimeError(f"hihilabs gave no answer (exit {p.returncode}): {tail[:300]}")
    if "error" in msg:
        raise RuntimeError(str((msg["error"] or {}).get("message") or msg["error"])[:400])
    return msg.get("result") or {}


def discover(cmd=None):
    """[OpenAI-style tool schema] for the brain tools hihilabs offers, limited to ALLOWED.
    [] when the door is not configured or does not answer: Loyd starts without them."""
    if not (cmd or command()):
        return []
    try:
        tools = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], 1, cmd).get("tools") or []
    except RuntimeError:
        return []
    out = []
    for t in tools:
        name = t.get("name")
        if name in ALLOWED:
            out.append({"type": "function", "function": {
                "name": name,
                "description": "[hihilabs] " + str(t.get("description") or "")[:900],
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}})
    return out


def call(name, args, cmd=None):
    if name not in ALLOWED:
        return f"ERROR: '{name}' is not a hihilabs brain tool."
    try:
        res = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": name, "arguments": args or {}}}], 1, cmd)
    except RuntimeError as e:
        return f"ERROR: {e}"
    text = "\n".join(c.get("text", "") for c in (res.get("content") or []) if c.get("type") == "text")
    if res.get("isError"):
        return f"ERROR: {text[:2000] or 'hihilabs refused the call'}"
    return text or "(empty answer)"


def _guarded(name, args, readonly):
    """Read-only tokens may leave notes, never a hard stop: a memory titled 'STOP: …' is
    read by every agent as an order to halt, which is an operator's call (red team 2026-11-20)."""
    if readonly and name == "create_project_memory" and str((args or {}).get("title") or "").strip().upper().startswith("STOP"):
        return "DENIED: a hard stop ('STOP: …' memory) needs an operator token. Save it as an ordinary memory, or ask an operator."
    return call(name, args)


def install(tool_schemas, impl):
    """Add the brain tools to Loyd's tool table. Returns the names installed."""
    names = []
    for schema in discover():
        name = schema["function"]["name"]
        if name in impl:                                   # never shadow one of Loyd's own tools
            continue
        tool_schemas.append(schema)
        impl[name] = (lambda n: lambda args, readonly=False: _guarded(n, args, readonly))(name)
        names.append(name)
    return names
