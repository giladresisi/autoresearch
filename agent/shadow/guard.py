"""No-lookahead guard (GIL-44 Phase 2, Wave 2.2).

The permanent safety net between the facts adapter and the LLM call: a snapshot whose
data extends even one bar past the trigger timestamp is a lookahead leak, and a
lookahead decision is worthless (it "knew" the future). assert_no_lookahead raises
LookaheadError loudly; the engine catches it, writes the record with error="lookahead",
and discards the decision WITHOUT calling the API.
"""

from __future__ import annotations


class LookaheadError(Exception):
    """A facts snapshot contains a bar strictly after the trigger timestamp."""


def assert_no_lookahead(snapshot, trigger_ts) -> None:
    """Raise LookaheadError iff the snapshot's max bar timestamp is strictly after
    `trigger_ts`. A bar at EXACTLY trigger_ts is allowed (inclusive; the trigger bar
    is legitimately part of the snapshot)."""
    max_ts = getattr(snapshot, "max_ts", None)
    if max_ts is not None and trigger_ts is not None and max_ts > trigger_ts:
        raise LookaheadError(
            f"snapshot max_ts {max_ts} is after trigger_ts {trigger_ts} — lookahead leak"
        )
