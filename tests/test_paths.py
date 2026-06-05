# tests/test_paths.py
# Unit tests for paths.py — the central env-overridable path resolver.
# Covers defaults, env overrides, dir auto-creation, TH run-folder naming, and the
# settable state-dir prefix. Migration-script move logic is co-located here in Wave 5.

import datetime
import importlib
from zoneinfo import ZoneInfo

import pytest

import paths


@pytest.fixture(autouse=True)
def _reset_state_dir():
    """Each test starts with the legacy default state dir and a clean reimport so a
    set_state_dir in one test never leaks into another."""
    importlib.reload(paths)
    yield


# ── Env overrides + auto-creation ──────────────────────────────────────────────

def test_global_root_override_creates_dir(tmp_path, monkeypatch):
    target = tmp_path / "global"
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(target))
    assert paths.global_root() == target
    assert target.is_dir()


def test_global_children_under_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path / "g"))
    assert paths.general_live_dir() == tmp_path / "g" / "general" / "live"
    assert paths.general_main_dir() == tmp_path / "g" / "general" / "main"
    assert paths.sessions_dir() == tmp_path / "g" / "sessions"
    for p in (paths.general_live_dir(), paths.general_main_dir(), paths.sessions_dir()):
        assert p.is_dir()


def test_global_root_default_when_no_env(monkeypatch):
    from pathlib import Path
    monkeypatch.delenv("ACT_GLOBAL_DIR", raising=False)
    assert paths.global_root() == Path("~/projects/auto-co-trader/global").expanduser()


def test_regression_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_REGRESSION_DIR", str(tmp_path / "reg"))
    assert paths.regression_dir() == tmp_path / "reg"
    assert (tmp_path / "reg").is_dir()


def test_regression_dir_default_is_cwd_regression(tmp_path, monkeypatch):
    monkeypatch.delenv("ACT_REGRESSION_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert paths.regression_dir() == tmp_path / "regression"


# ── regression_run_dir TH naming ───────────────────────────────────────────────

def test_regression_sessions_dir_under_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_REGRESSION_DIR", str(tmp_path / "reg"))
    assert paths.regression_sessions_dir() == tmp_path / "reg" / "sessions"
    assert paths.regression_sessions_dir().is_dir()


def test_run_dir_th_naming_from_et(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_REGRESSION_DIR", str(tmp_path / "reg"))
    # 2026-06-02 10:00:00 ET (EDT, UTC-4) == 21:00:00 TH (UTC+7).
    started = datetime.datetime(2026, 6, 2, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    run = paths.regression_run_dir("2026-06-02", started)
    # Per-date run folders live under <regression>/sessions/<date>/<HH-MM-SS>.
    assert run == tmp_path / "reg" / "sessions" / "2026-06-02" / "21-00-00"
    assert run.is_dir()


def test_run_dir_naive_treated_as_et(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_REGRESSION_DIR", str(tmp_path / "reg"))
    started = datetime.datetime(2026, 6, 2, 10, 0, 0)  # naive -> interpreted as ET
    run = paths.regression_run_dir("2026-06-02", started)
    assert run.name == "21-00-00"


def test_run_dir_th_date_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_REGRESSION_DIR", str(tmp_path / "reg"))
    # 2026-06-02 20:00 ET -> 07:00 TH next calendar day; the folder DATE is the passed
    # arg (caller-controlled), only the HH-MM-SS stamp reflects TH.
    started = datetime.datetime(2026, 6, 2, 20, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    run = paths.regression_run_dir("2026-06-02", started)
    assert run.name == "07-00-00"
    assert run.parent.name == "2026-06-02"


# ── state-dir prefix ───────────────────────────────────────────────────────────

def test_state_dir_default_is_legacy_data():
    from pathlib import Path
    assert paths.state_dir() == Path("data")


def test_set_state_dir_round_trip(tmp_path):
    paths.set_state_dir(tmp_path / "run")
    assert paths.state_dir() == tmp_path / "run"
    assert (tmp_path / "run").is_dir()
