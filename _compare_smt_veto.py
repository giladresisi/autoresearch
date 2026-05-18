"""Compare 1s regression P&L with vs without SMT-score veto (May 1-15 2026)."""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import pandas as pd
import strategy
from backtest_smt import run_backtest_v2

DATES = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-05-01", "2026-05-15")]

def run_all(label: str) -> dict:
    results = {}
    for date in DATES:
        r = run_backtest_v2(date, date, write_events=False, mode="1s")
        m = r.get("metrics", {})
        results[date] = {"pnl": m.get("total_pnl", 0.0), "n": m.get("n_trades", 0)}
    return results

# --- with veto ---
strategy.SMT_VETO_ENABLED = True
with_veto = run_all("with_veto")

# --- without veto ---
strategy.SMT_VETO_ENABLED = False
without_veto = run_all("without_veto")

# --- print comparison ---
print(f"\n{'Date':<12} {'NoVeto$':>10} {'Veto$':>10} {'Delta$':>10} {'NoVeto#':>8} {'Veto#':>7}")
print("-" * 62)
total_no, total_with = 0.0, 0.0
for date in DATES:
    nv = without_veto[date]
    wv = with_veto[date]
    delta = wv["pnl"] - nv["pnl"]
    total_no   += nv["pnl"]
    total_with += wv["pnl"]
    marker = " <--" if abs(delta) >= 50 else ""
    print(f"{date:<12} {nv['pnl']:>10.2f} {wv['pnl']:>10.2f} {delta:>+10.2f} {nv['n']:>8} {wv['n']:>7}{marker}")

print("-" * 62)
total_delta = total_with - total_no
print(f"{'TOTAL':<12} {total_no:>10.2f} {total_with:>10.2f} {total_delta:>+10.2f}")
