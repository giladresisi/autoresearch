import json
import pytest
from scripts.compare_analyzer_vs_legacy import compare, extract_legacy_intents


def test_comparison_reads_events_jsonl_but_never_writes_it(tmp_path):
    import inspect
    import scripts.compare_analyzer_vs_legacy as mod
    src = inspect.getsource(mod)
    assert 'open(' not in src or '"w"' not in src.split("events.jsonl")[0][-200:]


def test_extracts_legacy_entry_intents_from_events(tmp_path):
    ev = tmp_path / "events.jsonl"
    ev.write_text(json.dumps({"kind": "new-stop-entry", "time": "2026-08-13T09:35:00-04:00",
                              "price": 29800.0, "direction": "down"}) + "\n", encoding="utf-8")
    out = extract_legacy_intents(tmp_path)
    assert len(out) == 1 and out[0]["price"] == 29800.0


def test_compare_returns_both_sides_and_a_window(tmp_path):
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "trader_decisions.jsonl").write_text("", encoding="utf-8")
    out = compare(tmp_path)
    assert "legacy" in out and "trader" in out and "window" in out


def test_compare_restricts_to_the_0930_window(tmp_path):
    ev = tmp_path / "events.jsonl"
    ev.write_text("\n".join([
        json.dumps({"kind": "new-stop-entry", "time": "2026-08-13T03:00:00-04:00", "price": 1.0}),
        json.dumps({"kind": "new-stop-entry", "time": "2026-08-13T09:35:00-04:00", "price": 2.0}),
    ]) + "\n", encoding="utf-8")
    (tmp_path / "trader_decisions.jsonl").write_text("", encoding="utf-8")
    assert len(compare(tmp_path)["legacy"]) == 1


# --- added during implementation (not in the plan) --------------------------- #

def test_real_live_records_use_entry_price_not_price(tmp_path):
    """Live `new-stop-entry` records carry `entry_price`; the plan's fixture used
    `price`. Both must normalise to the same column or the artifact is blank in
    production."""
    ev = tmp_path / "events.jsonl"
    ev.write_text(json.dumps({"kind": "new-stop-entry", "time": "2026-08-13T09:35:00-04:00",
                              "entry_price": 29800.0, "stop_price": 29820.0,
                              "direction": "down"}) + "\n", encoding="utf-8")
    out = extract_legacy_intents(tmp_path)
    assert out[0]["price"] == 29800.0 and out[0]["stop"] == 29820.0


def test_trader_side_is_read_from_its_own_file(tmp_path):
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "trader_decisions.jsonl").write_text("\n".join([
        json.dumps({"kind": "intended_entry", "time": "2026-08-13T09:42:00-04:00",
                    "trigger": 29760.25, "stop": 29770.0, "mechanism": "m",
                    "artifact_id": "i", "artifact_label": "L"}),
        json.dumps({"kind": "veto", "time": "2026-08-13T09:44:00-04:00",
                    "mechanism": "m", "reason": "dol_floor", "detail": {"remaining": 40}}),
    ]) + "\n", encoding="utf-8")
    out = compare(tmp_path)
    assert len(out["trader"]) == 1 and out["trader"][0]["price"] == 29760.25
    assert len(out["vetoes"]) == 1 and out["vetoes"][0]["reason"] == "dol_floor"


def test_missing_files_are_not_an_error(tmp_path):
    out = compare(tmp_path)
    assert out["legacy"] == [] and out["trader"] == []


def test_render_produces_a_side_by_side_table(tmp_path):
    from scripts.compare_analyzer_vs_legacy import render
    (tmp_path / "events.jsonl").write_text(json.dumps(
        {"kind": "new-stop-entry", "time": "2026-08-13T09:35:00-04:00",
         "entry_price": 29800.0}) + "\n", encoding="utf-8")
    text = render(compare(tmp_path))
    assert "legacy" in text and "29800.0" in text
