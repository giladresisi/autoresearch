import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from orchestrator.main import _check_setup, run

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
    with patch("orchestrator.main.get_et_now", return_value=_dt(8, 0)), \
         patch("orchestrator.main.is_trading_day", return_value=True), \
         patch("orchestrator.main.next_session_open", return_value=_dt(9, 0)), \
         patch("orchestrator.main.ProcessManager") as mock_pm, \
         patch("orchestrator.main.time.sleep", side_effect=StopIteration) as mock_sleep:
        with pytest.raises(StopIteration):
            run(summarizer=mock_summarizer)
    mock_pm.assert_not_called()
    mock_summarizer.run.assert_not_called()
    # time.sleep was called with a positive delay (session open is 1h away)
    assert mock_sleep.call_count == 1
    delay_arg = mock_sleep.call_args.args[0]
    assert delay_arg == pytest.approx(3600, abs=1)


def test_main_after_grace_end_skips_to_next_day():
    mock_summarizer = MagicMock()
    next_open = _dt(9, 0, date=datetime.date(2026, 4, 22))
    with patch("orchestrator.main.get_et_now", return_value=_dt(14, 0)), \
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
    next_open = _dt(9, 0, date=datetime.date(2026, 4, 22))

    call_order = []
    mock_pm_instance.run_session.side_effect = lambda d: call_order.append(("run_session", d))
    mock_summarizer.run.side_effect = lambda *a, **kw: call_order.append(("summarize", a[0]))

    with patch("orchestrator.main._SESSIONS_DIR", tmp_path / "sessions"), \
         patch("orchestrator.main.get_et_now", return_value=_dt(9, 15)), \
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

    next_open = _dt(9, 0, date=datetime.date(2026, 4, 22))
    with patch("orchestrator.main._SESSIONS_DIR", sessions_dir), \
         patch("orchestrator.main.get_et_now", return_value=_dt(9, 15)), \
         patch("orchestrator.main.is_trading_day", return_value=True), \
         patch("orchestrator.main.next_session_open", return_value=next_open), \
         patch("orchestrator.main.ProcessManager", return_value=mock_pm_instance), \
         patch("orchestrator.main.time.sleep", side_effect=StopIteration):
        with pytest.raises(StopIteration):
            run(summarizer=mock_summarizer)

    assert (sessions_dir / "2026-04-21").exists()


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
