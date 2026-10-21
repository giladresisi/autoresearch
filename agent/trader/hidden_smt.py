"""`hidden_smt_5m`: a hidden SMT on the 5m close against the NY-morning extreme, entered
on the first 1m bar that closes with the thesis in BOTH assets.

`l2-mechanisms.md` §11.14 CANDIDATE (session 2026-10-19 O1, operator 2026-10-20). Not a rule.

**The levels.** Per asset (MNQ and MES separately), fixed at 09:30 ET: the NY-morning low
and high, the min Low / max High of the rows from 06:00:00 through 09:29:59 ET. A frame
that does not reach back to the 06:00 minute gives no levels and the mechanism is off for
the day (as `nym_mid_reject`'s `no_range`).

**The hidden SMT, thesis direction only.** On each completed 5m bar (left-labelled,
aligned 09:30, 09:35, ...; close instants 09:35 through 10:55), an UP thesis compares each
asset's 5m CLOSE with its own NY-morning LOW (DOWN: CLOSE vs HIGH). The SMT is the FIRST
5m bar on which exactly one asset closes strictly beyond its level while the other does
not (the other may wick through). Once either asset has closed beyond — alone or both on
the same bar — the side is spent for the day: there is at most one SMT a day.

**Confirmation and entry.** The first completed 1m bar whose close instant lies in
[the 5m close, the 5m close + 14 min] — the 1m bar closing at the 5m close counts — on
which BOTH MNQ and MES close with the thesis against their own open (UP: Close > Open).
Entry by MARKET at that MNQ 1m close, at the instant the bar completes. Only the FIRST
confirming bar counts: if the Executor cannot take it (a gate, a veto, a lost
arbitration, a bar it was not driven on), the day's setup is gone.

**The stop.** A fixed `STOP_PTS` from the entry (UP: below it; DOWN: above it), whatever
MNQ's extreme since the SMT bar (operator 2026-10-20; the fire still carries that extreme as
`swept_extreme`).

**Re-derived from the frames, never from what the Executor happened to show it.** Every
call catches up over the 5m boundaries and 1m bars it has not judged yet, reading them
from the frames it is handed (no lookahead: only rows strictly before each instant).
So bars the Executor skipped (a position open, a cooldown, a dead plan) still count,
exactly as they would on a chart.

**One FILL a day** (`latch()`), and the attempt it spends is the plan's RESERVED one —
the Executor's business, not this module's.

**Deliberately NOT a fact** (as `tmso_reject` / `nym_mid_reject`): the levels never enter
`derive_facts`, the menus or the KB, so no thesis recording is re-keyed.

**Switch.** `ACT_HIDDEN_SMT_ENTRY`, read ONCE per Executor (and by the Planner at the arm
for `armed_classes`): unset or empty = `DEFAULT_ENABLED`; `0`/`false`/`no`/`off` = off;
`1`/`true`/`yes`/`on` = on. `HIDDEN_SMT_ENTRY_ENABLED` (None = the environment decides)
overrides it for a harness. Off = the bar loop and the plan exactly as before.
"""
from __future__ import annotations

import os

import pandas as pd

ET = "America/New_York"
MECHANISM = "hidden_smt_5m"
ENV_FLAG = "ACT_HIDDEN_SMT_ENTRY"
#: THE default, one constant: what an unset / empty `ACT_HIDDEN_SMT_ENTRY` means.
DEFAULT_ENABLED = True
#: None = the environment decides. True / False = a harness override.
HIDDEN_SMT_ENTRY_ENABLED = None
#: The NY-morning range: [LEVEL_START_ET, OPEN_ET).
LEVEL_START_ET = (6, 0)
OPEN_ET = (9, 30)
#: The 5m timeframe and the close instants an SMT may form on (inclusive).
TF = pd.Timedelta(minutes=5)
FIRST_SMT_CLOSE_ET = (9, 35)
LAST_SMT_CLOSE_ET = (10, 55)
#: The confirmation window: 1m close instants in [5m close, 5m close + this].
CONFIRM_WINDOW = pd.Timedelta(minutes=14)
#: The stop's distance from the entry.
STOP_PTS = 20.0

_SHORT = ("DOWN", "SHORT")
_OFF = ("0", "false", "no", "off")
_ON = ("1", "true", "yes", "on")
_ONE_MIN = pd.Timedelta(minutes=1)


def enabled() -> bool:
    """Read once per Executor, at its construction — never inside the bar loop."""
    if HIDDEN_SMT_ENTRY_ENABLED is not None:
        return bool(HIDDEN_SMT_ENTRY_ENABLED)
    raw = str(os.environ.get(ENV_FLAG, "")).strip().lower()
    if raw in _OFF:
        return False
    if raw in _ON:
        return True
    return bool(DEFAULT_ENABLED)


