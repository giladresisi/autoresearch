# tests/test_smt_invalidation.py
# Unit tests for the Phase 1.1.5 (GIL-25) SMT lifecycle rework in the pure SMT V2 detection
# engine (smt_detect._detect_level_smts).
#
# The adverse-run invalidation mechanism (a fired SMT killed when MNQ close ran _invalidate_pts
# AGAINST the fire close) was REMOVED in Phase 1.1.5 — it was the too-tight killer. The tests
# below assert the NEW behavior:
#   - an adverse run NO LONGER invalidates a fired record (it stays pending);
#   - the state["__invalidations__"] trail is never created by detection;
#   - fulfillment is still terminal;
#   - supersession (a fresher same-direction record retires older ones) marks st["superseded"];
#   - opposite-direction fires never supersede; supersession is informational (records unchanged);
#   - a re-armed/re-fired fixed level clears superseded.
#
# The depletion-latch backstop (st["retired_depleted"]) is covered in
# tests/test_smt_level_invalidation.py.

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
# 1. adverse run NO LONGER invalidates (short + long mirror)
# ===========================================================================
def test_adverse_run_no_longer_invalidates_short():
    state = {}
    recs, state = _fire_short(state)
    assert [r["ref_name"] for r in recs] == ["prev1_day_high"]
    skey = "prev1_day_high|short|wick"
    fc = state[skey]["fire_mnq_close"]
    assert fc == 20995.0
    assert state[skey]["invalidated"] is False
    inv = _invalidate_pts("day", "mnq")

    # Adverse-up: close >= fc + inv = the OLD invalidation trigger.
    recs, state = _drift_short(state, mnq_close=fc + inv, t="2026-06-09T10:01:00")
    assert recs == []
    # NEW: the fired record is NOT invalidated and stays pending (fired & not terminal).
    assert state[skey]["invalidated"] is False
    assert state[skey]["fired"] is True
    assert state[skey]["fulfilled"] is False
    assert state[skey].get("superseded") is False
    assert state[skey].get("retired_depleted") is False
    # NEW: no invalidation trail is created.
    assert "__invalidations__" not in state


def test_adverse_run_no_longer_invalidates_long():
    state = {}
    recs, state = _fire_long(state)
    assert [r["ref_name"] for r in recs] == ["prev1_day_low"]
    skey = "prev1_day_low|long|wick"
    fc = state[skey]["fire_mnq_close"]
    assert fc == 21005.0
    inv = _invalidate_pts("day", "mnq")

    # Adverse-down: close <= fc - inv = the OLD invalidation trigger.
    recs, state = _drift_long(state, mnq_close=fc - inv, t="2026-06-09T10:01:00")
    assert recs == []
    assert state[skey]["invalidated"] is False
    assert state[skey]["fired"] is True
    assert "__invalidations__" not in state


# ===========================================================================
# 2. no __invalidations__ trail is created across several adverse bars
# ===========================================================================
def test_no_invalidations_trail_created():
    state = {}
    recs, state = _fire_short(state)
    skey = "prev1_day_high|short|wick"
    fc = state[skey]["fire_mnq_close"]
    inv = _invalidate_pts("day", "mnq")

    recs, state = _drift_short(state, mnq_close=fc + inv, t="2026-06-09T10:01:00")
    recs, state = _drift_short(state, mnq_close=fc + inv + 30.0, t="2026-06-09T10:02:00")
    recs, state = _drift_short(state, mnq_close=fc + inv + 50.0, t="2026-06-09T10:03:00")
    assert "__invalidations__" not in state
    assert state[skey]["invalidated"] is False


# ===========================================================================
# 3. fulfillment is still terminal (UNCHANGED behavior)
# ===========================================================================
def test_fulfillment_still_terminal():
    # A SHORT SMT: a FAVORABLE (down) follow-through past fc - FULFILL_PTS["day"]=40 fulfills.
    # A FIXED level is SINGLE-FIRE: fulfillment marks it fulfilled but never re-arms it (only
    # DYNAMIC day_/week_ levels re-arm). The key guarantee: a favorable move fulfills and never
    # produces an invalidation.
    state = {}
    recs, state = _fire_short(state)
    skey = "prev1_day_high|short|wick"
    fc = state[skey]["fire_mnq_close"]  # 20995
    # Favorable DOWN move past fc - 45 → fulfilled; the fixed level stays fired (no re-arm).
    recs, state = _drift_short(state, mnq_close=fc - 45.0, t="2026-06-09T10:01:00")
    assert state[skey]["fulfilled"] is True
    assert state[skey]["armed"] is False         # fixed level is single-fire — never re-arms
    assert state[skey]["invalidated"] is False
    assert state.get("__invalidations__", []) == []


