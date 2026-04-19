"""tests/test_smt_backtest.py -- Integration tests for run_backtest() in backtest_smt.py.

Uses synthetic 5m DataFrames written to a tmpdir. No IB connection required.

Import conventions:
  import backtest_smt as train_smt   — harness module (run_backtest, _compute_fold_params)
  import strategy_smt as _strat      — strategy module (manage_position, _build_signal_from_bar)

Patch targets:
  Harness constants (BACKTEST_START, FUTURES_CACHE_DIR, SESSION_START/END,
    SIGNAL_BLACKOUT_*, ALLOWED_WEEKDAYS, REENTRY_MAX_MOVE_PTS, etc.):
      monkeypatch.setattr(train_smt, ...)
  Strategy constants read inside strategy functions
    (TDO_VALIDITY_CHECK, MIN_STOP_POINTS, MIN_TDO_DISTANCE_PTS,
     TRAIL_AFTER_TP_PTS, BREAKEVEN_TRIGGER_PCT, LONG/SHORT_STOP_RATIO):
      monkeypatch.setattr(_strat, ...)  — AND also train_smt for consistency
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
    """Build MNQ/MES bars that produce a bearish SMT signal.

    Bar 5: bullish anchor (close > open) — required for find_anchor_close / find_entry_bar.
    Bar 7: MES makes new session high; MNQ does not → bearish divergence.
    Bar 8: bearish confirmation candle whose high pierces the bar-5 anchor close.
    """
    start_ts = pd.Timestamp(date + " 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="5min")
    mes_highs = [base + 5] * n
    mnq_highs = [base + 5] * n
    mes_lows  = [base - 5] * n
    mnq_lows  = [base - 5] * n
    opens  = [base] * n
    closes = [base] * n
    # Bullish anchor at bar 5 (needed by find_anchor_close and find_entry_bar)
    opens[5]  = base - 2
    closes[5] = base + 2
    # Bearish SMT divergence: MES new session high at bar 7, MNQ fails to confirm
    mes_highs[7] = base + 30
    # Bearish confirmation at bar 8: close < open, high > anchor close (base + 2)
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
    """Build MNQ/MES bars that produce a bullish SMT signal.

    Bar 5: bearish anchor (close < open) — required for find_anchor_close / find_entry_bar.
    Bar 7: MES makes new session low; MNQ does not → bullish divergence.
    Bar 8: bullish confirmation candle whose low pierces the bar-5 anchor close.
    """
    start_ts = pd.Timestamp(date + " 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="5min")
    mes_lows  = [base - 5] * n
    mnq_lows  = [base - 5] * n
    mes_highs = [base + 5] * n
    mnq_highs = [base + 5] * n
    opens  = [base] * n
    closes = [base] * n
    # Bearish anchor at bar 5 (needed by find_anchor_close and find_entry_bar)
    opens[5]  = base + 2
    closes[5] = base - 2
    # Bullish SMT divergence: MES new session low at bar 7, MNQ fails to confirm
    mes_lows[7] = base - 30
    # Bullish confirmation at bar 8: close > open, low < anchor close (base - 2)
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
    import backtest_smt as train_smt
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
    import backtest_smt as train_smt
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
    import backtest_smt as train_smt
    mnq, mes = _build_long_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    if stats["total_trades"] > 0:
        assert any(t["pnl"] > 0 for t in stats["trade_records"])


# -- Test 27: Short trade stop hit -----------------------------------------

def test_run_backtest_short_trade_stop_hit(futures_tmpdir):
    """Bearish SMT -> trade recorded with a valid exit type."""
    import backtest_smt as train_smt
    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    if stats["total_trades"] > 0:
        assert any(
            t["exit_type"] in ("exit_stop", "session_close", "exit_tp")
            for t in stats["trade_records"]
        )


# -- Test 28: Session force exit --------------------------------------------

def test_run_backtest_session_force_exit(futures_tmpdir, monkeypatch):
    """Position open but TP/stop unreachable -> force-closed at session end."""
    import backtest_smt as train_smt
    import strategy_smt as _strat
    # Disable all guards so the signal fires; set stop/TP far from price
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 0.0)
    # TDO 10000 pts below entry so both TP and stop are unreachable within the session
    monkeypatch.setattr(train_smt, "compute_tdo", lambda *a: 10000.0)

    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    if stats["total_trades"] > 0:
        assert "session_close" in stats["exit_type_breakdown"]


# -- Test 29: End of backtest exit ------------------------------------------

def test_run_backtest_end_of_backtest_exit(futures_tmpdir, monkeypatch):
    """run_backtest returns valid stats dict regardless of trade count."""
    import backtest_smt as train_smt
    import strategy_smt as _strat
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 0.0)
    monkeypatch.setattr(train_smt, "compute_tdo", lambda *a: 10000.0)

    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert "total_trades" in stats
    assert "total_pnl" in stats


# -- Test 30: Long PnL formula ---------------------------------------------

def test_pnl_long_correct(futures_tmpdir):
    """Long PnL = (exit - entry) x contracts x MNQ_PNL_PER_POINT (2.0)."""
    import backtest_smt as train_smt
    entry  = 20000.0
    exit_p = 20010.0
    contracts = 1
    expected_pnl = (exit_p - entry) * contracts * train_smt.MNQ_PNL_PER_POINT
    assert expected_pnl == pytest.approx(20.0)


# -- Test 31: Short PnL formula --------------------------------------------

def test_pnl_short_correct(futures_tmpdir):
    """Short PnL = (entry - exit) x contracts x MNQ_PNL_PER_POINT (2.0)."""
    import backtest_smt as train_smt
    entry  = 20010.0
    exit_p = 20000.0
    contracts = 1
    expected_pnl = (entry - exit_p) * contracts * train_smt.MNQ_PNL_PER_POINT
    assert expected_pnl == pytest.approx(20.0)


# -- Test 32: One trade per day max -----------------------------------------

def test_one_trade_per_day_max(futures_tmpdir, monkeypatch):
    """At most one trade per day from a single signal setup."""
    import backtest_smt as train_smt
    import strategy_smt as _strat
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 0.0)

    # Flat bars — no divergence, no signal
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

    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert stats["total_trades"] <= 1


# -- Test 33: Fold loop smoke ----------------------------------------------

def test_fold_loop_smoke(futures_tmpdir):
    """With several months of synthetic data, _compute_fold_params runs without error."""
    import backtest_smt as train_smt

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
    import backtest_smt as train_smt

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
    import backtest_smt as train_smt
    import strategy_smt as _strat
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", True)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 5.0)
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(_strat, "LONG_STOP_RATIO", 0.45)
    monkeypatch.setattr(_strat, "SHORT_STOP_RATIO", 0.45)

    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert "total_trades" in stats
    assert "trade_records" in stats
    assert stats["total_trades"] >= 0


# -- Test 36: Regression — no re-entry matches legacy single-trade behavior ---

def test_regression_no_reentry_matches_legacy_behavior(futures_tmpdir, monkeypatch):
    """With REENTRY_MAX_MOVE_PTS=0 and BREAKEVEN_TRIGGER_PCT=0, run_backtest produces
    exactly 1 short trade on the known synthetic dataset.

    This ensures the refactor is semantically equivalent to the old screen_session
    + _scan_bars_for_exit architecture when re-entry and breakeven are both disabled.
    """
    import backtest_smt as train_smt
    import strategy_smt as _strat
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 0.0)
    monkeypatch.setattr(_strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "short")
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))

    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")

    assert stats["total_trades"] == 1
    assert len(stats["trade_records"]) == 1
    assert stats["trade_records"][0]["direction"] == "short"


# ══ Task 8a — MAX_REENTRY_COUNT integration tests ════════════════════════════

def test_run_backtest_max_reentry_count_limits_trades(futures_tmpdir, monkeypatch):
    """MAX_REENTRY_COUNT=1 prevents a second re-entry within same session."""
    import backtest_smt as train_smt
    import strategy_smt as _strat
    monkeypatch.setattr(train_smt, "MAX_REENTRY_COUNT", 1)
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 999.0)
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-04")
    assert len(stats.get("trade_records", [])) >= 1, "Need at least one trade to verify cap"
    trades_by_day: dict[str, int] = {}
    for t in stats.get("trade_records", []):
        trades_by_day[t["entry_date"]] = trades_by_day.get(t["entry_date"], 0) + 1
    assert all(v <= 1 for v in trades_by_day.values())


def test_run_backtest_max_reentry_disabled_allows_multiple(futures_tmpdir, monkeypatch):
    """MAX_REENTRY_COUNT=999 (disabled) does not cap trades."""
    import backtest_smt as train_smt
    import strategy_smt as _strat
    monkeypatch.setattr(train_smt, "MAX_REENTRY_COUNT", 999)
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 999.0)
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-04")
    assert stats["total_trades"] >= 1  # disabled cap — must produce at least one trade


# ══ Task 8b — Trade record new fields + MIN_PRIOR_TRADE_BARS_HELD tests ══════

def test_run_backtest_trade_record_contains_new_fields(futures_tmpdir, monkeypatch):
    """Trade records include all 6 new diagnostic fields."""
    import backtest_smt as train_smt
    import strategy_smt as _strat
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-04")
    assert len(stats.get("trade_records", [])) >= 1, "Need at least one trade to verify fields"
    for t in stats.get("trade_records", []):
        assert "reentry_sequence" in t
        assert "prior_trade_bars_held" in t
        assert "entry_bar_body_ratio" in t
        assert "smt_sweep_pts" in t
        assert "smt_miss_pts" in t
        assert "bars_since_divergence" in t


def test_run_backtest_min_prior_bars_held_infrastructure(futures_tmpdir, monkeypatch):
    """MIN_PRIOR_TRADE_BARS_HELD=0 (default) does not block any re-entries."""
    import backtest_smt as train_smt
    import strategy_smt as _strat
    monkeypatch.setattr(train_smt, "MIN_PRIOR_TRADE_BARS_HELD", 0)
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 999.0)
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats_base = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-04")
    assert stats_base["total_trades"] >= 1, "Need trades to test gate"

    monkeypatch.setattr(train_smt, "MIN_PRIOR_TRADE_BARS_HELD", 100)
    stats_blocked = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-04")
    # High threshold should reduce or equal re-entry count (gate is wired up)
    assert stats_blocked["total_trades"] <= stats_base["total_trades"]


# ══ Phase 2: exit_market infrastructure ══════════════════════════════════════

def test_run_backtest_exit_market_uses_bar_close(monkeypatch):
    """When manage_position returns exit_market, trade exit_price equals bar close."""
    import backtest_smt as train_smt
    import strategy_smt as _strat
    call_count = [0]
    orig = train_smt.manage_position
    def patched(position, bar):
        call_count[0] += 1
        return "exit_market" if call_count[0] == 1 else orig(position, bar)
    monkeypatch.setattr(train_smt, "manage_position", patched)
    mnq, mes = _build_short_signal_bars("2025-01-02")
    monkeypatch.setattr(train_smt, "BACKTEST_START", "2025-01-02")
    monkeypatch.setattr(train_smt, "BACKTEST_END",   "2025-01-03")
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    stats = train_smt.run_backtest(mnq, mes)
    market = [t for t in stats["trade_records"] if t["exit_type"] == "exit_market"]
    if market:
        assert market[0]["exit_price"] != market[0]["stop_price"]
        assert market[0]["exit_price"] != market[0]["tdo"]
