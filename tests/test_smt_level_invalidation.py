# tests/test_smt_level_invalidation.py
# Unit tests for R2 (GIL-25): per-ticker liquidity-level depletion retirement + the
# fixed-level re-arm-until-invalidated rule in smt_detect._detect_level_smts.
#
# Distinct from tests/test_smt_invalidation.py (adverse-run invalidation of a fired SMT
# *record*). Here we test LEVEL-lifecycle invalidation: once a ticker runs a confirmed
# HH/LL FULFILL_PTS[tier] beyond a level, that level is retired (latched in the reserved
# state["__level_inv__"]) and the pair comparison is skipped if EITHER ticker retired it.

from __future__ import annotations

import smt_detect
from smt_detect import (
    FULFILL_PTS_MNQ,
    FULFILL_PTS_MES,
    detect_regular_smts,
    detect_hidden_smts,
)


# ---------------------------------------------------------------------------
# Builders (mirror tests/test_smt_detect.py)
# ---------------------------------------------------------------------------
def _level(name, price):
    sub = "high" if name.endswith("_high") else "low"
    return {"name": name, "kind": "level", "price": float(price), "sub": sub}


def _levels(**kv):
    return {name: _level(name, price) for name, price in kv.items()}


def _bar(high, low, close=None, time="2026-06-09T10:00:00"):
    if close is None:
        close = (high + low) / 2.0
    return {"time": time, "high": float(high), "low": float(low), "close": float(close)}


def _inv(state, name):
    return state.get("__level_inv__", {}).get(name, {"mnq": False, "mes": False})


# ===========================================================================
# Latch: a ticker running FULFILL_PTS[tier] beyond a level retires it for that ticker
# ===========================================================================
def test_high_level_latches_mnq_when_run_well_above():
    # prev1_day_high → ("fixed","day"); FULFILL day = MNQ 40 / MES 6.
    lm = _levels(prev1_day_high=21000.0)
    le = _levels(prev1_day_high=3000.0)
    # MNQ high runs >= 21000+40; MES high only +1 (< +6) → only MNQ latched.
    mnq = _bar(21045.0, 21030.0, 21040.0)
    mes = _bar(3001.0, 2995.0, 3000.0)
    _, state = detect_regular_smts(lm, le, mnq, mes, {})
    assert _inv(state, "prev1_day_high")["mnq"] is True
    assert _inv(state, "prev1_day_high")["mes"] is False


def test_low_level_latches_on_run_well_below():
    lm = _levels(prev1_day_low=21000.0)
    le = _levels(prev1_day_low=3000.0)
    # MNQ low runs <= 21000-40; MES low <= 3000-6 → both latched.
    mnq = _bar(20970.0, 20955.0, 20960.0)
    mes = _bar(2996.0, 2990.0, 2993.0)
    _, state = detect_regular_smts(lm, le, mnq, mes, {})
    assert _inv(state, "prev1_day_low")["mnq"] is True
    assert _inv(state, "prev1_day_low")["mes"] is True


def test_no_latch_below_threshold():
    lm = _levels(prev1_day_high=21000.0)
    le = _levels(prev1_day_high=3000.0)
    mnq = _bar(21039.0, 21020.0, 21035.0)   # 21039 < 21040
    mes = _bar(3005.0, 3000.0, 3002.0)      # 3005 < 3006
    _, state = detect_regular_smts(lm, le, mnq, mes, {})
    assert _inv(state, "prev1_day_high")["mnq"] is False
    assert _inv(state, "prev1_day_high")["mes"] is False


def test_uses_fulfill_pts_tier_thresholds():
    # Sanity: the reused thresholds are the FULFILL_PTS tables, day tier.
    assert FULFILL_PTS_MNQ["day"] == 40.0
    assert FULFILL_PTS_MES["day"] == 6.0


# ===========================================================================
# Phase 1.1 (GIL-25): pre-session depletion seeding — a past liquidity already run
# beyond before the session opened must be marked invalidated up front.
# ===========================================================================
def test_pre_session_depleted_high():
    from smt_detect import pre_session_depleted
    # high level 21000, day tier thr 40 (mnq): window High >= 21040 → depleted.
    assert pre_session_depleted("high", 21000.0, "day", "mnq", 21041.0, 20900.0) is True
    assert pre_session_depleted("high", 21000.0, "day", "mnq", 21039.0, 20900.0) is False


