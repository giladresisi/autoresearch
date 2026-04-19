"""
diagnose_bar_resolution.py — Bar resolution wick-gap diagnostic.

For each stop-exit trade, measures how far the exit bar's close was from the
stop level after the stop was triggered. A large gap means the bar's wick hit
the stop but price recovered within that bar — on a finer-resolution bar this
stop might have survived.

Verdict thresholds (applicable at whatever resolution is currently loaded):
  - Median wick gap > 10 pts  → bars are materially over-stopping;
                                 switching to a finer interval is worth it.
  - Median wick gap < 5 pts   → stop-outs are mostly genuine at this resolution.
  - Between 5 and 10 pts      → borderline; weigh against backtest run time.

History:
  - At 5m (pre-switch): median wick gap was 9.8 pts, ~49% of stops > 10 pts
    → motivated the switch to 1m bars.
  - At 1m (post-switch): median wick gap is 4.8 pts, ~22% of stops > 10 pts
    → switch confirmed correct; further refinement (30s/tick) is diminishing returns.

Run:
    uv run python diagnose_bar_resolution.py
"""
import numpy as np
import pandas as pd
import backtest_smt as train_smt


def main() -> None:
    data   = train_smt.load_futures_data()
    stats  = train_smt.run_backtest(
        data["MNQ"], data["MES"],
        start=train_smt.BACKTEST_START,
        end=train_smt.BACKTEST_END,
    )

    trades = pd.DataFrame(stats["trade_records"])
    stops  = trades[trades["exit_type"] == "exit_stop"].copy()

    wick   = stops["stop_bar_wick_pts"].dropna()

    if wick.empty:
        print("No stop-exit wick data — ensure train_smt.py includes stop_bar_wick_pts in trade records.")
        return

    print(f"Stop-exit trades analysed : {len(wick)}")
    print()
    print("Wick gap distribution (bar extreme that triggered stop -> bar close):")
    for pct in [10, 25, 50, 75, 90, 95]:
        print(f"  p{pct:2d}: {wick.quantile(pct / 100):6.1f} pts")
    print(f"  mean: {wick.mean():6.1f} pts")
    print(f"  max:  {wick.max():6.1f} pts")
    print()

    # How many stop-outs had a wick gap > 5 pts (would plausibly survive on 1m)?
    survived_5  = (wick > 5).sum()
    survived_10 = (wick > 10).sum()
    print(f"Stops with wick gap > 5 pts  (likely false on 1m): {survived_5:3d} / {len(wick)}  ({survived_5/len(wick):.1%})")
    print(f"Stops with wick gap > 10 pts (very likely false)  : {survived_10:3d} / {len(wick)}  ({survived_10/len(wick):.1%})")
    print()

    # Dollar impact: if those trades had survived, their losses turn to ~0 (breakeven)
    # Conservative estimate: each saved stop = avg stop loss recovered
    avg_stop_loss = stops[stops["pnl"] < 0]["pnl"].mean()
    potential_recovery_5  = survived_5  * abs(avg_stop_loss)
    potential_recovery_10 = survived_10 * abs(avg_stop_loss)
    print(f"Average stop loss (losing stops only): ${avg_stop_loss:.2f}")
    print(f"Potential PnL recovery if wick>5  stops survived : ${potential_recovery_5:.0f}")
    print(f"Potential PnL recovery if wick>10 stops survived : ${potential_recovery_10:.0f}")
    print()

    # Verdict
    median = wick.median()
    interval = "current resolution"
    try:
        import json, os
        _mpath = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "futures_data", "futures_manifest.json")
        with open(_mpath) as _f:
            interval = json.load(_f).get("fetch_interval", interval)
    except Exception:
        pass
    print("=" * 50)
    if median > 10:
        print(f"VERDICT: Bars are materially over-stopping at {interval}.")
        print(f"  Median wick gap {median:.1f} pts > 10 pt threshold.")
        print(f"  Switching to a finer interval is worth the compute cost.")
    elif median < 5:
        print(f"VERDICT: Stop-outs are mostly genuine at {interval}.")
        print(f"  Median wick gap {median:.1f} pts < 5 pt threshold.")
        print(f"  Finer resolution is unlikely to recover significant PnL.")
    else:
        print(f"VERDICT: Borderline at {interval} (median wick gap {median:.1f} pts, threshold 5-10).")
        print(f"  Consider finer resolution if backtest run time is acceptable.")
    print("=" * 50)


if __name__ == "__main__":
    main()
