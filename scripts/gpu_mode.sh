#!/usr/bin/env bash
# GPU modes for the box that shares its GPUs between the vision pipeline and Loyd.
#   online   vision owns both GPUs. Loyd's big model is unloaded from the shared
#            GPU's ollama; Loyd's small model loads on demand with a short keep-alive.
#   offline  Loyd's big model is loaded on the shared GPU — only while vision is idle
#            on that GPU or inside the night window. Vision is never paused or stopped.
#   status   what is loaded now, the recorded mode, and whether `offline` is allowed.
#
# Usage:  scripts/gpu_mode.sh online|offline|status [--apply]
# Dry-run by default (prints what it would do). --apply makes the change.
# Config: GPU_MODE_CONFIG (default /etc/loyd/gpu-mode.env), KEY=VALUE lines, read
# without being executed. Example: deploy/gpu-mode.env.example. Required keys:
#   VISION_CONTAINER  container of the vision worker on the shared GPU
#   VISION_GPU        nvidia-smi index of the shared GPU
#   OLLAMA_URL        ollama that serves the shared GPU, e.g. http://localhost:11435
#   OFFLINE_MODEL     Loyd's big model for offline mode
#   STATE_FILE        where the current mode is recorded (read by Loyd)
# Optional: KEEP_ALIVE_OFFLINE (30m), KEEP_ALIVE_ONLINE (2m), IDLE_UTIL_MAX (10, % GPU
#   utilisation at or below which vision counts as idle), NIGHT_HOURS (e.g. 23-6; unset = none).
# Exit: 0 done, 2 usage/config error, 3 refused (vision busy outside the night window),
#   4 a step failed.
set -euo pipefail

MODE=${1:-}
APPLY=${2:-}
case "$MODE" in online|offline|status) ;; *) echo "usage: gpu_mode.sh online|offline|status [--apply]" >&2; exit 2;; esac
case "$APPLY" in ""|--apply) ;; *) echo "unknown option: $APPLY" >&2; exit 2;; esac

CONFIG=${GPU_MODE_CONFIG:-/etc/loyd/gpu-mode.env}
[ -r "$CONFIG" ] || { echo "config not readable: $CONFIG (see deploy/gpu-mode.env.example)" >&2; exit 2; }
cfg() { grep -E "^$1=" "$CONFIG" | tail -1 | cut -d= -f2- | sed -E 's/^["'\'']//; s/["'\'']$//' || true; }
need() { local v; v=$(cfg "$1"); [ -n "$v" ] || { echo "config $CONFIG: $1 is required" >&2; exit 2; }; printf '%s' "$v"; }

VISION_CONTAINER=$(need VISION_CONTAINER)
VISION_GPU=$(need VISION_GPU)
OLLAMA_URL=$(need OLLAMA_URL); OLLAMA_URL=${OLLAMA_URL%/}
OFFLINE_MODEL=$(need OFFLINE_MODEL)
STATE_FILE=$(need STATE_FILE)
KEEP_ALIVE_OFFLINE=$(cfg KEEP_ALIVE_OFFLINE); KEEP_ALIVE_OFFLINE=${KEEP_ALIVE_OFFLINE:-30m}
KEEP_ALIVE_ONLINE=$(cfg KEEP_ALIVE_ONLINE); KEEP_ALIVE_ONLINE=${KEEP_ALIVE_ONLINE:-2m}
IDLE_UTIL_MAX=$(cfg IDLE_UTIL_MAX); IDLE_UTIL_MAX=${IDLE_UTIL_MAX:-10}
NIGHT_HOURS=$(cfg NIGHT_HOURS)
for n in "$VISION_GPU" "$IDLE_UTIL_MAX"; do
  [[ "$n" =~ ^[0-9]+$ ]] || { echo "config $CONFIG: VISION_GPU and IDLE_UTIL_MAX must be numbers" >&2; exit 2; }
done
for n in "$OFFLINE_MODEL" "$KEEP_ALIVE_OFFLINE" "$KEEP_ALIVE_ONLINE"; do
  [[ "$n" =~ ^[A-Za-z0-9._:/-]+$ ]] || { echo "config $CONFIG: bad value '$n'" >&2; exit 2; }
done

say() { printf '▸ %s\n' "$*"; }
step() {  # step <description> <command...>: print in dry-run, run with --apply
  local what=$1; shift
  if [ "$APPLY" = "--apply" ]; then say "$what"; "$@" || { echo "failed: $what" >&2; exit 4; }
  else printf '  DRY: %s\n' "$what"; fi
}

