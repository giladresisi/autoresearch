# tests/test_smt_relevance_rules.py
# SMT V2 Part B — consumer relevance rules (SHADOW-only). Unit tests for the pure
# primitives: smt_status (Contract C-STATUS), Rule A (C-RULEA), Rule B (C-RULEB, gated),
# and the leg-scoped sweep/reclaim suppressor (C-LEG), plus the consumer-trail event
# schema. Written against the interface contracts (no pipeline wiring needed).

from __future__ import annotations

import pandas as pd
import pytest

import smt_state
import hypothesis as hyp
from session_pipeline import SessionPipeline
from hypothesis import (
    apply_rule_a,
    apply_rule_b,
    update_leg,
    suppress_counter_trend,
    ingest_smts,
    to_record,
    RULE_B_ENABLED,
    RULE_B_MIN_AGE_MIN,
    RULE_B_ADVERSE_PTS,
    RULE_B_TIER_SLACK,
    RECLAIM_MARGIN_PTS,
)
from smt_detect import smt_status, fulfillment_status


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _rec(**overrides) -> dict:
    base = {
        "kind": "smt",
        "type": "wick",
        "side": "bearish",
        "direction": "short",
        "timeframe": "1m",
        "time": "2026-06-03T09:49:00",
        "leader": "mnq",
        "ref_name": "prev1_week_high",
        "tier": "week",
        "key": "prev1_week_high|short|wick",
        "fulfilled": False,
        "invalidated": False,
        "mnq_price": 30496.0,
        "mes_price": 4500.0,
        "mnq_lvl_price": 30807.0,
    }
    base.update(overrides)
    if "key" not in overrides and base["kind"] == "smt":
        base["key"] = f"{base['ref_name']}|{base['direction']}|{base['type']}"
    base.setdefault("keys", [base["key"]])
    return base


# ===========================================================================
# Contract C-STATUS — smt_status
# ===========================================================================
def test_smt_status_invalidated():
    keys = ["k1", "k2", "k3", "k4"]
    ds = {
        "k1": {"fulfilled": False, "invalidated": True},   # invalidated
        "k2": {"fulfilled": False},                         # unfulfilled (flag absent)
        "k3": {"fulfilled": False, "invalidated": False},  # unfulfilled
        # k4 absent → gone
    }
    out = smt_status(keys, ds)
    assert out["k1"] == "invalidated"
    assert out["k2"] == "unfulfilled"   # absent flag → unfulfilled (forward-compatible)
    assert out["k3"] == "unfulfilled"
    assert out["k4"] == "gone"
    # fulfillment_status wrapper collapses invalidated → unfulfilled (legacy vocab).
    legacy = fulfillment_status(keys, ds)
    assert legacy["k1"] == "unfulfilled"
    assert legacy["k4"] == "gone"


def test_status_fulfilled_precedence():
    # fulfilled is checked BEFORE invalidated → fulfilled wins even if both flags set.
    ds = {"k": {"fulfilled": True, "invalidated": True}}
    assert smt_status(["k"], ds)["k"] == "fulfilled"


# ===========================================================================
# ingest_smts — invalidated drop
# ===========================================================================
def test_ingest_drops_invalidated():
    good = _rec(key="prev1_week_high|short|wick")
    bad = _rec(ref_name="day_high", direction="short", key="day_high|short|wick",
               tier="day", invalidated=True)
    out = ingest_smts([good, bad], [], flat=True, cautious_targets=None,
                      backing_tier=None, x_pts=25.0)
    keys = {r["key"] for r in out}
    assert "prev1_week_high|short|wick" in keys
    assert "day_high|short|wick" not in keys  # invalidated dropped
    # to_record carries invalidated through.
    r = to_record({"kind": "smt", "ref_name": "x", "direction": "short",
                   "type": "wick", "invalidated": True})
    assert r["invalidated"] is True


# ===========================================================================
# Rule A — same-level latest-take-out-wins (C-RULEA)
# ===========================================================================
def test_rule_a_newer_opposite_supersedes_same_level():
    older = _rec(ref_name="prev1_week_high", direction="short",
                 key="prev1_week_high|short|wick", time="2026-06-03T09:49:00")
    newer = _rec(ref_name="prev1_week_high", direction="long", side="bullish",
                 key="prev1_week_high|long|wick", time="2026-06-03T09:55:00")
    kept, events = apply_rule_a([older, newer])
    keys = {r["key"] for r in kept}
    assert "prev1_week_high|long|wick" in keys    # newer kept
    assert "prev1_week_high|short|wick" not in keys  # older opposite dropped
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "superseded_same_level"
    assert ev["kept_direction"] == "long"
    assert ev["dropped_direction"] == "short"


