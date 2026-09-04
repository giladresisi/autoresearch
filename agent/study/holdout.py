"""The discovery / holdout split, fixed 2026-09-03 before any rule was searched for.

Phase 2 is labelling, which is mechanical and cannot overfit. The split matters for phase 3
— and it is fixed now, while nothing is known, because a split chosen after the first rule
has been seen is not a holdout, it is a second look at the same data.

**Block-interleaved by ISO week, one week in four.** Whole weeks move together because
adjacent sessions share structure: the same prior-week pools, the same open gap, often the
same unfinished business. A per-session split would leak a week's context into the holdout
and quietly inflate every phase-3 score.

`holdout.json` is the record and this function is the rule; a test asserts they agree, so
neither can drift without the other noticing.
"""
from __future__ import annotations

import datetime
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
SKELETON = os.path.join(_REPO, ".agents", "session-skeleton", "session_skeleton.jsonl")
HOLDOUT_JSON = os.path.join(_REPO, ".agents", "label-corpus", "holdout.json")

#: The residue chosen without looking at any outcome. Changing it needs a dated
#: justification in spec 26 and a full re-run of every phase that used the old split.
HOLDOUT_RESIDUE = 0


def is_holdout(date: datetime.date) -> bool:
    """ISO week mod 4. Pure, total, and independent of anything measured."""
    return date.isocalendar()[1] % 4 == HOLDOUT_RESIDUE


def _load_dates() -> "list[datetime.date]":
    seen = []
    with open(SKELETON, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            d = datetime.date.fromisoformat(json.loads(line)["date"])
            if d not in seen:
                seen.append(d)
    return sorted(seen)


ALL_DATES = _load_dates()
