"""Ask judge's 'camagent.is_nameplate' about every camagent unit's photo, politely.

camagent's two GPU workers share ollama-gpu0 with this batch. On 2026-11-21 a
back-to-back judge run made 3 concurrent requests against NUM_PARALLEL=2 and
the workers' VLM calls timed out four times. So this batch:
  * runs flat out only while camagent has no running job,
  * trickles one photo per --busy-gap seconds while it does,
  * waits while ollama-gpu0 is near its memory cap (default 13 GiB of 16),
and records every unit it has asked about, so it can stop and resume anytime.

    # a stratified sample first (fast comparison across spec-audit statuses)
    python scripts/judge_camagent_batch.py --camagent-db .../agent.db --photos .../photos --sample 200
    # then the whole fleet, as long as it takes
    python scripts/judge_camagent_batch.py --camagent-db .../agent.db --photos .../photos
    # report only
    python scripts/judge_camagent_batch.py --camagent-db .../agent.db --photos .../photos --report

Nothing is written to camagent. Results live in the judge DB (table batch_items).
"""
import argparse
import collections
import os
import re
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import judge  # noqa: E402

Q = "camagent.is_nameplate"
STATUSES = ("no_model", "unverified", "suspect", "not_a_unit", "unknown", "conflict", "confirmed", "decoded")

BATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS batch_items (
    question  TEXT NOT NULL,
    item      TEXT NOT NULL,       -- camagent unit id
    photo     TEXT,
    stratum   TEXT,                -- spec-audit tonnage status when asked
    p_raw     REAL,
    p         REAL,                -- calibrated when a fit existed
    error     TEXT,
    asked_at  TEXT,
    PRIMARY KEY (question, item)
);
"""


def camagent_busy(cdb):
    with sqlite3.connect(f"file:{cdb}?mode=ro", uri=True) as c:
        return c.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0] > 0


def gpu0_mem_gib(container="ollama-gpu0"):
    try:
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],
                             capture_output=True, text=True, timeout=30).stdout
        m = re.match(r"([\d.]+)\s*([KMG])iB", out.strip())
        return float(m.group(1)) * {"K": 1 / 1048576, "M": 1 / 1024, "G": 1}[m.group(2)] if m else 0.0
    except Exception:
        return 0.0


def unit_photo(c, photos_dir, unit_id):
    """The unit's newest CompanyCam photo that exists locally, else None."""
    for (ref,) in c.execute("SELECT source_ref FROM unit_photos WHERE unit_id=? AND source='companycam'"
                            " AND source_ref IS NOT NULL ORDER BY id DESC", (unit_id,)):
        for p in (os.path.join(photos_dir, d, f"{ref}.jpg") for d in _dirs(photos_dir)):
            if os.path.exists(p):
                return p
    return None


_DIRCACHE = None


def _dirs(photos_dir):
    global _DIRCACHE
    if _DIRCACHE is None:
        _DIRCACHE = [d for d in os.listdir(photos_dir) if os.path.isdir(os.path.join(photos_dir, d))]
    return _DIRCACHE


def _index(photos_dir):
    """photo id -> path, built once (the photos tree has ~10^5 files in project dirs)."""
    idx = {}
    for d in _dirs(photos_dir):
        for f in os.listdir(os.path.join(photos_dir, d)):
            if f.endswith(".jpg"):
                idx[f[:-4]] = os.path.join(photos_dir, d, f)
    return idx


def candidates(cdb, sample):
    with sqlite3.connect(f"file:{cdb}?mode=ro", uri=True) as c:
        rows = c.execute(
            "SELECT s.unit_id, s.status, (SELECT p.source_ref FROM unit_photos p WHERE p.unit_id=s.unit_id"
            " AND p.source='companycam' AND p.source_ref IS NOT NULL ORDER BY p.id DESC LIMIT 1)"
            " FROM unit_spec_status s WHERE s.field='tonnage'").fetchall()
    rows = [r for r in rows if r[2]]
    if not sample:
        return rows
    by = collections.defaultdict(list)
    for r in rows:
        by[r[1]].append(r)
    out = []
    for st in STATUSES:
        b = sorted(by.get(st, []), key=lambda r: (r[0] * 2654435761) % 4294967296)   # stable shuffle
        out += b[:sample]
    return out


def report(jdb_path):
    with sqlite3.connect(jdb_path) as j:
        j.executescript(BATCH_SCHEMA)
        rows = j.execute("SELECT stratum, p FROM batch_items WHERE question=? AND p IS NOT NULL", (Q,)).fetchall()
    by = collections.defaultdict(list)
    for st, p in rows:
        by[st].append(p)
    print(f"{'status':11} {'n':>5} {'nameplate (>=0.7)':>18} {'not (<=0.3)':>12} {'unsure':>7} {'mean P':>7}")
    for st in STATUSES:
        ps = by.get(st)
        if not ps:
            continue
        n = len(ps)
        print(f"{st:11} {n:5} {sum(p >= .7 for p in ps) / n:18.0%} {sum(p <= .3 for p in ps) / n:12.0%} "
              f"{sum(.3 < p < .7 for p in ps) / n:7.0%} {sum(ps) / n:7.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--camagent-db", required=True)
    ap.add_argument("--photos", required=True)
    ap.add_argument("--sample", type=int, default=0, help="N units per spec-audit status, then stop")
    ap.add_argument("--busy-gap", type=float, default=30.0)
    ap.add_argument("--mem-ceiling", type=float, default=13.0)
    ap.add_argument("--max-hours", type=float, default=0, help="stop after this long (0 = no limit)")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    jdb = judge.DB_PATH
    if a.report:
        report(jdb)
        return
    with judge._db() as j:
        j.executescript(BATCH_SCHEMA)
        done = {r[0] for r in j.execute("SELECT item FROM batch_items WHERE question=?", (Q,))}
    idx = _index(a.photos)
    todo = [r for r in candidates(a.camagent_db, a.sample) if str(r[0]) not in done]
    print(f"{len(todo)} units to ask about ({len(done)} already done)", flush=True)
    t0, asked = time.time(), 0
    for unit_id, status, ref in todo:
        if a.max_hours and time.time() - t0 > a.max_hours * 3600:
            print("time box reached", flush=True)
            break
        path = idx.get(str(ref))
        if not path:
            continue
        while gpu0_mem_gib() > a.mem_ceiling:
            time.sleep(60)
        busy = camagent_busy(a.camagent_db)
        err, p_raw, p = None, None, None
        try:
            ans = judge.noul(Q, images=[path])
            p_raw, p = ans.raw["yes"], ans.yes
        except judge.JudgeError as e:
            err = str(e)[:200]
        with judge._db() as j:
            j.execute("INSERT OR REPLACE INTO batch_items VALUES (?,?,?,?,?,?,?,?)",
                      (Q, str(unit_id), str(ref), status, p_raw, p, err, judge._now()))
        asked += 1
        if asked % 50 == 0:
            print(f"  asked {asked} ({'camagent busy, trickling' if busy else 'camagent idle'})", flush=True)
        if busy:
            time.sleep(a.busy_gap)
    print(f"asked {asked}", flush=True)
    report(jdb)


if __name__ == "__main__":
    main()
