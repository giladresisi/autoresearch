"""CANDIDATE (2026-10-03, operator; `l2-mechanisms.md` §11.8, plan 50): a follow-up entry
after the MES-sweep stop (§11.7) is touched in profit.

The sweep stop is MEANT to be hit: it exits as the retrace starts, and MNQ then went on to
make a new extreme in 84% of the studied cases. A profitable exit ends the plan
(`positive_close`), so that continuation was not traded. This stage keeps the plan alive
for a short window and re-enters on one of two triggers anchored at the RETRACE low
instead of the day extreme:

  followup_reject_close  §7's machine (`extreme_reject.ExtremeReject`), a FRESH instance
                         with no standing extreme: its first completed 1m bar from the
                         exit's minute on sets the extreme, three quiet closes arm it, and
                         a new-extreme bar closing with the thesis fires. Stop = the wick
                         capped 15, exactly §7's.
  followup_fvg_1m        the first thesis-direction 1m FVG (three bars, §2's convention)
                         whose FIRST bar opens at or after the exit's minute, on its third
                         bar's completion. Entry = that close, stop = the three bars'
                         extreme +/- 0.25 capped at 25 pts (§6's cap).

A pure state machine over completed 1m bars with no Executor imports; the Executor owns
every order, record and plan-death decision. The follow-up is OPEN until its window ends
(20 min, never past 11:00 ET), its budget of 2 attempts is spent, price trades beyond the
leg's origin, or a follow-up position exits in profit. A met thesis falsifier is NOT a
close (operator, 2026-10-04; on 09-18 it blocked a follow-up the tape rewarded), and a
follow-up position is exempt from the MES-sweep stop itself (09-04: the rebuilt stage swept
it out again for +5).

NOT a rule (`docs/entry-mechanism-change-protocol.md` step 0), and OFF by default
(operator decision 2026-10-05: the forced-entry rig found 2 winners in 15 trades, so the
work is parked; the MES-sweep exit stands without it): unset or empty = off;
`ACT_FOLLOWUP_ENTRY=1` (or true/yes/on) switches it on, read once per Executor;
`FOLLOWUP_ENABLED` overrides the environment for a harness (True / False; None = read the
environment).
"""
from __future__ import annotations

import os

import pandas as pd

from agent.trader.extreme_reject import ExtremeReject
from agent.trader.trail import is_long

ENV_FLAG = "ACT_FOLLOWUP_ENTRY"
#: None = the environment decides (default OFF). True / False = a harness override.
FOLLOWUP_ENABLED = None
#: The window after the exit, minutes; never past `FOLLOWUP_HARD_END_ET`.
FOLLOWUP_WINDOW_MIN = 20
FOLLOWUP_HARD_END_ET = (11, 0)
#: Follow-up positions that may be stopped out before it gives up.
FOLLOWUP_MAX_ATTEMPTS = 2
#: The leg's origin for the structure veto is MNQ's extreme AGAINST the trade since this
#: ET time (the 09:00 micro-session open). Operator, 2026-10-04: anchored at the 09:30
#: open, the origin of an early sweep was a few points under the open, and the veto closed
#: seven follow-ups on the forced-entry rig, six of them on days that then continued.
FOLLOWUP_ORIGIN_ET = (9, 0)
#: Opening needs this much of T2 left past the exit, in points AND as a fraction of the
#: first fill's own distance to T2.
FOLLOWUP_MIN_T2_PTS = 30.0
FOLLOWUP_MIN_T2_FRACTION = 0.25
#: Trigger F: the stop sits this far beyond the three bars' extreme, capped at this many
#: points from the entry; gaps under one tick are ignored.
FOLLOWUP_FVG_STOP_BUFFER_PTS = 0.25
FOLLOWUP_FVG_STOP_CAP_PTS = 25.0
TICK_PTS = 0.25

MECH_REJECT = "followup_reject_close"
MECH_FVG = "followup_fvg_1m"
MECHANISMS = (MECH_REJECT, MECH_FVG)


def enabled() -> bool:
    """Read ONCE per Executor, at its construction — never inside the bar loop."""
    if FOLLOWUP_ENABLED is not None:
        return bool(FOLLOWUP_ENABLED)
    raw = str(os.environ.get(ENV_FLAG, "")).strip().lower()
    return raw in ("1", "true", "yes", "on")


def window_end(exit_ts) -> pd.Timestamp:
    """`FOLLOWUP_WINDOW_MIN` after the exit, capped at `FOLLOWUP_HARD_END_ET` of the
    exit's own date."""
    exit_ts = pd.Timestamp(exit_ts)
    hard = exit_ts.normalize() + pd.Timedelta(hours=FOLLOWUP_HARD_END_ET[0],
                                              minutes=FOLLOWUP_HARD_END_ET[1])
    return min(exit_ts + pd.Timedelta(minutes=FOLLOWUP_WINDOW_MIN), hard)


def skip_reason(direction, exit_px, first_entry, t2) -> "str | None":
    """Why the follow-up must NOT open, or None: `"t2_unbound"` (the plan has no T2) or
    `"t2_too_close"` (the part of T2 still ahead of the exit is under the point floor or
    under the fraction of the first fill's own distance to it)."""
    if t2 is None:
        return "t2_unbound"
    sign = 1.0 if is_long(direction) else -1.0
    remaining = sign * (float(t2) - float(exit_px))
    first = sign * (float(t2) - float(first_entry)) if first_entry is not None else None
    if remaining < float(FOLLOWUP_MIN_T2_PTS):
        return "t2_too_close"
    if first is not None and remaining < float(FOLLOWUP_MIN_T2_FRACTION) * first:
        return "t2_too_close"
    return None


