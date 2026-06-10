# tests/test_smt_detect.py
# Unit tests for the pure SMT V2 detection engine (smt_detect.py): regular (wick)
# SMTs, hidden (body) SMTs, SMT-fills, SmtBuffer, PendingSmtWatch. No pipeline.

from __future__ import annotations

import pandas as pd

import smt_detect
from smt_detect import (
    MIN_REARM_OPP_MOVE_PTS_MNQ,
    WATCH_CONFIRM_PTS_MNQ,
    SmtBuffer,
    PendingSmtWatch,
    detect_regular_smts,
    detect_hidden_smts,
    detect_fill_smts,
    eligible_levels,
)


# ---------------------------------------------------------------------------
# Builders
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


# ===========================================================================
# Regular (wick) SMT
# ===========================================================================
def test_wick_smt_fires_on_divergence():
    lm = _levels(day_high=21000.0)
    le = _levels(day_high=3000.0)
    mnq = _bar(high=21001.0, low=20990.0, close=20995.0)   # touches 21000
    mes = _bar(high=2999.0, low=2990.0, close=2995.0)      # does NOT touch 3000
    recs, _ = detect_regular_smts(lm, le, mnq, mes, {})
    assert len(recs) == 1
    r = recs[0]
    assert r["kind"] == "smt" and r["type"] == "wick"
    assert r["side"] == "bearish" and r["direction"] == "short"
    assert r["leader"] == "mnq" and r["ref_name"] == "day_high"
    assert r["timeframe"] == "1m"


def test_no_fire_when_both_touch():
    lm = _levels(day_high=21000.0)
    le = _levels(day_high=3000.0)
    mnq = _bar(high=21001.0, low=20990.0)
    mes = _bar(high=3001.0, low=2990.0)
    recs, _ = detect_regular_smts(lm, le, mnq, mes, {})
    assert recs == []


def test_no_fire_when_neither_touch():
    lm = _levels(day_high=21000.0)
    le = _levels(day_high=3000.0)
    mnq = _bar(high=20990.0, low=20980.0)
    mes = _bar(high=2990.0, low=2980.0)
    recs, _ = detect_regular_smts(lm, le, mnq, mes, {})
    assert recs == []


def test_symmetric_leader_mes():
    lm = _levels(day_high=21000.0)
    le = _levels(day_high=3000.0)
    mnq = _bar(high=20999.0, low=20990.0)   # MNQ does NOT touch
    mes = _bar(high=3001.0, low=2990.0)     # MES touches → MES leads
    recs, _ = detect_regular_smts(lm, le, mnq, mes, {})
    assert len(recs) == 1
    assert recs[0]["leader"] == "mes"
    assert recs[0]["direction"] == "short"


def test_low_level_long_direction():
    lm = _levels(day_low=21000.0)
    le = _levels(day_low=3000.0)
    mnq = _bar(high=21010.0, low=20999.0)   # MNQ wick below day_low
    mes = _bar(high=3010.0, low=3001.0)     # MES does not reach its low
    recs, _ = detect_regular_smts(lm, le, mnq, mes, {})
    assert len(recs) == 1
    assert recs[0]["side"] == "bullish" and recs[0]["direction"] == "long"
    assert recs[0]["leader"] == "mnq"


def test_edge_fire_once():
    lm = _levels(day_high=21000.0)
    le = _levels(day_high=3000.0)
    mnq = _bar(high=21001.0, low=20990.0, close=20995.0)
    mes = _bar(high=2999.0, low=2990.0, close=2995.0)
    state = {}
    fired = 0
    for _ in range(3):
        recs, state = detect_regular_smts(lm, le, mnq, mes, state)
        fired += len(recs)
    assert fired == 1, "persistent divergence fires exactly once (rising edge)"


def test_opp_move_alone_does_not_rearm():
    # A sub-threshold price move (no opposite-direction SMT, no fulfillment) does NOT re-arm
    # even a DYNAMIC level. After day_high short fires (fire_mnq_close=20995, day fulfill
    # threshold 40 → fulfilled only if close <= 20955), a small/medium retreat that stays
    # ABOVE 20955 and a fresh re-touch does NOT re-fire.
    lm = _levels(day_high=21000.0)
    le = _levels(day_high=3000.0)
    touch_mnq = _bar(high=21001.0, low=20990.0, close=20995.0)
    miss_mes = _bar(high=2999.0, low=2990.0, close=2995.0)
    state = {}
    recs, state = detect_regular_smts(lm, le, touch_mnq, miss_mes, state)
    assert len(recs) == 1  # fired; now dormant

    # Small retreat (close 20990 > 20955 → not fulfilled) then re-touch → no re-fire.
    away_small = _bar(high=20992.0, low=20985.0, close=20990.0)
    recs, state = detect_regular_smts(lm, le, away_small, miss_mes, state)
    assert recs == []
    recs, state = detect_regular_smts(lm, le, touch_mnq, miss_mes, state)
    assert recs == [], "small sub-threshold move must not re-arm"

    # Medium retreat that still stays above the day fulfillment threshold (close 20965 > 20955
    # → not fulfilled) then re-touch → STILL no re-fire.
    away_med = _bar(high=20970.0, low=20962.0, close=20965.0)
    recs, state = detect_regular_smts(lm, le, away_med, miss_mes, state)
    assert recs == []  # not touching, no fire
    recs, state = detect_regular_smts(lm, le, touch_mnq, miss_mes, state)
    assert recs == [], "a sub-threshold move must NOT re-arm — needs fulfillment or an opposite SMT"