def _et(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize(ET) if ts.tzinfo is None else ts.tz_convert(ET)


def _at(day: pd.Timestamp, hm) -> pd.Timestamp:
    return day + pd.Timedelta(hours=hm[0], minutes=hm[1])


def _slice(df, start, end):
    """Rows with start <= index < end (the frame's index is sorted)."""
    idx = df.index
    return df.iloc[idx.searchsorted(start, side="left"):idx.searchsorted(end, side="left")]


def _bar(df, start, end):
    """(open, high, low, close) of the rows in [start, end), or None."""
    seg = _slice(df, start, end)
    if not len(seg):
        return None
    return (float(seg.iloc[0]["Open"]), float(seg["High"].max()),
            float(seg["Low"].min()), float(seg.iloc[-1]["Close"]))


def ny_morning_levels(df, day: pd.Timestamp) -> "dict | None":
    """`{low, high}` of the rows 06:00:00-09:29:59 ET on `day`, or None when the frame
    does not hold the 06:00 minute or the range is empty."""
    if df is None or not len(df):
        return None
    start = _at(day, LEVEL_START_ET)
    if _et(df.index[0]) >= start + _ONE_MIN:
        return None
    seg = _slice(df, start, _at(day, OPEN_ET))
    if not len(seg):
        return None
    return {"low": float(seg["Low"].min()), "high": float(seg["High"].max())}


class HiddenSmt:
    """Driven on completed 1m bars; `on_bar_close` returns a fire dict or None.

    State: the day's levels, the next 5m boundary to judge, the SMT (or the day spent),
    the next 1m close instant to judge for confirmation, and the fill latch.
    """

    def __init__(self, direction: str) -> None:
        self._short = str(direction or "").upper() in _SHORT
        self._day = None
        self._levels = None
        self._next_b = None
        self._spent = False
        self._smt = None
        self._next_c = None
        self._resolved = False
        self._filled = False

    @property
    def short(self) -> bool:
        return self._short

    @property
    def filled(self) -> bool:
        return self._filled

    def latch(self) -> None:
        """A fire of this mechanism FILLED: the day's one fill is spent."""
        self._filled = True

    def state(self) -> dict:
        return {"short": self._short, "levels": self._levels, "spent": self._spent,
                "smt": None if self._smt is None else {
                    k: (v.isoformat() if hasattr(v, "isoformat") else v)
                    for k, v in self._smt.items()},
                "resolved": self._resolved, "filled": self._filled}

    def _reset(self, day) -> None:
        self._day = day
        self._levels = None
        self._next_b = _at(day, FIRST_SMT_CLOSE_ET)
        self._spent = False
        self._smt = None
        self._next_c = None
        self._resolved = False

    def _beyond(self, close, level) -> bool:
        return close > level if self._short else close < level

    def _scan_smt(self, close_instant, mnq, mes) -> None:
        last_b = _at(self._day, LAST_SMT_CLOSE_ET)
        side = "high" if self._short else "low"
        while not self._spent and self._next_b <= min(close_instant, last_b):
            b = self._next_b
            self._next_b = b + TF
            bars = {tk: _bar(df, b - TF, b) for tk, df in (("MNQ", mnq), ("MES", mes))}
            if bars["MNQ"] is None or bars["MES"] is None:
                continue
            beyond = {tk: self._beyond(bars[tk][3], self._levels[tk][side])
                      for tk in ("MNQ", "MES")}
            n = sum(beyond.values())
            if n == 0:
                continue
            self._spent = True
            if n == 1:
                swept = "MNQ" if beyond["MNQ"] else "MES"
                self._smt = {"bar": b - TF, "close_at": b, "closed_beyond": swept,
                             "closes": {tk: bars[tk][3] for tk in ("MNQ", "MES")}}
                self._next_c = b

    def on_bar_close(self, now, mnq, mes) -> "dict | None":
        """`now` is the bar-close instant (the minute that just completed ends at
        `now.floor('1min')`); `mnq` / `mes` are the frames through `now`, reaching back
        to 06:00 ET."""
        if self._filled or mnq is None or mes is None or not len(mnq) or not len(mes):
            return None
        close_instant = _et(now).floor("1min")
        day = (close_instant - _ONE_MIN).normalize()
        if self._day != day:
            self._reset(day)
        if close_instant < _at(day, FIRST_SMT_CLOSE_ET):
            return None
        if self._levels is None:
            lv = {tk: ny_morning_levels(df, day) for tk, df in (("MNQ", mnq), ("MES", mes))}
            if lv["MNQ"] is None or lv["MES"] is None:
                self._spent = self._resolved = True     # no range: off for the day
                return None
            self._levels = lv
        if self._smt is None:
            self._scan_smt(close_instant, mnq, mes)
        if self._smt is None or self._resolved:
            return None
        end = self._smt["close_at"] + CONFIRM_WINDOW
        while self._next_c <= min(close_instant, end):
            c = self._next_c
            self._next_c = c + _ONE_MIN
            m, s = _bar(mnq, c - _ONE_MIN, c), _bar(mes, c - _ONE_MIN, c)
            if m is None or s is None:
                continue
            ok = ((m[3] < m[0] and s[3] < s[0]) if self._short
                  else (m[3] > m[0] and s[3] > s[0]))
            if not ok:
                continue
            self._resolved = True
            if c != close_instant:
                return None                     # confirmed on a bar nobody acted on
            return self._fire(now, c, m[3], mnq)
        if self._next_c > end:
            self._resolved = True
        return None

    def _fire(self, now, c, price, mnq) -> dict:
        seg = _slice(mnq, self._smt["bar"], c)
        if self._short:
            swept, stop = float(seg["High"].max()), price + STOP_PTS
        else:
            swept, stop = float(seg["Low"].min()), price - STOP_PTS
        risk = abs(price - stop)
        side = "high" if self._short else "low"
        return {"time": now, "price": price, "stop": stop,
                "direction": "DOWN" if self._short else "UP",
                "mechanism": MECHANISM, "risk": round(risk, 4),
                "swept_extreme": swept,
                "smt": {"bar": self._smt["bar"].isoformat(),
                        "closed_beyond": self._smt["closed_beyond"],
                        "closes": dict(self._smt["closes"]),
                        "levels": {tk: self._levels[tk][side] for tk in ("MNQ", "MES")},
                        "side": side},
                "bar": (c - _ONE_MIN).isoformat()}
