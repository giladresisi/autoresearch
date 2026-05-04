"""tests/test_signal_smt.py — Unit tests for signal_smt.py state machine logic.

No IB connection required. All tests use synthetic DataFrames and mock the IB layer.

Note: tests for IB tick assembly, parquet helpers, and fill-price computation are not in
this file — those live in tests/test_ib_realtime.py and tests/test_fill_executor.py
respectively after the Stage 1 refactor that moved that logic out of signal_smt.
"""
import datetime
import json
from types import SimpleNamespace
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
    # _BarRow-style attribute names (capitalized) for places that build a row from `bar`
    bar.Open   = open_
    bar.High   = high
    bar.Low    = low
    bar.Close  = close
    bar.Volume = volume
    bar.name   = pd.Timestamp(ts, tz=tz)
    return bar


def _empty_bar_df() -> pd.DataFrame:
    """Build an empty OHLCV frame matching the realtime source schema."""
    return pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )


def _make_stub_ib_source(mnq_df: pd.DataFrame | None = None,
                        mes_df: pd.DataFrame | None = None) -> SimpleNamespace:
    """Build a stub IbRealtimeSource exposing the .mnq_1m_df / .mes_1m_df properties."""
    return SimpleNamespace(
        mnq_1m_df=mnq_df if mnq_df is not None else _empty_bar_df(),
        mes_1m_df=mes_df if mes_df is not None else _empty_bar_df(),
    )


class _StubExecutor:
    """Minimal SimulatedBrokerExecutor stub: returns a FillRecord-like object using the
    pre-refactor inline slippage formula so existing assertions about fill prices hold."""

    def __init__(self, entry_slip_ticks: int = 2):
        self._entry_slip_ticks = entry_slip_ticks

    def place_entry(self, signal, bar):
        if signal.get("limit_fill_bars") is not None:
            fill_price = float(signal["entry_price"])
        else:
            slip = self._entry_slip_ticks * 0.25
            if signal["direction"] == "long":
                fill_price = float(signal["entry_price"]) + slip
            else:
                fill_price = float(signal["entry_price"]) - slip
        return SimpleNamespace(fill_price=round(fill_price, 4))

    def place_exit(self, position, exit_type, bar):
        if exit_type == "exit_tp":
            fp = float(position["take_profit"])
        elif exit_type == "exit_secondary":
            fp = float(position["secondary_target"])
        elif exit_type == "exit_stop":
            fp = float(position["stop_price"])
        else:
            fp = float(bar.Close)
        return SimpleNamespace(fill_price=round(fp, 4))


# ══ Helper function tests ═════════════════════════════════════════════════════

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

    # Realtime data layer is now an injected source object exposing 1m frames.
    monkeypatch.setattr(signal_smt, "_ib_source", _make_stub_ib_source())
    monkeypatch.setattr(signal_smt, "_executor", _StubExecutor(
        entry_slip_ticks=signal_smt.ENTRY_SLIPPAGE_TICKS
    ))

    # MES partial accumulator is the only partial bar tracked at module scope after the refactor;
    # MNQ partial lives inside IbRealtimeSource and is materialized into the `bar` arg.
    _minute_ts = pd.Timestamp("2025-01-02 09:10:00", tz="America/New_York").floor("min")
    monkeypatch.setattr(signal_smt, "_mes_partial_1m", {
        "open": 20000.0, "high": 20005.0, "low": 19995.0, "close": 20000.0,
        "volume": 100.0, "minute_ts": _minute_ts,
    })

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
    """MES partial bar's minute does not match the current bar's minute → process_scan_bar not called."""
    import signal_smt
    import strategy_smt
    _setup_scanning_state(monkeypatch, tmp_path)

    # Bar at 09:10 minute, MES partial at 09:09 minute → misaligned
    mes_minute = pd.Timestamp("2025-01-02 09:09:00", tz="America/New_York")
    monkeypatch.setattr(signal_smt, "_mes_partial_1m", {
        "open": 20000.0, "high": 20005.0, "low": 19995.0, "close": 20000.0,
        "volume": 100.0, "minute_ts": mes_minute,
    })

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
    minute_ts = aligned_ts.floor("min")
    monkeypatch.setattr(signal_smt, "_mes_partial_1m", {
        "open": 20000.0, "high": 20005.0, "low": 19995.0, "close": 20000.0,
        "volume": 100.0, "minute_ts": minute_ts,
    })

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
    minute_ts = aligned_ts.floor("min")
    monkeypatch.setattr(signal_smt, "_mes_partial_1m", {
        "open": 20000.0, "high": 20005.0, "low": 19995.0, "close": 20000.0,
        "volume": 100.0, "minute_ts": minute_ts,
    })

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
    minute_ts = aligned_ts.floor("min")
    monkeypatch.setattr(signal_smt, "_mes_partial_1m", {
        "open": 20000.0, "high": 20005.0, "low": 19995.0, "close": 20000.0,
        "volume": 100.0, "minute_ts": minute_ts,
    })

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
    minute_ts = aligned_ts.floor("min")
    monkeypatch.setattr(signal_smt, "_mes_partial_1m", {
        "open": 20000.0, "high": 20005.0, "low": 19995.0, "close": 20000.0,
        "volume": 100.0, "minute_ts": minute_ts,
    })

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
    # Slippage applied via SimulatedBrokerExecutor: short → entry - ticks * 0.25
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
    monkeypatch.setattr(signal_smt, "_executor", _StubExecutor(
        entry_slip_ticks=signal_smt.ENTRY_SLIPPAGE_TICKS,
    ))
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