def test_rearm_via_opposite_smt():
    # An opposite-direction SMT re-arms a DYNAMIC level (day_high) but NOT a FIXED level
    # (ny_evening_low). day_high short fires; a day_low long SMT in a later batch re-arms the
    # dynamic day_high; the fixed ny_evening_low stays dormant forever.
    # day_high (dynamic, short) and ny_evening_high (fixed, short) both fire in bar 1; bar 2's
    # day_low long SMT must re-arm the dynamic day_high but NOT the fixed ny_evening_high.
    lm = _levels(day_high=21000.0, day_low=20000.0, ny_evening_high=21000.0)
    le = _levels(day_high=3000.0, day_low=2000.0, ny_evening_high=3000.0)
    state = {}
    # Bar 1: MNQ touches day_high AND ny_evening_high (both short fire), MES not; day_low not hit.
    mnq1 = _bar(high=21001.0, low=20500.0, close=20800.0)
    mes1 = _bar(high=2999.0, low=2500.0, close=2800.0)
    recs, state = detect_regular_smts(lm, le, mnq1, mes1, state)
    assert any(r["ref_name"] == "day_high" for r in recs)
    assert any(r["ref_name"] == "ny_evening_high" for r in recs)

    # Bar 2: MNQ no longer touches the highs but touches day_low (long), MES neither.
    #   The day_low long record re-arms the DYNAMIC day_high in the batch (cond False this
    #   bar). The FIXED ny_evening_high (already fired) must NOT be re-armed.
    mnq2 = _bar(high=20990.0, low=19999.0, close=20100.0)
    mes2 = _bar(high=2990.0, low=2001.0, close=2100.0)
    recs, state = detect_regular_smts(lm, le, mnq2, mes2, state)
    assert any(r["ref_name"] == "day_low" for r in recs)
    assert not state["ny_evening_high|short|wick"]["armed"], "fixed level must NOT be re-armed by opposite SMT"

    # Bar 3: re-touch the highs → day_high fires again (dynamic re-armed); ny_evening_high
    # does NOT re-fire (fixed never re-arms).
    mnq3 = _bar(high=21001.0, low=20800.0, close=20900.0)
    mes3 = _bar(high=2999.0, low=2800.0, close=2900.0)
    recs, state = detect_regular_smts(lm, le, mnq3, mes3, state)
    assert any(r["ref_name"] == "day_high" for r in recs), \
        "opposite SMT re-armed dynamic day_high → re-touch fires"
    assert not any(r["ref_name"] == "ny_evening_high" for r in recs), \
        "fixed ny_evening_high must NOT re-fire"


def test_fixed_level_single_smt_ever():
    # A FIXED level (ny_evening_low, session) fires exactly once and NEVER again — not via an
    # opposite-direction SMT, not via a favorable (fulfilling) move + fresh re-touch.
    lm = _levels(ny_evening_low=21000.0, day_high=22000.0)
    le = _levels(ny_evening_low=3000.0, day_high=3200.0)
    state = {}
    # Bar 1: MNQ wicks below ny_evening_low (long fires), MES does not.
    recs, state = detect_regular_smts(
        lm, le, _bar(21010.0, 20999.0, 21005.0), _bar(3010.0, 3001.0, 3005.0), state)
    assert [r["ref_name"] for r in recs] == ["ny_evening_low"]
    assert recs[0]["direction"] == "long"

    # Bar 2: an opposite-direction SMT (day_high short) fires this batch.
    recs, state = detect_regular_smts(
        lm, le, _bar(22001.0, 21500.0, 21800.0), _bar(3199.0, 3100.0, 3150.0), state)
    assert any(r["ref_name"] == "day_high" for r in recs)
    assert not state["ny_evening_low|long|wick"]["armed"], "opposite SMT must NOT re-arm a fixed level"

    # Bar 3: price runs far below (would fulfill any tier) — fulfillment is informational only
    # for a fixed level and must NOT re-arm it.
    recs, state = detect_regular_smts(
        lm, le, _bar(20800.0, 20700.0, 20750.0), _bar(3199.0, 3100.0, 3150.0), state)
    assert state["ny_evening_low|long|wick"]["fulfilled"] is True
    assert not state["ny_evening_low|long|wick"]["armed"]

    # Bar 4: fresh re-touch of ny_evening_low → does NOT re-fire (fixed, single SMT ever).
    recs, state = detect_regular_smts(
        lm, le, _bar(21010.0, 20999.0, 21005.0), _bar(3199.0, 3100.0, 3150.0), state)
    assert not any(r["ref_name"] == "ny_evening_low" for r in recs), \
        "fixed level fires exactly once ever"


