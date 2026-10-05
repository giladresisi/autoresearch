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


@pytest.fixture(autouse=True)
def _no_real_r2(monkeypatch):
    """trade.py loads .env for the R2 commands: never let a test see (or use) real R2 creds."""
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    for v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(v, raising=False)


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


def _session_dir(tmp_path, monkeypatch):
    """A session folder for 'today' under an isolated global root."""
    import paths
    from session_times import session_date_str
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    d = paths.sessions_dir() / session_date_str()
    d.mkdir(parents=True)
    return d


def test_update_sl_queues_the_stop_for_the_agent(monkeypatch, tmp_path, capsys):
    """Plan 47 D1: the broker stop and the agent's modelled stop move together. The
    broker update goes out first; the control record is what the running agent reads."""
    import json
    monkeypatch.delenv("ACT_TRADER", raising=False)
    session = _session_dir(tmp_path, monkeypatch)
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {"direction": "long"}, "stop_entry": "", "stop_direction": "",
    }
    _run_trade(["update-sl", "30852"], monkeypatch, mock_lo, MagicMock())
    mock_lo.update_stop_loss.assert_called_once_with(30852.0, "user-requested",
                                                     direction="long")
    lines = (session / "operator_control.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[-1])
    assert len(lines) == 1 and rec["kind"] == "set_stop" and rec["price"] == 30852.0
    assert rec["seq"] == 1 and rec["created_at"]
    assert "Queued for the agent" in capsys.readouterr().out


def test_update_sl_is_not_queued_when_the_agent_is_off(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ACT_TRADER", "0")
    session = _session_dir(tmp_path, monkeypatch)
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {"direction": "long"}, "stop_entry": "", "stop_direction": "",
    }
    _run_trade(["update-sl", "30852"], monkeypatch, mock_lo, MagicMock())
    mock_lo.update_stop_loss.assert_called_once()
    assert not (session / "operator_control.jsonl").exists()


def test_update_sl_without_a_session_folder_queues_nothing(monkeypatch, tmp_path, capsys):
    """No session folder means no running agent to read the record; never create one."""
    monkeypatch.delenv("ACT_TRADER", raising=False)
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = {
        "active": {"direction": "long"}, "stop_entry": "", "stop_direction": "",
    }
    _run_trade(["update-sl", "30852"], monkeypatch, mock_lo, MagicMock())
    mock_lo.update_stop_loss.assert_called_once()
    assert not list(tmp_path.rglob("operator_control.jsonl"))


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


# ---------------------------------------------------------------------------
# gap-fill command
# ---------------------------------------------------------------------------

def test_gap_fill_loads_dotenv_then_calls_gap_fill_until_now(monkeypatch, capsys):
    """`trade.py gap-fill` loads .env, then invokes gap_fill_until_now()."""
    mock_dotenv = MagicMock()
    mock_gap_fill = MagicMock()
    monkeypatch.setitem(sys.modules, "dotenv", mock_dotenv)
    monkeypatch.setitem(sys.modules, "gap_fill", mock_gap_fill)

    _run_trade(["gap-fill"], monkeypatch, MagicMock(), MagicMock())

    mock_dotenv.load_dotenv.assert_called_once()
    mock_gap_fill.gap_fill_until_now.assert_called_once_with()
    out = capsys.readouterr().out
    assert "Gap-fill complete" in out


# ---------------------------------------------------------------------------
# promote command
# ---------------------------------------------------------------------------

def test_promote_calls_promote_live_to_main(monkeypatch, capsys):
    """`trade.py promote` copies the live parquets over main (backing up the prior
    main files) via check_session_parquets.promote_live_to_main()."""
    mock_csp = MagicMock()
    mock_csp.promote_live_to_main.return_value = {
        "MNQ_1m.parquet": "ok", "MES_1m.parquet": "ok",
        "MNQ_1s.parquet": "ok", "MES_1s.parquet": "ok",
    }
    monkeypatch.setitem(sys.modules, "scripts.check_session_parquets", mock_csp)

    _run_trade(["promote"], monkeypatch, MagicMock(), MagicMock())

    mock_csp.promote_live_to_main.assert_called_once_with()
    out = capsys.readouterr().out
    assert "Promoted 4 file(s)" in out


def test_promote_warns_when_nothing_promoted(monkeypatch, capsys):
    """No live parquets found -> say so instead of a silent no-op."""
    mock_csp = MagicMock()
    mock_csp.promote_live_to_main.return_value = {}
    monkeypatch.setitem(sys.modules, "scripts.check_session_parquets", mock_csp)

    _run_trade(["promote"], monkeypatch, MagicMock(), MagicMock())

    assert "nothing promoted" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# promote / rollover-prep -> R2 publish tail (plan 50)
# ---------------------------------------------------------------------------

def _promote_env(monkeypatch, publish_result=None, calls=None):
    from scripts import r2_sync
    calls = calls if calls is not None else []
    mock_csp = MagicMock()
    mock_csp.promote_live_to_main.side_effect = lambda: calls.append("promote") or {"MNQ_1m.parquet": "ok"}
    monkeypatch.setitem(sys.modules, "scripts.check_session_parquets", mock_csp)
    res = publish_result or {"configured": True, "success": True, "error": None, "report": None}
    monkeypatch.setattr(r2_sync, "publish_after_promote",
                        lambda *a, **k: calls.append("publish") or res)
    return calls


def test_promote_publishes_after_local_copy(monkeypatch, capsys):
    calls = _promote_env(monkeypatch)
    _run_trade(["promote"], monkeypatch, MagicMock(), MagicMock())
    assert calls == ["promote", "publish"]


def test_promote_upload_failure_keeps_local_promote_and_exits_nonzero(monkeypatch, capsys):
    calls = _promote_env(monkeypatch, {"configured": True, "success": False,
                                       "error": "boom", "report": None})
    with pytest.raises(SystemExit) as exc:
        _run_trade(["promote"], monkeypatch, MagicMock(), MagicMock())
    assert exc.value.code == 1 and calls == ["promote", "publish"]
    assert "Promoted 1 file(s)" in capsys.readouterr().out


def test_promote_without_r2_env_still_promotes_and_exits_zero(monkeypatch, capsys):
    mock_csp = MagicMock()
    mock_csp.promote_live_to_main.return_value = {"MNQ_1m.parquet": "ok"}
    monkeypatch.setitem(sys.modules, "scripts.check_session_parquets", mock_csp)
    _run_trade(["promote"], monkeypatch, MagicMock(), MagicMock())     # no SystemExit
    out = capsys.readouterr().out
    assert "Promoted 1 file(s)" in out and "R2 not configured" in out


def test_promote_no_publish_flag_skips_upload(monkeypatch, capsys):
    calls = _promote_env(monkeypatch)
    _run_trade(["promote", "--no-publish"], monkeypatch, MagicMock(), MagicMock())
    assert calls == ["promote"]


def _rollover_env(monkeypatch):
    from scripts import r2_sync
    calls = []
    mock_rp = MagicMock()
    mock_rp.run_rollover_prep.return_value = {
        "old_conids": {"mnq": 1, "mes": 2}, "new_conids": {"mnq": 3, "mes": 4},
        "gaps": {"mnq": 1.0, "mes": 1.0}, "boundaries": {"mnq": "t", "mes": "t"},
        "expiry": "2026-12-18", "subfolder": "2026-12", "next_prep_date": "2027-03-06"}
    monkeypatch.setitem(sys.modules, "scripts.rollover_prep", mock_rp)
    monkeypatch.setattr(r2_sync, "publish_after_promote",
                        lambda *a, **k: calls.append("publish")
                        or {"configured": True, "success": True, "error": None, "report": None})
    return calls


def test_rollover_prep_publishes_ledger_and_new_subfolder(monkeypatch, capsys):
    calls = _rollover_env(monkeypatch)
    _run_trade(["rollover-prep"], monkeypatch, MagicMock(), MagicMock())
    assert calls == ["publish"]


def test_rollover_prep_dry_run_does_not_publish(monkeypatch, capsys):
    calls = _rollover_env(monkeypatch)
    _run_trade(["rollover-prep", "--dry-run"], monkeypatch, MagicMock(), MagicMock())
    assert calls == []


# ---------------------------------------------------------------------------
# publish / sync subcommands
# ---------------------------------------------------------------------------

def test_publish_and_sync_commands_pass_selectors(monkeypatch, capsys):
    from scripts import r2_sync
    seen = {}
    ok = {"ok": True, "groups": {}, "errors": [], "conflicts": [], "warnings": []}
    monkeypatch.setattr(r2_sync, "publish", lambda **kw: seen.update(pub=kw) or ok)
    monkeypatch.setattr(r2_sync, "sync", lambda **kw: seen.update(syn=kw) or ok)
    _run_trade(["publish", "--only", "main,live", "--dry-run"], monkeypatch, MagicMock(), MagicMock())
    _run_trade(["sync", "--date", "2026-10-01", "--prune", "--force"], monkeypatch,
               MagicMock(), MagicMock())
    assert seen["pub"] == {"only": "main,live", "date": None, "dry_run": True, "force": False}
    assert seen["syn"] == {"only": None, "date": "2026-10-01", "dry_run": False,
                           "prune": True, "force": True}


def test_sync_conflict_exits_nonzero(monkeypatch, capsys):
    from scripts import r2_sync
    bad = {"ok": False, "groups": {}, "errors": [], "warnings": [],
           "conflicts": [{"group": "live", "path": "MNQ_1m.parquet",
                          "local_last_ts": "a", "remote_last_ts": "b"}]}
    monkeypatch.setattr(r2_sync, "sync", lambda **kw: bad)
    with pytest.raises(SystemExit) as exc:
        _run_trade(["sync"], monkeypatch, MagicMock(), MagicMock())
    assert exc.value.code == 1 and "CONFLICT" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Plan 52: POSIX launch kwargs + python3.x process-name matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("os_name,expected", [
    ("nt", {"creationflags": 0x08000000}),
    ("posix", {"start_new_session": True}),
])
def test_start_launch_uses_detached_kwargs_on_windows_and_posix(monkeypatch, tmp_path, os_name, expected):
    import os as _os
    import subprocess as _subp
    import time as _time
    import types
    import platform_compat
    monkeypatch.chdir(tmp_path)
    popen_kwargs = {}

    def _popen(*a, **k):
        popen_kwargs.update(k)
        return MagicMock()

    monkeypatch.setattr(_subp, "Popen", _popen)
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(platform_compat, "os", types.SimpleNamespace(name=os_name, getpid=_os.getpid))

    monkeypatch.setattr(sys, "argv", ["trade.py", "start"])
    monkeypatch.setitem(sys.modules, "live_orders", MagicMock())
    monkeypatch.setitem(sys.modules, "smt_state", MagicMock())
    if "trade" in sys.modules:
        del sys.modules["trade"]
    trade = importlib.import_module("trade")
    monkeypatch.setattr(trade, "_orchestrator_pid", lambda: None)
    monkeypatch.setattr(trade, "_terminate_all", MagicMock(return_value=[]))

    trade.main()

    for key, val in expected.items():
        assert popen_kwargs[key] == val
    if os_name == "posix":
        assert "creationflags" not in popen_kwargs
    else:
        assert "start_new_session" not in popen_kwargs


def _sweep_proc(pid, cwd, name, cmdline):
    p = MagicMock()
    p.pid = pid
    p.info = {"pid": pid, "name": name, "cmdline": list(cmdline)}
    p.cwd.return_value = cwd
    return p


def _import_trade(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path / "g"))
    monkeypatch.setitem(sys.modules, "live_orders", MagicMock())
    monkeypatch.setitem(sys.modules, "smt_state", MagicMock())
    if "trade" in sys.modules:
        del sys.modules["trade"]
    return importlib.import_module("trade")