# ===========================================================================
# 4. dynamic re-arm still resets the lifecycle flags (no invalidation involved)
# ===========================================================================
def test_dynamic_rearm_resets_flags_no_invalidation():
    # A DYNAMIC level (day_high, short) fires; an adverse-up run does NOT invalidate it (new
    # lifecycle). An opposite-direction (long) SMT in a later batch re-arms it → fired/fulfilled/
    # superseded/retired_depleted all reset to False, armed=True.
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

    # Bar 2: adverse-up close >= fc + 20 → NO invalidation any more.
    recs, state = detect_regular_smts(
        lm, le, _bar(fc + inv, fc + inv - 1.0, fc + inv), _bar(2990.0, 2985.0, 2988.0), state)
    assert state[skey]["invalidated"] is False
    assert "__invalidations__" not in state

    # Bar 3: an opposite-direction (long) SMT on day_low fires → re-arms the dynamic day_high
    # (post-pass): fired/fulfilled/superseded/retired_depleted reset.
    recs, state = detect_regular_smts(
        lm, le, _bar(20990.0, 19999.0, 20100.0), _bar(2990.0, 2001.0, 2100.0), state)
    assert any(r["ref_name"] == "day_low" for r in recs)
    assert state[skey]["armed"] is True
    assert state[skey]["fired"] is False
    assert state[skey]["fulfilled"] is False
    assert state[skey]["superseded"] is False
    assert state[skey]["retired_depleted"] is False


# ===========================================================================
# 5. records list is byte-identical with/without an adverse run (informational-only guarantee)
# ===========================================================================
def test_records_unchanged_by_adverse_run():
    inv = _invalidate_pts("day", "mnq")

    # Run A: fire, then a NON-adverse drift.
    state_a = {}
    recs_a1, state_a = _fire_short(state_a)
    recs_a2, state_a = _drift_short(state_a, mnq_close=20995.0 + inv - 5.0, t="2026-06-09T10:01:00")

    # Run B: fire, then an ADVERSE drift (used to invalidate; now a no-op).
    state_b = {}
    recs_b1, state_b = _fire_short(state_b)
    recs_b2, state_b = _drift_short(state_b, mnq_close=20995.0 + inv, t="2026-06-09T10:01:00")

    assert recs_a1 == recs_b1
    assert recs_a2 == recs_b2 == []
    # Neither run produced an invalidation trail.
    assert "__invalidations__" not in state_a
    assert "__invalidations__" not in state_b


# ===========================================================================
# NOTE: the old test_invalidation_survives_direction_flip regression (the 06-03
# prev1_week_high|short strand bug) is MOOT in Phase 1.1.5 — the adverse-run invalidation
# mechanism it guarded was removed entirely, so a fired SMT can no longer be "stranded" by a
# per-bar direction flip (there is nothing to strand). It is intentionally not ported.
# ===========================================================================


# ===========================================================================
# Supersession (NEW, Phase 1.1.5 / GIL-25 §A.2.3)
# ===========================================================================
def _fire_long_on(state, name, mnq_lvl, mes_lvl, t):
    """Fire a long (swept *_low) SMT on an arbitrary level name (fixed day tier here)."""
    lm = _levels(**{name: mnq_lvl})
    le = _levels(**{name: mes_lvl})
    # MNQ dips onto its low (touch), MES holds above (no touch) → divergent down-sweep → long.
    mnq = _bar(high=mnq_lvl + 10.0, low=mnq_lvl - 1.0, close=mnq_lvl + 5.0, time=t)
    mes = _bar(high=mes_lvl + 10.0, low=mes_lvl + 5.0, close=mes_lvl + 7.0, time=t)
    return detect_regular_smts(lm, le, mnq, mes, state)


def _fire_short_on(state, name, mnq_lvl, mes_lvl, t):
    """Fire a short (swept *_high) SMT on an arbitrary level name."""
    lm = _levels(**{name: mnq_lvl})
    le = _levels(**{name: mes_lvl})
    mnq = _bar(high=mnq_lvl + 1.0, low=mnq_lvl - 10.0, close=mnq_lvl - 5.0, time=t)
    mes = _bar(high=mes_lvl - 1.0, low=mes_lvl - 10.0, close=mes_lvl - 5.0, time=t)
    return detect_regular_smts(lm, le, mnq, mes, state)


def test_later_same_direction_supersedes_earlier():
    state = {}
    # Fire a long on level X (prev1_day_low).
    r1, state = _fire_long_on(state, "prev1_day_low", 21000.0, 3000.0, "t1")
    assert [r["ref_name"] for r in r1] == ["prev1_day_low"]
    kx = "prev1_day_low|long|wick"
    assert state[kx]["fired"] is True and state[kx]["superseded"] is False

    # Later fire a long on a DIFFERENT level Y (prev2_day_low) — different level, same direction.
    r2, state = _fire_long_on(state, "prev2_day_low", 22000.0, 3300.0, "t2")
    ky = "prev2_day_low|long|wick"
    assert [r["ref_name"] for r in r2] == ["prev2_day_low"]   # only Y emitted this batch
    # X is now superseded by Y; Y is fresh (not superseded).
    assert state[kx]["superseded"] is True
    assert state[ky]["superseded"] is False
    sups = state["__supersessions__"]
    assert len(sups) == 1
    assert sups[0]["key"] == kx
    assert sups[0]["superseded_by"] == ky
    assert sups[0]["direction"] == "long"
    assert sups[0]["type"] == "wick"
    assert sups[0]["ref_name"] == "prev1_day_low"


