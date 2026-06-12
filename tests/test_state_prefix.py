# tests/test_state_prefix.py
# Wave 4 — the state-dir prefix: disjoint-folder isolation, in-memory keying, and the
# final snapshot dump.

import json

import pytest

import paths
import smt_state


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """Each test starts from a known prefix and a clean, disk-backed store. ACT_GLOBAL_DIR
    is isolated so the live-mode global.json (which lives in general_live_dir()) never
    touches the real machine-global folder; per-test seed tests override it as needed."""
    monkeypatch.setattr(paths, "_STATE_DIR", paths.Path("data"))
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path / "_global"))
    smt_state.set_in_memory_mode(False)
    smt_state._hyp_cache = None
    smt_state._hyp_cache_valid = False
    yield
    smt_state.set_in_memory_mode(False)


# ── Disjoint on-disk isolation ─────────────────────────────────────────────────

def test_set_state_dir_writes_disjoint_files(tmp_path):
    # position.json follows state_dir() (unlike global.json, which is now pinned to
    # general_live_dir() in live mode) — so it exercises the disjoint-prefix isolation.
    a, b = tmp_path / "A", tmp_path / "B"
    paths.set_state_dir(a)
    smt_state.save_position({**smt_state.DEFAULT_POSITION, "failed_entries": 1})
    paths.set_state_dir(b)
    smt_state.save_position({**smt_state.DEFAULT_POSITION, "failed_entries": 2})

    assert (a / "position.json").exists() and (b / "position.json").exists()
    paths.set_state_dir(a)
    assert smt_state.load_position()["failed_entries"] == 1
    paths.set_state_dir(b)
    assert smt_state.load_position()["failed_entries"] == 2


# ── global.json: live = general_live_dir (cross-session); backtest = per-run state_dir ──

def test_global_json_persists_in_general_live_dir_when_live(tmp_path, monkeypatch):
    """LIVE (disk) mode: global.json lives in general_live_dir(), NOT the per-session
    state_dir — so the dynamic ATH persists across sessions with no prior-session seeding."""
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path / "g"))
    paths.set_state_dir(tmp_path / "session-A")
    smt_state.save_global({"all_time_high": 30807.0, "confidence": "medium", "trend": "up"})

    assert (paths.general_live_dir() / "global.json").exists()
    assert not (tmp_path / "session-A" / "global.json").exists()
    # A different session reads back the SAME persisted ATH (no seeding needed).
    paths.set_state_dir(tmp_path / "session-B")
    assert smt_state.load_global()["all_time_high"] == 30807.0


def test_global_json_under_state_dir_in_backtest(tmp_path):
    """BACKTEST (in-memory) mode: global.json stays under the per-run state_dir so each run
    is isolated and final_snapshot() captures it."""
    smt_state.set_in_memory_mode(True)
    run = tmp_path / "run"
    paths.set_state_dir(run)
    smt_state.save_global({"all_time_high": 5.0, "confidence": "medium", "trend": "up"})
    smt_state.final_snapshot()
    assert (run / "global.json").exists()
    assert json.loads((run / "global.json").read_text())["all_time_high"] == 5.0


# ── In-memory store keyed by the state dir (no cross-run clobber) ───────────────

def test_in_memory_store_keyed_by_state_dir(tmp_path):
    smt_state.set_in_memory_mode(True)
    paths.set_state_dir(tmp_path / "runA")
    smt_state.save_position({**smt_state.DEFAULT_POSITION, "failed_entries": 7})

    # A different run dir must not see runA's position.
    paths.set_state_dir(tmp_path / "runB")
    assert smt_state.load_position()["failed_entries"] == 0

    paths.set_state_dir(tmp_path / "runA")
    assert smt_state.load_position()["failed_entries"] == 7


def test_reset_in_memory_clears_store(tmp_path):
    smt_state.set_in_memory_mode(True)
    paths.set_state_dir(tmp_path / "run")
    smt_state.save_position({**smt_state.DEFAULT_POSITION, "failed_entries": 3})
    smt_state.reset_in_memory()
    assert smt_state.load_position()["failed_entries"] == 0


# ── final_snapshot ─────────────────────────────────────────────────────────────

def test_final_snapshot_dumps_four_jsons(tmp_path):
    run = tmp_path / "run"
    smt_state.set_in_memory_mode(True)
    paths.set_state_dir(run)
    smt_state.save_global({"all_time_high": 9.0, "confidence": "high", "trend": "up"})
    smt_state.save_daily({**smt_state.DEFAULT_DAILY, "estimated_dir": "down"})
    smt_state.save_hypothesis({**smt_state.DEFAULT_HYPOTHESIS, "direction": "up"})
    smt_state.save_position({**smt_state.DEFAULT_POSITION, "failed_entries": 2})

    smt_state.final_snapshot()

    for name in ("global.json", "daily.json", "hypothesis.json", "position.json"):
        assert (run / name).exists(), f"{name} should be snapshotted"
    assert json.loads((run / "global.json").read_text())["all_time_high"] == 9.0
    assert json.loads((run / "position.json").read_text())["failed_entries"] == 2
