"""Score one replay run folder by the operator's two criteria (`agent/study/corpus.py`).

    uv run python scripts/score_run.py regression/sessions/2026-10-01/23-56-20
    uv run python scripts/score_run.py <run_dir> --retrace-pts 30 --retrace-fraction 0.25 --json

Per trade: the ENTRY criterion (MFE up to the stop, a meaningful retrace or the window
end, against the stop the mechanism places) and the TARGET criterion (points from the
ideal entry — the origin of the leg that led to the exit — to the actual exit). The
session P&L is printed last, as context.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.study import corpus  # noqa: E402


def _hms(ts) -> str:
    return str(ts)[11:19] if ts else "-"


def render(s: dict) -> str:
    out = [f"[score] {s['date']} {s['run_dir']}",
           f"[score] retrace threshold: max({s['retrace']['pts']} pts, "
           f"{s['retrace']['fraction']:.0%} of the leg)",
           "[score] " + "-" * 100]
    for t in s["trades"]:
        e, x = t["entry_score"], t.get("exit_score") or {}
        out.append(
            f"[score] {_hms(t['entry_ts'])} {str(t['direction']):4} @ {t['entry']:<9} "
            f"{str(t['mechanism']):22} ENTRY: MFE {e['mfe']:>7.2f} ({e['r_multiple']}R vs "
            f"{e['stop_pts']:.2f} stop) ended by {e['ended_by']} {_hms(e['ended_at'])}"
            f"{'' if e['stop_survived'] else '  STOP HIT'}")
        if x:
            out.append(
                f"[score]          exit {_hms(t['exit_ts'])} {str(t['exit_kind']):14} @ {t['exit']:<9} "
                f"TARGET: {x.get('ideal_points'):>+8.2f} pts from the ideal-entry proxy "
                f"{x['ideal_entry']} ({_hms(x['ideal_entry_ts'])}, needs a {x.get('ideal_stop_needed')} stop) "
                f"| actual {t['points']:+.2f}")
        else:
            out.append(f"[score]          no exit (open at the window end)")
    out.append("[score] " + "-" * 100)
    out.append(f"[score] ENTRY  sum of MFE {s['entry_mfe_sum']:+.2f} over {s['n_trades']} entries, "
               f"stops survived {s['stops_survived']}/{s['n_trades']}")
    out.append(f"[score] TARGET sum of ideal-entry points {s['ideal_points_sum']:+.2f} "
               f"(proxy: farthest price of the leg, re-anchored on a {s['retrace']['pts']:.0f}-pt retrace; "
               f"no operator rule exists)")
    out.append(f"[score] session P&L (context only): {s['total_pts']:+.2f} pts "
               f"(realised {s['realised_pts']:+.2f}, marked {s['marked_pts']:+.2f}); "
               f"vetoes {s['vetoes'] or '{}'}; plan_dead {s['plan_dead'] or '-'}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir")
    ap.add_argument("--date", help="session date; default: the run folder's parent name")
    ap.add_argument("--retrace-pts", type=float, default=corpus.DEFAULT_RETRACE_PTS)
    ap.add_argument("--retrace-fraction", type=float, default=corpus.DEFAULT_RETRACE_FRACTION)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    s = corpus.score_run(args.run_dir, args.date, retrace_pts=args.retrace_pts,
                         retrace_fraction=args.retrace_fraction)
    print(json.dumps(s, indent=1, default=str) if args.json else render(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
