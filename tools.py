"""
Loyd tools — inspection (read-only) + a few confirm-gated write actions.

Each tool has an ollama function schema (TOOL_SCHEMAS) and an implementation.
execute_tool() dispatches by name and always returns a string (the tool result
the model sees). run_shell refuses obviously-mutating commands — a best-effort
denylist for a trusted local box + CLI-visible runs, NOT a security sandbox.
State-changing actions live in separate WRITE_TOOLS (write_file,
restart_container, prune_images); the harness gates each on operator
confirmation, so destructive work never rides through run_shell unconfirmed.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

MAX_OUTPUT_CHARS = 2500   # cap tool output so a small model isn't flooded
MAX_OUTPUT_LINES = 50
SHELL_TIMEOUT_S  = 30

# Where tools ACT. Empty = this machine. Set LOYD_REMOTE="root@HOST" (+
# LOYD_REMOTE_PORT, default 22) to run shell/docker/file tools on another box over
# SSH — e.g. act on the server's stack from a laptop. The brain (ollama) is separate:
# it's chosen by OLLAMA_URL, so you can think on one GPU and act on another host.
_REMOTE      = os.getenv("LOYD_REMOTE", "").strip()
_REMOTE_PORT = os.getenv("LOYD_REMOTE_PORT", "22").strip()


def _exec(command: str):
    """Run a bash command locally, or on LOYD_REMOTE via SSH when set.
    Uses a NON-login shell (bash -c): login shells (-l) source /etc/profile +
    ~/.bash_profile, which on managed hosts print banners (e.g. Plesk) that
    pollute tool output — and give no benefit here since docker/standard tools
    are on the default PATH."""
    if _REMOTE:
        full = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-p", _REMOTE_PORT, _REMOTE, f"bash -c {shlex.quote(command)}"]
    else:
        full = ["bash", "-c", command]
    return subprocess.run(full, capture_output=True, text=True, timeout=SHELL_TIMEOUT_S)


def where() -> str:
    return _REMOTE or "local"

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
    # --- write tools (v0.2): the harness requires operator confirmation before each runs ---
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write (create/overwrite) a text file. REQUIRES operator confirmation.",
        "parameters": {"type": "object",
            "properties": {"path": {"type": "string", "description": "absolute file path"},
                           "content": {"type": "string", "description": "full new file contents"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "restart_container",
        "description": "Restart a docker container by exact name. REQUIRES operator confirmation.",
        "parameters": {"type": "object",
            "properties": {"name": {"type": "string", "description": "exact container name"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "prune_images",
        "description": "Remove unused docker images to reclaim disk. scope='dangling' (default) "
                       "removes only untagged <none> images and is safe; scope='all' removes every "
                       "image not used by any container. Neither touches running containers. "
                       "REQUIRES operator confirmation.",
        "parameters": {"type": "object",
            "properties": {"scope": {"type": "string", "enum": ["dangling", "all"],
                                     "description": "'dangling' (default, untagged only) or 'all' (all unused)"}},
            "required": []}}},
]

# Tools that mutate state — the harness must get operator confirmation before running these.
WRITE_TOOLS = {"write_file", "restart_container", "prune_images"}

# Mutating command binaries / patterns we refuse in read-only mode.
_BLOCKED = re.compile(r"""(?xi)
    (^|[\s;&|()`])(
        rm|rmdir|mv|dd|mkfs\w*|shred|truncate|tee|chmod|chown|chgrp|ln|
        kill|pkill|killall|reboot|shutdown|halt|poweroff|init|
        systemctl|service|mount|umount|fdisk|parted|wipefs|sfdisk|
        apt|apt-get|yum|dnf|pacman|pip|pip3|npm|yarn|make|
        mkdir|touch|install|useradd|userdel|passwd|crontab|sysctl
    )([\s;&|)]|$)
  # docker: refuse ANY mutating verb anywhere after `docker` (up to a shell
  # separator), so `docker image prune`, `docker system prune -a -f`, `docker rmi`,
  # `docker container rm`, `docker builder prune`, etc. are all caught — not just the
  # bare `docker prune`/`docker rm` forms the old pattern matched. The required
  # whitespace before the verb keeps hyphenated names (e.g. `my-restart-app`) allowed.
  | \bdocker\b[^\n;&|]*?\s(prune|rm|rmi|remove|stop|start|restart|kill|pause|unpause|
       create|run|exec|build|rebuild|tag|push|pull|load|save|import|export|commit|
       update|rename|cp|up|down|deploy|scale|init|leave)\b
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
        p = _exec(command)
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
    if _REMOTE:
        cmd = (f"head -n {int(max_lines)} {shlex.quote(path)}"
               if max_lines and max_lines > 0 else f"cat {shlex.quote(path)}")
        try:
            p = _exec(cmd)
        except Exception as e:
            return f"ERROR reading {path} on {_REMOTE}: {type(e).__name__}: {e}"
        if p.returncode != 0:
            return f"ERROR reading {path} on {_REMOTE}: {p.stderr.strip()}"
        return _cap(p.stdout)
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
    if not _REMOTE and not shutil.which("docker"):
        return "ERROR: docker CLI not available on this host."
    fmt = ('{"running":{{.State.Running}},"status":"{{.State.Status}}",'
           '"health":"{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}",'
           '"image":"{{.Config.Image}}","started":"{{.State.StartedAt}}"}')
    def _inspect(cname):
        p = _exec(f"docker inspect -f {shlex.quote(fmt)} {shlex.quote(cname)}")
        if p.returncode != 0:
            return None
        try:
            return json.loads(p.stdout.strip())
        except Exception:
            return {"raw": p.stdout.strip()}

    try:
        d = _inspect(name)                       # exact name first
        if d is not None:
            return json.dumps(d)
        # fuzzy: prefer NAME-PREFIX matches (so 'plex' -> 'Plex-Media-Server', not the
        # 'octoplex-*' substring hits); fall back to substring only if nothing prefixes.
        lp = _exec("docker ps -a --format '{{.Names}}'")
        names = lp.stdout.split() if lp.returncode == 0 else []
        q = name.lower()
        hits = [n for n in names if n.lower().startswith(q)] or [n for n in names if q in n.lower()]
        if len(hits) == 1:
            d = _inspect(hits[0]) or {}
            d["matched_name"] = hits[0]
            return json.dumps(d)
        if len(hits) > 1:
            return (f"No container is named exactly '{name}'. Closest matches: "
                    f"{', '.join(hits[:10])}. Re-ask about one by its exact name.")
        return f"No container matching '{name}'."
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def write_file(path: str, content: str) -> str:
    try:
        pth = Path(path)
        pth.parent.mkdir(parents=True, exist_ok=True)
        existed = pth.exists()
        pth.write_text(content if content is not None else "")
        return f"{'Overwrote' if existed else 'Created'} {path} ({len(content or '')} chars)."
    except Exception as e:
        return f"ERROR writing {path}: {type(e).__name__}: {e}"


def restart_container(name: str) -> str:
    if not _REMOTE and not shutil.which("docker"):
        return "ERROR: docker CLI not available on this host."
    try:
        p = _exec(f"docker restart {shlex.quote(name)}")
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    if p.returncode != 0:
        return f"ERROR restarting {name}: {p.stderr.strip() or p.stdout.strip()}"
    return f"Restarted container {name}."


def prune_images(scope: str = "dangling") -> str:
    """Confirm-gated: reclaim disk by removing unused images. Runs its own docker
    command via _exec (not run_shell), so it's exempt from the read-only denylist —
    the operator confirmation in the harness is the gate instead."""
    if not _REMOTE and not shutil.which("docker"):
        return "ERROR: docker CLI not available on this host."
    scope = (scope or "dangling").strip().lower()
    if scope == "all":
        cmd = "docker image prune -a -f"
    elif scope == "dangling":
        cmd = "docker image prune -f"
    else:
        return f"ERROR: scope must be 'dangling' or 'all', got {scope!r}."
    try:
        p = _exec(cmd)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    if p.returncode != 0:
        return f"ERROR pruning {scope} images: {p.stderr.strip() or p.stdout.strip()}"
    return _cap((p.stdout or "").strip() or f"Nothing to remove ({scope} images).")


_IMPL = {
    "run_shell": lambda a: run_shell(a.get("command", "")),
    "read_file": lambda a: read_file(a.get("path", ""), a.get("max_lines")),
    "get_container_status": lambda a: get_container_status(a.get("name", "")),
    "write_file": lambda a: write_file(a.get("path", ""), a.get("content", "")),
    "restart_container": lambda a: restart_container(a.get("name", "")),
    "prune_images": lambda a: prune_images(a.get("scope", "dangling")),
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
