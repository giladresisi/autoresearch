"""
analyze_gaps.py — Gap-vs-PnL analysis for gap filter threshold calibration.

For each trade in trades.tsv:
  1. Load ticker parquet from CACHE_DIR (harness cache, not screener cache)
  2. Find the close on entry_date - 1 trading day (prev_close)
  3. Compute gap = (entry_price - prev_close) / prev_close

Outputs:
  - Gap distribution for winners vs losers
  - Fraction of losers with gap < -1%, -2%, -3%, -4%
  - Fraction of winners excluded at each threshold
  - Recommended threshold (largest negative gap with < 10% winner exclusion rate)

Usage:
    uv run analyze_gaps.py [--trades trades.tsv] [--cache-dir PATH]
"""
import argparse
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

from train import CACHE_DIR


def load_trades(path: str) -> pd.DataFrame:
    """Load trades.tsv; exit with error if not found."""
    if not os.path.exists(path):
        print(f"trades.tsv not found -- run train.py first (looked at: {path})")
        sys.exit(1)
    return pd.read_csv(path, sep="\t", parse_dates=["entry_date", "exit_date"])


def compute_gaps(trades: pd.DataFrame, cache_dir: str) -> pd.DataFrame:
    """
    Compute gap_pct for each trade.
    gap_pct = (entry_price - prev_close) / prev_close
    where prev_close is the last close before entry_date in the parquet.
    Trades with missing parquet or no prior close row are dropped with a warning.
    """
    records = []
    for _, row in trades.iterrows():
        ticker = row["ticker"]
        entry_date = row["entry_date"].date() if hasattr(row["entry_date"], "date") else row["entry_date"]
        parquet_path = os.path.join(cache_dir, f"{ticker}.parquet")
        if not os.path.exists(parquet_path):
            print(f"  WARNING: {ticker} not in cache, skipping")
            continue
        try:
            df = pd.read_parquet(parquet_path)
            # Find rows strictly before entry_date
            prior = df[df.index < entry_date]
            if prior.empty:
                print(f"  WARNING: {ticker} has no rows before {entry_date}, skipping")
                continue
            prev_close = float(prior["close"].iloc[-1])
            entry_price = float(row["entry_price"])
            gap_pct = (entry_price - prev_close) / prev_close
            records.append({
                "ticker":      ticker,
                "entry_date":  entry_date,
                "entry_price": entry_price,
                "prev_close":  prev_close,
                "gap_pct":     gap_pct,
                "pnl":         float(row["pnl"]),
                "winner":      float(row["pnl"]) > 0,
            })
        except Exception as e:
            print(f"  WARNING: error processing {ticker}: {e}")
            continue
    return pd.DataFrame(records)


def print_analysis(df: pd.DataFrame) -> None:
    """Print gap distribution and threshold analysis to stdout."""
    winners = df[df["winner"]]
    losers  = df[~df["winner"]]

    n_w = len(winners)
    n_l = len(losers)

    if n_w == 0 and n_l == 0:
        print("No trades with computable gaps found.")
        return

    def _stats(series):
        if series.empty:
            return "  mean= N/A   median= N/A"
        return f"  mean={series.mean()*100:+.2f}%  median={series.median()*100:+.2f}%"

    print(f"Gap distribution (winners, n={n_w}): {_stats(winners['gap_pct'])}")
    print(f"Gap distribution (losers,  n={n_l}):  {_stats(losers['gap_pct'])}")
    print()

    thresholds = [-0.01, -0.02, -0.03, -0.04]
    print("Threshold analysis:")
    best_threshold = None
    for t in thresholds:
        n_losers_removed  = int((losers["gap_pct"]  < t).sum()) if n_l > 0 else 0
        n_winners_removed = int((winners["gap_pct"] < t).sum()) if n_w > 0 else 0
        pct_l = n_losers_removed  / n_l   * 100 if n_l > 0 else 0.0
        pct_w = n_winners_removed / n_w   * 100 if n_w > 0 else 0.0
        print(
            f"  gap < {t*100:.0f}%:  removes {n_losers_removed:3d} losers ({pct_l:.1f}%) | "
            f"{n_winners_removed:3d} winners ({pct_w:.1f}%)"
        )
        # Recommend the most negative threshold with < 10% winner exclusion
        if pct_w < 10.0:
            best_threshold = t

    print()
    if best_threshold is not None:
        n_l_removed = int((losers["gap_pct"] < best_threshold).sum()) if n_l > 0 else 0
        pct_l_r = n_l_removed / n_l * 100 if n_l > 0 else 0.0
        n_w_removed = int((winners["gap_pct"] < best_threshold).sum()) if n_w > 0 else 0
        pct_w_r = n_w_removed / n_w * 100 if n_w > 0 else 0.0
        print(
            f"Recommended: GAP_THRESHOLD = {best_threshold*100:.0f}% "
            f"(removes {pct_l_r:.1f}% of losers, {pct_w_r:.1f}% of winners)"
        )
    else:
        print("Recommended: GAP_THRESHOLD = -1% (no threshold removes < 10% of winners)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gap vs PnL threshold analysis")
    parser.add_argument("--trades",    default="trades.tsv",  help="Path to trades.tsv")
    parser.add_argument("--cache-dir", default=CACHE_DIR,     help="Parquet cache directory")
    args = parser.parse_args()

    trades = load_trades(args.trades)
    df = compute_gaps(trades, args.cache_dir)

    if df.empty:
        print("No gap data computed -- check trades.tsv and cache-dir.")
        sys.exit(0)

    print_analysis(df)
