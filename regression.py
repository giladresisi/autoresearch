# regression.py
# Specific-day regression runner for the SMT v2 pipeline.
# Runs run_backtest_v2 for each date in regression.md, writes events.jsonl + trades.tsv,
# and diffs against committed baselines. Exit 0 if all pass; exit 1 on any diff.

import argparse
import json
import shutil
import sys
from pathlib import Path

import pandas as pd


def _parse_regression_md(path: str) -> list[str]:
    """Parse regression.md into a flat list of date strings (YYYY-MM-DD)."""
    dates: list[str] = []
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            # Strip inline comments and whitespace
            line = raw_line.split("#")[0].strip()
            if not line:
                continue
            if ":" in line:
                # Inclusive date range — all calendar days (run_backtest_v2 skips non-trading days)
                start_str, end_str = line.split(":", 1)
                start_str = start_str.strip()
                end_str = end_str.strip()
                date_range = pd.date_range(start_str, end_str, freq="D")
                for d in date_range:
                    dates.append(d.strftime("%Y-%m-%d"))
            else:
                dates.append(line)
    return dates


def _write_events_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(evt, sort_keys=True) for evt in events]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _write_trades_tsv(path: Path, trades: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not trades:
        path.write_text("", encoding="utf-8")
        return
    headers = list(trades[0].keys())
    rows = ["\t".join(str(t.get(h, "")) for h in headers) for t in trades]
    path.write_text("\t".join(headers) + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def run_regression(
    regression_md_path: str = "regression.md",
    *,
    update_baseline: bool = False,
) -> dict:
    """Run regression for every date in regression_md_path.

    Returns {date: {events_match: bool, trades_match: bool, n_trades: int, pnl: float}}.
    When update_baseline=True, copies current output to baseline files and skips diff.
    """
    from backtest_smt import run_backtest_v2

    dates = _parse_regression_md(regression_md_path)
    results: dict[str, dict] = {}

    for date in dates:
        result = run_backtest_v2(date, date, write_events=True)
        trades = result.get("trades", [])
        events = result.get("events", [])
        metrics = result.get("metrics", {})

        reg_dir = Path("data") / "regression" / date
        events_path  = reg_dir / "events.jsonl"
        trades_path  = reg_dir / "trades.tsv"
        bl_events    = reg_dir / "baseline_events.jsonl"
        bl_trades    = reg_dir / "baseline_trades.tsv"

        # Ensure files are written (run_backtest_v2 does this with write_events=True,
        # but we also write here to guarantee sort_keys=True canonical form)
        _write_events_jsonl(events_path, events)
        _write_trades_tsv(trades_path, trades)

        if update_baseline:
            shutil.copy2(events_path, bl_events)
            shutil.copy2(trades_path, bl_trades)
            results[date] = {
                "events_match": True,
                "trades_match": True,
                "n_trades":     metrics.get("n_trades", 0),
                "pnl":          metrics.get("total_pnl", 0.0),
                "updated":      True,
            }
            continue

        # Auto-lock: if no baseline exists yet, create it and report LOCKED
        if not bl_events.exists() or not bl_trades.exists():
            shutil.copy2(events_path, bl_events)
            shutil.copy2(trades_path, bl_trades)
            results[date] = {
                "events_match": True,
                "trades_match": True,
                "n_trades":     metrics.get("n_trades", 0),
                "pnl":          metrics.get("total_pnl", 0.0),
                "locked":       True,
            }
            continue

        # Canonical comparison
        current_events_lines = events_path.read_text(encoding="utf-8").splitlines()
        current_trades_text  = trades_path.read_text(encoding="utf-8")

        baseline_events_lines = bl_events.read_text(encoding="utf-8").splitlines()
        baseline_trades_text  = bl_trades.read_text(encoding="utf-8")

        results[date] = {
            "events_match": current_events_lines == baseline_events_lines,
            "trades_match": current_trades_text == baseline_trades_text,
            "n_trades":     metrics.get("n_trades", 0),
            "pnl":          metrics.get("total_pnl", 0.0),
        }

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="SMT v2 regression runner")
    parser.add_argument(
        "--regression-md", default="regression.md",
        help="Path to regression.md (default: regression.md)",
    )
    parser.add_argument(
        "--update-baseline", action="store_true",
        help="Update baseline files instead of diffing",
    )
    args = parser.parse_args()

    results = run_regression(args.regression_md, update_baseline=args.update_baseline)

    all_pass = True
    for date, res in results.items():
        if res.get("updated"):
            print(f"{date}: baseline updated (n_trades={res['n_trades']}, pnl={res['pnl']:.2f})")
        elif res.get("locked"):
            print(f"{date}: events=LOCKED trades=LOCKED n_trades={res['n_trades']} pnl={res['pnl']:.2f}")
        else:
            status_e = "PASS" if res["events_match"] else "FAIL"
            status_t = "PASS" if res["trades_match"] else "FAIL"
            print(
                f"{date}: events={status_e} trades={status_t} "
                f"n_trades={res['n_trades']} pnl={res['pnl']:.2f}"
            )
            if not res["events_match"] or not res["trades_match"]:
                all_pass = False

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
