<!-- source: claude -->
# Tokyo7 and panel: how the stack is wired (verified 2026-11-18)

## Hosts
- **Tokyo7**: Unraid box at home. Runs the apps, the vision pipeline, Ollama, Whisper, Plex. Two RTX 5060 Ti (16 GB each).
- **panel**: the VPS. Runs the Loyd MCP server (`loyd-mcp-tokyo7.service`) and `loyd-opencode.service`. Reaches Tokyo7 over Tailscale SSH (port 22022).

## Loyd MCP server (on panel)
- Code: `/opt/loyd` (git checkout). Python venv: `/opt/loyd/.venv`.
- Config: `/etc/loyd/tokyo7.env` (tokens, `LOYD_REMOTE`, `LOYD_MODE=operator`, `LOYD_MCP_PATH=/tokyo7`, `LOYD_MCP_PUBLIC_URL`, `LOYD_KITS=ops,netdiag`). Never print the tokens.
- Listens on 127.0.0.1:8848. Public door: `https://loyd.hihilabs.xyz/tokyo7` through Cloudflare and nginx (`/etc/nginx/conf.d/loyd-hihilabs.conf`, passes the Host header).
- Health: `curl -s -H 'Host: loyd.hihilabs.xyz' http://127.0.0.1:8848/healthz` → `{"ok": true}`.
- Audit log: `/root/.loyd/audit.jsonl` on panel.

## GPUs and Ollama (on Tokyo7)
- `ollama-gpu0` → GPU0, host port **11434**. `ollama-gpu1` → GPU1, host port **11435**. Compose: `/mnt/app_data/appdata/ollama-pool/docker-compose.yml`.
- GPU0 holds the vision model (minicpm-v) and nomic-embed. GPU1 had Loyd's text models (`loyd-agent`, a Qwen 3.5 model at 32k context, and `qwen2.5-coder:7b`) pinned forever, about 12 GB, until 2026-11-18.

## Vision pipeline (camagent)
- Reads HVAC data plates from job photos: brand, model, serial, refrigerant, tonnage, voltage, manufacture date. Photos are stored locally; nothing goes to a cloud vision API (no Anthropic key in the workers, metered use off).
- Compose: `/mnt/user/appdata/camagent/worker/docker-compose.yml`, shared env in `worker/.env`. Two workers: `camagent-worker-gpu0` (the fleet's `vlm_extract` node) and `camagent-worker-gpu1`.
- 2026-11-18: worker-gpu1 was sending its plate reads to GPU0 (port 11434 from `.env`). Fix: an `OLLAMA_URL=…:11435` override on worker-gpu1 only. Backups and the apply log: `/mnt/user/appdata/camagent/backups/gpu-wiring-20261118/`.
- Benchmarks and the human-checked "golden" set: `/mnt/user/appdata/camagent/bench_results/`. Known issues: some golden labels are wrong (see `golden_second_check_20260922.md`), and the scorer counts near-misses as hits; exact-match model accuracy was about 42%.
- A restarted worker loses its current job; the hub re-queues jobs stale for 30 minutes and they start over.

## DNS (known problem)
- Our own names (hihilabs.xyz, crm.hihilabs.xyz, agent.omegamep.com, local.* names) resolve to Cloudflare even inside the house, so container-to-container calls leave the house. They break when the internet does. Fix planned: split DNS on the router. `netdiag_triage service=hihilabs.xyz` shows "(public)" until it's fixed.

## Tools on Tokyo7
- Present: ping, traceroute, tracepath, dig, nslookup, ss, ip, curl, openssl, python3, tmux 3.5a.
- Missing: mtr, nc, nmap, mosh-server.
