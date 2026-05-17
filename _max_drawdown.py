import csv
from pathlib import Path

DATES = [
    "2026-05-01","2026-05-04","2026-05-05","2026-05-06","2026-05-07",
    "2026-05-08","2026-05-11","2026-05-12","2026-05-13","2026-05-14","2026-05-15",
]
REG_DIR = Path("data/regression")

# Load all trades in chronological sequence
all_trades = []
for d in DATES:
    p = REG_DIR / d / "trades_1s.tsv"
    if not p.exists():
        continue
    for row in csv.DictReader(p.open(encoding="utf-8"), delimiter="\t"):
        all_trades.append((d, row["entry_time"], row["exit_time"], float(row["pnl_dollars"])))

# Peak-to-trough max drawdown (trade resolution)
cum = 0.0
peak = 0.0
max_dd = 0.0
dd_peak_label = None
dd_trough_label = None
peak_label = None

for date, entry, exit_t, pnl in all_trades:
    cum += pnl
    if cum > peak:
        peak = cum
        peak_label = f"{date} exit={exit_t[11:19]}"
    dd = peak - cum
    if dd > max_dd:
        max_dd = dd
        dd_peak_label = peak_label
        dd_trough_label = f"{date} exit={exit_t[11:19]}"

print(f"Total trades : {len(all_trades)}")
print(f"Final cum PnL: ${cum:,.2f}")
print(f"Peak PnL     : ${peak:,.2f}  at {dd_peak_label}")
print(f"Trough PnL   : ${peak - max_dd:,.2f}  at {dd_trough_label}")
print(f"Max drawdown : ${max_dd:,.2f}")

print()
print("Cumulative PnL by end of day:")
cum2 = 0.0
for d in DATES:
    p = REG_DIR / d / "trades_1s.tsv"
    if not p.exists():
        continue
    rows = list(csv.DictReader(p.open(encoding="utf-8"), delimiter="\t"))
    if not rows:
        continue
    day_pnl = sum(float(r["pnl_dollars"]) for r in rows)
    cum2 += day_pnl
    print(f"  {d}  day={day_pnl:+8.2f}  cumulative={cum2:+8.2f}")
