"""
Loyd v0.1 tools — READ-ONLY.

Each tool has an ollama function schema (TOOL_SCHEMAS) and an implementation.
execute_tool() dispatches by name and always returns a string (the tool result
the model sees). Read-only by design: run_shell blocks obviously-mutating
commands. This is a best-effort guard for a trusted local box + CLI-visible
runs, NOT a security sandbox. Write/deploy tools come in a later version.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

MAX_OUTPUT_CHARS = 2500   # cap tool output so a small model isn't flooded
MAX_OUTPUT_LINES = 50
SHELL_TIMEOUT_S  = 30

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "run_shell",
        "description": "Run a READ-ONLY shell command on the Tokyo7 host and return its stdout/stderr. "
                       "Use for inspection: ls, cat, df, docker ps, docker logs, grep, ps, nvidia-smi, etc. "
                       "Mutating commands (rm, mv, docker stop/restart, git push, writes) are refused.",
        "parameters": {"type": "object",
            "properties": {"command": {"type": "string", "description": "the shell command to run"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file from disk and return its contents (optionally capped to the first N lines).",
        "parameters": {"type": "object",
            "properties": {"path": {"type": "string", "description": "absolute file path"},
                           "max_lines": {"type": "integer", "description": "optional cap on lines returned"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "get_container_status",
        "description": "Get the state of a docker container by exact name (running?, status, image, health).",
        "parameters": {"type": "object",
            "properties": {"name": {"type": "string", "description": "exact container name"}},
            "required": ["name"]}}},
]

# Mutating command binaries / patterns we refuse in read-only mode.
_BLOCKED = re.compile(r"""(?xi)
    (^|[\s;&|()`])(
        rm|rmdir|mv|dd|mkfs\w*|shred|truncate|tee|chmod|chown|chgrp|ln|
        kill|pkill|killall|reboot|shutdown|halt|poweroff|init|
        systemctl|service|mount|umount|fdisk|parted|wipefs|sfdisk|
        apt|apt-get|yum|dnf|pacman|pip|pip3|npm|yarn|make|
        mkdir|touch|install|useradd|userdel|passwd|crontab|sysctl
    )([\s;&|)]|$)
  | \bdocker\s+(stop|rm|kill|restart|start|down|up|prune|create|build|exec|run|compose|tag|push|pull|load|save|update|rename)\b
  | \bgit\s+(push|reset|checkout|clean|rebase|commit|merge|pull|fetch|tag|remote|branch\s+-[dD]|stash\s+drop)\b
  | \bsed\b[^|]*\s-i
  | \bperl\b[^|]*\s-i
  | (?<![0-9])>>?(?!\s*(/dev/null|&))   # output redirection to a real file (allow >/dev/null, 2>&1)
""")


def _cap(s: str) -> str:
    lines = s.split("\n")
    if len(lines) > MAX_OUTPUT_LINES:
        s = "\n".join(lines[:MAX_OUTPUT_LINES]) + f"\n... [truncated, {len(lines) - MAX_OUTPUT_LINES} more lines]"
    if len(s) > MAX_OUTPUT_CHARS:
        s = s[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(s) - MAX_OUTPUT_CHARS} more chars]"
    return s


def run_shell(command: str) -> str:
    command = (command or "").strip()
    if not command:
        return "ERROR: empty command."
    if _BLOCKED.search(command):
        return ("REFUSED: this looks like a mutating/destructive command and Loyd is read-only for now. "
                "Describe what you want changed instead, and the operator can do it.")
    try:
        p = subprocess.run(["bash", "-lc", command], capture_output=True, text=True,
                           timeout=SHELL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {SHELL_TIMEOUT_S}s."
    except Exception as e:
        return f"ERROR running command: {type(e).__name__}: {e}"
    out = p.stdout or ""
    if p.stderr:
        out += ("\n[stderr]\n" + p.stderr)
    if not out.strip():
        out = f"(no output; exit code {p.returncode})"
    return _cap(out.rstrip())


def read_file(path: str, max_lines: int = None) -> str:
    try:
        pth = Path(path)
        if not pth.is_file():
            return f"ERROR: not a file: {path}"
        with pth.open("r", errors="replace") as f:
            if max_lines and max_lines > 0:
                lines = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        lines.append(f"... [truncated at {max_lines} lines]")
                        break
                    lines.append(line.rstrip("\n"))
                return _cap("\n".join(lines))
            return _cap(f.read())
    except Exception as e:
        return f"ERROR reading {path}: {type(e).__name__}: {e}"


def get_container_status(name: str) -> str:
    if not shutil.which("docker"):
        return "ERROR: docker CLI not available on this host."
    fmt = ('{"running":{{.State.Running}},"status":"{{.State.Status}}",'
           '"health":"{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}",'
           '"image":"{{.Config.Image}}","started":"{{.State.StartedAt}}"}')
    try:
        p = subprocess.run(["docker", "inspect", "-f", fmt, name],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    if p.returncode != 0:
        return f"No such container '{name}' (or docker error): {p.stderr.strip()}"
    try:
        return json.dumps(json.loads(p.stdout.strip()))
    except Exception:
        return p.stdout.strip()


_IMPL = {
    "run_shell": lambda a: run_shell(a.get("command", "")),
    "read_file": lambda a: read_file(a.get("path", ""), a.get("max_lines")),
    "get_container_status": lambda a: get_container_status(a.get("name", "")),
}


def execute_tool(name: str, args: dict) -> str:
    fn = _IMPL.get(name)
    if not fn:
        return f"ERROR: unknown tool '{name}'. Available: {', '.join(_IMPL)}"
    if not isinstance(args, dict):
        return f"ERROR: tool arguments must be an object, got {type(args).__name__}"
    try:
        return fn(args)
    except Exception as e:
        return f"ERROR in tool {name}: {type(e).__name__}: {e}"
