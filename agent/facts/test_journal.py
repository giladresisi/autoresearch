import json
import pandas as pd
import pytest
from agent.facts.journal import Journal, FACTS_JOURNAL_NAME, FACTS_SNAPSHOT_NAME
from agent.facts.store import FactStore
from agent.facts.records import Fact, FactClass, FactState

TS = pd.Timestamp("2026-08-18 09:20", tz="America/New_York")


def _f(fid="a"):
    return Fact(id=fid, cls=FactClass.LEVEL, ticker="MNQ", label="l", name="day_high",
                reference_ts=TS, price=100.0, price_low=None, price_high=None,
                timeframe=None, resolution="1min", state=FactState.LIVE,
                state_ts=TS, provenance={}, extra={})


def test_journal_file_name_is_not_events_jsonl():
    """events.jsonl is the legacy stream the regression diffs — never touch it."""
    assert FACTS_JOURNAL_NAME != "events.jsonl"
    assert "events" not in FACTS_JOURNAL_NAME


def test_journal_appends_one_json_line_per_event(tmp_path):
    j = Journal(tmp_path)
    j.fact_created(_f("a"))
    j.fact_created(_f("b"))
    lines = (tmp_path / FACTS_JOURNAL_NAME).read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "fact_created"


def test_state_change_event_records_both_states(tmp_path):
    j = Journal(tmp_path)
    f = _f()
    old = f.state
    f.set_state(FactState.SWEPT, TS)
    j.fact_state_changed(f, old)
    rec = json.loads((tmp_path / FACTS_JOURNAL_NAME).read_text(encoding="utf-8").strip())
    assert rec["from"] == "live" and rec["to"] == "swept"


def test_every_record_carries_id_and_label(tmp_path):
    j = Journal(tmp_path)
    j.fact_created(_f())
    rec = json.loads((tmp_path / FACTS_JOURNAL_NAME).read_text(encoding="utf-8").strip())
    assert rec["id"] == "a" and rec["label"] == "l"


def test_snapshot_then_restore_round_trips_the_store(tmp_path):
    s = FactStore()
    s.upsert(_f("a"))
    s.extend_coverage(FactClass.LEVEL, "MNQ", TS - pd.Timedelta(days=14))
    j = Journal(tmp_path)
    j.snapshot(s)
    back = j.restore()
    assert back.get("a") is not None
    assert back.covered_from(FactClass.LEVEL, "MNQ") == TS - pd.Timedelta(days=14)


def test_restore_returns_none_when_no_snapshot_exists(tmp_path):
    assert Journal(tmp_path).restore() is None


def test_snapshot_write_is_atomic(tmp_path):
    """A partial write must never replace a good snapshot."""
    j = Journal(tmp_path)
    s = FactStore(); s.upsert(_f("a"))
    j.snapshot(s)
    assert not list(tmp_path.glob("*.tmp")), "temp files must be renamed away"
    assert (tmp_path / FACTS_SNAPSHOT_NAME).exists()


# --- added during implementation (not in the plan) --------------------------- #

def test_journal_never_raises_on_an_unwritable_dir(tmp_path):
    """A genuinely unwritable dir: a plain FILE occupies the path the dir would need.

    (NOT a literal "/nonexistent/..." — on Windows that resolves to C:\\nonexistent and
    is happily created, so it exercises the success path and litters the drive root.)
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    j = Journal(str(blocker / "facts"))
    j.fact_created(_f())
    j.snapshot(FactStore())
    assert j.restore() is None


def test_coverage_event_carries_id_and_label(tmp_path):
    j = Journal(tmp_path)
    j.coverage_extended(FactClass.LEVEL, "MNQ", TS)
    rec = json.loads((tmp_path / FACTS_JOURNAL_NAME).read_text(encoding="utf-8").strip())
    assert rec["id"] and rec["label"] and rec["event"] == "coverage_extended"
