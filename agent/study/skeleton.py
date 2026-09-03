"""Spec §2's structural vocabulary: the 09:30 move, decomposed.

**Why this does NOT use `legs.segment_legs`.** The first implementation mapped one leg
onto one move and it was wrong by a wide margin: `segment_legs` closes a leg on a retrace
beyond `max(30 pts, 25% of range)`, and a single MNQ 5m bar in this era routinely spans
more than 30 points, so the ZigZag flips almost every bar. Over 87 sessions it produced
~28 "moves" apiece — one every 7.6 minutes — and on 2026-08-13 it called the move over at
09:35 with 177 of the day's 404 points. The detector is not broken; it answers the
Executor's question (which trend is there to reverse or ride) and that is a different
question from this one.

**The rule that replaced it: a move ends where it stops extending.** Track the running
extreme; the move ends at that extreme once no new extreme appears within
`NO_EXTENSION_HORIZON`. Move-start is then the opposite extreme preceding it.

The horizon is a stated parameter, and the case for its value is that its value does not
matter: at 20, 30, 45 and 60 minutes it returns *identically* 10:35 on 2026-08-13 and
09:47 on 2026-08-11 — the two sessions whose structure was established by hand. A
parameter insensitive across a 3x range is not the thing doing the work. Contrast the
retrace-fraction rule this replaced, where 08-13's own numbers (59.5 pts of internal
drawdown against 163 after the top) had to be measured before any threshold could be
defended at all.

**Direction falls out rather than being assumed.** Both directions are evaluated from the
window start; whichever terminal extreme forms LATER is the move, and the earlier opposite
extreme is its origin. On 08-13 the low prints at 09:30 and the high at 10:35 (an up
move); on 08-11 the high prints at 09:30 and the low at 09:47 (a down move). Same rule,
no special-casing.

Roles, per spec §4.3: the first move is the PRIMARY labelled unit. Later moves sharing its
direction are SECONDARY continuations, kept separate so the primary label stays clean of
extensions no fact at entry could have anticipated. Opposing moves are COUNTER
observations, carried because §7's switching logic will need them.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

STATUS_OK = "ok"
STATUS_NO_MOVE = "no_move"

ROLE_PRIMARY = "primary"
ROLE_SECONDARY = "secondary"
ROLE_COUNTER = "counter"

#: A move ends at its extreme once nothing exceeds it for this long. See the module
#: docstring: the value is insensitive across 20-60 min on the hand-established cases.
NO_EXTENSION_HORIZON = pd.Timedelta(minutes=30)

#: Below this a "move" is noise. Raised 50 -> 100 on 2026-09-02: a move we could never
#: capture in full is not one worth labelling a target for, and the three sessions the
#: 50-pt floor admitted (54.8 / 60.2 / 67.0 pts) are not tradeable moves.
MIN_MOVE_PTS = 100.0

#: A tiered give-back rule — tightening the tolerated retracement as a move matures —
#: was implemented here on 2026-09-02 and REVERTED the same day. It is a good exit
#: policy and a bad move-end definition, and the measurements say so plainly: median
#: primary extent fell 253.2 -> 185.0, eleven primaries collapsed into a single 5m bar,
#: fourteen segment pairs ran consecutively in the SAME direction (a move ending and
#: immediately continuing is a premature cut by definition), and 2026-08-11 — the
#: session established by hand — ended 42 points short of the day low it was drawn to.
#: Labelling the DOL against a move we truncated ourselves defeats the study. The rule
#: survives in spec 26 as the exit-policy candidate for the later phase, where "past the
#: secondary cautious target" is implementable and where it belongs.


@dataclass(frozen=True)
class Segment:
    index: int
    role: str
    direction: str
    start_ts: pd.Timestamp
    start_price: float
    extreme_ts: pd.Timestamp
    extreme_price: float
    end_ts: pd.Timestamp
    extent: float
    closed: bool

    def to_dict(self) -> dict:
        return {"index": self.index, "role": self.role, "direction": self.direction,
                "start_ts": self.start_ts.isoformat(), "start_price": self.start_price,
                "extreme_ts": self.extreme_ts.isoformat(),
                "extreme_price": self.extreme_price,
                "end_ts": self.end_ts.isoformat(), "extent": self.extent,
                "closed": self.closed}


@dataclass(frozen=True)
class Skeleton:
    date: object
    status: str
    direction: "str | None"
    censored: bool
    segments: tuple

    def to_rows(self) -> "list[dict]":
        """One row per segment; one bare row when no qualifying move exists, so a
        no-move session is VISIBLE in the corpus rather than absent from it."""
        base = {"date": str(self.date), "status": self.status,
                "direction": self.direction, "censored": self.censored}
        if not self.segments:
            return [dict(base, index=None, role=None)]
        return [dict(base, **s.to_dict()) for s in self.segments]


def _terminal_extreme(bars: pd.DataFrame, direction: str, horizon: pd.Timedelta):
    """Walk forward tracking the running extreme; the move ends at that extreme once
    `horizon` elapses with nothing beyond it.

    **This rule uses lookahead, deliberately.** At the instant the extreme prints, it is
    unknowable that it is the extreme — confirmation arrives 30 minutes later. That is
    correct here and it never ships: move-end is the study's DEPENDENT VARIABLE, the
    thing being explained, not a signal anything acts on. Spec 26 §1.1 sanctions exactly
    this, and §10 draws the line it must not cross: the PREDICTORS — candidate universe,
    prior-state facts, class assignment — are all evaluated at or before the entry
    instant, and a rule that needs to know how the move ended is not a rule.

    Returns `(extreme_price, extreme_ts, extended_to_window_end)`. The third value is
    what `censored` is built from: a move whose extreme was still being extended when
    the bars ran out never satisfied the no-extension test, and reporting it as a
    completed move would fabricate an ending the tape never showed.
    """
    col = "High" if direction == "up" else "Low"
    best, best_ts = None, None
    for ts, row in bars.iterrows():
        v = float(row[col])
        if best is None or (v > best if direction == "up" else v < best):
            best, best_ts = v, ts
        elif (ts - best_ts) > horizon:
            return best, best_ts, False
    return best, best_ts, True


def _origin(bars: pd.DataFrame, direction: str, upto_ts: pd.Timestamp):
    """The opposite extreme preceding `upto_ts` — spec §2's move-start."""
    head = bars.loc[:upto_ts]
    if not len(head):
        return None, None
    col = "Low" if direction == "up" else "High"
    series = head[col]
    ts = series.idxmin() if direction == "up" else series.idxmax()
    return float(series.loc[ts]) if not hasattr(series.loc[ts], "__len__") \
        else float(series.loc[ts].iloc[0]), ts


