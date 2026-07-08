"""Offline-vs-online consistency-check tests (Wave 4.3)."""

import pytest

from conftest import fixtures_present, load_live_frames
from consistency import check_consistency
from facts_adapter import build_snapshot
from records import write_audit_record

pytestmark = pytest.mark.skipif(not fixtures_present(), reason="golden fixtures absent")


def _write_online_record(path, frames):
    snap = build_snapshot(frames)
    rec = {"trigger_ts": str(frames["now"]), "trigger_kind": "new-hypothesis",
           "facts_content_hash": snap.content_hash,
           "facts_snapshot_ref": {"inline": snap.text, "hash": snap.content_hash},
           "outcome": None}
    write_audit_record(path, rec)
    return snap


def test_matching_paths_zero_drift(tmp_path):
    frames = load_live_frames()
    path = tmp_path / "audit.jsonl"
    _write_online_record(path, frames)
    res = check_consistency(path, frames["mnq_today"], frames["mes_today"])
    assert res["n"] == 1
    assert res["matches"] == 1
    assert res["drift"] == []


def test_injected_difference_flagged_with_section(tmp_path):
    frames = load_live_frames()
    path = tmp_path / "audit.jsonl"
    _write_online_record(path, frames)          # online hash from the pristine frames

    # Offline recompute over a frame with a shifted last close → now_price drifts.
    mnq_mod = frames["mnq_today"].copy()
    mnq_mod.iloc[-1, mnq_mod.columns.get_loc("Close")] += 25.0
    res = check_consistency(path, mnq_mod, frames["mes_today"])
    assert res["matches"] == 0
    assert len(res["drift"]) == 1
    assert "now_price" in res["drift"][0]["sections"]
