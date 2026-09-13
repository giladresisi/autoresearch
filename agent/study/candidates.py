"""Spec §3's candidate universe: what the move could have been drawn to.

One `Candidate` is one PRICE on one instrument, not one name. Names collapse: on
2026-08-13 `prev1_week_high` and `prev6_day_high` are both 30073.25, and treating them as
two candidates would double-count the same pool — once as a week-tier draw and once as a
day-tier one, corrupting exactly the per-class rates spec §3.4's attractiveness hypothesis
is measured on. A collapsed candidate takes the HIGHEST tier among its names, because a
week pool that happens to coincide with a day pool is a week pool.

`classify` raises on an unknown name rather than bucketing it as "other". A new level
family entering `derive_facts` must surface as a failing test, not as a silently missing
class that dilutes every share in the report.

**FVG zones are deliberately absent.** `bundle.fvg_zones` carries 86 entries at a single
boundary; they are zones rather than prices, and admitting that many candidates would swamp
the null model before the named pools have been measured at all. Spec §3.2 already lowered
derived prices off the critical path. A phase-3 extension, recorded here so its absence
reads as a decision rather than an oversight.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CLS_WEEK = "week_extreme"
CLS_DAY = "day_extreme"
CLS_SESSION = "session_extreme"
CLS_OPEN = "open_price"
CLS_MID = "mid"

TIER_RANK = {"week": 3, "day": 2, "session": 1, "derived": 0}

_FIXED = {
    "TDO": (CLS_OPEN, "session"),
    "TWO": (CLS_OPEN, "session"),
    "daily_mid": (CLS_MID, "derived"),
    "weekly_mid": (CLS_MID, "derived"),
}
_PREV = re.compile(r"^prev\d+_(day|week)_(high|low)$")
#: `asia(cur)_high`, `london(cur)_low`, and any further sub-session the facts layer adds.
_SESSION = re.compile(r"^[a-z_]+\(cur\)_(high|low)$")

#: Names carried in `swept_at` but never in `levels` are not candidates; the mids arrive
#: through `bundle.mid_price` instead. Listed so the omission is explicit.
MID_NAMES = ("daily_mid", "weekly_mid")


@dataclass(frozen=True)
class Candidate:
    ticker: str
    names: tuple          # more than one when several names share a price
    price: float
    cls: str
    tier: str
    swept_before: bool    # sweep state at the EVALUATION INSTANT (spec §3.3)

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "names": list(self.names), "price": self.price,
                "cls": self.cls, "tier": self.tier, "swept_before": self.swept_before}


def classify(name: str) -> tuple:
    """`name` -> `(cls, tier)`. Raises `KeyError` on anything unrecognised."""
    if name in _FIXED:
        return _FIXED[name]
    m = _PREV.match(name)
    if m:
        return (CLS_WEEK, "week") if m.group(1) == "week" else (CLS_DAY, "day")
    if _SESSION.match(name):
        return CLS_SESSION, "session"
    raise KeyError(f"unclassified level name: {name!r}")


def universe(bundle, tickers=("MNQ", "MES")) -> "list[Candidate]":
    """Every candidate price on every ticker, as of the bundle's own boundary.

    Deterministically ordered by (ticker, price) so a corpus build is reproducible.
    """
    out = []
    for tkr in tickers:
        swept = bundle.swept_at.get(tkr, {}) or {}
        raw = []
        for name, val in (bundle.levels.get(tkr, {}) or {}).items():
            raw.append((name, float(val[0])))
        for name, price in (bundle.mid_price.get(tkr, {}) or {}).items():
            if price is not None:
                raw.append((name, float(price)))

        grouped: dict = {}
        for name, price in raw:
            grouped.setdefault(round(price, 4), []).append(name)

        for price, names in grouped.items():
            classes = [classify(n) for n in names]
            cls, tier = max(classes, key=lambda ct: TIER_RANK[ct[1]])
            out.append(Candidate(
                ticker=tkr, names=tuple(sorted(names)), price=price, cls=cls, tier=tier,
                # Two names on one price: if EITHER has been taken, the price has been
                # taken. Depletion is a property of the price, not of the label.
                swept_before=any(swept.get(n) is not None for n in names)))
    out.sort(key=lambda c: (c.ticker, c.price))
    return out
