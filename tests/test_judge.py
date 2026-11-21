"""judge: probabilities from logprobs, logging, outcomes, calibration. No GPU needed:
the Ollama call is replaced with a fake that returns a chosen next-token distribution."""
import math

import pytest


@pytest.fixture
def judge(tmp_path, monkeypatch):
    monkeypatch.setenv("LOYD_JUDGE_DB", str(tmp_path / "judge.db"))
    import importlib
    import judge as j
    importlib.reload(j)
    return j


def fake(dist):
    """A stand-in for judge._post returning top_logprobs for the given {token: p}."""
    def _post(url, body):
        return {"response": max(dist, key=dist.get),
                "logprobs": [{"top_logprobs": [{"token": t, "logprob": math.log(p)} for t, p in dist.items()]}]}
    return _post


def test_noul_sums_token_variants(judge):
    a = judge.noul("loyd.routine_question", state="is plex up?",
                   _post=fake({"yes": 0.6, " Yes": 0.2, "no": 0.1, "maybe": 0.1}))
    assert a.kind == "noul" and a.mass == pytest.approx(0.9)
    assert a.yes == pytest.approx(0.8 / 0.9)
    assert a.judgment_id is not None and not a.calibrated


def test_no_valid_mass_is_an_error_not_a_no(judge):
    with pytest.raises(judge.JudgeError):
        judge.noul("loyd.routine_question", state="x", _post=fake({"maybe": 0.9, "no": 0.05}))


def test_choice_and_score_map_back_to_names(judge, monkeypatch):
    judge.QUESTIONS["t.choice"] = {"kind": "choice", "version": 1,
                                   "question": "Which?", "options": {"red": "r", "blue": "b"}}
    judge.QUESTIONS["t.score"] = {"kind": "score", "version": 1,
                                  "question": "How much?", "levels": ["low", "mid", "high"]}
    c = judge.choice("t.choice", state={}, _post=fake({"B": 0.75, "A": 0.25}))
    assert c.top == "blue" and c.p["blue"] == pytest.approx(0.75)
    s = judge.score("t.score", state={}, _post=fake({"3": 0.5, "2": 0.5}))
    assert s.extra["expected"] == pytest.approx(2.5)


def test_outcomes_and_calibration_learn(judge):
    """The model says 0.9 every time but is right only 60% of the time: after
    calibrate(), answers are pulled toward what actually happens."""
    q = "loyd.routine_question"
    for i in range(50):
        a = judge.noul(q, state=f"question {i}", model="m",
                       _post=fake({"yes": 0.9, "no": 0.1}))
        judge.outcome(q, "yes" if i % 5 < 3 else "no", state=f"question {i}", source="test")
    rep = judge.calibrate(q, model="m")
    assert rep["n"] == 50 and rep["ece_calibrated"] < rep["ece_raw"]
    after = judge.noul(q, state="new question", model="m", _post=fake({"yes": 0.9, "no": 0.1}))
    assert after.calibrated and after.yes == pytest.approx(0.6, abs=0.05)
    assert after.raw["yes"] == pytest.approx(0.9)      # the raw answer is always kept


def test_platt_recovers_a_known_map(judge):
    import random
    random.seed(7)
    pairs = []
    for _ in range(4000):
        p = random.uniform(0.02, 0.98)
        true = judge._sigmoid(0.5 * judge._logit(p) - 0.3)
        pairs.append((p, 1 if random.random() < true else 0))
    a, b = judge.fit_platt(pairs)
    assert a == pytest.approx(0.5, abs=0.1) and b == pytest.approx(-0.3, abs=0.1)


def test_failed_call_is_judge_error(judge, monkeypatch):
    import urllib.error
    calls = []

    def boom(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 500, "runner died", {}, None)
    monkeypatch.setattr(judge.urllib.request, "urlopen", boom)
    with pytest.raises(judge.JudgeError):
        judge._post("http://x", {"model": "m"}, retries=1, pause=0)
    assert len(calls) == 2          # one retry, then give up
