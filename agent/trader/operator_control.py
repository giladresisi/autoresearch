"""The operator's control channel into a RUNNING session (plan 41).

`trade.py` appends one JSON record per command to `<state_dir>/operator_control.jsonl`;
the graft drains the pending ones on a bar close and applies them. A file, not a socket
or a signal, for three reasons:

  * **The CLI is a separate process.** It cannot reach the Executor's objects, and the
    agent must never import `live_orders` (CLAUDE.md), so the two halves meet on disk.
  * **The record IS the reproduction.** Every drained record is applied at a BAR instant
    and recorded in `trader_decisions.jsonl`; a replay handed the same file reproduces
    the session, overrides included. A signal would leave no trace.
  * **Ordering is explicit.** `seq` is monotonic per session and the graft remembers the
    last one it applied, so a re-read cannot double-apply and a command written while the
    loop was busy is never lost.

Nothing here reads a clock: the CLI stamps `created_at` (it is outside the bar loop, where
a wall clock is legitimate) and the graft stamps the bar time it applied the record at.
"""
from __future__ import annotations

import json
import os

CONTROL_FILE = "operator_control.jsonl"

#: The commands the graft knows. A record with any other kind is REJECTED and recorded as
#: such rather than ignored, so a typo in a live session is visible rather than silent.
KIND_SET_DIRECTION = "set_direction"
KIND_SET_TARGET = "set_target"
KIND_RESET_TARGET = "reset_target"
KINDS = (KIND_SET_DIRECTION, KIND_SET_TARGET, KIND_RESET_TARGET)


class OperatorControl:
    """Append-only reader/writer. Every read degrades to "nothing pending": a malformed
    or half-written line must not take down the bar loop."""

    def __init__(self, state_dir) -> None:
        self.state_dir = str(state_dir)

    @property
    def path(self) -> str:
        return os.path.join(self.state_dir, CONTROL_FILE)

    # -- writer (the CLI) ------------------------------------------------------ #

    def append(self, record: dict) -> dict:
        """Append `record` with the next `seq`. Returns the written record."""
        rec = dict(record or {})
        rec["seq"] = self.next_seq()
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
        return rec

    def next_seq(self) -> int:
        seqs = [int(r.get("seq") or 0) for r in self.all()]
        return (max(seqs) + 1) if seqs else 1

    # -- reader (the graft) ---------------------------------------------------- #

    def all(self) -> list:
        out = []
        try:
            if not os.path.exists(self.path):
                return out
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue          # half-written tail: the next drain sees it
                    if isinstance(rec, dict):
                        out.append(rec)
        except Exception:
            return out
        return out

    def pending(self, after_seq: int) -> list:
        """Records with `seq > after_seq`, oldest first."""
        recs = [r for r in self.all() if int(r.get("seq") or 0) > int(after_seq or 0)]
        return sorted(recs, key=lambda r: int(r.get("seq") or 0))
