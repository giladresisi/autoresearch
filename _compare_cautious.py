import csv
from pathlib import Path
import pandas as pd

dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-04-06", "2026-05-15")]

WITH_DIR    = Path("data/regression")
WITHOUT_DIR = Path("../live-may-15-no-cautious/data/regression")

def read_pnl(path):
    p = Path(path)
    if not p.exists():
        return 0, 0.0
    rows = list(csv.DictReader(p.open(encoding="utf-8"), delimiter="\t"))
    pnl = sum(float(r["pnl_dollars"]) for r in rows)
    return len(rows), round(pnl, 2)

hdr = f"{'Date':<12} {'Without':>9} {'With':>9}  {'Delta':>9}  {'CumW/O':>9} {'CumWith':>9}"
print(hdr)
print("-" * len(hdr))

cum_wo = 0.0; cum_w = 0.0
tot_wo = 0.0; tot_w = 0.0
for d in dates:
    _, wo  = read_pnl(WITHOUT_DIR / d / "trades.tsv")
    _, w   = read_pnl(WITH_DIR    / d / "trades.tsv")
    cum_wo += wo; cum_w += w
    tot_wo += wo; tot_w += w
    delta = round(w - wo, 2)
    flag = " ***" if abs(delta) >= 150 else ""
    print(f"{d:<12} {wo:>9.2f} {w:>9.2f}  {delta:>+9.2f}  {cum_wo:>9.2f} {cum_w:>9.2f}{flag}")

print("-" * len(hdr))
delta = round(tot_w - tot_wo, 2)
print(f"{'TOTAL':<12} {tot_wo:>9.2f} {tot_w:>9.2f}  {delta:>+9.2f}")