def test_rule_a_noop_same_direction():
    # Two same-direction records on the same ref_name → untouched (no supersession).
    a = _rec(ref_name="day_high", direction="short", key="day_high|short|wick",
             tier="day", time="2026-06-03T09:49:00")
    b = _rec(ref_name="day_high", direction="short", key="day_high|short|body",
             type="body", tier="day", time="2026-06-03T09:55:00")
    kept, events = apply_rule_a([a, b])
    assert len(kept) == 2
    assert events == []
    # Records on DIFFERENT ref_names → untouched even if opposite directions.
    c = _rec(ref_name="day_low", direction="long", side="bullish",
             key="day_low|long|wick", tier="day")
    kept2, events2 = apply_rule_a([a, c])
    assert len(kept2) == 2 and events2 == []


# ===========================================================================
# Rule B — recency-trend cross-tier suppression (C-RULEB, gated)
# ===========================================================================
def test_rule_b_suppresses_when_all_gates_met():
    # Older SHORT at week tier; newer opposite LONG (day tier, within slack); price has
    # moved adverse (above) the short's level by >= adverse_pts.
    older = _rec(ref_name="prev1_week_high", direction="short", tier="week",
                 key="prev1_week_high|short|wick", time="2026-06-03T09:49:00",
                 mnq_lvl_price=30807.0)
    newer = _rec(ref_name="day_low", direction="long", side="bullish", tier="day",
                 key="day_low|long|wick", time="2026-06-03T10:10:00",
                 mnq_lvl_price=30500.0)
    now_close = 30807.0 + RULE_B_ADVERSE_PTS  # above the short level by adverse_pts
    kept, events = apply_rule_b(
        [older, newer], now_close=now_close, enabled=True,
        min_age_min=RULE_B_MIN_AGE_MIN, adverse_pts=RULE_B_ADVERSE_PTS,
        tier_slack=RULE_B_TIER_SLACK)
    keys = {r["key"] for r in kept}
    assert "prev1_week_high|short|wick" not in keys  # older suppressed
    assert "day_low|long|wick" in keys
    assert len(events) == 1 and events[0]["event"] == "suppressed_by_trend"


def test_rule_b_noop_when_disabled():
    older = _rec(ref_name="prev1_week_high", direction="short", tier="week",
                 key="prev1_week_high|short|wick", time="2026-06-03T09:49:00",
                 mnq_lvl_price=30807.0)
    newer = _rec(ref_name="day_low", direction="long", side="bullish", tier="day",
                 key="day_low|long|wick", time="2026-06-03T10:10:00")
    now_close = 30807.0 + 200.0
    kept, events = apply_rule_b(
        [older, newer], now_close=now_close, enabled=False,
        min_age_min=RULE_B_MIN_AGE_MIN, adverse_pts=RULE_B_ADVERSE_PTS,
        tier_slack=RULE_B_TIER_SLACK)
    assert len(kept) == 2 and events == []
    # The shipped default constant is OFF.
    assert RULE_B_ENABLED is False


