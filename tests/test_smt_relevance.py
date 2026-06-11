# tests/test_smt_relevance.py
# Contract B unit tests (SMT V2 Phase 2): hypothesis.to_record / smt_authority /
# dominant / ingest_smts, the divs-record schema, active-set invalidation, and the
# SHADOW no-behavior-change assertions for session_pipeline._run_smt_v2_detection.
#
# Phase 2 is SHADOW-ONLY: the relevance active set + dominant are computed and stored
# under hypothesis.json debug keys but DO NOT drive direction. "Existing suite green"
# == parity. These tests exercise the pure functions exhaustively (every authority
# tiebreak, every ingest gate branch + both exact boundaries, every to_record tier)
# plus the shadow wiring's inertness and exception isolation.

from __future__ import annotations

import pandas as pd
import pytest

import smt_state
import hypothesis as hyp
from hypothesis import (
    RELEVANCE_X_PTS,
    to_record,
    smt_authority,
    dominant,
    ingest_smts,
    collapsed_status,
    _tier_rank,
)
from smt_detect import fulfillment_status
from session_pipeline import SessionPipeline


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _emission(**overrides) -> dict:
    """A full valid smt_detect level emission (override per test)."""
    base = {
        "kind": "smt",
        "type": "wick",
        "side": "bearish",
        "direction": "short",
        "timeframe": "1m",
        "time": "2026-06-09T10:00:00",
        "leader": "mnq",
        "ref_name": "day_high",
        "mnq_price": 21000.0,
        "mes_price": 3000.0,
        "mnq_lvl_price": 21000.0,
        "mes_lvl_price": 3000.0,
    }
    base.update(overrides)
    return base


def _rec(**overrides) -> dict:
    """A full valid divs-record (already in to_record schema)."""
    base = {
        "kind": "smt",
        "type": "wick",
        "side": "bearish",
        "direction": "short",
        "timeframe": "1m",
        "time": "2026-06-09T10:00:00",
        "leader": "mnq",
        "ref_name": "day_high",
        "tier": "day",
        "key": "day_high|short|wick",
        "fulfilled": False,
        "mnq_price": 21000.0,
        "mes_price": 3000.0,
        "mnq_lvl_price": 21000.0,
    }
    base.update(overrides)
    # Keep key consistent with ref_name/direction/type unless explicitly overridden.
    if "key" not in overrides and base["kind"] == "smt":
        base["key"] = f"{base['ref_name']}|{base['direction']}|{base['type']}"
    return base


# ===========================================================================
# smt_authority / dominant — authority ordering
# ===========================================================================
def test_day_high_wick_beats_day_high_body():
    a = _rec(type="wick", key="day_high|short|wick")
    b = _rec(type="body", key="day_high|short|body")
    assert smt_authority(a) > smt_authority(b)


def test_any_day_beats_any_fill():
    # day SMT (lowest kind/recency) vs fill (highest recency) → day still wins on tier.
    day = _rec(tier="day", type="body", time="2020-01-01T00:00:00")
    fill = _rec(tier="fill", type="fill_a", time="2030-01-01T00:00:00",
                key="fvg_x_bull", ref_name="fvg_x_bull", kind="fill")
    assert smt_authority(day) > smt_authority(fill)


def test_any_fill_beats_any_session():
    fill = _rec(tier="fill", type="fill_a", kind="fill",
                key="fvg_x_bull", ref_name="fvg_x_bull", time="2020-01-01T00:00:00")
    session = _rec(tier="session", type="wick", ref_name="ny_morning_high",
                   key="ny_morning_high|short|wick", time="2030-01-01T00:00:00")
    assert smt_authority(fill) > smt_authority(session)


def test_fill_does_not_beat_day():
    fill = _rec(tier="fill", type="fill_a", kind="fill",
                key="fvg_x_bull", ref_name="fvg_x_bull", time="2030-01-01T00:00:00")
    day = _rec(tier="day", time="2020-01-01T00:00:00")
    assert smt_authority(fill) < smt_authority(day)


