"""Tape access for the named-case reproductions: 1s bars, 1m bars, day extremes.

The named regression cases in `l2-mechanisms.md` are statements about REAL tape — "the
09:41 bar ticks 29770 and closes red", "the 09:38 bar prints 29437.00 at 09:38:14,
thirteen seconds before price enters the gap". Reproducing them means driving the
mechanism state machines over that tape, so the loading and the bar convention have to
live in exactly one place.

THE 5m/1m BAR CONVENTION IS PINNED (§11, 07-23 item) and it is not a detail: **left-
labelled, left-closed, 18:00-ET-session-anchored**. Bar `T` spans `[T, T+tf)` and
completes at `T+tf`. On 07-23 that single choice decided a whole trade — left-closed puts
the 09:40 bar's close at 28856.00, above the bound gap's top, inverting it; right-closed
puts it at 28813.50 and the gap survives. Never inherit a library default here.

Resolution is NOT the issue and never was: §11 measured 1m-resampled and 1s-resampled 5m
series and found them BYTE-IDENTICAL under the same alignment.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

TZ = "America/New_York"
SESSION_OPEN_HOUR = 18            # the CME session opening the prior evening
_AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
# Bounded: a full trading day of 1s MNQ bars is ~60 MB, and the named-case suites touch
# a dozen dates. Evicting the oldest keeps a long session from holding all of them.
_CACHE_MAX = 4
_CACHE: "dict" = {}


def tape_1s(date: str) -> "pd.DataFrame | None":
    """The date's contract-routed 1s MNQ parquet, or None when absent.

    Routing goes through `backtest_smt._main_dir_for_date`, the same resolver the replay
    uses — the June->September rollover sends July/August dates to `2026-09/`, and a
    hard-coded path silently reads the wrong contract.
    """
    if date not in _CACHE:
        from backtest_smt import _main_dir_for_date
        path = _main_dir_for_date(date) / "MNQ_1s.parquet"
        while len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[date] = pd.read_parquet(path) if path.exists() else None
    return _CACHE[date]


def _ts(date: str, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{date} {hhmm}", tz=TZ)


def window_1s(date: str, start: str = "09:30", end: str = "12:00") -> pd.DataFrame:
    df = tape_1s(date)
    if df is None:
        return pd.DataFrame()
    return df[(df.index >= _ts(date, start)) & (df.index < _ts(date, end))]


def bars(date: str, tf: str = "1min", start: str = "09:30",
         end: str = "12:00") -> pd.DataFrame:
    """Left-labelled, left-closed bars at `tf`. See the module docstring."""
    win = window_1s(date, start, end)
    if not len(win):
        return win
    return win.resample(tf, label="left", closed="left").agg(_AGG).dropna()


def session_open(date: str) -> pd.Timestamp:
    """The 24h CME session open: the PRIOR evening's 18:00 ET.

    A calendar-day offset, not `Timedelta(days=1)`: on a tz-aware stamp the latter
    crosses a DST boundary an hour off, and the Sunday-evening session bars sit on
    exactly those two weekends each year.
    """
    day = pd.Timestamp(date, tz=TZ).normalize()
    prev = pd.Timestamp(day - pd.tseries.offsets.DateOffset(days=1)).normalize()
    # `DateOffset(hours=...)`, not `Timedelta`: an ABSOLUTE 18-hour add onto a tz-aware
    # local midnight lands on 17:00 or 19:00 local across a DST transition — which is
    # exactly the Sunday-evening session bars this function exists to find.
    return prev + pd.DateOffset(hours=SESSION_OPEN_HOUR)


def counter_thesis_extreme(date: str, direction: str, at: pd.Timestamp):
    """(price, timestamp) of the 24h day extreme AGAINST `direction`, as of `at`.

    A DOWN thesis is opposed by the day HIGH, an UP thesis by the day LOW. The instant
    `at` must be pinned by the caller — §7 measures the extreme's AGE at the PLAN ARM,
    because the retired distance key straddled its threshold between a 09:20 and a 09:30
    reading of the same day.
    """
    df = tape_1s(date)
    if df is None:
        return None, None
    win = df[(df.index >= session_open(date)) & (df.index <= at)]
    if not len(win):
        return None, None
    if str(direction).upper() in ("DOWN", "SHORT"):
        return float(win["High"].max()), win["High"].idxmax()
    return float(win["Low"].min()), win["Low"].idxmin()
