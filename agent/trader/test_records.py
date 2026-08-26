import json
import pandas as pd
import pytest
from agent.trader.records import DecisionRecorder, DECISIONS_FILE

TS = pd.Timestamp("2026-08-18 09:42", tz="America/New_York")


def test_decisions_file_is_not_events_jsonl():
    assert DECISIONS_FILE != "events.jsonl" and "events" not in DECISIONS_FILE


def test_intended_entry_carries_every_replay_field(tmp_path):
    r = DecisionRecorder(tmp_path)
    r.intended_entry(now=TS, plan_id="p1", mechanism="extreme_reject_close",
                     artifact_id="abc123abc123", artifact_label="MNQ 1m bar 09:41",
                     trigger=29760.25, stop=29770.0, dol=29533.5)
    rec = json.loads((tmp_path / DECISIONS_FILE).read_text(encoding="utf-8").strip())
    for k in ("time", "plan_id", "mechanism", "artifact_id", "artifact_label",
              "trigger", "stop", "dol", "kind"):
        assert k in rec


def test_veto_records_the_reason(tmp_path):
    r = DecisionRecorder(tmp_path)
    r.veto(now=TS, plan_id="p1", mechanism="fvg_return_continuation",
           reason="dol_floor", detail={"remaining": 45.5})
    rec = json.loads((tmp_path / DECISIONS_FILE).read_text(encoding="utf-8").strip())
    assert rec["kind"] == "veto" and rec["reason"] == "dol_floor"


def test_records_are_append_only_one_json_per_line(tmp_path):
    r = DecisionRecorder(tmp_path)
    r.veto(now=TS, plan_id="p1", mechanism="m", reason="a", detail={})
    r.veto(now=TS, plan_id="p1", mechanism="m", reason="b", detail={})
    lines = (tmp_path / DECISIONS_FILE).read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2 and all(json.loads(ln) for ln in lines)


def test_artifact_id_and_label_always_travel_together(tmp_path):
    r = DecisionRecorder(tmp_path)
    r.bind(now=TS, plan_id="p1", mechanism="m", artifact_id="i", artifact_label="L")
    rec = json.loads((tmp_path / DECISIONS_FILE).read_text(encoding="utf-8").strip())
    assert rec["artifact_id"] == "i" and rec["artifact_label"] == "L"


def test_recorder_never_raises_on_unwritable_dir(tmp_path):
    """A genuinely unwritable dir: a plain FILE occupies the path the dir would need.

    (The plan's draft used a literal "/nonexistent/path/xyz". On Windows that resolves
    to C:\\nonexistent\\path\\xyz, is happily CREATED, and so exercises the success
    path while littering the drive root — the opposite of what the test is named for.)
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    r = DecisionRecorder(str(blocker / "decisions"))
    r.veto(now=TS, plan_id="p", mechanism="m", reason="r", detail={})


# --- added during implementation (not in the plan) --------------------------- #

def test_veto_detail_survives_the_round_trip(tmp_path):
    r = DecisionRecorder(tmp_path)
    r.veto(now=TS, plan_id="p1", mechanism="m", reason="dol_floor",
           detail={"remaining": 45.5, "trigger": 29760.25})
    rec = json.loads((tmp_path / DECISIONS_FILE).read_text(encoding="utf-8").strip())
    assert rec["detail"]["remaining"] == 45.5


def test_every_kind_writes_time_plan_id_and_kind(tmp_path):
    r = DecisionRecorder(tmp_path)
    r.bind(now=TS, plan_id="p", mechanism="m", artifact_id="i", artifact_label="L")
    r.unbind(now=TS, plan_id="p", mechanism="m", reason="superseded")
    r.plan_dead(now=TS, plan_id="p", reason="dol_reached")
    recs = [json.loads(l) for l in
            (tmp_path / DECISIONS_FILE).read_text(encoding="utf-8").strip().split("\n")]
    assert [r_["kind"] for r_ in recs] == ["bind", "unbind", "plan_dead"]
    for r_ in recs:
        assert r_["time"] and r_["plan_id"] == "p"


def test_time_is_the_bar_timestamp_verbatim(tmp_path):
    r = DecisionRecorder(tmp_path)
    r.plan_dead(now=TS, plan_id="p", reason="x")
    rec = json.loads((tmp_path / DECISIONS_FILE).read_text(encoding="utf-8").strip())
    assert rec["time"] == TS.isoformat()
