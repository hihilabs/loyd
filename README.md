# Loyd

A self-hosted dev/ops assistant for the hihilabs stack (Tokyo7 + the VPS). Loyd is
**strong tools + tight scaffolding** — the model just routes:

| Piece | What it is |
|---|---|
| `tools.py` | The tools: `run_shell`, `read_file`, `get_container_status`, and the operator writes `write_file` / `restart_container` / `prune_images`. Act locally or on `LOYD_REMOTE` over SSH. Read-only rules, a catastrophic-command backstop, output trimming, audit. |
| `cli.py` (`loyd`) | Terminal assistant. Cloud Claude by default, the local Ollama model (qwen2.5-coder:7b) as fallback; y/N confirm on every write. |
| `loyd_mcp.py` | The same tools over remote MCP, with OAuth, for claude.ai / the Claude apps / Desktop / VS Code. |
| `audit.py` | One JSON line per tool call: who, what, outcome. |

```bash
./install.sh                     # CLI: puts `loyd` on your PATH
loyd "is plex up and how full is /mnt/user?"
loyd                             # interactive; /help, /audit, /local, /update
```

- **MCP server, connectors, OAuth, security model:** [MCP.md](MCP.md)
- **Design decisions + model testing:** [FINDINGS.md](FINDINGS.md)
- **What's next:** [BACKLOG.md](BACKLOG.md)
- **Tests:** `pip install -r requirements-mcp.txt pytest httpx && pytest -q tests/` (CI runs them on every push)

Key env: `LOYD_MODE` (`readonly` | `operator`), `LOYD_REMOTE` / `LOYD_REMOTE_PORT`,
`LOYD_REMOTE_READONLY`, `LOYD_AUDIT_LOG`, `ANTHROPIC_API_KEY`, `LOYD_CLAUDE_MODEL`
(default `claude-opus-5-5`), `LOYD_CLAUDE_EFFORT` (`high`), `OLLAMA_URL`, `OLLAMA_AI_MODEL`.