def test_dynamic_rearms_on_fulfillment():
    # A DYNAMIC level (day_low) fires, then a favorable move past the day fulfillment threshold
    # (40 pts) marks it fulfilled → re-arms → a fresh re-touch re-fires. No opposite SMT needed.
    lm = _levels(day_low=21000.0)
    le = _levels(day_low=3000.0)
    state = {}
    # Bar 1: MNQ wicks below day_low (long fires), MES not. fire_mnq_close = 21005 (the MNQ
    # close at the firing bar — the NEW fulfillment reference, NOT the swept level 21000).
    recs, state = detect_regular_smts(
        lm, le, _bar(21010.0, 20999.0, 21005.0), _bar(3010.0, 3001.0, 3005.0), state)
    assert [r["ref_name"] for r in recs] == ["day_low"]
    assert state["day_low|long|wick"]["fire_mnq_close"] == 21005.0
    assert state["day_low|long|wick"]["armed"] is False

    # Bar 2: favorable move for a long (price rises) past fire_close 21005 + 40 = 21045 →
    # fulfilled → re-arm in the same bar (no re-touch this bar so no fire).
    recs, state = detect_regular_smts(
        lm, le, _bar(21050.0, 21030.0, 21045.0), _bar(3050.0, 3030.0, 3045.0), state)
    assert recs == []
    assert state["day_low|long|wick"]["armed"] is True, "dynamic level re-arms after a fulfilling move"

    # Bar 3: fresh re-touch of day_low → re-fires.
    recs, state = detect_regular_smts(
        lm, le, _bar(21010.0, 20999.0, 21005.0), _bar(3010.0, 3001.0, 3005.0), state)
    assert any(r["ref_name"] == "day_low" for r in recs), \
        "dynamic level re-fires after fulfillment + fresh re-touch"

    # A sub-threshold favorable move alone would NOT have fulfilled: verify the threshold is
    # real by checking that a fire (fire_close 21005) followed by a < 40pt move from that close
    # stays dormant.
    state2 = {}
    recs, state2 = detect_regular_smts(
        lm, le, _bar(21010.0, 20999.0, 21005.0), _bar(3010.0, 3001.0, 3005.0), state2)
    assert len(recs) == 1
    recs, state2 = detect_regular_smts(
        lm, le, _bar(21035.0, 21025.0, 21030.0), _bar(3035.0, 3025.0, 3030.0), state2)  # 21030-21005=+25 < 40
    assert state2["day_low|long|wick"]["fulfilled"] is False
    assert state2["day_low|long|wick"]["armed"] is False
    recs, state2 = detect_regular_smts(
        lm, le, _bar(21010.0, 20999.0, 21005.0), _bar(3010.0, 3001.0, 3005.0), state2)
    assert not any(r["ref_name"] == "day_low" for r in recs), \
        "sub-threshold move does not fulfill → no re-fire"


def test_running_level_advance_does_not_rearm():
    # A running-level advance does NOT re-arm. An SMT against a (dynamic) level fires once and
    # stays dormant through subsequent level updates and sub-threshold price moves — it only
    # re-arms on fulfillment (a threshold move) or an opposite-direction SMT. This suppresses
    # the per-bar re-fire flood that ticking running levels would otherwise produce.
    le = _levels(day_high=3000.0)
    mnq_touch = _bar(high=21001.0, low=20990.0, close=20995.0)
    mes_miss = _bar(high=2999.0, low=2990.0, close=2995.0)
    state = {}
    recs, state = detect_regular_smts(_levels(day_high=21000.0), le, mnq_touch, mes_miss, state)
    assert len(recs) == 1

    # Same divergence, same bar → no re-fire (dormant).
    recs, state = detect_regular_smts(_levels(day_high=21000.0), le, mnq_touch, mes_miss, state)
    assert recs == []

    # day_high advances to a new value, leader keeps touching → STILL no re-fire (a mere
    # level advance must not re-arm).
    mnq_touch2 = _bar(high=21006.0, low=20995.0, close=21000.0)
    recs, state = detect_regular_smts(_levels(day_high=21005.0), le, mnq_touch2, mes_miss, state)
    assert recs == [], "running level advance must NOT re-arm"

    # A sub-threshold retreat (close 20965, fire_mnq_close 20995, day threshold 40 → not
    # fulfilled since 20965 > 20955) followed by a fresh touch → STILL dormant.
    away_med = _bar(high=20970.0, low=20962.0, close=20965.0)
    recs, state = detect_regular_smts(_levels(day_high=21005.0), le, away_med, mes_miss, state)
    assert recs == []
    recs, state = detect_regular_smts(_levels(day_high=21005.0), le, mnq_touch2, mes_miss, state)
    assert recs == [], "sub-threshold move alone must NOT re-arm a dormant level"


