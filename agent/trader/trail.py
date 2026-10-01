"""O3 (2026-10-01): a trailing stop under the 1m continuation FVGs — STUDY ONLY, flag OFF.

The operator's rule (`<global>/sessions/2026-09-30/o3-trail-study.md`): once price has
reached the midpoint between the entry and the T2 target, every time a NEW 1m
continuation FVG becomes visible (its third bar closes), move the stop to just before the
PREVIOUS continuation FVG — the last one created before the new one. Ratchet only.

What the oracle study found (50 far-target days): a profit lock that captures ~60% of the
move, never loses once armed, and does better one gap behind than on the newest gap and
with gaps under 5 pts ignored. This module exists so the same rule can run on the replay
rig with the REAL T2 targets, flag-gated like `micro_smt`'s rules: every knob is a plain
module attribute read LIVE at call time, so an A/B harness flips them between
`run_replay` calls in one process. `TRAIL_ENABLED = False` is today's behaviour exactly.

Break-even at the arming instant (operator decision, 2026-10-01): the tick the position
reaches the mid, the stop moves to the entry (`TRAIL_BE_AT_ARM`, `TRAIL_BE_OFFSET_PTS` in
the trade's favour) if that tightens it, BEFORE any gap move; the gap trail then ratchets
from there. Why: in the 30-date rig 5 of the 21 positions that reached the mid had 0-1
continuation gaps, so the gap rule had nothing to move to; break-even at that instant saved
three losses and cost two trailed winners (o3-trail-study.md Part 3). `TRAIL_FVG_MOVES =
False` switches the gap moves off so break-even can be measured alone.

Not a rule yet (`docs/entry-mechanism-change-protocol.md` step 0): it is not in
`l2-mechanisms.md` and must not be switched on in live until it is.
"""
from __future__ import annotations

import pandas as pd

TRAIL_ENABLED = False
#: 1 = the gap BEFORE the newest one (the operator's rule); 0 = the newest gap.
TRAIL_LAG = 1
#: Continuation gaps smaller than this are ignored (the study's `min5` variant; 0 = all).
TRAIL_MIN_GAP_PTS = 5.0
#: "Just before" the gap's protective edge, in points.
TRAIL_BUFFER_PTS = 3.0
#: Arm once price has covered this fraction of the way from the entry to T2 (0.5 = the
#: midpoint, the operator's rule; 0.0 = trail from the fill; 0.65 = a looser variant).
TRAIL_ARM_FRACTION = 0.5
#: Move the stop to break-even on the tick that arms (before any gap move), if that tightens it.
TRAIL_BE_AT_ARM = True
#: Break-even = the entry plus/minus this many points in the trade's favour (0 = the entry).
TRAIL_BE_OFFSET_PTS = 0.0
#: False = no gap moves (the gaps are still counted); isolates the break-even rule.
TRAIL_FVG_MOVES = True

_LONG = ("UP", "LONG")


def is_long(direction) -> bool:
    return str(direction or "").upper() in _LONG


def completed_1m(frame: pd.DataFrame, start, now) -> pd.DataFrame:
    """The COMPLETED 1m bars of `frame` from the minute `start` up to (excluding) the
    minute containing `now`. The frame may be 1s or 1m bars; both resample the same."""
    if frame is None or not len(frame):
        return pd.DataFrame()
    lo = pd.Timestamp(start).floor("1min")
    hi = pd.Timestamp(now).floor("1min")
    seg = frame[(frame.index >= lo) & (frame.index < hi)]
    if not len(seg):
        return pd.DataFrame()
    m1 = pd.DataFrame({"Open": seg["Open"].resample("1min").first(),
                       "High": seg["High"].resample("1min").max(),
                       "Low": seg["Low"].resample("1min").min(),
                       "Close": seg["Close"].resample("1min").last()}).dropna()
    return m1


