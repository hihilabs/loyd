<!-- source: claude -->
# Leaving GitHub (decided 2026-11-20, not done yet)

Andrew's decision: keep Git, move off GitHub (Microsoft). Forgejo is already the primary
forge. This is the checklist for the rest, to do from a desktop, one step at a time.
Nothing here has been changed yet.

## What still depends on GitHub (as of 2026-11-20)
- **Repos**: `hihilabs/loyd`, `hihilabs/hihi`, `hihilabs/hihi.labs`, `hihilabs/communityplaylist`
  (and anything else under the hihilabs org, so list the org first).
- **hihilabs.xyz**: the VPS checkout (`/var/www/vhosts/hihilabs.xyz/app`) and the Unraid dev
  copy use the remote `git@github-hihi-labs:hihilabs/hihi.labs.git`. The VPS deploy flow is
  push to `origin main`, then pull on the VPS.
- **communityplaylist.com**: auto-deploys on push through Plesk's Git extension; remote
  `git@github.com:khildren/communityplaylist.git`, which redirects to `hihilabs/communityplaylist`.
- **The Loyd panel and Tokyo7 CLI checkouts** (`/opt/loyd`, `/mnt/user/appdata/loyd`) pull
  from GitHub; `deploy/deploy.sh` runs `git fetch origin`.
- **CI**: loyd's `.github/workflows/tests.yml` runs on GitHub Actions; GitGuardian scans each push.
- **Claude Code cloud sessions** (claude.ai/code) reach repos only through GitHub. After the move,
  this kind of work runs in Claude Code in VS Code or on Tokyo7, against Forgejo.
- **The scheduled "Loyd backlog" task** runs in those cloud sessions, so it stops working when
  GitHub goes. Move it to Tokyo7 (cron + `claude -p`) or retire it.

## Order
1. Inventory: list every repo in the GitHub org and check each has full history and all
   branches on Forgejo (`git ls-remote` both, compare).
2. Switch deploy remotes to Forgejo, one host at a time: the VPS hihilabs checkout, the
   Unraid copy, Plesk's Git setting for communityplaylist, `/opt/loyd`, Tokyo7's loyd checkout.
   Check each deploy still works (CLAUDE.md: prod changes wait for Andrew's yes).
3. CI: a Forgejo Actions runner on Tokyo7; copy `tests.yml` to `.forgejo/workflows/`
   (its syntax is close to GitHub Actions). Replace GitGuardian with a local secret scan
   (e.g. gitleaks in CI) so secrets are still caught before push.
4. Off-site backup of Forgejo's data (repos + database) to somewhere that isn't Tokyo7, e.g.
   the VPS or an encrypted bucket. GitHub has quietly been the off-site copy until now.
5. Archive the GitHub repos read-only; delete them later when comfortable.
6. Add a "Leave GitHub" line to BACKLOG.md's "Needs a human" list if it should be tracked
   there (left to Andrew: agents don't edit that section).

## Optional middle path
Forgejo stays primary; a one-way push mirror to GitHub only for repos that are fine there
keeps cloud Claude sessions working on those. Sensitive repos (camagent, client CRMs,
anything with customer data) never leave Andrew's machines.
