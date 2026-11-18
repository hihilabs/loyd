#!/usr/bin/env python3
"""
Export reviewed local-model traces as chat-format JSONL for fine-tuning a
Loyd LoRA. Only records that are ALL of: brain == "local", trainable, answered,
and rated good by a person (/good in the CLI). Claude traces are refused here a
second time by their brain field, so an edited trace file still can't leak
Claude output into training data (Anthropic terms).

  python3 scripts/export_training.py <trace_dir> <out.jsonl> [--system "..."]
"""
import argparse
import glob
import json
import os
import sys


def load(trace_dir):
    traces, verdicts = {}, {}
    for path in sorted(glob.glob(os.path.join(trace_dir, "traces-*.jsonl"))):
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if "review" in r:
                    verdicts[r["review"]] = r.get("verdict")      # latest verdict wins
                elif "id" in r:
                    traces[r["id"]] = r
    return traces, verdicts


def eligible(r, verdicts):
    return (r.get("brain") == "local" and r.get("trainable") is True
            and r.get("outcome") == "answered" and verdicts.get(r["id"]) == "good")


def to_chat(r, system=None):
    msgs = [{"role": "system", "content": system}] if system else []
    msgs.append({"role": "user", "content": r["task"]})
    for s in r.get("steps", []):
        if s.get("declined"):
            continue
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"type": "function",
                                     "function": {"name": s["tool"], "arguments": s.get("args", {})}}]})
        msgs.append({"role": "tool", "name": s["tool"], "content": s.get("result", "")})
    msgs.append({"role": "assistant", "content": r.get("final") or ""})
    return {"messages": msgs, "source_trace": r["id"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("trace_dir")
    ap.add_argument("out")
    ap.add_argument("--system", default=None, help="system prompt to prepend")
    a = ap.parse_args(argv)
    traces, verdicts = load(a.trace_dir)
    keep = [r for r in traces.values() if eligible(r, verdicts)]
    claude = sum(r.get("brain") == "claude" for r in traces.values())
    with open(a.out, "w") as f:
        for r in keep:
            f.write(json.dumps(to_chat(r, a.system)) + "\n")
    print(f"exported {len(keep)} of {len(traces)} traces "
          f"({claude} Claude traces excluded by rule; the rest unreviewed, rated bad, or failed)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
