# tests/test_live_orders.py
# Unit tests for the unified live_orders.py API.
# Each test redirects session output to tmp_path and mocks the executor.

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
# Test 1: place_stop_entry logs and syncs position.json
# ---------------------------------------------------------------------------

def test_place_stop_entry_logs_and_syncs(_in_tmp, _mock_today):
    empty_pos = {"active": {}, "stop_entry": "", "stop_direction": "",
                 "confirmation_bar": {}, "failed_entries": 0}
    mock_executor = MagicMock()
    saved: dict = {}
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=empty_pos), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)):
        live_orders.place_stop_entry("long", 19850.0, 19820.0)

    # Executor called with STP signal (has stop_fill_bars=1)
    mock_executor.place_entry.assert_called_once()
    pmt_signal = mock_executor.place_entry.call_args.args[0]
    assert pmt_signal["direction"] == "long"
    assert pmt_signal["entry_price"] == pytest.approx(19850.0)
    assert pmt_signal["stop_price"] == pytest.approx(19820.0)
    assert pmt_signal["stop_fill_bars"] == 1

    # position.json updated
    assert saved["stop_entry"] == "19850.0"
    assert saved["stop_direction"] == "up"

    # Event logged
    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "new-stop-entry"
    assert events[0]["direction"] == "long"


# ---------------------------------------------------------------------------
# Test 2: place_market_entry logs and syncs position.json
# ---------------------------------------------------------------------------

def test_place_market_entry_logs_and_syncs(_in_tmp, _mock_today):
    empty_pos = {"active": {}, "stop_entry": "", "stop_direction": "",
                 "confirmation_bar": {}, "failed_entries": 0}
    mock_executor = MagicMock()
    saved: dict = {}
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=empty_pos), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)):
        live_orders.place_market_entry("short", 19950.0, 19980.0)

    mock_executor.place_entry.assert_called_once()
    pmt_signal = mock_executor.place_entry.call_args.args[0]
    assert pmt_signal["direction"] == "short"
    assert pmt_signal["entry_price"] == pytest.approx(19950.0)
    assert pmt_signal["stop_price"] == pytest.approx(19980.0)
    # Market entry → no stop_fill_bars (instant fill at market)
    assert "stop_fill_bars" not in pmt_signal

    # position.json reflects active
    assert saved["active"]["direction"] == "short"
    assert saved["active"]["fill_price"] == pytest.approx(19950.0)
    assert saved["active"]["stop"] == pytest.approx(19980.0)
    assert saved["active"]["contracts"] == 2
    assert saved["active"]["cautious"] == "no"
    assert saved["stop_entry"] == ""
    assert saved["stop_direction"] == ""

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "market-entry"


# ---------------------------------------------------------------------------
# Test 3: move_stop_entry reads old entry from position.json
# ---------------------------------------------------------------------------

def test_move_stop_entry_reads_old_from_position(_in_tmp, _mock_today):
    pos = {"active": {}, "stop_entry": "19850.0", "stop_direction": "up",
           "confirmation_bar": {}, "failed_entries": 0}
    mock_executor = MagicMock()
    saved: dict = {}
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=pos), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)):
        live_orders.move_stop_entry(19900.0, 19870.0, "long")

    # modify_stop_entry receives both old + new pmt signals
    mock_executor.modify_stop_entry.assert_called_once()
    old_pmt, new_pmt, _ = mock_executor.modify_stop_entry.call_args.args
    assert old_pmt["entry_price"] == pytest.approx(19850.0)
    assert new_pmt["entry_price"] == pytest.approx(19900.0)
    assert new_pmt["stop_price"] == pytest.approx(19870.0)
    assert new_pmt["stop_fill_bars"] == 1

    assert saved["stop_entry"] == "19900.0"

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "move-stop-entry"


# ---------------------------------------------------------------------------
# Test 4: stop_entry_filled sends S/L and updates active.stop
# ---------------------------------------------------------------------------

def test_stop_entry_filled_sends_sl_and_updates_stop(_in_tmp, _mock_today):
    pos = {
        "active": {"direction": "long", "fill_price": 19850.0, "stop": 0.0,
                   "contracts": 2, "cautious": "no"},
        "stop_entry": "", "stop_direction": "", "confirmation_bar": {},
        "failed_entries": 0,
    }
    mock_executor = MagicMock()
    saved: dict = {}
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=pos), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)):
        live_orders.stop_entry_filled("long", 19820.0)

    mock_executor.update_stop_loss.assert_called_once()
    sl_signal = mock_executor.update_stop_loss.call_args.args[0]
    assert sl_signal["direction"] == "long"
    assert sl_signal["stop_price"] == pytest.approx(19820.0)

    # active.stop updated to the new stop_price
    assert saved["active"]["stop"] == pytest.approx(19820.0)

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "stop-entry-filled"


# ---------------------------------------------------------------------------
# Test 4b: stop_entry_filled fires executor but skips save when active is absent
# ---------------------------------------------------------------------------

def test_stop_entry_filled_noop_save_when_no_active(_in_tmp, _mock_today):
    pos = {"active": {}, "stop_entry": "19850.0", "stop_direction": "up",
           "confirmation_bar": {}, "failed_entries": 0}
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=pos), \
         patch("smt_state.save_position") as mock_save:
        live_orders.stop_entry_filled("long", 19820.0)

    # Executor must still fire (broker needs the stop order)
    mock_executor.update_stop_loss.assert_called_once()
    # position.json must NOT be updated (no active to patch)
    mock_save.assert_not_called()
    # Event is still logged
    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "stop-entry-filled"


