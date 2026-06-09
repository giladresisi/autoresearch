# tests/test_trade_cli.py
# Unit tests for trade.py CLI — direct import + monkeypatch for clean isolation.

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_trade(argv: list[str], monkeypatch,
               mock_lo: MagicMock, mock_smt: MagicMock) -> None:
    """Invoke trade.main() with given argv. live_orders + smt_state are patched."""
    monkeypatch.setattr(sys, "argv", ["trade.py", *argv])
    # Force a fresh import of trade so it picks up the patched modules
    monkeypatch.setitem(sys.modules, "live_orders", mock_lo)
    monkeypatch.setitem(sys.modules, "smt_state", mock_smt)
    if "trade" in sys.modules:
        del sys.modules["trade"]
    trade = importlib.import_module("trade")
    trade.main()


# ---------------------------------------------------------------------------
# Test 1: `trade.py up` reads bar_state.potential_stop_long
# ---------------------------------------------------------------------------

def test_up_market_reads_bar_state(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {}, "stop_entry": "", "stop_direction": "", "conf_bar_entry": {},
    }
    mock_smt = MagicMock()
    mock_smt.load_bar_state.return_value = {
        "time": "x", "potential_stop_long": 27000.0, "potential_stop_short": 27100.0,
    }
    _run_trade(["up"], monkeypatch, mock_lo, mock_smt)

    mock_lo.place_market_entry.assert_called_once_with("long", 0.0, 27000.0, source="manual")
    out = capsys.readouterr().out
    assert "Market LONG" in out
    assert "27000" in out


# ---------------------------------------------------------------------------
# Test 2: `trade.py up` fails when no bar_state.json
# ---------------------------------------------------------------------------

