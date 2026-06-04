# tests/test_live_orders.py
# Unit tests for the unified live_orders.py API.
# Each test redirects session output to tmp_path and mocks the executor.

from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
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
def _mock_today():
    """Fix the session date so events.jsonl lands in a predictable folder."""
    live_orders.set_session_date(_FIXED_DATE)
    yield _FIXED_DATE
    live_orders.set_session_date("")


# ---------------------------------------------------------------------------
# Test 1: place_stop_entry logs and syncs position.json
# ---------------------------------------------------------------------------

def test_place_stop_entry_logs_and_syncs(_in_tmp, _mock_today):
    empty_pos = {"active": {}, "stop_entry": "", "stop_direction": "",
                 "conf_bar_entry": {}, "failed_entries": 0}
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
    assert saved["pending_stop"] == pytest.approx(19820.0)

    # Event logged
    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "new-stop-entry"
    assert events[0]["direction"] == "long"


# ---------------------------------------------------------------------------
# Test 1b: STP->MKT downgrade fills immediately (records active, recomputes cautious)
# ---------------------------------------------------------------------------

def test_place_stop_entry_downgrade_fills_immediately(_in_tmp, _mock_today):
    """When the executor downgrades STP->MKT (entry within 5pts of market), the broker
    fills immediately, so place_stop_entry must record an active position right away —
    not a pending stop_entry — and re-anchor the cautious ladder to the fill price."""
    empty_pos = {"active": {}, "stop_entry": "", "stop_direction": "",
                 "conf_bar_entry": {}, "failed_entries": 0}
    mock_executor = MagicMock()
    mock_executor.place_entry.return_value = SimpleNamespace(order_type="market")
    mock_executor._entry_is_live = True
    saved: dict = {}
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=empty_pos), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)), \
         patch("smt_state.load_hypothesis", return_value={"direction": "up"}), \
         patch("smt_state.load_daily", return_value={"liquidities": []}), \
         patch("smt_state.load_global", return_value={"all_time_high": 21000.0}), \
         patch("smt_state.save_hypothesis") as mock_save_hyp, \
         patch("hypothesis.recompute_cautious_for_fill") as mock_recompute:
        live_orders.place_stop_entry("long", 19850.0, 19820.0)

    # Active position recorded immediately — no pending stop_entry left behind
    assert saved["active"]["direction"] == "long"
    assert saved["active"]["fill_price"] == pytest.approx(19850.0)
    assert saved["active"]["stop"] == pytest.approx(19820.0)
    assert saved["active"]["contracts"] == 2
    assert saved["active"]["cautious"] == "no"
    assert saved["active"]["source"] == "strategy"
    assert saved["stop_entry"] == ""
    assert saved["stop_direction"] == ""

    # Cautious ladder re-anchored to the fill price (Addendum 4)
    mock_recompute.assert_called_once()
    assert mock_recompute.call_args.args[1] == pytest.approx(19850.0)
    mock_save_hyp.assert_called_once()

    # stop-entry-filled is logged (NOT new-stop-entry)
    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "stop-entry-filled"
    assert events[0]["direction"] == "long"
    assert events[0]["price"] == pytest.approx(19850.0)
    assert events[0]["stop_price"] == pytest.approx(19820.0)


# ---------------------------------------------------------------------------
# Test 1c: a real resting STP (no downgrade) stays a pending stop_entry
# ---------------------------------------------------------------------------

def test_place_stop_entry_real_stop_stays_pending(_in_tmp, _mock_today):
    empty_pos = {"active": {}, "stop_entry": "", "stop_direction": "",
                 "conf_bar_entry": {}, "failed_entries": 0}
    mock_executor = MagicMock()
    mock_executor.place_entry.return_value = SimpleNamespace(order_type="stop")
    mock_executor._entry_is_live = True
    saved: dict = {}
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=empty_pos), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)):
        live_orders.place_stop_entry("long", 19850.0, 19820.0)

    # Pending stop_entry written; no active position
    assert saved["stop_entry"] == "19850.0"
    assert saved["stop_direction"] == "up"
    assert saved["pending_stop"] == pytest.approx(19820.0)
    assert saved["active"] == {}

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "new-stop-entry"


