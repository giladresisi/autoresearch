import csv, os

def read_trades(path):
    if not os.path.exists(path):
        return []
    return list(csv.DictReader(open(path, encoding="utf-8-sig"), delimiter="\t"))

dates = [
    ("2026-05-01", "+296 (1s wins)"),
    ("2026-05-04", "-259 (1s loses)"),
    ("2026-05-08", "-165 (1s loses)"),
    ("2026-05-13", "+233 (1s wins)"),
    ("2026-05-14", "-438 (1s loses)"),
    ("2026-05-15", "-149 (1s loses)"),
]

for d, label in dates:
    t1m = read_trades("data/regression/{}/baseline_trades.tsv".format(d))
    t1s = read_trades("data/regression/{}/trades_1s.tsv".format(d))
    p1m = sum(float(r["pnl_dollars"]) for r in t1m)
    p1s = sum(float(r["pnl_dollars"]) for r in t1s)
    print("--- {} {} ---".format(d, label))
    print("  1m ({} trades, {:.2f}):".format(len(t1m), p1m))
    for r in t1m:
        et = r["entry_time"][11:19]
        xt = r["exit_time"][11:19]
        print("    {} -> {} {:4s} {:15s} {:+8.2f}".format(
            et, xt, r["direction"], r["exit_reason"], float(r["pnl_dollars"])))
    print("  1s ({} trades, {:.2f}):".format(len(t1s), p1s))
    for r in t1s:
        et = r["entry_time"][11:19]
        xt = r["exit_time"][11:19]
        print("    {} -> {} {:4s} {:15s} {:+8.2f}".format(
            et, xt, r["direction"], r["exit_reason"], float(r["pnl_dollars"])))
    print()
