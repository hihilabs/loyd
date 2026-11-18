# Loyd over MCP

`loyd_mcp.py` exposes Loyd's tools (`run_shell`, `read_file`, `get_container_status`,
and the operator writes `write_file` / `restart_container` / `prune_images`) as a
**Streamable HTTP MCP server**. Point any MCP client at it — Claude Desktop, the
Claude apps, VS Code, opencode — and full Claude, on your subscription, drives your
ops tools. No `ANTHROPIC_API_KEY`, no per-token API billing.

It reuses `tools.py` verbatim and acts wherever `LOYD_REMOTE` points, so run it on
the VPS with `LOYD_REMOTE=root@tokyo7` and you're driving Tokyo7 from your car.

## ⚠️ This is remote shell/file/docker access. Read the security section.

## Install + run (on the host that will act — e.g. the VPS)

```bash
python3 -m venv ~/.loyd-mcp && . ~/.loyd-mcp/bin/activate
pip install -r requirements-mcp.txt

# one token per user:  user:role:token   (role r = read-only, o = operator)
export LOYD_MCP_TOKENS="you:o:$(openssl rand -hex 16),mate:r:$(openssl rand -hex 16)"
export LOYD_REMOTE=root@tokyo7          # optional: act on Tokyo7 over SSH (else acts local)
python3 loyd_mcp.py                      # binds 127.0.0.1:8848/mcp by default
```

Print `$LOYD_MCP_TOKENS` once to copy each user's token, then give each person theirs.
Env knobs: `LOYD_MCP_HOST` (default `127.0.0.1`), `LOYD_MCP_PORT` (`8848`),
`LOYD_MCP_PATH` (`/mcp`). With **no** tokens set it refuses to bind anything but
localhost (fail-closed).

## Security model (what's enforced)

| Control | Behavior |
|---|---|
| **Bearer token required** | No/!valid `Authorization: Bearer <token>` → `401`. Verified: a bad token can't `initialize`. |
| **Operator scoping** | Read-only tokens don't see *or* run the write tools — hidden in `tools/list` **and** denied in `tools/call` (belt + suspenders). Verified. |
| **Client approval** | Every MCP client also prompts the user to approve each tool call — the human gate, on top of the token. |
| **Fail-closed bind** | No tokens ⇒ localhost only; won't expose itself unauthenticated. |

**You still must terminate TLS in front of it.** The bearer token *is* the keys —
plain HTTP on a public interface hands it to anyone sniffing. Options below.

## Exposure — pick by client (this is the load-bearing decision)

Two kinds of client, two reachability needs:

**A. Client runs on your own device** (opencode, VS Code, Claude Desktop's *local*
MCP): the connection is made *from your machine*. Keep the server **on your tailnet**
and point the client at the tailscale URL — **no public exposure at all**, and the
bearer token is a second factor. This is the recommended path for car/Steam Deck/laptop.

```bash
# on the VPS (or Tokyo7), expose only to your tailnet:
tailscale serve --https=443 http://127.0.0.1:8848    # tailnet-only HTTPS
# clients use  https://<host>.<tailnet>.ts.net/mcp
```

**B. claude.ai account connector (web + the Claude mobile app):** Anthropic's servers
make the connection, so the URL must be **publicly reachable**. Use a public tunnel:

```bash
tailscale funnel --https=443 http://127.0.0.1:8848   # public HTTPS via tailscale, OR
cloudflared tunnel --url http://127.0.0.1:8848        # public HTTPS via cloudflare
```

**claude.ai / mobile / desktop Connectors use OAuth** — no token in the URL needed.
Add the connector with the bare `https://…/mcp` URL; the app discovers Loyd's OAuth
endpoints, opens a **Connect to Loyd** page, you paste your Loyd token once (tick
*Read-only* to hide the write tools for that connection), and you're in. The
`?key=<token>` URL fallback still works for anything that can't do OAuth, but it puts
the master token in access logs — prefer OAuth.

Set `LOYD_MCP_PUBLIC_URL=https://loyd.hihilabs.xyz` on the server so OAuth URLs always
match the public door (otherwise they're reflected from the `Host` header nginx passes).

## Per-client connect (verify each on your actual client — UIs drift)

**opencode / VS Code (Continue, Cline, Copilot MCP) / any config-file client** — add a
remote MCP server with an auth header:

```jsonc
{
  "mcpServers": {
    "loyd": {
      "url": "https://<host>.<tailnet>.ts.net/mcp",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

**Claude Desktop (local MCP via config):** same shape in
`claude_desktop_config.json` → `mcpServers` (transport `streamable-http`, `url`, and an
`Authorization` header). Restart Desktop after editing.

**claude.ai / Claude mobile app (account connector):** Settings → Connectors → *Add
custom connector* → Name `Loyd`, URL = your **public** `https://…/mcp` (no `?key=`).
Leave the OAuth client ID/secret fields blank — Loyd registers the client itself.
Click *Connect*, paste your token on the Loyd page, done.

## OAuth — how it works and what it guarantees

| Property | How |
|---|---|
| **Master token never leaves the box** | `/token` returns a 1h access token + 30-day rolling refresh token, HMAC-signed with a key derived from your static token. The app never sees the static token. |
| **Revocation = rotation** | Change a token in `LOYD_MCP_TOKENS` and restart: every OAuth grant minted from it stops verifying. |
| **Per-connection read-only** | The *Read-only* checkbox on the consent page scopes that grant (and its refreshes) to read tools only, even for an operator token. |
| **Survives restarts / `/update`** | Stateless: client_ids and tokens are signed blobs, nothing stored. Only the 5-min auth code lives in memory. |
| **No open redirect / code theft** | client_id is a signed record of its registered redirect_uris; `/authorize` refuses (doesn't redirect) any other URI. Redirects must be https, loopback http, or an app scheme. |
| **Brute-force limit** | 8 wrong tokens per IP per 10 min on the consent page → 429. |
| **Spec** | RFC 8414, 7591, 9728, 6749 (code + refresh), 7636 PKCE/S256 (required), 9207 `iss`. Consent page is `X-Frame-Options: DENY`. |

Knobs: `LOYD_MCP_ACCESS_TTL` (s, default 3600), `LOYD_MCP_REFRESH_TTL` (default 30d),
`LOYD_MCP_OAUTH_SECRET` (optional; signs client_ids — default is derived from the token
set, so changing tokens makes clients re-register, which they do automatically).

## Users & tokens

- One entry per person in `LOYD_MCP_TOKENS`; give each their own token.
- Make yourself an operator (`o`), teammates read-only (`r`) unless they need writes.
- Rotate by editing `LOYD_MCP_TOKENS` and restarting. Tokens are the whole security
  boundary — treat them like SSH keys.

## What's tested vs. not

- **Tested here:** server auth (401 on bad token), operator scoping (read-only can't
  see/run writes), real tool execution over MCP (`get_container_status` returned live
  state), stateless streamable-HTTP round-trip with the official MCP client.
- **OAuth:** `pytest tests/` (18 cases: full flow, read-only scoping, refresh rotation +
  client binding, PKCE, single-use codes, redirect validation, tampering, rotation
  revokes, expiry, throttle), plus an end-to-end run with the official MCP SDK's
  `OAuthClientProvider` (discover → register → consent → token → `tools/call`).
- **Not yet tested** (needs your devices): each client's connect flow (claude.ai web,
  mobile, Desktop) against the live `loyd.hihilabs.xyz` door. Do a dry run per client,
  ideally ticking *Read-only* first.