def test_rule_b_respects_min_age_and_adverse_and_tier_slack():
    older = _rec(ref_name="prev1_week_high", direction="short", tier="week",
                 key="prev1_week_high|short|wick", time="2026-06-03T09:49:00",
                 mnq_lvl_price=30807.0)

    def _run(newer, now_close, tier_slack=RULE_B_TIER_SLACK):
        return apply_rule_b([older, newer], now_close=now_close, enabled=True,
                            min_age_min=RULE_B_MIN_AGE_MIN, adverse_pts=RULE_B_ADVERSE_PTS,
                            tier_slack=tier_slack)

    now_close = 30807.0 + RULE_B_ADVERSE_PTS
    # (a) min-age NOT met → no suppression.
    too_young = _rec(ref_name="day_low", direction="long", side="bullish", tier="day",
                     key="day_low|long|wick", time="2026-06-03T09:50:00")  # 1 min later
    kept, events = _run(too_young, now_close)
    assert len(kept) == 2 and events == []

    # (b) adverse-move NOT met (price not far enough above) → no suppression.
    old_enough = _rec(ref_name="day_low", direction="long", side="bullish", tier="day",
                      key="day_low|long|wick", time="2026-06-03T10:10:00")
    kept, events = _run(old_enough, 30807.0 + (RULE_B_ADVERSE_PTS - 1.0))
    assert len(kept) == 2 and events == []

    # (c) tier-slack: newer is SESSION (rank 1), older is WEEK (rank 4); with slack 1 the
    # gate (1 >= 4-1=3) fails → no suppression. Bumping slack to 3 lets it pass.
    low_tier = _rec(ref_name="ny_morning_low", direction="long", side="bullish",
                    tier="session", key="ny_morning_low|long|wick",
                    time="2026-06-03T10:10:00")
    kept, events = _run(low_tier, now_close, tier_slack=1)
    assert len(kept) == 2 and events == []
    kept, events = _run(low_tier, now_close, tier_slack=3)
    assert any(e["event"] == "suppressed_by_trend" for e in events)


# ===========================================================================
# Leg-scope — sweep/reclaim detect + suppress (C-LEG)
# ===========================================================================
def test_leg_detect_sweep_then_reclaim():
    # prev1_week_high at 30807; price breaches ABOVE by margin, then closes back below →
    # a swept-and-reclaimed high → recovery_dir "short"? NO: a HIGH breached up then
    # reclaimed down means price rejected the high → recovery DOWN (short). But the
    # June-3 labeled case is the OPPOSITE: a high swept (price ABOVE), then a V-reversal
    # bottom and recovery UP. We exercise the low-breach (recovery long) path here, and
    # the labeled-case geometry in the suppression test below.
    fixed = [{"name": "prev1_week_low", "kind": "level", "price": 30000.0}]
    st = {}
    # Breach below by margin.
    st = update_leg(st, fixed_levels=fixed, now_close=30000.0 - RECLAIM_MARGIN_PTS,
                    now_time="2026-06-03T09:40:00")
    assert "leg" not in st  # breach alone is not a leg yet
    # Reclaim: close back above the low.
    st = update_leg(st, fixed_levels=fixed, now_close=30010.0,
                    now_time="2026-06-03T09:50:00")
    leg = st.get("leg")
    assert leg is not None
    assert leg["level_name"] == "prev1_week_low"
    assert leg["recovery_dir"] == "long"
    assert leg["origin_price"] == 30000.0
    assert leg["reclaim_time"] == "2026-06-03T09:50:00"
    # Dynamic selection: a hardcoded prev_day_low must NOT be assumed — here the level is
    # prev1_week_low, proving the detector keys off the data.
    assert leg["level_name"] != "prev_day_low"


def test_leg_suppresses_older_counter_trend_until_origin():
    # Leg: prev1_week_low swept-down then reclaimed-up at 09:50 → recovery_dir long.
    fixed = [{"name": "prev1_week_low", "kind": "level", "price": 30000.0}]
    st = update_leg({}, fixed_levels=fixed, now_close=30000.0 - RECLAIM_MARGIN_PTS,
                    now_time="2026-06-03T09:40:00")
    st = update_leg(st, fixed_levels=fixed, now_close=30010.0,
                    now_time="2026-06-03T09:50:00")

    older_short = _rec(ref_name="prev1_week_high", direction="short", tier="week",
                       key="prev1_week_high|short|wick", time="2026-06-03T09:45:00")
    newer_long = _rec(ref_name="day_low", direction="long", side="bullish", tier="day",
                      key="day_low|long|wick", time="2026-06-03T09:55:00")
    active = [older_short, newer_long]

    # Price above origin (30010 > 30000) → leg active → older counter-trend short dropped.
    kept, events, st2 = suppress_counter_trend(active, st, now_close=30010.0)
    keys = {r["key"] for r in kept}
    assert "prev1_week_high|short|wick" not in keys   # counter-trend, predates reclaim
    assert "day_low|long|wick" in keys                # aligned, kept
    assert len(events) == 1 and events[0]["event"] == "suppressed_by_leg"
    assert "leg" in st2  # leg still active

    # Price RETURNS to origin (<= 30000) → leg clears, nothing suppressed.
    kept2, events2, st3 = suppress_counter_trend(active, st, now_close=29999.0)
    assert len(kept2) == 2 and events2 == []
    assert "leg" not in st3  # leg cleared


