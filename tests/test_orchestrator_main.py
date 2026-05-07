import datetime
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from zoneinfo import ZoneInfo

import pytest

from orchestrator.main import _check_setup, _close_session_position, _pre_session_init, run

_ET = ZoneInfo("America/New_York")


def _dt(hour, minute=0, date=None):
    """Helper: return an ET datetime for a specific time on 2026-04-21 (Tuesday, trading day)."""
    if date is None:
        date = datetime.date(2026, 4, 21)
    return datetime.datetime(date.year, date.month, date.day, hour, minute, tzinfo=_ET)


def test_main_non_trading_day_sleeps_to_next_open():
    mock_summarizer = MagicMock()
    next_open = _dt(9, 0, date=datetime.date(2026, 4, 22))
    with patch("orchestrator.main.get_et_now", return_value=_dt(10, 0)), \
         patch("orchestrator.main.is_trading_day", return_value=False), \
         patch("orchestrator.main.next_session_open", return_value=next_open), \
         patch("orchestrator.main.ProcessManager") as mock_pm, \
         patch("orchestrator.main.time.sleep", side_effect=StopIteration):
        with pytest.raises(StopIteration):
            run(summarizer=mock_summarizer)
    mock_pm.assert_not_called()
    mock_summarizer.run.assert_not_called()


def test_main_before_session_open_sleeps_to_open():
    mock_summarizer = MagicMock()
    with patch("orchestrator.main.get_et_now", return_value=_dt(8, 20)), \
         patch("orchestrator.main.is_trading_day", return_value=True), \
         patch("orchestrator.main.next_session_open", return_value=_dt(9, 20)), \
         patch("orchestrator.main.ProcessManager") as mock_pm, \
         patch("orchestrator.main.time.sleep", side_effect=StopIteration) as mock_sleep:
        with pytest.raises(StopIteration):
            run(summarizer=mock_summarizer)
    mock_pm.assert_not_called()
    mock_summarizer.run.assert_not_called()
    # time.sleep was called with a positive delay (session open 09:20 is 1h away from 08:20)
    assert mock_sleep.call_count == 1
    delay_arg = mock_sleep.call_args.args[0]
    assert delay_arg == pytest.approx(3600, abs=1)


def test_main_after_grace_end_skips_to_next_day():
    mock_summarizer = MagicMock()
    next_open = _dt(9, 0, date=datetime.date(2026, 4, 22))
    with patch("orchestrator.main.get_et_now", return_value=_dt(17, 0)), \
         patch("orchestrator.main.is_trading_day", return_value=True), \
         patch("orchestrator.main.next_session_open", return_value=next_open) as mock_next_open, \
         patch("orchestrator.main.ProcessManager") as mock_pm, \
         patch("orchestrator.main.time.sleep", side_effect=StopIteration):
        with pytest.raises(StopIteration):
            run(summarizer=mock_summarizer)
    mock_pm.assert_not_called()
    mock_summarizer.run.assert_not_called()
    mock_next_open.assert_called()


def test_main_in_session_runs_session_then_summarizes(tmp_path):
    mock_summarizer = MagicMock()
    mock_pm_instance = MagicMock()
    next_open = _dt(9, 20, date=datetime.date(2026, 4, 22))

    call_order = []
    mock_pm_instance.run_session.side_effect = lambda d: call_order.append(("run_session", d))
    mock_summarizer.run.side_effect = lambda *a, **kw: call_order.append(("summarize", a[0]))

    with patch("orchestrator.main._SESSIONS_DIR", tmp_path / "sessions"), \
         patch("orchestrator.main.get_et_now", return_value=_dt(9, 25)), \
         patch("orchestrator.main.is_trading_day", return_value=True), \
         patch("orchestrator.main.next_session_open", return_value=next_open), \
         patch("orchestrator.main.ProcessManager", return_value=mock_pm_instance), \
         patch("orchestrator.main.time.sleep", side_effect=StopIteration):
        with pytest.raises(StopIteration):
            run(summarizer=mock_summarizer)

    today = datetime.date(2026, 4, 21)
    mock_pm_instance.run_session.assert_called_once_with(today)
    mock_summarizer.run.assert_called_once()
    # Verify order: run_session before summarize
    assert call_order[0] == ("run_session", today)
    assert call_order[1] == ("summarize", today)


def test_main_session_dirs_created(tmp_path):
    mock_summarizer = MagicMock()
    mock_pm_instance = MagicMock()
    sessions_dir = tmp_path / "sessions"

    # When ProcessManager.run_session is called, verify the session dir already exists
    def assert_dir_exists(date):
        assert (sessions_dir / "2026-04-21").exists()
    mock_pm_instance.run_session.side_effect = assert_dir_exists

    next_open = _dt(9, 20, date=datetime.date(2026, 4, 22))
    with patch("orchestrator.main._SESSIONS_DIR", sessions_dir), \
         patch("orchestrator.main.get_et_now", return_value=_dt(9, 25)), \
         patch("orchestrator.main.is_trading_day", return_value=True), \
         patch("orchestrator.main.next_session_open", return_value=next_open), \
         patch("orchestrator.main.ProcessManager", return_value=mock_pm_instance), \
         patch("orchestrator.main.time.sleep", side_effect=StopIteration):
        with pytest.raises(StopIteration):
            run(summarizer=mock_summarizer)

    assert (sessions_dir / "2026-04-21").exists()