class FollowUp:
    """The follow-up of ONE plan. Built at the exit with `open`; `on_bars` every bar close
    while open, `on_tick` every tick, `note_exit` when one of its positions exits."""

    def __init__(self, direction, exit_ts, *, exit_px, exit_kind, t2, first_entry,
                 counter_extreme) -> None:
        self.direction = direction
        self.long = is_long(direction)
        self.exit_ts = pd.Timestamp(exit_ts)
        self.exit_minute = self.exit_ts.floor("1min")
        self.exit_px = float(exit_px)
        self.exit_kind = exit_kind
        self.t2 = None if t2 is None else float(t2)
        self.first_entry = None if first_entry is None else float(first_entry)
        self.counter_extreme = None if counter_extreme is None else float(counter_extreme)
        self.window_end = window_end(self.exit_ts)
        self.attempts = 0
        self.closed_reason = None
        self._reject = ExtremeReject(direction, self.exit_ts, track="followup",
                                     extreme=None)
        self._last_bar = None
        self._attempted_gaps: set = set()

    @classmethod
    def open(cls, direction, exit_ts, *, exit_px, exit_kind, t2, first_entry,
             counter_extreme=None) -> "FollowUp":
        return cls(direction, exit_ts, exit_px=exit_px, exit_kind=exit_kind, t2=t2,
                   first_entry=first_entry, counter_extreme=counter_extreme)

    @property
    def is_open(self) -> bool:
        return self.closed_reason is None

    def close(self, reason: str) -> None:
        if self.closed_reason is None:
            self.closed_reason = reason

    # -- the tape -------------------------------------------------------------- #

    def on_tick(self, high, low) -> "str | None":
        """The structure veto: the first tick beyond the leg's origin (the post-09:30
        counter-thesis extreme as of the exit) closes the follow-up. Returns the reason
        on the tick it closes, else None."""
        if not self.is_open or self.counter_extreme is None:
            return None
        if self.long:
            broken = low is not None and float(low) < self.counter_extreme
        else:
            broken = high is not None and float(high) > self.counter_extreme
        if broken:
            self.close("structure_broken")
            return "structure_broken"
        return None

    def on_bars(self, now, m1: pd.DataFrame) -> list:
        """Drive every completed 1m bar of `m1` not yet seen, from the exit's minute on.
        Returns the fire dicts of the LATEST bar (both triggers may fire on it); earlier
        bars only advance the state. Nothing once closed."""
        if not self.is_open or m1 is None or not len(m1):
            return []
        seg = m1[m1.index >= self.exit_minute]
        fires: list = []
        for i in range(len(seg)):
            ts = seg.index[i]
            if self._last_bar is not None and ts <= self._last_bar:
                continue
            self._last_bar = ts
            fires = self._judge(now, seg.iloc[:i + 1])
        return fires

    def _judge(self, now, bars: pd.DataFrame) -> list:
        bar = bars.iloc[-1]
        fires = []
        fire = self._reject.on_bar_close(now, bar)
        if fire is not None:
            fires.append(dict(fire, mechanism=MECH_REJECT, followup=True, time=now))
        gap = self._fvg_fire(now, bars)
        if gap is not None:
            fires.append(gap)
        return fires

    def _fvg_fire(self, now, bars: pd.DataFrame) -> "dict | None":
        if len(bars) < 3:
            return None
        b0, b1, b2 = bars.iloc[-3], bars.iloc[-2], bars.iloc[-1]
        mid = bars.index[-2]
        if mid in self._attempted_gaps:
            return None
        if self.long:
            size = float(b2["Low"]) - float(b0["High"])
        else:
            size = float(b0["Low"]) - float(b2["High"])
        if size < TICK_PTS:
            return None
        self._attempted_gaps.add(mid)
        entry = float(b2["Close"])
        buf = float(FOLLOWUP_FVG_STOP_BUFFER_PTS)
        cap = float(FOLLOWUP_FVG_STOP_CAP_PTS)
        if self.long:
            stop = max(min(float(b["Low"]) for b in (b0, b1, b2)) - buf, entry - cap)
        else:
            stop = min(max(float(b["High"]) for b in (b0, b1, b2)) + buf, entry + cap)
        return {"time": now, "price": entry, "stop": stop,
                "direction": "UP" if self.long else "DOWN", "mechanism": MECH_FVG,
                "followup": True, "gap": str(mid), "gap_size": size}

    # -- the budget ------------------------------------------------------------ #

    def note_exit(self, profitable: bool) -> None:
        """A follow-up position exited. Profit closes the follow-up (`positive_close`);
        anything else spends one of its attempts, and the last one closes it
        (`attempts_exhausted`)."""
        if not self.is_open:
            return
        if profitable:
            self.close("positive_close")
            return
        self.attempts += 1
        if self.attempts >= int(FOLLOWUP_MAX_ATTEMPTS):
            self.close("attempts_exhausted")

    def state(self) -> dict:
        return {"open": self.is_open, "closed_reason": self.closed_reason,
                "attempts": self.attempts, "exit_px": self.exit_px,
                "exit_ts": str(self.exit_ts), "exit_kind": self.exit_kind,
                "window_end": str(self.window_end), "t2": self.t2,
                "counter_extreme": self.counter_extreme,
                "reject": {k: v for k, v in self._reject.state().items()
                           if k != "age_measured_at"}}
