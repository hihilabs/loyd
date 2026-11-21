"""judge: Loyd's typed judgments, answered with real probabilities on our own GPUs.

The idea (2026-11-21) is TypeSafe's Jev, done in-house: instead of asking a model
for prose and parsing it, ask one small typed question and read the answer's
probability straight off the model's next-token distribution.

    noul    "is this statement true?"            -> P(true)
    choice  "which of these options?"            -> {option: P}
    score   "which of these levels?"             -> {level: P} (+ expected level)

Judgments come from the model; decisions stay in calling code (the TypeSafe
rule). Every answer is logged, and when the truth turns up later (a golden
label, a person's correction, an audit agreement) it is recorded with
`outcome()`. `calibrate()` refits, per question, a small logistic map from the
raw probability to the observed rate, so over time 0.8 comes to mean "right
80% of the time" on *our* data. That log is also the training set if we later
fine-tune an adapter for a question.

Models (Ollama, token logprobs): text goes to Loyd's card, images to the vision
card. Override with LOYD_JUDGE_TEXT_URL/MODEL and LOYD_JUDGE_VISION_URL/MODEL.
Log: LOYD_JUDGE_DB (default ~/.loyd/judge.db). Stdlib only.
"""
import base64
import hashlib
import json
import math
import os
import sqlite3
import string
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TEXT_URL = os.getenv("LOYD_JUDGE_TEXT_URL", os.getenv("OLLAMA_URL", "http://localhost:11435"))
TEXT_MODEL = os.getenv("LOYD_JUDGE_TEXT_MODEL", "loyd-agent:latest")
VISION_URL = os.getenv("LOYD_JUDGE_VISION_URL", "http://localhost:11434")
VISION_MODEL = os.getenv("LOYD_JUDGE_VISION_MODEL", "minicpm-v:latest")
DB_PATH = os.getenv("LOYD_JUDGE_DB", str(Path.home() / ".loyd" / "judge.db"))
TOP_LOGPROBS = 20
TIMEOUT = int(os.getenv("LOYD_JUDGE_TIMEOUT", "180"))


# ── answers ───────────────────────────────────────────────────────────────────

@dataclass
class Answer:
    question: str                 # question id from judge_questions
    version: int
    kind: str                     # noul | choice | score
    raw: dict                     # label -> raw probability (renormalised over the labels)
    p: dict                       # label -> calibrated probability (== raw until calibrated)
    model: str
    mass: float                   # share of next-token mass that landed on a valid label
    judgment_id: int = None
    calibrated: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def top(self):
        return max(self.p, key=self.p.get)

    @property
    def yes(self):
        """Noul shortcut: calibrated P(true)."""
        return self.p.get("yes")


class JudgeError(RuntimeError):
    """The model could not be asked, or put almost no mass on any valid label.
    Callers must treat this as 'no answer', never as 'no'."""


# ── the model call ────────────────────────────────────────────────────────────

