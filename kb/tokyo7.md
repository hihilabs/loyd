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

## Appdata backup and Plex (changed 2026-11-20)
- The appdata.backup plugin runs daily at 00:00 (`/boot/config/plugins/appdata.backup/`), containers one after another; a full run takes about 3 hours. It used to stop Plex for 5–10 minutes whenever it reached it (between 03:00 and 07:00).
- Since 2026-11-20 Plex-Media-Server is set to `dontStop: yes`, `verifyBackup: no` in `config.json`, so it's backed up live. Plex's own database backups (every 3 days, in `.../Plug-in Support/Databases/`) are the consistent copy. Undo: `config.json.bak-20261120-plexlive`.
- Auto-update is on for every container, so on a night when Plex has an update, the backup still restarts it.

## Tdarr (TV library, audio leveling added 2026-11-20)
- `tdarr` container, web UI and API on :8265. Libraries: Movies (`/mnt/media/movies`) and TV Shows (`/mnt/media/series`, id `libTV`), both on the flow `plexFriendly1080p`. Transcodes run only 01:00–09:00, one GPU worker, about 1,400 files a night.
- 2026-11-20 the TV queue held 13,401 files (Pokémon, The Simpsons, Survivor, ...), so about 9 nights of work. Queue order is unsorted; the `bumped` flag on a file moves it to the front.
- Bob's Burgers volume: measured 2026-11-20, 312 episodes from −29.3 to −19.4 LUFS (season 7 about −29, stereo AAC in S10–S12 about −20). The flow now starts with `bob_*` nodes: a Bob's Burgers file whose first audio track lacks the `LOUDNORM=-24` tag gets that track leveled (`loudnorm=I=-24:TP=-2:LRA=11`, EAC3 640k, 48 kHz), video and subtitles copied, a 60–160% size check, then replace. Each episode takes about 3 minutes.
- The 108 episodes more than 1.5 dB from −24 were queued and bumped. A check was scheduled for 2026-11-21 09:20 Pacific; the first night didn't reach them. The other 204 are within 1.5 dB and were left alone.
- Files: `/mnt/tdarr_cache/appdata/tdarr/flow-backups/` holds the flow from before the change (restore it to undo), `queued.txt` (the 108) and `bob-loudness.tsv` (all measurements).
- Tdarr API: `POST /api/v2/cruddb` with `{"data":{"collection":"FlowsJSONDB"|"FileJSONDB"|"LibrarySettingsJSONDB","mode":"getById"|"getAll"|"update",...}}`. A `getAll` on FileJSONDB is slow (26k files); run it in the background.
