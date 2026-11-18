"""
Loyd kits — declarative tool bundles.

A kit is `kits/<name>.json`: a few named, vetted commands with typed, validated
parameters. Each command becomes a tool named `<kit>_<command>`. Kits let Loyd
drive a project (run its tests, read its logs, check migrations) with fixed,
reviewed command lines instead of free-form shell — safer for every brain, and
far easier for a small local model to use correctly.

  LOYD_KITS       comma-separated kits to load (default: none), e.g. "ops,hihilabs"
  LOYD_KITS_DIR   where kit files live (default: ./kits next to this file)

Kit file:
  {"name": "hihilabs", "description": "...",
   "commands": [
     {"name": "run_tests", "description": "...", "mode": "read" | "write",
      "verified": true, "timeout": 300,
      "params": {"app": {"type": "string", "pattern": "^[a-z_]+$", "required": false,
                         "default": "", "description": "..."}},
      "run": "docker exec web python manage.py test {app}"}]}

Rules (enforced at load — a bad kit fails loudly, it never half-loads):
  * every string param needs a `pattern` (anchored regex); integers take min/max;
    enums take `values`. Values are validated, then shell-quoted into `run`.
  * `{param}` placeholders must all be declared; other braces (docker's
    `{{.Names}}`, curl's `%{http_code}`, shell `${var:-x}`) are left alone.
  * mode "write" commands join WRITE_TOOLS: operator-only, confirm-gated, audited.
  * "verified": false commands are NOT offered as tools until a human checks them
    against the real system and flips the flag; calling one is refused.
  * timeout defaults to 30 s, capped at 600 s.
"""
import json
import os
import re
import shlex

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PLACEHOLDER = re.compile(r"(?<!%)\{([a-z_][a-z0-9_]*)\}")   # %{...} = curl -w, left alone
MAX_TIMEOUT = 600


class KitError(ValueError):
    pass


def _check_param(kit, cmd, pname, spec):
    where = f"{kit}.{cmd}.{pname}"
    if not _NAME.match(pname):
        raise KitError(f"{where}: bad param name")
    t = spec.get("type")
    if t == "string":
        pat = spec.get("pattern")
        if not pat or not pat.startswith("^") or not pat.endswith("$"):
            raise KitError(f"{where}: string params need an anchored 'pattern' (^...$)")
        re.compile(pat)
    elif t == "integer":
        for k in ("min", "max"):
            if not isinstance(spec.get(k), int):
                raise KitError(f"{where}: integer params need int 'min' and 'max'")
    elif t == "enum":
        vals = spec.get("values")
        if not vals or not all(isinstance(v, str) for v in vals):
            raise KitError(f"{where}: enum params need a list of string 'values'")
    else:
        raise KitError(f"{where}: type must be string, integer or enum")
    if "default" in spec:
        _coerce(where, spec, spec["default"], check_only=True)


def _coerce(where, spec, value, check_only=False):
    t = spec["type"]
    if t == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise KitError(f"{where}: expected an integer")
        try:
            v = int(value)
        except ValueError:
            raise KitError(f"{where}: expected an integer")
        if not spec["min"] <= v <= spec["max"]:
            raise KitError(f"{where}: must be between {spec['min']} and {spec['max']}")
        return str(v)
    if not isinstance(value, str):
        raise KitError(f"{where}: expected a string")
    if t == "enum":
        if value not in spec["values"]:
            raise KitError(f"{where}: must be one of {', '.join(spec['values'])}")
        return value
    if value == "" and not spec.get("required", True):
        return value
    if not re.fullmatch(spec["pattern"], value):
        raise KitError(f"{where}: {value!r} doesn't match {spec['pattern']}")
    return value


