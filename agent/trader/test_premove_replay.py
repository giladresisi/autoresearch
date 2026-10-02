"""Plan 46 — the UNRELATED path end to end on real tape (`-m slow`).

  * 2026-09-01 (UNRELATED, DOWN 488.25 before 09:20) with the flag ON and an EMPTY thesis
    cache: model-free and twice-identical.
  * 2026-08-13 (not big) and 2026-08-18 (PART): flag on == flag off, byte for byte. Both
    read the real recording, so they skip when it is not seeded.

Run folders go to a tmp `ACT_REGRESSION_DIR`; nothing is written to the global cache
(`allow_calls=False` everywhere).
"""
import json
import os

import pytest

from agent.trader import premove_context as pc
from agent.trader.cached_backend import NetworkCallRefused
from agent.trader.replay import run_replay

pytestmark = [pytest.mark.timeout(1200), pytest.mark.slow]

ARTIFACTS = ("trader_decisions.jsonl", "plans.json", "thesis_state.json")


def _bytes(run_dir, name):
    p = os.path.join(run_dir, name)
    with open(p, "rb") as fh:
        return fh.read()


def _decisions(run_dir):
    with open(os.path.join(run_dir, "trader_decisions.jsonl"), encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _replay(date, mode, monkeypatch, mid_target=False):
    monkeypatch.setattr(pc, "UNRELATED_PATH_MODE", mode)
    monkeypatch.setattr(pc, "MID_TARGET_ENABLED", mid_target)
    try:
        return run_replay([date], allow_calls=False)[date]
    finally:
        monkeypatch.setattr(pc, "UNRELATED_PATH_MODE", "off")


@pytest.fixture
def _regression_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_REGRESSION_DIR", str(tmp_path / "regression"))
    return tmp_path


def test_0901_unrelated_replay_is_deterministic_and_model_free(_regression_tmp,
                                                               monkeypatch):
    empty = _regression_tmp / "empty_cache"
    empty.mkdir()
    monkeypatch.setenv("ACT_THESIS_CACHE_DIR", str(empty))
    a = _replay("2026-09-01", "on", monkeypatch)
    b = _replay("2026-09-01", "on", monkeypatch)
    assert a["run_dir"] != b["run_dir"]
    for name in ARTIFACTS:
        assert _bytes(a["run_dir"], name) == _bytes(b["run_dir"], name), name
    assert a["cache"] == {"hits": 0, "misses": 0, "calls": 0, "refusals": 0}
    assert list(empty.iterdir()) == []

    blob = json.loads(_bytes(a["run_dir"], "thesis_state.json"))
    assert blob["thesis"]["thesis_source"] == "premove_unrelated"
    assert blob["thesis"]["bias"] == "UP"
    assert blob["call_meta"] == {"verdict": "premove_unrelated"}
    pm = json.loads(_bytes(a["run_dir"], "premove_context.json"))
    assert pm["context"]["status"] == "UNRELATED" and pm["action"] == "forced"
    # Direction only (the default): the forced plan takes the ordinary T2 pick.
    sel = [r for r in _decisions(a["run_dir"]) if r["kind"] == "target_selected"]
    assert sel, "no fill on 09-01 -- the target half would be untested"
    assert all(r["level"] != "premove_leg_mid" and "premove_mid" not in r for r in sel)

    # With the mid-target switch on, the same day takes profit at the leg's mid.
    t = _replay("2026-09-01", "on", monkeypatch, mid_target=True)
    sel_t = [r for r in _decisions(t["run_dir"]) if r["kind"] == "target_selected"]
    assert sel_t and sel_t[0]["level"] == "premove_leg_mid"


def _on_equals_off(date, monkeypatch, want_status):
    try:
        off = _replay(date, "off", monkeypatch)
    except NetworkCallRefused:
        pytest.skip(f"{date} is not seeded for the current code version")
    on = _replay(date, "on", monkeypatch)
    for name in ARTIFACTS:
        assert _bytes(off["run_dir"], name) == _bytes(on["run_dir"], name), name
    assert not os.path.exists(os.path.join(off["run_dir"], "premove_context.json"))
    pm = json.loads(_bytes(on["run_dir"], "premove_context.json"))
    assert pm["context"]["status"] == want_status


def test_non_big_control_is_byte_identical_flag_on_vs_off(_regression_tmp, monkeypatch):
    _on_equals_off("2026-08-13", monkeypatch, "NOT_BIG")


def test_part_day_flag_on_equals_flag_off(_regression_tmp, monkeypatch):
    """08-18 is PART and arm 1 does not fire there (age 35 m), so suppressing arm 1
    changes nothing and the model's recording decides in both arms."""
    _on_equals_off("2026-08-18", monkeypatch, "PART")
