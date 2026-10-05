"""`trade.py agent-direction` says so when there is no plan, instead of queueing a no_plan refusal."""
import json

import trade


def test_plans_present_means_no_reason(tmp_path):
    (tmp_path / "plans.json").write_text(json.dumps({"abc": {"direction": "UP"}}))
    assert trade._agent_no_plan_reason(tmp_path) is None


def test_failed_thesis_call_is_named(tmp_path):
    (tmp_path / "thesis_state.json").write_text(json.dumps(
        {"thesis": None, "call_error": {"type": "ConnectionError", "message": "down"}}))
    assert "ConnectionError" in trade._agent_no_plan_reason(tmp_path)


def test_null_thesis_without_error_reads_pending(tmp_path):
    (tmp_path / "thesis_state.json").write_text(json.dumps({"thesis": None}))
    assert "no L1 thesis yet" in trade._agent_no_plan_reason(tmp_path)


def test_empty_folder_has_a_reason(tmp_path):
    assert trade._agent_no_plan_reason(tmp_path)
