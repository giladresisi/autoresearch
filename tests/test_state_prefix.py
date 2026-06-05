# tests/test_state_prefix.py
# Wave 4 — the state-dir prefix: disjoint-folder isolation, in-memory keying, the final
# snapshot dump, and cross-session ATH continuity (seed_global_from_prior).

import json

import pytest

import paths
import smt_state


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Each test starts from a known prefix and a clean, disk-backed store."""
    monkeypatch.setattr(paths, "_STATE_DIR", paths.Path("data"))
    smt_state.set_in_memory_mode(False)
    smt_state._hyp_cache = None
    smt_state._hyp_cache_valid = False
    yield
    smt_state.set_in_memory_mode(False)


# ── Disjoint on-disk isolation ─────────────────────────────────────────────────

def test_set_state_dir_writes_disjoint_files(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    paths.set_state_dir(a)
    smt_state.save_global({"all_time_high": 1.0, "confidence": "medium", "trend": "up"})
    paths.set_state_dir(b)
    smt_state.save_global({"all_time_high": 2.0, "confidence": "medium", "trend": "up"})

    assert (a / "global.json").exists() and (b / "global.json").exists()
    paths.set_state_dir(a)
    assert smt_state.load_global()["all_time_high"] == 1.0
    paths.set_state_dir(b)
    assert smt_state.load_global()["all_time_high"] == 2.0


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


# ── Cross-session ATH continuity ───────────────────────────────────────────────

def test_seed_global_from_prior_carries_ath_forward(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    prior = paths.sessions_dir() / "2026-06-01"
    prior.mkdir(parents=True, exist_ok=True)
    (prior / "global.json").write_text(
        json.dumps({"all_time_high": 25000.0, "confidence": "high", "trend": "up"}),
        encoding="utf-8",
    )
    cur = paths.sessions_dir() / "2026-06-02"
    paths.set_state_dir(cur)
    smt_state.seed_global_from_prior()
    assert smt_state.load_global()["all_time_high"] == 25000.0


def test_seed_global_from_prior_missing_prior_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    paths.set_state_dir(paths.sessions_dir() / "2026-06-02")
    smt_state.seed_global_from_prior()  # no prior session exists → must not raise
    assert smt_state.load_global()["all_time_high"] == 0.0


def test_seed_global_from_prior_noop_in_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    prior = paths.sessions_dir() / "2026-06-01"
    prior.mkdir(parents=True, exist_ok=True)
    (prior / "global.json").write_text(
        json.dumps({"all_time_high": 25000.0}), encoding="utf-8"
    )
    smt_state.set_in_memory_mode(True)
    paths.set_state_dir(paths.sessions_dir() / "2026-06-02")
    smt_state.seed_global_from_prior()  # in-memory (backtest) → no-op
    assert smt_state.load_global()["all_time_high"] == 0.0
