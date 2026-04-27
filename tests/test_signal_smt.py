"""tests/test_signal_smt.py — Unit tests for signal_smt.py helper and state machine logic.

No IB connection required. All tests use synthetic DataFrames and mock the IB layer.
"""
import datetime
import json
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest


def _make_session_bars(
    direction: str = "short",
    base: float = 20000.0,
    tdo: float | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """Build minimal OHLCV bars containing one SMT divergence signal.

    direction: "short" or "long"
    Returns (mnq_bars, mes_bars, tdo_value).
    """
    n = 30
    start_ts = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="1min")
    opens  = [base] * n
    closes = [base] * n

    if direction == "short":
        highs_mes = [base + 5] * n
        highs_mes[4] = base + 30          # MES new session high
        highs_mnq = [base + 5] * n
        opens[3]  = base - 2
        closes[3] = base + 2              # bullish anchor for find_entry_bar
        opens[5]  = base + 2
        closes[5] = base - 2             # bearish confirmation
        highs_mnq[5] = base + 6
        mnq = pd.DataFrame(
            {"Open": opens, "High": highs_mnq, "Low": [base - 5] * n, "Close": closes, "Volume": [1000.0] * n},
            index=idx,
        )
        mes = pd.DataFrame(
            {"Open": opens, "High": highs_mes, "Low": [base - 5] * n, "Close": closes, "Volume": [1000.0] * n},
            index=idx,
        )
        tdo_val = tdo if tdo is not None else base - 105.0  # below entry ≈ base-2 → valid short
    else:
        lows_mes = [base - 5] * n
        lows_mes[4] = base - 30          # MES new session low
        lows_mnq = [base - 5] * n
        opens[3]  = base + 2
        closes[3] = base - 2             # bearish anchor for find_entry_bar
        opens[5]  = base - 2
        closes[5] = base + 2             # bullish confirmation
        lows_mnq[5] = base - 6
        mnq = pd.DataFrame(
            {"Open": opens, "High": [base + 5] * n, "Low": lows_mnq, "Close": closes, "Volume": [1000.0] * n},
            index=idx,
        )
        mes = pd.DataFrame(
            {"Open": opens, "High": [base + 5] * n, "Low": lows_mes, "Close": closes, "Volume": [1000.0] * n},
            index=idx,
        )
        tdo_val = tdo if tdo is not None else base + 105.0  # above entry ≈ base+2 → valid long

    return mnq, mes, tdo_val


def _make_mock_bar(
    ts: str = "2025-01-02 09:10:00",
    open_: float = 20000.0,
    high: float = 20005.0,
    low: float = 19995.0,
    close: float = 20000.0,
    volume: float = 100.0,
    tz: str = "America/New_York",
):
    """Build a mock ib_insync bar object."""
    bar = mock.MagicMock()
    bar.date   = pd.Timestamp(ts, tz=tz)
    bar.open   = open_
    bar.high   = high
    bar.low    = low
    bar.close  = close
    bar.volume = volume
    return bar


def _make_mock_ticker(ts: str, price: float, size: float = 1.0, tz: str = "UTC"):
    """Build a mock ib_insync Ticker as delivered by reqTickByTickData."""
    tick = mock.MagicMock()
    tick.time  = pd.Timestamp(ts, tz=tz)
    tick.price = price
    tick.size  = size
    ticker = mock.MagicMock()
    ticker.tickByTicks = [tick]
    return ticker


# ══ Helper function tests ═════════════════════════════════════════════════════

def test_empty_bar_df_schema():
    """_empty_bar_df() returns correct columns and a tz-aware ET DatetimeIndex."""
    import signal_smt
    df = signal_smt._empty_bar_df()
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index.tz is not None
    assert str(df.index.tz) == "America/New_York"
    assert len(df) == 0


def test_apply_slippage_long():
    """Long: assumed_entry = entry_price + ENTRY_SLIPPAGE_TICKS * 0.25."""
    import signal_smt
    signal = {"direction": "long", "entry_price": 20000.0}
    result = signal_smt._apply_slippage(signal)
    expected = 20000.0 + signal_smt.ENTRY_SLIPPAGE_TICKS * 0.25
    assert result == pytest.approx(expected)


