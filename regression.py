# regression.py
# Specific-day regression runner for the SMT v2 pipeline.
# Runs run_backtest_v2 for each date in regression.md, writes events.jsonl + trades.tsv,
# and diffs against baselines. Also plots a chart per date.
#
# Default: diff against existing baseline.
# --update-baseline: overwrite baseline with current run output.

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd


def _parse_regression_md(path: str) -> list[str]:
    """Parse regression.md into a flat list of date strings (YYYY-MM-DD)."""
    dates: list[str] = []
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.split("#")[0].strip()
            if not line:
                continue
            if ":" in line:
                start_str, end_str = line.split(":", 1)
                date_range = pd.date_range(start_str.strip(), end_str.strip(), freq="D")
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
    record: bool = False,
    update_baseline: "bool | None" = None,
) -> dict:
    """Run regression for every date in regression_md_path.

    update_baseline (alias for record) takes precedence when supplied.
    record=True / update_baseline=True: write baseline for each date.
    record=False / update_baseline=False: diff against existing baseline.

    Returns {date: {events_match, trades_match, n_trades, pnl, updated, locked}}.
    """
    from backtest_smt import run_backtest_v2

    if update_baseline is not None:
        record = update_baseline

    dates = _parse_regression_md(regression_md_path)
    results: dict[str, dict] = {}

    for date in dates:
        result = run_backtest_v2(date, date, write_events=True)
        trades  = result.get("trades", [])
        events  = result.get("events", [])
        metrics = result.get("metrics", {})

        reg_dir     = Path("data") / "regression" / date
        events_path = reg_dir / "events.jsonl"
        trades_path = reg_dir / "trades.tsv"
        bl_events   = reg_dir / "baseline_events.jsonl"
        bl_trades   = reg_dir / "baseline_trades.tsv"

        _write_events_jsonl(events_path, events)
        _write_trades_tsv(trades_path, trades)

        if record:
            shutil.copy2(events_path, bl_events)
            shutil.copy2(trades_path, bl_trades)
            res = {
                "events_match": True,
                "trades_match": True,
                "n_trades":     metrics.get("n_trades", 0),
                "pnl":          metrics.get("total_pnl", 0.0),
                "updated":      True,
            }
        elif not bl_events.exists() or not bl_trades.exists():
            shutil.copy2(events_path, bl_events)
            shutil.copy2(trades_path, bl_trades)
            res = {
                "events_match": True,
                "trades_match": True,
                "n_trades":     metrics.get("n_trades", 0),
                "pnl":          metrics.get("total_pnl", 0.0),
                "locked":       True,
            }
        else:
            res = {
                "events_match": (events_path.read_text(encoding="utf-8").splitlines()
                                 == bl_events.read_text(encoding="utf-8").splitlines()),
                "trades_match": (trades_path.read_text(encoding="utf-8")
                                 == bl_trades.read_text(encoding="utf-8")),
                "n_trades":     metrics.get("n_trades", 0),
                "pnl":          metrics.get("total_pnl", 0.0),
            }

        # Plot chart for this date regardless of record/skip-record mode.
        subprocess.run(
            [sys.executable, "data/regression/plot_regression.py", date],
            check=False,
        )

        results[date] = res

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="SMT v2 regression runner")
    parser.add_argument(
        "--regression-md", default="regression.md",
        help="Path to regression.md (default: regression.md)",
    )
    parser.add_argument(
        "--update-baseline", action="store_true",
        help="Overwrite baseline with current run output instead of diffing",
    )
    args = parser.parse_args()

    results = run_regression(args.regression_md, record=args.update_baseline)

    all_pass = True
    for date, res in results.items():
        n = res["n_trades"]
        pnl = res["pnl"]
        if res.get("updated"):
            print(f"{date}: updated   n_trades={n} pnl={pnl:.2f}")
        elif res.get("locked"):
            print(f"{date}: events=LOCKED trades=LOCKED n_trades={n} pnl={pnl:.2f}")
        else:
            status_e = "PASS" if res["events_match"] else "FAIL"
            status_t = "PASS" if res["trades_match"] else "FAIL"
            print(f"{date}: events={status_e} trades={status_t} n_trades={n} pnl={pnl:.2f}")
            if not res["events_match"] or not res["trades_match"]:
                all_pass = False

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
