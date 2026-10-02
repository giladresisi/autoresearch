"""A/B harness for the O3 trailing stop (`agent/trader/trail.py`, STUDY, flag OFF).

Same rig as `scripts/ab_micro_smt.py`: the last N trading dates, a LOOKAHEAD ORACLE
thesis per date (direction + DOL from the bars, `ab_micro_smt.oracle_for`), the real
Executor with its real mechanisms and the REAL T2 selected at each fill. Only the trail
knobs differ between arms, flipped as plain module attributes and read live.

One arm per process (`--arm NAME`), so several arms run in parallel on separate cores;
each appends rows to its own JSONL. `--summarize` reads every `<out>.<arm>.jsonl` next
to `--out` and prints the comparison, including WHERE the trades exited (by exit kind).

    python scripts/ab_trail.py --arm A --last-30 --end-date 2026-09-29
    python scripts/ab_trail.py --summarize
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import agent.trader.trail as trail                                        # noqa: E402
from agent.trader.replay import run_replay                                # noqa: E402
from scripts.ab_micro_smt import (_load_jsonl, last_n_trading_dates,      # noqa: E402
                                  oracle_for)
from scripts.report_replay_pnl import MNQ_PNL_PER_POINT, contracts, summarize  # noqa: E402

#: arm -> trail knobs. A is today's behaviour (flag off).
#: B..F pin the break-even knobs OFF / gap moves ON: the module defaults changed
#: 2026-10-01 and those rows must keep meaning the pure gap trail.
_NO_BE = dict(TRAIL_BE_AT_ARM=False, TRAIL_FVG_MOVES=True)
ARMS = {
    "A": dict(TRAIL_ENABLED=False),
    "B-lag1-min5-arm50": dict(TRAIL_ENABLED=True, TRAIL_LAG=1, TRAIL_MIN_GAP_PTS=5.0,
                              TRAIL_ARM_FRACTION=0.5, **_NO_BE),
    "C-lag2-min5-arm50": dict(TRAIL_ENABLED=True, TRAIL_LAG=2, TRAIL_MIN_GAP_PTS=5.0,
                              TRAIL_ARM_FRACTION=0.5, **_NO_BE),
    "D-lag1-min15-arm50": dict(TRAIL_ENABLED=True, TRAIL_LAG=1, TRAIL_MIN_GAP_PTS=15.0,
                               TRAIL_ARM_FRACTION=0.5, **_NO_BE),
    "E-lag1-min5-arm65": dict(TRAIL_ENABLED=True, TRAIL_LAG=1, TRAIL_MIN_GAP_PTS=5.0,
                              TRAIL_ARM_FRACTION=0.65, **_NO_BE),
    "F-lag1-min0-arm50": dict(TRAIL_ENABLED=True, TRAIL_LAG=1, TRAIL_MIN_GAP_PTS=0.0,
                              TRAIL_ARM_FRACTION=0.5, **_NO_BE),
    "G-be50-lag1-min5": dict(TRAIL_ENABLED=True, TRAIL_LAG=1, TRAIL_MIN_GAP_PTS=5.0,
                             TRAIL_ARM_FRACTION=0.5, TRAIL_BE_AT_ARM=True,
                             TRAIL_BE_OFFSET_PTS=0.0, TRAIL_FVG_MOVES=True),
    "H-be50-only": dict(TRAIL_ENABLED=True, TRAIL_ARM_FRACTION=0.5, TRAIL_BE_AT_ARM=True,
                        TRAIL_BE_OFFSET_PTS=0.0, TRAIL_FVG_MOVES=False),
    "I-be50-lag1-min5-armapply": dict(TRAIL_ENABLED=True, TRAIL_LAG=1, TRAIL_MIN_GAP_PTS=5.0,
                                      TRAIL_ARM_FRACTION=0.5, TRAIL_BE_AT_ARM=True,
                                      TRAIL_FVG_MOVES=True, TRAIL_APPLY_AT_ARM=True),
}
_KNOBS = ("TRAIL_APPLY_AT_ARM", "TRAIL_ENABLED", "TRAIL_LAG", "TRAIL_MIN_GAP_PTS", "TRAIL_ARM_FRACTION",
          "TRAIL_BUFFER_PTS", "TRAIL_BE_AT_ARM", "TRAIL_BE_OFFSET_PTS", "TRAIL_FVG_MOVES")


def _exit_kinds(run_dir: str) -> dict:
    out: dict = {}
    path = os.path.join(run_dir, "trader_decisions.jsonl")
    for r in _load_jsonl(path) if os.path.exists(path) else []:
        k = r.get("kind")
        if k in ("stop_moved", "trail_armed"):
            out[k] = out.get(k, 0) + 1
    return out


def run_arm(date: str, thesis: dict, arm: str) -> dict:
    saved = {k: getattr(trail, k) for k in _KNOBS}
    for k, v in ARMS[arm].items():
        setattr(trail, k, v)
    try:
        res = run_replay([date], allow_calls=False, thesis=thesis)
    finally:
        for k, v in saved.items():
            setattr(trail, k, v)
    run_dir = res[date]["run_dir"]
    s = summarize(run_dir)
    n = contracts()
    row = {
        "date": date, "arm": arm, "run_dir": run_dir, "status": s["status"],
        "realised_pts": s["realised_pts"], "marked_pts": s["marked_pts"],
        "total_pts": s["total_pts"], "total_usd": s["total_pts"] * MNQ_PNL_PER_POINT * n,
        "n_trades": s["n_trades"],
        "trades": [{"mechanism": t.get("mechanism"), "entry_ts": t.get("entry_ts"),
                    "entry": t.get("entry"), "exit_ts": t.get("exit_ts"),
                    "exit": t.get("exit"), "exit_kind": t.get("exit_kind"),
                    "points": t.get("points"), "initial": t.get("initial"),
                    "initial_reached": t.get("initial_reached"),
                    "initial_touched": t.get("initial_touched"),
                    "stop_moved_by": t.get("stop_moved_by")} for t in s["trades"]],
        "plan_dead": s["plan_dead"],
    }
    row.update(_exit_kinds(run_dir))
    return row


def _arm_path(out: str, arm: str) -> str:
    base, ext = os.path.splitext(out)
    return f"{base}.{arm}{ext}"


def _done(path: str) -> set:
    return {r["date"] for r in _load_jsonl(path)} if os.path.exists(path) else set()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", choices=sorted(ARMS))
    ap.add_argument("--dates")
    ap.add_argument("--last-30", action="store_true")
    ap.add_argument("--last-n", type=int)
    ap.add_argument("--end-date", default="2026-09-29")
    ap.add_argument("--out", default=os.path.join(_REPO, ".agents", "ab_trail.jsonl"))
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--summarize", action="store_true")
    args = ap.parse_args()
    if args.summarize:
        return _summarize(args.out)
    if not args.arm:
        ap.error("--arm is required (or --summarize)")
    if args.dates:
        dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    elif args.last_n:
        dates = last_n_trading_dates(args.end_date, args.last_n)
    elif args.last_30:
        dates = last_n_trading_dates(args.end_date, 30)
    else:
        ap.error("give --dates, --last-n N or --last-30")
    # Parallel arms rotate their date order so no two processes start the same date in
    # the same second (the replay's run dir is stamped to the second).
    k = sorted(ARMS).index(args.arm)
    dates = dates[k % len(dates):] + dates[:k % len(dates)]
    path = _arm_path(args.out, args.arm)
    done = _done(path) if args.resume else set()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for date in dates:
            if date in done:
                continue
            try:
                got = oracle_for(date)
                thesis = got["thesis"]
                row = run_arm(date, thesis, args.arm)
                row["thesis_source"] = got["source"]
            except Exception as e:
                row = {"date": date, "arm": args.arm, "status": "error",
                       "error": f"{type(e).__name__}: {e}"}
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            if row.get("status") == "error":
                print(f"[ab] {date} {args.arm}: ERROR {row['error']}")
                continue
            print(f"[ab] {date} {args.arm}: {row['total_pts']:+.2f} pts, "
                  f"{row['n_trades']} trades, "
                  + " ".join(f"{t['exit_kind']}" for t in row["trades"]))
    return 0


def _summarize(out: str) -> int:
    base, ext = os.path.splitext(out)
    rows = []
    for p in sorted(glob.glob(f"{base}.*{ext}")):
        rows += _load_jsonl(p)
    if not rows:
        print("no rows")
        return 1
    arms = sorted({r["arm"] for r in rows})
    dates = sorted({r["date"] for r in rows})
    ok = {(r["date"], r["arm"]): r for r in rows if r.get("status") == "ok"}
    common = [d for d in dates if all((d, a) in ok for a in arms)]
    n = contracts()
    print(f"{len(common)} dates with every arm ok (of {len(dates)}); arms: {', '.join(arms)}\n")
    print(f"{'arm':22s} {'pts':>9s} {'usd':>10s} {'trades':>6s}  exits by kind")
    for a in arms:
        pts = sum(ok[(d, a)]["total_pts"] for d in common)
        kinds: dict = {}
        tr = 0
        for d in common:
            for t in ok[(d, a)]["trades"]:
                kinds[t["exit_kind"]] = kinds.get(t["exit_kind"], 0) + 1
                tr += 1
        print(f"{a:22s} {pts:+9.2f} {pts * MNQ_PNL_PER_POINT * n:+10.2f} {tr:6d}  "
              + " ".join(f"{k}={v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])))
    if "A" in arms:
        print("\nper-date delta vs A (pts):")
        print(f"{'date':12s}" + "".join(f"{a[:14]:>15s}" for a in arms if a != "A"))
        for d in common:
            print(f"{d:12s}" + "".join(
                f"{ok[(d, a)]['total_pts'] - ok[(d, 'A')]['total_pts']:+15.2f}"
                for a in arms if a != "A"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