# ---------------------------------------------------------------------------
# Test 1d: downgrade while the entry window is blocked → no immediate fill
# ---------------------------------------------------------------------------

def test_place_stop_entry_downgrade_blocked_no_fill(_in_tmp, _mock_today):
    """If the entry window gate blocked the order (_entry_is_live False), nothing was
    sent to the broker — so we must NOT record an active fill even though the order
    would have downgraded. It stays a pending (unplaced) stop_entry."""
    empty_pos = {"active": {}, "stop_entry": "", "stop_direction": "",
                 "conf_bar_entry": {}, "failed_entries": 0}
    mock_executor = MagicMock()
    mock_executor.place_entry.return_value = SimpleNamespace(order_type="market")
    mock_executor._entry_is_live = False
    saved: dict = {}
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=empty_pos), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)):
        live_orders.place_stop_entry("long", 19850.0, 19820.0)

    assert saved["active"] == {}
    assert saved["stop_entry"] == "19850.0"
    assert saved["stop_entry_unplaced"] is True

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "new-stop-entry"


# ---------------------------------------------------------------------------
# Test 2: place_market_entry logs and syncs position.json
# ---------------------------------------------------------------------------

def test_place_market_entry_logs_and_syncs(_in_tmp, _mock_today):
    empty_pos = {"active": {}, "stop_entry": "", "stop_direction": "",
                 "conf_bar_entry": {}, "failed_entries": 0}
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
           "conf_bar_entry": {}, "failed_entries": 0}
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

def test_stop_entry_filled_updates_stop_only(_in_tmp, _mock_today):
    pos = {
        "active": {"direction": "long", "fill_price": 19850.0, "stop": 0.0,
                   "contracts": 2, "cautious": "no"},
        "stop_entry": "", "stop_direction": "", "conf_bar_entry": {},
        "failed_entries": 0,
    }
    mock_executor = MagicMock()
    saved: dict = {}
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=pos), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)):
        live_orders.stop_entry_filled("long", 19820.0)

    # Real S/L was embedded in the STP order at placement — no update_stop_loss needed
    mock_executor.update_stop_loss.assert_not_called()

    # active.stop is still updated in position.json
    assert saved["active"]["stop"] == pytest.approx(19820.0)

    events = _read_events(_in_tmp / "sessions", _FIXED_DATE)
    assert len(events) == 1
    assert events[0]["kind"] == "stop-entry-filled"


# ---------------------------------------------------------------------------
# Test 4b: stop_entry_filled fires executor but skips save when active is absent
# ---------------------------------------------------------------------------

def test_stop_entry_filled_noop_save_when_no_active(_in_tmp, _mock_today):
    pos = {"active": {}, "stop_entry": "19850.0", "stop_direction": "up",
           "conf_bar_entry": {}, "failed_entries": 0}
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=pos), \
         patch("smt_state.save_position") as mock_save:
        live_orders.stop_entry_filled("long", 19820.0)

    # No executor call — real S/L was in the STP order at placement
    mock_executor.update_stop_loss.assert_not_called()
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
                 "conf_bar_entry": {}, "failed_entries": 0}
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
        "conf_bar_entry": {"open": 19890.0},
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
    assert saved["conf_bar_entry"] == {}

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
        "conf_bar_entry": {"open": 19840.0},
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
    assert saved["conf_bar_entry"] == {}

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
        "stop_entry": "", "stop_direction": "", "conf_bar_entry": {},
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


# ---------------------------------------------------------------------------
# Helpers for D4 / D6 / D8 guard tests
# ---------------------------------------------------------------------------

def _reset_guard_state():
    """Reset all module-level guard timestamps between tests."""
    live_orders._entry_sent_bar_time = None
    live_orders._fill_bar_time = None
    live_orders._cancel_bar_time = None
    live_orders._pending_close_after = None