def continuation_gaps(m1: pd.DataFrame, direction, min_size: float) -> list:
    """Three-bar 1m gaps in the trade direction, oldest first. Each: the middle bar's
    label, the protective `edge` (the first bar's High for a long, its Low for a short),
    the size, and `visible` = the close of the third bar."""
    out = []
    if m1 is None or len(m1) < 3:
        return out
    hi, lo, idx = m1["High"].values, m1["Low"].values, m1.index
    long_ = is_long(direction)
    for i in range(1, len(m1) - 1):
        if long_ and hi[i - 1] < lo[i + 1]:
            size = float(lo[i + 1] - hi[i - 1])
            edge = float(hi[i - 1])
        elif not long_ and lo[i - 1] > hi[i + 1]:
            size = float(lo[i - 1] - hi[i + 1])
            edge = float(lo[i - 1])
        else:
            continue
        if size < float(min_size):
            continue
        out.append({"mid": idx[i], "edge": edge, "size": size,
                    "visible": idx[i + 1] + pd.Timedelta(minutes=1)})
    return out


class TrailStage:
    """The trail of ONE position. Built at the fill with the entry and the T2 target;
    `arm` is tested every tick, `on_bar_close` every completed 1m bar."""

    def __init__(self, direction, entry, target, opened_at) -> None:
        self.direction = direction
        self.long = is_long(direction)
        self.entry = float(entry)
        self.target = float(target)
        self.opened_at = opened_at
        self.mid = self.entry + float(TRAIL_ARM_FRACTION) * (self.target - self.entry)
        self.armed = float(TRAIL_ARM_FRACTION) <= 0.0
        self.armed_at = None
        self.n_seen = 0
        self.moves = 0

    def arm(self, now, hi, lo) -> bool:
        """True on the tick that arms it."""
        if self.armed or hi is None or lo is None:
            return False
        reached = (float(hi) >= self.mid) if self.long else (float(lo) <= self.mid)
        if reached:
            self.armed = True
            self.armed_at = now
        return reached

    def breakeven_stop(self, current_stop) -> "float | None":
        """The break-even stop to move to at arming, or None when the knob is off or it
        would not tighten `current_stop` (None current stop counts as tightening)."""
        if not TRAIL_BE_AT_ARM:
            return None
        off = float(TRAIL_BE_OFFSET_PTS)
        cand = self.entry + off if self.long else self.entry - off
        if current_stop is not None:
            tighter = (cand > float(current_stop)) if self.long else (cand < float(current_stop))
            if not tighter:
                return None
        return cand

    def on_bar_close(self, now, frame, current_stop) -> "dict | None":
        """A new protective stop to move to, or None. Reads the gaps visible by `now`
        since the fill's minute; moves only when a NEW gap became visible, to the gap
        `TRAIL_LAG` behind it, and only if that tightens the stop."""
        m1 = completed_1m(frame, self.opened_at, now)
        gaps = continuation_gaps(m1, self.direction, TRAIL_MIN_GAP_PTS)
        n_new = len(gaps) - self.n_seen
        self.n_seen = max(self.n_seen, len(gaps))
        if n_new <= 0 or not self.armed or not TRAIL_FVG_MOVES:
            return None
        k = len(gaps) - 1 - int(TRAIL_LAG)
        if k < 0:
            return None
        ref = gaps[k]
        buf = float(TRAIL_BUFFER_PTS)
        cand = ref["edge"] - buf if self.long else ref["edge"] + buf
        if current_stop is not None:
            tighter = (cand > float(current_stop)) if self.long else (cand < float(current_stop))
            if not tighter:
                return None
        self.moves += 1
        return {"stop": cand, "gap": ref["mid"], "gap_edge": ref["edge"],
                "gap_size": ref["size"], "n_gaps": len(gaps)}

    def state(self) -> dict:
        return {"mid": self.mid, "armed": self.armed,
                "armed_at": str(self.armed_at) if self.armed_at is not None else None,
                "gaps_seen": self.n_seen, "moves": self.moves}
