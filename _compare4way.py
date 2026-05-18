import csv
from pathlib import Path

dates = [
    "2026-05-01","2026-05-04","2026-05-05","2026-05-06","2026-05-07",
    "2026-05-08","2026-05-11","2026-05-12","2026-05-13","2026-05-14","2026-05-15",
]

def read_pnl(path):
    p = Path(path)
    if not p.exists():
        return 0, 0.0
    rows = list(csv.DictReader(p.open(encoding="utf-8"), delimiter="\t"))
    pnl = sum(float(r["pnl_dollars"]) for r in rows)
    return len(rows), round(pnl, 2)

hdr = f"{'Date':<12} {'1m-BL':>8} {'1m-Cur':>8} {'1s-BL':>8} {'1s-Cur':>8}  {'d1m(C-B)':>9} {'d1s(C-B)':>9} {'BL(1s-1m)':>10} {'Cur(1s-1m)':>11}"
print(hdr)
print("-" * len(hdr))

tot = dict(bl1m=0.0, cur1m=0.0, bl1s=0.0, cur1s=0.0)
for d in dates:
    base = f"data/regression/{d}"
    _, bl1m  = read_pnl(f"{base}/baseline_trades.tsv")
    _, cur1m = read_pnl(f"{base}/trades.tsv")
    _, bl1s  = read_pnl(f"{base}/baseline_trades_1s.tsv")
    _, cur1s = read_pnl(f"{base}/trades_1s.tsv")
    for k, v in zip(["bl1m","cur1m","bl1s","cur1s"],[bl1m,cur1m,bl1s,cur1s]):
        tot[k] += v
    d1m  = round(cur1m - bl1m, 2)
    d1s  = round(cur1s - bl1s, 2)
    bld  = round(bl1s  - bl1m, 2)
    curd = round(cur1s - cur1m, 2)
    print(f"{d:<12} {bl1m:>8.2f} {cur1m:>8.2f} {bl1s:>8.2f} {cur1s:>8.2f}  {d1m:>+9.2f} {d1s:>+9.2f} {bld:>+10.2f} {curd:>+11.2f}")

print("-" * len(hdr))
t = tot
d1m  = round(t["cur1m"] - t["bl1m"], 2)
d1s  = round(t["cur1s"] - t["bl1s"], 2)
bld  = round(t["bl1s"]  - t["bl1m"], 2)
curd = round(t["cur1s"] - t["cur1m"], 2)
print(f"{'TOTAL':<12} {t['bl1m']:>8.2f} {t['cur1m']:>8.2f} {t['bl1s']:>8.2f} {t['cur1s']:>8.2f}  {d1m:>+9.2f} {d1s:>+9.2f} {bld:>+10.2f} {curd:>+11.2f}")