_T0 = "2026-05-19T10:30:00-04:00"
_T0_PLUS_0_5S = "2026-05-19T10:30:00.5-04:00"
_T0_PLUS_1_5S = "2026-05-19T10:30:01.5-04:00"
_T0_PLUS_1S   = "2026-05-19T10:30:01-04:00"
_T0_PLUS_2S   = "2026-05-19T10:30:02-04:00"
_T0_PLUS_3S   = "2026-05-19T10:30:03-04:00"
_T0_PLUS_4S   = "2026-05-19T10:30:04-04:00"

_POS_WITH_STOP = {
    "active": {}, "stop_entry": "19900.0", "stop_direction": "up",
    "conf_bar_entry": {}, "failed_entries": 0,
}
_POS_EMPTY = {
    "active": {}, "stop_entry": "", "stop_direction": "",
    "conf_bar_entry": {}, "failed_entries": 0,
}
_POS_ACTIVE = {
    "active": {"direction": "long", "fill_price": 19900.0, "stop": 19870.0,
               "contracts": 2, "cautious": "no"},
    "stop_entry": "", "stop_direction": "", "conf_bar_entry": {}, "failed_entries": 0,
}


# ---------------------------------------------------------------------------
# D4 tests
# ---------------------------------------------------------------------------

def test_d4_cancel_suppressed_within_1s(_in_tmp, _mock_today):
    """D4: stop-entry-cancelled fires 0.5s after new-stop-entry → cancel NOT dispatched."""
    _reset_guard_state()
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=_POS_WITH_STOP), \
         patch("smt_state.save_position"):
        live_orders.dispatch({
            "kind": "new-stop-entry", "time": _T0,
            "direction": "up", "price": 19900.0, "stop": 19870.0,
        })
        live_orders.dispatch({
            "kind": "stop-entry-cancelled", "time": _T0_PLUS_0_5S,
            "price": 19900.0, "reason": "trend-broken", "direction": "up",
        })

    mock_executor.place_close.assert_not_called()


def test_d4_cancel_allowed_after_1s(_in_tmp, _mock_today):
    """D4: stop-entry-cancelled fires 1.5s after new-stop-entry → cancel IS dispatched."""
    _reset_guard_state()
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=_POS_WITH_STOP), \
         patch("smt_state.save_position"):
        live_orders.dispatch({
            "kind": "new-stop-entry", "time": _T0,
            "direction": "up", "price": 19900.0, "stop": 19870.0,
        })
        live_orders.dispatch({
            "kind": "stop-entry-cancelled", "time": _T0_PLUS_1_5S,
            "price": 19900.0, "reason": "trend-broken", "direction": "up",
        })

    mock_executor.place_close.assert_called_once_with("cancel-stop")


def test_d4_cancel_allowed_no_prior_entry(_in_tmp, _mock_today):
    """D4: stop-entry-cancelled with no prior new-stop-entry → cancel IS dispatched."""
    _reset_guard_state()
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=_POS_WITH_STOP), \
         patch("smt_state.save_position"):
        live_orders.dispatch({
            "kind": "stop-entry-cancelled", "time": _T0,
            "price": 19900.0, "reason": "trend-broken", "direction": "up",
        })

    mock_executor.place_close.assert_called_once_with("cancel-stop")


# ---------------------------------------------------------------------------
# D6 tests
# ---------------------------------------------------------------------------

def test_d6_stop_exit_suppressed_within_3s(_in_tmp, _mock_today):
    """D6: stop-exit fires 1s after stop-entry-filled → close NOT dispatched."""
    _reset_guard_state()
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=_POS_ACTIVE), \
         patch("smt_state.save_position"):
        live_orders.dispatch({
            "kind": "stop-entry-filled", "time": _T0,
            "direction": "up", "price": 19900.0, "stop": 19870.0,
        })
        live_orders.dispatch({
            "kind": "stop-exit", "time": _T0_PLUS_1S,
            "direction": "up", "price": 19900.0,
        })

    mock_executor.place_close.assert_not_called()


