"""The single owner of timeframe bar construction.

Every producer of timeframe-derived facts — incremental and batch alike — MUST call
this module. Two independent notions of a "5m bar" inside one view is the failure
mode l2-mechanisms.md §11 records for 07-23, where a resampled 5m close appeared to
invalidate a gap before a validated fill.

Conventions, pinned:
  - left-labeled, closed-left bins
  - COMPLETED bins only. A bin is complete when the source covers its ENTIRE span:
    the bin starts at or after the first covered bar of its session segment, and ends
    at or before the last covered bar's end. A bin missing bars at either edge is
    dropped — that is the rule the 16:50 pre-maintenance bin fails.
  - session-anchored to 18:00 ET (not midnight)
  - bins never span the 16:55-18:00 CME maintenance break: the frame is split into
    CME session segments (each opening at an 18:00 ET) and each segment is resampled
    independently against its own session origin.
"""
from __future__ import annotations

import datetime

import pandas as pd

SESSION_OPEN_HOUR = 18
MAINT_LO = datetime.time(16, 55)
MAINT_HI = datetime.time(18, 0)

_AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
_SUPPORTED = {"5min", "15min", "1h", "4h"}
_DEFAULT_STEP = pd.Timedelta(minutes=1)


def _session_origin(ts: pd.Timestamp) -> pd.Timestamp:
    """The 18:00 ET session open at or before `ts`."""
    anchor = ts.normalize() + pd.Timedelta(hours=SESSION_OPEN_HOUR)
    if ts < anchor:
        anchor -= pd.Timedelta(days=1)
    return anchor


def _drop_maintenance(df: pd.DataFrame) -> pd.DataFrame:
    t = df.index.time
    keep = ~((t >= MAINT_LO) & (t < MAINT_HI))
    return df[keep]


def _source_step(idx: pd.DatetimeIndex) -> pd.Timedelta:
    """Bar width of the SOURCE frame, inferred from the smallest positive gap."""
    if len(idx) < 2:
        return _DEFAULT_STEP
    diffs = pd.Series(idx).diff().dropna()
    diffs = diffs[diffs > pd.Timedelta(0)]
    return pd.Timedelta(diffs.min()) if len(diffs) else _DEFAULT_STEP


def resample(df: pd.DataFrame, tf: str, *, session_anchored: bool = True) -> pd.DataFrame:
    """1m bars -> `tf` bars. Completed bins only. See module docstring for conventions."""
    if tf not in _SUPPORTED:
        raise ValueError(f"unsupported timeframe {tf!r}; expected one of {sorted(_SUPPORTED)}")
    if df is None or len(df) == 0:
        return df.iloc[0:0] if df is not None else pd.DataFrame()

    src = _drop_maintenance(df)
    if len(src) == 0:
        return src.iloc[0:0]

    span = pd.Timedelta(tf)
    step = _source_step(src.index)

    # Split into CME session segments so no bin can ever straddle the maintenance break.
    # Vectorised: a Python `_session_origin` per row is O(n) interpreter calls and was
    # the single largest cost on the bar-close path once history frames (~24k rows)
    # started reaching this module.
    anchor = src.index.normalize() + pd.Timedelta(hours=SESSION_OPEN_HOUR)
    seg_key = pd.Series(anchor.where(src.index >= anchor, anchor - pd.Timedelta(days=1)),
                        index=src.index)
    pieces = []
    for origin, seg in src.groupby(seg_key, sort=True):
        kwargs = {"label": "left", "closed": "left"}
        if session_anchored:
            kwargs["origin"] = origin
        out = seg.resample(tf, **kwargs).agg(_AGG).dropna(how="all")
        if not len(out):
            continue
        first_covered = seg.index[0]
        last_covered = seg.index[-1] + step
        complete = (out.index >= first_covered) & (out.index + span <= last_covered)
        out = out[complete]
        if len(out):
            pieces.append(out)

    if not pieces:
        return src.iloc[0:0].reindex(columns=list(_AGG))
    return pd.concat(pieces).sort_index()
