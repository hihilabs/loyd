"""Teach Loyd's judge camagent's 'is this the nameplate?' from the golden set.

Asks judge.noul('camagent.is_nameplate') about every labeled golden photo (each
answer lands in the judge log), records the human label as the outcome, then
measures honestly: the calibration is fitted on a training split and scored on
photos it never saw. Finally the fit over all outcomes is stored, so every
later answer comes back calibrated.

    python scripts/judge_golden.py --camagent-db /mnt/user/appdata/camagent/agent/data/agent.db \
        --photos /mnt/user/appdata/camagent/photos [--limit N] [--holdout 0.3]

Decision bands follow the TypeSafe guidance: yes >= 0.7, no <= 0.3, the middle
is "ask a person". Re-run after relabeling or rewording; it only asks the model
about photos that have no judgment yet for the current question version, so
the nightly run (cron, 04:00) just picks up new golden labels and refits.
"""
import argparse
import glob
import hashlib
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import judge  # noqa: E402

Q = "camagent.is_nameplate"


def _busy(cdb):
    try:
        with sqlite3.connect(f"file:{cdb}?mode=ro", uri=True) as c:
            return c.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0] > 0
    except sqlite3.Error:
        return True


def _auc(pairs):
    pos = [p for p, y in pairs if y]
    neg = [p for p, y in pairs if not y]
    if not pos or not neg:
        return None
    return sum((a > b) + 0.5 * (a == b) for a in pos for b in neg) / (len(pos) * len(neg))


def _bands(pairs, hi=0.7, lo=0.3):
    decided = [(p, y) for p, y in pairs if p >= hi or p <= lo]
    right = sum((p >= hi) == bool(y) for p, y in decided)
    return len(decided) / len(pairs), (right / len(decided) if decided else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--camagent-db", required=True)
    ap.add_argument("--photos", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--holdout", type=float, default=0.3)
    ap.add_argument("--busy-gap", type=float, default=30.0,
                    help="seconds between photos while camagent has a running job (shared GPU)")
    a = ap.parse_args()

    c = sqlite3.connect(f"file:{a.camagent_db}?mode=ro", uri=True)
    rows = c.execute("SELECT photo_id, status FROM golden_items WHERE status IN ('confirmed', 'not_plate')"
                     " ORDER BY photo_id").fetchall()
    if a.limit:
        rows = rows[:a.limit]
    q = judge.QUESTIONS[Q]
    with judge._db() as jc:
        done = {k for (k,) in jc.execute("SELECT state_key FROM judgments WHERE question=? AND version=?",
                                         (Q, q["version"]))}
    asked = 0
    for pid, status in rows:
        hit = glob.glob(os.path.join(a.photos, "*", f"{pid}.jpg"))
        if not hit:
            continue
        key = judge.state_key(images=[hit[0]])
        label = "yes" if status == "confirmed" else "no"
        if key not in done:
            if _busy(a.camagent_db):
                time.sleep(a.busy_gap)          # camagent's workers share this GPU: trickle
            try:
                judge.noul(Q, images=[hit[0]])
                asked += 1
            except judge.JudgeError as e:
                print("no answer:", pid, e, flush=True)
                continue
            if asked % 25 == 0:
                print(f"  asked {asked}", flush=True)
        judge.outcome(Q, label, key=key, source="golden")

    with judge._db() as jc:
        model = judge.VISION_MODEL
        rows = jc.execute(
            "SELECT j.state_key, j.raw, o.label FROM judgments j JOIN outcomes o"
            " ON o.question=j.question AND o.state_key=j.state_key"
            " WHERE j.question=? AND j.version=? AND j.model=? GROUP BY j.state_key",
            (Q, q["version"], model)).fetchall()
    import json
    data = [(k, json.loads(r)["yes"], 1 if lab == "yes" else 0) for k, r, lab in rows]
    test = [(p, y) for k, p, y in data if int(hashlib.sha1(k.encode()).hexdigest(), 16) % 100 < a.holdout * 100]
    train = [(p, y) for k, p, y in data if int(hashlib.sha1(k.encode()).hexdigest(), 16) % 100 >= a.holdout * 100]
    print(f"\nasked the model {asked} new photos; {len(data)} judged golden photos "
          f"(train {len(train)}, held out {len(test)})")
    if len(train) < 20 or len(test) < 10:
        print("not enough data for an honest split yet")
        return
    fa, fb = judge.fit_platt(train)
    cal = [(judge._sigmoid(fa * judge._logit(p) + fb), y) for p, y in test]
    print(f"separation (AUC, held out): {_auc(test):.3f}   (1.0 perfect, 0.5 coin flip)")
    for name, pairs in (("raw", test), ("calibrated", cal)):
        cov, acc = _bands(pairs)
        acc5 = sum((p >= 0.5) == bool(y) for p, y in pairs) / len(pairs)
        print(f"  {name:10} accuracy@0.5 {acc5:.3f}   ECE {judge._ece(pairs):.3f}   "
              f"decides {cov:.0%} on its own (>=0.7 / <=0.3), right {acc if acc is None else round(acc, 3)}")
    rep = judge.calibrate(Q, model=model)
    print("stored calibration over all outcomes:", rep)


if __name__ == "__main__":
    main()