# ---------------------------------------------------------------------------
# Test 5: cancel_stop_entry is a no-op when stop_entry is empty
# ---------------------------------------------------------------------------

def test_cancel_stop_entry_noop_when_empty(_in_tmp, _mock_today):
    empty_pos = {"active": {}, "stop_entry": "", "stop_direction": "",
                 "confirmation_bar": {}, "failed_entries": 0}
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=empty_pos), \
         patch("smt_state.save_position") as mock_save:
        live_orders.cancel_stop_entry()

    # No executor call, no save, no events
    mock_executor.place_close.assert_not_called()
    mock_save.assert_not_called()
    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert events == []


# ---------------------------------------------------------------------------
# Test 6: cancel_stop_entry clears position when stop_entry is set
# ---------------------------------------------------------------------------

def test_cancel_stop_entry_clears_position(_in_tmp, _mock_today):
    pos = {
        "active": {},
        "stop_entry": "19900.0",
        "stop_direction": "up",
        "confirmation_bar": {"open": 19890.0},
        "failed_entries": 0,
    }
    mock_executor = MagicMock()
    saved: dict = {}
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=pos), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)):
        live_orders.cancel_stop_entry("user-requested")

    mock_executor.place_close.assert_called_once_with("cancel-stop")
    assert saved["stop_entry"] == ""
    assert saved["stop_direction"] == ""
    assert saved["confirmation_bar"] == {}

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "cancel-stop-entry"
    assert events[0]["entry_price"] == pytest.approx(19900.0)


# ---------------------------------------------------------------------------
# Test 7: close_position clears active and other fields
# ---------------------------------------------------------------------------

def test_close_position_clears_active(_in_tmp, _mock_today):
    pos = {
        "active": {"direction": "long", "fill_price": 19850.0, "stop": 19820.0,
                   "contracts": 2, "cautious": "no"},
        "stop_entry": "19850.0",
        "stop_direction": "up",
        "confirmation_bar": {"open": 19840.0},
        "failed_entries": 1,
    }
    mock_executor = MagicMock()
    saved: dict = {}
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=pos), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)):
        live_orders.close_position(19855.0, "test")

    mock_executor.place_close.assert_called_once_with("close")
    assert saved["active"] == {}
    assert saved["stop_entry"] == ""
    assert saved["stop_direction"] == ""
    assert saved["confirmation_bar"] == {}

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "market-close"
    assert events[0]["price"] == pytest.approx(19855.0)
    assert events[0]["reason"] == "test"


# ---------------------------------------------------------------------------
# Test 8: update_stop_loss dispatches and updates active.stop
# ---------------------------------------------------------------------------

def test_update_stop_loss_dispatches_update_sl(_in_tmp, _mock_today):
    pos = {
        "active": {"direction": "long", "fill_price": 19850.0, "stop": 19820.0,
                   "contracts": 2, "cautious": "no"},
        "stop_entry": "", "stop_direction": "", "confirmation_bar": {},
        "failed_entries": 0,
    }
    mock_executor = MagicMock()
    saved: dict = {}
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=pos), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)):
        live_orders.update_stop_loss(19835.0, "user-requested")

    mock_executor.update_stop_loss.assert_called_once()
    sl_signal = mock_executor.update_stop_loss.call_args.args[0]
    assert sl_signal["direction"] == "long"
    assert sl_signal["stop_price"] == pytest.approx(19835.0)

    assert saved["active"]["stop"] == pytest.approx(19835.0)

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "update-stop-loss"
    assert events[0]["stop_price"] == pytest.approx(19835.0)


# ---------------------------------------------------------------------------
# Test 9: _log appends, not overwrites
# ---------------------------------------------------------------------------

def test_log_appends_not_overwrites(_in_tmp, _mock_today):
    live_orders._log({"kind": "a"})
    live_orders._log({"kind": "b"})

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 2
    assert events[0]["kind"] == "a"
    assert events[1]["kind"] == "b"


# ---------------------------------------------------------------------------
# Test 10: has_active_position returns True/False
# ---------------------------------------------------------------------------

def test_has_active_position_true_false():
    with patch("smt_state.load_position", return_value={"active": {"direction": "long"}}):
        assert live_orders.has_active_position() is True
    with patch("smt_state.load_position", return_value={"active": {}}):
        assert live_orders.has_active_position() is False


# ---------------------------------------------------------------------------
# Test 11: has_pending_entry returns True/False
# ---------------------------------------------------------------------------

def test_has_pending_entry_true_false():
    with patch("smt_state.load_position", return_value={"stop_entry": "19900.0"}):
        assert live_orders.has_pending_entry() is True
    with patch("smt_state.load_position", return_value={"stop_entry": ""}):
        assert live_orders.has_pending_entry() is False


# ---------------------------------------------------------------------------
# Test 12: get_position delegates to smt_state
# ---------------------------------------------------------------------------

def test_get_position_delegates():
    pos = {"active": {"direction": "long"}, "stop_entry": "", "stop_direction": ""}
    with patch("smt_state.load_position", return_value=pos):
        assert live_orders.get_position() == pos
