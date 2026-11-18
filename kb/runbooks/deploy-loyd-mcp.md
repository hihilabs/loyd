<!-- source: claude -->
# Runbook: deploy the Loyd MCP server on panel safely

Used twice on 2026-11-18 (e7c9f73, then 08f19ec). The restart cuts the connection to Loyd for a few seconds, so the switch is scheduled, with automatic rollback.

## 1. Test a separate copy
`git -C /opt/loyd fetch origin <branch>` → `git worktree add --detach /opt/loyd-next FETCH_HEAD` → run `pytest tests/` with `/opt/loyd/.venv/bin/python`. Every test must pass.

## 2. Smoke test on a spare port
Load `/etc/loyd/tokyo7.env`, set `LOYD_MCP_PORT=18848`, run `/opt/loyd-next/loyd_mcp.py` in the background. Check `/healthz`, the OAuth metadata issuer, `tools/list`, one real tool call against Tokyo7, and that a read-only OAuth grant is refused writes. Then kill it.

## 3. Switch, with automatic rollback
- Back up the env: `cp -p /etc/loyd/tokyo7.env /etc/loyd/tokyo7.env.bak-<date>`.
- Write a check script that tests `systemctl is-active`, `/healthz` = 200, the OAuth metadata = 200, and unauthenticated POST to `/tokyo7/` = 401. If any check fails, it checks out the previous commit, restores the env backup and restarts.
- `git -C /opt/loyd checkout --detach <new commit>`.
- `systemd-run --on-active=3 systemctl restart loyd-mcp-tokyo7` and `systemd-run --on-active=45 <check script>`.
- Result: `/root/loyd-deploy-20261118.log`.

## Rollback by hand
`git -C /opt/loyd checkout --detach <previous commit>`, restore the env backup, `systemctl restart loyd-mcp-tokyo7`.

## After a PR merges
`git -C /opt/loyd checkout main && git -C /opt/loyd pull` (same code, no restart needed).
