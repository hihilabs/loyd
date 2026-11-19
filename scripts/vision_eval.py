import json, re
B="/mnt/user/appdata/camagent/bench_results"
def load(n): return {json.loads(l)["golden_meta"]["photo_id"]: json.loads(l) for l in open(f"{B}/{n}/per_photo.jsonl")}
M, Q = load("h_minicpm"), load("h_qwen3vl")
ids = [i for i in M if i in Q]
norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s or "").upper())
CONF = str.maketrans("OIZSBQ", "012580")
loose = lambda s: norm(s).translate(CONF)
REF = {"410A":"R-410A","22":"R-22","32":"R-32","454B":"R-454B","407C":"R-407C","134A":"R-134A"}
def ref(s):
    m = re.search(r"(410A|454B|407C|134A|(?<!\d)32(?!\d)|(?<!\d)22(?!\d))", norm(s).replace("R", " ").replace(" ", ""))
    return REF.get(m.group(1)) if m else None
CAP = {"018":1.5,"024":2,"030":2.5,"036":3,"042":3.5,"048":4,"060":5,"18":1.5,"24":2,"30":2.5,"36":3,"42":3.5,"48":4,"60":5}
def tons_from_model(model):
    m = norm(model)
    for code in re.findall(r"(?<!\d)(0[1-6][0-9])(?!\d)", m) + re.findall(r"[A-Z](18|24|30|36|42|48|60)[A-Z]", m):
        if code in CAP: return CAP[code]
    return None
def tons_eq(a, b):
    try: return a is not None and b is not None and abs(float(a) - float(b)) < 0.01
    except (TypeError, ValueError): return False
pct = lambda a, n: f"{100*a/n:5.1f}%" if n else "  n/a"
print(f"photos scored: {len(ids)}  (golden labels contain known errors; numbers are a floor)\n")
print(f"{'field':12} {'minicpm exact':>14} {'qwen exact':>11} {'char-fix ceiling':>17} {'best-per-field':>15} {'both agree: share / right':>27}")
for f in ("brand", "model", "serial"):
    g = [i for i in ids if M[i]["golden"].get(f)]
    ex = lambda R, i: norm(R[i]["predicted"].get(f)) == norm(R[i]["golden"][f]) and norm(R[i]["golden"][f]) != ""
    lo = lambda R, i: loose(R[i]["predicted"].get(f)) == loose(R[i]["golden"][f])
    em, eq = sum(ex(M, i) for i in g), sum(ex(Q, i) for i in g)
    best = max(em, eq)
    cf = max(sum(lo(M, i) for i in g), sum(lo(Q, i) for i in g))
    agree = [i for i in g if norm(M[i]["predicted"].get(f)) and norm(M[i]["predicted"].get(f)) == norm(Q[i]["predicted"].get(f))]
    right = sum(ex(M, i) for i in agree)
    print(f"{f:12} {pct(em,len(g)):>14} {pct(eq,len(g)):>11} {pct(cf,len(g)):>17} {pct(best,len(g)):>15} {pct(len(agree),len(g))+' / '+pct(right,len(agree)):>27}")
g = [i for i in ids if ref(M[i]["golden"].get("refrigerant"))]
for name, R in (("minicpm", M), ("qwen", Q)):
    raw = sum(norm(R[i]["predicted"].get("refrigerant")) == norm(R[i]["golden"]["refrigerant"]) for i in g)
    snap = sum(ref(R[i]["predicted"].get("refrigerant")) == ref(R[i]["golden"]["refrigerant"]) for i in g)
    print(f"refrigerant  {name:8} raw exact {pct(raw,len(g))}  -> snapped to valid list {pct(snap,len(g))}   (n={len(g)}, golden values that aren't a refrigerant excluded)")
g = [i for i in ids if M[i]["golden"].get("tonnage") not in (None, "")]
for name, R in (("minicpm", M), ("qwen", Q)):
    vlm = sum(tons_eq(R[i]["predicted"].get("tonnage"), R[i]["golden"]["tonnage"]) for i in g)
    der = [i for i in g if tons_from_model(R[i]["predicted"].get("model")) is not None]
    derok = sum(tons_eq(tons_from_model(R[i]["predicted"].get("model")), R[i]["golden"]["tonnage"]) for i in der)
    comb = sum(tons_eq(tons_from_model(R[i]["predicted"].get("model")) if tons_from_model(R[i]["predicted"].get("model")) is not None else R[i]["predicted"].get("tonnage"), R[i]["golden"]["tonnage"]) for i in g)
    print(f"tonnage      {name:8} as read {pct(vlm,len(g))}  | from model code: covers {pct(len(der),len(g))}, right {pct(derok,len(der))} | code-first then read {pct(comb,len(g))}   (n={len(g)})")
