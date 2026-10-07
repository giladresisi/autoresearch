"""`nym_mid_reject`: a sweep of the NY-morning mid that closes back across it with the thesis.

`l2-mechanisms.md` §11.9 CANDIDATE (operator spec, 2026-10-06). Not a rule.

**The level.** The MNQ NY-morning mid, recomputed at the close of every 1m bar: the
midpoint of the range from the 06:00 ET open through the close of the LAST CLOSED 1m bar
(the bar being judged included). A running level: a bar that extends the range moves it.

**Precondition, once a day.** The 09:30 ET open must sit on the thesis' favourable side of
the mid as of that instant (the range 06:00 through the 09:29 bar): UP open > mid, DOWN
open < mid. Otherwise the mechanism is off for the day.

**Sweep and fire.** A bar that trades through the mid against the thesis (UP: Low < mid;
DOWN: High > mid) is a sweep, whichever side it closes on. The sweep bar itself, or the
bar immediately after it, fires when it closes on the favourable side of ITS OWN mid and
with the thesis against its own open (UP: Close > mid and Close > Open). Entry by MARKET
at that bar's close (the instant the bar completes), stop `STOP_PTS` from the entry.

**One fill a day.** The machine does NOT latch itself on a fire: a fire that is vetoed,
blocked or loses arbitration leaves it free to fire on a later sweep. The Executor calls
`latch()` when a fire of this mechanism actually FILLS, and nothing fires after that
(no re-fire after a stop-out). No stop-bar retry.

**Window.** Bars labelled 09:31 or later, fires strictly before 10:30:00 ET. The 09:30
opening bar is EXCLUDED (operator, 2026-10-07): it can neither sweep nor confirm, because on
it every print is a fresh post-open extreme (09-18: its self-confirming opening-bar sweep
was stopped on the next bar with 0 MFE). The first sweep is the 09:31 bar (the
Executor's `ENTRY_CUTOFF_ET` refuses a fire at 10:30:00 anyway; the machine says it too so
it is testable alone). Every other block and veto the market mechanisms get is the
Executor's, not this module's.

**Deliberately NOT a fact and NOT a DOL candidate** (same reasoning as `tmso_reject`): the
mid is computed here from the bars the loop already carries and never enters
`derive_facts`, the menus or the KB, so no thesis recording is re-keyed.

**Switch.** `ACT_NYM_MID_REJECT`, read ONCE per Executor (and by the Planner at the arm
for `armed_classes`): unset or empty = `DEFAULT_ENABLED`; `0`/`false`/`no`/`off` = off;
`1`/`true`/`yes`/`on` = on. `NYM_MID_REJECT_ENABLED` (None = the environment decides)
overrides it for a harness. Off = the bar loop exactly as before.
"""
from __future__ import annotations

import os

import pandas as pd

ET = "America/New_York"
ENV_FLAG = "ACT_NYM_MID_REJECT"
#: THE default, one constant: what an unset / empty `ACT_NYM_MID_REJECT` means.
DEFAULT_ENABLED = True
#: None = the environment decides. True / False = a harness override.
NYM_MID_REJECT_ENABLED = None
#: Stop distance from the entry, in points (operator, 2026-10-06). UNTUNED.
STOP_PTS = 20.0
#: The range the mid is taken over starts at this ET clock time.
MID_START_ET = (6, 0)
#: The RTH open: the precondition's open. Its own bar never sweeps.
OPEN_ET = (9, 30)
#: The first bar that may sweep (the 09:30 opening bar is excluded, operator 2026-10-07).
FIRST_SWEEP_ET = (9, 31)
#: No fire at or after this ET time (= `executor.ENTRY_CUTOFF_ET`).
WINDOW_END_ET = (10, 30)

_SHORT = ("DOWN", "SHORT")
_OFF = ("0", "false", "no", "off")
_ON = ("1", "true", "yes", "on")


def enabled() -> bool:
    """Read once per Executor, at its construction — never inside the bar loop."""
    if NYM_MID_REJECT_ENABLED is not None:
        return bool(NYM_MID_REJECT_ENABLED)
    raw = str(os.environ.get(ENV_FLAG, "")).strip().lower()
    if raw in _OFF:
        return False
    if raw in _ON:
        return True
    return bool(DEFAULT_ENABLED)


