import datetime
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from zoneinfo import ZoneInfo

import pytest

from orchestrator.main import (
    _check_parquet_files,
    _check_setup,
    _cli_check_parquets,
    _cli_create_empty_parquets,
    _close_session_position,
    _pre_session_init,
    run,
)

_ET = ZoneInfo("America/New_York")


def _dt(hour, minute=0, date=None):
    """Helper: return an ET datetime for a specific time on 2026-04-21 (Tuesday, trading day)."""
    if date is None:
        date = datetime.date(2026, 4, 21)
    return datetime.datetime(date.year, date.month, date.day, hour, minute, tzinfo=_ET)


def test_main_non_trading_day_sleeps_to_next_open():
    mock_summarizer = MagicMock()
    next_open = _dt(9, 0, date=datetime.date(2026, 4, 22))
    with patch("orchestrator.main._check_parquet_files"), \
         patch("orchestrator.main.get_et_now", return_value=_dt(10, 0)), \
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
    with patch("orchestrator.main._check_parquet_files"), \
         patch("orchestrator.main.get_et_now", return_value=_dt(8, 20)), \
         patch("orchestrator.main.is_trading_day", return_value=True), \
         patch("orchestrator.main.next_session_open", return_value=_dt(9, 20)), \
         patch("orchestrator.main.ProcessManager") as mock_pm, \
         patch("orchestrator.main.time.sleep", side_effect=StopIteration) as mock_sleep:
        with pytest.raises(StopIteration):
            run(summarizer=mock_summarizer)
    mock_pm.assert_not_called()
    mock_summarizer.run.assert_not_called()
    # First sleep is to _stop_ts (session_open - 30s = 3570s away), not overnight.
    # This verifies the orchestrator sleeps toward session open, not the next day.
    assert mock_sleep.call_count == 1
    delay_arg = mock_sleep.call_args.args[0]
    from orchestrator.main import _PRE_SESSION_IB_STOP_EARLY_SECS
    assert delay_arg == pytest.approx(3600 - _PRE_SESSION_IB_STOP_EARLY_SECS, abs=2)


def test_main_after_grace_end_skips_to_next_day():
    mock_summarizer = MagicMock()
    next_open = _dt(9, 0, date=datetime.date(2026, 4, 22))
    with patch("orchestrator.main._check_parquet_files"), \
         patch("orchestrator.main.get_et_now", return_value=_dt(17, 0)), \
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

    with patch("orchestrator.main._check_parquet_files"), \
         patch("orchestrator.main._SESSIONS_DIR", tmp_path / "sessions"), \
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
    with patch("orchestrator.main._check_parquet_files"), \
         patch("orchestrator.main._SESSIONS_DIR", sessions_dir), \
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
    pos = {"active": {"fill_price": 19850.0}, "stop_entry": "", "stop_direction": ""}
    with patch("smt_state.load_position", return_value=pos), \
         patch("live_orders.manual_close") as mock_close:
        _close_session_position(log_ch)
    mock_close.assert_called_once_with(19850.0, reason="session-end")


def test_close_session_position_noop_when_no_active():
    """Does nothing when no active position in position.json."""
    log_ch = MagicMock()
    pos = {"active": {}, "stop_entry": "", "stop_direction": ""}
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
    with patch("orchestrator.main._check_parquet_files"), \
         patch("orchestrator.main._pre_session_init", side_effect=record_pre_session), \
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

    with patch("orchestrator.main._check_parquet_files"), \
         patch("orchestrator.main._SESSIONS_DIR", tmp_path / "sessions"), \
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

    with patch("orchestrator.main._check_parquet_files"), \
         patch("orchestrator.main._SESSIONS_DIR", tmp_path / "sessions"), \
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


# ---------------------------------------------------------------------------
# Pre-session IB accumulator tests
# ---------------------------------------------------------------------------

