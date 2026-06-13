# tests/test_bar_state.py
# Unit tests for bar_state.json helpers in smt_state.py and the per-1m write
# in SessionPipeline._write_bar_state.

from __future__ import annotations

import pandas as pd
import pytest

import smt_state
from session_pipeline import SessionPipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point the global dir (and cwd) at tmp_path so sessions/{date}/bar_state.json —
    now resolved via paths.sessions_dir() — lands inside the test dir."""
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield tmp_path


# ---------------------------------------------------------------------------
# Test 1: save_bar_state + load_bar_state roundtrip
# ---------------------------------------------------------------------------

def test_save_and_load_bar_state_roundtrip(_isolate):
    payload = {
        "time": "2026-04-27T10:05:00+00:00",
        "potential_stop_long": 19820.5,
        "potential_stop_short": 19880.25,
    }
    smt_state.save_bar_state(payload, date_str="2026-04-27")
    loaded = smt_state.load_bar_state(date_str="2026-04-27")
    assert loaded == payload


# ---------------------------------------------------------------------------
# Test 2: potential_stop_long formula — body_low - 15 wins (wick farther away)
# ---------------------------------------------------------------------------

def test_bar_state_potential_stop_long_formula(_isolate):
    """body_low=100, bar_low=80 → potential_stop_long = max(80, 100-15) = 85."""
    now = pd.Timestamp("2026-04-27 10:05:00", tz="UTC")
    # Build 5 1m bars in [10:00, 10:05) window with body_low=100, bar_low=80
    idx = pd.date_range("2026-04-27 10:00:00", periods=5, freq="1min", tz="UTC")
    today_mnq = pd.DataFrame(
        {
            "Open":  [100.0, 102.0, 105.0, 108.0, 110.0],
            "High":  [105.0, 107.0, 110.0, 112.0, 115.0],
            "Low":   [ 80.0,  95.0,  98.0, 105.0, 108.0],
            "Close": [102.0, 105.0, 108.0, 110.0, 110.0],  # last close=110 → body_low=min(100,110)=100
        },
        index=idx,
    )
    sp = SessionPipeline(pd.DataFrame(), pd.DataFrame(), emit_fn=lambda _: None)
    sp._write_bar_state(now, today_mnq)

    state = smt_state.load_bar_state()
    assert state is not None
    assert state["potential_stop_long"] == pytest.approx(85.0)


# ---------------------------------------------------------------------------
# Test 3: potential_stop_short formula — body_high + 15 wins (wick farther away)
# ---------------------------------------------------------------------------

def test_bar_state_potential_stop_short_formula(_isolate):
    """body_high=100, bar_high=120 → potential_stop_short = min(120, 100+15) = 115."""
    now = pd.Timestamp("2026-04-27 10:05:00", tz="UTC")
    idx = pd.date_range("2026-04-27 10:00:00", periods=5, freq="1min", tz="UTC")
    # First open=100, last close=90 → body_high=max(100,90)=100, bar_high=120
    today_mnq = pd.DataFrame(
        {
            "Open":  [100.0, 98.0, 96.0, 93.0, 92.0],
            "High":  [105.0, 110.0, 115.0, 120.0, 118.0],
            "Low":   [ 90.0,  88.0,  87.0,  85.0,  85.0],
            "Close": [ 98.0,  96.0,  93.0,  92.0,  90.0],
        },
        index=idx,
    )
    sp = SessionPipeline(pd.DataFrame(), pd.DataFrame(), emit_fn=lambda _: None)
    sp._write_bar_state(now, today_mnq)

    state = smt_state.load_bar_state()
    assert state is not None
    assert state["potential_stop_short"] == pytest.approx(115.0)


# ---------------------------------------------------------------------------
# Test 4: wick-cap binds for long — bar_low close to body_low → bar_low wins
# ---------------------------------------------------------------------------

def test_bar_state_wick_cap_binds_for_long(_isolate):
    """bar_low=98 (close to body_low=100) → potential_stop_long = max(98, 85) = 98."""
    now = pd.Timestamp("2026-04-27 10:05:00", tz="UTC")
    idx = pd.date_range("2026-04-27 10:00:00", periods=5, freq="1min", tz="UTC")
    # First open=100, last close=110 → body_low=min(100,110)=100, bar_low=98
    today_mnq = pd.DataFrame(
        {
            "Open":  [100.0, 102.0, 104.0, 106.0, 108.0],
            "High":  [105.0, 107.0, 109.0, 111.0, 113.0],
            "Low":   [ 98.0,  99.0, 100.0, 102.0, 105.0],
            "Close": [102.0, 104.0, 106.0, 108.0, 110.0],
        },
        index=idx,
    )
    sp = SessionPipeline(pd.DataFrame(), pd.DataFrame(), emit_fn=lambda _: None)
    sp._write_bar_state(now, today_mnq)

    state = smt_state.load_bar_state()
    assert state is not None
    assert state["potential_stop_long"] == pytest.approx(98.0)


# ---------------------------------------------------------------------------
# Test 5: nulls when no bars in the window
# ---------------------------------------------------------------------------

def test_bar_state_nulls_when_no_window(_isolate):
    """Empty today_mnq → potential_stop_long=None, potential_stop_short=None."""
    now = pd.Timestamp("2026-04-27 10:05:00", tz="UTC")
    today_mnq = pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    today_mnq.index = pd.DatetimeIndex([], tz="UTC")

    sp = SessionPipeline(pd.DataFrame(), pd.DataFrame(), emit_fn=lambda _: None)
    sp._write_bar_state(now, today_mnq)

    state = smt_state.load_bar_state()
    assert state is not None
    assert state["potential_stop_long"] is None
    assert state["potential_stop_short"] is None


# ---------------------------------------------------------------------------
# Test 6: bar_state written after on_1m_bar
# ---------------------------------------------------------------------------

def test_bar_state_written_after_1m_bar(_isolate, monkeypatch):
    """Calling _write_bar_state writes a file at sessions/{date}/bar_state.json."""
    now = pd.Timestamp("2026-04-27 10:05:00", tz="UTC")
    idx = pd.date_range("2026-04-27 10:00:00", periods=5, freq="1min", tz="UTC")
    today_mnq = pd.DataFrame(
        {
            "Open":  [100.0, 101.0, 102.0, 103.0, 104.0],
            "High":  [105.0, 106.0, 107.0, 108.0, 109.0],
            "Low":   [ 95.0,  96.0,  97.0,  98.0,  99.0],
            "Close": [101.0, 102.0, 103.0, 104.0, 105.0],
        },
        index=idx,
    )

    sp = SessionPipeline(pd.DataFrame(), pd.DataFrame(), emit_fn=lambda _: None)
    sp._write_bar_state(now, today_mnq)

    # Resolve the expected path via the same helper save_bar_state uses (ET session date),
    # not datetime.date.today() — the two diverge on Sundays / after the daily roll.
    path = smt_state.bar_state_path()
    assert path.exists(), f"bar_state.json should exist at {path}"


# ---------------------------------------------------------------------------
# Test 7: load_bar_state returns None when missing
# ---------------------------------------------------------------------------

def test_load_bar_state_returns_none_when_missing(_isolate):
    """No file → load_bar_state returns None."""
    result = smt_state.load_bar_state(date_str="2099-01-01")
    assert result is None