def test_close_session_position_closes_active_position():
    """Sends manual_close when position.json shows an active trade."""
    log_ch = MagicMock()
    pos = {"active": {"fill_price": 19850.0}, "limit_entry": "", "limit_direction": ""}
    with patch("smt_state.load_position", return_value=pos), \
         patch("live_orders.manual_close") as mock_close:
        _close_session_position(log_ch)
    mock_close.assert_called_once_with(19850.0, reason="session-end")


def test_close_session_position_noop_when_no_active():
    """Does nothing when no active position in position.json."""
    log_ch = MagicMock()
    pos = {"active": {}, "limit_entry": "", "limit_direction": ""}
    with patch("smt_state.load_position", return_value=pos), \
         patch("live_orders.manual_close") as mock_close:
        _close_session_position(log_ch)
    mock_close.assert_not_called()


def test_check_setup_exits_0_with_valid_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with patch("orchestrator.main._SESSIONS_DIR", tmp_path / "sessions"), \
         patch("orchestrator.main.Summarizer") as mock_summarizer_cls:
        mock_summarizer_cls.return_value = MagicMock()
        with pytest.raises(SystemExit) as exc_info:
            _check_setup()
    assert exc_info.value.code == 0


def test_check_setup_exits_1_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        _check_setup()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _pre_session_init tests
# ---------------------------------------------------------------------------

def test_pre_session_init_skips_when_no_api_key(monkeypatch, capsys):
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    _pre_session_init()  # must not raise
    out = capsys.readouterr().out
    # The guard must print the skip message and must NOT start the backfill.
    assert "DATABENTO_API_KEY not set" in out
    assert "Running Databento" not in out


def test_pre_session_init_calls_backfill_when_key_set(monkeypatch):
    monkeypatch.setenv("DATABENTO_API_KEY", "test-key")
    # backfill_parquets is imported locally inside _pre_session_init via
    # "from data.databento_backfill import backfill_parquets"; patch at source module
    with patch("data.databento_backfill.backfill_parquets") as mock_bp:
        _pre_session_init()
    mock_bp.assert_called_once()


def test_pre_session_init_does_not_raise_on_backfill_exception(monkeypatch):
    monkeypatch.setenv("DATABENTO_API_KEY", "test-key")
    with patch("data.databento_backfill.backfill_parquets", side_effect=RuntimeError("network")):
        _pre_session_init()  # must not raise despite the exception


def test_pre_session_init_called_before_session_loop(tmp_path):
    """_pre_session_init() must be called before the first iteration of the session loop."""
    mock_summarizer = MagicMock()
    call_order = []

    def record_pre_session():
        call_order.append("pre_session_init")

    def record_is_trading_day(d):
        call_order.append("is_trading_day")
        return False

    next_open = _dt(9, 0, date=datetime.date(2026, 4, 22))
    with patch("orchestrator.main._pre_session_init", side_effect=record_pre_session), \
         patch("orchestrator.main.get_et_now", return_value=_dt(10, 0)), \
         patch("orchestrator.main.is_trading_day", side_effect=record_is_trading_day), \
         patch("orchestrator.main.next_session_open", return_value=next_open), \
         patch("orchestrator.main.time.sleep", side_effect=StopIteration):
        with pytest.raises(StopIteration):
            run(summarizer=mock_summarizer)

    assert call_order[0] == "pre_session_init"
    assert "is_trading_day" in call_order


# ---------------------------------------------------------------------------
# ib_disconnected handling tests
# ---------------------------------------------------------------------------

def test_run_exits_3_on_ib_disconnected(tmp_path):
    mock_summarizer = MagicMock()
    mock_pm_instance = MagicMock()
    mock_pm_instance.run_session.return_value = "ib_disconnected"
    next_open = _dt(9, 20, date=datetime.date(2026, 4, 22))

    with patch("orchestrator.main._SESSIONS_DIR", tmp_path / "sessions"), \
         patch("orchestrator.main.get_et_now", return_value=_dt(9, 25)), \
         patch("orchestrator.main.is_trading_day", return_value=True), \
         patch("orchestrator.main.next_session_open", return_value=next_open), \
         patch("orchestrator.main.ProcessManager", return_value=mock_pm_instance), \
         patch("orchestrator.main._pre_session_init"), \
         patch("orchestrator.main._close_session_position"):
        with pytest.raises(SystemExit) as exc_info:
            run(summarizer=mock_summarizer)

    assert exc_info.value.code == 3


def test_run_closes_position_before_ib_disconnect_exit(tmp_path):
    mock_summarizer = MagicMock()
    mock_pm_instance = MagicMock()
    mock_pm_instance.run_session.return_value = "ib_disconnected"
    next_open = _dt(9, 20, date=datetime.date(2026, 4, 22))
    call_order = []

    def record_close(_):
        call_order.append("close")

    def record_exit(code):
        call_order.append(f"exit_{code}")
        raise SystemExit(code)

    with patch("orchestrator.main._SESSIONS_DIR", tmp_path / "sessions"), \
         patch("orchestrator.main.get_et_now", return_value=_dt(9, 25)), \
         patch("orchestrator.main.is_trading_day", return_value=True), \
         patch("orchestrator.main.next_session_open", return_value=next_open), \
         patch("orchestrator.main.ProcessManager", return_value=mock_pm_instance), \
         patch("orchestrator.main._pre_session_init"), \
         patch("orchestrator.main._close_session_position", side_effect=record_close), \
         patch("orchestrator.main.sys.exit", side_effect=record_exit):
        with pytest.raises(SystemExit):
            run(summarizer=mock_summarizer)

    assert call_order == ["close", "exit_3"]