def test_start_pre_session_ib_creates_daemon_thread(tmp_path, monkeypatch):
    import threading
    import time
    from orchestrator.main import _start_pre_session_ib

    monkeypatch.setenv("MNQ_CONID", "770561201")
    monkeypatch.setenv("MES_CONID", "770561194")
    started = threading.Event()

    class FakeSource:
        def start(self):
            started.set()
            time.sleep(0.05)
        def stop(self): pass

    fake = FakeSource()
    with patch("data.ib_realtime.IbRealtimeSource", return_value=fake):
        src, thr = _start_pre_session_ib(tmp_path)

    assert src is fake
    assert thr is not None and thr.daemon
    assert started.wait(timeout=1.0), "source.start() never called in thread"


def test_start_pre_session_ib_returns_none_when_conid_not_set(tmp_path, monkeypatch):
    monkeypatch.delenv("MNQ_CONID", raising=False)
    monkeypatch.delenv("MES_CONID", raising=False)
    from orchestrator.main import _start_pre_session_ib
    src, thr = _start_pre_session_ib(tmp_path)
    assert src is None and thr is None


def test_stop_pre_session_ib_calls_stop_and_join(tmp_path):
    from orchestrator.main import _stop_pre_session_ib

    source = MagicMock()
    thread = MagicMock()
    thread.is_alive.return_value = True
    _stop_pre_session_ib(source, thread)
    source.stop.assert_called_once()
    thread.join.assert_called_once_with(timeout=15.0)


def test_stop_pre_session_ib_noop_when_source_none():
    from orchestrator.main import _stop_pre_session_ib
    _stop_pre_session_ib(None, None)  # must not raise


# ---------------------------------------------------------------------------
# _check_parquet_files tests
# ---------------------------------------------------------------------------

def test_check_parquet_files_returns_when_all_exist(tmp_path):
    """Returns immediately without prompting when all 4 parquets are present."""
    import pandas as pd
    empty = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )
    for fname in ["MNQ_1m.parquet", "MES_1m.parquet", "MNQ_1s.parquet", "MES_1s.parquet"]:
        empty.to_parquet(tmp_path / fname)

    with patch("builtins.input") as mock_input:
        _check_parquet_files(tmp_path)

    mock_input.assert_not_called()


def test_check_parquet_files_exits_10_when_non_tty_and_missing(tmp_path):
    """In non-TTY context (agent), exits with code 10 instead of prompting."""
    with patch("sys.stdin.isatty", return_value=False), \
         patch("builtins.input") as mock_input:
        with pytest.raises(SystemExit) as exc_info:
            _check_parquet_files(tmp_path)
    assert exc_info.value.code == 10
    mock_input.assert_not_called()


def test_check_parquet_files_option2_creates_empty_parquets(tmp_path):
    """Choosing option 2 creates empty parquet files for every missing file."""
    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="2"):
        _check_parquet_files(tmp_path)

    import pandas as pd
    for fname in ["MNQ_1m.parquet", "MES_1m.parquet", "MNQ_1s.parquet", "MES_1s.parquet"]:
        path = tmp_path / fname
        assert path.exists(), f"{fname} was not created"
        df = pd.read_parquet(path)
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert df.empty


def test_check_parquet_files_option2_only_creates_missing(tmp_path):
    """Option 2 only creates files that were missing, not ones that already exist."""
    import pandas as pd
    existing = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-05-01 09:30:00", tz="America/New_York")]),
    )
    existing.to_parquet(tmp_path / "MNQ_1m.parquet")
    existing.to_parquet(tmp_path / "MES_1m.parquet")
    # 1s files are missing

    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="2"):
        _check_parquet_files(tmp_path)

    # Existing 1m files must be untouched (still 1 row)
    assert len(pd.read_parquet(tmp_path / "MNQ_1m.parquet")) == 1
    # Missing 1s files must now be empty parquets
    assert (tmp_path / "MNQ_1s.parquet").exists()
    assert (tmp_path / "MES_1s.parquet").exists()


