# Loyd backlog

Worked by the **"Loyd backlog"** scheduled task (every 5 hours) and by humans.
Rules for the agent: take the **first unchecked item in "Agent-ready"**, do only
that item, add tests, keep CI green, open a PR that also ticks the box here.
Never deploy, merge, rotate tokens, or touch hosts — those are in "Needs a human".
If an item turns out bigger than one PR, split it here and do the first piece.

## Agent-ready (top = next)

- [ ] **GPU modes: `online` / `offline` profiles** (`scripts/gpu_mode.sh` — never a `tools/` dir, it would shadow tools.py — + an operator-only,
      confirm-gated tool). `online`: vision owns both GPUs; local Loyd model loads on demand
      with a short ollama keep-alive so it never camps on VRAM. vision is fully local and keeps
      running offline (decided 2026-11-18), so `offline` gives Loyd's bigger model GPU1 only
      when vision is idle or at night, never by pausing vision; `online` reverses it. Dry-run by default; container names come from config, not hardcoded.
      Hooked to offline detection (suggest the switch; don't auto-flip without confirm).
- [ ] **Small-model double tool call** (FINDINGS rough edge): `loyd_core.LoydAgent` sometimes
      repeats a near-identical call before concluding. After a repeat is detected, drop the
      tools for the next step (force a text answer). Tests with a fake ollama server.
- [ ] **Tests for `loyd_core`**: `_first_json_obj`, `_extract_call` (native vs text-JSON),
      the conclude-guard on the last step, confirm/decline path — using a stub `/api/chat`.
- [ ] **Tests for `cli.py`**: `should_confirm`, `handle_slash` (`/tools`, `/model`, `/audit`),
      `self_update` refusing on a detached HEAD.
## Needs a human

- [ ] camagent: decide on agreement-gated auto-accept (both models agree → accept, else review) and Qwen + valid-list snap for refrigerant (36% → 86%). See `kb/vision-accuracy.md`.
- [ ] Fix the golden labels from `golden_second_check_20260922.md` (a person confirms each), then re-run `scripts/vision_eval.py`.
- [ ] Optional: switch panel to the hardened `deploy/loyd-mcp.service` (review the paths first), then redeploy with `deploy/deploy.sh`.
- [ ] For field audits: install nmap on the field device (laptop / Termux: `pkg install nmap dnsutils openssl-tool`), write the scope file from the client's written authorization, then `LOYD_KITS=...,audit`.
- [ ] Install mosh on Tokyo7 (Unraid: un-get / NerdTools) so `mosh tokyo7 -- tmux new -A -s loyd loyd` works from the phone; Termux + Tailscale on the Samsung.
- [ ] Verify the `hihilabs` kit commands (`kits/hihilabs.json`: container name, test runner, migrate) and set `LOYD_KITS=ops,hihilabs` on the MCP server against the real container/repo and flip
      `"verified": true` (first app-dev kit, chosen 2026-11-18).
- [ ] Decide if/when to flip `LOYD_BRAIN_POLICY` to `auto` (reverses "GPU box never primary").
- [ ] GPU plan (decided 2026-11-18: Quadros are out; the two 5060 Tis time-share). Confirm
      what the vision pipeline needs offline (does anything run without CompanyCam?) and
      pick the 32B offline model that fits both cards with useful context.
- [ ] LoRA experiment (after traces + a 30–50 task eval set exist): base Qwen + per-kit
      adapters trained only on `trainable` data; keep only if it beats the base on the eval.
- [ ] Merge PR #1, then on panel: `git -C /opt/loyd checkout main && git -C /opt/loyd pull`.
- [ ] Rotate the `admin1` token (it was shown in a chat on 2026-11-18). Reconnect clients after.
- [ ] Create the `loyd-ro` login on Tokyo7 and set `LOYD_REMOTE_READONLY` (MCP.md).
- [ ] Per-person tokens for Geoff and IamMe (read-only to start).
- [ ] Update the Tokyo7 CLI checkout (`/mnt/user/appdata/loyd`) after merge.

## Done

- [x] Vision accuracy measured (`scripts/vision_eval.py`, `kb/vision-accuracy.md`): agreement-gated auto-accept and Qwen+snap for refrigerant are the big wins — 2026-11-19
- [x] Brain router: `brain.py` + `LOYD_BRAIN_POLICY=claude-first|local-first|auto` (default claude-first; auto asks local one YES/NO, doubt → Claude; local-first escalates on error/empty/step limit via `LoydAgent.last_outcome`) — tests/test_brain.py
- [x] `audit_tail` (operator-only, filter by actor/outcome) and per-identity MCP rate limit (`LOYD_MCP_RATE`, default 60/min, throttles audited) — PR #1
- [x] Trace recorder: `loydtrace.py` (`LOYD_TRACE_DIR`), `/good` `/bad` ratings in the CLI, `scripts/export_training.py` (local + rated-good only; Claude traces refused twice, incl. a tampered-file test) — PR #1
- [x] Safe deploy codified: `deploy/deploy.sh <ref> [--dry-run]` (worktree, tests, spare-port smoke, scheduled restart + auto-rollback), tested end to end with a real smoke server and a forced rollback; hardened `deploy/loyd-mcp.service` example — PR #1
- [x] Knowledge base: `kb.py` (`search_kb`, `read_kb`; BM25 over sections of kb/*.md + repo docs, stdlib, offline), seeded with the Tokyo7 stack map and outage/deploy runbooks — PR #1
- [x] `tail_logs` — covered by the `ops` kit's `ops_container_logs` (name + last N lines) — PR #1
- [x] `audit` kit (`auditkit.py`): scope-file gate, discover/services (nmap, rate-limited), TLS, mail DNS, report; real-nmap test — PR #1
- [x] Offline detection: one cached probe of the Claude API (120 s), mid-request network errors remembered, `LOYD_OFFLINE=1/0`, OFFLINE banner — PR #1
- [x] tmux tools: `run_in_tmux` (Loyd's own `tmux -L loyd` server, run_shell's rules), `job_output`, `tmux_read` — live-tested on Tokyo7 — PR #1
- [x] `netdiag` kit (11 read-only commands incl. layered `triage`), all run live on Tokyo7 2026-11-18 — PR #1
- [x] Kit framework: `kits.py`, `kits/ops.json` (verified, read-only), `kits/hihilabs.json` skeleton (unverified) — PR #1
- [x] OAuth completed (signed short-lived tokens, refresh, redirect validation) — PR #1
- [x] Read-only callers get a read-only shell; `LOYD_REMOTE_READONLY` — PR #1
- [x] Audit log (+ rotation), remote `write_file`, head+tail trimming, CI — PR #1
- [x] Claude backend → `claude-opus-5-5`, prompt caching, effort, refusal fallbacks — PR #1
- [x] Deployed to panel 2026-11-18 (e7c9f73), health-checked with auto-rollback armed
