import csv
from pathlib import Path
import pandas as pd

dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-04-06", "2026-05-15")]

def read_pnl(path):
    p = Path(path)
    if not p.exists():
        return 0, 0.0
    rows = list(csv.DictReader(p.open(encoding="utf-8"), delimiter="\t"))
    pnl = sum(float(r["pnl_dollars"]) for r in rows)
    return len(rows), round(pnl, 2)

hdr = f"{'Date':<12} {'BL-1m':>8} {'Cur-1m':>8}  {'Delta':>8}  {'Cum-BL':>9} {'Cum-Cur':>9}"
print(hdr)
print("-" * len(hdr))

cum_bl = 0.0; cum_cur = 0.0
tot_bl = 0.0; tot_cur = 0.0
for d in dates:
    base = f"data/regression/{d}"
    _, bl  = read_pnl(f"{base}/baseline_trades.tsv")
    _, cur = read_pnl(f"{base}/trades.tsv")
    cum_bl  += bl
    cum_cur += cur
    tot_bl  += bl
    tot_cur += cur
    delta = round(cur - bl, 2)
    flag = " ***" if abs(delta) >= 150 else ""
    print(f"{d:<12} {bl:>8.2f} {cur:>8.2f}  {delta:>+8.2f}  {cum_bl:>9.2f} {cum_cur:>9.2f}{flag}")

print("-" * len(hdr))
delta = round(tot_cur - tot_bl, 2)
print(f"{'TOTAL':<12} {tot_bl:>8.2f} {tot_cur:>8.2f}  {delta:>+8.2f}")
