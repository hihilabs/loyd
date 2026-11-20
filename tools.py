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
import time
from pathlib import Path

import audit

# Cap tool output so a small model isn't flooded. Head AND tail are kept (errors
# and summaries live at the end). The MCP server raises these — full Claude can
# take much more. Override with LOYD_MAX_OUTPUT_CHARS / LOYD_MAX_OUTPUT_LINES.
MAX_OUTPUT_CHARS = int(os.getenv("LOYD_MAX_OUTPUT_CHARS", "2500"))
MAX_OUTPUT_LINES = int(os.getenv("LOYD_MAX_OUTPUT_LINES", "50"))
SHELL_TIMEOUT_S  = 30

# Where tools ACT. Empty = this machine. Set LOYD_REMOTE="root@HOST" (+
# LOYD_REMOTE_PORT, default 22) to run shell/docker/file tools on another box over
# SSH — e.g. act on the server's stack from a laptop. The brain (ollama) is separate:
# it's chosen by OLLAMA_URL, so you can think on one GPU and act on another host.
_REMOTE      = os.getenv("LOYD_REMOTE", "").strip()
_REMOTE_PORT = os.getenv("LOYD_REMOTE_PORT", "22").strip()
# Optional OS-enforced read-only path: an unprivileged SSH login (no sudo, not in
# the docker group) that read-only callers' run_shell/read_file go through. The
# regex denylist is best-effort; this is the real boundary. See MCP.md.
_REMOTE_RO   = os.getenv("LOYD_REMOTE_READONLY", "").strip()

# Capability mode. "readonly" (default): run_shell refuses every mutating command
# (see _BLOCKED); state changes go only through confirm-gated WRITE_TOOLS.
# "operator": run_shell may run ANYTHING except a few catastrophic, irreversible
# commands (see _CATASTROPHIC), and the harness still shows a y/N confirm for each
# individually-destructive command (see is_destructive) so a human approves it.
# Set LOYD_MODE=operator to do real dev/ops work on a trusted host.
_MODE = os.getenv("LOYD_MODE", "readonly").strip().lower()


def _exec(command: str, input: str = None, ro: bool = False, timeout: int = None):
    """Run a bash command locally, or on LOYD_REMOTE via SSH when set.
    Uses a NON-login shell (bash -c): login shells (-l) source /etc/profile +
    ~/.bash_profile, which on managed hosts print banners (e.g. Plesk) that
    pollute tool output — and give no benefit here since docker/standard tools
    are on the default PATH."""
    target = _REMOTE_RO if (ro and _REMOTE_RO) else _REMOTE
    if target:
        full = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-p", _REMOTE_PORT, target, f"bash -c {shlex.quote(command)}"]
    else:
        full = ["bash", "-c", command]
    return subprocess.run(full, capture_output=True, text=True,
                          timeout=timeout or SHELL_TIMEOUT_S, input=input)


def where() -> str:
    return _REMOTE or "local"