def test_d6_stop_exit_allowed_after_3s(_in_tmp, _mock_today):
    """D6: stop-exit fires 4s after stop-entry-filled → close IS dispatched."""
    _reset_guard_state()
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=_POS_ACTIVE), \
         patch("smt_state.save_position"):
        live_orders.dispatch({
            "kind": "stop-entry-filled", "time": _T0,
            "direction": "up", "price": 19900.0, "stop": 19870.0,
        })
        live_orders.dispatch({
            "kind": "stop-exit", "time": _T0_PLUS_4S,
            "direction": "up", "price": 19880.0,
        })

    mock_executor.place_close.assert_called_once_with("close")


def test_d6_stop_exit_allowed_no_prior_fill(_in_tmp, _mock_today):
    """D6: stop-exit with no prior stop-entry-filled → close IS dispatched."""
    _reset_guard_state()
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=_POS_ACTIVE), \
         patch("smt_state.save_position"):
        live_orders.dispatch({
            "kind": "stop-exit", "time": _T0,
            "direction": "up", "price": 19900.0,
        })

    mock_executor.place_close.assert_called_once_with("close")


# ---------------------------------------------------------------------------
# D8 tests
# ---------------------------------------------------------------------------

def test_d8_market_close_deferred_within_3s(_in_tmp, _mock_today):
    """D8: market-close fires 1s after stop-entry-cancelled → close_position NOT called."""
    _reset_guard_state()
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=_POS_EMPTY), \
         patch("smt_state.save_position"):
        live_orders.dispatch({
            "kind": "stop-entry-cancelled", "time": _T0,
            "price": 19900.0, "reason": "trend-broken", "direction": "up",
        })
        live_orders.dispatch({
            "kind": "market-close", "time": _T0_PLUS_1S,
            "price": 19890.0, "reason": "strategy",
        })

    # place_close("cancel-stop") from the cancel is called, but place_close("close") is not
    call_args_list = [call.args[0] for call in mock_executor.place_close.call_args_list]
    assert "close" not in call_args_list


def test_d8_market_close_allowed_after_3s(_in_tmp, _mock_today):
    """D8: market-close fires 4s after stop-entry-cancelled → close_position IS called."""
    _reset_guard_state()
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=_POS_EMPTY), \
         patch("smt_state.save_position"):
        live_orders.dispatch({
            "kind": "stop-entry-cancelled", "time": _T0,
            "price": 19900.0, "reason": "trend-broken", "direction": "up",
        })
        live_orders.dispatch({
            "kind": "market-close", "time": _T0_PLUS_4S,
            "price": 19890.0, "reason": "strategy",
        })

    call_args_list = [call.args[0] for call in mock_executor.place_close.call_args_list]
    assert "close" in call_args_list


def test_d8_fill_cancels_pending_close(_in_tmp, _mock_today):
    """D8: fill arrives during the 3s deferral → pending_close cleared; market-close proceeds."""
    _reset_guard_state()
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=_POS_ACTIVE), \
         patch("smt_state.save_position"):
        live_orders.dispatch({
            "kind": "stop-entry-cancelled", "time": _T0,
            "price": 19900.0, "reason": "trend-broken", "direction": "up",
        })
        live_orders.dispatch({
            "kind": "stop-entry-filled", "time": _T0_PLUS_1S,
            "direction": "up", "price": 19900.0, "stop": 19870.0,
        })
        live_orders.dispatch({
            "kind": "market-close", "time": _T0_PLUS_2S,
            "price": 19890.0, "reason": "strategy",
        })

    # pending_close_after was cleared by the fill, so market-close at T+2s goes through
    call_args_list = [call.args[0] for call in mock_executor.place_close.call_args_list]
    assert "close" in call_args_list


def test_d8_market_close_noop_pending_if_no_prior_cancel(_in_tmp, _mock_today):
    """D8: market-close with no prior stop-entry-cancelled → close_position IS called (no deferral)."""
    _reset_guard_state()
    mock_executor = MagicMock()
    with patch.object(live_orders, "_executor", mock_executor), \
         patch("smt_state.load_position", return_value=_POS_EMPTY), \
         patch("smt_state.save_position"):
        live_orders.dispatch({
            "kind": "market-close", "time": _T0,
            "price": 19890.0, "reason": "strategy",
        })

    call_args_list = [call.args[0] for call in mock_executor.place_close.call_args_list]
    assert "close" in call_args_list
