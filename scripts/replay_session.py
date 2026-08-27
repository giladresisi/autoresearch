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
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.trader.replay import DEFAULT_ARRIVAL_LATENCY_SEC, run_replay   # noqa: E402
from agent.trader.thesis_cache import ThesisCache                         # noqa: E402


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
    args = ap.parse_args()

    if args.clear_cache:
        print(f"[replay] cleared {ThesisCache().clear()} recorded theses")
        return 0

    if not args.dates:
        ap.error("--dates is required unless --clear-cache is given")

    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    res = run_replay(dates, allow_calls=args.seed,
                     arrival_latency_sec=args.arrival_latency_sec,
                     arm_hhmm=args.arm_hhmm)
    for d in dates:
        print(f"[replay] {d} done -> {(res.get(d) or {}).get('run_dir')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
