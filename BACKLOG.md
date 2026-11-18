# Loyd backlog

Worked by the **"Loyd backlog"** scheduled task (every 5 hours) and by humans.
Rules for the agent: take the **first unchecked item in "Agent-ready"**, do only
that item, add tests, keep CI green, open a PR that also ticks the box here.
Never deploy, merge, rotate tokens, or touch hosts — those are in "Needs a human".
If an item turns out bigger than one PR, split it here and do the first piece.

## Agent-ready (top = next)

- [ ] **Codify the safe deploy** as `deploy/deploy.sh` + `deploy/loyd-mcp.service`: git worktree
      of the target ref → run `pytest` with the prod venv → start it on a spare port with the
      prod env file and smoke-test `/healthz`, OAuth metadata, 401 on the MCP path → switch
      `/opt/loyd` → schedule restart + health check with auto-rollback (what was done by hand
      on 2026-11-18, see `/root/loyd-deploy-check.sh` on panel). Add systemd hardening
      (`NoNewPrivileges`, `ProtectSystem=strict` + `ReadWritePaths` for the audit log,
      `PrivateTmp`). Document in MCP.md. Dry-run mode that prints steps.
- [ ] **Small-model double tool call** (FINDINGS rough edge): `loyd_core.LoydAgent` sometimes
      repeats a near-identical call before concluding. After a repeat is detected, drop the
      tools for the next step (force a text answer). Tests with a fake ollama server.
- [ ] **Tests for `loyd_core`**: `_first_json_obj`, `_extract_call` (native vs text-JSON),
      the conclude-guard on the last step, confirm/decline path — using a stub `/api/chat`.
- [ ] **Tests for `cli.py`**: `should_confirm`, `handle_slash` (`/tools`, `/model`, `/audit`),
      `self_update` refusing on a detached HEAD.
- [ ] **`tail_logs` read tool**: `docker logs --tail N [--since]` for a container with an optional
      grep, so the model stops hand-building `docker logs | grep` pipelines. Read-only, works
      over `LOYD_REMOTE`, input-validated (container name, N ≤ 2000).
- [ ] **`audit_tail` MCP tool (operator-only)**: last N audit records, filterable by actor /
      outcome, so Claude can answer "what did Loyd do today?".
- [ ] **Per-identity rate limit on MCP tool calls** (e.g. 60/min per user), audited as
      `outcome: "throttled"`, so a runaway client loop can't hammer Tokyo7.

## Needs a human

- [ ] Merge PR #1, then on panel: `git -C /opt/loyd checkout main && git -C /opt/loyd pull`.
- [ ] Rotate the `admin1` token (it was shown in a chat on 2026-11-18). Reconnect clients after.
- [ ] Create the `loyd-ro` login on Tokyo7 and set `LOYD_REMOTE_READONLY` (MCP.md).
- [ ] Per-person tokens for Geoff and IamMe (read-only to start).
- [ ] Update the Tokyo7 CLI checkout (`/mnt/user/appdata/loyd`) after merge.

## Done

- [x] OAuth completed (signed short-lived tokens, refresh, redirect validation) — PR #1
- [x] Read-only callers get a read-only shell; `LOYD_REMOTE_READONLY` — PR #1
- [x] Audit log (+ rotation), remote `write_file`, head+tail trimming, CI — PR #1
- [x] Claude backend → `claude-opus-5-5`, prompt caching, effort, refusal fallbacks — PR #1
- [x] Deployed to panel 2026-11-18 (e7c9f73), health-checked with auto-rollback armed