def test_day_low_wick_outranks_day_high_wick_by_recency():
    # Same tier+kind, different levels → newer fire wins.
    older = _rec(ref_name="day_high", key="day_high|short|wick",
                 time="2026-06-09T10:00:00")
    newer = _rec(ref_name="day_low", direction="long", side="bullish",
                 key="day_low|long|wick", time="2026-06-09T10:05:00")
    assert smt_authority(newer) > smt_authority(older)


def test_ath_and_week_top_bucket():
    # ATH and week share the top tier rank → tier alone does not separate them.
    ath = _rec(tier="ATH", time="2026-06-09T10:00:00")
    week = _rec(tier="week", ref_name="week_high", key="week_high|short|wick",
                time="2026-06-09T10:00:00")
    auth_ath = smt_authority(ath)
    auth_week = smt_authority(week)
    assert auth_ath[0] == auth_week[0]  # same tier_rank
    # With identical kind+recency+tf they are equal on the tuple.
    assert auth_ath == auth_week


def test_30m_outranks_15m_hidden_subtiebreak():
    # Same tier/kind/recency, 30m > 15m (lowest-significance sub-tiebreak).
    r30 = _rec(type="body", timeframe="30m", key="day_high|short|body")
    r15 = _rec(type="body", timeframe="15m", key="day_high|short|body")
    assert smt_authority(r30) > smt_authority(r15)


def test_dominant_empty_returns_none():
    assert dominant([]) is None
    assert dominant(None) is None


def test_dominant_picks_highest_authority():
    day = _rec(tier="day", type="wick")
    session = _rec(tier="session", ref_name="ny_morning_high",
                   key="ny_morning_high|short|wick")
    fill = _rec(tier="fill", kind="fill", type="fill_a",
                ref_name="fvg_x", key="fvg_x")
    assert dominant([session, fill, day]) is day


# ===========================================================================
# to_record — schema round-trips
# ===========================================================================
def test_to_record_wick_1m():
    r = to_record(_emission(type="wick", ref_name="day_high", direction="short",
                            timeframe="1m"))
    assert r["tier"] == "day"
    assert r["key"] == "day_high|short|wick"
    assert r["fulfilled"] is False
    for f in ("kind", "type", "side", "direction", "timeframe", "time", "leader",
              "ref_name", "tier", "key", "fulfilled", "mnq_price", "mes_price",
              "mnq_lvl_price"):
        assert f in r


def test_to_record_body_15m():
    r = to_record(_emission(type="body", ref_name="day_high", direction="short",
                            timeframe="15m"))
    assert r["type"] == "body" and r["timeframe"] == "15m"
    assert r["tier"] == "day"
    assert r["key"].endswith("|body")


def test_to_record_body_30m():
    r = to_record(_emission(type="body", ref_name="week_low", direction="long",
                            timeframe="30m"))
    assert r["type"] == "body" and r["timeframe"] == "30m"
    assert r["tier"] == "week"
    assert r["key"] == "week_low|long|body"


def test_to_record_fill_a_1h():
    r = to_record({
        "kind": "fill", "type": "fill_a", "side": "bullish", "direction": "long",
        "timeframe": "1h", "time": "t", "leader": "mnq", "ref_name": "fvg_1_bull",
        "mnq_price": 1.0, "mes_price": 2.0,
    })
    assert r["kind"] == "fill" and r["tier"] == "fill"
    assert r["key"] == "fvg_1_bull"


def test_to_record_fill_b_1h():
    r = to_record({
        "kind": "fill", "type": "fill_b", "side": "bearish", "direction": "short",
        "timeframe": "1h", "time": "t", "leader": "mes", "ref_name": "fvg_2_bear",
    })
    assert r["type"] == "fill_b" and r["tier"] == "fill"
    assert r["key"] == "fvg_2_bear"


def test_to_record_week_tier():
    r = to_record(_emission(ref_name="week_high", direction="short", type="wick"))
    assert r["tier"] == "week"


def test_to_record_session_tier():
    r = to_record(_emission(ref_name="ny_morning_high", direction="short", type="wick"))
    assert r["tier"] == "session"


