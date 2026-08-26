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


def running_extremes(bars: pd.DataFrame, since: pd.Timestamp, ticker: str) -> "list[Fact]":
    """day_high / day_low over `bars[bars.index >= since]`. Total — `[]` on empty."""
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
                      at=ts)
        f = Fact(
            id=fid, cls=FactClass.EXTREME, ticker=ticker, label="", name=None,
            reference_ts=ts, price=price, price_low=None, price_high=None,
            timeframe=None, resolution="1min", state=FactState.LIVE, state_ts=ts,
            provenance={"detector": "extremes", "since": since},
            extra={"kind": kind, "body_price": body, "since": since},
        )
        f.label = fact_label(f)
        out.append(f)
    return out
