"""
Loyd knowledge base — notes and runbooks on disk that Loyd can search, so it
still knows how this stack works when the internet (and Claude) is gone.

RETRIEVAL ONLY. Notes are handed to the model as context at question time;
they are never training data. Each note may carry a first line
`<!-- source: human | claude -->` so a future training export can skip
Claude-written text (Anthropic terms).

Searched: every *.md under the kb dirs, plus the repo's own docs.
  LOYD_KB_DIRS   extra directories, colon-separated (default: ./kb next to this file)
Stdlib-only ranked keyword search (BM25 over heading sections).
"""
import math
import os
import re
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DOCS = ["README.md", "MCP.md", "FINDINGS.md", "BACKLOG.md", "audits/README.md"]
_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]*[a-z0-9]|[a-z0-9]")
_STOP = set("the a an and or of to in on for is are be it this that with as at by from "
            "how what when where why do does i you we our your".split())


def kb_dirs():
    extra = [d for d in os.getenv("LOYD_KB_DIRS", "").split(":") if d.strip()]
    return [os.path.join(_HERE, "kb")] + extra


def _files():
    out = {}
    for d in kb_dirs():
        if os.path.isdir(d):
            for root, _, names in os.walk(d):
                for n in sorted(names):
                    if n.endswith(".md"):
                        p = os.path.join(root, n)
                        out[os.path.relpath(p, os.path.dirname(d))] = p
    for rel in REPO_DOCS:
        p = os.path.join(_HERE, rel)
        if os.path.isfile(p):
            out[rel] = p
    return out


def _tokens(text):
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def _sections(path):
    title, buf, out = "", [], []
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("#"):
                if "".join(buf).strip():
                    out.append((title, "".join(buf)))
                title, buf = line.strip("# \n"), []
            else:
                buf.append(line)
    if "".join(buf).strip():
        out.append((title, "".join(buf)))
    return out


def search(query, limit=5):
    """-> [(score, name, heading, text)] best sections first."""
    q = _tokens(query)
    if not q:
        return []
    docs = []
    for name, path in _files().items():
        for heading, body in _sections(path):
            toks = _tokens(heading + " " + heading + " " + body)      # headings count double
            docs.append((name, heading, body, Counter(toks), len(toks)))
    if not docs:
        return []
    n = len(docs)
    avg = sum(d[4] for d in docs) / n
    df = Counter(t for d in docs for t in set(d[3]))
    k1, b = 1.4, 0.75
    scored = []
    for name, heading, body, tf, ln in docs:
        s = 0.0
        for t in q:
            if tf[t]:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * ln / avg))
        if s > 0:
            scored.append((round(s, 2), name, heading, body))
    scored.sort(key=lambda x: -x[0])
    return scored[:limit]


def search_kb(query, limit=5):
    limit = max(1, min(int(limit or 5), 10))
    hits = search(query or "", limit)
    if not hits:
        return f"No notes match {query!r}. (Searched: {', '.join(sorted(_files())) or 'nothing'})"
    out = []
    for score, name, heading, body in hits:
        snippet = re.sub(r"\n{3,}", "\n\n", body.strip())
        if len(snippet) > 900:
            snippet = snippet[:900] + " …"
        out.append(f"## {name} › {heading or '(top)'}  (score {score})\n{snippet}")
    return "\n\n".join(out) + "\n\nRead a whole note with read_kb(name)."


def read_kb(name):
    files = _files()
    if name not in files:
        return f"ERROR: no note {name!r}. Notes: {', '.join(sorted(files))}"
    with open(files[name], errors="replace") as f:
        return f.read()


TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "search_kb",
        "description": "Search Loyd's notes and runbooks (how this stack is wired, past fixes, procedures). "
                       "Use BEFORE guessing about our own setup. Works offline.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "keywords, e.g. 'gpu1 vision worker port'"},
            "limit": {"type": "integer", "description": "max sections (default 5, max 10)"}},
            "required": ["query"]}}},
    {"type": "function", "function": {"name": "read_kb",
        "description": "Read one whole note from Loyd's knowledge base by the name search_kb shows.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "note name, e.g. kb/tokyo7.md"}}, "required": ["name"]}}},
]
IMPL = {
    "search_kb": lambda a, ro: search_kb(a.get("query", ""), a.get("limit", 5)),
    "read_kb": lambda a, ro: read_kb(a.get("name", "")),
}
