#!/usr/bin/env bash
# Safe deploy of the Loyd MCP server — the procedure run by hand twice on
# 2026-11-18, as a script (kb/runbooks/deploy-loyd-mcp.md):
#   1. a separate copy of the new code (git worktree), 2. the test suite with the
#   production venv, 3. a smoke test on a spare port with the real config,
#   4. the switch: restart scheduled a few seconds out + a health check that rolls
#   back to the previous commit and config if anything is off.
#
# Usage, on the MCP host as root:   deploy/deploy.sh <branch-or-commit> [--dry-run]
# Env overrides: LOYD_DIR (/opt/loyd) ENV_FILE (/etc/loyd/tokyo7.env)
#   SERVICE (loyd-mcp-tokyo7) SPARE_PORT (18848) LOG (/root/loyd-deploy.log)
set -euo pipefail
REF=${1:?usage: deploy.sh <branch-or-commit> [--dry-run]}
DRY=${2:-}
LOYD_DIR=${LOYD_DIR:-/opt/loyd}
ENV_FILE=${ENV_FILE:-/etc/loyd/tokyo7.env}
SERVICE=${SERVICE:-loyd-mcp-tokyo7}
SPARE_PORT=${SPARE_PORT:-18848}
PY=${PY:-$LOYD_DIR/.venv/bin/python}
NEXT=${NEXT:-$LOYD_DIR-next}
LOG=${LOG:-/root/loyd-deploy.log}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

say() { printf '▸ %s\n' "$*"; }
run() { if [ "$DRY" = "--dry-run" ]; then printf '  DRY: %s\n' "$*"; else bash -c "$*"; fi; }
envval() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }

PORT=$(envval LOYD_MCP_PORT); PORT=${PORT:-8848}
MPATH=$(envval LOYD_MCP_PATH); MPATH=${MPATH:-/mcp}
HOST=$(envval LOYD_MCP_PUBLIC_URL | sed -E 's#^https?://##; s#/.*##'); HOST=${HOST:-localhost}
PREV=$(git -C "$LOYD_DIR" rev-parse --short HEAD)

say "1/4 fetch $REF into a separate copy at $NEXT (live: $PREV)"
run "git -C '$LOYD_DIR' fetch -q origin '$REF'"
if [ "$DRY" = "--dry-run" ]; then NEW=$(git -C "$LOYD_DIR" rev-parse --short "$REF" 2>/dev/null || echo "<$REF>")
else NEW=$(git -C "$LOYD_DIR" rev-parse --short FETCH_HEAD); fi
run "rm -rf '$NEXT'; git -C '$LOYD_DIR' worktree prune; git -C '$LOYD_DIR' worktree add -q --detach '$NEXT' $NEW"

say "2/4 test suite with the production venv"
run "cd '$NEXT' && LOYD_AUDIT_LOG=off '$PY' -m pytest -q -p no:cacheprovider ${PYTEST_ARGS:-tests/}"

say "3/4 smoke test on 127.0.0.1:$SPARE_PORT with the real config"
SMOKE=$(cat <<SMK
set -a; . '$ENV_FILE'; set +a
export LOYD_MCP_PORT=$SPARE_PORT LOYD_AUDIT_LOG=/tmp/loyd-smoke-audit.jsonl
'$PY' '$NEXT/loyd_mcp.py' >/tmp/loyd-smoke.log 2>&1 & pid=\$!
trap 'kill \$pid 2>/dev/null; rm -f /tmp/loyd-smoke-audit.jsonl' EXIT
for i in \$(seq 50); do curl -s -o /dev/null http://127.0.0.1:$SPARE_PORT/healthz && break; sleep 0.2; done
code() { curl -s -o /dev/null -w '%{http_code}' -H 'Host: $HOST' "\$@"; }
[ "\$(code http://127.0.0.1:$SPARE_PORT/healthz)" = 200 ] || { echo 'smoke: /healthz failed'; exit 1; }
[ "\$(code http://127.0.0.1:$SPARE_PORT/.well-known/oauth-authorization-server)" = 200 ] || { echo 'smoke: OAuth metadata failed'; exit 1; }
[ "\$(code -X POST http://127.0.0.1:$SPARE_PORT$MPATH/)" = 401 ] || { echo 'smoke: unauthenticated request was not refused'; exit 1; }
tok=\$(printf '%s' "\$LOYD_MCP_TOKENS" | tr , '\n' | awk -F: '\$2=="o"{print \$3; exit}')
curl -s -H 'Host: $HOST' -H "Authorization: Bearer \$tok" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  http://127.0.0.1:$SPARE_PORT$MPATH/ | grep -q '"run_shell"' || { echo 'smoke: tools/list failed'; exit 1; }
echo 'smoke: healthz, OAuth metadata, 401 on $MPATH, tools/list all OK'
SMK
)
run "$SMOKE"

say "4/4 switch $PREV -> $NEW; restart in 3 s, health check + auto-rollback at 45 s"
CHECK=${CHECK_DIR:-/root}/loyd-deploy-check-$STAMP.sh
run "cp -p '$ENV_FILE' '$ENV_FILE.bak-$STAMP'"
CHECK_BODY=$(cat <<CHK
#!/bin/bash
# written by deploy.sh $STAMP: health-check $NEW, roll back to $PREV if unhealthy
ok=1
systemctl is-active -q $SERVICE || ok=0
code() { curl -s -o /dev/null -w '%{http_code}' -H 'Host: $HOST' "\$@"; }
[ "\$(code http://127.0.0.1:$PORT/healthz)" = 200 ] || ok=0
[ "\$(code http://127.0.0.1:$PORT/.well-known/oauth-authorization-server)" = 200 ] || ok=0
[ "\$(code -X POST http://127.0.0.1:$PORT$MPATH/)" = 401 ] || ok=0
if [ \$ok = 1 ]; then echo "\$(date -Is) HEALTHY at $NEW (from $PREV)" >> '$LOG'; exit 0; fi
echo "\$(date -Is) UNHEALTHY at $NEW, rolling back to $PREV" >> '$LOG'
git -C '$LOYD_DIR' checkout -q --detach $PREV
cp -p '$ENV_FILE.bak-$STAMP' '$ENV_FILE'
systemctl restart $SERVICE
journalctl -u $SERVICE -n 30 --no-pager >> '$LOG'
CHK
)
if [ "$DRY" = "--dry-run" ]; then printf '  DRY: write %s:\n%s\n' "$CHECK" "$CHECK_BODY" | sed 's/^/    /'
else printf '%s\n' "$CHECK_BODY" > "$CHECK"; chmod 700 "$CHECK"; fi
run "git -C '$LOYD_DIR' checkout -q --detach $NEW"
run "git -C '$LOYD_DIR' worktree remove --force '$NEXT'; git -C '$LOYD_DIR' worktree prune"
run "systemd-run -q --unit=loyd-restart-$STAMP --on-active=3 /bin/systemctl restart $SERVICE"
run "systemd-run -q --unit=loyd-check-$STAMP --on-active=45 '$CHECK'"
say "scheduled. Result in about a minute: tail -2 $LOG"