def test_up_market_fails_no_bar_state(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_smt = MagicMock()
    mock_smt.load_bar_state.return_value = None
    with pytest.raises(SystemExit) as exc:
        _run_trade(["up"], monkeypatch, mock_lo, mock_smt)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "ERROR" in out
    mock_lo.place_market_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: `trade.py up` fails when potential_stop_long is null
# ---------------------------------------------------------------------------

def test_up_market_fails_null_stop(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_smt = MagicMock()
    mock_smt.load_bar_state.return_value = {
        "time": "x", "potential_stop_long": None, "potential_stop_short": 27100.0,
    }
    with pytest.raises(SystemExit) as exc:
        _run_trade(["up"], monkeypatch, mock_lo, mock_smt)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "ERROR" in out
    mock_lo.place_market_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: `trade.py up 27000` places stop entry LONG
# ---------------------------------------------------------------------------

def test_up_stop_entry_places_stp(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {}, "stop_entry": "", "stop_direction": "", "conf_bar_entry": {},
    }
    mock_smt = MagicMock()
    # Explicit sl_price as third argv arg
    _run_trade(["up", "27000", "26950"], monkeypatch, mock_lo, mock_smt)

    mock_lo.place_stop_entry.assert_called_once_with("long", 27000.0, 26950.0, source="manual")
    out = capsys.readouterr().out
    assert "Stop entry LONG" in out
    assert "27000" in out


# ---------------------------------------------------------------------------
# Test 4b: `trade.py up 27000` reads sl from bar_state when no explicit sl given
# ---------------------------------------------------------------------------

def test_up_stop_entry_reads_sl_from_bar_state(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {}, "stop_entry": "", "stop_direction": "", "conf_bar_entry": {},
    }
    mock_smt = MagicMock()
    mock_smt.load_bar_state.return_value = {
        "time": "x", "potential_stop_long": 26900.0, "potential_stop_short": 27150.0,
    }
    _run_trade(["up", "27000"], monkeypatch, mock_lo, mock_smt)

    mock_lo.place_stop_entry.assert_called_once_with("long", 27000.0, 26900.0, source="manual")
    out = capsys.readouterr().out
    assert "Stop entry LONG" in out


# ---------------------------------------------------------------------------
# Test 5: `trade.py down` uses potential_stop_short
# ---------------------------------------------------------------------------

def test_down_market_uses_potential_stop_short(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {}, "stop_entry": "", "stop_direction": "", "conf_bar_entry": {},
    }
    mock_smt = MagicMock()
    mock_smt.load_bar_state.return_value = {
        "time": "x", "potential_stop_long": 27000.0, "potential_stop_short": 27150.0,
    }
    _run_trade(["down"], monkeypatch, mock_lo, mock_smt)

    mock_lo.place_market_entry.assert_called_once_with("short", 0.0, 27150.0, source="manual")
    out = capsys.readouterr().out
    assert "Market SHORT" in out


# ---------------------------------------------------------------------------
# Test 6: `trade.py down 27000` places stop entry SHORT
# ---------------------------------------------------------------------------

def test_down_stop_entry_places_stp(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {}, "stop_entry": "", "stop_direction": "", "conf_bar_entry": {},
    }
    mock_smt = MagicMock()
    # Explicit sl_price as third argv arg
    _run_trade(["down", "27000", "27050"], monkeypatch, mock_lo, mock_smt)

    mock_lo.place_stop_entry.assert_called_once_with("short", 27000.0, 27050.0, source="manual")
    out = capsys.readouterr().out
    assert "Stop entry SHORT" in out


# ---------------------------------------------------------------------------
# Test 7: `trade.py cancel` is a no-op when stop_entry is empty
# ---------------------------------------------------------------------------

def test_cancel_noop_when_no_pending(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {}, "stop_entry": "", "stop_direction": "", "conf_bar_entry": {},
    }
    mock_smt = MagicMock()
    with pytest.raises(SystemExit) as exc:
        _run_trade(["cancel"], monkeypatch, mock_lo, mock_smt)
    assert exc.value.code == 1
    mock_lo.cancel_stop_entry.assert_not_called()
    out = capsys.readouterr().out
    assert "ERROR" in out


# ---------------------------------------------------------------------------
# Test 8: `trade.py cancel` calls cancel_stop_entry
# ---------------------------------------------------------------------------

def test_cancel_calls_cancel_stop_entry(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {}, "stop_entry": "27000.0", "stop_direction": "up",
        "conf_bar_entry": {},
    }
    mock_smt = MagicMock()
    _run_trade(["cancel"], monkeypatch, mock_lo, mock_smt)

    mock_lo.cancel_stop_entry.assert_called_once_with("user-requested", force=False)


# ---------------------------------------------------------------------------
# Test 9: `trade.py move 28000` fails when no pending
# ---------------------------------------------------------------------------

def test_move_fails_when_no_pending(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {}, "stop_entry": "", "stop_direction": "", "conf_bar_entry": {},
    }
    mock_smt = MagicMock()
    with pytest.raises(SystemExit) as exc:
        _run_trade(["move", "28000"], monkeypatch, mock_lo, mock_smt)
    assert exc.value.code == 1
    mock_lo.move_stop_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Test 10: `trade.py move 28000` calls move_stop_entry
# ---------------------------------------------------------------------------

def test_move_calls_move_stop_entry(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {}, "stop_entry": "27000.0", "stop_direction": "up",
        "conf_bar_entry": {},
    }
    mock_smt = MagicMock()
    _run_trade(["move", "28000"], monkeypatch, mock_lo, mock_smt)

    mock_lo.move_stop_entry.assert_called_once_with(28000.0, 0.0, "long", force=False)


# ---------------------------------------------------------------------------
# Test 11: `trade.py close` calls close_position when active
# ---------------------------------------------------------------------------

def test_close_market_calls_close_position(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {"direction": "long", "fill_price": 27000.0, "stop": 26970.0},
        "stop_entry": "", "stop_direction": "", "conf_bar_entry": {},
    }
    mock_smt = MagicMock()
    _run_trade(["close"], monkeypatch, mock_lo, mock_smt)

    mock_lo.close_position.assert_called_once_with(0.0, "user-requested")


# ---------------------------------------------------------------------------
# Test 12: `trade.py close` fails when no active
# ---------------------------------------------------------------------------

def test_close_market_fails_when_no_active(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {}, "stop_entry": "", "stop_direction": "", "conf_bar_entry": {},
    }
    mock_smt = MagicMock()
    with pytest.raises(SystemExit) as exc:
        _run_trade(["close"], monkeypatch, mock_lo, mock_smt)
    assert exc.value.code == 1
    mock_lo.close_position.assert_not_called()



# ---------------------------------------------------------------------------
# update-sl <price>
# ---------------------------------------------------------------------------

def test_update_sl_calls_update_stop_loss(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {"direction": "long"}, "stop_entry": "", "stop_direction": "",
    }
    mock_smt = MagicMock()
    _run_trade(["update-sl", "19700"], monkeypatch, mock_lo, mock_smt)
    mock_lo.update_stop_loss.assert_called_once_with(19700.0, "user-requested", direction="long")


def test_update_sl_fails_when_no_active(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {}, "stop_entry": "", "stop_direction": "",
    }
    mock_smt = MagicMock()
    with pytest.raises(SystemExit) as exc:
        _run_trade(["update-sl", "19700"], monkeypatch, mock_lo, mock_smt)
    assert exc.value.code == 1
    mock_lo.update_stop_loss.assert_not_called()


def test_update_sl_fails_when_no_price_arg(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {"direction": "long"}, "stop_entry": "", "stop_direction": "",
    }
    mock_smt = MagicMock()
    with pytest.raises(SystemExit) as exc:
        _run_trade(["update-sl"], monkeypatch, mock_lo, mock_smt)
    assert exc.value.code == 1
    mock_lo.update_stop_loss.assert_not_called()


# ---------------------------------------------------------------------------
# pause / resume commands
# ---------------------------------------------------------------------------

def test_pause_command_engages(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.pause.return_value = True
    _run_trade(["pause"], monkeypatch, mock_lo, MagicMock())
    mock_lo.pause.assert_called_once()
    assert "Paused" in capsys.readouterr().out


def test_pause_command_already_paused(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.pause.return_value = False
    _run_trade(["pause"], monkeypatch, mock_lo, MagicMock())
    assert "Already paused" in capsys.readouterr().out


def test_resume_command_lifts(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.resume.return_value = True
    _run_trade(["resume"], monkeypatch, mock_lo, MagicMock())
    mock_lo.resume.assert_called_once()
    assert "Resumed" in capsys.readouterr().out


def test_resume_command_already_running(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.resume.return_value = False
    _run_trade(["resume"], monkeypatch, mock_lo, MagicMock())
    assert "Already running" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# start --pause / --resume
# ---------------------------------------------------------------------------

def test_start_pause_and_resume_mutually_exclusive(monkeypatch, capsys):
    """--pause and --resume together → error exit before any launch."""
    mock_lo = MagicMock()
    with pytest.raises(SystemExit) as exc:
        _run_trade(["start", "--pause", "--resume"], monkeypatch, mock_lo, MagicMock())
    assert exc.value.code == 1
    assert "mutually exclusive" in capsys.readouterr().out
    mock_lo.pause.assert_not_called()
    mock_lo.resume.assert_not_called()


def test_start_pause_engages_pause_then_launches(monkeypatch, tmp_path, capsys):
    """start --pause engages pause() and proceeds to launch the orchestrator."""
    import subprocess as _subp
    import time as _time
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_subp, "Popen", lambda *a, **k: MagicMock())
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)

    mock_lo = MagicMock()
    mock_lo.pause.return_value = True
    monkeypatch.setattr(sys, "argv", ["trade.py", "start", "--pause"])
    monkeypatch.setitem(sys.modules, "live_orders", mock_lo)
    monkeypatch.setitem(sys.modules, "smt_state", MagicMock())
    if "trade" in sys.modules:
        del sys.modules["trade"]
    trade = importlib.import_module("trade")
    monkeypatch.setattr(trade, "_orchestrator_pid", lambda: None)
    # start now ALWAYS sweeps (even with no pid file) — mock it so the test doesn't scan
    # real processes, and assert the sweep is invoked regardless of pid-file state (Bug 1).
    terminate_mock = MagicMock(return_value=[])
    monkeypatch.setattr(trade, "_terminate_all", terminate_mock)

    trade.main()

    terminate_mock.assert_called_once()
    mock_lo.pause.assert_called_once()
    assert "paused mode" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# GIL-8: `trade.py set-direction` / `flip` / `unlock`
# ---------------------------------------------------------------------------

def test_set_direction_calls_live_orders(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.set_direction.return_value = True
    _run_trade(["set-direction", "down"], monkeypatch, mock_lo, MagicMock())
    mock_lo.set_direction.assert_called_once_with("down")


def test_flip_alias_calls_set_direction(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.set_direction.return_value = True
    _run_trade(["flip", "up"], monkeypatch, mock_lo, MagicMock())
    mock_lo.set_direction.assert_called_once_with("up")


def test_set_direction_requires_direction_arg(monkeypatch, capsys):
    mock_lo = MagicMock()
    with pytest.raises(SystemExit):
        _run_trade(["set-direction"], monkeypatch, mock_lo, MagicMock())
    mock_lo.set_direction.assert_not_called()


def test_set_direction_exits_nonzero_on_refusal(monkeypatch, capsys):
    mock_lo = MagicMock()
    mock_lo.set_direction.return_value = False
    with pytest.raises(SystemExit):
        _run_trade(["set-direction", "down"], monkeypatch, mock_lo, MagicMock())


def test_unlock_calls_live_orders(monkeypatch, capsys):
    mock_lo = MagicMock()
    _run_trade(["unlock"], monkeypatch, mock_lo, MagicMock())
    mock_lo.unlock_direction.assert_called_once_with()
