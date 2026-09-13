"""JSON communication bus for the v2 standing decisions (spec §10).

`thesis.json` and `trade_plan.json` in the run dir are the standing-state single source of
truth. Writes are atomic (temp + os.replace) so a reader never observes a partial file. IDs
are assigned here (`th_<date>_<seq>` / `pl_<date>_<seq>`) and a plan carries its parent
`thesis_id`, so a thesis death instantly invalidates dependent plans (the executor clears
`trade_plan.json` when it drops the thesis).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def atomic_write_json(path, obj) -> None:
    """Write `obj` as pretty JSON to `path` via a temp file + os.replace (atomic on
    Windows and POSIX). A crash mid-write leaves the prior file intact; a reader sees
    either the whole old file or the whole new one, never a torn write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path) -> Optional[dict]:
    path = Path(path)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class DecisionBus:
    """Owns thesis.json / trade_plan.json for one run. Sequence counters mint stable,
    monotonic IDs; parent linkage lives in the plan's `thesis_id`."""

    def __init__(self, run_dir, date: str = ""):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.date = date
        self.thesis_path = self.run_dir / "thesis.json"
        self.plan_path = self.run_dir / "trade_plan.json"
        self._thesis_seq = 0
        self._plan_seq = 0

    def publish_thesis(self, block: dict, *, issued_at=None, facts_hash=None) -> dict:
        """Stamp an id + linkage fields onto an L1 block and atomically publish it. A new
        thesis supersedes any standing plan (its parent is gone) — the plan file is cleared."""
        self._thesis_seq += 1
        obj = dict(block)
        obj["thesis_id"] = f"th_{self.date}_{self._thesis_seq:03d}"
        obj["issued_at"] = issued_at
        obj["facts_hash"] = facts_hash
        atomic_write_json(self.thesis_path, obj)
        self.invalidate_plan()
        return obj

    def publish_plan(self, block: dict, *, thesis_id: str, issued_at=None,
                     facts_hash=None) -> dict:
        """Stamp an id + parent `thesis_id` onto an L2 block and atomically publish it."""
        self._plan_seq += 1
        obj = dict(block)
        obj["plan_id"] = f"pl_{self.date}_{self._plan_seq:03d}"
        obj["thesis_id"] = thesis_id
        obj["issued_at"] = issued_at
        obj["facts_hash"] = facts_hash
        atomic_write_json(self.plan_path, obj)
        return obj

    def invalidate_plan(self) -> None:
        """Remove the standing plan (its parent thesis died). Idempotent."""
        try:
            self.plan_path.unlink()
        except FileNotFoundError:
            pass

    def read_thesis(self) -> Optional[dict]:
        return read_json(self.thesis_path)

    def read_plan(self) -> Optional[dict]:
        return read_json(self.plan_path)
