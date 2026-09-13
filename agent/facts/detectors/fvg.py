"""FVG detection, close-through eligibility, and max anti-excursion. Pure.

Detection mirrors `derive_facts.fvgs` (3-bar imbalance): `bar3.Low > bar1.High` is a
bull gap spanning [bar1.High, bar3.Low]; `bar3.High < bar1.Low` is a bear gap spanning
[bar3.High, bar1.Low].

Eligibility is CLOSE-based (l2-mechanisms.md §2): a gap dies only when a bar CLOSES
beyond its far end on the gap's own timeframe. A gap that was wicked through but
repelled the close is a *proven* barrier, not a dead one — that distinction is what
saved the 07-16 09:30 open.

`bars` are ALREADY at `tf` — this module never resamples. The driver owns that
(`agent.facts.bars.resample`), so there is exactly one notion of a "5m bar".
"""
from __future__ import annotations

import pandas as pd

from agent.facts.detectors._common import normalize, same_session
from agent.facts.ids import fact_id, fact_label
from agent.facts.records import Fact, FactClass, FactState

_SEEN = "seen_ids"


def _window_is_contiguous(idx, i: int, span: pd.Timedelta) -> bool:
    """The three bars (i-1, i, i+1) must be adjacent on the tf grid AND inside one
    CME session. A window straddling the 16:55-18:00 maintenance break (or a weekend)
    is not an imbalance — it is a hole in the tape."""
    a, b, c = idx[i - 1], idx[i], idx[i + 1]
    if (b - a) != span or (c - b) != span:
        return False
    return same_session(a, c)


def detect_fvgs(bars: pd.DataFrame, tf: str, ticker: str,
                prior: dict) -> "tuple[list[Fact], dict]":
    """3-bar imbalances on `bars` (already at `tf`). Idempotent: `prior` carries the
    already-emitted IDs, so re-running over the same bars emits nothing new."""
    state = dict(prior or {})
    seen = set(state.get(_SEEN) or ())
    df = normalize(bars)
    out: list[Fact] = []
    if len(df) < 3:
        state[_SEEN] = sorted(seen)
        return out, state

    try:
        span = pd.Timedelta(tf)
    except ValueError:
        state[_SEEN] = sorted(seen)
        return out, state

    idx = df.index
    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)

    for i in range(1, len(df) - 1):
        if not _window_is_contiguous(idx, i, span):
            continue
        ph, pl = highs[i - 1], lows[i - 1]
        nh, nl = highs[i + 1], lows[i + 1]
        if nl > ph:
            direction, lo, hi = "bull", float(ph), float(nl)
        elif nh < pl:
            direction, lo, hi = "bear", float(nh), float(pl)
        else:
            continue
        ref_ts = idx[i]
        fid = fact_id(FactClass.FVG, ticker, timeframe=tf, reference_ts=ref_ts,
                      price_low=lo, price_high=hi)
        if fid in seen:
            continue
        seen.add(fid)
        fact = Fact(
            id=fid, cls=FactClass.FVG, ticker=ticker, label="", name=None,
            reference_ts=ref_ts, price=None, price_low=lo, price_high=hi,
            timeframe=tf, resolution=tf, state=FactState.LIVE, state_ts=ref_ts,
            provenance={"detector": "fvg", "tf": tf},
            extra={"direction": direction, "max_anti_excursion": 0.0,
                   "height": round(hi - lo, 6),
                   # IDENTITY vs EXISTENCE (l2-mechanisms.md §2, pinned 2026-08-27).
                   # `reference_ts` / `creating_bar_ts` = the MIDDLE bar: the gap's name,
                   # the bar it is visible across on a chart. `confirm_bar_ts` = the third
                   # bar's LABEL. `exists_from` = that bar's COMPLETION — the earliest
                   # instant anything may act on the gap. They differ by one bar-width, and
                   # a consumer that reads a LABEL as an instant is early by exactly that
                   # much: the §11 08-21 erratum is that mistake, an entry recorded 09:36:04
                   # on a gap that only existed at 09:40:00. Time-gated rules read
                   # `exists_from`, never the labels.
                   "creating_bar_ts": ref_ts, "confirm_bar_ts": idx[i + 1],
                   "exists_from": idx[i + 1] + span},
        )
        fact.label = fact_label(fact)
        out.append(fact)

    state[_SEEN] = sorted(seen)
    return out, state


def update_fvg_states(facts: "list[Fact]", bars: pd.DataFrame, tf: str) -> "list[Fact]":
    """Re-evaluate every live gap against bars AFTER its confirming bar.

    A **close** beyond the far end on the gap's own timeframe sets INVALIDATED; a
    wick-only excursion leaves it LIVE (l2-mechanisms.md §2). `max_anti_excursion` is
    tracked as a running max of how far price pushed past the far end (wick basis),
    which is what the §2 distance-invalidation rule reads.
    """
    df = normalize(bars)
    if len(df) == 0 or not facts:
        return list(facts or [])

    # numpy views + searchsorted rather than a boolean mask per fact: the batch driver
    # re-evaluates every gap in a 17-day window against that whole window, so a
    # per-fact full-frame mask is quadratic in the number of gaps.
    idx = df.index
    closes = df["Close"].to_numpy(dtype=float)
    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)

    for f in facts:
        if f.cls is not FactClass.FVG:
            continue
        # Scan from the gap's EXISTENCE instant (third-bar completion), inclusive.
        # `exists_from` is already the next bar's label, so side="left" starts at that
        # bar — identical to the legacy `searchsorted(confirm_bar_ts, side="right")`,
        # which is why this carries no behaviour change; it just stops the correctness
        # from depending on a label/instant coincidence.
        exists_from = f.extra.get("exists_from")
        if exists_from is not None:
            after_ts = pd.Timestamp(exists_from)
            side = "left"
        else:
            after_ts = f.extra.get("confirm_bar_ts") or f.reference_ts
            if not isinstance(after_ts, pd.Timestamp):
                after_ts = pd.Timestamp(after_ts)
            side = "right"
        start = int(idx.searchsorted(after_ts, side=side))
        if start >= len(idx):
            continue
        c, h, l = closes[start:], highs[start:], lows[start:]
        if f.extra.get("direction") == "bull":
            far = f.price_low
            excursion = float(far - l.min())
            hits = (c < far).nonzero()[0]
        else:
            far = f.price_high
            excursion = float(h.max() - far)
            hits = (c > far).nonzero()[0]

        prev = float(f.extra.get("max_anti_excursion") or 0.0)
        f.extra["max_anti_excursion"] = max(prev, max(0.0, excursion))
        if len(hits) and f.state is FactState.LIVE:
            f.set_state(FactState.INVALIDATED, idx[start + int(hits[0])])
    return list(facts)
