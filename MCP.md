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

**claude.ai web modal auth:** the "Add custom connector" modal takes a URL + *optional
OAuth* only — no custom-header field, so it can't send a bearer header. `loyd_mcp`
handles this with a **`?key=<token>` URL fallback**: paste `https://…/mcp?key=<token>`
into the modal. It's authenticated the same way (same token map, same operator scoping)
— the only cost is the token appears in access logs, so prefer the header path on
clients that support it, and consider a read-only token for the web/mobile surface.

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
custom connector* → Name `Loyd`, URL = your **public** `https://…/mcp` — see the auth
gap above.

## Users & tokens

- One entry per person in `LOYD_MCP_TOKENS`; give each their own token.
- Make yourself an operator (`o`), teammates read-only (`r`) unless they need writes.
- Rotate by editing `LOYD_MCP_TOKENS` and restarting. Tokens are the whole security
  boundary — treat them like SSH keys.

## What's tested vs. not

- **Tested here:** server auth (401 on bad token), operator scoping (read-only can't
  see/run writes), real tool execution over MCP (`get_container_status` returned live
  state), stateless streamable-HTTP round-trip with the official MCP client.
- **Not yet tested** (needs your devices): each specific client's connect flow, the
  claude.ai web modal auth, and the TLS tunnel. Do a dry run per client before trusting
  it, ideally with a read-only token first.
