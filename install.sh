#!/usr/bin/env bash
# Loyd installer — puts a `loyd` command on your PATH that talks to the Tokyo7 ollama.
#
#   curl -fsSL https://raw.githubusercontent.com/hihilabs/loyd/main/install.sh | bash
#   # or, from a local checkout:  ./install.sh
#
# Env overrides:
#   OLLAMA_URL  (default http://localhost:11434 — Tokyo7 over tailscale; use
#                http://localhost:11434 on the LAN)
#   LOYD_MODEL  (default qwen2.5-coder:7b)
#   LOYD_HOME   (default ~/.loyd)      BIN_DIR (default ~/.local/bin)
#   LOYD_REPO   (default git@github.com:hihilabs/loyd.git)
set -euo pipefail

LOYD_HOME="${LOYD_HOME:-$HOME/.loyd}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
LOYD_MODEL="${LOYD_MODEL:-qwen2.5-coder:7b}"
LOYD_REPO="${LOYD_REPO:-git@github.com:hihilabs/loyd.git}"

say()  { printf '\033[36m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 is required (Loyd uses only the stdlib)."

# 1. Get the source into LOYD_HOME. Prefer a local checkout if we're running inside one.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/cli.py" ] && [ -f "$SELF_DIR/loyd_core.py" ]; then
  say "Installing from local checkout: $SELF_DIR"
  mkdir -p "$LOYD_HOME"
  cp -f "$SELF_DIR"/*.py "$LOYD_HOME"/
elif [ -d "$LOYD_HOME/.git" ]; then
  say "Updating existing install at $LOYD_HOME"
  git -C "$LOYD_HOME" pull --ff-only || warn "git pull failed; keeping current files"
else
  command -v git >/dev/null || die "git is required to clone $LOYD_REPO"
  say "Cloning $LOYD_REPO -> $LOYD_HOME"
  git clone --depth 1 "$LOYD_REPO" "$LOYD_HOME" \
    || die "clone failed — is this machine authorized for the repo? (SSH key / repo access)"
fi

# 2. Write the launcher with the ollama URL + model baked in as overridable defaults.
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/loyd" <<LAUNCH
#!/usr/bin/env bash
export OLLAMA_URL="\${OLLAMA_URL:-$OLLAMA_URL}"
export OLLAMA_AI_MODEL="\${OLLAMA_AI_MODEL:-$LOYD_MODEL}"
exec python3 "$LOYD_HOME/cli.py" "\$@"
LAUNCH
chmod +x "$BIN_DIR/loyd"
say "Installed launcher: $BIN_DIR/loyd  (model=$LOYD_MODEL, ollama=$OLLAMA_URL)"

# 3. Best-effort connectivity check.
if python3 - "$OLLAMA_URL" "$LOYD_MODEL" <<'PY' 2>/dev/null; then
import sys, json, urllib.request
url, model = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(url.rstrip('/') + "/api/tags", timeout=5) as r:
        tags = [m["name"] for m in json.load(r).get("models", [])]
    print("  ollama reachable; models:", ", ".join(tags[:6]) or "(none)")
    sys.exit(0 if any(model.split(':')[0] in t for t in tags) else 3)
except Exception as e:
    print("  ollama not reachable:", e); sys.exit(1)
PY
  say "ollama OK and $LOYD_MODEL is present."
else
  rc=$?
  [ "$rc" = 3 ] && warn "ollama reachable but $LOYD_MODEL not pulled there — run: ollama pull $LOYD_MODEL"
  [ "$rc" = 1 ] && warn "Couldn't reach ollama at $OLLAMA_URL — set OLLAMA_URL and re-run, or check tailscale/LAN."
fi

# 4. PATH hint.
case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) warn "Add $BIN_DIR to your PATH:  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc && source ~/.bashrc" ;;
esac

printf '\033[32m✓ Loyd installed.\033[0m  Try:  loyd \"is the plex container running and how much free disk on /mnt/user?\"\n'
