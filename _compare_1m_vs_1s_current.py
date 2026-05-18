import csv, os
import pandas as pd

dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-05-01", "2026-05-15")]

def read_trades(path):
    if not os.path.exists(path):
        return 0, 0.0, []
    rows = list(csv.DictReader(open(path, encoding="utf-8"), delimiter="\t"))
    return len(rows), round(sum(float(r["pnl_dollars"]) for r in rows), 2), rows

hdr = "{:<12} {:>9} {:>4} {:>9} {:>4}  {:>9}".format("Date", "1m_pnl", "n1m", "1s_pnl", "n1s", "delta")
print(hdr)
print("-" * 58)
tot1m = tot1s = 0.0
for d in dates:
    n1m, p1m, t1m = read_trades("data/regression/{}/trades.tsv".format(d))
    n1s, p1s, t1s = read_trades("data/regression/{}/trades_1s.tsv".format(d))
    tot1m += p1m; tot1s += p1s
    delta = p1s - p1m
    flag = " ***" if abs(delta) >= 150 else ""
    print("{:<12} {:>9.2f} {:>4} {:>9.2f} {:>4}  {:>+9.2f}{}".format(
        d, p1m, n1m, p1s, n1s, delta, flag))
print("-" * 58)
print("{:<12} {:>9.2f} {:>4} {:>9.2f} {:>4}  {:>+9.2f}".format(
    "TOTAL", tot1m, "", tot1s, "", tot1s - tot1m))