def test_pre_session_depleted_low():
    from smt_detect import pre_session_depleted
    # low level 21000, day tier thr 40 (mnq): window Low <= 20960 → depleted.
    assert pre_session_depleted("low", 21000.0, "day", "mnq", 21100.0, 20960.0) is True
    assert pre_session_depleted("low", 21000.0, "day", "mnq", 21100.0, 20961.0) is False


def test_pre_session_depleted_uses_instrument_threshold():
    from smt_detect import pre_session_depleted
    # MES day thr = 6: 3007 >= 3000+6 depleted; 3005 not.
    assert pre_session_depleted("high", 3000.0, "day", "mes", 3007.0, 2990.0) is True
    assert pre_session_depleted("high", 3000.0, "day", "mes", 3005.0, 2990.0) is False


def test_universe_levels_carry_window_end():
    import pandas as pd, datetime
    import daily
    idx = pd.date_range("2026-05-25 18:00", periods=60*24*9, freq="1min", tz="America/New_York")
    df = pd.DataFrame({"Open": 100.0, "High": 105.0, "Low": 95.0, "Close": 100.0}, index=idx)
    levels = daily.compute_universe_levels(df, datetime.date(2026, 6, 3))
    prevs = [l for l in levels if l["name"].startswith("prev") and l["name"].endswith(("_high", "_low"))]
    assert prevs, "expected prev-day/week universe levels"
    assert all("window_end" in l for l in prevs), "every prev universe level must carry window_end"


# ===========================================================================
# Skip-if-either-invalidated: a retired level is inert (no fire) on a fresh divergence
# ===========================================================================
def test_latched_level_is_skipped_no_fire():
    lm = _levels(prev1_day_high=21000.0)
    le = _levels(prev1_day_high=3000.0)
    # Bar 1: both run well above → both touch (no divergence, no fire) + latch both.
    r1, state = detect_regular_smts(
        lm, le, _bar(21050.0, 21040.0, 21045.0, "t1"), _bar(3010.0, 3007.0, 3008.0, "t1"), {})
    assert r1 == []
    assert _inv(state, "prev1_day_high")["mnq"] is True
    # Bar 2: a divergent up-sweep (MNQ touches, MES does not) that WOULD fire short —
    # but the level is depleted → skipped.
    r2, state = detect_regular_smts(
        lm, le, _bar(21001.0, 20990.0, 20995.0, "t2"), _bar(2999.0, 2990.0, 2995.0, "t2"), state)
    assert r2 == []


def test_same_divergence_fires_without_prior_latch_control():
    # Control for the test above: the identical bar 2 fires when the level is NOT retired.
    lm = _levels(prev1_day_high=21000.0)
    le = _levels(prev1_day_high=3000.0)
    r, _ = detect_regular_smts(
        lm, le, _bar(21001.0, 20990.0, 20995.0), _bar(2999.0, 2990.0, 2995.0), {})
    assert len(r) == 1 and r[0]["direction"] == "short"


def test_asymmetric_one_ticker_depleted_skips_pair():
    # Q8 shape: one ticker (MES) cleared the level in a rally (retired); the other (MNQ)
    # later re-touches divergently. EITHER-ticker retirement → no spurious SMT.
    # asia_high → ("fixed","session"); FULFILL session = MNQ 20 / MES 3.
    lm = _levels(asia_high=21000.0)
    le = _levels(asia_high=3000.0)
    # Bar 1: MES spikes well above (>= 3000+3) → MES retired; MNQ only +5 (< +20) → not.
    _, state = detect_regular_smts(
        lm, le, _bar(21005.0, 20995.0, 21000.0, "t1"), _bar(3010.0, 3005.0, 3008.0, "t1"), {})
    assert _inv(state, "asia_high")["mes"] is True
    assert _inv(state, "asia_high")["mnq"] is False
    # Bar 2: MNQ re-touches its asia_high (MES has retraced back below) → divergent up-sweep
    # that WOULD fire, but MES retired the level → skipped.
    r2, state = detect_regular_smts(
        lm, le, _bar(21001.0, 20990.0, 20995.0, "t2"), _bar(2999.0, 2990.0, 2995.0, "t2"), state)
    assert r2 == []


