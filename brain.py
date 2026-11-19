"""
Brain router — which brain answers a turn.

  LOYD_BRAIN_POLICY=claude-first   (default) Claude answers; local brain only when
                                   the cloud is unavailable. Policy 2026-09-04: the
                                   GPU box is never primary until a human flips this.
  LOYD_BRAIN_POLICY=local-first    local brain answers; escalate to Claude when the
                                   local run errors or hits the step limit.
  LOYD_BRAIN_POLICY=auto           ask the local model ONE yes/no question —
                                   "routine maintenance a kit covers?" — and route
                                   routine work local-first, everything else
                                   claude-first. Any doubt (unparseable answer, local
                                   model down) routes to Claude.

Stdlib only; the CLI imports it.
"""
import os

POLICIES = ("claude-first", "local-first", "auto")
DEFAULT_POLICY = "claude-first"

ROUTINE, RESEARCH = "routine", "research"

CLASSIFY_PROMPT = (
    "You route requests for a dev/ops assistant. Answer with exactly one word: YES or NO.\n"
    "YES = routine maintenance a known tool covers: checking container/service status, "
    "disk/memory/GPU usage, reading logs, restarting a container, pruning images, "
    "simple network checks.\n"
    "NO = research or open-ended work: explaining why something happens, designing, "
    "comparing options, writing code, anything multi-step or unclear.\n\n"
    "Request: {q}\n\nRoutine? (YES/NO)"
)


def policy(env=None):
    """The active policy. Unknown values fall back to the default (and say so via
    the second return value) rather than silently picking something riskier."""
    raw = (os.environ if env is None else env).get("LOYD_BRAIN_POLICY", "")
    val = raw.strip().lower()
    if not val:
        return DEFAULT_POLICY, None
    if val in POLICIES:
        return val, None
    return DEFAULT_POLICY, f"unknown LOYD_BRAIN_POLICY={raw!r}, using {DEFAULT_POLICY}"


def parse_yes_no(text):
    """True for yes, False for no, None if the answer is neither."""
    t = (text or "").strip().lower().lstrip("*\"'` ")
    if t.startswith("yes"):
        return True
    if t.startswith("no"):
        return False
    return None


def classify(question, chat):
    """Route one question. `chat(messages) -> str` asks the local model with no
    tools. Returns (ROUTINE|RESEARCH, why). Failure or ambiguity -> RESEARCH."""
    try:
        reply = chat([{"role": "user", "content": CLASSIFY_PROMPT.format(q=question)}])
    except Exception as e:
        return RESEARCH, f"classifier unavailable ({type(e).__name__})"
    yn = parse_yes_no(reply)
    if yn is None:
        return RESEARCH, f"classifier unsure ({(reply or '').strip()[:40]!r})"
    return (ROUTINE, "classifier: routine") if yn else (RESEARCH, "classifier: research")


def plan(pol, question, chat):
    """Which path a turn takes under policy `pol`: ('claude-first'|'local-first', why)."""
    if pol == "local-first":
        return "local-first", "policy local-first"
    if pol == "auto":
        kind, why = classify(question, chat)
        return ("local-first" if kind == ROUTINE else "claude-first"), why
    return "claude-first", "policy claude-first"