def test_dedup_near_coincident_levels_keeps_highest_scope():
    # Refinement #1: two same-side levels within DEDUP_TOL_PTS, both "touched" in one bar →
    # exactly ONE emitted level-SMT, the higher-scope one (week over day). Fills exempt.
    from session_pipeline import _dedup_level_smts, DEDUP_TOL_PTS

    recs = [
        {"kind": "smt", "side": "bearish", "type": "wick", "ref_name": "day_high"},
        {"kind": "smt", "side": "bearish", "type": "wick", "ref_name": "week_high"},
    ]
    # Level prices within tolerance (2 pts apart < 5.0).
    lvl_px = {"day_high": 21002.0, "week_high": 21000.0}
    assert abs(lvl_px["day_high"] - lvl_px["week_high"]) <= DEDUP_TOL_PTS
    out = _dedup_level_smts(recs, lvl_px)
    assert len(out) == 1
    assert out[0]["ref_name"] == "week_high", "week scope kept over day"

    # Far apart (> tol) → both kept (no merge).
    lvl_px_far = {"day_high": 21030.0, "week_high": 21000.0}
    out_far = _dedup_level_smts(recs, lvl_px_far)
    assert sorted(r["ref_name"] for r in out_far) == ["day_high", "week_high"]

    # Different sides are never merged even if prices coincide.
    recs_sides = [
        {"kind": "smt", "side": "bearish", "type": "wick", "ref_name": "day_high"},
        {"kind": "smt", "side": "bullish", "type": "wick", "ref_name": "week_low"},
    ]
    out_sides = _dedup_level_smts(recs_sides, {"day_high": 21000.0, "week_low": 21001.0})
    assert sorted(r["ref_name"] for r in out_sides) == ["day_high", "week_low"]

    # Fills are exempt — never deduped, always passed through.
    recs_fill = recs + [{"kind": "fill", "side": "bearish", "type": "fill_a", "ref_name": "fvg_x"}]
    out_fill = _dedup_level_smts(recs_fill, lvl_px)
    assert "fvg_x" in [r["ref_name"] for r in out_fill]
    assert sum(1 for r in out_fill if r["kind"] == "smt") == 1


def test_completed_session_only():
    # ny_morning closes at 12:00 ET; before noon it is not eligible.
    liqs = [{"name": "ny_morning_high", "kind": "level", "price": 21000.0}]
    before = pd.Timestamp("2026-06-09 11:00", tz="America/New_York")
    after = pd.Timestamp("2026-06-09 13:00", tz="America/New_York")
    assert "ny_morning_high" not in eligible_levels(liqs, before)
    assert "ny_morning_high" in eligible_levels(liqs, after)


def test_asia_not_eligible_while_forming():
    """asia forms 18:00–24:00 ET: its level is NOT eligible during 18:00–23:59, but IS
    eligible after midnight (00:00–18:00)."""
    liqs = [{"name": "asia_high", "kind": "level", "price": 21000.0}]
    forming = pd.Timestamp("2026-06-09 20:00", tz="America/New_York")
    closed = pd.Timestamp("2026-06-09 02:00", tz="America/New_York")
    assert "asia_high" not in eligible_levels(liqs, forming)
    assert "asia_high" in eligible_levels(liqs, closed)


def test_universe_prev_levels_always_eligible():
    """Universe (B) prev-day / prev-week fixed levels are eligible at any hour (completed
    history — no session-window or running-extreme gating) and pass close_price through."""
    liqs = [
        {"name": "prev1_day_high", "kind": "level", "price": 21000.0, "close_price": 20998.0},
        {"name": "prev7_day_low", "kind": "level", "price": 20500.0},
        {"name": "prev1_week_high", "kind": "level", "price": 21100.0},
        {"name": "prev2_week_low", "kind": "level", "price": 20400.0},
        {"name": "prev1_TDO", "kind": "level", "price": 20777.0},  # not a high/low → dropped
    ]
    for t in (pd.Timestamp("2026-06-09 20:00", tz="America/New_York"),   # Asia forming
              pd.Timestamp("2026-06-09 03:00", tz="America/New_York"),   # overnight
              pd.Timestamp("2026-06-09 10:00", tz="America/New_York")):  # RTH
        elig = eligible_levels(liqs, t)
        assert {"prev1_day_high", "prev7_day_low", "prev1_week_high", "prev2_week_low"} <= set(elig)
        assert "prev1_TDO" not in elig  # TDO has no _high/_low sub
    assert abs(elig["prev1_day_high"]["close_price"] - 20998.0) < 1e-9


def test_universe_prev_levels_are_fixed():
    """prev-day → ('fixed','day'); prev-week → ('fixed','week') (never re-arm)."""
    assert smt_detect._level_class("prev1_day_high") == ("fixed", "day")
    assert smt_detect._level_class("prev13_day_low") == ("fixed", "day")
    assert smt_detect._level_class("prev1_week_high") == ("fixed", "week")
    assert smt_detect._level_class("prev2_week_low") == ("fixed", "week")


