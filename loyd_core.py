"""
Loyd core — a small, model-agnostic tool-using agent loop.

Design goals:
  * Model is a swappable backend (Cogito 3B today -> 8B / cloud tomorrow, one arg).
  * Works whether the model emits ollama-native tool_calls (Cogito) OR a JSON blob
    in its content (qwen-coder) — _extract_call() normalizes both.
  * Small models whiff, so the loop is the safety net: bad/unknown tool calls come
    back as an error the model sees and corrects, bounded by max_iters.

Reusable: a CLI drives it today; the worker's ai_task handler can import LoydAgent
and call .run(task) tomorrow. No third-party deps (stdlib urllib only).
"""
import json
import re
import time
import urllib.request

def build_system(acts="local", mode="readonly"):
    """System prompt tailored to WHERE Loyd is acting (so it never guesses the host)
    and what it's allowed to do (readonly vs operator)."""
    loc = ("the local machine you are running on" if acts in ("local", "", None)
           else f"the remote host {acts} (reached over SSH)")
    if mode == "operator":
        cap = ("You are in OPERATOR mode: your shell tool can run real commands — installs, "
               "git, builds, file edits, service restarts. Destructive commands are shown to "
               "the operator for a yes/no first; a few catastrophic ones (disk wipe, "
               "system-root delete, power-off) are always refused. Use this power only for "
               "what the user actually asked; don't clean up or refactor beyond the request.")
    else:
        cap = ("You are in READ-ONLY mode: your shell tool refuses mutating commands. State "
               "changes go only through the confirm-gated write tools (write_file, "
               "restart_container, prune_images).")
    return (
        "You are Loyd, a local dev/ops assistant. You ACT by calling tools — you never just "
        "describe what you would do.\n"
        f"WHERE YOU ARE: every tool runs on {loc}. Do not assume any other machine; if the "
        "user asks about a host you are not acting on, say so plainly instead of guessing.\n"
        f"{cap}\n"
        "HARD RULES:\n"
        "1. If answering needs real system state, IMMEDIATELY call a tool — never write 'I "
        "will run...' or name a tool in prose; actually emit the tool call.\n"
        "2. Take ONE tool action at a time; you'll see its result, then decide the next. You "
        "drive the whole task — never ask the user to continue.\n"
        "3. Report findings in your final answer. NEVER write a file just to record an "
        "observation or a note-to-self — that is not a deliverable.\n"
        "4. Stop as soon as you can answer. If a container/file/host isn't found, say so "
        "plainly and answer with what you know — don't keep exploring.\n"
        "5. When you already have what you need, reply with a short, direct final answer and "
        "NO tool call.\n"
        "Be concise and practical."
    )


DEFAULT_SYSTEM = build_system()


def _first_json_obj(s):
    """Return the first brace-balanced {...} substring (string/escape aware), or None.
    Non-greedy: stops at the first complete object, so multiple tool-call JSON blobs
    in one message don't get merged into one unparseable blob."""
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def _extract_call(msg):
    """Normalize a tool call from native tool_calls OR a JSON object in content.
    Returns (name, args, source) with source in {native, text-json, none}."""
    calls = msg.get("tool_calls") or []
    if calls:
        f = calls[0].get("function", {})
        args = f.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        return f.get("name"), (args or {}), "native"
    blob = _first_json_obj((msg.get("content") or "").strip())
    if blob:
        try:
            obj = json.loads(blob)
            fn = obj.get("name") or (obj.get("function") or {}).get("name")
            args = obj.get("arguments") or (obj.get("function") or {}).get("arguments") or {}
            if fn:
                return fn, (args if isinstance(args, dict) else {}), "text-json"
        except Exception:
            pass
    return None, None, "none"


