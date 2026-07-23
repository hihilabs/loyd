#!/usr/bin/env python3
"""
Loyd CLI — talk to your local dev/ops assistant.

  loyd "is the plex container running and how much disk is free?"   # one-shot
  loyd                                                              # interactive REPL

The local brain is swappable:  --model cogito:8b  or  OLLAMA_AI_MODEL=cogito:8b loyd ...

REPL slash commands:
  /claude <q>   escalate one hard question to cloud Claude (same tools; needs ANTHROPIC_API_KEY)
  /model [name] show or hot-swap the local brain
  /tools        list the tools Loyd can call
  /help         this help
  /exit         quit
(`loyd "/claude ..."` also works as a one-shot.)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loyd_core import LoydAgent           # noqa: E402
from claude_backend import ClaudeAgent    # noqa: E402
from tools import TOOL_SCHEMAS, execute_tool, WRITE_TOOLS, where   # noqa: E402

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
    return on_event


class Session:
    """Holds the local agent + a lazily-built Claude escalation agent so both
    share tools, executor, confirm gate, and the on_event printer."""

    def __init__(self, model, url, claude_model, max_iters):
        self.printer = make_printer()
        self.claude_model = claude_model
        self.max_iters = max_iters
        self.agent = LoydAgent(
            model=model, ollama_url=url,
            tool_schemas=TOOL_SCHEMAS, execute=execute_tool,
            max_iters=max_iters, on_event=self.printer,
            confirm=confirm_write, write_tools=WRITE_TOOLS)
        self._claude = None

    def claude(self):
        if self._claude is None:
            self._claude = ClaudeAgent(
                model=self.claude_model, api_key=os.getenv("ANTHROPIC_API_KEY"),
                tool_schemas=TOOL_SCHEMAS, execute=execute_tool,
                max_iters=self.max_iters, on_event=self.printer,
                confirm=confirm_write, write_tools=WRITE_TOOLS)
        return self._claude

    def ask_local(self, q):
        self.agent.run(q)

    def ask_claude(self, q):
        key = "set" if os.getenv("ANTHROPIC_API_KEY") else "MISSING"
        print(f"{DIM}↑ escalating to {self.claude_model} (key {key}){RST}", file=sys.stderr)
        self.claude().run(q)


def handle_slash(q, sess):
    """Dispatch a /command in the REPL. Returns 'exit' to quit, else None."""
    cmd, _, rest = q.partition(" ")
    cmd, rest = cmd.lower(), rest.strip()
    if cmd in ("/help", "/?"):
        print(f"{DIM}/claude <q>  escalate to cloud Claude (same tools)\n"
              f"/model [name] show or swap the local brain\n"
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
    elif cmd == "/claude":
        if rest:
            sess.ask_claude(rest)
        else:
            print(f"{DIM}usage: /claude <question>{RST}")
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

    key = "claude ready" if os.getenv("ANTHROPIC_API_KEY") else "/claude needs ANTHROPIC_API_KEY"
    print(f"{DIM}Loyd · brain={args.model} @ {args.url} · acts={where()} · "
          f"{len(TOOL_SCHEMAS)} tools ({len(WRITE_TOOLS)} write, confirm-gated) · {key}{RST}",
          file=sys.stderr)

    if args.question:
        q = " ".join(args.question)
        if q.startswith("/"):
            handle_slash(q, sess)          # e.g. loyd "/claude why did the worker freeze?"
        else:
            sess.ask_local(q)
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
        sess.ask_local(q)


if __name__ == "__main__":
    main()
