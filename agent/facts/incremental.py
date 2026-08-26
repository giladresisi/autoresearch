"""Incremental driver — the bar-close cascade fed by the live 1 Hz loop.

The per-second path stays O(small): only running extremes are recomputed. Everything
expensive is gated on a bar CLOSE, and each timeframe's detection runs only when a new
bin of that timeframe has actually completed (a session-anchored floor comparison, no
resample needed to decide). FVG detection, close-through re-evaluation and leg
re-segmentation ride those cascades.

The seam invariant: driven bar-by-bar to the same `now`, this must produce exactly the
fact set `run_batch` produces for the same window. Both drivers therefore share the
detector set, `bars.resample`, and `batch.FVG_TIMEFRAMES` — there is no second copy of
any rule here.
"""
from __future__ import annotations

import pandas as pd

from agent.facts.bars import _session_origin
from agent.facts.batch import FVG_TIMEFRAMES, LEG_TIMEFRAME, _tf_frame
from agent.facts.detectors._common import normalize, truncate
from agent.facts.detectors.extremes import running_extremes
from agent.facts.detectors.fvg import detect_fvgs, update_fvg_states
from agent.facts.detectors.legs import segment_legs
from agent.facts.records import FactClass
from agent.facts.store import FactStore

# LTF scope for the cascade — mirrors EXECUTOR_REQUIREMENT's FVG/LEG window so the
# incremental and batch views cover the same span.
INCREMENTAL_WINDOW = pd.Timedelta(hours=24)

# A cascade only ever has ONE new completed bin to inspect, and detection needs a
# 3-bar window, so scanning the whole 24h frame every bar-close is O(n) work for O(1)
# news — quadratic across a session. Detection therefore runs over the last N bins
# only; `detect_fvgs` is idempotent (seen-ids in `state`), so the overlap is free and
# the resulting fact set is identical to the batch driver's.
DETECT_TAIL_BINS = 64


def _tf_floor(ts: pd.Timestamp, tf: str) -> "pd.Timestamp | None":
    """Session-anchored floor of `ts` to `tf`. `Timestamp.floor` would use the midnight
    grid, which disagrees with the 18:00-anchored bins `bars.resample` emits."""
    if ts is None:
        return None
    if tf == "1min":
        return ts.floor("1min")
    span = pd.Timedelta(tf)
    origin = _session_origin(ts)
    return origin + ((ts - origin) // span) * span


def run_incremental(store: FactStore, state: dict, bars_by_ticker: dict,
                    now: pd.Timestamp, bar_complete: bool) -> dict:
    """One cascade pass. Returns the updated detector state (JSON-serializable)."""
    st = dict(state or {})
    if store is None:
        return st

    for tkr in sorted((bars_by_ticker or {}).keys()):
        try:
            _one_ticker(store, st, tkr, bars_by_ticker.get(tkr), now, bar_complete)
        except Exception:
            # Total by contract: a degenerate frame for one ticker must not stop the
            # other, and must never propagate into the bar loop.
            continue
    return st


def _one_ticker(store: FactStore, st: dict, tkr: str, raw, now: pd.Timestamp,
                bar_complete: bool) -> None:
    df = truncate(normalize(raw), now)
    if len(df) == 0:
        return
    win = df[df.index >= now - INCREMENTAL_WINDOW] if now is not None else df

    # ---- per-second path: running extremes only ---------------------------- #
    since = _session_origin(now) if now is not None else win.index[0]
    if len(win) and win.index[0] > since:
        since = win.index[0]
    store.replace_class(FactClass.EXTREME, tkr, running_extremes(win, since, tkr))
    if now is not None:
        store.extend_coverage(FactClass.EXTREME, tkr, since)

    if not bar_complete:
        return

    # ---- bar-close cascades -------------------------------------------------- #
    st.setdefault("cascade", {})
    marks = st["cascade"].setdefault(tkr, {})
    fvg_state = st.setdefault("fvg", {}).setdefault(tkr, {})

    fired_any = False
    for tf in FVG_TIMEFRAMES:
        floor = _tf_floor(now, tf)
        if floor is None:
            continue
        key = f"fvg:{tf}"
        if marks.get(key) == str(floor):
            continue                                   # no new bin of this TF yet
        marks[key] = str(floor)
        fired_any = True
        try:
            frame = _tf_frame(win, tf)
        except Exception:
            continue
        if len(frame) < 3:
            continue
        tail = frame.iloc[-DETECT_TAIL_BINS:]
        new_facts, fvg_state[tf] = detect_fvgs(tail, tf, tkr, fvg_state.get(tf) or {})
        for f in new_facts:
            store.upsert(f)
        held = [f for f in store.query(cls=FactClass.FVG, ticker=tkr)
                if f.timeframe == tf]
        update_fvg_states(held, frame, tf)

    if fired_any and now is not None:
        store.extend_coverage(FactClass.FVG, tkr, max(win.index[0], now - INCREMENTAL_WINDOW))

    # ---- legs re-segment on the 5m close ------------------------------------- #
    leg_floor = _tf_floor(now, LEG_TIMEFRAME)
    if leg_floor is not None and marks.get("legs") != str(leg_floor):
        marks["legs"] = str(leg_floor)
        try:
            legs = segment_legs(_tf_frame(win, LEG_TIMEFRAME), now, tkr)
        except Exception:
            legs = []
        store.replace_class(FactClass.LEG, tkr, legs)
        if now is not None:
            store.extend_coverage(FactClass.LEG, tkr,
                                  max(win.index[0], now - INCREMENTAL_WINDOW))

    st["last_cascade"] = str(now)