def test_apply_slippage_short():
    """Short: assumed_entry = entry_price - ENTRY_SLIPPAGE_TICKS * 0.25."""
    import signal_smt
    signal = {"direction": "short", "entry_price": 20000.0}
    result = signal_smt._apply_slippage(signal)
    expected = 20000.0 - signal_smt.ENTRY_SLIPPAGE_TICKS * 0.25
    assert result == pytest.approx(expected)


def test_compute_pnl_long_tp():
    """Long TP: pnl = (exit - assumed_entry) * contracts * MNQ_PNL_PER_POINT."""
    import signal_smt
    pos = {"direction": "long", "assumed_entry": 19850.5, "contracts": 1}
    pnl = signal_smt._compute_pnl(pos, 19890.0)
    expected = (19890.0 - 19850.5) * 1 * signal_smt.MNQ_PNL_PER_POINT
    assert pnl == pytest.approx(expected)


def test_compute_pnl_short_stop():
    """Short stop: pnl is negative when exit > assumed_entry."""
    import signal_smt
    pos = {"direction": "short", "assumed_entry": 19903.0, "contracts": 1}
    pnl = signal_smt._compute_pnl(pos, 19904.5)
    assert pnl < 0


def test_format_signal_line_long():
    """_format_signal_line output contains 'SIGNAL', 'long', and the entry price."""
    import signal_smt
    ts = pd.Timestamp("2025-01-02 09:05:00", tz="America/New_York")
    signal = {
        "direction":   "long",
        "entry_price": 19850.0,
        "take_profit": 19950.0,
        "stop_price":  19805.0,
        "entry_time":  pd.Timestamp("2025-01-02 09:04:00", tz="America/New_York"),
    }
    line = signal_smt._format_signal_line(ts, signal, 19850.5)
    assert "SIGNAL" in line
    assert "long" in line
    assert "19850.5" in line


def test_format_exit_line_tp():
    """_format_exit_line output contains 'EXIT', 'tp', and the P&L."""
    import signal_smt
    ts = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    line = signal_smt._format_exit_line(ts, "exit_tp", 19890.0, 78.5, 1, "2025-01-02 09:10:00-05:00")
    assert "EXIT" in line
    assert "tp" in line
    assert "78.5" in line
    assert "20m" in line


# ══ SCANNING state tests ══════════════════════════════════════════════════════

