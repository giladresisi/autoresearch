# analyze_hypothesis.py
# Reads trades.tsv and prints a hypothesis alignment analysis: aligned vs misaligned
# vs no-hypothesis split with per-group stats, per-fold consistency, and edge verdict.
import sys
from typing import Optional

import pandas as pd


def load_trades(path: str = "trades.tsv") -> pd.DataFrame:
    """Read trades.tsv; return empty DataFrame (with warning) if file not found."""
    try:
        df = pd.read_csv(path, sep="\t", dtype=str)
    except FileNotFoundError:
        print(f"Warning: {path} not found. Run backtest_smt.py first.")
        return pd.DataFrame()
    if df.empty:
        return df
    if "pnl" in df.columns:
        df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce").fillna(0.0)
    return df


def split_by_alignment(df: pd.DataFrame) -> dict:
    """Partition df on matches_hypothesis into aligned / misaligned / no_hypothesis.

    Handles both boolean True/False and string 'True'/'False' from TSV serialization.
    """
    if df.empty or "matches_hypothesis" not in df.columns:
        return {"aligned": pd.DataFrame(), "misaligned": pd.DataFrame(), "no_hypothesis": df}
    col = df["matches_hypothesis"].astype(str).str.strip()
    aligned     = df[col == "True"]
    misaligned  = df[col == "False"]
    no_hyp_mask = ~col.isin(["True", "False"])
    no_hypothesis = df[no_hyp_mask]
    return {"aligned": aligned, "misaligned": misaligned, "no_hypothesis": no_hypothesis}


def compute_group_stats(group: pd.DataFrame) -> dict:
    """Compute performance stats for a group of trades."""
    if group.empty:
        return {
            "count": 0, "win_rate": 0.0, "avg_pnl": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "avg_rr": 0.0, "exit_types": {},
        }
    pnl = group["pnl"] if "pnl" in group.columns else pd.Series(dtype=float)
    winners = pnl[pnl > 0]
    losers  = pnl[pnl <= 0]
    avg_win  = float(winners.mean()) if not winners.empty else 0.0
    avg_loss = float(losers.mean())  if not losers.empty  else 0.0
    # avg_rr: average winner / |average loser|; 0.0 when no losers
    avg_rr = (avg_win / abs(avg_loss)) if avg_loss != 0.0 else 0.0
    exit_types: dict = {}
    if "exit_type" in group.columns:
        exit_types = dict(group["exit_type"].value_counts())
    return {
        "count":     len(group),
        "win_rate":  len(winners) / len(group),
        "avg_pnl":   float(pnl.mean()),
        "avg_win":   avg_win,
        "avg_loss":  avg_loss,
        "avg_rr":    avg_rr,
        "exit_types": exit_types,
    }


def compute_fold_stats(df: pd.DataFrame, fold_boundaries: Optional[list] = None) -> list:
    """Return one dict per fold with aligned/misaligned stats.

    fold_boundaries: list of ISO date strings; each adjacent pair defines a fold window.
    When None, infers 6 equal-sized folds from the entry_date distribution.
    """
    if df.empty or "entry_date" not in df.columns:
        return []
    dates = pd.to_datetime(df["entry_date"], errors="coerce").dropna()
    if dates.empty:
        return []
    if fold_boundaries is None:
        # Infer 6 folds via quantile bucketing
        n_folds = 6
        quantiles = [i / n_folds for i in range(n_folds + 1)]
        fold_boundaries = [str(d.date()) for d in dates.quantile(quantiles)]
        if len(set(fold_boundaries)) < 2:
            return []
    results = []
    for i in range(len(fold_boundaries) - 1):
        start = fold_boundaries[i]
        end   = fold_boundaries[i + 1]
        mask  = (df["entry_date"] >= start) & (df["entry_date"] < end)
        fold_df = df[mask]
        groups  = split_by_alignment(fold_df)
        results.append({
            "fold":         i + 1,
            "start":        start,
            "end":          end,
            "trade_count":  len(fold_df),
            "aligned":      compute_group_stats(groups["aligned"]),
            "misaligned":   compute_group_stats(groups["misaligned"]),
            "no_hypothesis": compute_group_stats(groups["no_hypothesis"]),
        })
    return results