def test_to_record_ath_tier():
    # ATH via explicit ref_name and via is_ath kwarg-on-emission.
    r1 = to_record(_emission(ref_name="ATH", direction="short", type="wick"))
    assert r1["tier"] == "ATH"
    r2 = to_record(_emission(ref_name="week_high", is_ath=True))
    assert r2["tier"] == "ATH"


def test_to_record_total_on_missing_fields():
    r = to_record({"kind": "smt"})  # partial emission
    assert r["fulfilled"] is False
    assert r["mnq_price"] is None
    # Non-dict input also must not raise.
    r2 = to_record(None)
    assert isinstance(r2, dict)


# ===========================================================================
# ingest_smts — the gate
# ===========================================================================
def test_ingest_flat_any_tier_enters():
    rec = _rec(tier="session", ref_name="ny_morning_high",
               key="ny_morning_high|short|wick", mnq_lvl_price=21000.0)
    out = ingest_smts([rec], [], flat=True, cautious_targets=None,
                      backing_tier="week", x_pts=RELEVANCE_X_PTS)
    assert len(out) == 1 and out[0]["key"] == "ny_morning_high|short|wick"


def test_ingest_active_proximity_enters():
    # tier session < backing week, but within x_pts of a cautious target → enters.
    rec = _rec(tier="session", ref_name="ny_morning_high",
               key="ny_morning_high|short|wick", mnq_lvl_price=21010.0)
    ct = {"cautious_price_initial": 21000.0, "cautious_price_secondary": ""}
    out = ingest_smts([rec], [], flat=False, cautious_targets=ct,
                      backing_tier="week", x_pts=25.0)
    assert len(out) == 1


def test_ingest_active_tier_enters():
    # tier week >= backing week, far from any target → enters on tier.
    rec = _rec(tier="week", ref_name="week_high", key="week_high|short|wick",
               mnq_lvl_price=25000.0)
    ct = {"cautious_price_initial": 21000.0, "cautious_price_secondary": ""}
    out = ingest_smts([rec], [], flat=False, cautious_targets=ct,
                      backing_tier="week", x_pts=25.0)
    assert len(out) == 1


def test_ingest_active_rejects_far_low_tier():
    # tier session < backing week AND far from targets → rejected.
    rec = _rec(tier="session", ref_name="ny_morning_high",
               key="ny_morning_high|short|wick", mnq_lvl_price=25000.0)
    ct = {"cautious_price_initial": 21000.0, "cautious_price_secondary": ""}
    out = ingest_smts([rec], [], flat=False, cautious_targets=ct,
                      backing_tier="week", x_pts=25.0)
    assert out == []


def test_ingest_boundary_exact_x_pts_passes():
    # distance exactly == x_pts passes (inclusive <=).
    rec = _rec(tier="session", ref_name="ny_morning_high",
               key="ny_morning_high|short|wick", mnq_lvl_price=21025.0)
    ct = {"cautious_price_initial": 21000.0, "cautious_price_secondary": ""}
    out = ingest_smts([rec], [], flat=False, cautious_targets=ct,
                      backing_tier="week", x_pts=25.0)
    assert len(out) == 1
    # one tick further → rejected.
    rec2 = _rec(tier="session", ref_name="ny_morning_high",
                key="ny_morning_high|short|wick", mnq_lvl_price=21025.001)
    out2 = ingest_smts([rec2], [], flat=False, cautious_targets=ct,
                       backing_tier="week", x_pts=25.0)
    assert out2 == []


def test_ingest_boundary_exact_backing_tier_passes():
    # tier == backing_tier passes (inclusive >= on tier_rank), even far from targets.
    rec = _rec(tier="day", ref_name="day_high", key="day_high|short|wick",
               mnq_lvl_price=99999.0)
    ct = {"cautious_price_initial": 21000.0, "cautious_price_secondary": ""}
    out = ingest_smts([rec], [], flat=False, cautious_targets=ct,
                      backing_tier="day", x_pts=25.0)
    assert len(out) == 1