def test_body_pass_skips_invalidated_level():
    lm = _levels(prev1_day_high=21000.0)
    le = _levels(prev1_day_high=3000.0)
    # Wick pass latches the level (both run well above).
    _, state = detect_regular_smts(
        lm, le, _bar(21050.0, 21040.0, 21045.0, "t1"), _bar(3010.0, 3007.0, 3008.0, "t1"), {})
    assert _inv(state, "prev1_day_high")["mnq"] is True
    # Body (hidden) pass on a divergent close must also skip the retired level.
    # MNQ close >= level (touch by body), MES close < level (no touch) → would fire short.
    bm = _bar(21010.0, 20990.0, 21001.0, "t2")
    be = _bar(2999.0, 2980.0, 2995.0, "t2")
    r, state = detect_hidden_smts(lm, le, bm, be, "1m", state)
    assert r == []


# ===========================================================================
# Fixed-level re-arm-until-invalidated (AC#3 / GIL-25 2026-06-13 comment)
# ===========================================================================
def test_fixed_level_refires_after_departure_and_retouch():
    lm = _levels(prev1_day_high=21000.0)
    le = _levels(prev1_day_high=3000.0)
    # Bar 1: divergent up-sweep → fires short.
    r1, s = detect_regular_smts(
        lm, le, _bar(21001.0, 20990.0, 20995.0, "t1"), _bar(2999.0, 2990.0, 2995.0, "t1"), {})
    assert len(r1) == 1
    # Bar 2: price reverses well below the level (close departs by >= FULFILL day 40) → no touch.
    r2, s = detect_regular_smts(
        lm, le, _bar(20958.0, 20945.0, 20950.0, "t2"), _bar(2985.0, 2980.0, 2982.0, "t2"), s)
    assert r2 == []
    # Bar 3: a fresh re-approach, divergent again → re-fires (fixed re-arm on a fresh re-visit).
    r3, s = detect_regular_smts(
        lm, le, _bar(21001.0, 20990.0, 20995.0, "t3"), _bar(2999.0, 2990.0, 2995.0, "t3"), s)
    assert len(r3) == 1


def test_fixed_level_no_refire_without_departure():
    lm = _levels(prev1_day_high=21000.0)
    le = _levels(prev1_day_high=3000.0)
    # Bar 1: fires short.
    r1, s = detect_regular_smts(
        lm, le, _bar(21001.0, 20990.0, 20995.0, "t1"), _bar(2999.0, 2990.0, 2995.0, "t1"), {})
    assert len(r1) == 1
    # Bar 2: a tiny pullback — cond drops but price stays within the departure margin
    # (|close-level| = 10 < 40) → NOT departed.
    r2, s = detect_regular_smts(
        lm, le, _bar(20998.0, 20988.0, 20990.0, "t2"), _bar(2997.0, 2988.0, 2990.0, "t2"), s)
    assert r2 == []
    # Bar 3: re-touch — but no departure occurred → not re-armed → no re-fire (single fire).
    r3, s = detect_regular_smts(
        lm, le, _bar(21001.0, 20990.0, 20995.0, "t3"), _bar(2999.0, 2990.0, 2995.0, "t3"), s)
    assert r3 == []


def test_invalidated_fixed_level_never_refires():
    lm = _levels(prev1_day_high=21000.0)
    le = _levels(prev1_day_high=3000.0)
    # Bar 1: fires short.
    r1, s = detect_regular_smts(
        lm, le, _bar(21001.0, 20990.0, 20995.0, "t1"), _bar(2999.0, 2990.0, 2995.0, "t1"), {})
    assert len(r1) == 1
    # Bar 2: BOTH run well above level+thr → retire the level (both touch → no fire).
    r2, s = detect_regular_smts(
        lm, le, _bar(21050.0, 21040.0, 21045.0, "t2"), _bar(3010.0, 3007.0, 3008.0, "t2"), s)
    assert _inv(s, "prev1_day_high")["mnq"] is True
    # Bar 3: a fresh divergent re-approach that would otherwise re-fire → retired forever.
    r3, s = detect_regular_smts(
        lm, le, _bar(21001.0, 20990.0, 20995.0, "t3"), _bar(2999.0, 2990.0, 2995.0, "t3"), s)
    assert r3 == []