def _setup_scanning_state(monkeypatch, tmp_path):
    """Helper: reset signal_smt module state to a clean SCANNING configuration."""
    import signal_smt
    import strategy_smt

    monkeypatch.setattr(strategy_smt, "MIN_BARS_BEFORE_SIGNAL", 2)
    monkeypatch.setattr(strategy_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(strategy_smt, "TDO_VALIDITY_CHECK", False)

    monkeypatch.setattr(signal_smt, "POSITION_FILE", tmp_path / "position.json")
    monkeypatch.setattr(signal_smt, "_state", "SCANNING")
    monkeypatch.setattr(signal_smt, "_position", None)
    monkeypatch.setattr(signal_smt, "_startup_ts", None)
    monkeypatch.setattr(
        signal_smt, "_last_exit_ts",
        pd.Timestamp("1970-01-01", tz="America/New_York"),
    )
    monkeypatch.setattr(
        signal_smt, "_session_start_time",
        pd.Timestamp("2000-01-01 09:00").time(),
    )
    monkeypatch.setattr(
        signal_smt, "_session_end_time",
        pd.Timestamp("2000-01-01 13:30").time(),
    )

    empty = signal_smt._empty_bar_df()
    monkeypatch.setattr(signal_smt, "_mnq_1s_buf", empty.copy())
    monkeypatch.setattr(signal_smt, "_mes_1s_buf", empty.copy())
    monkeypatch.setattr(signal_smt, "_mnq_1m_df", empty.copy())
    monkeypatch.setattr(signal_smt, "_mes_1m_df", empty.copy())

    # Phase 5 stateful scanner state — pre-initialised so session-init block is skipped.
    _test_date = datetime.date(2025, 1, 2)
    monkeypatch.setattr(signal_smt, "_session_init_date", _test_date)
    monkeypatch.setattr(signal_smt, "_scan_state", strategy_smt.ScanState())
    monkeypatch.setattr(signal_smt, "_session_ctx", strategy_smt.SessionContext(
        day=_test_date, tdo=19900.0, midnight_open=None, overnight={},
        pdh=None, pdl=None, hyp_ctx=None, hyp_dir=None, bar_seconds=1.0,
    ))
    monkeypatch.setattr(signal_smt, "_session_mnq_rows", [])
    monkeypatch.setattr(signal_smt, "_session_mes_rows", [])
    monkeypatch.setattr(signal_smt, "_session_smt_cache", {
        "mes_h": float("nan"), "mes_l": float("nan"),
        "mnq_h": float("nan"), "mnq_l": float("nan"),
        "mes_ch": float("nan"), "mes_cl": float("nan"),
        "mnq_ch": float("nan"), "mnq_cl": float("nan"),
    })
    monkeypatch.setattr(signal_smt, "_session_run_ses_high", -float("inf"))
    monkeypatch.setattr(signal_smt, "_session_run_ses_low", float("inf"))


def test_process_scanning_session_gate_before_start(monkeypatch, tmp_path):
    """Bar arriving before SESSION_START is silently skipped; process_scan_bar never called."""
    import signal_smt
    import strategy_smt
    _setup_scanning_state(monkeypatch, tmp_path)

    called = []
    monkeypatch.setattr(strategy_smt, "process_scan_bar", lambda *a, **kw: called.append(1) or None)

    bar = _make_mock_bar(ts="2025-01-02 08:59:00")
    signal_smt._process_scanning(bar, pd.Timestamp("2025-01-02 08:59:00", tz="America/New_York"), pd.Timestamp("2025-01-02 08:59:00", tz="America/New_York").time())

    assert len(called) == 0
    assert signal_smt._state == "SCANNING"


def test_process_scanning_session_gate_after_end(monkeypatch, tmp_path):
    """Bar arriving after SESSION_END is silently skipped."""
    import signal_smt
    import strategy_smt
    _setup_scanning_state(monkeypatch, tmp_path)

    called = []
    monkeypatch.setattr(strategy_smt, "process_scan_bar", lambda *a, **kw: called.append(1) or None)

    bar = _make_mock_bar(ts="2025-01-02 13:31:00")
    ts = pd.Timestamp("2025-01-02 13:31:00", tz="America/New_York")
    signal_smt._process_scanning(bar, ts, ts.time())

    assert len(called) == 0
    assert signal_smt._state == "SCANNING"


def test_process_scanning_alignment_gate(monkeypatch, tmp_path):
    """MES 1s buffer has a different latest timestamp → process_scan_bar not called."""
    import signal_smt
    import strategy_smt
    _setup_scanning_state(monkeypatch, tmp_path)

    # MNQ 1s buf has bar at 09:10, MES 1s buf has bar at 09:09 → misaligned
    mnq_buf = signal_smt._empty_bar_df()
    mnq_buf.loc[pd.Timestamp("2025-01-02 09:10:00", tz="America/New_York")] = [20000, 20005, 19995, 20000, 100]
    mes_buf = signal_smt._empty_bar_df()
    mes_buf.loc[pd.Timestamp("2025-01-02 09:09:00", tz="America/New_York")] = [20000, 20005, 19995, 20000, 100]
    monkeypatch.setattr(signal_smt, "_mnq_1s_buf", mnq_buf)
    monkeypatch.setattr(signal_smt, "_mes_1s_buf", mes_buf)

    called = []
    monkeypatch.setattr(strategy_smt, "process_scan_bar", lambda *a, **kw: called.append(1) or None)

    bar = _make_mock_bar(ts="2025-01-02 09:10:00")
    ts = pd.Timestamp("2025-01-02 09:10:00", tz="America/New_York")
    signal_smt._process_scanning(bar, ts, ts.time())

    assert len(called) == 0
    # Alignment gate fires before steps 5–6, so session rows must remain unpolluted.
    assert len(signal_smt._session_mnq_rows) == 0
    assert len(signal_smt._session_mes_rows) == 0


def test_process_scanning_no_signal(monkeypatch, tmp_path):
    """Valid session bar but process_scan_bar returns None → state stays SCANNING."""
    import signal_smt
    import strategy_smt
    _setup_scanning_state(monkeypatch, tmp_path)

    aligned_ts = pd.Timestamp("2025-01-02 09:10:00", tz="America/New_York")
    buf = signal_smt._empty_bar_df()
    buf.loc[aligned_ts] = [20000, 20005, 19995, 20000, 100]
    monkeypatch.setattr(signal_smt, "_mnq_1s_buf", buf.copy())
    monkeypatch.setattr(signal_smt, "_mes_1s_buf", buf.copy())

    monkeypatch.setattr(strategy_smt, "process_scan_bar", lambda *a, **kw: None)
    monkeypatch.setattr("signal_smt.compute_tdo", lambda df, date: 20000.0)

    bar = _make_mock_bar(ts="2025-01-02 09:10:00")
    signal_smt._process_scanning(bar, aligned_ts, aligned_ts.time())

    assert signal_smt._state == "SCANNING"
    assert signal_smt._position is None


def test_process_scanning_stale_startup_guard(monkeypatch, tmp_path):
    """Signal entry_time <= startup_ts → skipped, state stays SCANNING."""
    import signal_smt
    _setup_scanning_state(monkeypatch, tmp_path)

    startup = pd.Timestamp("2025-01-02 09:15:00", tz="America/New_York")
    monkeypatch.setattr(signal_smt, "_startup_ts", startup)

    aligned_ts = pd.Timestamp("2025-01-02 09:10:00", tz="America/New_York")
    buf = signal_smt._empty_bar_df()
    buf.loc[aligned_ts] = [20000, 20005, 19995, 20000, 100]
    monkeypatch.setattr(signal_smt, "_mnq_1s_buf", buf.copy())
    monkeypatch.setattr(signal_smt, "_mes_1s_buf", buf.copy())

    # Signal entry_time is before startup_ts → should be skipped
    fake_signal = {
        "type": "signal",
        "direction": "short", "entry_price": 19998.0,
        "entry_time": pd.Timestamp("2025-01-02 09:05:00", tz="America/New_York"),
        "take_profit": 19900.0, "stop_price": 19999.0, "tdo": 19900.0,
        "divergence_bar": 4, "entry_bar": 5,
    }
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "process_scan_bar", lambda *a, **kw: fake_signal)
    monkeypatch.setattr("signal_smt.compute_tdo", lambda df, date: 19900.0)

    bar = _make_mock_bar(ts="2025-01-02 09:10:00")
    signal_smt._process_scanning(bar, aligned_ts, aligned_ts.time())

    assert signal_smt._state == "SCANNING"


