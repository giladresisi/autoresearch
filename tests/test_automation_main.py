"""tests/test_automation_main.py — Unit tests for automation/main.py.

automation/main.py mirrors signal_smt.py's state machine but routes fills through
PickMyTradeExecutor (async). All tests heavily mock IbRealtimeSource and
PickMyTradeExecutor so no IB connection or HTTP request is ever attempted.
"""
import datetime
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_bar_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )


def _make_stub_ib_source() -> SimpleNamespace:
    return SimpleNamespace(
        mnq_1m_df=_empty_bar_df(),
        mes_1m_df=_empty_bar_df(),
    )


def _setup_scanning_state(monkeypatch, tmp_path):
    """Reset automation.main module state to a clean SCANNING configuration."""
    import automation.main as am
    import strategy_smt

    monkeypatch.setattr(strategy_smt, "MIN_BARS_BEFORE_SIGNAL", 2)
    monkeypatch.setattr(strategy_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(strategy_smt, "TDO_VALIDITY_CHECK", False)

    monkeypatch.setattr(am, "POSITION_FILE", tmp_path / "position.json")
    monkeypatch.setattr(am, "_state", "SCANNING")
    monkeypatch.setattr(am, "_position", None)
    monkeypatch.setattr(am, "_startup_ts", None)
    monkeypatch.setattr(
        am, "_last_exit_ts",
        pd.Timestamp("1970-01-01", tz="America/New_York"),
    )
    monkeypatch.setattr(am, "_session_start_time",
                        pd.Timestamp("2000-01-01 09:00").time())
    monkeypatch.setattr(am, "_session_end_time",
                        pd.Timestamp("2000-01-01 13:30").time())

    monkeypatch.setattr(am, "_ib_source", _make_stub_ib_source())

    _minute_ts = pd.Timestamp("2026-04-30 10:05:00", tz="America/New_York").floor("min")
    monkeypatch.setattr(am, "_mes_partial_1m", {
        "open": 20000.0, "high": 20005.0, "low": 19995.0, "close": 20000.0,
        "volume": 100.0, "minute_ts": _minute_ts,
    })

    _test_date = datetime.date(2026, 4, 30)
    monkeypatch.setattr(am, "_session_init_date", _test_date)
    monkeypatch.setattr(am, "_scan_state", strategy_smt.ScanState())
    monkeypatch.setattr(am, "_session_ctx", strategy_smt.SessionContext(
        day=_test_date, tdo=19900.0, midnight_open=None, overnight={},
        pdh=None, pdl=None, hyp_ctx=None, hyp_dir=None, bar_seconds=1.0,
    ))
    monkeypatch.setattr(am, "_session_mnq_rows", [])
    monkeypatch.setattr(am, "_session_mes_rows", [])
    monkeypatch.setattr(am, "_session_smt_cache", {
        "mes_h": float("nan"), "mes_l": float("nan"),
        "mnq_h": float("nan"), "mnq_l": float("nan"),
        "mes_ch": float("nan"), "mes_cl": float("nan"),
        "mnq_ch": float("nan"), "mnq_cl": float("nan"),
    })
    monkeypatch.setattr(am, "_session_run_ses_high", -float("inf"))
    monkeypatch.setattr(am, "_session_run_ses_low", float("inf"))
    monkeypatch.setattr(am, "_current_session_date", _test_date)
    monkeypatch.setattr(am, "_current_divergence_level", None)
    monkeypatch.setattr(am, "_divergence_reentry_count", 0)
    monkeypatch.setattr(am, "_hypothesis_manager", None)
    monkeypatch.setattr(am, "_hypothesis_generated", True)


def _setup_managing_state(monkeypatch, tmp_path, direction="long"):
    """Reset automation.main to MANAGING with an open position."""
    import automation.main as am
    monkeypatch.setattr(am, "POSITION_FILE", tmp_path / "position.json")
    monkeypatch.setattr(am, "_state", "MANAGING")
    monkeypatch.setattr(am, "_session_start_time",
                        pd.Timestamp("2000-01-01 09:00").time())
    monkeypatch.setattr(am, "_session_end_time",
                        pd.Timestamp("2000-01-01 13:30").time())
    monkeypatch.setattr(am, "_last_exit_ts",
                        pd.Timestamp("1970-01-01", tz="America/New_York"))
    monkeypatch.setattr(am, "_last_move_stop_bar_idx", -10**9)
    monkeypatch.setattr(am, "_move_stop_bar_counter", 0)
    monkeypatch.setattr(am, "_hypothesis_manager", None)
    monkeypatch.setattr(am, "_scan_state", None)

    pos = {
        "direction":     direction,
        "entry_price":   20000.0,
        "assumed_entry": 20000.5 if direction == "long" else 19999.5,
        "take_profit":   20100.0 if direction == "long" else 19900.0,
        "stop_price":    19950.0 if direction == "long" else 20050.0,
        "tdo":           20100.0 if direction == "long" else 19900.0,
        "contracts":     1,
        "instrument":    "MNQ1!",
        "entry_time":    "2026-04-30 10:00:00-04:00",
        "tp_breached":   False,
    }
    monkeypatch.setattr(am, "_position", pos)
    return pos


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_main_validates_env_vars_before_start(monkeypatch, tmp_path):
    """main() raises RuntimeError when PMT_WEBHOOK_URL or PMT_API_KEY is missing."""
    monkeypatch.delenv("PMT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("PMT_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    with patch("automation.main.IbRealtimeSource") as MockIb, \
         patch("automation.main.PickMyTradeExecutor") as MockPMT, \
         patch("automation.main.HypothesisManager"), \
         patch("automation.main._load_hist_mnq", return_value=pd.DataFrame()):
        MockIb.return_value = MagicMock()
        MockPMT.return_value = MagicMock()

        import automation.main as am
        with pytest.raises(RuntimeError, match="Missing required env vars"):
            am.main()


def test_executor_started_before_ib_source(monkeypatch, tmp_path):
    """executor.start() must run before ib_source.start() so fill listener is ready."""
    monkeypatch.setenv("PMT_WEBHOOK_URL", "https://example.com")
    monkeypatch.setenv("PMT_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)

    call_order: list[str] = []

    mock_executor = MagicMock()
    mock_executor.start.side_effect = lambda: call_order.append("executor.start")
    mock_executor.stop.side_effect = lambda: call_order.append("executor.stop")

    mock_ib = MagicMock()
    mock_ib.start.side_effect = lambda: call_order.append("ib.start")

    with patch("automation.main.PickMyTradeExecutor", return_value=mock_executor), \
         patch("automation.main.IbRealtimeSource", return_value=mock_ib), \
         patch("automation.main.HypothesisManager"), \
         patch("automation.main._load_hist_mnq", return_value=pd.DataFrame()):
        import automation.main as am
        # Ensure no leftover position file from previous tests
        monkeypatch.setattr(am, "POSITION_FILE", tmp_path / "live_position.json")
        am.main()

    assert "executor.start" in call_order
    assert "ib.start" in call_order
    assert call_order.index("executor.start") < call_order.index("ib.start")


def test_executor_stopped_in_finally_on_ib_error(monkeypatch, tmp_path):
    """If ib_source.start() raises, executor.stop() must still be called via finally."""
    monkeypatch.setenv("PMT_WEBHOOK_URL", "https://example.com")
    monkeypatch.setenv("PMT_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)

    mock_executor = MagicMock()
    mock_ib = MagicMock()
    mock_ib.start.side_effect = RuntimeError("IB connection failed")

    with patch("automation.main.PickMyTradeExecutor", return_value=mock_executor), \
         patch("automation.main.IbRealtimeSource", return_value=mock_ib), \
         patch("automation.main.HypothesisManager"), \
         patch("automation.main._load_hist_mnq", return_value=pd.DataFrame()):
        import automation.main as am
        monkeypatch.setattr(am, "POSITION_FILE", tmp_path / "live_position.json")
        with pytest.raises(RuntimeError, match="IB connection failed"):
            am.main()

    mock_executor.stop.assert_called_once()


def test_place_entry_called_on_signal_detection(monkeypatch, tmp_path, capsys):
    """When process_scan_bar emits a signal, executor.place_entry is invoked."""
    import automation.main as am
    import strategy_smt
    _setup_scanning_state(monkeypatch, tmp_path)

    mock_executor = MagicMock()
    mock_executor.place_entry.return_value = None  # async fill
    monkeypatch.setattr(am, "_executor", mock_executor)

    test_ts = pd.Timestamp("2026-04-30 10:05:00", tz="America/New_York")

    fake_signal = {
        "type":         "signal",
        "direction":    "long",
        "entry_price":  20000.0,
        "stop_price":   19980.0,
        "take_profit":  20040.0,
        "entry_time":   test_ts,
        "smt_type":     "wick",
    }

    monkeypatch.setattr(strategy_smt, "process_scan_bar",
                        lambda *a, **kw: fake_signal)
    monkeypatch.setattr(am, "compute_tdo", lambda df, date: 19900.0)

    bar = strategy_smt._BarRow(20000.0, 20005.0, 19995.0, 20000.0, 100.0, test_ts)
    am._process_scanning(bar, test_ts, test_ts.time())

    mock_executor.place_entry.assert_called_once()
    assert am._state == "MANAGING"
    # Drain captured stdout so it doesn't leak into other tests
    capsys.readouterr()


def test_place_exit_called_on_managing_exit(monkeypatch, tmp_path, capsys):
    """When manage_position returns an exit, executor.place_exit is invoked."""
    import automation.main as am
    import strategy_smt
    pos = _setup_managing_state(monkeypatch, tmp_path, direction="long")

    pos_file = tmp_path / "position.json"
    pos_file.write_text(json.dumps(pos))
    monkeypatch.setattr(am, "POSITION_FILE", pos_file)

    mock_executor = MagicMock()
    mock_executor.place_exit.return_value = None
    monkeypatch.setattr(am, "_executor", mock_executor)

    monkeypatch.setattr(am, "manage_position", lambda p, b: "exit_tp")

    test_ts = pd.Timestamp("2026-04-30 10:30:00", tz="America/New_York")
    bar = strategy_smt._BarRow(20100.0, 20105.0, 20095.0, 20100.0, 100.0, test_ts)
    am._process_managing(bar, test_ts, test_ts.time())

    mock_executor.place_exit.assert_called_once()
    args = mock_executor.place_exit.call_args
    # Second positional arg is exit_type
    assert args[0][1] == "exit_tp"
    assert am._state == "SCANNING"
    capsys.readouterr()


def test_assumed_entry_falls_back_to_signal_price_when_fill_is_none(monkeypatch, tmp_path, capsys):
    """When place_entry returns None (async), assumed_entry == signal['entry_price']."""
    import automation.main as am
    import strategy_smt
    _setup_scanning_state(monkeypatch, tmp_path)

    mock_executor = MagicMock()
    mock_executor.place_entry.return_value = None
    monkeypatch.setattr(am, "_executor", mock_executor)

    test_ts = pd.Timestamp("2026-04-30 10:05:00", tz="America/New_York")

    fake_signal = {
        "type":         "signal",
        "direction":    "long",
        "entry_price":  20000.0,
        "stop_price":   19980.0,
        "take_profit":  20040.0,
        "entry_time":   test_ts,
        "smt_type":     "wick",
    }

    monkeypatch.setattr(strategy_smt, "process_scan_bar",
                        lambda *a, **kw: fake_signal)
    monkeypatch.setattr(am, "compute_tdo", lambda df, date: 19900.0)

    bar = strategy_smt._BarRow(20000.0, 20005.0, 19995.0, 20000.0, 100.0, test_ts)
    am._process_scanning(bar, test_ts, test_ts.time())

    assert am._position is not None
    assert am._position["assumed_entry"] == pytest.approx(20000.0)
    capsys.readouterr()


def test_exit_price_falls_back_to_bar_close_when_fill_is_none(monkeypatch, tmp_path, capsys):
    """When place_exit returns None, the EXIT JSON line uses bar.Close as exit price."""
    import automation.main as am
    import strategy_smt
    pos = _setup_managing_state(monkeypatch, tmp_path, direction="long")

    pos_file = tmp_path / "position.json"
    pos_file.write_text(json.dumps(pos))
    monkeypatch.setattr(am, "POSITION_FILE", pos_file)

    mock_executor = MagicMock()
    mock_executor.place_exit.return_value = None  # async fill
    monkeypatch.setattr(am, "_executor", mock_executor)

    monkeypatch.setattr(am, "manage_position", lambda p, b: "exit_tp")

    test_ts = pd.Timestamp("2026-04-30 10:30:00", tz="America/New_York")
    bar_close = 20097.25
    bar = strategy_smt._BarRow(20100.0, 20105.0, 20095.0, bar_close, 100.0, test_ts)
    am._process_managing(bar, test_ts, test_ts.time())

    captured = capsys.readouterr()
    # Find the EXIT JSON line
    exit_line = None
    for line in captured.out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("signal_type") == "EXIT":
            exit_line = obj
            break

    assert exit_line is not None, f"No EXIT JSON line found in stdout: {captured.out!r}"
    assert exit_line["exit_price"] == pytest.approx(bar_close)


def test_automation_uses_correct_ib_client_id(monkeypatch, tmp_path):
    """IB_CLIENT_ID is read from AUTOMATION_IB_CLIENT_ID env var (default 20)."""
    monkeypatch.setenv("PMT_WEBHOOK_URL", "https://example.com")
    monkeypatch.setenv("PMT_API_KEY", "test-key")
    monkeypatch.setenv("AUTOMATION_IB_CLIENT_ID", "25")
    monkeypatch.chdir(tmp_path)

    captured_kwargs: list[dict] = []

    def mock_ib_init(*args, **kwargs):
        captured_kwargs.append(kwargs)
        mock = MagicMock()
        return mock

    # Reload BEFORE applying patches so module-level IB_CLIENT_ID picks up env var
    import automation.main as am
    am = importlib.reload(am)

    with patch("automation.main.PickMyTradeExecutor"), \
         patch("automation.main.HypothesisManager"), \
         patch("automation.main._load_hist_mnq", return_value=pd.DataFrame()), \
         patch("automation.main.IbRealtimeSource", side_effect=mock_ib_init):
        monkeypatch.setattr(am, "POSITION_FILE", tmp_path / "live_position.json")
        am.main()

    assert am.IB_CLIENT_ID == 25
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["client_id"] == 25


def test_signal_json_line_emitted_to_stdout(monkeypatch, tmp_path, capsys):
    """After a signal fires, a JSON line for the human-trader payload is emitted."""
    import automation.main as am
    import strategy_smt
    _setup_scanning_state(monkeypatch, tmp_path)

    # Force human-mode so the typed JSON payload is emitted alongside the log line
    monkeypatch.setattr(strategy_smt, "HUMAN_EXECUTION_MODE", True)
    monkeypatch.setattr(strategy_smt, "MIN_CONFIDENCE_THRESHOLD", 0.0)

    mock_executor = MagicMock()
    mock_executor.place_entry.return_value = None
    monkeypatch.setattr(am, "_executor", mock_executor)

    test_ts = pd.Timestamp("2026-04-30 10:05:00", tz="America/New_York")

    fake_signal = {
        "type":         "signal",
        "direction":    "long",
        "entry_price":  20000.0,
        "stop_price":   19980.0,
        "take_profit":  20040.0,
        "entry_time":   test_ts,
        "smt_type":     "wick",
        "confidence":   0.85,
    }

    monkeypatch.setattr(strategy_smt, "process_scan_bar",
                        lambda *a, **kw: fake_signal)
    monkeypatch.setattr(am, "compute_tdo", lambda df, date: 19900.0)

    bar = strategy_smt._BarRow(20000.0, 20005.0, 19995.0, 20000.0, 100.0, test_ts)
    am._process_scanning(bar, test_ts, test_ts.time())

    captured = capsys.readouterr()
    # At least one JSON-decodable line should appear
    json_lines = []
    for line in captured.out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            json_lines.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    assert json_lines, f"No JSON line found in stdout: {captured.out!r}"
    # At least one of the JSON lines should describe an entry signal
    entry_payloads = [j for j in json_lines if j.get("signal_type") in ("ENTRY_LIMIT", "ENTRY_MARKET")]
    assert entry_payloads, f"No ENTRY_* signal_type JSON found in: {json_lines}"


def test_exit_json_line_emitted_to_stdout(monkeypatch, tmp_path, capsys):
    """After an exit fires via _process_managing, an EXIT JSON line is emitted."""
    import automation.main as am
    import strategy_smt
    pos = _setup_managing_state(monkeypatch, tmp_path, direction="long")

    pos_file = tmp_path / "position.json"
    pos_file.write_text(json.dumps(pos))
    monkeypatch.setattr(am, "POSITION_FILE", pos_file)

    mock_executor = MagicMock()
    mock_executor.place_exit.return_value = None
    monkeypatch.setattr(am, "_executor", mock_executor)

    monkeypatch.setattr(am, "manage_position", lambda p, b: "exit_tp")

    test_ts = pd.Timestamp("2026-04-30 10:30:00", tz="America/New_York")
    bar = strategy_smt._BarRow(20100.0, 20105.0, 20095.0, 20100.0, 100.0, test_ts)
    am._process_managing(bar, test_ts, test_ts.time())

    captured = capsys.readouterr()
    found_exit = False
    for line in captured.out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("signal_type") == "EXIT":
            found_exit = True
            assert obj["exit_reason"] == "exit_tp"
            assert obj["direction"] == "long"
            break

    assert found_exit, f"No EXIT JSON line found in stdout: {captured.out!r}"


def test_fills_path_in_session_directory(monkeypatch, tmp_path):
    """fills.jsonl path passed to PickMyTradeExecutor lives under sessions/YYYY-MM-DD/."""
    monkeypatch.setenv("PMT_WEBHOOK_URL", "https://example.com")
    monkeypatch.setenv("PMT_API_KEY", "test-key")
    sessions_root = tmp_path / "sessions_root"
    monkeypatch.setenv("SESSIONS_DIR", str(sessions_root))
    monkeypatch.chdir(tmp_path)

    captured_kwargs: list[dict] = []

    def mock_pmt_init(**kwargs):
        captured_kwargs.append(kwargs)
        return MagicMock()

    # Reload BEFORE applying patches so module-level SESSIONS_DIR picks up env var.
    # (patch() patches the existing module attribute; a subsequent reload would
    # rebind it to the real import and silently bypass the patch.)
    import automation.main as am
    am = importlib.reload(am)

    with patch("automation.main.IbRealtimeSource") as MockIb, \
         patch("automation.main.HypothesisManager"), \
         patch("automation.main._load_hist_mnq", return_value=pd.DataFrame()), \
         patch("automation.main.PickMyTradeExecutor", side_effect=mock_pmt_init):
        MockIb.return_value = MagicMock()
        monkeypatch.setattr(am, "POSITION_FILE", tmp_path / "live_position.json")
        am.main()

    assert len(captured_kwargs) == 1
    fills_path = captured_kwargs[0].get("fills_path")
    assert fills_path is not None
    fills_path = Path(fills_path)
    assert fills_path.name == "fills.jsonl"
    # Parent dir should be sessions_root/YYYY-MM-DD/
    assert fills_path.parent.parent == sessions_root, (
        f"fills_path={fills_path} sessions_root={sessions_root}"
    )
    # Date dir name should be parseable as YYYY-MM-DD
    datetime.datetime.strptime(fills_path.parent.name, "%Y-%m-%d")
    # Parent dir should have been created
    assert fills_path.parent.exists()
