"""Outcome-annotation tests (Wave 4.1)."""

import pandas as pd

from annotate import annotate_record, annotate_session
from records import read_audit_records, write_audit_record


def _bars(rows, start="2026-05-19 10:00:00"):
    idx = pd.date_range(start, periods=len(rows), freq="1min", tz="America/New_York")
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])


def _record(direction, target_price, arrival="2026-05-19 10:00:00", hyp="up"):
    return {"arrival_ts": arrival,
            "decision": {"next_move": {"direction": direction,
                                       "move_target": {"level": "x", "price": target_price}}},
            "paired_diff": {"direction": {"hypothesis": hyp}}}


def test_target_reached_first():
    bars = _bars([(30000, 30000, 30000, 30000), (30010, 30130, 29995, 30120)])
    out = annotate_record(_record("up", 30120), bars)
    assert out["status"] == "ok"
    assert out["target_outcome"] == "target"
    assert out["realized_direction"] == "up"
    assert out["ai_correct"] is True


def test_flip_fired_first():
    bars = _bars([(30000, 30000, 30000, 30000), (30000, 30050, 29870, 29900)])
    out = annotate_record(_record("up", 30200), bars)
    assert out["target_outcome"] == "flip"
    assert out["realized_direction"] == "down"
    assert out["ai_correct"] is False


def test_neither_within_horizon():
    bars = _bars([(30000, 30000, 30000, 30000), (30000, 30040, 29960, 30010)])
    out = annotate_record(_record("up", 30300), bars)
    assert out["target_outcome"] == "neither"
    assert out["realized_direction"] == "chop"


def test_truncated_when_no_post_arrival_bars():
    bars = _bars([(30000, 30000, 30000, 30000)])
    out = annotate_record(_record("up", 30120, arrival="2026-05-19 12:00:00"), bars)
    assert out["status"] == "truncated"


def test_annotate_session_fills_outcome_slot(tmp_path):
    path = tmp_path / "ai_decisions_audit.jsonl"
    rec = _record("up", 30120)
    rec.update({"trigger_ts": "2026-05-19 09:59:00", "trigger_kind": "new-hypothesis",
                "facts_content_hash": "h", "facts_snapshot_ref": {}, "verdict": "clean",
                "fallback": False, "error": None, "outcome": None})
    write_audit_record(path, rec)
    bars = _bars([(30000, 30000, 30000, 30000), (30010, 30130, 29995, 30120)])
    annotate_session(path, bars)
    loaded = read_audit_records(path)
    assert loaded[0]["outcome"]["target_outcome"] == "target"
    assert loaded[0]["outcome"]["status"] == "ok"