def test_session_eligibility_day_scoped():
    """Day-scoped 6hr-session eligibility (smt_detect.eligible_levels docstring).

    - Asia time (19:15 ET, hour>=18): only YESTERDAY's ny_morning/ny_evening session
      levels are eligible; today's forming asia + the older london are excluded.
    - London time (02:15 ET, hour<18): only today's asia has closed (>=00:00); london
      (>=06:00), ny_morning (>=12:00), ny_evening (>=17:00) have not, so they are absent.
    - day_/week_ levels are always eligible regardless of clock.
    """
    liqs = [
        {"name": "asia_high", "kind": "level", "price": 21000.0},
        {"name": "london_high", "kind": "level", "price": 21010.0},
        {"name": "ny_morning_high", "kind": "level", "price": 21020.0},
        {"name": "ny_evening_low", "kind": "level", "price": 20990.0},
        {"name": "day_high", "kind": "level", "price": 21030.0},
        {"name": "week_low", "kind": "level", "price": 20980.0},
    ]

    # Asia time: only ny_morning/ny_evening among sessions; london/asia absent.
    asia_t = pd.Timestamp("2026-06-09 19:15", tz="America/New_York")
    elig_asia = eligible_levels(liqs, asia_t)
    assert "ny_morning_high" in elig_asia
    assert "ny_evening_low" in elig_asia
    assert "london_high" not in elig_asia
    assert "asia_high" not in elig_asia
    assert "day_high" in elig_asia and "week_low" in elig_asia

    # London time: only asia among sessions; ny_morning/ny_evening/london absent.
    london_t = pd.Timestamp("2026-06-09 02:15", tz="America/New_York")
    elig_london = eligible_levels(liqs, london_t)
    assert "asia_high" in elig_london
    assert "london_high" not in elig_london
    assert "ny_morning_high" not in elig_london
    assert "ny_evening_low" not in elig_london
    assert "day_high" in elig_london and "week_low" in elig_london


# ===========================================================================
# Hidden (body) SMT
# ===========================================================================
def test_hidden_close_vs_level_15m():
    lm = _levels(day_high=21000.0)
    le = _levels(day_high=3000.0)
    # MNQ CLOSE beyond level, MES close not.
    mnq = _bar(high=21010.0, low=20990.0, close=21005.0)
    mes = _bar(high=3010.0, low=2990.0, close=2995.0)
    recs, _ = detect_hidden_smts(lm, le, mnq, mes, "15m", {})
    assert len(recs) == 1
    assert recs[0]["type"] == "body" and recs[0]["timeframe"] == "15m"
    assert recs[0]["direction"] == "short"


def test_hidden_distinct_from_wick():
    lm = _levels(day_high=21000.0)
    le = _levels(day_high=3000.0)
    # Wick state from a wick fire.
    wstate = {}
    mnq_w = _bar(high=21001.0, low=20990.0, close=20995.0)
    mes_w = _bar(high=2999.0, low=2990.0, close=2995.0)
    recs, wstate = detect_regular_smts(lm, le, mnq_w, mes_w, wstate)
    assert len(recs) == 1
    # A separate hidden (body) detection with its OWN state still fires.
    mnq_b = _bar(high=21010.0, low=20990.0, close=21005.0)
    mes_b = _bar(high=3010.0, low=2990.0, close=2995.0)
    recs2, _ = detect_hidden_smts(lm, le, mnq_b, mes_b, "15m", {})
    assert len(recs2) == 1 and recs2[0]["type"] == "body"


def test_wick_and_body_both_fire_shared_state():
    # A wick SMT and a body SMT on the SAME (level, direction) both fire when sharing one
    # state dict, because the per-(level,direction) state key now includes rec_type. This
    # yields two independent state entries: "day_high|short|wick" and "day_high|short|body".
    lm = _levels(day_high=21000.0)
    le = _levels(day_high=3000.0)
    state = {}
    # Bar that satisfies BOTH: MNQ wick AND close beyond day_high, MES neither.
    mnq = _bar(high=21010.0, low=20990.0, close=21005.0)
    mes = _bar(high=2999.0, low=2990.0, close=2995.0)
    recs_w, state = detect_regular_smts(lm, le, mnq, mes, state)
    assert len(recs_w) == 1 and recs_w[0]["type"] == "wick"
    recs_b, state = detect_hidden_smts(lm, le, mnq, mes, "1m", state)
    assert len(recs_b) == 1 and recs_b[0]["type"] == "body", \
        "body SMT fires independently despite the wick SMT on the same level (shared state)"
    assert recs_w[0]["direction"] == "short" and recs_b[0]["direction"] == "short"
    # Two independent state keys exist, one per rec_type.
    assert "day_high|short|wick" in state
    assert "day_high|short|body" in state
    assert state["day_high|short|wick"]["fired"] is True
    assert state["day_high|short|body"]["fired"] is True