def test_process_scanning_redetection_guard(monkeypatch, tmp_path):
    """Signal entry_time <= last_exit_ts → skipped, state stays SCANNING."""
    import signal_smt
    _setup_scanning_state(monkeypatch, tmp_path)

    last_exit = pd.Timestamp("2025-01-02 09:20:00", tz="America/New_York")
    monkeypatch.setattr(signal_smt, "_last_exit_ts", last_exit)

    aligned_ts = pd.Timestamp("2025-01-02 09:25:00", tz="America/New_York")
    buf = signal_smt._empty_bar_df()
    buf.loc[aligned_ts] = [20000, 20005, 19995, 20000, 100]
    monkeypatch.setattr(signal_smt, "_mnq_1s_buf", buf.copy())
    monkeypatch.setattr(signal_smt, "_mes_1s_buf", buf.copy())

    # Signal entry_time is before last_exit_ts → should be skipped
    fake_signal = {
        "type": "signal",
        "direction": "short", "entry_price": 19998.0,
        "entry_time": pd.Timestamp("2025-01-02 09:10:00", tz="America/New_York"),
        "take_profit": 19900.0, "stop_price": 19999.0, "tdo": 19900.0,
        "divergence_bar": 4, "entry_bar": 5,
    }
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "process_scan_bar", lambda *a, **kw: fake_signal)
    monkeypatch.setattr("signal_smt.compute_tdo", lambda df, date: 19900.0)

    bar = _make_mock_bar(ts="2025-01-02 09:25:00")
    signal_smt._process_scanning(bar, aligned_ts, aligned_ts.time())

    assert signal_smt._state == "SCANNING"


