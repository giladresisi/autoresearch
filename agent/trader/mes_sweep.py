"""CANDIDATE (2026-10-02/03, operator; `l2-mechanisms.md` §11.7): tighten the MNQ stop
when MES sweeps its day extreme during the position and the sweeping bar fails to close
beyond it.

The observation it came from (2026-10-02, long from 31128.25 at 09:42:00, T2 31267.5).
MES's day high stood at 7803.00 since 09:05:22. The 09:45 bar took it (7805.00, first
print above at 09:45:17) and closed 7802.00, back under the level. MNQ had not taken its
own high. From there both gave back: MNQ 31198.25 -> 31157.50 in the 09:48 bar and
31107.00 by 09:57:30. The operator moved the stop by hand to 31160.00 at 09:45:52 and was
stopped there.

The rule, per open position:

  level     MES's day extreme IN THE TRADE'S DIRECTION (the high for a long, the low for
            a short) over the 24h session (from the prior 18:00 ET) AS OF THE FILL. A
            level MES took before the entry is not this level: only a first sweep that
            happens while the position is open counts. MNQ's own extreme is not consulted.
  sweep     the first completed MES 1m bar, from the fill's minute on, that trades
            beyond the level. It must OPEN before `MES_SWEEP_CUTOFF_ET` (10:30 ET): a
            first sweep at or after it ends the stage with nothing done.
  failure   that bar closes at or inside the level (`MES_SWEEP_REQUIRE_FAILED_CLOSE`;
            `MES_SWEEP_CONFIRM_BARS` such closes in a row, 1 = the sweeping bar alone).
            A close beyond the level is acceptance, and the stage is done for this
            position: no later bar re-arms it.
  action    on the tick that bar completes, move the MNQ stop to the CLOSE of the MNQ 1m
            bar that just completed, minus (long) / plus (short) `MES_SWEEP_STOP_PTS`,
            IF that tightens it AND the new stop is at or beyond the entry in the
            trade's favour (`MES_SWEEP_REQUIRE_PROFIT_STOP`): the rule locks a result, it
            never swaps the original stop for a tighter LOSING one. Once per position.
            The move goes through `move_stop`, so a touch books `stop_out_initial`.

Why this tight and this early (operator, 2026-10-03, after the retrace study in
`<global>/studies/mes_day_extreme_sweep`): the stop is MEANT to be hit. Over 09:30-10:30
sweeps on the 1s history, when the sweeping bar closed inside the level MNQ went 5 pts or
more below that bar's close in 88% of 57 events before continuing, with a median retrace
of 46 pts; when it closed beyond the level the median was 12.6. The exit skips the retrace
and a follow-up entry (not built yet) is expected to take the continuation, which came in
86% of those events. The first version of this rule (two failing closes, the current
price less 15) gave back about 11 more points before acting. The 10:30 cutoff is from the
same study: after 10:30 a failed sweep was followed by a 15-pt retrace no more often than
an accepted one (60% against 62%).

It may fire before break-even at 50% arms; break-even only ever moves the stop when
that tightens it (`trail.TrailStage.breakeven_stop`), so it cannot pull this stop back.

NOT a rule (`docs/entry-mechanism-change-protocol.md` step 0), but ON by default
(operator decision 2026-10-03): unset or empty = on; `ACT_MES_SWEEP_STOP=0` (or
false/no/off) is the rollback, read once per Executor; `MES_SWEEP_ENABLED` overrides the
environment for a harness (True / False; None = read the environment).
"""
from __future__ import annotations

import os

import pandas as pd

from agent.trader.tape import session_open
from agent.trader.trail import completed_1m, is_long

ENV_FLAG = "ACT_MES_SWEEP_STOP"
#: None = the environment decides (default ON). True / False = a harness override.
MES_SWEEP_ENABLED = None
#: The new stop's distance from the close of the MNQ 1m bar that just completed, in points.
MES_SWEEP_STOP_PTS = 5.0
#: Consecutive completed MES 1m bars to wait for, the sweeping bar included (1 = act on
#: the sweeping bar's own close).
MES_SWEEP_CONFIRM_BARS = 1
#: True = each of those bars must close AT OR INSIDE the level (a close beyond it is
#: acceptance and ends the stage). False = act on the sweep alone, whatever the close.
MES_SWEEP_REQUIRE_FAILED_CLOSE = True
#: The sweeping bar must OPEN before this ET time, `(hour, minute)`; None = no cutoff.
MES_SWEEP_CUTOFF_ET = (10, 30)
#: True = move only when the new stop is at or beyond the entry in the trade's favour.
MES_SWEEP_REQUIRE_PROFIT_STOP = True
#: How late after 18:00 ET a frame may start and still count as holding the session.
SESSION_START_TOLERANCE = pd.Timedelta(minutes=10)


