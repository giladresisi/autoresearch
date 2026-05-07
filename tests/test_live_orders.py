# tests/test_live_orders.py
# Unit tests for live_orders.py.
# All tests redirect session output to tmp_path and mock the executor.

from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import live_orders


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_events(sessions_dir: Path, date: str) -> list[dict]:
    """Read all events from sessions/{date}/events.jsonl."""
    path = sessions_dir / date / "events.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture(autouse=True)
def _reset_pending_limit():
    """Reset _pending_limit to None before each test."""
    live_orders._pending_limit = None
    yield
    live_orders._pending_limit = None


@pytest.fixture()
def _in_tmp(tmp_path, monkeypatch):
    """chdir to tmp_path so Path('sessions/...') lands in tmp_path."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


_FIXED_DATE = "2026-01-15"


@pytest.fixture()
def _mock_today(monkeypatch):
    """Patch datetime.date.today() to return a fixed date."""
    fixed = datetime.date(2026, 1, 15)
    monkeypatch.setattr(
        "live_orders.datetime.date",
        type("_date", (), {"today": staticmethod(lambda: fixed)})(),
    )
    return _FIXED_DATE


# ---------------------------------------------------------------------------
# Test 1: manual_close writes events.jsonl
# ---------------------------------------------------------------------------

def test_manual_close_writes_events_jsonl(_in_tmp, _mock_today):
    mock_executor = MagicMock()
    empty_pos = {"active": {}, "limit_entry": "", "limit_direction": "", "confirmation_bar": {}}
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=empty_pos), \
         patch("smt_state.save_position"):
        live_orders.manual_close(19850.0, "test")

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    e = events[0]
    assert e["kind"] == "market-close"
    assert e["price"] == pytest.approx(19850.0)
    assert e["source"] == "manual"
    assert e["reason"] == "test"


# ---------------------------------------------------------------------------
# Test 2: manual_cancel_limit is a no-op when _pending_limit is None
# ---------------------------------------------------------------------------

def test_manual_cancel_limit_noop_when_no_pending(_in_tmp, _mock_today):
    assert live_orders._pending_limit is None  # ensure reset
    empty_pos = {"active": {}, "limit_entry": "", "limit_direction": "", "confirmation_bar": {}}
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=empty_pos):
        live_orders.manual_cancel_limit()

    # No events file written, no executor call
    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert events == []
    mock_executor.place_close.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: manual_cancel_limit logs and clears _pending_limit
# ---------------------------------------------------------------------------

def test_manual_cancel_limit_logs_and_clears(_in_tmp, _mock_today):
    live_orders._pending_limit = {"entry_price": 19900.0, "direction": "long"}
    empty_pos = {"active": {}, "limit_entry": "", "limit_direction": "", "confirmation_bar": {}}
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=empty_pos), \
         patch("smt_state.save_position"):
        live_orders.manual_cancel_limit()

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    e = events[0]
    assert e["kind"] == "cancel-limit-entry"
    assert e["source"] == "manual"
    assert live_orders._pending_limit is None


# ---------------------------------------------------------------------------
# Test 4: place_entry with limit_fill_bars sets _pending_limit
# ---------------------------------------------------------------------------

def test_place_entry_limit_sets_pending():
    signal = {"direction": "long", "entry_price": 19850.0,
               "stop_price": 19820.0, "limit_fill_bars": 1}
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor):
        live_orders.place_entry(signal)

    assert live_orders._pending_limit == signal
    mock_executor.place_entry.assert_called_once_with(signal, None)


# ---------------------------------------------------------------------------
# Test 5: place_entry without limit_fill_bars clears _pending_limit
# ---------------------------------------------------------------------------

def test_place_entry_market_clears_pending():
    live_orders._pending_limit = {"entry_price": 19900.0}
    signal = {"direction": "short", "entry_price": 19950.0, "stop_price": 19980.0}
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor):
        live_orders.place_entry(signal)

    assert live_orders._pending_limit is None


# ---------------------------------------------------------------------------
# Test 6: close clears _pending_limit and calls executor
# ---------------------------------------------------------------------------

def test_close_clears_pending():
    live_orders._pending_limit = {"entry_price": 19900.0}
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor):
        live_orders.close()

    assert live_orders._pending_limit is None
    mock_executor.place_close.assert_called_once()


# ---------------------------------------------------------------------------
# Test 7: _log appends, not overwrites
# ---------------------------------------------------------------------------

def test_log_appends_not_overwrites(_in_tmp, _mock_today):
    live_orders._log({"kind": "a"})
    live_orders._log({"kind": "b"})

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 2
    assert events[0]["kind"] == "a"
    assert events[1]["kind"] == "b"


# ---------------------------------------------------------------------------
# Test 8: manual_close clears position.json fields
# ---------------------------------------------------------------------------

def test_manual_close_clears_position_json(_in_tmp, _mock_today):
    pos = {
        "active": {"direction": "long", "fill_price": 19850.0, "stop": 19820.0},
        "limit_entry": "19850.0",
        "limit_direction": "long",
        "confirmation_bar": {"open": 19845.0},
        "failed_entries": 1,
    }
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=pos), \
         patch("smt_state.save_position") as mock_save:
        live_orders.manual_close(19850.0, "test")

    mock_save.assert_called_once()
    saved = mock_save.call_args.args[0]
    assert saved["active"] == {}
    assert saved["limit_entry"] == ""
    assert saved["limit_direction"] == ""
    assert saved["confirmation_bar"] == {}


# ---------------------------------------------------------------------------
# Test 9: manual_cancel_limit reads position.json limit_entry when _pending_limit is None
# ---------------------------------------------------------------------------

def test_manual_cancel_limit_uses_position_json_limit_entry(_in_tmp, _mock_today):
    """Cancel should fire from position.json limit_entry even if _pending_limit is None."""
    assert live_orders._pending_limit is None
    pos = {
        "active": {},
        "limit_entry": "19900.0",
        "limit_direction": "long",
        "confirmation_bar": {},
    }
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=pos), \
         patch("smt_state.save_position") as mock_save:
        live_orders.manual_cancel_limit()

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "cancel-limit-entry"
    assert events[0]["price"] == pytest.approx(19900.0)
    saved = mock_save.call_args.args[0]
    assert saved["limit_entry"] == ""
    assert saved["limit_direction"] == ""
    assert saved["confirmation_bar"] == {}


# ---------------------------------------------------------------------------
# Test 10: get_position delegates to smt_state
# ---------------------------------------------------------------------------

def test_get_position_returns_position():
    pos = {"active": {"direction": "long"}, "limit_entry": "", "limit_direction": ""}
    with patch("smt_state.load_position", return_value=pos):
        assert live_orders.get_position() == pos


# ---------------------------------------------------------------------------
# Test 11: has_active_position returns True/False
# ---------------------------------------------------------------------------

def test_has_active_position():
    with patch("smt_state.load_position", return_value={"active": {"direction": "long"}}):
        assert live_orders.has_active_position() is True
    with patch("smt_state.load_position", return_value={"active": {}}):
        assert live_orders.has_active_position() is False


# ---------------------------------------------------------------------------
# Test 12: has_pending_limit returns True/False
# ---------------------------------------------------------------------------

def test_has_pending_limit():
    with patch("smt_state.load_position", return_value={"limit_entry": "19900.0"}):
        assert live_orders.has_pending_limit() is True
    with patch("smt_state.load_position", return_value={"limit_entry": ""}):
        assert live_orders.has_pending_limit() is False
