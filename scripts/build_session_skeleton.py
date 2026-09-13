"""Build the cycle-4 session skeleton over every study session.

Output is deliberately TWO files: a JSONL row per segment for analysis, and a summary
JSON for the human gate. The summary is what tells you at a glance whether the
definitions are behaving -- a no-move count of 40 out of 87 would mean the leg
qualification is wrong for this window, and that is a finding to act on BEFORE
anything is labelled on top.

A no-move session is written as a bare row rather than omitted. An absent session and
a session with nothing in it are different facts, and a corpus that cannot tell them
apart hides its own coverage gaps.
"""
from __future__ import annotations

import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study.sessions import SessionBars           # noqa: E402
from agent.study.skeleton import session_skeleton      # noqa: E402

JSONL_NAME = "session_skeleton.jsonl"
SUMMARY_NAME = "session_skeleton_summary.json"


def build(out_dir: str, dates=None) -> dict:
    bars = SessionBars()
    dates = list(dates) if dates is not None else bars.dates()

    rows, skeletons = [], []
    for d in dates:
        sk = session_skeleton(bars.window_5m("MNQ", d), d)
        skeletons.append(sk)
        rows.extend(sk.to_rows())

    ok = [s for s in skeletons if s.status == "ok"]
    summary = {
        "sessions": len(skeletons),
        "no_move": sum(1 for s in skeletons if s.status == "no_move"),
        "censored": sum(1 for s in ok if s.censored),
        "direction_up": sum(1 for s in ok if s.direction == "up"),
        "direction_down": sum(1 for s in ok if s.direction == "down"),
        "segments_total": sum(len(s.segments) for s in skeletons),
        "secondary_sessions": sum(
            1 for s in ok if any(g.role == "secondary" for g in s.segments)),
        "generated_from": "MNQ 1m -> 5m, 09:30-13:00 ET",
    }

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, JSONL_NAME), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")
    with open(os.path.join(out_dir, SUMMARY_NAME), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
    return summary


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else ".agents/session-skeleton"
    s = build(out)
    print(json.dumps(s, indent=2, sort_keys=True))
