"""Phase-3 JSON bus tests: atomic writes + ID/parent linkage (plan §Phase 3)."""

import json

import pytest

from bus import DecisionBus, atomic_write_json, read_json


def test_atomic_write_leaves_no_tmp_and_valid_json(tmp_path):
    p = tmp_path / "thesis.json"
    atomic_write_json(p, {"a": 1, "b": [1, 2, 3]})
    assert not (tmp_path / "thesis.json.tmp").exists()
    assert json.loads(p.read_text()) == {"a": 1, "b": [1, 2, 3]}


def test_interrupted_write_keeps_prior_file_intact(tmp_path, monkeypatch):
    p = tmp_path / "thesis.json"
    atomic_write_json(p, {"v": "first"})

    # Simulate a crash during the replace step: the temp file is written but the rename
    # fails → the reader must still see the whole prior file, never a torn write.
    import bus as _bus
    real_replace = _bus.os.replace

    def boom(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(_bus.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(p, {"v": "second"})
    monkeypatch.setattr(_bus.os, "replace", real_replace)
    assert read_json(p) == {"v": "first"}          # prior content intact


def test_ids_and_parent_linkage(tmp_path):
    bus = DecisionBus(tmp_path, date="2026-05-19")
    th = bus.publish_thesis({"bias": "UP"}, issued_at="t0", facts_hash="h1")
    assert th["thesis_id"] == "th_2026-05-19_001"
    pl = bus.publish_plan({"verdict": "SETUP"}, thesis_id=th["thesis_id"], issued_at="t1")
    assert pl["plan_id"] == "pl_2026-05-19_001"
    assert pl["thesis_id"] == th["thesis_id"]       # parent linkage
    assert bus.read_thesis()["thesis_id"] == th["thesis_id"]
    assert bus.read_plan()["plan_id"] == pl["plan_id"]


def test_new_thesis_invalidates_standing_plan(tmp_path):
    bus = DecisionBus(tmp_path, date="2026-05-19")
    th1 = bus.publish_thesis({"bias": "UP"}, issued_at="t0")
    bus.publish_plan({"verdict": "SETUP"}, thesis_id=th1["thesis_id"], issued_at="t1")
    assert bus.read_plan() is not None
    bus.publish_thesis({"bias": "DOWN"}, issued_at="t2")   # a new thesis
    assert bus.read_plan() is None                  # dependent plan cleared
