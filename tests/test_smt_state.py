# tests/test_smt_state.py
# Unit tests for smt_state.py: defaults, round-trips, atomic write, determinism.

import copy
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import smt_state
from smt_state import (
    DEFAULT_DAILY,
    DEFAULT_GLOBAL,
    DEFAULT_HYPOTHESIS,
    DEFAULT_POSITION,
    load_daily,
    load_global,
    load_hypothesis,
    load_position,
    save_daily,
    save_global,
    save_hypothesis,
    save_position,
)

# 4th element is the bare filename; the state dir is redirected to tmp_path by _isolate,
# so the four JSONs resolve to tmp_path/<filename>.
_LOAD_SAVE_PAIRS = [
    (load_global,     save_global,     DEFAULT_GLOBAL,     "global.json"),
    (load_daily,      save_daily,      DEFAULT_DAILY,      "daily.json"),
    (load_hypothesis, save_hypothesis, DEFAULT_HYPOTHESIS, "hypothesis.json"),
    (load_position,   save_position,   DEFAULT_POSITION,   "position.json"),
]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect the state-dir prefix into a fresh tmp_path for each test, and isolate the
    global root so global.json (which lives in general_live_dir() in live/disk mode) never
    touches the real machine-global folder."""
    import paths
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path / "_global"))
    # Reset hypothesis cache and in-memory flag so tests don't bleed state into each other
    monkeypatch.setattr(smt_state, "_hyp_cache",       None)
    monkeypatch.setattr(smt_state, "_hyp_cache_valid", False)
    monkeypatch.setattr(smt_state, "_IN_MEMORY",       False)


def _state_path(filename):
    """Where a given state file lands under the test's isolated dirs: global.json lives in
    general_live_dir() (live/disk mode); daily/hypothesis/position under state_dir()."""
    import paths
    base = paths.general_live_dir() if filename == "global.json" else paths.state_dir()
    return base / filename


class TestLoadReturnsDefaultWhenMissing:
    @pytest.mark.parametrize("load_fn,_save,default,_path", _LOAD_SAVE_PAIRS)
    def test_returns_default(self, load_fn, _save, default, _path):
        result = load_fn()
        assert result == default

    @pytest.mark.parametrize("load_fn,_save,default,_path", _LOAD_SAVE_PAIRS)
    def test_returns_deep_copy(self, load_fn, _save, default, _path):
        result = load_fn()
        result["__mutated__"] = True
        assert "__mutated__" not in load_fn()


class TestLoadReturnsDefaultWhenSchemaMismatch:
    @pytest.mark.parametrize("load_fn,save_fn,default,path_attr", _LOAD_SAVE_PAIRS)
    def test_bad_file_returns_default(self, load_fn, save_fn, default, path_attr, tmp_path):
        # Write a file with only an unrecognized key (missing all required keys)
        bad_path = _state_path(path_attr)
        bad_path.write_text(json.dumps({"foo": 1}), encoding="utf-8")
        result = load_fn()
        assert result == default

    @pytest.mark.parametrize("load_fn,save_fn,default,path_attr", _LOAD_SAVE_PAIRS)
    def test_bad_file_left_on_disk(self, load_fn, save_fn, default, path_attr, tmp_path):
        bad_path = _state_path(path_attr)
        bad_path.write_text(json.dumps({"foo": 1}), encoding="utf-8")
        load_fn()
        # Original bad file must still be present (load does not overwrite)
        assert bad_path.exists()
        assert json.loads(bad_path.read_text()) == {"foo": 1}


class TestSaveThenLoadRoundtrip:
    def test_global_roundtrip(self):
        data = {"all_time_high": 21500.0, "confidence": "medium", "trend": "down"}
        save_global(data)
        assert load_global() == data

    def test_daily_roundtrip(self):
        data = {
            "date": "2026-04-27",
            "formed_at": "2026-04-27T09:30:00",
            "liquidities": [{"name": "TDO", "kind": "level", "price": 21412.5}],
            "liquidities_mes": [{"name": "TDO", "kind": "level", "price": 2412.5}],
            "estimated_dir": "down",
            "opposite_premove": "yes",
        }
        save_daily(data)
        assert load_daily() == data

    def test_hypothesis_roundtrip(self):
        data = {
            "direction": "up",
            "weekly_mid": "above",
            "daily_mid": "mid",
            "last_liquidity": "day_low",
            "divs": [{"type": "wick"}],
            "targets": [{"name": "day_high", "price": 21425.0}],
            "cautious_price": "",
            "entry_ranges": [{"source": "12hr", "low": 100.0, "high": 110.0}],
        }
        save_hypothesis(data)
        assert load_hypothesis() == data

    def test_position_roundtrip(self):
        data = {
            "active": {"fill_price": 21400.0, "direction": "up"},
            "stop_entry": 21395.0,
            "stop_direction": "long",
            "conf_bar_entry": {"high": 21402.0, "low": 21390.0},
            "conf_bar_exit":  {},
            "pending_stop": 21380.0,
            "failed_entries": 1,
            "cautious_dist_shrinks": 1,
            "session_mid_crosses": 0,
        }
        save_position(data)
        assert load_position() == data

    def test_smts_roundtrip(self):
        from smt_state import DEFAULT_SMTS, load_smts, save_smts
        data = {
            "detect_state": {"day_high|short": {"armed": False, "last_cond": True,
                                                "fire_price": 21500.0, "level_price": 21500.0}},
            "watch": {"retained": [{"kind": "smt", "type": "wick", "direction": "short"}]},
        }
        save_smts(data)
        assert load_smts() == data
        # Missing file → default.
        assert load_smts.__module__  # sanity
        assert DEFAULT_SMTS == {"detect_state": {}, "watch": {"retained": []}}


class TestSmtsInMemory:
    def test_smts_inmemory(self, monkeypatch):
        """In-memory (backtest) mode: save_smts/load_smts use the _STORE dict."""
        from smt_state import load_smts, save_smts
        monkeypatch.setattr(smt_state, "_IN_MEMORY", True)
        smt_state._STORE.clear()
        data = {"detect_state": {"week_low|long": {"armed": True}},
                "watch": {"retained": []}}
        save_smts(data)
        assert load_smts() == data
        # No file should have been written in in-memory mode.
        import paths
        assert not (paths.state_dir() / "smts.json").exists()


class TestSaveIsAtomic:
    def test_crash_in_os_replace_preserves_original(self, tmp_path):
        original = {"all_time_high": 100.0, "confidence": "high", "trend": "up"}
        save_global(original)

        with patch("os.replace", side_effect=OSError("simulated crash")):
            with pytest.raises(OSError):
                save_global({"all_time_high": 999.0, "trend": "down"})

        # Original must still be intact
        assert load_global() == original

    def test_crash_when_no_prior_file_leaves_no_corruption(self, tmp_path):
        # No file exists yet; crash in os.replace should leave nothing (or the .tmp)
        with patch("os.replace", side_effect=OSError("simulated crash")):
            with pytest.raises(OSError):
                save_global({"all_time_high": 999.0, "trend": "down"})

        # Main file must not exist (or if .tmp exists, that's acceptable)
        assert not _state_path("global.json").exists()


class TestSaveUsesSortKeysForDeterminism:
    def test_byte_identical_regardless_of_dict_insertion_order(self, tmp_path):
        d_a = {"trend": "up", "all_time_high": 21500.0}
        d_b = {"all_time_high": 21500.0, "trend": "up"}

        save_global(d_a)
        bytes_a = _state_path("global.json").read_bytes()

        save_global(d_b)
        bytes_b = _state_path("global.json").read_bytes()

        assert bytes_a == bytes_b


_HYP = {
    "direction": "up",
    "weekly_mid": "above",
    "daily_mid": "mid",
    "last_liquidity": "day_low",
    "divs": [],
    "targets": [],
    "cautious_price": "",
    "entry_ranges": [],
}


class TestHypothesisCache:
    def test_load_hypothesis_returns_cached_value(self):
        save_hypothesis(_HYP)
        with patch.object(smt_state, "_load", side_effect=AssertionError("should use cache")):
            result = load_hypothesis()
        assert result["direction"] == "up"

    def test_cache_invalidated_after_in_memory_toggle(self):
        save_hypothesis({**_HYP, "direction": "down", "weekly_mid": "below", "last_liquidity": "day_high"})
        assert smt_state._hyp_cache_valid is True
        smt_state.set_in_memory_mode(True)
        smt_state.set_in_memory_mode(False)
        assert smt_state._hyp_cache_valid is False

    def test_cache_not_used_in_in_memory_mode(self):
        smt_state.set_in_memory_mode(True)
        save_hypothesis(_HYP)
        assert smt_state._hyp_cache_valid is False
        load_hypothesis()
        assert smt_state._hyp_cache_valid is False

    def test_position_not_cached(self):
        load_position()
        assert smt_state._hyp_cache_valid is False
        assert smt_state._hyp_cache is None
        load_position()
        assert smt_state._hyp_cache_valid is False
        assert smt_state._hyp_cache is None


# ---------------------------------------------------------------------------
# is_paused: manual entry-pause sentinel (resolved in general_live_dir(); inert in backtest)
# ---------------------------------------------------------------------------

def _make_pause_sentinel(tmp_path, monkeypatch):
    """Point the global root at tmp and return the sentinel path (where
    smt_state.pause_path() resolves: general_live_dir()/paused) so a test can
    create/inspect it. The pause flag is no longer per-session."""
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    return smt_state.pause_path()


def test_is_paused_reflects_sentinel(tmp_path, monkeypatch):
    flag = _make_pause_sentinel(tmp_path, monkeypatch)
    monkeypatch.setattr(smt_state, "_IN_MEMORY", False)
    assert smt_state.is_paused() is False
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("x")
    assert smt_state.is_paused() is True


def test_is_paused_false_in_memory_mode(tmp_path, monkeypatch):
    """Pause must never affect backtests (in-memory mode), even if a sentinel exists on disk."""
    flag = _make_pause_sentinel(tmp_path, monkeypatch)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("x")
    monkeypatch.setattr(smt_state, "_IN_MEMORY", True)
    assert smt_state.is_paused() is False
    monkeypatch.setattr(smt_state, "_IN_MEMORY", False)
    assert smt_state.is_paused() is True
