"""One-off script: single full-range backtest + session-direction comparison → pre-strategy-update.tsv"""
import csv
import datetime
from pathlib import Path

import pandas as pd

from backtest_smt import run_backtest, BACKTEST_START, BACKTEST_END
from strategy_smt import load_futures_data
from hypothesis_smt import compute_hypothesis_direction

OUTFILE = "pre-strategy-update.tsv"

FIELDNAMES = [
    "entry_date", "entry_time", "exit_time", "direction",
    "entry_price", "exit_price", "tdo", "stop_price", "contracts",
    "pnl", "exit_type", "divergence_bar", "entry_bar",
    "stop_bar_wick_pts", "reentry_sequence", "prior_trade_bars_held",
    "entry_bar_body_ratio", "smt_sweep_pts", "smt_miss_pts", "bars_since_divergence",
    "matches_hypothesis",
    # New comparison fields
    "session_direction",
    "entry_matches_session",
]

def main():
    dfs = load_futures_data()
    mnq_df = dfs["MNQ"]
    mes_df = dfs["MES"]

    hist_mnq_path = Path("data/MNQ.parquet")
    hist_mnq_df = pd.read_parquet(hist_mnq_path) if hist_mnq_path.exists() else pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"]
    )

    print(f"Running single backtest {BACKTEST_START} to {BACKTEST_END} ...")
    stats = run_backtest(mnq_df, mes_df, start=BACKTEST_START, end=BACKTEST_END)
    trades = stats["trade_records"]
    print(f"Backtest complete: {len(trades)} trades, total PnL ${stats['total_pnl']:.2f}")

    # Cache hypothesis direction per unique date
    hyp_cache: dict[datetime.date, str | None] = {}
    unique_dates = {t["entry_date"] for t in trades}
    for raw_date in unique_dates:
        date = datetime.date.fromisoformat(str(raw_date))
        hyp_cache[raw_date] = compute_hypothesis_direction(mnq_df, hist_mnq_df, date)

    # Annotate trades with session_direction and entry_matches_session
    for trade in trades:
        hyp_dir = hyp_cache.get(trade["entry_date"])
        trade["session_direction"] = hyp_dir if hyp_dir is not None else ""
        if hyp_dir is None:
            trade["entry_matches_session"] = ""
        else:
            trade["entry_matches_session"] = trade["direction"] == hyp_dir

    # Write TSV
    with open(OUTFILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, delimiter="\t", extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(trades)

    print(f"Written to {OUTFILE} ({len(trades)} records)")

    # Print alignment summary
    aligned   = [t for t in trades if t["entry_matches_session"] is True]
    opposing  = [t for t in trades if t["entry_matches_session"] is False]
    no_hyp    = [t for t in trades if t["entry_matches_session"] == ""]

    print(f"\n=== Session Direction Alignment ===")
    print(f"Total trades:           {len(trades)}")
    print(f"Hypothesis available:   {len(aligned) + len(opposing)}")
    print(f"  Aligned with session: {len(aligned)}  (PnL ${sum(t['pnl'] for t in aligned):.2f})")
    print(f"  Against session:      {len(opposing)} (PnL ${sum(t['pnl'] for t in opposing):.2f})")
    print(f"No hypothesis:         {len(no_hyp)}")

    for label, subset in [("Aligned", aligned), ("Against", opposing)]:
        if not subset:
            continue
        wins = sum(1 for t in subset if t["pnl"] > 0)
        wr = wins / len(subset) * 100
        avg = sum(t["pnl"] for t in subset) / len(subset)
        print(f"  {label}: WR={wr:.1f}%  avg_pnl=${avg:.2f}")


if __name__ == "__main__":
    main()