def load_kit(path, reserved=()):
    with open(path) as f:
        kit = json.load(f)
    name = kit.get("name", "")
    if not _NAME.match(name):
        raise KitError(f"{path}: kit 'name' must match {_NAME.pattern}")
    cmds = kit.get("commands")
    if not isinstance(cmds, list) or not cmds:
        raise KitError(f"{name}: 'commands' must be a non-empty list")
    seen = set()
    for c in cmds:
        cname = c.get("name", "")
        if not _NAME.match(cname) or cname in seen:
            raise KitError(f"{name}: bad or duplicate command name {cname!r}")
        seen.add(cname)
        if f"{name}_{cname}" in reserved:
            raise KitError(f"{name}.{cname}: tool name collides with a core tool")
        if c.get("mode") not in ("read", "write"):
            raise KitError(f"{name}.{cname}: mode must be 'read' or 'write'")
        if not isinstance(c.get("run"), str) or not c["run"].strip():
            raise KitError(f"{name}.{cname}: 'run' command line required")
        if not c.get("description"):
            raise KitError(f"{name}.{cname}: 'description' required")
        t = c.get("timeout", 30)
        if not isinstance(t, int) or not 1 <= t <= MAX_TIMEOUT:
            raise KitError(f"{name}.{cname}: timeout must be 1..{MAX_TIMEOUT} seconds")
        params = c.get("params") or {}
        for pname, spec in params.items():
            _check_param(name, cname, pname, spec)
        for ph in _PLACEHOLDER.findall(c["run"]):
            if ph not in params:
                raise KitError(f"{name}.{cname}: placeholder {{{ph}}} is not a declared param")
    return kit


def render(kit_name, cmd, args):
    """Validate args against the command's params and build the command line."""
    params = cmd.get("params") or {}
    args = args or {}
    unknown = set(args) - set(params)
    if unknown:
        raise KitError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    values = {}
    for pname, spec in params.items():
        where = f"{kit_name}.{cmd['name']}.{pname}"
        if pname in args:
            values[pname] = _coerce(where, spec, args[pname])
        elif "default" in spec:
            values[pname] = _coerce(where, spec, spec["default"])
        elif spec.get("required", True):
            raise KitError(f"{where}: required")
        else:
            values[pname] = ""
    return _PLACEHOLDER.sub(
        lambda m: shlex.quote(values[m.group(1)]) if values[m.group(1)] != "" else "",
        cmd["run"])


def schema_for(kit_name, cmd):
    props, required = {}, []
    for pname, spec in (cmd.get("params") or {}).items():
        p = {"description": spec.get("description", "")}
        if spec["type"] == "integer":
            p.update(type="integer", minimum=spec["min"], maximum=spec["max"])
        elif spec["type"] == "enum":
            p.update(type="string", enum=list(spec["values"]))
        else:
            p.update(type="string")
        props[pname] = p
        if spec.get("required", True) and "default" not in spec:
            required.append(pname)
    tag = "REQUIRES operator confirmation. " if cmd["mode"] == "write" else ""
    return {"type": "function", "function": {
        "name": f"{kit_name}_{cmd['name']}",
        "description": f"[{kit_name} kit] {tag}{cmd['description']}",
        "parameters": {"type": "object", "properties": props, "required": required}}}


def kits_dir():
    return os.getenv("LOYD_KITS_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "kits")


def selected():
    return [k.strip() for k in os.getenv("LOYD_KITS", "").split(",") if k.strip()]


def install(tool_schemas, impl, write_tools, runner, names=None, directory=None):
    """Load the selected kits and register their verified commands as tools.
    runner(command_line, timeout) -> str executes on the host Loyd acts on.
    Returns {tool_name: (kit, cmd)} for every command, verified or not."""
    reserved = {s["function"]["name"] for s in tool_schemas}
    registry = {}
    for kname in (selected() if names is None else names):
        kit = load_kit(os.path.join(directory or kits_dir(), f"{kname}.json"), reserved)
        for cmd in kit["commands"]:
            tname = f"{kit['name']}_{cmd['name']}"
            registry[tname] = (kit, cmd)
            if not cmd.get("verified", False):
                impl[tname] = (lambda a, ro, k=kit["name"]:
                               f"REFUSED: {k} kit command not verified yet — a human must "
                               f"check it against the real system and set \"verified\": true.")
                continue
            tool_schemas.append(schema_for(kit["name"], cmd))
            if cmd["mode"] == "write":
                write_tools.add(tname)

            def run(a, ro, kit=kit, cmd=cmd):
                try:
                    line = render(kit["name"], cmd, a)
                except KitError as e:
                    return f"ERROR: {e}"
                return runner(line, cmd.get("timeout", 30))
            impl[tname] = run
        reserved |= set(registry)
    return registry
