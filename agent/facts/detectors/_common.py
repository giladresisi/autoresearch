"""Shared helpers for the pure detectors. No I/O, no wall clock."""
from __future__ import annotations

import pandas as pd

from agent.facts.bars import _session_origin  # noqa: F401  (re-exported for detectors)

_CANON = {"open": "Open", "high": "High", "low": "Low", "close": "Close",
          "volume": "Volume"}


_REQUIRED = ("Open", "High", "Low", "Close")


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


def normalize(df: "pd.DataFrame | None") -> pd.DataFrame:
    """Capital-cased OHLC view. Accepts either the engine's lowercase frames
    (`derive_facts.load`) or the capitalised ones the live loop passes around.
    Returns an EMPTY frame — never raises — on degenerate input.

    Both the rename and the sort are SKIPPED when they would be no-ops. This sits on
    the per-second path over a frame that now carries session history (~20k rows); an
    unconditional `rename` + `sort_index` copies the whole frame several times per
    second for nothing.
    """
    if df is None or len(df) == 0:
        return _empty()
    if not isinstance(df.index, pd.DatetimeIndex):
        return _empty()
    out = df
    if not all(c in out.columns for c in _REQUIRED):
        ren = {c: _CANON[str(c).lower()] for c in out.columns if str(c).lower() in _CANON}
        if not ren:
            return _empty()
        out = out.rename(columns=ren)
        if not all(c in out.columns for c in _REQUIRED):
            return _empty()
    return out if out.index.is_monotonic_increasing else out.sort_index()


def truncate(df: pd.DataFrame, now: "pd.Timestamp | None") -> pd.DataFrame:
    """No-lookahead slice: nothing strictly after `now` survives.

    Fast path: when the frame already ends at or before `now` — the overwhelmingly
    common live case — return it untouched instead of materialising a boolean mask and
    a copy of the whole frame.
    """
    if now is None or len(df) == 0:
        return df
    if df.index[-1] <= now:
        return df
    return df[df.index <= now]


def same_session(a: pd.Timestamp, b: pd.Timestamp) -> bool:
    """True when two timestamps belong to the same CME session (18:00 ET open)."""
    return _session_origin(a) == _session_origin(b)
