"""Running day / post-arm extremes. Pure.

l2-mechanisms.md §7: "day" means the 24h CME session opening at the prior 18:00 ET —
not RTH. The caller supplies `since`, so the SAME function serves both the day extreme
(since = session open) and the post-arm extreme (since = the L1 arm time); §7 arms off
the counter-thesis extreme measured from the arm, not from midnight.
"""
from __future__ import annotations

import pandas as pd

from agent.facts.detectors._common import normalize
from agent.facts.ids import fact_id, fact_label
from agent.facts.records import Fact, FactClass, FactState


RTH_OPEN_HOUR = 9
RTH_OPEN_MINUTE = 30


def post_0930_since(now: pd.Timestamp) -> "pd.Timestamp | None":
    """09:30 ET of the TRADE DATE `now` belongs to.

    The CME session opens at 18:00 the previous evening, so a 20:00 bar belongs to the
    NEXT calendar day's trade date and its RTH open is that day's 09:30 — not a 09:30
    that already passed. §7's fallback track starts there.
    """
    if now is None:
        return None
    date = now.normalize()
    if now.hour >= 18:
        # A CALENDAR day, not 24 absolute hours: `pd.Timedelta(days=1)` on a tz-aware
        # stamp crosses a DST boundary an hour off, and 18:00 Sunday-evening session
        # bars sit on exactly those two weekends each year.
        date = (date + pd.tseries.offsets.DateOffset(days=1)).normalize()
    return date + pd.Timedelta(hours=RTH_OPEN_HOUR, minutes=RTH_OPEN_MINUTE)


def extreme_age_at(fact, at: pd.Timestamp) -> "pd.Timedelta | None":
    """How old the extreme is at `at`. §7 keys its stale-extreme fallback on this,
    measured AT THE PLAN ARM — the instant must be pinned by the caller, because the
    old distance key straddled its threshold between a 09:20 and a 09:30 reading."""
    if fact is None or at is None or getattr(fact, "reference_ts", None) is None:
        return None
    return at - fact.reference_ts


def running_extremes(bars: pd.DataFrame, since: pd.Timestamp, ticker: str,
                     *, track: str = "24h") -> "list[Fact]":
    """day_high / day_low over `bars[bars.index >= since]`. Total — `[]` on empty.

    `track` names WHICH extreme series this is: `"24h"` (since the prior 18:00 CME
    session open) or `"post_0930"` (since the RTH open). §7's state machine follows the
    24h extreme and falls back to the post-09:30 one when the 24h extreme is stale;
    §6 needs a day extreme printed after the last 5m FVG's creation. The two must be
    separately addressable, so the track is part of the fact's IDENTITY, not a label.
    """
    df = normalize(bars)
    if len(df) == 0 or since is None:
        return []
    win = df[df.index >= since]
    if len(win) == 0:
        return []

    out: list[Fact] = []
    for kind, col, agg in (("day_high", "High", "max"), ("day_low", "Low", "min")):
        series = win[col]
        ts = series.idxmax() if agg == "max" else series.idxmin()
        price = float(series.max() if agg == "max" else series.min())
        body = float(win["Close"].max() if agg == "max" else win["Close"].min())
        fid = fact_id(FactClass.EXTREME, ticker, kind=kind, since=since, price=price,
                      at=ts, track=track)
        f = Fact(
            id=fid, cls=FactClass.EXTREME, ticker=ticker, label="", name=None,
            reference_ts=ts, price=price, price_low=None, price_high=None,
            timeframe=None, resolution="1min", state=FactState.LIVE, state_ts=ts,
            provenance={"detector": "extremes", "since": since},
            extra={"kind": kind, "body_price": body, "since": since, "track": track},
        )
        f.label = fact_label(f)
        out.append(f)
    return out