def test_check_parquet_files_option1_loops_until_files_copied(tmp_path):
    """Option 1 re-checks after the user presses Enter; returns when files appear."""
    import pandas as pd
    empty = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )

    files_created = [False]

    def fake_input(prompt=""):
        if "1 or 2" in prompt:
            return "1"
        # "Press Enter when files have been copied" prompt — create files now
        if not files_created[0]:
            for fname in ["MNQ_1m.parquet", "MES_1m.parquet", "MNQ_1s.parquet", "MES_1s.parquet"]:
                empty.to_parquet(tmp_path / fname)
            files_created[0] = True
        return ""

    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", side_effect=fake_input):
        _check_parquet_files(tmp_path)

    assert files_created[0]


def test_check_parquet_files_invalid_input_loops(tmp_path):
    """Invalid input re-prompts; eventually option 2 is accepted."""
    responses = iter(["x", "", "3", "2"])
    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", side_effect=responses):
        _check_parquet_files(tmp_path)

    for fname in ["MNQ_1m.parquet", "MES_1m.parquet", "MNQ_1s.parquet", "MES_1s.parquet"]:
        assert (tmp_path / fname).exists()


# ---------------------------------------------------------------------------
# _cli_check_parquets tests
# ---------------------------------------------------------------------------

def test_cli_check_parquets_exits_0_when_all_present(tmp_path, capsys):
    """--check-parquets exits 0 and reports empty missing list when all files exist."""
    import json, pandas as pd
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    empty = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )
    for fname in ["MNQ_1m.parquet", "MES_1m.parquet", "MNQ_1s.parquet", "MES_1s.parquet"]:
        empty.to_parquet(data_dir / fname)

    with patch("orchestrator.main.__file__", str(tmp_path / "orchestrator" / "main.py")), \
         pytest.raises(SystemExit) as exc_info:
        _cli_check_parquets()

    assert exc_info.value.code == 0
    data = json.loads(capsys.readouterr().out.strip())
    assert data == {"missing": []}


def test_cli_check_parquets_exits_1_when_files_missing(tmp_path, capsys):
    """--check-parquets exits 1 and lists missing files in JSON when data/ is empty."""
    import json
    (tmp_path / "data").mkdir()  # empty data dir — no parquets

    with patch("orchestrator.main.__file__", str(tmp_path / "orchestrator" / "main.py")), \
         pytest.raises(SystemExit) as exc_info:
        _cli_check_parquets()

    assert exc_info.value.code == 1
    data = json.loads(capsys.readouterr().out.strip())
    assert set(data["missing"]) == {
        "MNQ_1m.parquet", "MES_1m.parquet", "MNQ_1s.parquet", "MES_1s.parquet"
    }


def test_cli_create_empty_parquets_creates_all_missing(tmp_path, capsys):
    """--create-empty-parquets creates all 4 files when none exist."""
    import pandas as pd

    with patch("orchestrator.main.__file__",
               str(tmp_path / "orchestrator" / "main.py")):
        with pytest.raises(SystemExit) as exc_info:
            _cli_create_empty_parquets()

    assert exc_info.value.code == 0
    data_dir = tmp_path / "data"
    for fname in ["MNQ_1m.parquet", "MES_1m.parquet", "MNQ_1s.parquet", "MES_1s.parquet"]:
        assert (data_dir / fname).exists()
        df = pd.read_parquet(data_dir / fname)
        assert df.empty


def test_cli_create_empty_parquets_skips_existing(tmp_path):
    """--create-empty-parquets does not overwrite files that already have data."""
    import pandas as pd
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    existing = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-05-01 09:30:00", tz="America/New_York")]),
    )
    existing.to_parquet(data_dir / "MNQ_1m.parquet")

    with patch("orchestrator.main.__file__",
               str(tmp_path / "orchestrator" / "main.py")):
        with pytest.raises(SystemExit):
            _cli_create_empty_parquets()

    # Existing file must still have 1 row
    assert len(pd.read_parquet(data_dir / "MNQ_1m.parquet")) == 1
