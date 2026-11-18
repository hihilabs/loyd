# Loyd backlog

Worked by the **"Loyd backlog"** scheduled task (every 5 hours) and by humans.
Rules for the agent: take the **first unchecked item in "Agent-ready"**, do only
that item, add tests, keep CI green, open a PR that also ticks the box here.
Never deploy, merge, rotate tokens, or touch hosts — those are in "Needs a human".
If an item turns out bigger than one PR, split it here and do the first piece.

## Agent-ready (top = next)

- [ ] **Trace recorder.** Opt-in (`LOYD_TRACE_DIR`) full-session traces: task, each tool call
      + result, final answer, and a `brain` tag (`claude` | `local` | `human`). The trace
      format has a `trainable` flag that is ALWAYS false when `brain == "claude"` (Anthropic
      terms: no Claude output as model-training data). Include a `tools/export_training.py`
      that only exports `trainable: true` records and test that Claude records can't leak.
- [ ] **Knowledge base + `search_kb` tool (retrieval, not training).** `kb/` of markdown
      notes: FINDINGS, runbooks, kit notes, and short "session notes" written at the end of
      Claude research sessions. Stdlib-only ranked keyword search (no embeddings dependency
      yet). Used as *context* for the local model; never fed to training.
- [ ] **Brain router.** `LOYD_BRAIN_POLICY=claude-first | local-first | auto`. `auto` asks the
      local model one yes/no question ("routine maintenance a kit covers, or research /
      open-ended?") and routes routine work local, research to Claude, with escalation to
      Claude if the local run fails or hits the step limit. Default stays `claude-first`
      (policy 2026-09-04) until a human flips it.
- [ ] **GPU modes: `online` / `offline` profiles** (`tools/gpu_mode.sh` + an operator-only,
      confirm-gated tool). `online`: vision owns both GPUs; local Loyd model loads on demand
      with a short ollama keep-alive so it never camps on VRAM. vision is fully local and keeps
      running offline (decided 2026-11-18), so `offline` gives Loyd's bigger model GPU1 only
      when vision is idle or at night, never by pausing vision; `online` reverses it. Dry-run by default; container names come from config, not hardcoded.
      Hooked to offline detection (suggest the switch; don't auto-flip without confirm).
- [ ] **Offline detection.** Probe the Claude API once, cache the result a few minutes, skip
      straight to local while down; `LOYD_OFFLINE=1` override; OFFLINE in the banner.
- [ ] **Codify the safe deploy** as `deploy/deploy.sh` + `deploy/loyd-mcp.service`: git worktree
      of the target ref → run `pytest` with the prod venv → start it on a spare port with the
      prod env file and smoke-test `/healthz`, OAuth metadata, 401 on the MCP path → switch
      `/opt/loyd` → schedule restart + health check with auto-rollback (what was done by hand
      on 2026-11-18, see `/root/loyd-deploy-check.sh` on panel). Add systemd hardening
      (`NoNewPrivileges`, `ProtectSystem=strict` + `ReadWritePaths` for the audit log,
      `PrivateTmp`). Document in MCP.md. Dry-run mode that prints steps.
- [ ] **tmux tools.** `run_in_tmux(name, command)` starts a long job (build, backup,
      `docker pull`) in a named detached tmux window and returns at once, beating
      `run_shell`'s 30 s timeout; `tmux_read(target, lines)` captures a pane's recent output
      (read-only) so "what's failing in my build pane?" works. Writes go through the
      operator/confirm/audit path like any write tool; window names validated.
- [ ] **`netdiag` kit (read-only troubleshooting)**, acting wherever Loyd runs: `net_ping`
      (count ≤ 10), `net_trace`, `net_dns` (name, type, optional server — compare the router's
      answer with 1.1.1.1 to spot split-DNS issues), `net_port` (TCP connect test),
      `net_http` (status, timing, cert expiry), `net_iface` (addresses, routes, gateway, DNS
      servers), `net_neighbors` (ARP/neighbour table), `whats_listening` (local listening
      sockets). Plus a **"something's out" playbook**: gateway → DNS → internet → each HiHi
      service, stopping at the first failure and saying which layer broke. Inputs validated
      (hostnames/IPs only), counts and timeouts capped.
- [ ] **`audit` kit — authorized network audits only, scope-gated.** Loads only when a scope
      file `audits/<client>.json` exists: client, who authorized it, authorization reference,
      allowed CIDRs + domains, start/expiry, intensity (`gentle` default). EVERY audit tool
      refuses a target outside scope or after expiry, and every call is audited with the
      scope id. Tools: host discovery, service/version inventory (nmap, rate-limited, top
      ports by default), TLS check (cert expiry, old protocols), DNS + mail hygiene for the
      client's domains (SPF/DKIM/DMARC), flags for exposed management services (RDP, SMB,
      Telnet, UPnP, admin panels on default ports), and `audit_report` → findings markdown
      with severity and a fix per finding. Explicitly NOT included: exploitation, credential
      guessing, traffic interception, Wi-Fi attacks. Tests must prove out-of-scope and
      expired scopes are refused. On a client site Loyd runs on the field device (laptop or
      Termux in DeX) with `LOYD_REMOTE` unset, so it acts on that network.
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

- [x] Kit framework: `kits.py`, `kits/ops.json` (verified, read-only), `kits/hihilabs.json` skeleton (unverified) — PR #1
- [x] OAuth completed (signed short-lived tokens, refresh, redirect validation) — PR #1
- [x] Read-only callers get a read-only shell; `LOYD_REMOTE_READONLY` — PR #1
- [x] Audit log (+ rotation), remote `write_file`, head+tail trimming, CI — PR #1
- [x] Claude backend → `claude-opus-5-5`, prompt caching, effort, refusal fallbacks — PR #1
- [x] Deployed to panel 2026-11-18 (e7c9f73), health-checked with auto-rollback armed
