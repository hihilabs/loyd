#!/bin/bash
# Re-assert the Tailscale Funnel registration for the Loyd MCP port after
# tailscaled (re)starts. Local `tailscale serve` config auto-restores from
# on-disk state on restart, but the Funnel *relay*-side registration doesn't
# always come back with it — `tailscale funnel status` keeps reporting "on"
# from local state even when the public relay has no live mapping, which
# shows up externally as a TLS RST immediately after ClientHello. Fire-and-
# forget, backgrounds and exits immediately — must never delay tailscaled
# startup (Type=notify already gates readiness on the daemon itself, not on
# this).
FUNNEL_PORT=10000
FUNNEL_TARGET=http://127.0.0.1:8848
LOG_FILE=/opt/loyd/logs/reassert_funnel.log
mkdir -p "$(dirname "$LOG_FILE")"

{
  # tailscaled reports ready to systemd before it's actually connected to the
  # control plane — poll `tailscale status` until it stops saying so, capped
  # so a genuinely broken tailscaled can't hang this forever.
  for i in $(seq 1 30); do
    if tailscale status >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done

  OUT=$(tailscale funnel --bg --https="$FUNNEL_PORT" --yes "$FUNNEL_TARGET" 2>&1)
  echo "$(date -Is) reassert_funnel: $OUT" | tr '\n' ' '
  echo
} >> "$LOG_FILE" 2>&1 &
disown
exit 0