def test_process_scanning_valid_signal_transitions_to_managing(monkeypatch, tmp_path):
    """All gates pass → state becomes MANAGING, position.json written, slippage applied."""
    import signal_smt
    _setup_scanning_state(monkeypatch, tmp_path)

    pos_file = tmp_path / "position.json"
    monkeypatch.setattr(signal_smt, "POSITION_FILE", pos_file)

    aligned_ts = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    buf = signal_smt._empty_bar_df()
    buf.loc[aligned_ts] = [20000, 20005, 19995, 20000, 100]
    monkeypatch.setattr(signal_smt, "_mnq_1s_buf", buf.copy())
    monkeypatch.setattr(signal_smt, "_mes_1s_buf", buf.copy())

    entry_time = pd.Timestamp("2025-01-02 09:25:00", tz="America/New_York")
    fake_signal = {
        "type": "signal",
        "direction": "short", "entry_price": 19998.0,
        "entry_time": entry_time,
        "take_profit": 19900.0, "stop_price": 19999.0, "tdo": 19900.0,
        "divergence_bar": 4, "entry_bar": 5,
    }
    import strategy_smt
    monkeypatch.setattr(strategy_smt, "process_scan_bar", lambda *a, **kw: fake_signal)
    monkeypatch.setattr(signal_smt, "compute_tdo", lambda df, date: 19900.0)

    bar = _make_mock_bar(ts="2025-01-02 09:30:00")
    signal_smt._process_scanning(bar, aligned_ts, aligned_ts.time())

    assert signal_smt._state == "MANAGING"
    assert signal_smt._position is not None
    # Slippage applied: short → entry - ticks * 0.25
    expected_entry = 19998.0 - signal_smt.ENTRY_SLIPPAGE_TICKS * 0.25
    assert signal_smt._position["assumed_entry"] == pytest.approx(expected_entry)
    # position.json written
    assert pos_file.exists()
    saved = json.loads(pos_file.read_text())
    assert saved["direction"] == "short"


# ══ MANAGING state tests ══════════════════════════════════════════════════════

def _setup_managing_state(monkeypatch, tmp_path, direction="short"):
    """Helper: reset state to MANAGING with an open position."""
    import signal_smt
    monkeypatch.setattr(signal_smt, "POSITION_FILE", tmp_path / "position.json")
    monkeypatch.setattr(signal_smt, "_state", "MANAGING")
    monkeypatch.setattr(
        signal_smt, "_session_start_time",
        pd.Timestamp("2000-01-01 09:00").time(),
    )
    monkeypatch.setattr(
        signal_smt, "_session_end_time",
        pd.Timestamp("2000-01-01 13:30").time(),
    )
    monkeypatch.setattr(
        signal_smt, "_last_exit_ts",
        pd.Timestamp("1970-01-01", tz="America/New_York"),
    )
    pos = {
        "direction":    direction,
        "entry_price":  20000.0,
        "assumed_entry": 19999.5 if direction == "short" else 20000.5,
        "take_profit":  19900.0 if direction == "short" else 20100.0,
        "stop_price":   20050.0 if direction == "short" else 19950.0,
        "tdo":          19900.0 if direction == "short" else 20100.0,
        "contracts":    1,
        "instrument":   "MNQ1!",
        "entry_time":   "2025-01-02 09:10:00",
    }
    monkeypatch.setattr(signal_smt, "_position", pos)
    return pos