def _post(url, body):
    req = urllib.request.Request(f"{url}/api/generate", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _image_b64(img):
    if isinstance(img, (bytes, bytearray)):
        return base64.b64encode(img).decode()
    return base64.b64encode(Path(img).read_bytes()).decode()


def next_token_dist(prompt, images=None, url=None, model=None, _post=_post):
    """{token: prob} over the top next tokens, and the model used."""
    vision = bool(images)
    url = url or (VISION_URL if vision else TEXT_URL)
    model = model or (VISION_MODEL if vision else TEXT_MODEL)
    body = {"model": model, "prompt": prompt, "stream": False, "think": False,
            "logprobs": True, "top_logprobs": TOP_LOGPROBS,
            "options": {"num_predict": 1, "temperature": 0}}
    if vision:
        body["images"] = [_image_b64(i) for i in images]
    d = _post(url, body)
    lp = (d.get("logprobs") or [{}])[0].get("top_logprobs") or []
    if not lp:
        raise JudgeError(f"{model} returned no logprobs (ollama too old, or model refused)")
    return {t["token"]: math.exp(t["logprob"]) for t in lp}, model


def _collect(dist, labels):
    """Sum token probability onto labels ('yes' gets 'Yes', ' yes', ...)."""
    out = {lab: 0.0 for lab in labels}
    for tok, p in dist.items():
        t = tok.strip().lower().strip(".:)*\"'`")
        if t in out:
            out[t] += p
    return out


# ── the three question kinds ──────────────────────────────────────────────────

def _state_text(state):
    if state is None:
        return ""
    if isinstance(state, str):
        return state
    return json.dumps(state, indent=1, ensure_ascii=False, default=str)


def _ask(qid, kind, prompt, labels, state, images, url, model, min_mass, _post):
    dist, used = next_token_dist(prompt, images, url, model, _post=_post)
    got = _collect(dist, labels)
    mass = sum(got.values())
    if mass < min_mass:
        raise JudgeError(f"{qid}: only {mass:.2f} of the next-token mass was a valid answer")
    raw = {k: v / mass for k, v in got.items()}
    q = QUESTIONS[qid]
    ans = Answer(qid, q["version"], kind, raw, dict(raw), used, round(mass, 4))
    _apply_calibration(ans)
    ans.judgment_id = _log(ans, state, images)
    return ans


def noul(qid, state=None, images=None, url=None, model=None, min_mass=0.5, _post=_post):
    """P(statement true) for a registered noul question."""
    q = QUESTIONS[qid]
    assert q["kind"] == "noul", qid
    prompt = (f"{q['statement']}\n"
              f"True when: {q['true_when']}\n"
              f"False when: {q['false_when']}\n"
              + (f"\nState:\n{_state_text(state)}\n" if state is not None else "")
              + "\nIs the statement true? Reply with exactly one word: yes or no.\nAnswer:")
    return _ask(qid, "noul", prompt, ["yes", "no"], state, images, url, model, min_mass, _post)


def choice(qid, state=None, images=None, url=None, model=None, min_mass=0.5, _post=_post):
    """{option name: P} for a registered choice question. Options are lettered so
    each answer is one token; names and descriptions are both shown."""
    q = QUESTIONS[qid]
    assert q["kind"] == "choice", qid
    names = list(q["options"])
    letters = string.ascii_lowercase[:len(names)]
    lines = "\n".join(f"{letters[i].upper()}) {n}: {q['options'][n]}" for i, n in enumerate(names))
    prompt = (f"{q['question']}\n\nOptions:\n{lines}\n"
              + (f"\nState:\n{_state_text(state)}\n" if state is not None else "")
              + f"\nReply with exactly one letter ({', '.join(l.upper() for l in letters)}).\nAnswer:")
    a = _ask(qid, "choice", prompt, list(letters), state, images, url, model, min_mass, _post)
    remap = dict(zip(letters, names))
    a.raw = {remap[k]: v for k, v in a.raw.items()}
    a.p = {remap[k]: v for k, v in a.p.items()}
    return a


def score(qid, state=None, images=None, url=None, model=None, min_mass=0.5, _post=_post):
    """{level: P} for a registered score question (levels numbered from 1), plus
    the expected level in .extra['expected']."""
    q = QUESTIONS[qid]
    assert q["kind"] == "score", qid
    levels = q["levels"]
    lines = "\n".join(f"{i + 1}: {desc}" for i, desc in enumerate(levels))
    prompt = (f"{q['question']}\n\nLevels:\n{lines}\n"
              + (f"\nState:\n{_state_text(state)}\n" if state is not None else "")
              + f"\nReply with exactly one number from 1 to {len(levels)}.\nAnswer:")
    a = _ask(qid, "score", prompt, [str(i + 1) for i in range(len(levels))], state, images,
             url, model, min_mass, _post)
    a.extra["expected"] = sum(int(k) * v for k, v in a.p.items())
    return a


# ── the log: judgments and the truth that turns up later ─────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS judgments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    model       TEXT NOT NULL,
    state_key   TEXT NOT NULL,       -- hash of state + images: the same input judged again shares it
    raw         TEXT NOT NULL,       -- JSON label -> raw p
    calibrated  TEXT,                -- JSON label -> calibrated p, when a fit existed
    mass        REAL,
    asked_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS judgments_q ON judgments(question, version, model);
CREATE TABLE IF NOT EXISTS outcomes (
    question    TEXT NOT NULL,
    state_key   TEXT NOT NULL,
    label       TEXT NOT NULL,       -- the true answer: 'yes'/'no', an option name, a level
    source      TEXT,                -- golden | person | audit | ...
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (question, state_key)
);
CREATE TABLE IF NOT EXISTS calibrations (
    question    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    model       TEXT NOT NULL,
    a           REAL NOT NULL,       -- calibrated logit = a * raw logit + b   (Platt)
    b           REAL NOT NULL,
    n           INTEGER NOT NULL,
    fitted_at   TEXT NOT NULL,
    report      TEXT,
    PRIMARY KEY (question, version, model)
);
"""


def _db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.executescript(SCHEMA)
    return c


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def state_key(state=None, images=None):
    h = hashlib.sha256(_state_text(state).encode())
    for i in images or []:
        h.update(i if isinstance(i, (bytes, bytearray)) else Path(i).read_bytes())
    return h.hexdigest()[:24]


def _log(ans, state, images):
    try:
        with _db() as c:
            cur = c.execute(
                "INSERT INTO judgments (question, version, kind, model, state_key, raw, calibrated,"
                " mass, asked_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ans.question, ans.version, ans.kind, ans.model, state_key(state, images),
                 json.dumps(ans.raw), json.dumps(ans.p) if ans.calibrated else None, ans.mass, _now()))
            return cur.lastrowid
    except sqlite3.Error:
        return None                 # logging must never cost the caller its answer


def outcome(question, label, state=None, images=None, source=None, key=None):
    """Record the true answer for an input. Feeds calibrate() and, later, training."""
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?)",
                  (question, key or state_key(state, images), str(label), source, _now()))


# ── learning: per-question Platt scaling on our own outcomes ─────────────────

def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sigmoid(z):
    return 1 / (1 + math.exp(-z)) if z > -60 else 0.0


def fit_platt(pairs, iters=200, l2=1e-3):
    """pairs: [(raw_p, y)] -> (a, b) minimising log loss of sigmoid(a*logit(p)+b).
    Newton's method on two parameters; a light L2 keeps tiny samples sane."""
    a, b = 1.0, 0.0
    xs = [(_logit(p), y) for p, y in pairs]
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in xs:
            q = _sigmoid(a * x + b)
            ga += (q - y) * x; gb += (q - y)
            w = q * (1 - q)
            haa += w * x * x; hab += w * x; hbb += w
        ga += l2 * (a - 1); haa += l2
        hbb += 1e-9
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) < 1e-7 and abs(db) < 1e-7:
            break
    return a, b