def test_same_batch_same_direction_fires_do_not_supersede_each_other():
    # Two same-direction (long) levels swept in ONE batch must NOT supersede each other — both
    # are the freshest of their direction this bar. (Regression: the old `skey == fresh_key`
    # self-guard left siblings superseding each other → both wrongly dropped from the active set.)
    lm = _levels(prev1_day_low=21000.0, prev2_day_low=20000.0)
    le = _levels(prev1_day_low=3000.0, prev2_day_low=2800.0)
    # MNQ dips onto BOTH lows (touch), MES holds above (no touch) → both fire long this batch.
    mnq = _bar(21010.0, 19999.0, 20500.0, "t1")
    mes = _bar(3010.0, 3005.0, 3007.0, "t1")
    recs, state = detect_regular_smts(lm, le, mnq, mes, {})
    assert {r["ref_name"] for r in recs} == {"prev1_day_low", "prev2_day_low"}
    assert state["prev1_day_low|long|wick"]["superseded"] is False
    assert state["prev2_day_low|long|wick"]["superseded"] is False
    assert "__supersessions__" not in state


def test_opposite_direction_does_not_supersede():
    state = {}
    r1, state = _fire_long_on(state, "prev1_day_low", 21000.0, 3000.0, "t1")
    kx = "prev1_day_low|long|wick"
    assert state[kx]["fired"] is True
    # Later fire a SHORT on a different level → opposite direction → must NOT supersede the long.
    r2, state = _fire_short_on(state, "prev1_day_high", 22000.0, 3300.0, "t2")
    assert any(r["direction"] == "short" for r in r2)
    assert state[kx]["superseded"] is False
    assert "__supersessions__" not in state


def test_supersession_does_not_change_records():
    # The records list of the second (superseding) batch is identical whether or not an older
    # same-direction record exists to be superseded → informational-only guarantee.
    # Run A: only the second fire (no prior long to supersede).
    state_a = {}
    ra, state_a = _fire_long_on(state_a, "prev2_day_low", 22000.0, 3300.0, "t2")
    # Run B: an earlier long exists, then the same second fire (which supersedes it).
    state_b = {}
    _, state_b = _fire_long_on(state_b, "prev1_day_low", 21000.0, 3000.0, "t1")
    rb, state_b = _fire_long_on(state_b, "prev2_day_low", 22000.0, 3300.0, "t2")
    assert ra == rb
    # The surviving (fresh) record's fire bookkeeping is unaffected by the supersession.
    ky = "prev2_day_low|long|wick"
    for f in ("fired", "fulfilled", "armed", "fire_mnq_close", "fire_price", "fire_time"):
        assert state_a[ky][f] == state_b[ky][f]


def test_superseded_fixed_level_stays_terminal_no_refire():
    # A fixed level superseded by a fresher same-direction record is SINGLE-FIRE: a later
    # depart + re-touch does NOT re-arm or re-fire it, and superseded stays True (only DYNAMIC
    # day_/week_ levels re-arm and clear superseded).
    state = {}
    # Bar 1: fire long on X.
    _, state = _fire_long_on(state, "prev1_day_low", 21000.0, 3000.0, "t1")
    kx = "prev1_day_low|long|wick"
    # Bar 2: fire long on Y → supersedes X.
    _, state = _fire_long_on(state, "prev2_day_low", 22000.0, 3300.0, "t2")
    assert state[kx]["superseded"] is True
    # Bar 3: X departs UPWARD (price reverses far ABOVE the low level) — no touch, no depletion
    # (an upward move past a *_low does not trip its below-level latch). A fixed level never
    # re-arms, so the departure changes nothing.
    lm = _levels(prev1_day_low=21000.0)
    le = _levels(prev1_day_low=3000.0)
    _, state = detect_regular_smts(
        lm, le, _bar(21050.0, 21040.0, 21045.0, "t3"), _bar(3050.0, 3040.0, 3045.0, "t3"), state)
    assert state[kx]["armed"] is False        # fixed level never re-arms
    assert state[kx]["superseded"] is True    # stays terminal
    # Bar 4: a fresh re-approach on X → does NOT re-fire (single fire for the session).
    r4, state = detect_regular_smts(
        lm, le, _bar(21010.0, 20999.0, 21005.0, "t4"), _bar(3010.0, 3005.0, 3007.0, "t4"), state)
    assert r4 == []
    assert state[kx]["fired"] is True
    assert state[kx]["superseded"] is True
