"""
Loyd Claude backend — escalate a hard turn to cloud Claude with the SAME tools.

When the local model (qwen-coder on the GPU) can't crack something, the `/claude`
bridge hands that one question to Claude via the Anthropic Messages API, running
the identical tool-use loop against the identical tools (run_shell, read_file,
get_container_status, write_file, restart_container). Same on_event hooks and
same confirm gate as LoydAgent, so the CLI printer works unchanged.

Stdlib-only on purpose (urllib, no `anthropic` SDK) — Loyd installs via
`curl | bash` with nothing but python3, and we keep it that way. Needs
ANTHROPIC_API_KEY in the environment.
"""
import json
import time
import urllib.error
import urllib.request


class ClaudeUnavailable(Exception):
    """The cloud path could not be used -- missing key or a failed request.
    Distinct from Claude answering badly, which is a normal return."""


from loyd_core import DEFAULT_SYSTEM

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def _to_anthropic_tools(schemas):
    """ollama {"type":"function","function":{name,description,parameters}}
       -> Anthropic {name, description, input_schema}."""
    out = []
    for s in schemas or []:
        f = s.get("function", s)
        out.append({
            "name": f["name"],
            "description": f.get("description", ""),
            "input_schema": f.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


class ClaudeAgent:
    """Mirrors LoydAgent's interface (.run + on_event/confirm/write_tools) but
    talks to the Anthropic Messages API and drives its native tool_use loop."""

    def __init__(self, model="claude-opus-4-8", api_key=None,
                 tool_schemas=None, execute=None,
                 system=DEFAULT_SYSTEM, max_iters=8, max_tokens=4096,
                 on_event=None, timeout=120, confirm=None, write_tools=None,
                 should_confirm=None):
        self.model = model
        self.api_key = api_key
        self.tools = _to_anthropic_tools(tool_schemas)
        self.execute = execute            # fn(name, args) -> str
        self.system = system
        self.max_iters = max_iters
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.on_event = on_event or (lambda kind, data: None)
        self.confirm = confirm            # fn(name, args) -> bool, operator's yes/no gate
        self.write_tools = set(write_tools or [])
        self.should_confirm = should_confirm or (lambda name, args: name in self.write_tools)

    def _messages(self, messages):
        body = json.dumps({
            "model": self.model, "max_tokens": self.max_tokens,
            "system": self.system, "tools": self.tools, "messages": messages,
        }).encode()
        req = urllib.request.Request(ANTHROPIC_URL, data=body, headers={
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        })
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def run(self, user_message):
        """Returns the final text. Raises ClaudeUnavailable when the cloud
        path could not be used at all -- no key, or the request never got a
        reply. That is out-of-band on purpose: the caller falls back to the
        local brain only while nothing has been emitted yet, and a bracketed
        string return is indistinguishable from a real answer."""
        """Run the tool-use loop for one user turn. Returns the final text answer."""
        if not self.api_key:
            raise ClaudeUnavailable("ANTHROPIC_API_KEY is not set")
        messages = [{"role": "user", "content": user_message}]
        for _step in range(self.max_iters):
            t0 = time.time()
            try:
                resp = self._messages(messages)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:400]
                raise ClaudeUnavailable(f"Claude API error {e.code}: {detail}")
            except Exception as e:
                raise ClaudeUnavailable(f"{type(e).__name__}: {e}")
            dt = time.time() - t0

            content = resp.get("content", []) or []
            if resp.get("stop_reason") != "tool_use":
                text = "".join(b.get("text", "") for b in content
                               if b.get("type") == "text").strip()
                self.on_event("final", {"text": text, "dt": dt})
                return text

            # Record Claude's turn verbatim (text + tool_use blocks), then answer each tool.
            messages.append({"role": "assistant", "content": content})
            results = []
            for b in content:
                if b.get("type") != "tool_use":
                    continue
                name, args, tuid = b.get("name"), (b.get("input") or {}), b.get("id")
                self.on_event("tool_call", {"name": name, "args": args,
                                            "source": "claude", "dt": dt})
                dt = 0.0  # attribute the request time to the first call only
                if self.confirm and self.should_confirm(name, args) and not self.confirm(name, args):
                    result = ("Operator DECLINED this action — do not retry it. "
                              "Continue without it, or give your final answer.")
                    self.on_event("tool_declined", {"name": name, "args": args})
                else:
                    result = self.execute(name, args) if self.execute else "[no executor]"
                    self.on_event("tool_result", {"name": name, "result": result, "repeat": False})
                results.append({"type": "tool_result", "tool_use_id": tuid, "content": result})
            messages.append({"role": "user", "content": results})
        return "[Claude hit the step limit without finishing — try narrowing the request]"
