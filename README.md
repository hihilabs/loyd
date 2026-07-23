# Loyd — local dev/ops assistant (v0.1)

A small, model-agnostic tool-using agent for the Tokyo7 Unraid box (and, later, the
hihilabs VPS). Runs on a local ollama model — `qwen2.5-coder:7b` by default — so it's
private, free, and coexists with the cam workers.

**Model note (from live testing):** qwen2.5-coder:7b reliably *chains tools and concludes*.
Cogito 3B aced isolated single-tool calls but tends to *narrate* the plan instead of
acting on multi-step/compound tasks — weaker orchestrator. Cogito 8B (native tool_calls,
still fits VRAM) is the next one to A/B. Swap with `--model` / `OLLAMA_AI_MODEL`.

## Install (one-liner)
```bash
# On a machine with repo access (SSH key), or if the repo is public:
curl -fsSL https://raw.githubusercontent.com/hihilabs/loyd/main/install.sh | bash
# From a local checkout (e.g. on Tokyo7):
./install.sh
```
Puts a `loyd` command on your PATH that talks to the Tokyo7 ollama. Default `OLLAMA_URL`
is Tokyo7 over tailscale (`localhost:11434`) so it works from any of your machines;
override at install: `OLLAMA_URL=http://localhost:11434 LOYD_MODEL=cogito:8b ./install.sh`.
(Private repo → `curl | bash` needs it public or a token; the installer clones via SSH by
default, which works wherever your GitHub key does.)

## Run
```bash
loyd "is the plex container running and how much disk is free?"
loyd                              # interactive REPL
# pre-install, directly:
python3 /mnt/user/appdata/loyd/cli.py "tail the docker log for camagent-worker-gpu0"
```

## Swap the brain (one flag / env var)
```bash
loyd --model cogito:8b "..."        # more reasoning, still fits VRAM
OLLAMA_AI_MODEL=cogito:8b loyd ...
```
The harness is the same; only the backend model changes. Escalation to a bigger
local model or cloud Claude for hard tasks is the intended growth path.

## Design
- `tools.py` — read-only tool schemas + implementations (`run_shell`, `read_file`,
  `get_container_status`). `run_shell` refuses obviously-mutating commands (best-effort
  guard, not a sandbox — v0.1 is read-only on a trusted box).
- `loyd_core.py` — `LoydAgent`: the tool-loop. Normalizes native tool_calls (Cogito)
  and text-JSON (qwen-coder), with validate-and-retry for small-model whiffs. No deps
  (stdlib only). Importable by the worker's `ai_task` handler later ("both eventually").
- `cli.py` — one-shot + interactive front-end with transparent tool-call display.

## Status / roadmap
- v0.1: read-only tools (`run_shell`, `read_file`, `get_container_status`), CLI. ✓
- v0.2: confirm-gated write tools (`write_file`, `restart_container`) — each prompts the
  operator y/N before running; declining feeds back cleanly. **← you are here**
- next: VPS reach (ssh/deploy/log-read into the hihilabs box); wire the core into
  worker.py `ai_task` so hihilabs.xyz jobs can drive Loyd.