def test_body_smt_uses_close_price_extreme():
    # The 2026-06-03 MES day-low case: wick day-low (price) = 7604.0 (lowest LOW),
    # body day-low (close_price) = 7604.0... here generalized: the body extreme
    # (close_price) is ABOVE the wick price, so a close that crosses close_price but
    # whose LOW has NOT yet reached the wick price must fire a BODY SMT.
    # MES day_low: wick price 7602.50 (lowest low), body close_price 7604.00 (lowest close).
    lm = {"day_low": {"name": "day_low", "kind": "level", "price": 30575.75,
                      "close_price": 30575.75, "sub": "low"}}
    le = {"day_low": {"name": "day_low", "kind": "level", "price": 7602.50,
                      "close_price": 7604.00, "sub": "low"}}
    # MES close 7603.25 is BELOW the body extreme (7604.00) but its low (7603.00) is
    # still ABOVE the wick price (7602.50) → wick would NOT fire, body MUST fire.
    mes = _bar(high=7606.0, low=7603.0, close=7603.25)
    # MNQ close (30668) far above its body day-low (30575.75) → MNQ does not touch.
    mnq = _bar(high=30680.0, low=30660.0, close=30668.0)

    # Body (hidden) SMT: MES leads (its close crossed its body extreme), MNQ did not.
    recs_b, _ = detect_hidden_smts(lm, le, mnq, mes, "1m", {})
    assert len(recs_b) == 1
    assert recs_b[0]["type"] == "body" and recs_b[0]["direction"] == "long"
    assert recs_b[0]["leader"] == "mes" and recs_b[0]["ref_name"] == "day_low"
    # The record carries the MES body extreme as the comparison level.
    assert recs_b[0]["mes_lvl_price"] == 7604.00
    assert recs_b[0]["mnq_lvl_price"] == 30575.75

    # Wick SMT on the SAME bar: compares LOW to the wick price (7602.50). MES low 7603.00
    # > 7602.50 → no touch → no wick SMT.
    recs_w, _ = detect_regular_smts(lm, le, mnq, mes, {})
    assert recs_w == [], "wick SMT must NOT fire: low has not reached the wick price yet"


def test_body_smt_falls_back_to_price_without_close_price():
    # A body level missing close_price falls back to the wick price for comparison.
    lm = _levels(day_high=21000.0)   # no close_price key
    le = _levels(day_high=3000.0)
    mnq = _bar(high=21010.0, low=20990.0, close=21005.0)  # close beyond wick price
    mes = _bar(high=3010.0, low=2990.0, close=2995.0)
    recs, _ = detect_hidden_smts(lm, le, mnq, mes, "1m", {})
    assert len(recs) == 1 and recs[0]["type"] == "body"
    assert recs[0]["mnq_lvl_price"] == 21000.0  # fell back to wick price


def test_body_high_uses_highest_close():
    # *_high body SMT references the HIGHEST close. Body extreme (close_price) is BELOW
    # the wick price, so a close above close_price (but high below wick price) fires body.
    lm = {"day_high": {"name": "day_high", "kind": "level", "price": 21010.0,
                       "close_price": 21000.0, "sub": "high"}}
    le = {"day_high": {"name": "day_high", "kind": "level", "price": 3010.0,
                       "close_price": 3000.0, "sub": "high"}}
    # MNQ close 21002 > body extreme 21000 but high 21005 < wick price 21010 → body fires,
    # wick does not. MES close 2995 < its body extreme 3000 → MES does not touch.
    mnq = _bar(high=21005.0, low=20990.0, close=21002.0)
    mes = _bar(high=2998.0, low=2985.0, close=2995.0)
    recs_b, _ = detect_hidden_smts(lm, le, mnq, mes, "1m", {})
    assert len(recs_b) == 1 and recs_b[0]["direction"] == "short"
    assert recs_b[0]["mnq_lvl_price"] == 21000.0
    recs_w, _ = detect_regular_smts(lm, le, mnq, mes, {})
    assert recs_w == [], "wick high not reached (21005 < 21010)"


def test_hidden_not_on_1m_tags_30m():
    lm = _levels(day_low=21000.0)
    le = _levels(day_low=3000.0)
    mnq = _bar(high=21010.0, low=20990.0, close=20995.0)   # close below day_low
    mes = _bar(high=3010.0, low=2990.0, close=3005.0)
    recs, _ = detect_hidden_smts(lm, le, mnq, mes, "30m", {})
    assert len(recs) == 1 and recs[0]["timeframe"] == "30m"
    assert recs[0]["direction"] == "long"


# ===========================================================================
# SMT-fill
# ===========================================================================
def _pair(name, side, mnq_top, mnq_bot, mes_top, mes_bot):
    return {
        "name": name, "side": side,
        "mnq": {"top": float(mnq_top), "bottom": float(mnq_bot)},
        "mes": {"top": float(mes_top), "bottom": float(mes_bot)},
    }


def test_fill_pairing_one_sided_no_fire():
    # paired_fvgs empty (the pipeline only passes intersected pairs) → no fill.
    recs, _ = detect_fill_smts([], _bar(21010, 20990), _bar(3010, 2990), {})
    assert recs == []


def test_fill_a():
    p = _pair("fvg_20260609_1000_bull", "bull", 21010, 21000, 3010, 3000)
    # Bull FVG = filled by a retrace DOWN. MNQ low dips into its zone [21000,21010];
    # MES stays ABOVE its zone (low > top) so it has NOT entered.
    mnq = _bar(high=21015.0, low=21005.0, close=21008.0)
    mes = _bar(high=3060.0, low=3050.0, close=3055.0)
    recs, _ = detect_fill_smts([p], mnq, mes, {})
    assert len(recs) == 1
    assert recs[0]["type"] == "fill_a" and recs[0]["leader"] == "mnq"
    assert recs[0]["direction"] == "long"