def _next_move(bars: pd.DataFrame, horizon: pd.Timedelta, forced_origin=None):
    """The next move in `bars`: whichever direction's terminal extreme forms LATER.

    `forced_origin` is `(price, ts)` — the previous segment's extreme, which IS this
    segment's origin. Passing it is what makes segments chain exactly; deriving the
    origin from `bars` instead would re-search a window that no longer contains it.

    Returns a dict, or None when neither direction produces a qualifying move.
    """
    if len(bars) < 2:
        return None

    best = None
    for direction in ("up", "down"):
        price, ts, open_ended = _terminal_extreme(bars, direction, horizon)
        if ts is None:
            continue
        if best is None or ts > best["extreme_ts"]:
            best = {"direction": direction, "extreme_price": price,
                    "extreme_ts": ts, "open_ended": open_ended}
    if best is None:
        return None

    if forced_origin is not None:
        start_price, start_ts = forced_origin
        # Direction is dictated by the chain, not re-derived: a segment runs from the
        # previous extreme to this one.
        best["direction"] = "up" if best["extreme_price"] > start_price else "down"
    else:
        start_price, start_ts = _origin(bars, best["direction"], best["extreme_ts"])
    if start_ts is None:
        return None
    extent = abs(best["extreme_price"] - start_price)
    if extent < MIN_MOVE_PTS:
        return None

    best.update({"start_price": start_price, "start_ts": start_ts,
                 "extent": round(extent, 4)})
    return best


def session_skeleton(bars_5m: pd.DataFrame, date, ticker: str = "MNQ") -> Skeleton:
    """5m session bars -> the §2 skeleton. Total: never raises on a degenerate frame."""
    empty = Skeleton(date=date, status=STATUS_NO_MOVE, direction=None,
                     censored=False, segments=())
    if bars_5m is None or not len(bars_5m):
        return empty

    # Segments chain exactly: each one's origin IS the previous one's extreme, carried
    # forward explicitly rather than re-derived. The original implementation searched a
    # window starting one bar past the extreme, so on 2026-08-13 the primary ended
    # 10:35 @ 30267.00 while the counter began 10:40 @ 30260.75 — the segments did not
    # meet and every extent after the first was understated by a bar. Searching STRICTLY
    # after the extreme while forcing the origin gives both exact chaining and
    # guaranteed progress; an inclusive slice gives chaining but can stall.
    moves, rest, carry = [], bars_5m, None
    while len(rest) >= 2:
        mv = _next_move(rest, NO_EXTENSION_HORIZON, forced_origin=carry)
        if mv is None:
            break
        moves.append(mv)
        carry = (mv["extreme_price"], mv["extreme_ts"])
        after = bars_5m.loc[mv["extreme_ts"]:]
        rest = after.iloc[1:] if len(after) else after.iloc[0:0]

    if not moves:
        return empty

    primary_dir = moves[0]["direction"]
    segments = []
    for i, mv in enumerate(moves):
        if i == 0:
            role = ROLE_PRIMARY
        elif mv["direction"] == primary_dir:
            role = ROLE_SECONDARY
        else:
            role = ROLE_COUNTER
        segments.append(Segment(
            index=i, role=role, direction=mv["direction"],
            start_ts=mv["start_ts"], start_price=mv["start_price"],
            extreme_ts=mv["extreme_ts"], extreme_price=mv["extreme_price"],
            end_ts=mv["extreme_ts"], extent=mv["extent"],
            closed=not mv["open_ended"]))

    return Skeleton(date=date, status=STATUS_OK, direction=primary_dir,
                    censored=not segments[0].closed, segments=tuple(segments))
