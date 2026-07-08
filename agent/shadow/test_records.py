"""Audit + dual-event record tests (Wave 2.5)."""

import json

from records import (
    REAL_HYP_EVENT_KEYS,
    build_ai_daily_trend_event,
    build_ai_hypothesis_event,
    build_paired_diff,
    read_audit_records,
    resolve_snapshot,
    snapshot_ref,
    write_audit_record,
)

_AUDIT_KEYS = {"trigger_ts", "arrival_ts", "trigger_kind", "facts_content_hash",
               "facts_snapshot_ref", "attempts", "decision", "paired_diff", "verdict",
               "fallback", "error", "outcome"}


def _sample_next():
    return {"direction": "up", "confidence": "medium",
            "move_target": {"level": "prev1_day_high", "price": 30010.0},
            "flip_trigger": "close back below the daily mid", "flipped_target": None,
            "arm_entry_confirmation": "yes"}


def _sample_daily():
    return {"direction": "up", "confidence": "medium", "regime": "trend", "S": 4.0,
            "day_dol": "prev-day high", "weakens_to_neutral_if": "x", "flips_if": "y"}


def test_audit_record_round_trip(tmp_path):
    path = tmp_path / "shadow_audit.jsonl"
    record = {
        "trigger_ts": "2026-05-19 09:27:59-04:00",
        "arrival_ts": "2026-05-19 09:29:18-04:00",
        "trigger_kind": "5m_boundary",
        "facts_content_hash": "deadbeef",
        "facts_snapshot_ref": {"inline": "## S0 META\n...", "hash": "deadbeef"},
        "attempts": [], "decision": {"next_move": _sample_next()},
        "paired_diff": {}, "verdict": "clean", "fallback": False,
        "error": None, "outcome": None,
    }
    write_audit_record(path, record)
    write_audit_record(path, record)
    loaded = read_audit_records(path)
    assert len(loaded) == 2
    assert _AUDIT_KEYS.issubset(loaded[0].keys())
    assert loaded[0] == json.loads(json.dumps(record))


def test_events_native_new_hypothesis_shape():
    real = {"kind": "new-hypothesis", "time": "t", "direction": "up", "price": 30000.0,
            "weekly_mid": 29900.0, "daily_mid": 29950.0, "last_liquidity": "TDO",
            "targets": [], "cautious_price_initial": 30010.0,
            "cautious_price_initial_level": "prev1_day_high",
            "cautious_price_secondary": None, "cautious_price_secondary_level": None,
            "entry_ranges": [], "direction_reason": "sweep"}
    ai = build_ai_hypothesis_event(_sample_next(), time_iso="2026-05-19 09:29:18-04:00",
                                   hyp_event=real, now_price=30001.0)
    for k in REAL_HYP_EVENT_KEYS:
        assert k in ai                       # carries every key the real event has
    assert ai["source"] == "ai-shadow"
    assert ai["kind"] == "new-hypothesis"
    assert ai["direction"] == "up"           # next.direction up → hyp none↔ maps up
    assert ai["price"] == 30001.0            # decision-time price from the snapshot


def test_events_native_daily_trend_shape():
    ai = build_ai_daily_trend_event(_sample_daily(), time_iso="t")
    assert ai["kind"] == "daily-trend" and ai["source"] == "ai-shadow"
    assert ai["direction"] == "up" and ai["regime"] == "trend"


def test_snapshot_ref_inline_and_file(tmp_path):
    small = "short facts"
    ref_small = snapshot_ref(small, "h1", tmp_path / "snaps", inline_max=4000)
    assert ref_small["inline"] == small
    assert resolve_snapshot(ref_small) == small

    big = "x" * 5000
    ref_big = snapshot_ref(big, "h2", tmp_path / "snaps", inline_max=4000)
    assert "ref" in ref_big and "inline" not in ref_big
    assert (tmp_path / "snaps" / "h2.txt").exists()
    assert resolve_snapshot(ref_big) == big


def test_outcome_slot_empty_then_fillable(tmp_path):
    path = tmp_path / "audit.jsonl"
    rec = {"trigger_ts": "t", "arrival_ts": "t2", "trigger_kind": "k",
           "facts_content_hash": "h", "facts_snapshot_ref": {}, "attempts": [],
           "decision": {}, "paired_diff": {}, "verdict": "clean", "fallback": False,
           "error": None, "outcome": None}
    write_audit_record(path, rec)
    loaded = read_audit_records(path)
    assert loaded[0]["outcome"] is None                 # empty on write
    loaded[0]["outcome"] = {"realized_direction": "up", "target_reached": True}
    assert loaded[0]["outcome"]["target_reached"] is True


def test_paired_diff_direction_mapping():
    hyp = {"direction": "none", "cautious_price_initial_level": None}
    diff = build_paired_diff(hyp, {"direction": "neutral", "move_target": None,
                                   "arm_entry_confirmation": "no", "confidence": "low"},
                             _sample_daily())
    assert diff["direction"]["hypothesis"] == "neutral"  # none↔neutral
    assert diff["direction"]["agree"] is True