def test_ingest_drops_incoming_fulfilled():
    rec = _rec(fulfilled=True)
    out = ingest_smts([rec], [], flat=True, cautious_targets=None,
                      backing_tier=None, x_pts=25.0)
    assert out == []


def test_ingest_dedup_by_key_supersede():
    old = _rec(time="2026-06-09T10:00:00", mnq_price=100.0)
    new = _rec(time="2026-06-09T10:05:00", mnq_price=200.0)  # same key, newer
    out = ingest_smts([new], [old], flat=True, cautious_targets=None,
                      backing_tier=None, x_pts=25.0)
    assert len(out) == 1 and out[0]["mnq_price"] == 200.0
    assert out[0]["time"] == "2026-06-09T10:05:00"


def test_ingest_none_records_returns_copy():
    active = [_rec()]
    out = ingest_smts(None, active, flat=True, cautious_targets=None,
                      backing_tier=None, x_pts=25.0)
    assert out == active
    assert out is not active  # copy, not the same list object


def test_ingest_does_not_mutate_input_list():
    active = [_rec(key="day_high|short|wick")]
    new = _rec(ref_name="week_high", key="week_high|short|wick", tier="week")
    out = ingest_smts([new], active, flat=True, cautious_targets=None,
                      backing_tier=None, x_pts=25.0)
    assert len(active) == 1  # input not mutated
    assert len(out) == 2


def test_ingest_active_secondary_target_proximity():
    # Only the secondary target is within range → still enters.
    rec = _rec(tier="session", ref_name="ny_morning_high",
               key="ny_morning_high|short|wick", mnq_lvl_price=20990.0)
    ct = {"cautious_price_initial": "", "cautious_price_secondary": 21000.0}
    out = ingest_smts([rec], [], flat=False, cautious_targets=ct,
                      backing_tier="week", x_pts=25.0)
    assert len(out) == 1


# ===========================================================================
# _tier_rank — every tier + unknown
# ===========================================================================
def test_tier_rank_all_values():
    assert _tier_rank("ATH") == 4
    assert _tier_rank("week") == 4
    assert _tier_rank("day") == 3
    assert _tier_rank("fill") == 2
    assert _tier_rank("session") == 1
    assert _tier_rank("bogus") == 0
    assert _tier_rank(None) == 0


# ===========================================================================
# Invalidation — active-set lifecycle (mirrors Task 2.1 drop logic + Contract C)
# ===========================================================================
def _invalidate(active: list[dict], detect_state: dict) -> list[dict]:
    """The drop step from the shadow block: drop fulfilled/gone/flagged-fulfilled."""
    status = fulfillment_status([r.get("key") for r in active], detect_state)
    return [
        r for r in active
        if status.get(r.get("key")) == "unfulfilled" and not r.get("fulfilled")
    ]


def test_invalidation_drops_fulfilled():
    active = [_rec(key="day_high|short|wick")]
    ds = {"day_high|short|wick": {"fulfilled": True}}
    assert _invalidate(active, ds) == []


def test_invalidation_drops_gone():
    active = [_rec(key="day_high|short|wick")]
    ds = {}  # key absent → gone
    assert _invalidate(active, ds) == []


def test_invalidation_drops_contradicted():
    # An opposite-direction record on the same key supersedes via ingest dedup.
    active = [_rec(direction="short", key="day_high|short|wick")]
    # A new opposite-direction SMT on day_low (different key) plus dedup; the
    # contradicted short stays only if not fulfilled — here we model contradiction as
    # the opposite SMT entering and the old being dropped by fulfillment.
    ds = {"day_high|short|wick": {"fulfilled": True}}  # short got fulfilled (regime flip)
    remaining = _invalidate(active, ds)
    new = _rec(ref_name="day_low", direction="long", side="bullish",
               key="day_low|long|wick", time="2026-06-09T10:05:00")
    out = ingest_smts([new], remaining, flat=True, cautious_targets=None,
                      backing_tier=None, x_pts=25.0)
    assert len(out) == 1 and out[0]["direction"] == "long"


