#!/usr/bin/env python3
"""
Loyd CLI — talk to your local dev/ops assistant.

  loyd "is the plex container running and how much disk is free?"   # one-shot
  loyd                                                              # interactive REPL

Cloud Claude answers by default; the local Ollama brain is the fallback for
when the cloud path can't be reached (policy 2026-09-04 — the GPU box is never
primary, its uptime isn't ours to guarantee). Both share the same tools.

The local brain is swappable:  --model cogito:8b  or  OLLAMA_AI_MODEL=cogito:8b loyd ...

REPL slash commands:
  /local <q>    force one question onto the local brain instead of Claude
  /claude <q>   force one question onto cloud Claude (the default anyway)
  /model [name] show or hot-swap the local brain
  /tools        list the tools Loyd can call
  /help         this help
  /exit         quit
(`loyd "/claude ..."` also works as a one-shot.)
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loyd_core import LoydAgent, build_system     # noqa: E402
from claude_backend import ClaudeAgent, ClaudeUnavailable   # noqa: E402
from tools import TOOL_SCHEMAS, execute_tool, WRITE_TOOLS, where, mode, is_destructive  # noqa: E402


def should_confirm(name, args):
    """Which tool calls get a y/N. Write tools always; in operator mode also any
    destructive run_shell command (so safe commands run free, dangerous ones prompt)."""
    if name in WRITE_TOOLS:
        return True
    if name == "run_shell" and mode() == "operator":
        return is_destructive(args.get("command", ""))
    return False

DIM, CYAN, GREEN, YEL, RED, RST = "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def confirm_write(name, args):
    """Interactive y/N gate before any mutating tool runs."""
    if name == "write_file":
        detail = f"path={args.get('path')!r}  ({len(args.get('content') or '')} chars)"
    else:
        detail = ", ".join(f"{k}={v!r}" for k, v in args.items())
    try:
        ans = input(f"{RED}⚠  Loyd wants to run {name}({detail}). Allow? [y/N] {RST}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


def make_printer():
    def on_event(kind, data):
        if kind == "tool_call":
            args = ", ".join(f"{k}={v!r}" for k, v in data["args"].items())
            src = "" if data["source"] == "native" else f" {DIM}[{data['source']}]{RST}"
            print(f"{CYAN}→ {data['name']}({args}){RST}{src} {DIM}{data['dt']:.1f}s{RST}")
        elif kind == "tool_result":
            r = data["result"]
            preview = r if len(r) <= 500 else r[:500] + f" …(+{len(r)-500})"
            print(f"{DIM}{preview}{RST}")
        elif kind == "tool_declined":
            print(f"{RED}✗ declined: {data['name']}{RST}")
        elif kind == "final":
            print(f"{GREEN}{data['text']}{RST}")
        elif kind == "error":
            print(f"{RED}{data['text']}{RST}", file=sys.stderr)
    return on_event


class Session:
    """Holds the local agent + a lazily-built Claude escalation agent so both
    share tools, executor, confirm gate, and the on_event printer."""

    def __init__(self, model, url, claude_model, max_iters):
        self.printer = make_printer()
        self.claude_model = claude_model
        self.max_iters = max_iters
        self._system = build_system(where(), mode())
        self.agent = LoydAgent(
            model=model, ollama_url=url, system=self._system,
            tool_schemas=TOOL_SCHEMAS, execute=execute_tool,
            max_iters=max_iters, on_event=self.printer,
            confirm=confirm_write, write_tools=WRITE_TOOLS, should_confirm=should_confirm)
        self._claude = None

    def claude(self):
        if self._claude is None:
            self._claude = ClaudeAgent(
                model=self.claude_model, api_key=os.getenv("ANTHROPIC_API_KEY"),
                tool_schemas=TOOL_SCHEMAS, execute=execute_tool, system=self._system,
                max_iters=self.max_iters, on_event=self.printer,
                confirm=confirm_write, write_tools=WRITE_TOOLS, should_confirm=should_confirm)
        return self._claude

    def ask_local(self, q):
        self.agent.run(q)

    def ask_claude(self, q):
        key = "set" if os.getenv("ANTHROPIC_API_KEY") else "MISSING"
        print(f"{DIM}↑ {self.claude_model} (key {key}){RST}", file=sys.stderr)
        self.claude().run(q)

    def ask(self, q):
        """The default path: Claude, with the local brain picking up slack.

        Falling back is only safe because ClaudeUnavailable is raised before
        anything is emitted -- same 'fall back only while uncommitted' rule
        the hihilabs router uses. A Claude answer we dislike is still an
        answer and does not get retried locally."""
        try:
            self.ask_claude(q)
        except ClaudeUnavailable as e:
            print(f"{DIM}↓ cloud unavailable ({e}) — falling back to {self.agent.model}{RST}",
                  file=sys.stderr)
            self.ask_local(q)


def self_update():
    """git pull the install dir (where this cli.py lives) and re-exec to load it.
    No-op with a hint if the dir isn't a git checkout (e.g. installed via file copy)."""
    home = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isdir(os.path.join(home, ".git")):
        print(f"{DIM}{home} isn't a git checkout — re-run install.sh to update.{RST}")
        return
    print(f"{DIM}updating {home} …{RST}")
    p = subprocess.run(["git", "-C", home, "pull", "--ff-only"], capture_output=True, text=True)
    out = (p.stdout + p.stderr).strip()
    if out:
        print(f"{DIM}{out}{RST}")
    if p.returncode != 0:
        print(f"{RED}update failed — resolve in {home} and retry.{RST}")
        return
    if "up to date" in out.lower():
        return
    print(f"{GREEN}updated — reloading Loyd…{RST}")
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, os.path.join(home, "cli.py"), *sys.argv[1:]])


