#!/usr/bin/env python3
"""
Loyd CLI — talk to your local dev/ops assistant.

  loyd "is the plex container running and how much disk is free?"   # one-shot
  loyd                                                              # interactive REPL

Model is swappable:  --model cogito:8b   or   OLLAMA_AI_MODEL=cogito:8b loyd ...
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loyd_core import LoydAgent           # noqa: E402
from tools import TOOL_SCHEMAS, execute_tool, WRITE_TOOLS   # noqa: E402

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


def main():
    ap = argparse.ArgumentParser(description="Loyd — local dev/ops assistant")
    ap.add_argument("question", nargs="*", help="one-shot question (omit for interactive)")
    ap.add_argument("--model", default=os.getenv("OLLAMA_AI_MODEL", "qwen2.5-coder:7b"))
    ap.add_argument("--url", default=os.getenv("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--max-iters", type=int, default=8)
    args = ap.parse_args()

    agent = LoydAgent(model=args.model, ollama_url=args.url,
                      tool_schemas=TOOL_SCHEMAS, execute=execute_tool,
                      max_iters=args.max_iters, on_event=make_printer(),
                      confirm=confirm_write, write_tools=WRITE_TOOLS)

    print(f"{DIM}Loyd · {args.model} · {len(TOOL_SCHEMAS)} tools "
          f"({len(WRITE_TOOLS)} write, confirm-gated){RST}", file=sys.stderr)

    if args.question:
        agent.run(" ".join(args.question))
        return

    print(f"{DIM}Interactive. Ctrl-C or 'exit' to quit.{RST}", file=sys.stderr)
    while True:
        try:
            q = input(f"{YEL}loyd> {RST}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if q.lower() in ("exit", "quit", ":q"):
            break
        if q:
            agent.run(q)


if __name__ == "__main__":
    main()
