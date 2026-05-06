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

_LOAD_SAVE_PAIRS = [
    (load_global,     save_global,     DEFAULT_GLOBAL,     smt_state.GLOBAL_PATH),
    (load_daily,      save_daily,      DEFAULT_DAILY,      smt_state.DAILY_PATH),
    (load_hypothesis, save_hypothesis, DEFAULT_HYPOTHESIS, smt_state.HYPOTHESIS_PATH),
    (load_position,   save_position,   DEFAULT_POSITION,   smt_state.POSITION_PATH),
]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect all four state paths into a fresh tmp_path for each test."""
    monkeypatch.setattr(smt_state, "DATA_DIR",        tmp_path)
    monkeypatch.setattr(smt_state, "GLOBAL_PATH",     tmp_path / "global.json")
    monkeypatch.setattr(smt_state, "DAILY_PATH",      tmp_path / "daily.json")
    monkeypatch.setattr(smt_state, "HYPOTHESIS_PATH", tmp_path / "hypothesis.json")
    monkeypatch.setattr(smt_state, "POSITION_PATH",   tmp_path / "position.json")


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
        bad_path = tmp_path / path_attr.name
        bad_path.write_text(json.dumps({"foo": 1}), encoding="utf-8")
        result = load_fn()
        assert result == default

    @pytest.mark.parametrize("load_fn,save_fn,default,path_attr", _LOAD_SAVE_PAIRS)
    def test_bad_file_left_on_disk(self, load_fn, save_fn, default, path_attr, tmp_path):
        bad_path = tmp_path / path_attr.name
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
            "liquidities": [{"name": "TDO", "kind": "level", "price": 21412.5}],
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
            "limit_entry": 21395.0,
            "confirmation_bar": {"high": 21402.0, "low": 21390.0},
            "failed_entries": 1,
        }
        save_position(data)
        assert load_position() == data


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
        assert not (tmp_path / "global.json").exists()


class TestSaveUsesSortKeysForDeterminism:
    def test_byte_identical_regardless_of_dict_insertion_order(self, tmp_path):
        d_a = {"trend": "up", "all_time_high": 21500.0}
        d_b = {"all_time_high": 21500.0, "trend": "up"}

        save_global(d_a)
        bytes_a = (tmp_path / "global.json").read_bytes()

        save_global(d_b)
        bytes_b = (tmp_path / "global.json").read_bytes()

        assert bytes_a == bytes_b