def test_fill_b():
    p = _pair("fvg_20260609_1000_bull", "bull", 21010, 21000, 3010, 3000)
    # Bull FVG, retrace DOWN. Both entered; MNQ passes the far edge (low <= bottom),
    # MES still inside (bottom < low <= top).
    mnq = _bar(high=21010.0, low=20999.0, close=21001.0)   # passed (low <= bottom)
    mes = _bar(high=3010.0, low=3005.0, close=3007.0)      # entered, not passed
    recs, _ = detect_fill_smts([p], mnq, mes, {})
    types = [r["type"] for r in recs]
    assert "fill_b" in types
    fb = next(r for r in recs if r["type"] == "fill_b")
    assert fb["leader"] == "mnq"


def test_fill_b_follow_on():
    p = _pair("fvg_20260609_1000_bull", "bull", 21010, 21000, 3010, 3000)
    state = {}
    # Bull FVG, retrace DOWN.
    # Step 1: MNQ low dips into zone (entered), MES still above → Fill-A.
    recs, state = detect_fill_smts(
        [p], _bar(21015, 21005, 21008), _bar(3060, 3050, 3055), state)
    assert [r["type"] for r in recs] == ["fill_a"]
    # Step 2: MES enters too (both inside, neither passed) → no fire.
    recs, state = detect_fill_smts(
        [p], _bar(21012, 21005, 21008), _bar(3012, 3005, 3007), state)
    assert recs == []
    # Step 3: MNQ passes far edge (low <= bottom), MES still inside → Fill-B without re-arm.
    recs, state = detect_fill_smts(
        [p], _bar(21008, 20999, 21001), _bar(3010, 3006, 3008), state)
    assert [r["type"] for r in recs] == ["fill_b"]


def test_fill_independent_b():
    p = _pair("fvg_20260609_1000_bull", "bull", 21010, 21000, 3010, 3000)
    # Bull FVG, retrace DOWN. Both enter on the same bar (no prior A); MNQ passes far
    # edge (low <= bottom), MES entered but inside (bottom < low <= top).
    mnq = _bar(high=21010.0, low=20999.0, close=21001.0)
    mes = _bar(high=3010.0, low=3005.0, close=3007.0)
    recs, _ = detect_fill_smts([p], mnq, mes, {})
    assert any(r["type"] == "fill_b" for r in recs)


def test_fill_entered_vs_passed_boundary():
    p = _pair("fvg_20260609_1000_bull", "bull", 21010, 21000, 3010, 3000)
    # Bull FVG, retrace DOWN. Near edge = top (21010), far edge = bottom (21000).
    # Wick LOW exactly at the near edge (top=21010) = entered (inclusive), not passed.
    entered_bar = _bar(high=21020.0, low=21010.0, close=21015.0)
    e, passed = smt_detect._fvg_progress(entered_bar, p["mnq"], "bull")
    assert e is True and passed is False
    # Wick LOW exactly at the far edge (bottom=21000) = passed (inclusive).
    passed_bar = _bar(high=21008.0, low=21000.0, close=21004.0)
    e2, passed2 = smt_detect._fvg_progress(passed_bar, p["mnq"], "bull")
    assert e2 is True and passed2 is True


def test_fill_no_double_enter_while_inside():
    # fill_a fires once, then the fill_a_fired latch + armed=False suppress re-fires while
    # the leader stays inside (no opposite-direction SMT to re-arm).
    p = _pair("fvg_20260609_1000_bull", "bull", 21010, 21000, 3010, 3000)
    state = {}
    # Bull FVG, retrace DOWN. fill_a fires (MNQ low enters zone, MES stays above).
    recs, state = detect_fill_smts(
        [p], _bar(21015, 21005, 21008), _bar(3060, 3050, 3055), state)
    assert [r["type"] for r in recs] == ["fill_a"]
    # MNQ STILL inside (no exit, no opposite SMT) → latched → no re-fire.
    recs, state = detect_fill_smts(
        [p], _bar(21008, 21002, 21005), _bar(3060, 3050, 3055), state)
    assert recs == []


def test_fill_rearm_via_opposite_smt():
    # After fill_a fires (armed=False), an opposite-direction record in a later batch
    # re-arms the fill so a fresh divergent approach re-fires fill_a.
    p = _pair("fvg_20260609_1000_bull", "bull", 21010, 21000, 3010, 3000)
    state = {}
    # fill_a fires (MNQ enters, MES above).
    recs, state = detect_fill_smts(
        [p], _bar(21015, 21005, 21008), _bar(3060, 3050, 3055), state)
    assert [r["type"] for r in recs] == ["fill_a"]
    assert state["fvg_20260609_1000_bull"]["armed"] is False
    # Inject an opposite-direction (short) bear FVG that fires the same batch, re-arming
    # the bull fill. The bear FVG: MNQ rallies UP into its zone, MES stays below.
    q = _pair("fvg_20260609_1000_bear", "bear", 21110, 21100, 3110, 3100)
    recs, state = detect_fill_smts(
        [p, q], _bar(21115, 21105, 21108), _bar(3060, 3050, 3055), state)
    # The bear fill_a fired (short) → re-armed the bull fill.
    assert any(r["direction"] == "short" for r in recs)
    assert state["fvg_20260609_1000_bull"]["armed"] is True
    # Fresh divergent bull approach now re-fires fill_a.
    recs, state = detect_fill_smts(
        [p], _bar(21015, 21005, 21008), _bar(3060, 3050, 3055), state)
    assert [r["type"] for r in recs] == ["fill_a"]


