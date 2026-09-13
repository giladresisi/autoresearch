"""Content-derived fact IDs plus human labels.

The ID hashes IDENTITY-DEFINING fields only, never mutable state and never the
level `name` — names move (yesterday's prev1_day_high is today's prev2_day_high),
so hashing the name would make one fact look like two. Keeping the ID stable while
the name moves makes level aging traceable, and gives the future per-plan gap
blacklist a handle that survives re-binds.
"""
from __future__ import annotations

import hashlib

import pandas as pd

from agent.facts.records import Fact, FactClass

_ID_LEN = 12


def _norm(v) -> str:
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def fact_id(cls: FactClass, ticker: str, **identity) -> str:
    """Stable short hash over (class, ticker, sorted identity fields)."""
    parts = [str(cls.value), str(ticker)]
    parts += [f"{k}={_norm(identity[k])}" for k in sorted(identity)]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:_ID_LEN]


def fact_label(fact: Fact) -> str:
    """Human rendering. Always logged next to the ID; never used as an identifier."""
    when = fact.reference_ts.strftime("%m-%d %H:%M")
    if fact.cls is FactClass.FVG:
        direction = fact.extra.get("direction", "?")
        return (f"{fact.ticker} {fact.timeframe} {direction} FVG {when} "
                f"[{fact.price_low}, {fact.price_high}]")
    if fact.cls is FactClass.LEVEL:
        return f"{fact.ticker} {fact.name or '?'} {fact.price} ({when})"
    if fact.cls is FactClass.LEG:
        return (f"{fact.ticker} {fact.timeframe} leg {fact.extra.get('direction','?')} "
                f"{when} range={fact.extra.get('range','?')}")
    if fact.cls is FactClass.EXTREME:
        # The track is suffixed only when it is NOT the 24h one, so every label written
        # before the second track existed stays byte-identical.
        track = fact.extra.get("track")
        suffix = f" [{track}]" if track and track != "24h" else ""
        return (f"{fact.ticker} {fact.extra.get('kind', 'extreme')} {fact.price}"
                f"{suffix} ({when})")
    return f"{fact.ticker} {fact.cls.value} {when}"