def handle_slash(q, sess):
    """Dispatch a /command in the REPL. Returns 'exit' to quit, else None."""
    cmd, _, rest = q.partition(" ")
    cmd, rest = cmd.lower(), rest.strip()
    if cmd in ("/help", "/?"):
        print(f"{DIM}/local <q>   force the local brain (Claude is the default)\n"
              f"/claude <q>  force cloud Claude (same tools)\n"
              f"/model [name] show or swap the local brain\n"
              f"/update       git-pull the latest Loyd and reload\n"
              f"/tools        list callable tools\n"
              f"/exit         quit{RST}")
    elif cmd == "/tools":
        for s in TOOL_SCHEMAS:
            f = s["function"]
            tag = f" {RED}(write, confirm-gated){RST}" if f["name"] in WRITE_TOOLS else ""
            print(f"{CYAN}{f['name']}{RST}{tag} {DIM}{f['description'].split('.')[0]}{RST}")
    elif cmd == "/model":
        if rest:
            sess.agent.model = rest
            print(f"{DIM}local brain -> {rest}{RST}")
        else:
            print(f"{DIM}local brain = {sess.agent.model}{RST}")
    elif cmd == "/local":
        if rest:
            sess.ask_local(rest)
        else:
            print(f"{DIM}usage: /local <question>{RST}")
    elif cmd == "/claude":
        if rest:
            sess.ask_claude(rest)
        else:
            print(f"{DIM}usage: /claude <question>{RST}")
    elif cmd == "/update":
        self_update()
    elif cmd in ("/exit", "/quit", "/q"):
        return "exit"
    else:
        print(f"{DIM}unknown command {cmd!r} — try /help{RST}")
    return None


def main():
    ap = argparse.ArgumentParser(description="Loyd — local dev/ops assistant")
    ap.add_argument("question", nargs="*", help="one-shot question (omit for interactive)")
    ap.add_argument("--model", default=os.getenv("OLLAMA_AI_MODEL", "qwen2.5-coder:7b"))
    ap.add_argument("--url", default=os.getenv("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--claude-model", default=os.getenv("LOYD_CLAUDE_MODEL", "claude-opus-4-8"))
    ap.add_argument("--max-iters", type=int, default=8)
    args = ap.parse_args()

    sess = Session(args.model, args.url, args.claude_model, args.max_iters)

    key = ("claude ready" if os.getenv("ANTHROPIC_API_KEY")
           else f"{RED}no ANTHROPIC_API_KEY — every turn falls back to {args.model}{RST}{DIM}")
    m = mode()
    mtag = f"{RED}OPERATOR{RST}{DIM}" if m == "operator" else "read-only"
    print(f"{DIM}Loyd · {args.claude_model} · fallback brain={args.model} @ {args.url} · "
          f"acts={where()} · mode={mtag} · "
          f"{len(TOOL_SCHEMAS)} tools ({len(WRITE_TOOLS)} write, confirm-gated) · {key}{RST}",
          file=sys.stderr)

    if args.question:
        q = " ".join(args.question)
        if q.startswith("/"):
            handle_slash(q, sess)          # e.g. loyd "/local why did the worker freeze?"
        else:
            sess.ask(q)
        return

    print(f"{DIM}Interactive. /help for commands, Ctrl-C or 'exit' to quit.{RST}", file=sys.stderr)
    while True:
        try:
            q = input(f"{YEL}loyd> {RST}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit", ":q"):
            break
        if q.startswith("/"):
            if handle_slash(q, sess) == "exit":
                break
            continue
        sess.ask(q)


if __name__ == "__main__":
    main()
