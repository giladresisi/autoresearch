# regression.py
# Specific-day regression runner for the SMT v2 pipeline.
# Runs run_backtest_v2 for each date in regression.md, writes events.jsonl + trades.tsv,
# and diffs against baselines. Also plots a chart per date.
#
# Default: diff against existing baseline; auto-lock (LOCK) when none exists.
# --skip-lock: when no baseline exists, skip locking and return SKIP.
# --update-baseline: overwrite baseline with current run output.

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import paths


def _git_version() -> str:
    """Short commit + dirty flag for the run's info.md (best-effort; never raises)."""
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=False).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True, check=False).stdout.strip()
        return f"{head or 'unknown'}{' (dirty)' if dirty else ''}"
    except Exception:
        return "unknown"


def _write_run_info(run_dir: Path, date: str, mode: str,
                    started: datetime.datetime, baseline_ref: str) -> None:
    """Write a minimal info.md recording what produced this run's outputs.

    Fields are intentionally minimal for now; the full schema is user-specified later.
    """
    th_start = started.astimezone(ZoneInfo("Asia/Bangkok")).strftime("%H-%M-%S")
    (run_dir / "info.md").write_text(
        f"# Regression run\n\n"
        f"- date: {date}\n"
        f"- mode: {mode}\n"
        f"- th_start: {th_start}\n"
        f"- code_version: {_git_version()}\n"
        f"- baseline_ref: {baseline_ref}\n\n"
        f"<!-- TODO: full info.md field schema to be specified later. -->\n",
        encoding="utf-8",
    )


def _parse_date_tokens(tokens: list[str]) -> list[str]:
    """Expand a list of date strings and YYYY-MM-DD:YYYY-MM-DD ranges into a flat date list."""
    dates: list[str] = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            start_str, end_str = token.split(":", 1)
            date_range = pd.date_range(start_str.strip(), end_str.strip(), freq="D")
            for d in date_range:
                dates.append(d.strftime("%Y-%m-%d"))
        else:
            dates.append(token)
    return dates


def _parse_regression_md(path: str) -> list[str]:
    """Parse regression.md into a flat list of date strings (YYYY-MM-DD)."""
    tokens: list[str] = []
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            token = raw_line.split("#")[0].strip()
            if token:
                tokens.append(token)
    return _parse_date_tokens(tokens)


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
    dates: "list[str] | None" = None,
    record: bool = False,
    update_baseline: "bool | None" = None,
    skip_lock: bool = False,
    no_plot: bool = False,
    mode: str = "1m",
) -> dict:
    """Run regression for every date in regression_md_path (or dates if provided).

    dates: explicit list of date strings / range tokens; overrides regression_md_path when set.
    update_baseline (alias for record) takes precedence when supplied.
    record=True / update_baseline=True: write baseline for each date.
    record=False / update_baseline=False: diff against existing baseline.
    skip_lock=True: when no baseline exists, skip locking (SKIP); default locks (LOCK).
    mode: "1m" (default) or "1s". 1s mode uses _1s suffix for all output/baseline files.

    Returns {date: {events_match, trades_match, n_trades, pnl, updated, locked, skipped}}.
    """
    from backtest_smt import run_backtest_v2

    if update_baseline is not None:
        record = update_baseline

    if dates is not None:
        dates = _parse_date_tokens(dates)
    else:
        dates = _parse_regression_md(regression_md_path)
    results: dict[str, dict] = {}
    _sfx = "_1s" if mode == "1s" else ""
    # One run timestamp for the whole invocation so every date's outputs share a stamp.
    started = datetime.datetime.now(ZoneInfo("America/New_York"))

    for date in dates:
        result = run_backtest_v2(date, date, write_events=True, mode=mode, started=started)
        trades  = result.get("trades", [])
        events  = result.get("events", [])
        metrics = result.get("metrics", {})

        # Run outputs go in a per-run timestamped folder; baselines live at a stable
        # <regression>/<date>/baseline/ so A/B baselines survive across runs.
        run_dir     = paths.regression_run_dir(date, started)
        bl_dir      = paths.regression_sessions_dir() / date / "baseline"
        bl_dir.mkdir(parents=True, exist_ok=True)
        events_path = run_dir / f"events{_sfx}.jsonl"
        trades_path = run_dir / f"trades{_sfx}.tsv"
        bl_events   = bl_dir / f"baseline_events{_sfx}.jsonl"
        bl_trades   = bl_dir / f"baseline_trades{_sfx}.tsv"

        _write_events_jsonl(events_path, events)
        _write_trades_tsv(trades_path, trades)
        _write_run_info(run_dir, date, mode, started, baseline_ref=str(bl_dir))

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
            if skip_lock:
                res = {
                    "events_match": False,
                    "trades_match": False,
                    "n_trades":     metrics.get("n_trades", 0),
                    "pnl":          metrics.get("total_pnl", 0.0),
                    "skipped":      True,
                }
            else:
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
        if not no_plot:
            import webbrowser
            _plot_result = subprocess.run(
                [sys.executable, "regression/plot_regression.py", date, mode, str(run_dir)],
                check=False,
                capture_output=True,
                text=True,
                env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent)),
            )
            if _plot_result.stdout:
                print(_plot_result.stdout, end="")
            _chart_line = next(
                (l for l in _plot_result.stdout.splitlines() if l.startswith("Chart:")),
                None,
            )
            if _chart_line:
                webbrowser.open(Path(_chart_line.split("Chart:", 1)[1].strip()).as_uri())

        results[date] = res

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="SMT v2 regression runner")
    parser.add_argument(
        "--regression-md", default="regression.md",
        help="Path to regression.md (default: regression.md)",
    )
    parser.add_argument(
        "--dates", nargs="+", metavar="DATE_OR_RANGE",
        help="One or more dates (YYYY-MM-DD) or ranges (YYYY-MM-DD:YYYY-MM-DD); "
             "overrides regression.md when specified",
    )
    parser.add_argument(
        "--update-baseline", action="store_true",
        help="Overwrite baseline with current run output instead of diffing",
    )
    parser.add_argument(
        "--skip-lock", action="store_true",
        help="When no baseline exists, skip locking and return SKIP instead of LOCK",
    )
    parser.add_argument(
        "--no-plot", action="store_true",
        help="Skip chart generation",
    )
    parser.add_argument(
        "--mode", choices=["1m", "1s"], default="1m",
        help="Bar mode: 1m (default) or 1s (partial-bar simulation, requires MNQ/MES_1s.parquet)",
    )
    args = parser.parse_args()

    results = run_regression(
        args.regression_md,
        dates=args.dates,
        record=args.update_baseline,
        skip_lock=args.skip_lock,
        no_plot=args.no_plot,
        mode=args.mode,
    )

    all_pass = True
    for date, res in results.items():
        n = res["n_trades"]
        pnl = res["pnl"]
        if res.get("updated"):
            print(f"{date}: updated   n_trades={n} pnl={pnl:.2f}")
        elif res.get("locked"):
            print(f"{date}: events=LOCKED trades=LOCKED n_trades={n} pnl={pnl:.2f}")
        elif res.get("skipped"):
            print(f"{date}: events=SKIP trades=SKIP n_trades={n} pnl={pnl:.2f}")
        else:
            status_e = "PASS" if res["events_match"] else "FAIL"
            status_t = "PASS" if res["trades_match"] else "FAIL"
            print(f"{date}: events={status_e} trades={status_t} n_trades={n} pnl={pnl:.2f}")
            if not res["events_match"] or not res["trades_match"]:
                all_pass = False

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