def test_fill_state_json_serializable():
    import json
    p = _pair("fvg_20260609_1000_bull", "bull", 21010, 21000, 3010, 3000)
    _, state = detect_fill_smts(
        [p], _bar(21015, 21005, 21008), _bar(3060, 3050, 3055), {})
    # State must round-trip through JSON (live restart continuity).
    assert json.loads(json.dumps(state)) == state


# ===========================================================================
# SmtBuffer
# ===========================================================================
def test_buffer_per_minute_overwrite():
    buf = SmtBuffer()
    a = [{"id": "A", "direction": "long"}]
    b = [{"id": "B", "direction": "short"}]
    buf.add(a, pd.Timestamp("2026-06-09 10:00", tz="America/New_York"))
    buf.add(b, pd.Timestamp("2026-06-09 10:01", tz="America/New_York"))
    assert buf.get_new("1m") == b


def test_buffer_5m_accumulates():
    buf = SmtBuffer()
    base = pd.Timestamp("2026-06-09 10:00", tz="America/New_York")
    for i in range(3):
        buf.add([{"id": i, "direction": "long"}], base + pd.Timedelta(minutes=i))
    accum = buf.get_new("5m")
    assert [r["id"] for r in accum] == [0, 1, 2]


def test_buffer_drain_at_boundary():
    buf = SmtBuffer()
    t1 = pd.Timestamp("2026-06-09 10:01", tz="America/New_York")
    buf.add([{"id": 1, "direction": "long"}], t1)
    buf.drain_if_boundary(t1)   # first call sets marker to 10:00 floor, clears
    buf.add([{"id": 2, "direction": "long"}], t1)
    # Same 5m floor → no further drain.
    buf.drain_if_boundary(pd.Timestamp("2026-06-09 10:02", tz="America/New_York"))
    assert [r["id"] for r in buf.get_new("5m")] == [2]
    assert buf.get_new("1m") == [{"id": 2, "direction": "long"}]
    # Cross into the next 5m block → accum cleared.
    t2 = pd.Timestamp("2026-06-09 10:05", tz="America/New_York")
    buf.drain_if_boundary(t2)
    assert buf.get_new("5m") == []
    # Per-minute untouched by drain.
    assert buf.get_new("1m") == [{"id": 2, "direction": "long"}]


# ===========================================================================
# PendingSmtWatch
# ===========================================================================
def test_watch_preserve_through_drain():
    buf = SmtBuffer()
    watch = PendingSmtWatch()
    rec = {"kind": "smt", "direction": "short", "mnq_price": 21000.0, "mes_price": 3000.0}
    buf.add([rec], pd.Timestamp("2026-06-09 10:01", tz="America/New_York"))
    watch.ingest(buf.get_new("1m"))
    buf.drain_if_boundary(pd.Timestamp("2026-06-09 10:05", tz="America/New_York"))
    assert buf.get_new("5m") == []
    assert len(watch.retained()) == 1
    # Mutating the buffer source must not touch the retained (copy-detached) record.
    rec["mnq_price"] = -1.0
    assert watch.retained()[0]["mnq_price"] == 21000.0


def test_watch_invalidate_on_trend():
    watch = PendingSmtWatch()
    watch.ingest([{"direction": "short", "mnq_price": 21000.0, "mes_price": 3000.0}])
    # Price moves DOWN >= WATCH_CONFIRM_PTS in the short direction → expected move → dropped.
    watch.update(None, 21000.0 - WATCH_CONFIRM_PTS_MNQ, 3000.0)
    assert watch.retained() == []


def test_watch_invalidate_on_contradiction():
    watch = PendingSmtWatch()
    watch.ingest([{"direction": "short", "mnq_price": 21000.0, "mes_price": 3000.0}])
    watch.ingest([{"direction": "long", "mnq_price": 21000.0, "mes_price": 3000.0}])
    # Opposite-direction SMTs contradict each other → both dropped.
    watch.update(None, 21000.0, 3000.0)
    assert watch.retained() == []


def test_watch_roundtrip():
    watch = PendingSmtWatch()
    watch.ingest([{"direction": "short", "mnq_price": 21000.0, "mes_price": 3000.0}])
    d = watch.to_dict()
    watch2 = PendingSmtWatch.from_dict(d)
    assert watch2.retained() == watch.retained()


# ===========================================================================
# Edge cases / totality
# ===========================================================================
def test_empty_frames_no_crash():
    recs, st = detect_regular_smts({}, {}, {}, {}, {})
    assert recs == [] and st == {}
    recs, st = detect_fill_smts([], {}, {}, {})
    assert recs == [] and st == {}
    recs, st = detect_hidden_smts({}, {}, {}, {}, "15m", {})
    assert recs == [] and st == {}


def test_level_one_sided_no_fire():
    # Level present only in MNQ → no pair → no fire.
    lm = _levels(day_high=21000.0)
    le = {}
    mnq = _bar(high=21001.0, low=20990.0)
    mes = _bar(high=2999.0, low=2990.0)
    recs, _ = detect_regular_smts(lm, le, mnq, mes, {})
    assert recs == []