def test_terminate_sweep_finds_python3_12_orchestrator_by_cmdline_and_cwd(monkeypatch, tmp_path):
    from pathlib import Path
    trade = _import_trade(monkeypatch, tmp_path)
    root = str(Path(trade.__file__).resolve().parent)
    orch = _sweep_proc(994001, root, "python3.12", ("python3.12", "-m", "orchestrator.main"))
    monkeypatch.setattr("psutil.process_iter", lambda attrs=None: [orch])
    killed = trade._terminate_all()
    orch.terminate.assert_called_once()
    assert any("994001" in k for k in killed)


def test_terminate_sweep_ignores_non_python_even_with_matching_cmdline(monkeypatch, tmp_path):
    from pathlib import Path
    trade = _import_trade(monkeypatch, tmp_path)
    root = str(Path(trade.__file__).resolve().parent)
    uv = _sweep_proc(994002, root, "uv", ("uv", "run", "python", "-m", "orchestrator.main"))
    monkeypatch.setattr("psutil.process_iter", lambda attrs=None: [uv])
    trade._terminate_all()
    uv.terminate.assert_not_called()


def test_terminate_sweep_ignores_other_worktree_cwd(monkeypatch, tmp_path):
    trade = _import_trade(monkeypatch, tmp_path)
    orch = _sweep_proc(994003, str(tmp_path / "other_worktree"), "python3.12",
                       ("python3.12", "-m", "orchestrator.main"))
    monkeypatch.setattr("psutil.process_iter", lambda attrs=None: [orch])
    trade._terminate_all()
    orch.terminate.assert_not_called()
