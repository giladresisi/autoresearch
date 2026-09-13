"""Comparison harness tests (Wave 4.2)."""

from compare import build_comparison, render_comparison_table
from records import write_audit_record


def _rec(hyp_dir, ai_dir, agree, verdict="clean", realized="up",
         ai_correct=True):
    return {
        "trigger_ts": "2026-05-19 09:27:59-04:00", "arrival_ts": "2026-05-19 09:29:18-04:00",
        "trigger_kind": "new-hypothesis", "facts_content_hash": "h",
        "facts_snapshot_ref": {}, "attempts": [], "latency_total_sec": 70.0,
        "usage_total": {"input_tokens": 12000, "output_tokens": 400,
                        "cache_read_input_tokens": 11000},
        "decision": {}, "verdict": verdict, "fallback": False,
        "error": None,
        "paired_diff": {"direction": {"hypothesis": hyp_dir, "ai": ai_dir, "agree": agree},
                        "move_target": {"hypothesis": "L1", "ai": "L1", "agree": True},
                        "entry_seeking": {"ai_confidence": "medium"}},
        "outcome": {"realized_direction": realized, "ai_correct": ai_correct,
                    "hypothesis_correct": True},
    }


def test_build_comparison_counts_and_cost(tmp_path):
    path = tmp_path / "audit.jsonl"
    write_audit_record(path, _rec("up", "up", True))
    write_audit_record(path, _rec("down", "neutral", False))

    comp = build_comparison(path)
    s = comp["summary"]
    assert s["n_triggers"] == 2
    assert s["agree"]["direction"] == 1
    assert s["disagree"]["direction"] == 1
    assert s["agree"]["move_target"] == 2
    assert s["api_calls"] == 2
    assert s["mean_latency_sec"] == 70.0
    assert s["est_cost_usd"] > 0
    assert s["ai_correct"] == 2 and s["scored"] == 2
    assert len(comp["rows"]) == 2


def test_render_comparison_table_is_text(tmp_path):
    path = tmp_path / "audit.jsonl"
    write_audit_record(path, _rec("up", "up", True))
    table = render_comparison_table(build_comparison(path))
    assert isinstance(table, str)
    assert "triggers=1" in table
    assert "new-hypothesis" in table
