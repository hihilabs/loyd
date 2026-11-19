"""gpu_mode tool — operator-only wrapper over scripts/gpu_mode.sh (BACKLOG: GPU modes 2/3).

    gpu_mode(mode="status")                  what is loaded, recorded mode, may we go offline
    gpu_mode(mode="online"|"offline")        dry run only: what would change
    gpu_mode(mode=..., apply=true)           confirm-gated (needs_confirm): runs the dry run
                                             first and applies only if that succeeded

Installed only when LOYD_GPU_MODE_CONFIG is set: the path of the script's config file
on the host Loyd acts on (deploy/gpu-mode.env.example). The script text is sent from
this checkout, so it runs on LOYD_REMOTE over SSH the same way run_shell does.

The local brain reads the recorded mode too: in `online` it sends the recorded short
keep_alive with every ollama call (keep_alive()), so its model never camps on VRAM
that vision owns. LOYD_GPU_MODE_STATE is that state file's path on the box the CLI runs on.
"""
import os
import shlex
import subprocess
from pathlib import Path

NAME = "gpu_mode"
SCRIPT = Path(__file__).resolve().parent / "scripts" / "gpu_mode.sh"
MODES = ("status", "online", "offline")

SCHEMA = {"type": "function", "function": {
    "name": NAME,
    "description": ("Operators only. Share the GPU between the vision pipeline and Loyd's big local model. "
                    "mode='status' shows what is loaded and whether offline is allowed. 'online' gives vision "
                    "both GPUs (unloads Loyd's big model); 'offline' loads Loyd's big model on the shared GPU, "
                    "only while vision is idle or at night (vision is never paused). Without apply it is a dry "
                    "run; apply=true makes the change and REQUIRES operator confirmation. Dry-run first."),
    "parameters": {"type": "object",
        "properties": {"mode": {"type": "string", "enum": list(MODES)},
                       "apply": {"type": "boolean", "description": "make the change (default false = dry run)"}},
        "required": ["mode"]}}}


def config_path():
    return os.getenv("LOYD_GPU_MODE_CONFIG", "").strip()


def _truthy(v):
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"))


def needs_confirm(name, args):
    """Only a real change asks for y/N; status and dry runs don't."""
    return name == NAME and isinstance(args, dict) and _truthy(args.get("apply"))


def _run(exec_fn, cap, mode, apply):
    cmd = (f"GPU_MODE_CONFIG={shlex.quote(config_path())} "
           f"bash -c {shlex.quote(SCRIPT.read_text())} gpu_mode {mode}" + (" --apply" if apply else ""))
    try:
        p = exec_fn(cmd, timeout=330)                 # a cold 32B load can take minutes
    except subprocess.TimeoutExpired:
        return 4, "ERROR: gpu_mode timed out."
    out = ((p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")).strip()
    return p.returncode, cap(out or f"(no output; exit code {p.returncode})")


def gpu_mode(exec_fn, cap, mode, apply=False):
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        return f"ERROR: mode must be one of {', '.join(MODES)}, got {mode!r}."
    if not config_path():
        return "ERROR: gpu_mode is not configured (LOYD_GPU_MODE_CONFIG)."
    apply = _truthy(apply) and mode != "status"
    rc, out = _run(exec_fn, cap, mode, False)
    if rc == 3:
        return "REFUSED: " + out
    if rc != 0:
        return f"ERROR (exit {rc}): {out}"
    if not apply:
        return out
    rc, done = _run(exec_fn, cap, mode, True)         # the dry run passed; now do it
    if rc == 3:
        return "REFUSED: " + done                     # vision got busy in between
    if rc != 0:
        return f"ERROR (exit {rc}): {done}"
    return done


def install(schemas, impl, operator_tools, exec_fn, cap):
    if not config_path():
        return False
    schemas.append(SCHEMA)
    impl[NAME] = lambda a, ro: gpu_mode(exec_fn, cap, a.get("mode", ""), a.get("apply", False))
    operator_tools.add(NAME)
    return True


def read_state(path=None):
    path = path or os.getenv("LOYD_GPU_MODE_STATE", "").strip()
    if not path:
        return {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return {}
    return dict(ln.split("=", 1) for ln in lines if "=" in ln)


def keep_alive(path=None):
    """keep_alive to send with local ollama calls: the recorded one in `online`, else None."""
    st = read_state(path)
    return (st.get("keep_alive") or None) if st.get("mode") == "online" else None
