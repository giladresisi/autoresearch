"""§3 ZigZag leg segmentation on 5m bars. Pure.

l2-mechanisms.md §3:
  - Walk the last ~4 hours of 5m bars tracking the current leg's extreme.
  - The leg ENDS when price retraces from that extreme by more than
    `max(30 pts, 25% of the leg's range so far)`. Smaller pullbacks do not interrupt it.
  - A leg QUALIFIES at range >= 50 pts.
  - "Last trend" = of the qualifying legs whose extreme formed <= 60 min ago, the one
    with the LARGER RANGE ("If two qualify in the window, take the larger range" — §3
    scopes by recency, then selects by range; recency only breaks range ties). That
    selection lives in `last_trend` and is consumed by the Planner, not here
    — §3 is explicit that the 60-min recency test scopes the *reversal* mechanism only
    and must NOT limit continuation-gap relevance.

The currently-forming leg is emitted too, once it qualifies: a trend in progress is
exactly the thing the reversal mechanism reverses, and waiting for it to close would
make the plan blind for the whole leg.
"""
from __future__ import annotations

import pandas as pd

from agent.facts.detectors._common import normalize, truncate
from agent.facts.ids import fact_id, fact_label
from agent.facts.records import Fact, FactClass, FactState

WINDOW = pd.Timedelta(hours=4)
MIN_RETRACE_PTS = 30.0
RETRACE_FRAC = 0.25
QUALIFY_RANGE_PTS = 50.0
LAST_TREND_MAX_AGE = pd.Timedelta(minutes=60)


def _retrace_threshold(leg_range: float) -> float:
    return max(MIN_RETRACE_PTS, RETRACE_FRAC * leg_range)


def segment_legs(bars_5m: pd.DataFrame, now: pd.Timestamp, ticker: str) -> "list[Fact]":
    """ZigZag segmentation of the last 4 hours of 5m bars, as of `now`.

    Total: an empty / malformed frame yields `[]`, never an exception. Nothing after
    `now` is read (no lookahead).
    """
    df = truncate(normalize(bars_5m), now)
    if len(df) < 2:
        return []
    if now is not None:
        df = df[df.index >= now - WINDOW]
    if len(df) < 2:
        return []

    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    idx = list(df.index)

    legs: list[dict] = []
    direction = None            # "up" | "down" | None (undetermined)
    start_i = 0
    hi, hi_i = highs[0], 0
    lo, lo_i = lows[0], 0

    def _close(dir_: str, s_i: int, e_i: int, ext_i: int, rng: float) -> None:
        legs.append({"direction": dir_, "start_i": s_i, "end_i": e_i,
                     "extreme_i": ext_i, "range": rng, "closed": True})

    for i in range(1, len(df)):
        h, l = highs[i], lows[i]
        if h > hi:
            hi, hi_i = h, i
        if l < lo:
            lo, lo_i = l, i

        if direction is None:
            # Undetermined until one side's excursion from the start exceeds the other.
            up_move, dn_move = hi - lows[start_i], highs[start_i] - lo
            if up_move > dn_move and hi_i >= lo_i:
                direction = "up"
            elif dn_move > up_move and lo_i >= hi_i:
                direction = "down"
            else:
                continue

        if direction == "up":
            leg_range = hi - lows[start_i]
            if (hi - l) > _retrace_threshold(leg_range):
                _close("up", start_i, i, hi_i, leg_range)
                direction, start_i = "down", hi_i
                hi, hi_i = highs[hi_i], hi_i
                lo, lo_i = l, i
        else:
            leg_range = highs[start_i] - lo
            if (h - lo) > _retrace_threshold(leg_range):
                _close("down", start_i, i, lo_i, leg_range)
                direction, start_i = "up", lo_i
                lo, lo_i = lows[lo_i], lo_i
                hi, hi_i = h, i

    # The leg still forming at `now`.
    if direction == "up":
        legs.append({"direction": "up", "start_i": start_i, "end_i": len(df) - 1,
                     "extreme_i": hi_i, "range": hi - lows[start_i], "closed": False})
    elif direction == "down":
        legs.append({"direction": "down", "start_i": start_i, "end_i": len(df) - 1,
                     "extreme_i": lo_i, "range": highs[start_i] - lo, "closed": False})

    out: list[Fact] = []
    for leg in legs:
        rng = float(leg["range"])
        if rng < QUALIFY_RANGE_PTS:
            continue
        s_ts, e_ts = idx[leg["start_i"]], idx[leg["end_i"]]
        ext_ts = idx[leg["extreme_i"]]
        fid = fact_id(FactClass.LEG, ticker, timeframe="5min", direction=leg["direction"],
                      start_ts=s_ts, extreme_ts=ext_ts)
        f = Fact(
            id=fid, cls=FactClass.LEG, ticker=ticker, label="", name=None,
            reference_ts=ext_ts, price=None,
            price_low=float(lows[leg["start_i"]:leg["end_i"] + 1].min()),
            price_high=float(highs[leg["start_i"]:leg["end_i"] + 1].max()),
            timeframe="5min", resolution="5min", state=FactState.LIVE, state_ts=ext_ts,
            provenance={"detector": "legs"},
            extra={"direction": leg["direction"], "range": round(rng, 4),
                   "start_ts": s_ts, "end_ts": e_ts, "extreme_ts": ext_ts,
                   "closed": bool(leg["closed"])},
        )
        f.label = fact_label(f)
        out.append(f)
    return out


def last_trend(legs: "list[Fact]", now: pd.Timestamp) -> "Fact | None":
    """§3 'last trend': among qualifying legs whose extreme formed <= 60 min ago, the
    one with the larger range ("If two qualify in the window, take the larger range").
    Recency scopes the candidate set and breaks range ties; it does not outrank range.
    """
    if not legs or now is None:
        return None
    fresh = [f for f in legs
             if f.reference_ts is not None and (now - f.reference_ts) <= LAST_TREND_MAX_AGE
             and (now - f.reference_ts) >= pd.Timedelta(0)]
    if not fresh:
        return None
    return max(fresh, key=lambda f: (float(f.extra.get("range") or 0.0), f.reference_ts))