def print_analysis(df: pd.DataFrame) -> None:
    """Print full alignment analysis to stdout. ASCII output only."""
    total = len(df)
    groups = split_by_alignment(df)
    n_aligned    = len(groups["aligned"])
    n_misaligned = len(groups["misaligned"])
    n_none       = len(groups["no_hypothesis"])

    print("=" * 60)
    print("HYPOTHESIS ALIGNMENT ANALYSIS")
    print("=" * 60)
    print(f"Total trades : {total}")
    if total > 0:
        print(f"  Aligned    : {n_aligned} ({100*n_aligned/total:.1f}%)")
        print(f"  Misaligned : {n_misaligned} ({100*n_misaligned/total:.1f}%)")
        print(f"  No hypoth. : {n_none} ({100*n_none/total:.1f}%)")
    print()

    # Per-group stats table
    print("-" * 60)
    print(f"{'Group':<14} {'N':>5} {'WR':>7} {'AvgPnL':>8} {'AvgWin':>8} {'AvgRR':>7}")
    print("-" * 60)
    for label, key in [("Aligned", "aligned"), ("Misaligned", "misaligned"), ("No-hyp", "no_hypothesis")]:
        s = compute_group_stats(groups[key])
        print(
            f"{label:<14} {s['count']:>5} {s['win_rate']:>7.1%} "
            f"{s['avg_pnl']:>8.2f} {s['avg_win']:>8.2f} {s['avg_rr']:>7.2f}"
        )
    print("-" * 60)
    print()

    # Per-fold consistency check
    fold_stats = compute_fold_stats(df)
    if fold_stats:
        print("PER-FOLD CONSISTENCY (aligned WR > misaligned WR?)")
        print("-" * 60)
        for f in fold_stats:
            awr = f["aligned"]["win_rate"]
            mwr = f["misaligned"]["win_rate"]
            n_a = f["aligned"]["count"]
            n_m = f["misaligned"]["count"]
            if n_a == 0 or n_m == 0:
                verdict = "N/A"
            elif awr > mwr:
                verdict = "YES"
            else:
                verdict = "NO "
            edge = f"{(awr - mwr)*100:+.1f}pp" if (n_a > 0 and n_m > 0) else "  N/A"
            print(
                f"  Fold {f['fold']} ({f['start']} - {f['end']}) "
                f"aligned={awr:.1%}({n_a}) mis={mwr:.1%}({n_m}) "
                f"edge={edge} [{verdict}]"
            )
        print()

    # Rule decomposition section
    rule_cols = ["pd_range_case", "pd_range_bias", "week_zone", "day_zone", "trend_direction"]
    available = [c for c in rule_cols if c in df.columns and df[c].notna().any()]
    if available and "pnl" in df.columns:
        print("RULE DECOMPOSITION (win rate by value)")
        print("-" * 60)
        for col in available:
            print(f"  {col}:")
            sub = df[[col, "pnl"]].dropna(subset=[col])
            for val, grp in sub.groupby(col):
                wr = (grp["pnl"] > 0).mean()
                print(f"    {str(val):<20} n={len(grp):3d}  WR={wr:.1%}")
        print()
    else:
        print("Rule decomposition: columns not found in trades.tsv (run new backtest first).")
        print()

    # Meaningful-edge verdict
    aligned_stats    = compute_group_stats(groups["aligned"])
    misaligned_stats = compute_group_stats(groups["misaligned"])
    awr = aligned_stats["win_rate"]
    mwr = misaligned_stats["win_rate"]
    edge_pp = (awr - mwr) * 100
    consistent_folds = sum(
        1 for f in fold_stats
        if f["aligned"]["count"] > 0 and f["misaligned"]["count"] > 0
        and f["aligned"]["win_rate"] > f["misaligned"]["win_rate"]
    )
    total_scoreable_folds = sum(
        1 for f in fold_stats
        if f["aligned"]["count"] > 0 and f["misaligned"]["count"] > 0
    )

    print("=" * 60)
    if edge_pp >= 10.0 and consistent_folds >= 4:
        print(f"VERDICT: POTENTIAL EDGE (+{edge_pp:.1f}pp, {consistent_folds}/{total_scoreable_folds} folds consistent)")
    else:
        reasons = []
        if edge_pp < 10.0:
            reasons.append(f"edge only +{edge_pp:.1f}pp (need >=10pp)")
        if consistent_folds < 4:
            reasons.append(f"consistent in {consistent_folds}/{total_scoreable_folds} folds (need >=4)")
        print(f"VERDICT: NO CLEAR EDGE ({'; '.join(reasons)})")
    print("=" * 60)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "trades.tsv"
    df = load_trades(path)
    if df.empty:
        print(f"No trades loaded from {path}")
        sys.exit(1)
    print_analysis(df)