def test_leg_does_not_suppress_post_reclaim_or_aligned():
    fixed = [{"name": "prev1_week_low", "kind": "level", "price": 30000.0}]
    st = update_leg({}, fixed_levels=fixed, now_close=30000.0 - RECLAIM_MARGIN_PTS,
                    now_time="2026-06-03T09:40:00")
    st = update_leg(st, fixed_levels=fixed, now_close=30010.0,
                    now_time="2026-06-03T09:50:00")

    # (a) aligned (long) record → not suppressed even though it predates the reclaim.
    aligned = _rec(ref_name="day_low", direction="long", side="bullish", tier="day",
                   key="day_low|long|wick", time="2026-06-03T09:45:00")
    # (b) counter-trend short that POSTDATES the reclaim → not suppressed.
    post_short = _rec(ref_name="day_high", direction="short", tier="day",
                      key="day_high|short|wick", time="2026-06-03T09:55:00")
    kept, events, _ = suppress_counter_trend([aligned, post_short], st, now_close=30010.0)
    assert len(kept) == 2  # neither suppressed
    assert events == []


# ===========================================================================
# Consumer-trail event schema
# ===========================================================================
def test_consumer_trail_event_schema():
    # Rule A event.
    older = _rec(ref_name="prev1_week_high", direction="short",
                 key="prev1_week_high|short|wick", time="2026-06-03T09:49:00")
    newer = _rec(ref_name="prev1_week_high", direction="long", side="bullish",
                 key="prev1_week_high|long|wick", time="2026-06-03T09:55:00")
    _, a_events = apply_rule_a([older, newer])
    assert a_events and set(a_events[0]) >= {
        "event", "ref_name", "kept_key", "kept_direction", "dropped_key",
        "dropped_direction", "kept_time", "dropped_time"}
    assert a_events[0]["event"] == "superseded_same_level"

    # Rule B event.
    b_older = _rec(ref_name="prev1_week_high", direction="short", tier="week",
                   key="prev1_week_high|short|wick", time="2026-06-03T09:49:00",
                   mnq_lvl_price=30807.0)
    b_newer = _rec(ref_name="day_low", direction="long", side="bullish", tier="day",
                   key="day_low|long|wick", time="2026-06-03T10:10:00")
    _, b_events = apply_rule_b(
        [b_older, b_newer], now_close=30807.0 + RULE_B_ADVERSE_PTS, enabled=True,
        min_age_min=RULE_B_MIN_AGE_MIN, adverse_pts=RULE_B_ADVERSE_PTS,
        tier_slack=RULE_B_TIER_SLACK)
    assert b_events and set(b_events[0]) >= {
        "event", "ref_name", "dropped_key", "dropped_direction", "by_key",
        "by_direction"}
    assert b_events[0]["event"] == "suppressed_by_trend"

    # Leg event.
    fixed = [{"name": "prev1_week_low", "kind": "level", "price": 30000.0}]
    st = update_leg({}, fixed_levels=fixed, now_close=30000.0 - RECLAIM_MARGIN_PTS,
                    now_time="2026-06-03T09:40:00")
    st = update_leg(st, fixed_levels=fixed, now_close=30010.0,
                    now_time="2026-06-03T09:50:00")
    older_short = _rec(ref_name="prev1_week_high", direction="short", tier="week",
                       key="prev1_week_high|short|wick", time="2026-06-03T09:45:00")
    _, leg_events, _ = suppress_counter_trend([older_short], st, now_close=30010.0)
    assert leg_events and set(leg_events[0]) >= {
        "event", "ref_name", "dropped_key", "dropped_direction", "leg_level_name",
        "leg_recovery_dir"}
    assert leg_events[0]["event"] == "suppressed_by_leg"


# ===========================================================================
# Wave 2 — shadow parity / inertness (Task 2.2)
# ===========================================================================
@pytest.fixture()
def _isolate_state(tmp_path, monkeypatch):
    import paths
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    monkeypatch.setattr(smt_state, "_IN_MEMORY", False)
    monkeypatch.setattr(smt_state, "_hyp_cache", None)
    monkeypatch.setattr(smt_state, "_hyp_cache_valid", False)


