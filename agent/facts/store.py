"""In-memory multi-class fact store with per-class coverage watermarks.

Keyed by the content-derived `fact.id`, so an upsert REPLACES rather than appends —
re-detecting the same gap must never double it. Watermarks are per `(FactClass,
ticker)`: MNQ and MES are symmetric, never MNQ-primary with MES annotations, and the
level universe legitimately reaches back further than the FVG universe.

Coverage is a PRECONDITION, not a trigger: `coverage_status` is a pure function of
`(now, price)` and what the store holds, so it answers the same way however the store
was reached. `COMPLETE_AT_CAP` exists because "no levels above" at an all-time high is
a COMPLETE answer, not a degraded one — collapsing it into `INSUFFICIENT` would make
the executor refill forever at every new high.
"""
from __future__ import annotations

import dataclasses
from enum import Enum

import pandas as pd

from agent.facts.records import Fact, FactClass, FactState


class CoverageStatus(str, Enum):
    OK = "ok"
    INSUFFICIENT = "insufficient"
    COMPLETE_AT_CAP = "complete_at_cap"


class FactStore:
    """Facts by id + per-(class, ticker) coverage watermarks."""

    def __init__(self) -> None:
        self._facts: "dict[str, Fact]" = {}
        self._covered_from: "dict[tuple, pd.Timestamp]" = {}
        # What the lookback was ASKED for, as opposed to what the bars could actually
        # supply. These are different numbers whenever the bar slab is shorter than the
        # requirement's window, and conflating them makes the watermark lie: the store
        # would claim 14 days of level coverage off a 15-hour frame. `covered_from`
        # stays honest; this drives the "already at the cap, do not re-batch" check.
        self._requested_from: "dict[tuple, pd.Timestamp]" = {}

    # -- content ------------------------------------------------------------- #

    def upsert(self, fact: Fact) -> None:
        if fact is None or not fact.id:
            return
        self._facts[fact.id] = fact

    def get(self, fact_id: str) -> "Fact | None":
        return self._facts.get(fact_id)

    def remove(self, fact_id: str) -> None:
        self._facts.pop(fact_id, None)

    def query(self, cls: "FactClass | None" = None, ticker: "str | None" = None,
              state: "FactState | None" = None,
              price_range: "tuple | None" = None) -> "list[Fact]":
        out = []
        for f in self._facts.values():
            if cls is not None and f.cls is not cls:
                continue
            if ticker is not None and f.ticker != ticker:
                continue
            if state is not None and f.state is not state:
                continue
            if price_range is not None:
                lo, hi = price_range
                candidates = [p for p in (f.price, f.price_low, f.price_high) if p is not None]
                if not candidates or not any(lo <= p <= hi for p in candidates):
                    continue
            out.append(f)
        return out

    # -- coverage watermarks -------------------------------------------------- #

    def covered_from(self, cls: FactClass, ticker: str) -> "pd.Timestamp | None":
        return self._covered_from.get((cls, ticker))

    def extend_coverage(self, cls: FactClass, ticker: str, new_from: pd.Timestamp) -> None:
        """Watermarks only ever move BACKWARD. A forward move would silently claim
        coverage the store does not have."""
        if new_from is None:
            return
        cur = self._covered_from.get((cls, ticker))
        self._covered_from[(cls, ticker)] = new_from if cur is None else min(cur, new_from)

    def mark_requested(self, cls: FactClass, ticker: str, from_ts: pd.Timestamp) -> None:
        """Record the window the lookback was DRIVEN over (not what it found)."""
        if from_ts is None:
            return
        cur = self._requested_from.get((cls, ticker))
        self._requested_from[(cls, ticker)] = from_ts if cur is None else min(cur, from_ts)

    def requested_from(self, cls: FactClass, ticker: str) -> "pd.Timestamp | None":
        return self._requested_from.get((cls, ticker))

    def trim_far_end(self, cls: FactClass, ticker: str, keep_from: pd.Timestamp) -> None:
        """Drop this class/ticker's facts older than `keep_from` and move the watermark
        forward to match. Trimming from the MIDDLE is never offered: it would break
        contiguity and make the watermark lie."""
        if keep_from is None:
            return
        drop = [f.id for f in self.query(cls=cls, ticker=ticker)
                if f.reference_ts is not None and f.reference_ts < keep_from]
        for fid in drop:
            self._facts.pop(fid, None)
        self._covered_from[(cls, ticker)] = keep_from

    def replace_class(self, cls: FactClass, ticker: str, facts: "list[Fact]") -> None:
        """Wholesale replacement of one class/ticker slice — the batch driver's write
        primitive. "Refill" means "produce the canonical view for this scope", never
        "add what's missing", or stale derived state survives a re-derivation."""
        for f in self.query(cls=cls, ticker=ticker):
            self._facts.pop(f.id, None)
        for f in facts or ():
            self.upsert(f)

    # -- coverage precondition ------------------------------------------------ #

    def price_span(self, cls: FactClass, ticker: str) -> "tuple | None":
        prices = []
        for f in self.query(cls=cls, ticker=ticker):
            prices += [p for p in (f.price, f.price_low, f.price_high) if p is not None]
        return (min(prices), max(prices)) if prices else None

    def coverage_status(self, cls: FactClass, ticker: str, *, price: float,
                        envelope: float, at_time_cap: bool = False) -> CoverageStatus:
        span = self.price_span(cls, ticker)
        if span is not None:
            lo, hi = span
            if lo <= price - envelope and hi >= price + envelope:
                return CoverageStatus.OK
        return CoverageStatus.COMPLETE_AT_CAP if at_time_cap else CoverageStatus.INSUFFICIENT

    def health(self, cls: FactClass, ticker: str) -> dict:
        facts = self.query(cls=cls, ticker=ticker)
        stamps = [f.reference_ts for f in facts if f.reference_ts is not None]
        return {
            "cls": cls.value,
            "ticker": ticker,
            "covered_from": self.covered_from(cls, ticker),
            "complete_through": max(stamps) if stamps else None,
            "count": len(facts),
            "price_span": self.price_span(cls, ticker),
        }

    # -- snapshot ------------------------------------------------------------- #

    def to_dict(self) -> dict:
        return {
            "facts": [f.to_dict() for f in self._facts.values()],
            "covered_from": [
                {"cls": cls.value, "ticker": tkr,
                 "from": ts.isoformat() if ts is not None else None}
                for (cls, tkr), ts in self._covered_from.items()
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FactStore":
        s = FactStore()
        for fd in (d or {}).get("facts") or ():
            try:
                s.upsert(Fact.from_dict(fd))
            except Exception:
                continue
        for row in (d or {}).get("covered_from") or ():
            try:
                s._covered_from[(FactClass(row["cls"]), row["ticker"])] = (
                    pd.Timestamp(row["from"]) if row.get("from") else None)
            except Exception:
                continue
        return s


# --------------------------------------------------------------------------- #
# Task 9 — the coverage precondition                                          #
# --------------------------------------------------------------------------- #

DEFAULT_AVG_RANGE_1H = 60.0
_EXTEND_STEPS = (0.5, 1.0)      # fractions of the requirement's declared window (the cap)


def ensure_coverage(store: FactStore, bars: dict, req, now: pd.Timestamp, price: float,
                    envelope_atr_mult: float = 2.0,
                    avg_range_1h: "float | None" = None) -> CoverageStatus:
    """Extend coverage backward until every declared class spans the price envelope, or
    the requirement's own time cap is reached.

    Startup, restart and a mid-session refill all take THIS path — there is no separate
    "cold start" code. Returns `COMPLETE_AT_CAP` (never `INSUFFICIENT`) once the cap is
    reached: having looked as far back as policy allows IS a complete answer.
    """
    from agent.facts.batch import run_batch          # local: batch imports the store

    envelope = float(envelope_atr_mult) * float(avg_range_1h or DEFAULT_AVG_RANGE_1H)

    def _all_ok(at_cap: bool) -> CoverageStatus:
        worst = CoverageStatus.OK
        for cls in req.fact_classes:
            for tkr in req.tickers:
                st = store.coverage_status(cls, tkr, price=price, envelope=envelope,
                                           at_time_cap=at_cap)
                if st is CoverageStatus.INSUFFICIENT:
                    return CoverageStatus.INSUFFICIENT
                if st is CoverageStatus.COMPLETE_AT_CAP:
                    worst = CoverageStatus.COMPLETE_AT_CAP
        return worst

    status = _all_ok(at_cap=False)
    if status is CoverageStatus.OK:
        return status

    # Already extended to the declared cap for every class? Then there is nothing left
    # to fetch and re-running the lookback would be pure cost. This short-circuit is
    # what keeps `ensure_coverage` cheap enough to sit on the bar-close path: without
    # it, a class that is inherently narrow (EXTREME holds two facts) would never
    # report OK and would re-drive the whole lookback on every single call.
    at_cap_already = all(
        (store.requested_from(cls, tkr) is not None
         and (req.windows or {}).get(cls) is not None
         and store.requested_from(cls, tkr) <= now - req.windows[cls])
        for cls in req.fact_classes for tkr in req.tickers
    )
    if at_cap_already:
        return _all_ok(at_cap=True)

    for i, frac in enumerate(_EXTEND_STEPS):
        at_cap = (i == len(_EXTEND_STEPS) - 1)
        scaled = dataclasses.replace(
            req, windows={k: v * frac for k, v in (req.windows or {}).items()})
        run_batch(store, bars, scaled, now)
        status = _all_ok(at_cap=at_cap)
        if status is CoverageStatus.OK:
            return status
    return status