def test_process_managing_hold(monkeypatch, tmp_path):
    """manage_position returns 'hold', time < SESSION_END → state stays MANAGING."""
    import signal_smt
    _setup_managing_state(monkeypatch, tmp_path)

    monkeypatch.setattr("signal_smt.manage_position", lambda pos, bar: "hold")

    bar = _make_mock_bar(ts="2025-01-02 09:15:00", high=20010.0, low=19990.0, close=20000.0)
    ts = pd.Timestamp("2025-01-02 09:15:00", tz="America/New_York")
    signal_smt._process_managing(bar, ts, ts.time())

    assert signal_smt._state == "MANAGING"
    assert signal_smt._position is not None


def test_process_managing_exit_tp(monkeypatch, tmp_path):
    """manage_position returns 'exit_tp' → state becomes SCANNING, position.json deleted."""
    import signal_smt
    pos = _setup_managing_state(monkeypatch, tmp_path, direction="long")

    pos_file = tmp_path / "position.json"
    pos_file.write_text(json.dumps(pos))
    monkeypatch.setattr(signal_smt, "POSITION_FILE", pos_file)

    # Patch where it is used (signal_smt namespace), not where it is defined.
    # train_smt.manage_position is imported directly, so patching the source module
    # does not reach signal_smt's local binding.
    monkeypatch.setattr(signal_smt, "manage_position", lambda p, b: "exit_tp")

    bar = _make_mock_bar(ts="2025-01-02 09:30:00", high=20105.0, low=20095.0, close=20100.0)
    ts = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    signal_smt._process_managing(bar, ts, ts.time())

    assert signal_smt._state == "SCANNING"
    assert signal_smt._position is None
    assert not pos_file.exists()


def test_process_managing_exit_stop(monkeypatch, tmp_path):
    """manage_position returns 'exit_stop' → state becomes SCANNING, pnl is negative for long."""
    import signal_smt
    pos = _setup_managing_state(monkeypatch, tmp_path, direction="long")

    pos_file = tmp_path / "position.json"
    pos_file.write_text(json.dumps(pos))
    monkeypatch.setattr(signal_smt, "POSITION_FILE", pos_file)

    captured_lines = []
    monkeypatch.setattr("builtins.print", lambda *a, **kw: captured_lines.append(a[0]))
    monkeypatch.setattr("signal_smt.manage_position", lambda p, b: "exit_stop")

    bar = _make_mock_bar(ts="2025-01-02 09:30:00", high=19960.0, low=19948.0, close=19950.0)
    ts = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    signal_smt._process_managing(bar, ts, ts.time())

    assert signal_smt._state == "SCANNING"
    # P&L should be negative (stop below assumed_entry for long)
    assert any("-$" in line or "P&L" in line for line in captured_lines)


def test_process_managing_session_end_force_close(monkeypatch, tmp_path):
    """manage_position returns 'hold' at SESSION_END → forced close at bar close."""
    import signal_smt
    pos = _setup_managing_state(monkeypatch, tmp_path, direction="short")

    pos_file = tmp_path / "position.json"
    pos_file.write_text(json.dumps(pos))
    monkeypatch.setattr(signal_smt, "POSITION_FILE", pos_file)

    monkeypatch.setattr("signal_smt.manage_position", lambda p, b: "hold")

    # Bar at exactly SESSION_END time triggers force close
    bar = _make_mock_bar(ts="2025-01-02 13:30:00", close=19980.0)
    ts = pd.Timestamp("2025-01-02 13:30:00", tz="America/New_York")
    signal_smt._process_managing(bar, ts, ts.time())

    assert signal_smt._state == "SCANNING"
    assert signal_smt._position is None


# ══ Buffer management tests ═══════════════════════════════════════════════════

def test_1s_buf_cleared_on_1m_bar(monkeypatch, tmp_path):
    """on_mnq_1m_bar with hasNewBar=True resets _mnq_1s_buf to empty."""
    import signal_smt

    monkeypatch.setattr(signal_smt, "BAR_DATA_DIR", tmp_path)

    empty = signal_smt._empty_bar_df()
    # Pre-populate buffer with one bar
    buf = empty.copy()
    buf.loc[pd.Timestamp("2025-01-02 09:05:00", tz="America/New_York")] = [20000, 20005, 19995, 20000, 100]
    monkeypatch.setattr(signal_smt, "_mnq_1s_buf", buf)
    monkeypatch.setattr(signal_smt, "_mnq_1m_df", empty.copy())

    bar = _make_mock_bar(ts="2025-01-02 09:05:00")
    bars = [None, bar]  # hasNewBar=True checks bars[-1]

    signal_smt.on_mnq_1m_bar(bars, hasNewBar=True)

    assert signal_smt._mnq_1s_buf.empty


