<!-- source: claude -->
# DSPy for Loyd / Tokyo7 (idea, not implemented — written 2026-11-20)

Handoff note: Andrew liked the idea of using DSPy and wants any session (VS Code Claude,
Fable, Loyd itself) to pick it up from here. Nothing below is built yet. Backlog item:
"DSPy prototype: auto-router classifier" in `BACKLOG.md`.

## What DSPy is
A framework for *programming* LLM pipelines instead of hand-writing prompts:
- **Signatures** declare a task's inputs and outputs (e.g. `question -> route: bool`).
- **Modules** compose them (`Predict`, `ChainOfThought`, `ReAct`).
- **Optimizers** (`BootstrapFewShot`, `MIPROv2`) tune instructions and few-shot examples
  against a metric on a labelled set; `Evaluate` scores a program on that set.
- It can drive local models through Ollama, so no metered API is needed.

## The setup it would plug into
- **Loyd** (`hihilabs/loyd`): Claude answers by default, a local Ollama model on Tokyo7 is
  the fallback. `LOYD_BRAIN_POLICY=claude-first|local-first|auto` (`brain.py`); in `auto`,
  the local model answers one YES/NO question to decide which brain takes the turn.
- **Tokyo7 GPUs**: two RTX 5060 Ti (16 GB each). `ollama-gpu0` (:11434) holds vision +
  embeddings; `ollama-gpu1` (:11435) is shared between vision and Loyd's text models through
  GPU modes `online` / `offline` (`gpumode.py`, `scripts/gpu_mode.sh`, CLI `/gpu`).
  See `kb/tokyo7.md`.
- **Training data already exists**: `loydtrace.py` records traces (`LOYD_TRACE_DIR`), the CLI's
  `/good` and `/bad` rate them, `scripts/export_training.py` exports local, rated-good
  traces only (Claude traces are refused). The backlog plans a 30–50 task eval set and a
  LoRA experiment.
- **Vision pipeline (camagent)** reads HVAC data plates; accuracy is measured against 154
  human-labelled photos (`kb/vision-accuracy.md`, `scripts/vision_eval.py`). Refrigerant:
  36% right, about 86% with Qwen plus snapping to a list of valid values.

## Where DSPy fits best (in order)
1. **Auto-router classifier**: a small signature with an easy metric (did it route to the
   right brain?), labels from rated traces. Cheapest first win; compare with today's
   hand-written YES/NO prompt in `brain.py`.
2. **Small-model tool calling**: cut the local model's malformed or repeated tool calls
   (rough edge in `FINDINGS.md`) by optimizing its instructions and few-shot examples
   against recorded traces.
3. **Vision plate extraction**: optimize the vision model's extraction prompt against the
   golden labels, exact match per field as the metric. The golden labels need fixing
   first (a "Needs a human" item in `BACKLOG.md`).
4. **Eval harness**: `dspy.Evaluate` could be the 30–50 task eval set the LoRA plan needs,
   so prompt optimization and LoRA are measured the same way.

## Constraints
- **Allowances only**: optimization runs on the local GPUs; a metered API only behind the
  gate a person arms.
- **The CLI stays stdlib-only**: DSPy lives in `scripts/` or an optional requirements file,
  never in `cli.py`'s import path. Never create a `tools/` directory (it shadows `tools.py`).
- **GPU time**: optimization runs should go in `offline` GPU mode or at night, never by
  pausing vision.

## Suggested first step
`scripts/dspy_router.py`: a DSPy version of the `auto` router classifier, evaluated on
exported rated traces, reporting accuracy next to the current prompt. Optional deps in a
separate `requirements-dspy.txt`; tests must not need DSPy installed.
