"""Trading-session replay CLI (cycle 2).

Separate from `regression.py` on purpose: that script diffs `events.jsonl` line-for-line
against locked recordings and is the legacy engine's protection. This one has its own
window, its own artifacts, and never writes one.

    python scripts/replay_session.py --dates 2026-08-12
    python scripts/replay_session.py --dates 2026-08-12 --seed      # allow a real call
    python scripts/replay_session.py --clear-cache
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.trader.replay import DEFAULT_ARRIVAL_LATENCY_SEC, run_replay   # noqa: E402
from agent.trader.thesis_cache import ThesisCache                         # noqa: E402
from scripts.report_replay_pnl import (MNQ_PNL_PER_POINT, contracts,      # noqa: E402
                                       render, summarize)


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay a trading session (09:20-11:00 ET).")
    ap.add_argument("--dates", help="comma-separated YYYY-MM-DD")
    ap.add_argument("--seed", action="store_true",
                    help="allow real model calls on a cache miss and record them")
    ap.add_argument("--arrival-latency-sec", type=float,
                    default=DEFAULT_ARRIVAL_LATENCY_SEC)
    ap.add_argument("--arm-hhmm", default=None,
                    help="override the 09:20 arm (fidelity fixtures only)")
    ap.add_argument("--clear-cache", action="store_true",
                    help="delete every recorded thesis, then exit")
    ap.add_argument("--thesis", default=None,
                    help="inline JSON oracle thesis (mutually exclusive with --seed)")
    ap.add_argument("--thesis-file", default=None,
                    help="path to a JSON oracle thesis")
    ap.add_argument("--no-arrival-gate", action="store_true",
                    help="oracle runs only: make the thesis visible at the arm instant")
    ap.add_argument("--operator-control", default=None, metavar="FILE",
                    help="a recorded operator_control.jsonl to replay (plan 41): the "
                         "session's direction/target overrides are applied at the same "
                         "bars they were applied live")
    ap.add_argument("--window-end", default=None, metavar="HH:MM",
                    help="override the window end (default 13:00; the fidelity "
                         "fixtures were calibrated at 11:00)")
    args = ap.parse_args()

    if args.clear_cache:
        print(f"[replay] cleared {ThesisCache().clear()} recorded theses")
        return 0

    if not args.dates:
        ap.error("--dates is required unless --clear-cache is given")

    if args.thesis and args.thesis_file:
        ap.error("--thesis and --thesis-file are mutually exclusive")
    oracle = None
    try:
        if args.thesis:
            oracle = json.loads(args.thesis)
        elif args.thesis_file:
            with open(args.thesis_file, encoding="utf-8") as fh:
                oracle = json.load(fh)
    except (OSError, ValueError) as exc:
        ap.error(f"could not read the oracle thesis: {exc}")
    if oracle is not None and args.seed:
        ap.error("--thesis/--thesis-file cannot be combined with --seed")
    if args.no_arrival_gate and oracle is None:
        # Enforced, not just documented: zeroing the latency on a CACHED run silently
        # changes which bar the thesis becomes visible on, i.e. what the replay
        # reproduces, with no trace in the run dir.
        ap.error("--no-arrival-gate applies to oracle runs only "
                 "(pass --thesis or --thesis-file)")

    window_end = None
    if args.window_end:
        try:
            hh, mm = args.window_end.split(":")
            window_end = (int(hh), int(mm))
        except ValueError:
            ap.error("--window-end takes HH:MM")

    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    res = run_replay(dates, allow_calls=args.seed,
                     arrival_latency_sec=args.arrival_latency_sec,
                     arm_hhmm=args.arm_hhmm, thesis=oracle,
                     gate_arrival=not args.no_arrival_gate, window_end=window_end,
                     operator_control=args.operator_control)

    # The P&L, printed per date and then totalled. Best-effort per date: a report that
    # cannot be built must not hide the run that succeeded, but it must say so rather
    # than leave a blank where a number belongs.
    total = 0.0
    for d in dates:
        run_dir = (res.get(d) or {}).get("run_dir")
        print(f"[replay] {d} done -> {run_dir}")
        try:
            summary = summarize(run_dir)
            total += summary["total_pts"]
            print(render(summary, d))
        except Exception as exc:
            print(f"[pnl] {d}: could not summarize {run_dir}: "
                  f"{type(exc).__name__}: {exc}")
    if len(dates) > 1:
        print(f"[pnl] ALL DATES {total:+.2f} pts = "
              f"${total * MNQ_PNL_PER_POINT * contracts():+.2f} "
              f"at {contracts()} contract(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