def _et(ts: pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize(ET) if ts.tzinfo is None else ts.tz_convert(ET)


def _at(day: pd.Timestamp, hm) -> pd.Timestamp:
    return day + pd.Timedelta(hours=hm[0], minutes=hm[1])


def _slice(mnq, start, end):
    """Rows with start <= index < end (the frame's index is sorted)."""
    idx = mnq.index
    return mnq.iloc[idx.searchsorted(start, side="left"):idx.searchsorted(end, side="left")]


def nym_mid(mnq, day: pd.Timestamp, through_label: pd.Timestamp) -> "float | None":
    """(High + Low) / 2 of the rows from `day` 06:00 ET through the end of the 1m bar
    labelled `through_label`. None when the frame does not hold the 06:00 open (a mid read
    off a frame that starts later would be a different level) or the range is empty."""
    if mnq is None or not len(mnq):
        return None
    start = _at(day, MID_START_ET)
    if _et(mnq.index[0]) >= start + pd.Timedelta(minutes=1):
        return None
    seg = _slice(mnq, start, through_label + pd.Timedelta(minutes=1))
    if not len(seg):
        return None
    return (float(seg["High"].max()) + float(seg["Low"].min())) / 2.0


def precondition(mnq, day: pd.Timestamp, *, short: bool) -> dict:
    """`{ok, open, mid, reason}` for `day`: is the 09:30 open on the favourable side of the
    mid as of 09:30 (range 06:00 through the 09:29 bar)? `open` is the first print of the
    09:30 bar."""
    open_ts = _at(day, OPEN_ET)
    mid = nym_mid(mnq, day, open_ts - pd.Timedelta(minutes=1))
    first = _slice(mnq, open_ts, open_ts + pd.Timedelta(minutes=1)) if mnq is not None \
        and len(mnq) else None
    op = float(first.iloc[0]["Open"]) if first is not None and len(first) else None
    if mid is None:
        return {"ok": False, "open": op, "mid": None, "reason": "no_range"}
    if op is None:
        return {"ok": False, "open": None, "mid": mid, "reason": "no_open"}
    ok = op < mid if short else op > mid
    return {"ok": bool(ok), "open": op, "mid": mid,
            "reason": None if ok else "open_on_unfavourable_side"}


class NymMidReject:
    """Driven one COMPLETED 1m bar at a time; `on_bar_close` returns a fire dict or None.

    State: the day's precondition (evaluated once, from the frame), the sweep bar awaiting
    its next-bar confirmation, and the fill latch the Executor sets via `latch()`.
    """

    def __init__(self, direction: str) -> None:
        self._short = str(direction or "").upper() in _SHORT
        self._day = None
        self._pre = None
        self._armed_label = None
        self._filled = False

    @property
    def short(self) -> bool:
        return self._short

    def state(self) -> dict:
        return {"short": self._short, "precondition": None if self._pre is None
                else dict(self._pre), "armed_sweep_bar":
                None if self._armed_label is None else self._armed_label.isoformat(),
                "filled": self._filled}

    def latch(self) -> None:
        """A fire of this mechanism FILLED: the day's one fire is spent."""
        self._filled = True
        self._armed_label = None

    @property
    def filled(self) -> bool:
        return self._filled

    def on_bar_close(self, now, bar, mnq) -> "dict | None":
        """`now` is the instant `bar` completed; `bar` carries Open/High/Low/Close and, as
        `bar.name`, its left label. `mnq` is the MNQ frame through `now` (the whole
        session: the mid reads back to 06:00)."""
        if self._filled or bar is None:
            return None
        now = _et(now)
        label = bar.name if isinstance(getattr(bar, "name", None), pd.Timestamp) \
            else now.floor("1min") - pd.Timedelta(minutes=1)
        label = _et(label)
        day = label.normalize()
        if label < _at(day, FIRST_SWEEP_ET) or now >= _at(day, WINDOW_END_ET):
            return None
        if self._day != day:
            self._day = day
            self._pre = precondition(mnq, day, short=self._short)
            self._armed_label = None
        if not self._pre["ok"]:
            return None
        mid = nym_mid(mnq, day, label)
        if mid is None:
            return None
        o, h, l, c = (float(bar[k]) for k in ("Open", "High", "Low", "Close"))
        favourable = (c < mid and c < o) if self._short else (c > mid and c > o)
        if (self._armed_label is not None
                and label == self._armed_label + pd.Timedelta(minutes=1) and favourable):
            return self._fire(now, label, c, mid, "next_bar")
        swept = (h > mid) if self._short else (l < mid)
        self._armed_label = None
        if swept:
            if favourable:
                return self._fire(now, label, c, mid, "sweep_bar")
            self._armed_label = label
        return None

    def _fire(self, now, label, close, mid, how) -> dict:
        self._armed_label = None
        stop = close + STOP_PTS if self._short else close - STOP_PTS
        return {"time": now, "price": close, "stop": stop,
                "direction": "DOWN" if self._short else "UP", "level": mid,
                "mechanism": "nym_mid_reject", "confirm": how,
                "bar": label.isoformat()}