class LoydAgent:
    def __init__(self, model="qwen2.5-coder:7b",
                 ollama_url="http://localhost:11434",
                 tool_schemas=None, execute=None,
                 system=DEFAULT_SYSTEM, max_iters=8, num_ctx=8192,
                 on_event=None, timeout=180,
                 confirm=None, write_tools=None, should_confirm=None):
        self.model = model
        self.url = ollama_url.rstrip("/") + "/api/chat"
        self.tool_schemas = tool_schemas or []
        self.execute = execute            # fn(name, args) -> str
        self.system = system
        self.max_iters = max_iters
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.on_event = on_event or (lambda kind, data: None)  # (kind, data) hook for UI
        self.confirm = confirm            # fn(name, args) -> bool, operator's yes/no gate
        self.write_tools = set(write_tools or [])
        # Predicate: does THIS call need confirmation? Defaults to "is a write tool";
        # callers can widen it (e.g. also confirm destructive run_shell in operator mode).
        self.should_confirm = should_confirm or (lambda name, args: name in self.write_tools)
        # How the last run() ended: "final" | "empty" | "error" | "step_limit" | "no_executor".
        # The brain router escalates to Claude on anything but "final".
        self.last_outcome = None

    def _chat(self, messages, tools=None):
        body = json.dumps({
            "model": self.model, "stream": False,
            "tools": self.tool_schemas if tools is None else tools,
            "options": {"num_ctx": self.num_ctx}, "messages": messages,
        }).encode()
        req = urllib.request.Request(self.url, data=body,
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())
        return data.get("message", {}), time.time() - t0

    def run(self, user_message):
        """Run the agent loop for one user turn. Returns the final text answer."""
        messages = [{"role": "system", "content": self.system},
                    {"role": "user", "content": user_message}]
        seen = {}   # key -> result, so identical repeat calls are cached, not re-run
        self.last_outcome = None
        for step in range(self.max_iters):
            # On the last allowed step, force a text answer (no tools) so the model
            # concludes instead of spiralling through more exploratory calls.
            last = step == self.max_iters - 1
            try:
                if last:
                    messages.append({"role": "user", "content":
                                     "Stop calling tools. Give your best final answer now "
                                     "using what you've already found."})
                    msg, dt = self._chat(messages, tools=[])
                else:
                    msg, dt = self._chat(messages)
            except Exception as e:
                # Emit, don't just return: callers print via on_event, so a
                # bare return here exits silently -- which is how a dead local
                # brain looked like a successful empty answer.
                err = f"[Loyd error talking to the model: {type(e).__name__}: {e}]"
                self.on_event("error", {"text": err})
                self.last_outcome = "error"
                return err
            name, args, source = (None, None, "none") if last else _extract_call(msg)

            if name is None:
                answer = (msg.get("content") or "").strip()
                self.on_event("final", {"text": answer, "dt": dt})
                # A conclusion forced by the last-step guard (after tool steps) is
                # the step limit in disguise; an empty answer is no answer.
                self.last_outcome = ("step_limit" if last and step > 0
                                     else "empty" if not answer else "final")
                return answer

            self.on_event("tool_call", {"name": name, "args": args, "source": source, "dt": dt})
            if not self.execute:
                self.last_outcome = "no_executor"
                return "[Loyd has no tool executor configured]"

            if self.confirm and self.should_confirm(name, args) and not self.confirm(name, args):
                repeat = False
                result = ("Operator DECLINED this action — do not retry it. "
                          "Continue without it, or give your final answer.")
                self.on_event("tool_declined", {"name": name, "args": args})
            else:
                key = name + "|" + json.dumps(args, sort_keys=True, default=str)
                repeat = key in seen
                result = seen[key] if repeat else self.execute(name, args)
                seen[key] = result
                self.on_event("tool_result", {"name": name, "result": result, "repeat": repeat})

            # Record the model's action + the observation, then continue the loop.
            messages.append({"role": "assistant",
                             "content": msg.get("content", "") or "",
                             **({"tool_calls": msg["tool_calls"]} if source == "native" else {})})
            obs = result
            if repeat:
                obs += ("\n\n[You have ALREADY run this exact call — do not call it again. "
                        "Use the results you now have and give your final answer.]")
            if source == "native":
                messages.append({"role": "tool", "name": name, "content": obs})
            else:
                messages.append({"role": "user",
                                 "content": f"Observation from {name}:\n{obs}\n\n"
                                            f"Answer now if you can, or call a DIFFERENT tool."})
        self.last_outcome = "step_limit"
        return "[Loyd hit the step limit without finishing — try narrowing the request]"