def test_invalidation_dominant_redrives_after_drop():
    day = _rec(tier="day", key="day_high|short|wick")
    week = _rec(tier="week", ref_name="week_high", key="week_high|short|wick")
    active = [day, week]
    assert dominant(active) in (day, week)
    # week is the higher/equal tier; drop it (fulfilled) → dominant re-derives to day.
    ds = {"week_high|short|wick": {"fulfilled": True},
          "day_high|short|wick": {"fulfilled": False}}
    remaining = _invalidate(active, ds)
    assert remaining == [day]
    assert dominant(remaining) is day


# ===========================================================================
# SHADOW no-behavior-change + exception isolation (Task 3.3)
# ===========================================================================
@pytest.fixture()
def _isolate_state(tmp_path, monkeypatch):
    import paths
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    monkeypatch.setattr(smt_state, "_IN_MEMORY", False)
    # The process-local hypothesis cache (smt_state._hyp_cache) is keyed by nothing and
    # leaks across tests; invalidate it so each test reads from its own tmp_path.
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
    # MNQ + MES day_high levels so a wick SMT can fire (MNQ touches, MES doesn't).
    return {
        "liquidities": [
            {"name": "day_high", "kind": "level", "price": 21000.0, "sub": "high"},
        ],
        "liquidities_mes": [
            {"name": "day_high", "kind": "level", "price": 3000.0, "sub": "high"},
        ],
    }


def test_shadow_populates_active_set_without_touching_direction(_isolate_state):
    """Shadow runs (smt_active_set/smt_dominant populated) while `direction` is
    untouched by the shadow block — it stays at whatever it was before the bar."""
    mnq, mes = _hist()
    pipe = SessionPipeline(mnq, mes, lambda e: None)
    # Seed a known direction the shadow must not change.
    h = smt_state.load_hypothesis()
    h["direction"] = "down"
    smt_state.save_hypothesis(h)

    now = pd.Timestamp("2026-06-09 10:00:00", tz="America/New_York")
    mnq_row = pd.Series({"Open": 21000.0, "High": 21001.0, "Low": 20990.0,
                         "Close": 20995.0})  # MNQ touches 21000
    mes_row = pd.Series({"Open": 3000.0, "High": 2999.0, "Low": 2990.0,
                         "Close": 2995.0})   # MES does NOT touch 3000
    pipe._run_smt_v2_detection(now, mnq_row, mes_row, mnq, mes, is_5m=False,
                               pre_daily=_pre_daily())

    out = smt_state.load_hypothesis()
    assert out["direction"] == "down"  # shadow did NOT change direction
    assert isinstance(out["smt_active_set"], list)
    assert len(out["smt_active_set"]) >= 1  # the wick SMT was ingested
    assert out["smt_dominant"] is not None
    assert out["smt_dominant"]["key"] == "day_high|short|wick"


def test_shadow_does_not_change_direction_no_smt(_isolate_state):
    """With no SMT firing, direction is still untouched and active set stays empty."""
    mnq, mes = _hist()
    pipe = SessionPipeline(mnq, mes, lambda e: None)
    h = smt_state.load_hypothesis()
    h["direction"] = "up"
    smt_state.save_hypothesis(h)

    now = pd.Timestamp("2026-06-09 10:00:00", tz="America/New_York")
    # Neither instrument touches → no SMT.
    mnq_row = pd.Series({"Open": 21000.0, "High": 20990.0, "Low": 20980.0,
                         "Close": 20985.0})
    mes_row = pd.Series({"Open": 3000.0, "High": 2990.0, "Low": 2980.0,
                         "Close": 2985.0})
    pipe._run_smt_v2_detection(now, mnq_row, mes_row, mnq, mes, is_5m=False,
                               pre_daily=_pre_daily())
    out = smt_state.load_hypothesis()
    assert out["direction"] == "up"
    assert out["smt_active_set"] == []
    assert out["smt_dominant"] is None