def enabled() -> bool:
    """Read ONCE per Executor, at its construction — never inside the bar loop."""
    if MES_SWEEP_ENABLED is not None:
        return bool(MES_SWEEP_ENABLED)
    raw = str(os.environ.get(ENV_FLAG, "")).strip().lower()
    return raw not in ("0", "false", "no", "off")


def day_extreme(mes: pd.DataFrame, direction, at) -> "tuple | None":
    """`(price, timestamp)` of MES's 24h-session extreme in the trade's direction as of
    `at` (inclusive), or None when the frame does not reach back to the session open —
    an extreme read off a short frame would be a level that was never the day's."""
    if mes is None or not len(mes) or at is None:
        return None
    at = pd.Timestamp(at)
    day = at.normalize()
    if at.hour >= 18:                                   # the evening belongs to tomorrow
        day = day + pd.DateOffset(days=1)
    start = session_open(day.strftime("%Y-%m-%d"))
    # A frame that starts AT the session open is whole: live can hand over today's bars
    # alone, and their first stamp is the first bar that traded, not 18:00:00 itself.
    if mes.index[0] > start + SESSION_START_TOLERANCE:
        return None
    seg = mes[(mes.index >= start) & (mes.index <= at)]
    if not len(seg):
        return None
    col = seg["High"] if is_long(direction) else seg["Low"]
    ts = col.idxmax() if is_long(direction) else col.idxmin()
    return float(col.loc[ts]), ts


class MesSweepStage:
    """The stage of ONE position. Built at the fill; `on_bar_close` every completed 1m bar."""

    def __init__(self, direction, opened_at, level=None, level_set_at=None) -> None:
        self.direction = direction
        self.long = is_long(direction)
        self.opened_at = opened_at
        self.level = None if level is None else float(level)
        self.level_set_at = level_set_at
        self.sweep_bar = None
        self.fails = 0
        self.last_bar = None
        #: None while watching; "fired" | "accepted" | "late" | "no_level" once done.
        self.outcome = None if self.level is not None else "no_level"

    @property
    def done(self) -> bool:
        return self.outcome is not None

    def on_bar_close(self, now, mes) -> bool:
        """True on the tick the rule fires (once). `mes` is the session frame truncated
        to `now`, 1s or 1m."""
        if self.done:
            return False
        m1 = completed_1m(mes, self.opened_at, now)
        for ts, bar in m1.iterrows():
            if self.last_bar is not None and ts <= self.last_bar:
                continue
            self.last_bar = ts
            if self._judge(ts, bar):
                return True
            if self.done:
                break
        return False

    def _judge(self, ts, bar) -> bool:
        if self.sweep_bar is None:
            if (MES_SWEEP_CUTOFF_ET is not None
                    and (ts.hour, ts.minute) >= tuple(MES_SWEEP_CUTOFF_ET)):
                self.outcome = "late"             # nothing swept before the cutoff
                return False
            swept = (float(bar["High"]) > self.level) if self.long \
                else (float(bar["Low"]) < self.level)
            if not swept:
                return False
            self.sweep_bar = ts
        close = float(bar["Close"])
        beyond = (close > self.level) if self.long else (close < self.level)
        if beyond and MES_SWEEP_REQUIRE_FAILED_CLOSE:
            self.outcome = "accepted"
            return False
        self.fails += 1
        if self.fails >= int(MES_SWEEP_CONFIRM_BARS):
            self.outcome = "fired"
            return True
        return False

    def new_stop(self, mnq_price, current_stop, entry=None) -> tuple:
        """`(stop, None)` — the stop to move to off `mnq_price` (the close of the MNQ 1m
        bar that just completed) — or `(None, why)`: `"no_price"`, `"not_in_profit"` (the
        new stop would sit on the losing side of `entry`) or `"not_tighter"`."""
        if mnq_price is None:
            return None, "no_price"
        off = float(MES_SWEEP_STOP_PTS)
        cand = float(mnq_price) - off if self.long else float(mnq_price) + off
        if MES_SWEEP_REQUIRE_PROFIT_STOP and entry is not None:
            locked = (cand >= float(entry)) if self.long else (cand <= float(entry))
            if not locked:
                return None, "not_in_profit"
        if current_stop is not None:
            tighter = (cand > float(current_stop)) if self.long \
                else (cand < float(current_stop))
            if not tighter:
                return None, "not_tighter"
        return cand, None

    def state(self) -> dict:
        return {"level": self.level,
                "level_set_at": str(self.level_set_at) if self.level_set_at is not None else None,
                "sweep_bar": str(self.sweep_bar) if self.sweep_bar is not None else None,
                "fails": self.fails, "outcome": self.outcome}
