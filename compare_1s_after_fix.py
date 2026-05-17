import csv, os

import pandas as pd
dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-05-01", "2026-05-15")]

def read_pnl(path):
    if not os.path.exists(path):
        return 0, 0.0
    rows = list(csv.DictReader(open(path, encoding="utf-8"), delimiter="\t"))
    return len(rows), sum(float(r["pnl_dollars"]) for r in rows)

hdr = "{:<12} {:>9} {:>6} {:>9} {:>6} {:>9}".format(
    "Date", "1m_pnl", "n1m", "1s_pnl", "n1s", "delta"
)
print(hdr)
print("-" * len(hdr))
tot1m = tot1s = 0.0
for d in dates:
    n1m, p1m = read_pnl("data/regression/{}/baseline_trades.tsv".format(d))
    n1s, p1s = read_pnl("data/regression/{}/trades_1s.tsv".format(d))
    tot1m += p1m
    tot1s += p1s
    delta = p1s - p1m
    flag = " ***" if abs(delta) >= 200 else ""
    print("{:<12} {:>9.2f} {:>6} {:>9.2f} {:>6} {:>+9.2f}{}".format(
        d, p1m, n1m, p1s, n1s, delta, flag
    ))
print("-" * len(hdr))
print("{:<12} {:>9.2f} {:>6} {:>9.2f} {:>6} {:>+9.2f}".format(
    "TOTAL", tot1m, "", tot1s, "", tot1s - tot1m
))