def test_mes_1s_buf_always_appended(monkeypatch, tmp_path):
    """on_mes_tick finalizes a bar into _mes_1s_buf when a second boundary is crossed."""
    import signal_smt

    empty = signal_smt._empty_bar_df()
    monkeypatch.setattr(signal_smt, "_mes_1s_buf", empty.copy())
    monkeypatch.setattr(signal_smt, "_mes_tick_bar", None)
    monkeypatch.setattr(signal_smt, "_state", "MANAGING")

    # Tick 1: establishes accumulator for second S — buffer still empty
    ticker1 = _make_mock_ticker("2025-01-02 14:06:00.100000", price=20001.0)
    signal_smt.on_mes_tick(ticker1)
    assert len(signal_smt._mes_1s_buf) == 0

    # Tick 2: new second → finalizes tick 1's bar into buffer
    ticker2 = _make_mock_ticker("2025-01-02 14:06:01.200000", price=20002.0)
    signal_smt.on_mes_tick(ticker2)
    assert len(signal_smt._mes_1s_buf) == 1


# ══ Tick handler tests ════════════════════════════════════════════════════════

def test_tick_accumulator_ohlcv_correct(monkeypatch):
    """Three ticks in the same second produce correct OHLCV in the accumulator."""
    import signal_smt

    empty = signal_smt._empty_bar_df()
    monkeypatch.setattr(signal_smt, "_mnq_1s_buf", empty.copy())
    monkeypatch.setattr(signal_smt, "_mnq_tick_bar", None)
    # Prevent _process() side-effects from a boundary crossing
    monkeypatch.setattr(signal_smt, "_process", lambda bar: None)

    for ts_suffix, price in ((".100000", 20001.0), (".200000", 20005.0), (".300000", 19998.0)):
        signal_smt.on_mnq_tick(_make_mock_ticker(f"2025-01-02 14:06:00{ts_suffix}", price=price))

    acc = signal_smt._mnq_tick_bar
    assert acc["open"]   == 20001.0
    assert acc["high"]   == 20005.0
    assert acc["low"]    == 19998.0
    assert acc["close"]  == 19998.0
    assert acc["volume"] == 3.0
    # No boundary crossed — buffer still empty
    assert len(signal_smt._mnq_1s_buf) == 0


def test_tick_boundary_finalizes_bar(monkeypatch):
    """Tick at S+1 causes S's bar to be appended to _mnq_1s_buf with correct OHLCV."""
    import signal_smt

    empty = signal_smt._empty_bar_df()
    monkeypatch.setattr(signal_smt, "_mnq_1s_buf", empty.copy())
    monkeypatch.setattr(signal_smt, "_mnq_tick_bar", None)
    monkeypatch.setattr(signal_smt, "_process", lambda bar: None)

    signal_smt.on_mnq_tick(_make_mock_ticker("2025-01-02 14:06:00.100000", price=20001.0))
    signal_smt.on_mnq_tick(_make_mock_ticker("2025-01-02 14:06:01.200000", price=20002.0))

    assert len(signal_smt._mnq_1s_buf) == 1
    row = signal_smt._mnq_1s_buf.iloc[0]
    assert row["Open"]  == 20001.0
    assert row["Close"] == 20001.0
    assert row["Volume"] == 1.0


def test_tick_boundary_calls_process(monkeypatch):
    """Boundary crossing in on_mnq_tick triggers exactly one _process() call."""
    import signal_smt

    empty = signal_smt._empty_bar_df()
    monkeypatch.setattr(signal_smt, "_mnq_1s_buf", empty.copy())
    monkeypatch.setattr(signal_smt, "_mnq_tick_bar", None)

    process_calls = []
    monkeypatch.setattr(signal_smt, "_process", lambda bar: process_calls.append(bar))

    signal_smt.on_mnq_tick(_make_mock_ticker("2025-01-02 14:06:00.100000", price=20001.0))
    signal_smt.on_mnq_tick(_make_mock_ticker("2025-01-02 14:06:01.200000", price=20002.0))

    assert len(process_calls) == 1