# Vision on the shared GPU counts as idle when its container isn't running, or the
# GPU's utilisation is at or below IDLE_UTIL_MAX. Unknown (no nvidia-smi) = busy.
vision_state() {
  local running util
  running=$(docker inspect -f '{{.State.Running}}' "$VISION_CONTAINER" 2>/dev/null || echo missing)
  if [ "$running" != "true" ]; then echo "idle (container $VISION_CONTAINER: $running)"; return; fi
  util=$(nvidia-smi --id="$VISION_GPU" --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | tr -dc '0-9' || true)
  if [ -z "$util" ]; then echo "busy (GPU$VISION_GPU utilisation unknown)"; return; fi
  if [ "$util" -le "$IDLE_UTIL_MAX" ]; then echo "idle (GPU$VISION_GPU at ${util}%)"; else echo "busy (GPU$VISION_GPU at ${util}%)"; fi
}

in_night() {  # NIGHT_HOURS=start-end in local hours; wraps midnight (23-6)
  [ -n "$NIGHT_HOURS" ] || return 1
  [[ "$NIGHT_HOURS" =~ ^([0-9]{1,2})-([0-9]{1,2})$ ]] || { echo "config $CONFIG: NIGHT_HOURS must be like 23-6" >&2; exit 2; }
  local s=$((10#${BASH_REMATCH[1]})) e=$((10#${BASH_REMATCH[2]})) h
  h=$((10#${GPU_MODE_HOUR:-$(date +%H)}))
  if [ "$s" -le "$e" ]; then [ "$h" -ge "$s" ] && [ "$h" -lt "$e" ]; else [ "$h" -ge "$s" ] || [ "$h" -lt "$e" ]; fi
}

ollama_load() {  # ollama_load <model> <keep_alive>: keep_alive 0 unloads
  curl -sf -m 300 -X POST "$OLLAMA_URL/api/generate" \
    -d "{\"model\":\"$1\",\"keep_alive\":$( [ "$2" = 0 ] && echo 0 || echo "\"$2\"" )}" >/dev/null
}

write_state() {
  mkdir -p "$(dirname "$STATE_FILE")"
  printf 'mode=%s\nkeep_alive=%s\nmodel=%s\nsince=%s\n' "$1" "$2" "$3" "$(date -Is)" > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}

VISION=$(vision_state)
case "$MODE" in
status)
  echo "recorded mode: $(grep -E '^mode=' "$STATE_FILE" 2>/dev/null | cut -d= -f2 || true)"
  [ -s "$STATE_FILE" ] || echo "  (no state file at $STATE_FILE)"
  echo "vision:        $VISION"
  if in_night; then echo "night window:  yes ($NIGHT_HOURS)"; else echo "night window:  no${NIGHT_HOURS:+ ($NIGHT_HOURS)}"; fi
  echo "loaded on $OLLAMA_URL:"
  if ps=$(curl -sf -m 10 "$OLLAMA_URL/api/ps"); then
    models=$(printf '%s' "$ps" | grep -oE '"name":"[^"]*"' | cut -d'"' -f4 || true)
    if [ -n "$models" ]; then printf '%s\n' "$models" | sed 's/^/  /'; else echo "  (nothing loaded)"; fi
  else echo "  (ollama not reachable)"; fi
  ;;
online)
  say "online: vision owns GPU$VISION_GPU; unload $OFFLINE_MODEL, small model on demand (keep-alive $KEEP_ALIVE_ONLINE)"
  step "unload $OFFLINE_MODEL from $OLLAMA_URL" ollama_load "$OFFLINE_MODEL" 0
  step "record mode=online in $STATE_FILE" write_state online "$KEEP_ALIVE_ONLINE" ""
  ;;
offline)
  if [[ "$VISION" == idle* ]]; then why="vision $VISION"
  elif in_night; then why="night window $NIGHT_HOURS (vision $VISION; it keeps running)"
  else echo "refused: vision is $VISION and it is outside the night window${NIGHT_HOURS:+ ($NIGHT_HOURS)}. Vision is never paused for this; try again when it is idle." >&2; exit 3
  fi
  say "offline: $why; load $OFFLINE_MODEL on GPU$VISION_GPU (keep-alive $KEEP_ALIVE_OFFLINE)"
  step "load $OFFLINE_MODEL on $OLLAMA_URL" ollama_load "$OFFLINE_MODEL" "$KEEP_ALIVE_OFFLINE"
  step "record mode=offline in $STATE_FILE" write_state offline "$KEEP_ALIVE_OFFLINE" "$OFFLINE_MODEL"
  ;;
esac
[ "$MODE" = status ] || [ "$APPLY" = "--apply" ] || echo "dry run — nothing changed. Add --apply to do it."