def _ece(pairs, bins=10):
    """Expected calibration error: how far stated confidence is from observed rate."""
    tot, err = len(pairs), 0.0
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        b = [(p, y) for p, y in pairs if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        if b:
            err += len(b) / tot * abs(sum(p for p, _ in b) / len(b) - sum(y for _, y in b) / len(b))
    return err


def _pairs(c, question, version, model, label="yes"):
    """(raw P(label), 1 if the true answer is label) per judged input with an outcome."""
    rows = c.execute(
        "SELECT j.raw, o.label FROM judgments j JOIN outcomes o"
        " ON o.question = j.question AND o.state_key = j.state_key"
        " WHERE j.question=? AND j.version=? AND j.model=? GROUP BY j.state_key",
        (question, version, model)).fetchall()
    return [(json.loads(r)[label], 1 if o == label else 0) for r, o in rows if label in json.loads(r)]


def calibrate(question, model=None, min_n=20):
    """Refit the question's calibration from every judged input that has an outcome.
    Noul only for now (choice/score keep raw probabilities). Returns a report."""
    q = QUESTIONS[question]
    if q["kind"] != "noul":
        return {"question": question, "skipped": "only noul questions are calibrated so far"}
    model = model or (VISION_MODEL if q.get("vision") else TEXT_MODEL)
    with _db() as c:
        pairs = _pairs(c, question, q["version"], model)
        if len(pairs) < min_n:
            return {"question": question, "n": len(pairs), "skipped": f"need {min_n} outcomes"}
        a, b = fit_platt(pairs)
        cal = [(_sigmoid(a * _logit(p) + b), y) for p, y in pairs]
        rep = {"question": question, "model": model, "n": len(pairs), "a": round(a, 4), "b": round(b, 4),
               "ece_raw": round(_ece(pairs), 4), "ece_calibrated": round(_ece(cal), 4),
               "base_rate": round(sum(y for _, y in pairs) / len(pairs), 3)}
        c.execute("INSERT OR REPLACE INTO calibrations VALUES (?,?,?,?,?,?,?,?)",
                  (question, q["version"], model, a, b, len(pairs), _now(), json.dumps(rep)))
    return rep


def _apply_calibration(ans):
    if ans.kind != "noul":
        return
    try:
        with _db() as c:
            r = c.execute("SELECT a, b FROM calibrations WHERE question=? AND version=? AND model=?",
                          (ans.question, ans.version, ans.model)).fetchone()
    except sqlite3.Error:
        r = None
    if r:
        y = _sigmoid(r[0] * _logit(ans.raw["yes"]) + r[1])
        ans.p = {"yes": y, "no": 1 - y}
        ans.calibrated = True


from judge_questions import QUESTIONS  # noqa: E402  (wording lives in one module)
