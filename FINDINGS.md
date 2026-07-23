# Loyd — build findings & decisions (2026-07-23)

Durable record of how Loyd v0.1 was designed and what the model testing showed, so
we don't re-derive it. Broader Tokyo7 session findings (docker/shfs recovery,
CompanyCam-429 worker fix) live in Claude memory + the `fix/companycam-429-worker-freeze`
PR, not here.

## Goal
A **local, self-hosted dev/ops assistant** ("Loyd") that helps build & maintain the
stack across **Tokyo7 (Unraid)** and the **hihilabs VPS**. Runs on a local ollama model
so it's private/free and leaves the GPUs for cam. Principle (operator's words):
*"more like tools than Sonnet"* — a small model can't plan open-endedly, so compensate
with **strong tools + tight scaffolding**; the model just routes.

## Hardware constraint that drove model choice
2× RTX 5060 Ti (16GB each, Blackwell). While the cam workers run they hold ~10GB
(torch/EasyOCR) + ~9GB (minicpm-v) across the two cards → only ~5–6GB headroom.
So Loyd must fit that headroom to coexist with cam. **north-mini-code-1.0 (18GB) is
disqualified** — too big for one 16GB card *and* can't coexist with cam (it timed out
loading; spills to CPU).

## Tool-calling test results (ollama /api/chat with `tools`)
Suite: 4 base + 2 hard ("infer intent from a symptom", "vague→right tool").

| Model | Size (Q4) | Decisions | Tool format | Speed | Notes |
|---|---|---|---|---|---|
| **qwen2.5-coder:7b** | ~4.7GB (8.8GB@32k ctx) | **6/6, 2/2 hard** | text-JSON | 0.4–0.9s | Most reliable. Needs the harness's JSON parser. |
| **Cogito 3B** | 2.3GB | 5/6, 2/2 hard (isolated) | **native tool_calls** | 0.4s (one 81s whiff) | Tiny + native, BUT **narrates plans instead of acting on compound/multi-step tasks** — weak orchestrator in the real loop. |
| llama3:8b-instruct | 4.9GB | 0/6 | — | — | ollama template has **no tools support** (HTTP 400). Out. |

**In the real harness** (compound question, chained tools): qwen2.5-coder:7b executed
the tools, chained them, and concluded with a correct answer. Cogito 3B wrote out the
plan as prose ("Call `get_container_status`…") and asked to continue — did not act.

## Decisions
- **v0.1 brain: `qwen2.5-coder:7b`** (default). Reliable orchestration > small footprint.
  Already pulled; coexists with cam (~6GB @ 8k ctx).
- **Next to A/B: Cogito 8B** (5.4GB, native tool_calls) — likely the sweet spot between
  3B's footprint and 7B's reliability. Cogito 3B kept as a curiosity, not the driver.
- **Model is a swappable backend** (`OLLAMA_AI_MODEL` / `--model`): 3B → 8B → cloud Claude
  escalation, same harness/tools.
- **Topology option (open):** dedicate gpu1 to Loyd so it can run a 14–16B (DeepSeek-Coder-V2-Lite
  15.7B MoE was the best coder on the fitmyllm 5060 Ti list) — at the cost of cam losing a card.

## Harness bugs found & fixed during the build
- Greedy `{.*}` regex merged two tool-call JSON blobs → unparseable. Fixed with a
  brace-balanced **first-object** extractor (`_first_json_obj`).
- Small model **spiralled** (kept exploring, never concluded). Fixed with a conclude-guard:
  the last step forces a tool-less text answer.

## Known rough edges (next iteration)
- Occasional redundant double tool-call before concluding.
- Huge tool outputs (`docker ps -a`) flood a small model's context — needs smarter trimming.
- `run_shell` read-only guard is best-effort (denylist), not a sandbox.
