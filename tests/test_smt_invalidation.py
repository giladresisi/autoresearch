# tests/test_smt_invalidation.py
# Unit tests for adverse-run invalidation in the pure SMT V2 detection engine
# (smt_detect._detect_level_smts). Invalidation is the mirror of fulfillment: a fired,
# not-yet-fulfilled SMT whose MNQ close runs AGAINST its direction past the fire close by
# _invalidate_pts(tier) is flagged st["invalidated"]=True and appends ONE structured event
# to the reserved state["__invalidations__"] list (no plot, no record change).

from __future__ import annotations

import smt_detect
from smt_detect import (
    INVALIDATE_PTS_MNQ,
    detect_regular_smts,
    _invalidate_pts,
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


# A fixed prev-day HIGH swept from BELOW (rising into it) is an UP-sweep → BEARISH/short.
# MNQ leads (touches), MES holds below. fire_mnq_close = the MNQ close at the firing bar.
def _fire_short(state, t="2026-06-09T10:00:00"):
    lm = _levels(prev1_day_high=21000.0)
    le = _levels(prev1_day_high=3000.0)
    mnq = _bar(high=21001.0, low=20990.0, close=20995.0, time=t)  # close below ⇒ from below
    mes = _bar(high=2999.0, low=2990.0, close=2995.0, time=t)     # MES holds below
    recs, state = detect_regular_smts(lm, le, mnq, mes, state)
    return recs, state


# A fixed prev-day LOW swept from ABOVE (dipping onto it) is a DOWN-sweep → BULLISH/long.
def _fire_long(state, t="2026-06-09T10:00:00"):
    lm = _levels(prev1_day_low=21000.0)
    le = _levels(prev1_day_low=3000.0)
    mnq = _bar(high=21010.0, low=20999.0, close=21005.0, time=t)  # close above ⇒ from above
    mes = _bar(high=3010.0, low=3005.0, close=3007.0, time=t)     # MES holds above
    recs, state = detect_regular_smts(lm, le, mnq, mes, state)
    return recs, state


# A non-firing bar that just carries MNQ/MES closes forward (levels not touched by either).
def _drift_short(state, mnq_close, t):
    lm = _levels(prev1_day_high=21000.0)
    le = _levels(prev1_day_high=3000.0)
    # No touch on either instrument (both wicks stay below the level) ⇒ no fire/re-arm.
    mnq = _bar(high=mnq_close, low=mnq_close - 1.0, close=mnq_close, time=t)
    mes = _bar(high=2990.0, low=2985.0, close=2988.0, time=t)
    return detect_regular_smts(lm, le, mnq, mes, state)


def _drift_long(state, mnq_close, t):
    lm = _levels(prev1_day_low=21000.0)
    le = _levels(prev1_day_low=3000.0)
    mnq = _bar(high=mnq_close + 1.0, low=mnq_close, close=mnq_close, time=t)
    mes = _bar(high=3010.0, low=3008.0, close=3009.0, time=t)
    return detect_regular_smts(lm, le, mnq, mes, state)


# ===========================================================================
# 1. short invalidated when close runs UP past threshold
# ===========================================================================
def test_short_invalidated_when_close_runs_up_past_threshold():
    state = {}
    recs, state = _fire_short(state)
    assert [r["ref_name"] for r in recs] == ["prev1_day_high"]
    skey = "prev1_day_high|short|wick"
    fc = state[skey]["fire_mnq_close"]
    assert fc == 20995.0
    inv = _invalidate_pts("day", "mnq")
    assert state[skey]["invalidated"] is False

    # Adverse-up: close >= fc + inv = 21015.0
    recs, state = _drift_short(state, mnq_close=fc + inv, t="2026-06-09T10:01:00")
    assert recs == []  # invalidation adds no record
    assert state[skey]["invalidated"] is True
    trail = state["__invalidations__"]
    assert len(trail) == 1
    ev = trail[0]
    assert ev["reason"] == "adverse_run"
    assert ev["key"] == skey
    assert ev["ref_name"] == "prev1_day_high"
    assert ev["tier"] == "day"
    assert ev["direction"] == "short"
    assert ev["type"] == "wick"
    assert ev["fire_mnq_close"] == 20995.0
    assert ev["trigger_mnq_close"] == fc + inv
    assert ev["threshold_pts"] == inv
    assert ev["fire_time"] == "2026-06-09T10:00:00"
    assert ev["time"] == "2026-06-09T10:01:00"
    assert state[skey]["invalidated_time"] == "2026-06-09T10:01:00"
    assert state[skey]["invalidated_mnq_close"] == fc + inv


# ===========================================================================
# 2. long invalidated when close runs DOWN past threshold (mirror)
# ===========================================================================
def test_long_invalidated_when_close_runs_down_past_threshold():
    state = {}
    recs, state = _fire_long(state)
    assert [r["ref_name"] for r in recs] == ["prev1_day_low"]
    skey = "prev1_day_low|long|wick"
    fc = state[skey]["fire_mnq_close"]
    assert fc == 21005.0
    inv = _invalidate_pts("day", "mnq")

    # Adverse-down: close <= fc - inv = 20985.0
    recs, state = _drift_long(state, mnq_close=fc - inv, t="2026-06-09T10:01:00")
    assert recs == []
    assert state[skey]["invalidated"] is True
    trail = state["__invalidations__"]
    assert len(trail) == 1
    ev = trail[0]
    assert ev["reason"] == "adverse_run"
    assert ev["direction"] == "long"
    assert ev["fire_mnq_close"] == 21005.0
    assert ev["trigger_mnq_close"] == fc - inv
    assert ev["threshold_pts"] == inv


# ===========================================================================
# 3. not invalidated just below threshold
# ===========================================================================
def test_not_invalidated_just_below_threshold():
    state = {}
    recs, state = _fire_short(state)
    skey = "prev1_day_high|short|wick"
    fc = state[skey]["fire_mnq_close"]
    inv = _invalidate_pts("day", "mnq")

    # close at fc + inv - epsilon → NOT invalidated
    recs, state = _drift_short(state, mnq_close=fc + inv - 0.25, t="2026-06-09T10:01:00")
    assert state[skey]["invalidated"] is False
    assert state.get("__invalidations__", []) == []


# ===========================================================================
# 4. fulfillment takes precedence in the same bar
# ===========================================================================
def test_fulfillment_takes_precedence_same_bar():
    # A long SMT: fulfillment is a FAVORABLE (up) move past fc + FULFILL_PTS["day"]=40.
    # Invalidation is an ADVERSE (down) move. They can't both numerically trigger on the same
    # close, so to test precedence we craft a SHORT SMT where a SINGLE bar satisfies both:
    #   - fulfill (short, favorable=DOWN): close <= fc - 40
    #   - invalidate (short, adverse=UP): close >= fc + 20
    # These are mutually exclusive numerically, so precedence is instead proven by construction:
    # the invalidation branch is guarded by `not st.get("fulfilled")` re-checked AFTER the
    # fulfillment branch ran this bar. We verify a bar that fulfills never sets invalidated.
    # R2 note: for a FIXED level a favorable follow-through also DEPARTS the level (price left
    # the level region) → it re-arms (fired→False). Either way the key guarantee holds: a
    # favorable move never produces an ADVERSE-run invalidation.
    state = {}
    recs, state = _fire_short(state)
    skey = "prev1_day_high|short|wick"
    fc = state[skey]["fire_mnq_close"]  # 20995
    # Favorable DOWN move past fc - 40 = 20955 → fulfilled, then (R2) departed → re-armed.
    recs, state = _drift_short(state, mnq_close=fc - 45.0, t="2026-06-09T10:01:00")
    assert state[skey]["armed"] is True          # R2: held reversal re-armed the fixed level
    assert state[skey]["invalidated"] is False
    assert state.get("__invalidations__", []) == []


# ===========================================================================
# 5. invalidation event emitted exactly once
# ===========================================================================
def test_invalidation_event_emitted_once():
    state = {}
    recs, state = _fire_short(state)
    skey = "prev1_day_high|short|wick"
    fc = state[skey]["fire_mnq_close"]
    inv = _invalidate_pts("day", "mnq")

    recs, state = _drift_short(state, mnq_close=fc + inv, t="2026-06-09T10:01:00")
    assert state[skey]["invalidated"] is True
    assert len(state["__invalidations__"]) == 1

    # Further adverse bars (still above threshold) must NOT append duplicates.
    recs, state = _drift_short(state, mnq_close=fc + inv + 30.0, t="2026-06-09T10:02:00")
    recs, state = _drift_short(state, mnq_close=fc + inv + 50.0, t="2026-06-09T10:03:00")
    assert len(state["__invalidations__"]) == 1


# ===========================================================================
# 6. dynamic re-arm resets invalidated
# ===========================================================================
def test_dynamic_rearm_resets_invalidated():
    # A DYNAMIC level (day_high, short) fires, gets invalidated by an adverse-up run, then is
    # re-armed by an opposite-direction (long) SMT in a later batch → invalidated resets False.
    lm = _levels(day_high=21000.0, day_low=20000.0)
    le = _levels(day_high=3000.0, day_low=2000.0)
    state = {}
    # Bar 1: MNQ touches day_high (short fires), MES not; day_low not hit.
    recs, state = detect_regular_smts(
        lm, le, _bar(21001.0, 20500.0, 20800.0), _bar(2999.0, 2500.0, 2800.0), state)
    skey = "day_high|short|wick"
    assert any(r["ref_name"] == "day_high" for r in recs)
    fc = state[skey]["fire_mnq_close"]  # 20800
    inv = _invalidate_pts("day", "mnq")

    # Bar 2: adverse-up close >= fc + 20 → day_high short invalidated. (No level touched.)
    recs, state = detect_regular_smts(
        lm, le, _bar(fc + inv, fc + inv - 1.0, fc + inv), _bar(2990.0, 2985.0, 2988.0), state)
    assert state[skey]["invalidated"] is True

    # Bar 3: an opposite-direction (long) SMT on day_low fires → re-arms the dynamic day_high
    # (post-pass), resetting invalidated to False.
    recs, state = detect_regular_smts(
        lm, le, _bar(20990.0, 19999.0, 20100.0), _bar(2990.0, 2001.0, 2100.0), state)
    assert any(r["ref_name"] == "day_low" for r in recs)
    assert state[skey]["armed"] is True
    assert state[skey]["invalidated"] is False
    assert state[skey]["fired"] is False


# ===========================================================================
# 7. fixed-level invalidation does not change records
# ===========================================================================
def test_fixed_level_invalidation_does_not_change_records():
    inv = _invalidate_pts("day", "mnq")

    # Run A: fire, then a NON-adverse drift (no invalidation).
    state_a = {}
    recs_a1, state_a = _fire_short(state_a)
    recs_a2, state_a = _drift_short(state_a, mnq_close=20995.0 + inv - 5.0, t="2026-06-09T10:01:00")

    # Run B: fire, then an ADVERSE drift (invalidation trips).
    state_b = {}
    recs_b1, state_b = _fire_short(state_b)
    recs_b2, state_b = _drift_short(state_b, mnq_close=20995.0 + inv, t="2026-06-09T10:01:00")

    # The records list is byte-for-byte identical with/without invalidation.
    assert recs_a1 == recs_b1
    assert recs_a2 == recs_b2 == []
    # And invalidation only differs in the reserved trail key.
    assert state_a.get("__invalidations__", []) == []
    assert len(state_b["__invalidations__"]) == 1


# ===========================================================================
# 8. reserved key skipped by the post-pass + contains no "|"
# ===========================================================================
def test_reserved_key_skipped_by_postpass():
    # Drive a batch that BOTH invalidates a fired SMT AND triggers the post-pass re-arm
    # (records non-empty), then assert __invalidations__ is untouched by the post-pass and is
    # a list (not split/keyed), and its key contains no "|".
    lm = _levels(day_high=21000.0, day_low=20000.0)
    le = _levels(day_high=3000.0, day_low=2000.0)
    state = {}
    # Bar 1: day_high short fires.
    recs, state = detect_regular_smts(
        lm, le, _bar(21001.0, 20500.0, 20800.0), _bar(2999.0, 2500.0, 2800.0), state)
    skey = "day_high|short|wick"
    fc = state[skey]["fire_mnq_close"]
    inv = _invalidate_pts("day", "mnq")

    # Bar 2: adverse-up invalidates day_high short.
    recs, state = detect_regular_smts(
        lm, le, _bar(fc + inv, fc + inv - 1.0, fc + inv), _bar(2990.0, 2985.0, 2988.0), state)
    assert state[skey]["invalidated"] is True
    assert "__invalidations__" in state
    assert len(state["__invalidations__"]) == 1

    # Bar 3: a long SMT fires (records non-empty → post-pass runs). The reserved list key must
    # NOT be treated as a level-SMT entry: it has no "|", so the guard short-circuits before
    # any .get on the list. The list survives intact.
    recs, state = detect_regular_smts(
        lm, le, _bar(20990.0, 19999.0, 20100.0), _bar(2990.0, 2001.0, 2100.0), state)
    assert any(r["ref_name"] == "day_low" for r in recs)
    assert isinstance(state["__invalidations__"], list)
    assert len(state["__invalidations__"]) == 1
    assert "|" not in "__invalidations__"
    # No reserved key accidentally split into a level entry.
    assert all("|" in k or k.startswith("__") for k in state.keys())


# ===========================================================================
# 9. invalidation survives the per-bar direction FLIP (regression for the
#    prev1_week_high|short 09:49 strand bug)
# ===========================================================================
def _cross_bar(state, mnqc, mesc, t):
    # Both instruments sit clearly ABOVE prev1_day_high (21000 / 3000) with no wick touching
    # either level, so no new SMT fires — the bar only carries the closes (and the approach
    # reference) forward. Used to walk price up THROUGH the level after a short has fired.
    lm = _levels(prev1_day_high=21000.0)
    le = _levels(prev1_day_high=3000.0)
    mnq = _bar(mnqc + 1.0, mnqc - 1.0, mnqc, t)
    mes = _bar(mesc + 1.0, mesc - 1.0, mesc, t)
    return detect_regular_smts(lm, le, mnq, mes, state)


def test_invalidation_survives_direction_flip():
    # Fixed-level adverse-run invalidation must NOT be keyed to the CURRENT bar's approach
    # direction. A short fires while price approaches from below (prev_ref < level ⇒ short).
    # As price then runs UP THROUGH the level, the approach reference crosses above it, so the
    # per-bar computed direction flips to LONG and the engine starts keying the |long state —
    # which would strand the original |short key's invalidation check (the real 06-03 bug where
    # prev1_week_high|short fired 09:49:25 and never invalidated though price crossed +40 by
    # 09:51:37). The post-loop maintenance pass must still invalidate the |short SMT.
    state = {}
    recs, state = _fire_short(state)  # close 20995, prev_ref→20995 (< level 21000) ⇒ short
    skey = "prev1_day_high|short|wick"
    fc = state[skey]["fire_mnq_close"]  # 20995
    inv = _invalidate_pts("day", "mnq")  # day INVALIDATE_PTS (threshold-relative; tune-proof)
    level = 21000.0
    assert fc < level and fc + inv > level  # bug only reproduces if the threshold is ABOVE the level
    assert state[skey]["invalidated"] is False

    # Bar 2: ABOVE the level but BELOW the +inv threshold. prev_ref is still 20995 (< level), so
    # this bar is computed as short; both legs sit above their levels ⇒ no touch, no fire.
    recs, state = _cross_bar(state, level + 10.0, 3010.0, "2026-06-09T10:01:00")
    assert recs == []
    assert state[skey]["invalidated"] is False  # not adverse enough yet

    # Bar 3: close at fc + inv (>= threshold). prev_ref is now level+10 (> level) ⇒ the per-bar
    # direction FLIPS to long, so the engine keys |long this bar — the OLD in-loop check would
    # never touch the |short state again. The direction-independent pass must still invalidate it.
    recs, state = _cross_bar(state, fc + inv, 3015.0, "2026-06-09T10:02:00")
    assert recs == []  # no new SMT fired on the crossing bar
    assert state[skey]["invalidated"] is True, "short SMT stranded after direction flip (the bug)"
    trail = state["__invalidations__"]
    assert len(trail) == 1
    ev = trail[0]
    assert ev["key"] == skey and ev["direction"] == "short" and ev["reason"] == "adverse_run"
    assert ev["trigger_mnq_close"] == fc + inv
    assert ev["time"] == "2026-06-09T10:02:00"
    assert state[skey]["invalidated_time"] == "2026-06-09T10:02:00"