def _hist():
    idx = pd.date_range("2026-06-09 09:00:00", periods=30, freq="1min",
                        tz="America/New_York")
    mnq = pd.DataFrame({
        "Open": [21000.0] * 30, "High": [21010.0] * 30,
        "Low": [20990.0] * 30, "Close": [21000.0] * 30, "Volume": [100] * 30,
    }, index=idx)
    mes = pd.DataFrame({
        "Open": [3000.0] * 30, "High": [3010.0] * 30,
        "Low": [2990.0] * 30, "Close": [3000.0] * 30, "Volume": [100] * 30,
    }, index=idx)
    return mnq, mes


def _pre_daily():
    return {
        "liquidities": [
            {"name": "day_high", "kind": "level", "price": 21000.0, "sub": "high"},
        ],
        "liquidities_mes": [
            {"name": "day_high", "kind": "level", "price": 3000.0, "sub": "high"},
        ],
    }


def _fire_bar():
    now = pd.Timestamp("2026-06-09 10:00:00", tz="America/New_York")
    mnq_row = pd.Series({"Open": 21000.0, "High": 21001.0, "Low": 20990.0,
                         "Close": 20995.0})  # MNQ touches 21000
    mes_row = pd.Series({"Open": 3000.0, "High": 2999.0, "Low": 2990.0,
                         "Close": 2995.0})   # MES does NOT touch 3000
    return now, mnq_row, mes_row


def test_shadow_block_does_not_change_direction_or_position(_isolate_state):
    """The shadow block (with all Part B rules wired) changes NO field the strategy/
    executor reads — direction and the position record stay exactly as seeded."""
    mnq, mes = _hist()
    pipe = SessionPipeline(mnq, mes, lambda e: None)
    h = smt_state.load_hypothesis()
    h["direction"] = "down"
    h["cautious_price_initial"] = 20950.0
    h["cautious_price_secondary"] = 20900.0
    smt_state.save_hypothesis(h)
    pos_before = smt_state.load_position()

    now, mnq_row, mes_row = _fire_bar()
    pipe._run_smt_v2_detection(now, mnq_row, mes_row, mnq, mes, is_5m=False,
                               pre_daily=_pre_daily())

    out = smt_state.load_hypothesis()
    assert out["direction"] == "down"                  # untouched
    assert out["cautious_price_initial"] == 20950.0    # untouched
    assert out["cautious_price_secondary"] == 20900.0  # untouched
    assert smt_state.load_position() == pos_before     # position untouched
    # Shadow keys ARE populated.
    assert isinstance(out["smt_active_set"], list)
    assert out["smt_dominant"] is not None


def test_suppressions_debug_key_populated_and_isolated(_isolate_state):
    """`smt_suppressions` is written alongside `smt_active_set`/`smt_dominant` and is a
    list; the leg-state debug key is also persisted. None of these are read by strategy."""
    mnq, mes = _hist()
    pipe = SessionPipeline(mnq, mes, lambda e: None)
    h = smt_state.load_hypothesis()
    h["direction"] = "up"
    smt_state.save_hypothesis(h)

    now, mnq_row, mes_row = _fire_bar()
    pipe._run_smt_v2_detection(now, mnq_row, mes_row, mnq, mes, is_5m=False,
                               pre_daily=_pre_daily())

    out = smt_state.load_hypothesis()
    assert "smt_suppressions" in out and isinstance(out["smt_suppressions"], list)
    assert "smt_leg_state" in out and isinstance(out["smt_leg_state"], dict)
    assert out["direction"] == "up"  # still untouched


def test_shadow_exception_isolated(_isolate_state, monkeypatch):
    """A raising rule inside the shadow block cannot perturb the live path or any
    strategy-read field. Monkeypatch apply_rule_b to raise; the bar still runs, the
    smt-div event is still emitted, and direction stays untouched."""
    mnq, mes = _hist()
    pipe = SessionPipeline(mnq, mes, lambda e: None)
    h = smt_state.load_hypothesis()
    h["direction"] = "down"
    smt_state.save_hypothesis(h)

    def _raiser(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(hyp, "apply_rule_b", _raiser)

    now, mnq_row, mes_row = _fire_bar()
    events = pipe._run_smt_v2_detection(now, mnq_row, mes_row, mnq, mes,
                                        is_5m=False, pre_daily=_pre_daily())
    assert any(e.get("kind") == "smt-div" for e in events)  # live path produced its event
    out = smt_state.load_hypothesis()
    assert out["direction"] == "down"  # untouched despite the raise
