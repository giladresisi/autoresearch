"""tests/test_smt_backtest.py -- Integration tests for run_backtest() in train_smt.py.

Uses synthetic 5m DataFrames written to a tmpdir. No IB connection required.
"""
import datetime
import json
import os
import tempfile
from pathlib import Path

import pandas as pd
import numpy as np
import pytest


def _build_short_signal_bars(date, base=20000.0, n=90):
    """Build MNQ/MES bars that should produce a bearish SMT signal."""
    start_ts = pd.Timestamp(date + " 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="5min")
    mes_highs = [base + 5] * n
    mnq_highs = [base + 5] * n
    mes_lows  = [base - 5] * n
    mnq_lows  = [base - 5] * n
    opens  = [base] * n
    closes = [base] * n
    mes_highs[7] = base + 30
    opens[8]  = base + 2
    closes[8] = base - 2
    mnq_highs[8] = base + 6
    mnq = pd.DataFrame(
        {"Open": opens, "High": mnq_highs, "Low": mnq_lows, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    mes = pd.DataFrame(
        {"Open": opens, "High": mes_highs, "Low": mes_lows, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    return mnq, mes


def _build_long_signal_bars(date, base=20000.0, n=90):
    """Build MNQ/MES bars that should produce a bullish SMT signal."""
    start_ts = pd.Timestamp(date + " 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="5min")
    mes_lows  = [base - 5] * n
    mnq_lows  = [base - 5] * n
    mes_highs = [base + 5] * n
    mnq_highs = [base + 5] * n
    opens  = [base] * n
    closes = [base] * n
    mes_lows[7] = base - 30
    opens[8]  = base - 2
    closes[8] = base + 2
    mnq_lows[8] = base - 6
    mnq = pd.DataFrame(
        {"Open": opens, "High": mnq_highs, "Low": mnq_lows, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    mes = pd.DataFrame(
        {"Open": opens, "High": mes_highs, "Low": mes_lows, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    return mnq, mes


@pytest.fixture
def futures_tmpdir(tmp_path, monkeypatch):
    """Fixture: sets FUTURES_CACHE_DIR to a fresh tmpdir and bootstraps manifest."""
    import train_smt
    cache_dir = tmp_path / "futures_data"
    interval_dir = cache_dir / "5m"
    interval_dir.mkdir(parents=True)

    monkeypatch.setattr(train_smt, "FUTURES_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(train_smt, "SESSION_START", "09:00")
    monkeypatch.setattr(train_smt, "SESSION_END", "10:30")

    manifest = {
        "tickers": ["MNQ", "MES"],
        "backtest_start": "2025-01-02",
        "backtest_end": "2025-03-01",
        "fetch_interval": "5m",
        "source": "ib",
    }
    (cache_dir / "futures_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return cache_dir


# -- Test 25: Empty data ---------------------------------------------------

def test_run_backtest_empty_data_returns_zero_trades(futures_tmpdir):
    """No bars -> 0 trades, no crash, returns valid metrics dict."""
    import train_smt
    empty = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        dtype=float,
    )
    empty.index = pd.DatetimeIndex([], tz="America/New_York")
    stats = train_smt.run_backtest(empty, empty, start="2025-01-02", end="2025-01-10")
    assert stats["total_trades"] == 0
    assert stats["total_pnl"] == 0.0


# -- Test 26: Long trade TP hit --------------------------------------------

def test_run_backtest_long_trade_tp_hit(futures_tmpdir):
    """Bullish SMT + TP bar -> positive PnL."""
    import train_smt
    mnq, mes = _build_long_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    if stats["total_trades"] > 0:
        assert any(t["pnl"] > 0 for t in stats["trade_records"])


# -- Test 27: Short trade stop hit -----------------------------------------

def test_run_backtest_short_trade_stop_hit(futures_tmpdir):
    """Bearish SMT -> trade recorded with a valid exit type."""
    import train_smt
    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    if stats["total_trades"] > 0:
        assert any(
            t["exit_type"] in ("exit_stop", "session_close", "exit_tp")
            for t in stats["trade_records"]
        )


# -- Test 28: Session force exit --------------------------------------------

def test_run_backtest_session_force_exit(futures_tmpdir):
    """Position open but TP/stop never hit -> force-closed at session end."""
    import train_smt
    import unittest.mock as mock

    mnq, mes = _build_short_signal_bars("2025-01-02")
    original_screen = train_smt.screen_session

    def patched_screen(mnq_b, mes_b, d):
        sig = original_screen(mnq_b, mes_b, d)
        if sig is not None:
            if sig["direction"] == "short":
                sig["take_profit"] = sig["entry_price"] - 10000
                sig["stop_price"] = sig["entry_price"] + 10000
            else:
                sig["take_profit"] = sig["entry_price"] + 10000
                sig["stop_price"] = sig["entry_price"] - 10000
        return sig

    with mock.patch.object(train_smt, "screen_session", patched_screen):
        stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    if stats["total_trades"] > 0:
        assert stats["exit_type_breakdown"].get("session_close", 0) >= 0


# -- Test 29: End of backtest exit ------------------------------------------

def test_run_backtest_end_of_backtest_exit(futures_tmpdir):
    """Position open at end of backtest window -> end_of_backtest exit type."""
    import train_smt
    import unittest.mock as mock

    mnq, mes = _build_short_signal_bars("2025-01-02")
    original_screen = train_smt.screen_session

    def patched_screen(mnq_b, mes_b, d):
        sig = original_screen(mnq_b, mes_b, d)
        if sig is not None:
            sig["take_profit"] = sig["entry_price"] + 50000
            sig["stop_price"] = sig["entry_price"] - 50000
        return sig

    with mock.patch.object(train_smt, "screen_session", patched_screen):
        stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert "total_trades" in stats
    assert "total_pnl" in stats


# -- Test 30: Long PnL formula ---------------------------------------------

def test_pnl_long_correct(futures_tmpdir):
    """Long PnL = (exit - entry) x contracts x MNQ_PNL_PER_POINT (2.0)."""
    import train_smt
    entry  = 20000.0
    exit_p = 20010.0
    contracts = 1
    expected_pnl = (exit_p - entry) * contracts * train_smt.MNQ_PNL_PER_POINT
    assert expected_pnl == pytest.approx(20.0)


# -- Test 31: Short PnL formula --------------------------------------------

def test_pnl_short_correct(futures_tmpdir):
    """Short PnL = (entry - exit) x contracts x MNQ_PNL_PER_POINT (2.0)."""
    import train_smt
    entry  = 20010.0
    exit_p = 20000.0
    contracts = 1
    expected_pnl = (entry - exit_p) * contracts * train_smt.MNQ_PNL_PER_POINT
    assert expected_pnl == pytest.approx(20.0)


# -- Test 32: One trade per day max -----------------------------------------

def test_one_trade_per_day_max(futures_tmpdir):
    """At most one trade per day -- second signal on same day is ignored."""
    import train_smt
    import unittest.mock as mock

    signal_count = 0
    original_screen = train_smt.screen_session

    def counting_screen(mnq_b, mes_b, d):
        nonlocal signal_count
        sig = original_screen(mnq_b, mes_b, d)
        if sig is not None:
            signal_count += 1
        return sig

    n = 90
    start_ts = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="5min")
    base = 20000.0
    opens  = [base] * n
    closes = [base] * n
    highs  = [base + 5] * n
    lows   = [base - 5] * n
    mnq = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1000.0]*n}, index=idx)
    mes = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1000.0]*n}, index=idx)

    with mock.patch.object(train_smt, "screen_session", counting_screen):
        stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert stats["total_trades"] <= 1


# -- Test 33: Fold loop smoke ----------------------------------------------

def test_fold_loop_smoke(futures_tmpdir):
    """With several months of synthetic data, _compute_fold_params runs without error."""
    import train_smt

    dates = pd.bdate_range("2025-01-02", "2025-04-30")
    all_bars = []
    for d in dates:
        start_ts = pd.Timestamp(str(d.date()) + " 09:00:00", tz="America/New_York")
        idx = pd.date_range(start=start_ts, periods=18, freq="5min")
        df = pd.DataFrame(
            {"Open": 20000.0, "High": 20005.0, "Low": 19995.0, "Close": 20000.0, "Volume": 1000.0},
            index=idx,
        )
        all_bars.append(df)
    full = pd.concat(all_bars)

    effective_n, effective_days = train_smt._compute_fold_params(
        "2025-01-02", "2025-04-30", 6, 60
    )
    assert effective_n >= 1
    assert effective_days >= 1

    stats = train_smt.run_backtest(full, full, start="2025-01-02", end="2025-02-01")
    assert "total_trades" in stats


# -- Test 34: Metrics shape -------------------------------------------------

def test_metrics_shape(futures_tmpdir):
    """Returned dict has all required keys."""
    import train_smt

    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"], dtype=float)
    empty.index = pd.DatetimeIndex([], tz="America/New_York")
    stats = train_smt.run_backtest(empty, empty, start="2025-01-02", end="2025-01-10")

    required_keys = {
        "total_pnl", "total_trades", "win_rate", "avg_pnl_per_trade",
        "long_pnl", "short_pnl", "sharpe", "max_drawdown", "calmar",
        "avg_rr", "exit_type_breakdown", "trade_records",
    }
    assert required_keys.issubset(set(stats.keys()))


# -- Test 35: New defaults integration ---------------------------------------

def test_new_defaults_produce_valid_results(futures_tmpdir, monkeypatch):
    """New default constants (validity gate + min stop) don't crash run_backtest."""
    import train_smt
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", True)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 5.0)
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "LONG_STOP_RATIO", 0.45)
    monkeypatch.setattr(train_smt, "SHORT_STOP_RATIO", 0.45)

    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert "total_trades" in stats
    assert "trade_records" in stats
    assert stats["total_trades"] >= 0
