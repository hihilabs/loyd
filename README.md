# Loyd

A self-hosted dev/ops assistant for the hihilabs stack (Tokyo7 + the VPS). Loyd is
**strong tools + tight scaffolding** — the model just routes:

| Piece | What it is |
|---|---|
| `tools.py` | The tools: `run_shell`, `read_file`, `get_container_status`, and the operator writes `write_file` / `restart_container` / `prune_images`. Act locally or on `LOYD_REMOTE` over SSH. Read-only rules, a catastrophic-command backstop, output trimming, audit. |
| `cli.py` (`loyd`) | Terminal assistant. Cloud Claude by default, the local Ollama model (qwen2.5-coder:7b) as fallback (`LOYD_BRAIN_POLICY=claude-first\|local-first\|auto`, see `brain.py`); y/N confirm on every write. |
| `loyd_mcp.py` | The same tools over remote MCP, with OAuth, for claude.ai / the Claude apps / Desktop / VS Code. |
| `audit.py` | One JSON line per tool call: who, what, outcome. |
| `kb.py` + `kb/` | Notes and runbooks Loyd searches (`search_kb`, `read_kb`) — how the stack is wired, past fixes, procedures. Works offline. Add a note: drop a `.md` in `kb/` with `<!-- source: human -->` on line 1. |
| `loydtrace.py` | Opt-in traces (`LOYD_TRACE_DIR`) of each answer for evaluation; rate with `/good` `/bad`. `scripts/export_training.py` exports only local-model answers a person rated good — never Claude output. |
| `auditkit.py` | Scope-gated network audit tools for authorized client audits (`audits/README.md`). |
| `kits.py` + `kits/*.json` | Declarative tool kits: vetted commands with validated params, per project (`LOYD_KITS=ops,hihilabs`). Write commands are confirm-gated; unverified commands stay hidden. |

```bash
./install.sh                     # CLI: puts `loyd` on your PATH
loyd "is plex up and how full is /mnt/user?"
loyd                             # interactive; /help, /audit, /local, /update
```

- **MCP server, connectors, OAuth, security model:** [MCP.md](MCP.md)
- **Design decisions + model testing:** [FINDINGS.md](FINDINGS.md)
- **What's next:** [BACKLOG.md](BACKLOG.md)
- **Tests:** `pip install -r requirements-mcp.txt pytest httpx && pytest -q tests/` (CI runs them on every push)

Key env: `LOYD_KITS`, `LOYD_MODE` (`readonly` | `operator`), `LOYD_REMOTE` / `LOYD_REMOTE_PORT`,
`LOYD_REMOTE_READONLY`, `LOYD_AUDIT_LOG`, `ANTHROPIC_API_KEY`, `LOYD_CLAUDE_MODEL`
(default `claude-opus-5-5`), `LOYD_CLAUDE_EFFORT` (`high`), `LOYD_OFFLINE` (`1` force local, `0` skip the probe), `OLLAMA_URL`, `OLLAMA_AI_MODEL`.
