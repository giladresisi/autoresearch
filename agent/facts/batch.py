"""The lookback — batch driver over a bar slab.

`run_batch` produces the CANONICAL view of every class in a requirement over that
class's window. It REPLACES each class/ticker slice wholesale rather than topping it
up: "refill" means "produce the canonical view for this scope", never "add what's
missing" — otherwise stale derived state (a supersession verdict, a close-through
that a later re-derivation would undo) survives a re-derivation and the store stops
being a pure function of the bars.

That property is the acid test in `test_gate_store_equivalence.py`: deleting the
store must not change any result.

No-lookahead: nothing strictly after `now` enters the view, whatever the caller hands
in. (`agent/bench/facts.py` enforces the same precondition for the offline path.)
"""
from __future__ import annotations

import pandas as pd

from agent.facts.bars import _session_origin, resample
from agent.facts.detectors._common import normalize, truncate
from agent.facts.detectors.extremes import post_0930_since, running_extremes
from agent.facts.detectors.fvg import detect_fvgs, update_fvg_states
from agent.facts.detectors.legs import segment_legs
from agent.facts.detectors.levels import (apply_sweep_states, apply_supersession,
                                          detect_levels, week_start_ts)
from agent.facts.ids import fact_id, fact_label
from agent.facts.records import Fact, FactClass, FactState
from agent.facts.store import FactStore

# The timeframes FVGs are tracked on. The raw 1m frame plus every resampled TF the
# mechanisms read. Both drivers MUST use this exact tuple or the seam invariant
# (`test_incremental_matches_batch_for_the_same_window`) breaks.
FVG_TIMEFRAMES = ("1min", "5min", "15min", "1h", "4h")
LEG_TIMEFRAME = "5min"


def _slice(df: pd.DataFrame, now: pd.Timestamp, window) -> pd.DataFrame:
    out = truncate(normalize(df), now)
    if window is not None and len(out) and now is not None:
        out = out[out.index >= now - window]
    return out


def _tf_frame(win: pd.DataFrame, tf: str) -> pd.DataFrame:
    return win if tf == "1min" else resample(win, tf)


def _fvg_facts(win: pd.DataFrame, ticker: str) -> "list[Fact]":
    out: list[Fact] = []
    for tf in FVG_TIMEFRAMES:
        try:
            frame = _tf_frame(win, tf)
        except Exception:
            continue
        if len(frame) < 3:
            continue
        facts, _ = detect_fvgs(frame, tf, ticker, {})
        out += update_fvg_states(facts, frame, tf)
    return out


def _anchor_facts(win: pd.DataFrame, now: pd.Timestamp, ticker: str) -> "list[Fact]":
    """Session / week anchors — the time origins every other class is measured from."""
    if len(win) == 0 or now is None:
        return []
    out = []
    for name, ts in (("session_open", _session_origin(now)),
                     ("week_open", week_start_ts(now))):
        fid = fact_id(FactClass.ANCHOR, ticker, kind=name, at=ts)
        f = Fact(id=fid, cls=FactClass.ANCHOR, ticker=ticker, label="", name=name,
                 reference_ts=ts, price=None, price_low=None, price_high=None,
                 timeframe=None, resolution="1min", state=FactState.LIVE, state_ts=ts,
                 provenance={"detector": "anchors"}, extra={"kind": name})
        f.label = fact_label(f)
        out.append(f)
    return out


def run_batch(store: FactStore, bars_by_ticker: dict, req, now: pd.Timestamp) -> FactStore:
    """Populate every class in `req` over its own window, price-unbounded, with
    lifecycle state evaluated forward to `now`. Returns the same store, mutated."""
    if store is None:
        store = FactStore()
    if req is None:
        return store

    tickers = tuple(req.tickers or ())
    windows = req.windows or {}

    for cls in req.fact_classes:
        window = windows.get(cls)
        for tkr in tickers:
            try:
                win = _slice((bars_by_ticker or {}).get(tkr), now, window)
                facts: "list[Fact]" = []

                if cls is FactClass.LEVEL:
                    facts = detect_levels(win, now, tkr)
                    facts = apply_supersession(facts)
                    facts = apply_sweep_states(facts, win, now)
                elif cls is FactClass.FVG:
                    facts = _fvg_facts(win, tkr)
                elif cls is FactClass.LEG:
                    facts = segment_legs(_tf_frame(win, LEG_TIMEFRAME), now, tkr)
                elif cls is FactClass.EXTREME:
                    # TWO tracks. §7 follows the 24h extreme and falls back to the
                    # post-09:30 one when it is stale; §6 needs a day extreme printed
                    # after the last 5m FVG. One track cannot serve both.
                    facts = []
                    since = _session_origin(now) if now is not None else None
                    if since is not None and len(win) and win.index[0] > since:
                        since = win.index[0]
                    if since is not None:
                        facts += running_extremes(win, since, tkr, track="24h")
                    rth = post_0930_since(now)
                    if rth is not None and len(win) and win.index[-1] >= rth:
                        facts += running_extremes(win, rth, tkr, track="post_0930")
                elif cls is FactClass.ANCHOR:
                    facts = _anchor_facts(win, now, tkr)

                store.replace_class(cls, tkr, facts)
                if now is not None:
                    requested = (now - window) if window is not None else now
                    store.mark_requested(cls, tkr, requested)
                    # The watermark records what the BARS actually reached back to, not
                    # what was asked for. A 14-day request over a 15-hour slab covers
                    # 15 hours; claiming otherwise makes `health()` and the coverage
                    # precondition report history the store does not hold.
                    achieved = requested if len(win) == 0 else max(requested, win.index[0])
                    store.extend_coverage(cls, tkr, achieved)
            except Exception:
                # A detector must never take the caller down. The class simply stays
                # at whatever coverage it already had.
                continue

    return store