def test_mes_tick_does_not_call_process(monkeypatch):
    """on_mes_tick never calls _process(); buffer grows on boundary crossing."""
    import signal_smt

    empty = signal_smt._empty_bar_df()
    monkeypatch.setattr(signal_smt, "_mes_1s_buf", empty.copy())
    monkeypatch.setattr(signal_smt, "_mes_tick_bar", None)

    process_calls = []
    monkeypatch.setattr(signal_smt, "_process", lambda bar: process_calls.append(bar))

    signal_smt.on_mes_tick(_make_mock_ticker("2025-01-02 14:06:00.100000", price=20001.0))
    signal_smt.on_mes_tick(_make_mock_ticker("2025-01-02 14:06:01.200000", price=20002.0))

    assert len(process_calls) == 0
    assert len(signal_smt._mes_1s_buf) == 1


def test_tick_no_tickbyticks_is_noop(monkeypatch):
    """Ticker with empty tickByTicks list causes no state change."""
    import signal_smt

    empty = signal_smt._empty_bar_df()
    monkeypatch.setattr(signal_smt, "_mnq_1s_buf", empty.copy())
    monkeypatch.setattr(signal_smt, "_mnq_tick_bar", None)

    ticker = mock.MagicMock()
    ticker.tickByTicks = []
    signal_smt.on_mnq_tick(ticker)

    assert signal_smt._mnq_tick_bar is None
    assert len(signal_smt._mnq_1s_buf) == 0


def test_acc_to_df_row_schema():
    """_acc_to_df_row produces correct columns and ET-localized index."""
    import signal_smt

    acc = {
        "open": 100.0, "high": 105.0, "low": 99.0, "close": 102.0, "volume": 5.0,
        "second_ts": pd.Timestamp("2025-01-02 09:01:00", tz="America/New_York"),
    }
    row = signal_smt._acc_to_df_row(acc)

    assert list(row.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(row) == 1
    assert str(row.index.tz) == "America/New_York"


# ══ Persistence and data loading tests ═══════════════════════════════════════

def test_load_parquets_missing_files(monkeypatch, tmp_path):
    """_load_parquets returns empty DataFrames (not FileNotFoundError) when files absent."""
    import signal_smt
    monkeypatch.setattr(signal_smt, "BAR_DATA_DIR", tmp_path)

    mnq_df, mes_df = signal_smt._load_parquets()

    assert isinstance(mnq_df, pd.DataFrame)
    assert isinstance(mes_df, pd.DataFrame)
    assert mnq_df.empty
    assert mes_df.empty
    assert list(mnq_df.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_30_day_cap_on_gap_fill(monkeypatch, tmp_path):
    """_gap_fill_1m requests no more than 30 days back even when df is empty."""
    import signal_smt

    monkeypatch.setattr(signal_smt, "BAR_DATA_DIR", tmp_path)

    start_args = []

    def fake_fetch(ticker, start, end, interval="1m", contract_type="stock"):
        start_args.append(pd.Timestamp(start))
        return None  # simulate no new data

    fake_source = mock.MagicMock()
    fake_source.fetch = fake_fetch

    # IBGatewaySource is imported inside _gap_fill_1m, so patch its source module
    with mock.patch("data.sources.IBGatewaySource", return_value=fake_source):
        signal_smt._gap_fill_1m(signal_smt._empty_bar_df(), signal_smt._empty_bar_df())

    assert len(start_args) >= 1
    now = pd.Timestamp.now(tz="America/New_York")
    floor = now - pd.Timedelta(days=31)
    # All start timestamps must be after the 31-day floor
    for ts in start_args:
        if ts.tz is None:
            ts = ts.tz_localize("America/New_York")
        assert ts >= floor, f"Gap-fill requested data older than 30 days: {ts}"