def test_shadow_block_exception_is_swallowed(_isolate_state, monkeypatch):
    """A defect in the relevance infrastructure must never break the live path:
    monkeypatch to_record to raise; the bar still runs and smts.json is still
    written (live detection unaffected)."""
    mnq, mes = _hist()
    pipe = SessionPipeline(mnq, mes, lambda e: None)
    h = smt_state.load_hypothesis()
    h["direction"] = "down"
    smt_state.save_hypothesis(h)

    def _raiser(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(hyp, "to_record", _raiser)

    now = pd.Timestamp("2026-06-09 10:00:00", tz="America/New_York")
    mnq_row = pd.Series({"Open": 21000.0, "High": 21001.0, "Low": 20990.0,
                         "Close": 20995.0})
    mes_row = pd.Series({"Open": 3000.0, "High": 2999.0, "Low": 2990.0,
                         "Close": 2995.0})
    # Must NOT raise.
    events = pipe._run_smt_v2_detection(now, mnq_row, mes_row, mnq, mes,
                                        is_5m=False, pre_daily=_pre_daily())
    # Live path still produced its smt-div event for the fired SMT.
    assert any(e.get("kind") == "smt-div" for e in events)
    out = smt_state.load_hypothesis()
    assert out["direction"] == "down"  # untouched
    # Shadow store skipped (raise before save) → active set unchanged (default []).
    assert out["smt_active_set"] == []


# ===========================================================================
# --- wick/body collapse ---
# ===========================================================================

# (d) wick+body collapse to one logical member; wick confirmation supersedes; keys union.
def test_ingest_collapses_wick_body():
    wick = _rec(ref_name="week_high", direction="short", side="bearish", tier="week",
                type="wick", key="week_high|short|wick",
                time="2026-06-09T10:00:00", mnq_lvl_price=25000.0)
    body = _rec(ref_name="week_high", direction="short", side="bearish", tier="week",
                type="body", key="week_high|short|body",
                time="2026-06-09T10:00:00", mnq_lvl_price=25000.0)
    out = ingest_smts([body, wick], [], flat=True, cautious_targets=None,
                      backing_tier=None, x_pts=RELEVANCE_X_PTS)
    # Exactly ONE member for the logical (week_high, short) key.
    members = [r for r in out
               if r.get("ref_name") == "week_high" and r.get("direction") == "short"]
    assert len(members) == 1
    member = members[0]
    assert member["type"] == "wick"  # wick supersedes body
    assert set(member["keys"]) == {"week_high|short|wick", "week_high|short|body"}


# (e) fulfillment of a collapsed record (maps to >1 detect key).
def test_collapsed_record_fulfillment():
    keys = ["week_high|short|wick", "week_high|short|body"]
    rec = _rec(ref_name="week_high", direction="short", tier="week", type="wick",
               key="week_high|short|wick")
    rec["keys"] = list(keys)

    # ANY fulfilled → "fulfilled".
    ds1 = {"week_high|short|wick": {"fulfilled": True},
           "week_high|short|body": {"fulfilled": False}}
    st1 = fulfillment_status(keys, ds1)
    assert collapsed_status(rec, st1) == "fulfilled"

    # wick absent (gone), body unfulfilled → "unfulfilled" (not all gone).
    ds2 = {"week_high|short|body": {"fulfilled": False}}
    st2 = fulfillment_status(keys, ds2)
    assert collapsed_status(rec, st2) == "unfulfilled"

    # both keys absent → "gone".
    ds3: dict = {}
    st3 = fulfillment_status(keys, ds3)
    assert collapsed_status(rec, st3) == "gone"


# ===========================================================================
# (f) totality / None-safety of the collapse helpers
# ===========================================================================
def test_collapsed_status_totality():
    assert collapsed_status(None, {}) == "gone"
    assert collapsed_status({"keys": []}, {}) == "gone"
    # missing keys treated as gone
    assert collapsed_status({"keys": ["a", "b"]}, None) == "gone"


def test_to_record_carries_keys_list():
    r = to_record(_emission(type="wick", ref_name="day_high", direction="short"))
    assert r["keys"] == [r["key"]]