def mode() -> str:
    return "operator" if _MODE == "operator" else "readonly"

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "run_shell",
        "description": "Run a shell command on the host Loyd is acting on and return its stdout/stderr. "
                       "In read-only mode, mutating commands (rm, mv, docker stop/restart, git push, "
                       "writes) are refused. In operator mode they run — destructive ones after operator "
                       "confirmation, and a few catastrophic ones (disk wipe, system-root delete, power-off) "
                       "are always refused.",
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
    {"type": "function", "function": {
        "name": "audit_tail",
        "description": "Operators only. Recent entries from Loyd's own audit log: who ran which tool, when, "
                       "and the outcome. Answers 'what did Loyd do today?' or 'what got refused?'.",
        "parameters": {"type": "object",
            "properties": {"lines": {"type": "integer", "description": "entries to show (default 20, max 200)"},
                           "actor": {"type": "string", "description": "only this actor, e.g. mcp:geoff or cli:root"},
                           "outcome": {"type": "string", "enum": ["", "ok", "denied", "refused", "error",
                                                                  "declined", "throttled"],
                                       "description": "only this outcome"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "run_in_tmux",
        "description": "Start a LONG-running shell command (build, backup, big docker pull, test suite) as a "
                       "background job in Loyd's own tmux server and return at once — use instead of run_shell "
                       "for anything that may take over 30 seconds. Same rules as run_shell. Then read its output "
                       "with job_output. People can watch it live: tmux -L loyd attach -t jobs.",
        "parameters": {"type": "object",
            "properties": {"name": {"type": "string", "description": "short job name: a-z 0-9 _ -"},
                           "command": {"type": "string", "description": "the shell command to run"}},
            "required": ["name", "command"]}}},
    {"type": "function", "function": {
        "name": "job_output",
        "description": "Read the latest output of a background job started with run_in_tmux, and whether it "
                       "is still running or finished (with its exit code).",
        "parameters": {"type": "object",
            "properties": {"name": {"type": "string", "description": "the job name"},
                           "lines": {"type": "integer", "description": "lines from the end (default 50, max 400)"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "tmux_read",
        "description": "Read what is on screen in someone's tmux pane (read-only), e.g. target 'dev:build' or "
                       "'loyd:0.1', to see a build error or log they're looking at. Lists panes if target is empty.",
        "parameters": {"type": "object",
            "properties": {"target": {"type": "string", "description": "session:window.pane, or empty to list panes"},
                           "lines": {"type": "integer", "description": "lines of history (default 60, max 400)"}},
            "required": []}}},
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
# Read-only but sensitive: operators only (hidden from and denied to read-only callers),
# with no y/N prompt since nothing changes.
OPERATOR_TOOLS = {"audit_tail"}

# Mutating command binaries / patterns we refuse in read-only mode.
_BLOCKED = re.compile(r"""(?xi)
    (^|[\s;&|()`])(
        rm|rmdir|mv|dd|mkfs\w*|shred|truncate|tee|chmod|chown|chgrp|ln|
        kill|pkill|killall|reboot|shutdown|halt|poweroff|init|
        systemctl|service|mount|umount|fdisk|parted|wipefs|sfdisk|
        apt|apt-get|yum|dnf|pacman|pip|pip3|npm|yarn|make|
        mkdir|touch|install|useradd|userdel|passwd|crontab|sysctl|
        cp|rsync|scp|sftp|wget|unzip|patch|eval|exec|source|sudo|su|doas|nohup
    )([\s;&|)]|$)
  # inline code: an interpreter running a -c/-e string or a heredoc/stdin can do
  # anything the denylist forbids (python3 -c "open(p,'w')"), so refuse them all
  | \b(python[0-9.]*|perl|ruby|node|nodejs|php|lua|bash|sh|zsh|dash|ksh|awk|gawk|osascript)\b
      [^\n;&|]*\s(-c|-e|-E|-r|--eval|-)(\s|$)
  | \b(python[0-9.]*|perl|ruby|node|php|bash|sh|zsh)\s*<<
  | \|\s*(python[0-9.]*|perl|ruby|node|php|bash|sh|zsh|dash|ksh)\b
  | \bawk\b[^\n]*\b(system|print\s*>)
  | \bcurl\b[^\n;&|]*\s(-o|-O|--output|--remote-name|-T|--upload-file|-X\s*(POST|PUT|DELETE|PATCH)|-d|--data\S*)\b
  | \bfind\b[^\n;&|]*\s-(delete|exec|execdir|ok|fprint\w*)\b
  | \bxargs\b
  | \btar\s+-?[a-z]*[xcru]           # extract/create/append/update (t = list is fine)
  | \bgit\s+(apply|am|restore|switch|worktree|config|gc|prune|update-ref|submodule)\b
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

# Irreversible, box-bricking commands — refused even in operator mode, even if the
# operator confirmed. Last-resort backstop; the confirm gate below already shows the
# operator every destructive command, so this only has to catch the un-take-backs:
# wiping a disk, deleting a system root, or powering the box off.
_CATASTROPHIC = re.compile(r"""(?xi)
    \brm\s+(-\S+\s+)*(/(\s|$)|/\*|~(\s|/|$)|\$HOME|/(etc|boot|bin|sbin|usr|lib\w*|var|root|dev|proc|sys)(/|\s|$))
  | \b(mkfs\w*|wipefs|sfdisk|fdisk|parted)\b
  | \bdd\b.*\bof=/dev/
  | >\s*/dev/(sd|nvme|hd|vd|mmcblk)
  | \bshred\b.*\s/dev/
  | \b(shutdown|reboot|halt|poweroff|telinit)\b
  | \binit\s+[06]\b
  | :\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:[^}]*&   # fork bomb  :(){ :|:& };:
""")


def is_destructive(command: str) -> bool:
    """True if a shell command mutates state (matches the read-only denylist).
    The harness uses this in operator mode to show a y/N confirm for each such
    command — so operator mode runs safe commands freely but gates destructive ones."""
    return bool(_BLOCKED.search(command or ""))


def _cap(s: str) -> str:
    """Trim to MAX_OUTPUT_LINES / MAX_OUTPUT_CHARS keeping ~2/3 head + ~1/3 tail,
    so the end of a log / the error at the bottom of a build survives."""
    lines = s.split("\n")
    if len(lines) > MAX_OUTPUT_LINES:
        head = max(1, MAX_OUTPUT_LINES * 2 // 3)
        tail = max(1, MAX_OUTPUT_LINES - head)
        s = "\n".join(lines[:head]
                      + [f"... [{len(lines) - head - tail} lines omitted — narrow with grep/head/tail] ..."]
                      + lines[-tail:])
    if len(s) > MAX_OUTPUT_CHARS:
        head = MAX_OUTPUT_CHARS * 2 // 3
        tail = MAX_OUTPUT_CHARS - head
        s = s[:head] + f"\n... [{len(s) - head - tail} chars omitted] ...\n" + s[-tail:]
    return s


def run_shell(command: str, readonly: bool = False) -> str:
    """readonly=True forces read-only rules for THIS call even if the server runs in
    operator mode (an MCP read-only token, an OAuth grant scoped read-only). It can
    only restrict: a readonly-mode server never runs operator rules."""
    command = (command or "").strip()
    if not command:
        return "ERROR: empty command."
    ro = readonly or _MODE != "operator"
    refusal = _shell_gate(command, ro)      # the harness confirms destructive ones first
    if refusal:
        return refusal
    try:
        p = _exec(command, ro=ro)
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


def read_file(path: str, max_lines: int = None, readonly: bool = False) -> str:
    ro = readonly or _MODE != "operator"
    target = _REMOTE_RO if (ro and _REMOTE_RO) else _REMOTE
    if target:
        cmd = (f"head -n {int(max_lines)} -- {shlex.quote(path)}"
               if max_lines and max_lines > 0 else f"cat -- {shlex.quote(path)}")
        try:
            p = _exec(cmd, ro=ro)
        except Exception as e:
            return f"ERROR reading {path} on {target}: {type(e).__name__}: {e}"
        if p.returncode != 0:
            return f"ERROR reading {path} on {target}: {p.stderr.strip()}"
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


JOB_SERVER = "loyd"            # tmux -L loyd: Loyd's own tmux server, never the user's sessions
JOB_SESSION = "jobs"
JOB_LOG_DIR = "/tmp/loyd-jobs"
_JOB_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_TMUX_TARGET = re.compile(r"^[A-Za-z0-9_-]{1,64}(:[A-Za-z0-9_-]{1,64}(\.[0-9]{1,3})?)?$")


def _shell_gate(command: str, ro: bool):
    """The run_shell rules, shared: None if allowed, else the refusal text."""
    if not ro:
        if _CATASTROPHIC.search(command):
            return ("REFUSED: this is a catastrophic, irreversible command (disk wipe, "
                    "system-root delete, or power-off). Loyd never runs it, even in operator "
                    "mode — do it by hand if you truly mean to.")
        return None
    if _BLOCKED.search(command):
        return ("REFUSED: this looks like a mutating/destructive command and Loyd is in "
                "read-only mode. Set LOYD_MODE=operator to allow changes (destructive ones "
                "still prompt for confirmation), or use a confirm-gated write tool.")
    return None


def run_in_tmux(name: str, command: str, readonly: bool = False) -> str:
    name, command = (name or "").strip(), (command or "").strip()
    if not _JOB_NAME.match(name):
        return "ERROR: job name must be 1-32 chars of a-z 0-9 _ - (starting with a letter or digit)."
    if not command:
        return "ERROR: empty command."
    ro = readonly or _MODE != "operator"
    refusal = _shell_gate(command, ro)
    if refusal:
        return refusal
    log = f"{JOB_LOG_DIR}/{name}.log"
    inner = (f"{{ {command}\n}} 2>&1 | tee -a {log}; "
             f"echo \"[loyd-job exit ${{PIPESTATUS[0]}}]\" | tee -a {log}")
    t = f"tmux -L {JOB_SERVER}"
    script = (
        f"mkdir -p {JOB_LOG_DIR} && "
        f"{{ {t} has-session -t {JOB_SESSION} 2>/dev/null || "
        f"{t} new-session -d -s {JOB_SESSION} -n idle; }} && "
        f"{t} set-option -g -w remain-on-exit on >/dev/null && "
        f"if {t} list-windows -t {JOB_SESSION} -F '#{{window_name}}' | grep -qx {name}; then "
        f"echo 'ERROR: a job named {name} already exists — pick another name, or read it with job_output'; exit 1; fi && "
        f": > {log} && {t} new-window -d -t {JOB_SESSION} -n {name} bash -c {shlex.quote(inner)} && "
        f"echo 'started job {name}. Read it with job_output; watch live: tmux -L {JOB_SERVER} attach -t {JOB_SESSION}'")
    try:
        p = _exec(script, ro=ro)
    except Exception as e:
        return f"ERROR starting job: {type(e).__name__}: {e}"
    out = (p.stdout + p.stderr).strip()
    return out if out else f"(no output; exit code {p.returncode})"


def job_output(name: str, lines: int = 50, readonly: bool = False) -> str:
    name = (name or "").strip()
    if not _JOB_NAME.match(name):
        return "ERROR: bad job name."
    n = max(1, min(int(lines or 50), 400))
    log = f"{JOB_LOG_DIR}/{name}.log"
    ro = readonly or _MODE != "operator"
    script = (f"[ -f {log} ] || {{ echo 'ERROR: no job named {name}'; exit 1; }}; "
              f"if grep -q '^\\[loyd-job exit ' {log}; then "
              f"echo \"status: finished ($(grep '^\\[loyd-job exit ' {log} | tail -1 | tr -d '[]'))\"; "
              f"else echo 'status: running'; fi; echo; tail -n {n} {log}")
    try:
        p = _exec(script, ro=ro)
    except Exception as e:
        return f"ERROR reading job: {type(e).__name__}: {e}"
    return _cap((p.stdout + p.stderr).rstrip())


def tmux_read(target: str = "", lines: int = 60, readonly: bool = False) -> str:
    target = (target or "").strip()
    ro = readonly or _MODE != "operator"
    if not target:
        cmd = "tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}  #{window_name}  #{pane_current_command}' 2>&1"
    elif not _TMUX_TARGET.match(target):
        return "ERROR: target must look like session, session:window or session:window.pane."
    else:
        n = max(1, min(int(lines or 60), 400))
        cmd = f"tmux capture-pane -p -J -t {shlex.quote(target)} -S -{n} 2>&1 | sed -e :a -e '/^\\n*$/{{$d;N;ba' -e '}}'"
    try:
        p = _exec(cmd, ro=ro)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    return _cap((p.stdout or p.stderr or "(empty)").rstrip())


def audit_tail(lines: int = 20, actor: str = "", outcome: str = "") -> str:
    n = max(1, min(int(lines or 20), 200))
    recs = audit.tail(5000)
    if actor:
        recs = [r for r in recs if r.get("actor") == actor]
    if outcome:
        recs = [r for r in recs if r.get("outcome") == outcome]
    recs = recs[-n:]
    if not recs:
        return "No matching audit entries (or the audit log is off: LOYD_AUDIT_LOG)."
    out = []
    for r in recs:
        args = ", ".join(f"{k}={v!r}" for k, v in (r.get("args") or {}).items())
        if len(args) > 160:
            args = args[:160] + "…"
        ro = " [ro]" if r.get("readonly") else ""
        out.append(f"{r.get('ts')}  {r.get('actor')}{ro}  {r.get('tool')}({args})  -> {r.get('outcome')}")
    return _cap("\n".join(out))


def write_file(path: str, content: str) -> str:
    """Writes on the host Loyd ACTS on — over SSH (content via stdin) when
    LOYD_REMOTE is set, so it lands on the same box run_shell/read_file see."""
    content = content if content is not None else ""
    if _REMOTE:
        q = shlex.quote(path)
        cmd = (f"mkdir -p -- \"$(dirname -- {q})\" && "
               f"if [ -e {q} ]; then echo Overwrote; else echo Created; fi && cat > {q}")
        try:
            p = _exec(cmd, input=content)
        except Exception as e:
            return f"ERROR writing {path} on {_REMOTE}: {type(e).__name__}: {e}"
        if p.returncode != 0:
            return f"ERROR writing {path} on {_REMOTE}: {p.stderr.strip() or p.stdout.strip()}"
        verb = (p.stdout.strip().splitlines() or ["Wrote"])[0]
        return f"{verb} {path} on {_REMOTE} ({len(content)} chars)."
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
    "run_shell": lambda a, ro: run_shell(a.get("command", ""), readonly=ro),
    "read_file": lambda a, ro: read_file(a.get("path", ""), a.get("max_lines"), readonly=ro),
    "get_container_status": lambda a, ro: get_container_status(a.get("name", "")),
    "audit_tail": lambda a, ro: audit_tail(a.get("lines", 20), a.get("actor", ""), a.get("outcome", "")),
    "run_in_tmux": lambda a, ro: run_in_tmux(a.get("name", ""), a.get("command", ""), readonly=ro),
    "job_output": lambda a, ro: job_output(a.get("name", ""), a.get("lines", 50), readonly=ro),
    "tmux_read": lambda a, ro: tmux_read(a.get("target", ""), a.get("lines", 60), readonly=ro),
    "write_file": lambda a, ro: write_file(a.get("path", ""), a.get("content", "")),
    "restart_container": lambda a, ro: restart_container(a.get("name", "")),
    "prune_images": lambda a, ro: prune_images(a.get("scope", "dangling")),
}


def _run_kit_command(line: str, timeout: int) -> str:
    """Run a vetted kit command line (already validated + quoted by kits.render)
    on the host Loyd acts on. Kit commands are fixed, reviewed templates, so they
    run with the main login like get_container_status — not through run_shell's
    denylist, and not through the unprivileged read-only login."""
    try:
        p = _exec(line, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"ERROR: kit command timed out after {timeout}s."
    except Exception as e:
        return f"ERROR running kit command: {type(e).__name__}: {e}"
    out = p.stdout or ""
    if p.stderr:
        out += ("\n[stderr]\n" + p.stderr)
    if p.returncode != 0:
        out += f"\n[exit code {p.returncode}]"
    return _cap(out.rstrip() or f"(no output; exit code {p.returncode})")


import kb as _kb                                      # noqa: E402
TOOL_SCHEMAS.extend(_kb.TOOL_SCHEMAS)                 # read-only notes search, local to Loyd
_IMPL.update(_kb.IMPL)
import kits as _kits                                  # noqa: E402
import auditkit as _auditkit                          # noqa: E402
_SELECTED_KITS = _kits.selected()
KITS = _kits.install(TOOL_SCHEMAS, _IMPL, WRITE_TOOLS, _run_kit_command,
                     names=[k for k in _SELECTED_KITS if k != "audit"])


def _audit_exec(cmd: str, timeout: int):
    p = _exec(cmd, timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""


if "audit" in _SELECTED_KITS:                         # Python kit: scope logic can't be JSON
    _auditkit.install(TOOL_SCHEMAS, _IMPL, WRITE_TOOLS, _audit_exec, _cap)

import gpumode as _gpumode                            # noqa: E402
_gpumode.install(TOOL_SCHEMAS, _IMPL, OPERATOR_TOOLS, _exec, _cap)   # only with LOYD_GPU_MODE_CONFIG


def needs_confirm(name: str, args: dict) -> bool:
    """Tool calls that always need the operator's y/N: write tools, and a gpu_mode apply."""
    return name in WRITE_TOOLS or _gpumode.needs_confirm(name, args)


def execute_tool(name: str, args: dict, actor: str = "local", readonly: bool = False) -> str:
    """Dispatch a tool call and audit it. `actor` is who asked (e.g. "mcp:alice",
    "cli:root"); readonly=True denies write tools and forces read-only shell rules
    for this call regardless of LOYD_MODE."""
    t0 = time.time()
    fn = _IMPL.get(name)
    if not fn:
        result = f"ERROR: unknown tool '{name}'. Available: {', '.join(_IMPL)}"
    elif not isinstance(args, dict):
        result = f"ERROR: tool arguments must be an object, got {type(args).__name__}"
    elif readonly and name in WRITE_TOOLS:
        result = f"DENIED: '{name}' is an operator-only write tool; {actor} is read-only."
    elif readonly and name in OPERATOR_TOOLS:
        result = f"DENIED: '{name}' is operator-only; {actor} is read-only."
    else:
        try:
            result = fn(args, bool(readonly))
        except Exception as e:
            result = f"ERROR in tool {name}: {type(e).__name__}: {e}"
    audit.record(actor, name, args if isinstance(args, dict) else {"_raw": repr(args)},
                 audit.outcome_of(result), len(result), time.time() - t0, readonly)
    return result
