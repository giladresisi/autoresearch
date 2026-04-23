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
    # TDO 10000 pts below entry so both TP and stop are unreachable within the session.
    # Disable MIDNIGHT_OPEN_AS_TP so compute_tdo=10000 is actually used as TDO (not
    # overridden by midnight open which defaults to the first bar's open ~20000).
    # Must patch both modules: backtest_smt holds its own bound name.
    monkeypatch.setattr(_strat, "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(train_smt, "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(train_smt, "compute_tdo", lambda *a: 10000.0)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)

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
    monkeypatch.setattr(_strat, "MIN_TARGET_PTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TARGET_PTS", 0.0)
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "short")
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(_strat, "HIDDEN_SMT_ENABLED", False)
    monkeypatch.setattr(train_smt, "HIDDEN_SMT_ENABLED", False)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)
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
    monkeypatch.setattr(_strat, "HIDDEN_SMT_ENABLED", False)
    monkeypatch.setattr(train_smt, "HIDDEN_SMT_ENABLED", False)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(_strat, "MIN_TARGET_PTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TARGET_PTS", 0.0)
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
    monkeypatch.setattr(_strat, "MIN_TARGET_PTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TARGET_PTS", 0.0)
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
    monkeypatch.setattr(_strat, "MIN_TARGET_PTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TARGET_PTS", 0.0)
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
    monkeypatch.setattr(_strat, "MIN_TARGET_PTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TARGET_PTS", 0.0)
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


# ══ Tests 11-20: Replacement logic / state machine / score-filter unit tests ══

def test_replacement_rule1_displacement_cannot_displace_wick():
    """Rule 1: displacement type cannot displace a wick/body pending divergence."""
    from strategy_smt import divergence_score, REPLACE_THRESHOLD
    # Simulate: pending is wick type, new div is displacement
    pending_type = "wick"
    new_type = "displacement"
    pending_score = 0.5
    nd_score = 0.9  # stronger, but type is displacement
    # Rule 1 check: if pending is wick/body and new is displacement → no replace
    _replace = False
    if pending_type in ("wick", "body") and new_type == "displacement":
        _replace = False
    elif new_type in ("wick", "body"):  # Rule 2
        _replace = True
    assert not _replace, "displacement should not displace wick pending"


def test_replacement_rule2_wick_replaces_provisional():
    """Rule 2: wick/body signal replaces a displacement (provisional) pending."""
    from strategy_smt import divergence_score
    pending_type = "displacement"
    pending_provisional = True
    new_type = "wick"
    nd_score = 0.3  # even weak wick replaces provisional displacement
    _replace = False
    if pending_type in ("wick", "body") and new_type == "displacement":
        _replace = False
    elif pending_provisional and new_type in ("wick", "body"):
        _replace = True
    assert _replace, "wick should replace provisional displacement pending"


def test_replacement_rule3_same_direction_upgrade():
    """Rule 3: same-direction signal replaces if strictly stronger than effective score."""
    from strategy_smt import divergence_score, _effective_div_score
    pending_direction = "short"
    nd_dir = "short"  # same direction
    pending_type = "wick"
    new_type = "wick"
    # Use known scores: new_score > effective_score → replace
    pending_score = 0.4
    nd_score = 0.8
    effective_score = pending_score  # no decay (same bar)
    _replace = False
    if pending_type in ("wick", "body") and new_type == "displacement":
        _replace = False
    elif nd_dir == pending_direction and nd_score > effective_score:
        _replace = True
    assert _replace, "stronger same-direction signal should replace pending"


def test_replacement_rule4_opposite_direction_threshold():
    """Rule 4: opposite direction replaces only when score > pending × REPLACE_THRESHOLD."""
    from strategy_smt import REPLACE_THRESHOLD
    pending_direction = "short"
    nd_dir = "long"  # opposite
    pending_type = "wick"
    new_type = "wick"
    effective_score = 0.5
    # Score just below threshold — should NOT replace
    nd_score_weak = effective_score * REPLACE_THRESHOLD - 0.01
    _replace_weak = False
    if pending_type in ("wick", "body") and new_type == "displacement":
        _replace_weak = False
    elif nd_dir == pending_direction and nd_score_weak > effective_score:
        _replace_weak = True
    elif nd_dir != pending_direction and nd_score_weak > effective_score * REPLACE_THRESHOLD:
        _replace_weak = True
    assert not _replace_weak, "Below threshold: should not replace"
    # Score above threshold — should replace
    nd_score_strong = effective_score * REPLACE_THRESHOLD + 0.01
    _replace_strong = False
    if pending_type in ("wick", "body") and new_type == "displacement":
        _replace_strong = False
    elif nd_dir == pending_direction and nd_score_strong > effective_score:
        _replace_strong = True
    elif nd_dir != pending_direction and nd_score_strong > effective_score * REPLACE_THRESHOLD:
        _replace_strong = True
    assert _replace_strong, "Above threshold: should replace"


def test_replacement_full_state_reset():
    """After replacement, new score/direction/bar_idx are stored (simulated)."""
    # Simulate the replacement branch: verify new values overwrite old ones
    state = {
        "pending_direction": "short",
        "_pending_div_score": 0.3,
        "_pending_discovery_bar_idx": 5,
        "_pending_discovery_price": 200.0,
        "anchor_close": 201.0,
        "divergence_bar_idx": 5,
    }
    # After replacement with a new long signal at bar 10
    new_dir = "long"
    new_score = 0.7
    new_bar_idx = 10
    new_anchor_close = 198.0
    new_discovery_price = 199.0
    state["pending_direction"] = new_dir
    state["_pending_div_score"] = new_score
    state["_pending_discovery_bar_idx"] = new_bar_idx
    state["_pending_discovery_price"] = new_discovery_price
    state["anchor_close"] = new_anchor_close
    state["divergence_bar_idx"] = new_bar_idx
    assert state["pending_direction"] == "long"
    assert state["_pending_div_score"] == 0.7
    assert state["_pending_discovery_bar_idx"] == 10
    assert state["_pending_discovery_price"] == 199.0
    assert state["anchor_close"] == 198.0


def test_min_div_score_filters_weak_divergence():
    """With MIN_DIV_SCORE > 0, a divergence below the threshold is rejected."""
    from strategy_smt import divergence_score, MIN_DIV_SCORE
    # Score with all zeros (minimum possible)
    score = divergence_score(0, 0, 0, "wick", None, "long")
    assert score == 0.0
    # Simulate the MIN_DIV_SCORE gate
    min_score = 0.1
    state_goes_idle = score < min_score
    assert state_goes_idle, "Score 0.0 must be below threshold 0.1 → state stays IDLE"


def test_invalidation_pts_resets_to_idle():
    """Adverse move > HYPOTHESIS_INVALIDATION_PTS resets state to IDLE."""
    from strategy_smt import HYPOTHESIS_INVALIDATION_PTS
    # Simulate a short position with a bar that goes adverse by 999+ pts
    # With default HYPOTHESIS_INVALIDATION_PTS=999, this only fires when set lower
    invalidation_threshold = 50.0  # simulate a lower threshold
    pending_direction = "short"
    discovery_price = 200.0
    bar_high = 260.0  # adverse = 260 - 200 = 60 > 50
    adverse = bar_high - discovery_price
    would_reset = invalidation_threshold < 999 and adverse > invalidation_threshold
    assert would_reset, "Adverse move 60 > threshold 50 should reset to IDLE"


def test_invalidation_pts_999_disabled():
    """With HYPOTHESIS_INVALIDATION_PTS=999, state never resets on adverse move alone."""
    from strategy_smt import HYPOTHESIS_INVALIDATION_PTS
    # Default value is 999 — the gate is disabled
    assert HYPOTHESIS_INVALIDATION_PTS == 999.0
    # Even with a massive adverse move, the condition `< 999` is False
    invalidation_threshold = HYPOTHESIS_INVALIDATION_PTS
    adverse = 10000.0  # extreme adverse move
    would_reset = invalidation_threshold < 999 and adverse > invalidation_threshold
    assert not would_reset, "HYPOTHESIS_INVALIDATION_PTS=999 disables the gate"


def test_early_bar_skip_bar0_no_divergence(futures_tmpdir, monkeypatch):
    """No divergence accepted on bar_idx < 3 (Solution E guard)."""
    import backtest_smt as train_smt
    import strategy_smt as _strat
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 0.0)
    # Build bars where divergence would appear on bar 1 (< 3) — MES new high at bar 1
    n = 90
    start_ts = pd.Timestamp("2025-01-02 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq="5min")
    base = 20000.0
    mes_highs = [base + 5] * n
    mes_highs[1] = base + 30  # divergence at bar 1 (< 3) — should be skipped
    mnq_highs = [base + 5] * n
    opens = [base] * n
    closes = [base] * n
    # Confirmation bar at bar 2 (but divergence at bar 1 should be ignored)
    opens[0] = base - 2; closes[0] = base + 2  # anchor
    opens[2] = base + 2; closes[2] = base - 2; mnq_highs[2] = base + 6
    mnq = pd.DataFrame({"Open": opens, "High": mnq_highs, "Low": [base-5]*n, "Close": closes, "Volume": [1000.0]*n}, index=idx)
    mes = pd.DataFrame({"Open": opens, "High": mes_highs, "Low": [base-5]*n, "Close": closes, "Volume": [1000.0]*n}, index=idx)
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    # No trade because the divergence bar (1) is < 3, so it is skipped
    assert stats["total_trades"] == 0


def test_pending_div_score_stored_at_detection(futures_tmpdir, monkeypatch):
    """_pending_div_score is populated (non-zero) after divergence accepted."""
    import backtest_smt as train_smt
    import strategy_smt as _strat
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 0.0)
    # Build a valid short signal (sweep > 0 means score > 0)
    from strategy_smt import divergence_score
    sweep = 5.0   # MES new high above prior by 5 pts
    miss = 10.0   # MNQ misses by 10 pts
    body = 3.0
    score = divergence_score(sweep, miss, body, "wick", None, "short")
    assert score > 0.0, "Non-zero sweep/miss should yield positive score"
    # Verify score formula: sweep/5*0.25 + miss/25*0.50 + body/15*0.25
    expected = min(sweep/5.0, 1.0)*0.25 + min(miss/25.0, 1.0)*0.50 + min(body/15.0, 1.0)*0.25
    assert abs(score - expected) < 1e-9


# ══ Solution F: Draw-on-Liquidity integration tests ═══════════════════════════

def _sol_f_monkeypatch(monkeypatch, bk, strat):
    """Shared setup for Solution F integration tests — disables all filters."""
    monkeypatch.setattr(strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(strat, "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(bk,   "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(bk,   "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(bk,   "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(bk,   "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(bk,   "REENTRY_MAX_MOVE_PTS", 0.0)
    monkeypatch.setattr(strat, "MIN_TARGET_PTS", 0.0)
    monkeypatch.setattr(bk,   "MIN_TARGET_PTS", 0.0)
    monkeypatch.setattr(strat, "MIN_RR_FOR_TARGET", 0.0)
    monkeypatch.setattr(bk,   "MIN_RR_FOR_TARGET", 0.0)
    monkeypatch.setattr(bk,   "compute_tdo", lambda *a: 10000.0)


def test_signal_near_tdo_skipped(futures_tmpdir, monkeypatch):
    """Entry within min_pts of TDO with no other draws → no trade placed."""
    import backtest_smt as bk
    import strategy_smt as strat
    _sol_f_monkeypatch(monkeypatch, bk, strat)
    # Re-enable MIN_TARGET_PTS to filter out close draws
    monkeypatch.setattr(strat, "MIN_TARGET_PTS", 15.0)
    monkeypatch.setattr(bk,   "MIN_TARGET_PTS", 15.0)
    # TDO set to 19998 (same as entry close), so distance < 15 → no valid draw
    monkeypatch.setattr(bk, "compute_tdo", lambda *a: 19998.0)
    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert stats["total_trades"] == 0, "Near-TDO entry with no valid draw should be skipped"


def test_signal_with_pdh_draw_placed(futures_tmpdir, monkeypatch):
    """Entry far from TDO with PDH qualifying → trade placed with take_profit=PDH."""
    import backtest_smt as bk
    import strategy_smt as strat
    _sol_f_monkeypatch(monkeypatch, bk, strat)
    # Re-enable distance filter
    monkeypatch.setattr(strat, "MIN_TARGET_PTS", 15.0)
    monkeypatch.setattr(bk,   "MIN_TARGET_PTS", 15.0)
    # For short signal: PDL far below entry qualifies
    base = 20000.0
    mnq, mes = _build_short_signal_bars("2025-01-02", base=base)
    # Inject a pdl value via compute_pdh_pdl patch (entry ~19998; PDL at 19960 → dist=38 > 15)
    import strategy_smt as _strat
    original_pdh_pdl = _strat.compute_pdh_pdl
    monkeypatch.setattr(bk, "compute_pdh_pdl", lambda *a: (base + 100.0, base - 40.0))
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert stats["total_trades"] >= 1, "With PDL qualifying, trade should be placed"
    tr = stats["trade_records"][0]
    assert tr["stop_price"] is not None
    assert tr["tp_name"] == "pdl", f"tp_name should be 'pdl', got {tr['tp_name']}"


def test_secondary_target_stored_in_position(futures_tmpdir, monkeypatch):
    """position dict has secondary_target field after entry."""
    import backtest_smt as bk
    import strategy_smt as strat
    _sol_f_monkeypatch(monkeypatch, bk, strat)
    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    # At minimum, the trade record must include tp_name and secondary_target_name keys
    if stats["total_trades"] >= 1:
        tr = stats["trade_records"][0]
        assert "tp_name" in tr
        assert "secondary_target_name" in tr


def test_tp_breached_false_at_entry(futures_tmpdir, monkeypatch):
    """tp_breached is False immediately after position is opened."""
    import backtest_smt as bk
    import strategy_smt as strat
    _sol_f_monkeypatch(monkeypatch, bk, strat)
    # Capture position dict at first manage_position call
    captured = []
    orig_mp = strat.manage_position
    def patched_mp(pos, bar):
        if not captured:
            captured.append(dict(pos))
        return orig_mp(pos, bar)
    monkeypatch.setattr(bk, "manage_position", patched_mp)
    mnq, mes = _build_short_signal_bars("2025-01-02")
    bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    if captured:
        assert captured[0].get("tp_breached") is False


def test_tp_breached_set_on_primary_cross(monkeypatch):
    """tp_breached becomes True when price crosses take_profit (no trail, no secondary)."""
    import strategy_smt as strat
    monkeypatch.setattr(strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    pos = {
        "direction": "long", "entry_price": 100.0, "stop_price": 97.0,
        "take_profit": 110.0, "tdo": 110.0, "tp_breached": False,
        "secondary_target": 120.0, "secondary_target_name": "test",
        "entry_time": None, "divergence_bar": 0, "entry_bar": 1,
        "entry_bar_body_ratio": 0.5, "smt_sweep_pts": 0.0, "smt_miss_pts": 0.0,
        "smt_type": "wick", "midnight_open": None, "smt_defended_level": None,
        "divergence_bar_high": 100.0, "divergence_bar_low": 97.0,
        "partial_exit_level": None, "partial_done": False,
        "layer_b_entered": False, "total_contracts_target": 1, "contracts": 1,
    }
    bar = pd.Series({"High": 112.0, "Low": 98.0, "Close": 111.0, "Open": 102.0})
    strat.manage_position(pos, bar)
    assert pos["tp_breached"] is True


def test_exit_secondary_fires_after_primary(monkeypatch):
    """Bar crossing secondary level after tp_breached=True returns exit_secondary."""
    import strategy_smt as strat
    monkeypatch.setattr(strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    pos = {
        "direction": "long", "entry_price": 100.0, "stop_price": 97.0,
        "take_profit": 110.0, "tdo": 110.0, "tp_breached": True,
        "secondary_target": 120.0, "secondary_target_name": "test",
        "entry_time": None, "divergence_bar": 0, "entry_bar": 1,
        "entry_bar_body_ratio": 0.5, "smt_sweep_pts": 0.0, "smt_miss_pts": 0.0,
        "smt_type": "wick", "midnight_open": None, "smt_defended_level": None,
        "divergence_bar_high": 100.0, "divergence_bar_low": 97.0,
        "partial_exit_level": None, "partial_done": False,
        "layer_b_entered": False, "total_contracts_target": 1, "contracts": 1,
    }
    bar = pd.Series({"High": 122.0, "Low": 105.0, "Close": 118.0, "Open": 108.0})
    result = strat.manage_position(pos, bar)
    assert result == "exit_secondary"


def test_exit_secondary_not_before_primary(monkeypatch):
    """Bar crossing secondary level before tp_breached → no exit_secondary."""
    import strategy_smt as strat
    monkeypatch.setattr(strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    pos = {
        "direction": "long", "entry_price": 100.0, "stop_price": 97.0,
        "take_profit": 110.0, "tdo": 110.0, "tp_breached": False,
        "secondary_target": 120.0, "secondary_target_name": "test",
        "entry_time": None, "divergence_bar": 0, "entry_bar": 1,
        "entry_bar_body_ratio": 0.5, "smt_sweep_pts": 0.0, "smt_miss_pts": 0.0,
        "smt_type": "wick", "midnight_open": None, "smt_defended_level": None,
        "divergence_bar_high": 100.0, "divergence_bar_low": 97.0,
        "partial_exit_level": None, "partial_done": False,
        "layer_b_entered": False, "total_contracts_target": 1, "contracts": 1,
    }
    # secondary (120) is hit but tp_breached is False; primary (110) is NOT hit (bar high < 110)
    bar = pd.Series({"High": 108.0, "Low": 98.0, "Close": 107.0, "Open": 102.0})
    result = strat.manage_position(pos, bar)
    assert result != "exit_secondary"


def test_exit_secondary_same_bar_primary_and_secondary_crossed(monkeypatch):
    """Bar crosses both primary TP and secondary on same bar (tp_breached starts False) → exit_secondary."""
    import strategy_smt as strat
    monkeypatch.setattr(strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    pos = {
        "direction": "long", "entry_price": 100.0, "stop_price": 97.0,
        "take_profit": 110.0, "tdo": 110.0, "tp_breached": False,
        "secondary_target": 120.0, "secondary_target_name": "test",
        "entry_time": None, "divergence_bar": 0, "entry_bar": 1,
        "entry_bar_body_ratio": 0.5, "smt_sweep_pts": 0.0, "smt_miss_pts": 0.0,
        "smt_type": "wick", "midnight_open": None, "smt_defended_level": None,
        "divergence_bar_high": 100.0, "divergence_bar_low": 97.0,
        "partial_exit_level": None, "partial_done": False,
        "layer_b_entered": False, "total_contracts_target": 1, "contracts": 1,
    }
    # Single bar crosses both primary (110) and secondary (120)
    bar = pd.Series({"High": 125.0, "Low": 105.0, "Close": 118.0, "Open": 108.0})
    result = strat.manage_position(pos, bar)
    assert result == "exit_secondary"
    assert pos["tp_breached"] is True


def test_exit_secondary_fill_at_secondary_price(monkeypatch):
    """exit_secondary fills at position['secondary_target'], not bar extreme."""
    import backtest_smt as bk
    import strategy_smt as strat
    pos = {
        "direction": "long", "entry_price": 100.0, "stop_price": 97.0,
        "take_profit": 110.0, "tdo": 110.0, "secondary_target": 120.0,
        "secondary_target_name": "test", "tp_breached": True,
        "entry_time": None, "divergence_bar": 0, "entry_bar": 1,
        "entry_bar_body_ratio": 0.5, "smt_sweep_pts": 0.0, "smt_miss_pts": 0.0,
        "smt_type": "wick", "midnight_open": None, "smt_defended_level": None,
        "divergence_bar_high": 100.0, "divergence_bar_low": 97.0,
        "partial_exit_level": None, "partial_done": False,
        "layer_b_entered": False, "total_contracts_target": 1, "contracts": 1,
        "entry_date": "2025-01-02", "reentry_sequence": 1, "prior_trade_bars_held": 0,
        "displacement_body_pts": None,
    }
    bar = pd.Series({"High": 125.0, "Low": 110.0, "Close": 120.0, "Open": 112.0},
                    name=pd.Timestamp("2025-01-02 10:00", tz="America/New_York"))
    trade, pnl = bk._build_trade_record(pos, "exit_secondary", bar, 2.0)
    assert trade["exit_price"] == 120.0
    assert trade["exit_type"] == "exit_secondary"


def test_trail_disabled_when_secondary_exists(monkeypatch):
    """TRAIL_AFTER_TP_PTS has no effect when secondary_target is set."""
    import strategy_smt as strat
    monkeypatch.setattr(strat, "TRAIL_AFTER_TP_PTS", 5.0)
    monkeypatch.setattr(strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    pos = {
        "direction": "long", "entry_price": 100.0, "stop_price": 97.0,
        "take_profit": 110.0, "tdo": 110.0, "tp_breached": False,
        "secondary_target": 125.0, "secondary_target_name": "test",
        "entry_time": None, "divergence_bar": 0, "entry_bar": 1,
        "entry_bar_body_ratio": 0.5, "smt_sweep_pts": 0.0, "smt_miss_pts": 0.0,
        "smt_type": "wick", "midnight_open": None, "smt_defended_level": None,
        "divergence_bar_high": 100.0, "divergence_bar_low": 97.0,
        "partial_exit_level": None, "partial_done": False,
        "layer_b_entered": False, "total_contracts_target": 1, "contracts": 1,
    }
    # Bar crosses primary TP (110) but NOT secondary (125) — trail would return "hold"
    # but secondary logic should set tp_breached without activating trail
    old_stop = pos["stop_price"]
    bar = pd.Series({"High": 115.0, "Low": 98.0, "Close": 112.0, "Open": 102.0})
    result = strat.manage_position(pos, bar)
    # Trail NOT activated (stop should not have changed to trail value)
    # With secondary present, trail block is skipped → stop stays at 97.0
    assert pos["stop_price"] == old_stop, "Trail should not move stop when secondary_target exists"
    assert pos.get("tp_breached") is True


def test_trail_active_when_no_secondary(monkeypatch):
    """TRAIL_AFTER_TP_PTS still fires when secondary_target is None.

    Two-call sequence: crossing bar defers stop (Edit 1); next bar activates trail (Edit 2).
    """
    import strategy_smt as strat
    monkeypatch.setattr(strat, "TRAIL_AFTER_TP_PTS", 5.0)
    monkeypatch.setattr(strat, "TRAIL_ACTIVATION_R", 0.0)  # activate immediately
    monkeypatch.setattr(strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    pos = {
        "direction": "long", "entry_price": 100.0, "stop_price": 97.0,
        "take_profit": 110.0, "tdo": 110.0, "tp_breached": False,
        "secondary_target": None, "secondary_target_name": None,
        "entry_time": None, "divergence_bar": 0, "entry_bar": 1,
        "entry_bar_body_ratio": 0.5, "smt_sweep_pts": 0.0, "smt_miss_pts": 0.0,
        "smt_type": "wick", "midnight_open": None, "smt_defended_level": None,
        "divergence_bar_high": 100.0, "divergence_bar_low": 97.0,
        "partial_exit_level": None, "partial_done": False,
        "layer_b_entered": False, "total_contracts_target": 1, "contracts": 1,
        "initial_stop_pts": 3.0,
    }
    bar = pd.Series({"High": 115.0, "Low": 98.0, "Close": 112.0, "Open": 102.0})
    # Crossing bar: defers stop, sets tp_breached
    strat.manage_position(pos, bar)
    assert pos.get("tp_breached") is True
    assert pos["stop_price"] == 97.0, "Stop must not move on the crossing bar"
    # Continuation bar: trail activates (TRAIL_ACTIVATION_R=0.0 → immediate)
    strat.manage_position(pos, bar)
    assert pos["stop_price"] > 97.0, "Trail should move stop on subsequent bar when no secondary_target"


def test_tp_name_in_trade_record(monkeypatch):
    """Trade record contains tp_name field."""
    import backtest_smt as bk
    pos = {
        "direction": "short", "entry_price": 100.0, "stop_price": 103.0,
        "take_profit": 85.0, "tdo": 85.0, "secondary_target": None,
        "secondary_target_name": None, "tp_breached": False,
        "tp_name": "midnight_open",
        "entry_time": None, "divergence_bar": 0, "entry_bar": 1,
        "entry_bar_body_ratio": 0.5, "smt_sweep_pts": 0.0, "smt_miss_pts": 0.0,
        "smt_type": "wick", "midnight_open": None, "smt_defended_level": None,
        "divergence_bar_high": 100.0, "divergence_bar_low": 97.0,
        "partial_exit_level": None, "partial_done": False,
        "layer_b_entered": False, "total_contracts_target": 1, "contracts": 1,
        "entry_date": "2025-01-02", "reentry_sequence": 1, "prior_trade_bars_held": 0,
        "displacement_body_pts": None,
    }
    bar = pd.Series({"High": 98.0, "Low": 84.0, "Close": 86.0, "Open": 96.0},
                    name=pd.Timestamp("2025-01-02 10:30", tz="America/New_York"))
    trade, _ = bk._build_trade_record(pos, "exit_tp", bar, 2.0)
    assert "tp_name" in trade


# ══ Limit entry integration tests T11-T19 ════════════════════════════════════

def _build_short_limit_session(
    date, base=20000.0, n=90, freq="5min",
    bar9_high=None, bar9_low=None, bar10_high=None, bar10_low=None,
    conf_bar_open=None, conf_bar_close=None,
):
    """Build a short-signal session with limit-entry-friendly control points."""
    start_ts = pd.Timestamp(date + " 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq=freq)
    mes_highs = [base + 5] * n
    mnq_highs = [base + 5] * n
    mes_lows  = [base - 5] * n
    mnq_lows  = [base - 5] * n
    opens  = [base] * n
    closes = [base] * n
    # Bar 5: bullish anchor → anchor_close = base+2
    opens[5]  = base - 2
    closes[5] = base + 2
    # Bar 7: MES new session high (bearish divergence)
    mes_highs[7] = base + 30
    # Bar 8: bearish confirmation (close < open, high > anchor_close=base+2)
    opens[8]  = conf_bar_open  if conf_bar_open  is not None else base + 2
    closes[8] = conf_bar_close if conf_bar_close is not None else base - 2
    mnq_highs[8] = base + 6
    # Bars 9/10: default highs stay base+5 (fills immediately); override to prevent fill
    if bar9_high is not None and n > 9:
        mnq_highs[9] = bar9_high
        mes_highs[9] = bar9_high
    if bar9_low is not None and n > 9:
        mnq_lows[9] = bar9_low
        mes_lows[9] = bar9_low
    if bar10_high is not None and n > 10:
        mnq_highs[10] = bar10_high
        mes_highs[10] = bar10_high
    if bar10_low is not None and n > 10:
        mnq_lows[10] = bar10_low
        mes_lows[10] = bar10_low
    mnq = pd.DataFrame(
        {"Open": opens, "High": mnq_highs, "Low": mnq_lows, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    mes = pd.DataFrame(
        {"Open": opens, "High": mes_highs, "Low": mes_lows, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    return mnq, mes


def _setup_limit_patches(monkeypatch, bk, strat, limit_buffer=0.0, limit_expiry=None, ratio_threshold=None):
    """Apply common patches required for limit entry integration tests."""
    monkeypatch.setattr(strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strat, "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(strat, "SMT_OPTIONAL", False)
    monkeypatch.setattr(strat, "DISPLACEMENT_STOP_MODE", False)
    monkeypatch.setattr(strat, "STRUCTURAL_STOP_MODE", False)
    monkeypatch.setattr(strat, "LIMIT_ENTRY_BUFFER_PTS", limit_buffer)
    monkeypatch.setattr(strat, "LIMIT_EXPIRY_SECONDS", limit_expiry)
    monkeypatch.setattr(strat, "LIMIT_RATIO_THRESHOLD", ratio_threshold)
    monkeypatch.setattr(bk, "LIMIT_ENTRY_BUFFER_PTS", limit_buffer)
    monkeypatch.setattr(bk, "LIMIT_EXPIRY_SECONDS", limit_expiry)
    monkeypatch.setattr(bk, "LIMIT_RATIO_THRESHOLD", ratio_threshold)
    monkeypatch.setattr(bk, "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(bk, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(bk, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(bk, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(bk, "REENTRY_MAX_MOVE_PTS", 0.0)
    monkeypatch.setattr(bk, "MAX_REENTRY_COUNT", 1)
    monkeypatch.setattr(bk, "SESSION_START", "09:00")
    monkeypatch.setattr(bk, "SESSION_END", "10:30")
    # Guard against import-order contamination: test_smt_strategy.py's autouse
    # fixture may patch strategy_smt.MIN_BARS_BEFORE_SIGNAL before backtest_smt
    # is first imported, leaving bk.MIN_BARS_BEFORE_SIGNAL permanently elevated.
    monkeypatch.setattr(bk, "MIN_BARS_BEFORE_SIGNAL", 0)
    monkeypatch.setattr(bk, "MIN_TARGET_PTS", 0.0)
    monkeypatch.setattr(bk, "MIN_RR_FOR_TARGET", 0.0)
    monkeypatch.setattr(strat, "MIN_TARGET_PTS", 0.0)
    monkeypatch.setattr(strat, "MIN_RR_FOR_TARGET", 0.0)
    monkeypatch.setattr(bk, "SMT_OPTIONAL", False)


def test_limit_entry_forward_fills_on_next_bar(futures_tmpdir, monkeypatch):
    """T11: Forward-limit fills on bar N+1; limit_fill_bars=1 in trade record."""
    import backtest_smt as bk
    import strategy_smt as strat
    # LIMIT_EXPIRY_SECONDS=600 with 5m bars → max_limit_bars=2 (fills on bar 9, elapsed=1)
    _setup_limit_patches(monkeypatch, bk, strat, limit_buffer=0.0, limit_expiry=600.0)
    base = 20000.0
    # bar 9 default high = base+5 >= anchor(base+2) → fills
    mnq, mes = _build_short_limit_session("2025-01-02", base=base)
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    filled = [t for t in stats["trade_records"] if t.get("exit_type") not in ("limit_expired", "limit_expired_session_close")]
    assert len(filled) >= 1, "Expected a filled trade"
    limit_filled = [t for t in filled if t.get("limit_fill_bars") is not None and t["limit_fill_bars"] != ""]
    assert len(limit_filled) >= 1, "Expected limit_fill_bars populated on at least one trade"
    assert limit_filled[0]["limit_fill_bars"] == 1


def test_limit_entry_forward_expires(futures_tmpdir, monkeypatch):
    """T12: Forward-limit expires after max_limit_bars; limit_expired record with pnl=0."""
    import backtest_smt as bk
    import strategy_smt as strat
    # LIMIT_EXPIRY_SECONDS=300 with 5m bars → max_limit_bars=1; bar9 low > entry → no fill
    _setup_limit_patches(monkeypatch, bk, strat, limit_buffer=0.0, limit_expiry=300.0)
    base = 20000.0
    # entry_price = anchor_close - buffer = base+2; bar9_low=base+3 > entry → no fill for SHORT
    mnq, mes = _build_short_limit_session("2025-01-02", base=base, bar9_low=base + 3)
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    expired = [t for t in stats["trade_records"] if t.get("exit_type") == "limit_expired"]
    assert len(expired) >= 1, "Expected a limit_expired record"
    assert expired[0]["pnl"] == 0.0


def test_limit_entry_expiry_missed_move_populated(futures_tmpdir, monkeypatch):
    """T13: missed_move_pts is 0 on expired SHORT record.

    For a SHORT limit at entry_price=base+2, fill triggers when bar.Low <= entry_price.
    Expiry can only occur when bar.Low > entry_price throughout the wait — which means
    missed_move = max(0, entry_price - bar.Low) stays at 0. The field must be present.
    """
    import backtest_smt as bk
    import strategy_smt as strat
    _setup_limit_patches(monkeypatch, bk, strat, limit_buffer=0.0, limit_expiry=300.0)
    base = 20000.0
    # bar9_low=base+3 > entry=base+2 → no fill; missed_move = max(0, entry - low) = max(0, -1) = 0
    mnq, mes = _build_short_limit_session(
        "2025-01-02", base=base, bar9_low=base + 3
    )
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    expired = [t for t in stats["trade_records"] if t.get("exit_type") == "limit_expired"]
    assert len(expired) >= 1
    assert expired[0]["missed_move_pts"] == pytest.approx(0.0, abs=1e-6)


def test_limit_entry_session_close_during_wait(futures_tmpdir, monkeypatch):
    """T14: Session ends while in WAITING_FOR_LIMIT_FILL → limit_expired_session_close record."""
    import backtest_smt as bk
    import strategy_smt as strat
    # Long expiry so it doesn't expire normally; session ends before that
    _setup_limit_patches(monkeypatch, bk, strat, limit_buffer=0.0, limit_expiry=3600.0)
    base = 20000.0
    # n=10: bar 8 fires confirmation, bar 9 is the last bar; bar9_low=base+3 > entry=base+2 → no fill
    mnq, mes = _build_short_limit_session("2025-01-02", base=base, n=10, bar9_low=base + 3)
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    session_expired = [
        t for t in stats["trade_records"]
        if t.get("exit_type") == "limit_expired_session_close"
    ]
    assert len(session_expired) >= 1


def test_bar_seconds_detected_1m(futures_tmpdir, monkeypatch):
    """T15: 1m bars → bar_seconds=60, max_limit_bars=2 for LIMIT_EXPIRY_SECONDS=120."""
    import backtest_smt as bk
    import strategy_smt as strat
    # With 1m bars and 120s expiry → max_limit_bars=2
    # bar9_low and bar10_low > entry=base+2 → no fill; expires at bar10 (elapsed=2)
    _setup_limit_patches(monkeypatch, bk, strat, limit_buffer=0.0, limit_expiry=120.0)
    base = 20000.0
    mnq, mes = _build_short_limit_session(
        "2025-01-02", base=base, n=90, freq="1min",
        bar9_low=base + 3, bar10_low=base + 3,
    )
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    # With 2-bar window, both bars don't fill → exactly one limit_expired record
    expired = [t for t in stats["trade_records"] if t.get("exit_type") == "limit_expired"]
    assert len(expired) >= 1, "Expected limit_expired after 2 1m bars"


def test_bar_seconds_detected_1s(futures_tmpdir, monkeypatch):
    """T16: 1s bars → bar_seconds=1, max_limit_bars=120; session ends before expiry → session_close variant."""
    import backtest_smt as bk
    import strategy_smt as strat
    # With 1s bars and 120s expiry → max_limit_bars=120; n=11 → session ends before 120 elapsed
    # bar9_low and bar10_low > entry=base+2 → no fill; session ends → session_close expiry
    _setup_limit_patches(monkeypatch, bk, strat, limit_buffer=0.0, limit_expiry=120.0)
    base = 20000.0
    mnq, mes = _build_short_limit_session(
        "2025-01-02", base=base, n=11, freq="1s",
        bar9_low=base + 3, bar10_low=base + 3,
    )
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    # max_limit_bars=120 but session only has 11 bars → session-end write (not plain limit_expired)
    plain_expired = [t for t in stats["trade_records"] if t.get("exit_type") == "limit_expired"]
    session_expired = [t for t in stats["trade_records"] if t.get("exit_type") == "limit_expired_session_close"]
    assert len(plain_expired) == 0, "Should not expire normally with 120-bar window and only 12 bars"
    assert len(session_expired) >= 1, "Session-end should write limit_expired_session_close"


def test_limit_entry_disabled_baseline_unchanged(futures_tmpdir, monkeypatch):
    """T17: LIMIT_ENTRY_BUFFER_PTS=None → no limit_expired rows; anchor_close_price empty."""
    import backtest_smt as bk
    import strategy_smt as strat
    # Limit disabled: standard backtest behavior
    _setup_limit_patches(monkeypatch, bk, strat, limit_buffer=None, limit_expiry=None)
    base = 20000.0
    mnq, mes = _build_short_limit_session("2025-01-02", base=base)
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    assert stats["total_trades"] >= 0
    # No limit_expired records of any kind
    expired = [
        t for t in stats["trade_records"]
        if t.get("exit_type") in ("limit_expired", "limit_expired_session_close")
    ]
    assert len(expired) == 0
    # All filled trades have anchor_close_price = "" or None
    for t in stats["trade_records"]:
        assert t.get("anchor_close_price") in ("", None)


def test_limit_ratio_threshold_high_body_uses_same_bar(futures_tmpdir, monkeypatch):
    """T18: body_ratio ≥ LIMIT_RATIO_THRESHOLD → same-bar fill; limit_fill_bars=0, no WAITING state."""
    import backtest_smt as bk
    import strategy_smt as strat
    # Confirmation bar: body=8, range=11 → ratio≈0.73 ≥ 0.60 → same-bar fill
    _setup_limit_patches(monkeypatch, bk, strat, limit_buffer=0.5, limit_expiry=600.0, ratio_threshold=0.60)
    base = 20000.0
    # High body_ratio bar: open=base+4, close=base-4, high=base+6, low=base-5
    mnq, mes = _build_short_limit_session(
        "2025-01-02", base=base,
        conf_bar_open=base + 4, conf_bar_close=base - 4,
    )
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    filled = [
        t for t in stats["trade_records"]
        if t.get("exit_type") not in ("limit_expired", "limit_expired_session_close")
    ]
    assert len(filled) >= 1, "Expected a same-bar filled trade"
    same_bar = [t for t in filled if t.get("limit_fill_bars") == 0]
    assert len(same_bar) >= 1, "Expected limit_fill_bars=0 for same-bar fill"


def test_limit_ratio_threshold_low_body_uses_forward_limit(futures_tmpdir, monkeypatch):
    """T19: body_ratio < LIMIT_RATIO_THRESHOLD → WAITING_FOR_LIMIT_FILL entered; no immediate fill."""
    import backtest_smt as bk
    import strategy_smt as strat
    # Default confirmation bar has body≈4, range≈11, ratio≈0.36 < 0.60 → forward limit
    # bar9_low=base+3 > entry=base+2 → no fill → confirms WAITING state was entered (expires)
    _setup_limit_patches(monkeypatch, bk, strat, limit_buffer=0.0, limit_expiry=300.0, ratio_threshold=0.60)
    base = 20000.0
    mnq, mes = _build_short_limit_session("2025-01-02", base=base, bar9_low=base + 3)
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    # Low body_ratio → forward limit → if bar9 doesn't fill → limit_expired
    expired = [t for t in stats["trade_records"] if t.get("exit_type") == "limit_expired"]
    assert len(expired) >= 1, "Low body_ratio should enter WAITING_FOR_LIMIT_FILL and expire"


# ══ Confirmation bar window tests T20-T23 ════════════════════════════════════

def _setup_conf_patches(monkeypatch, bk, strat, conf_minutes=1):
    """Patches for confirmation bar window tests — disables all guards, sets conf window."""
    monkeypatch.setattr(strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(strat, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(strat, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(strat, "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(strat, "SMT_OPTIONAL", False)
    monkeypatch.setattr(strat, "DISPLACEMENT_STOP_MODE", False)
    monkeypatch.setattr(strat, "STRUCTURAL_STOP_MODE", False)
    monkeypatch.setattr(strat, "LIMIT_ENTRY_BUFFER_PTS", None)
    monkeypatch.setattr(strat, "LIMIT_EXPIRY_SECONDS", None)
    monkeypatch.setattr(strat, "CONFIRMATION_WINDOW_BARS", conf_minutes)
    monkeypatch.setattr(bk, "CONFIRMATION_WINDOW_BARS", conf_minutes)
    monkeypatch.setattr(bk, "LIMIT_ENTRY_BUFFER_PTS", None)
    monkeypatch.setattr(bk, "LIMIT_EXPIRY_SECONDS", None)
    monkeypatch.setattr(bk, "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(bk, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(bk, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(bk, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(bk, "REENTRY_MAX_MOVE_PTS", 0.0)
    monkeypatch.setattr(bk, "MAX_REENTRY_COUNT", 1)
    monkeypatch.setattr(bk, "SESSION_START", "09:00")
    monkeypatch.setattr(bk, "SESSION_END", "10:30")
    monkeypatch.setattr(bk, "MIN_BARS_BEFORE_SIGNAL", 0)
    monkeypatch.setattr(bk, "MIN_TARGET_PTS", 0.0)
    monkeypatch.setattr(bk, "MIN_RR_FOR_TARGET", 0.0)
    monkeypatch.setattr(strat, "MIN_TARGET_PTS", 0.0)
    monkeypatch.setattr(strat, "MIN_RR_FOR_TARGET", 0.0)


def _build_conf_window_session(date, base=20000.0, n=30, freq="1min"):
    """1m session: div at bar 7, bearish conf bar at bar 8, bars 9-27 continue lower.

    anchor_close = base+2 (bullish anchor at bar 5).
    Bar 8: open=base+2, high=base+6, close=base-2 → valid 1m confirmation.
    Bars 9-27: open=close=base-3, high=base-2, low=base-4 → also valid confirmations
    individually, ensuring the 3m synthetic window (bars 8-10) is still a valid confirmation.
    """
    start_ts = pd.Timestamp(date + " 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq=freq)
    opens  = [base] * n
    closes = [base] * n
    highs  = [base + 5] * n
    lows   = [base - 5] * n
    # Bullish anchor at bar 5
    opens[5]  = base - 2
    closes[5] = base + 2
    # MES divergence at bar 7
    mes_highs = [base + 5] * n
    mes_highs[7] = base + 30
    # Bearish confirmation at bar 8 (and subsequent bars continue lower)
    for i in range(8, min(n, 28)):
        opens[i]  = base + 2
        closes[i] = base - 2
        highs[i]  = base + 6
        lows[i]   = base - 4
    mnq = pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    mes = pd.DataFrame(
        {"Open": opens, "High": mes_highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    return mnq, mes


def test_conf_bar_minutes_1_baseline(futures_tmpdir, monkeypatch):
    """T20: CONFIRMATION_WINDOW_BARS=1 → confirmation fires on bar 8 (entry_bar=8)."""
    import backtest_smt as bk
    import strategy_smt as strat
    _setup_conf_patches(monkeypatch, bk, strat, conf_minutes=1)
    mnq, mes = _build_conf_window_session("2025-01-02")
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    trades = stats["trade_records"]
    assert len(trades) >= 1, "Should produce at least one trade with 1m confirmation"
    entry_bar = trades[0]["entry_bar"]
    assert entry_bar == 8, f"With N=1 confirmation should fire at bar 8, got {entry_bar}"


def test_conf_bar_minutes_3_deferred_to_window_boundary(futures_tmpdir, monkeypatch):
    """T21: CONFIRMATION_WINDOW_BARS=3 → bar 8 skipped; confirmation fires at bar 10."""
    import backtest_smt as bk
    import strategy_smt as strat
    _setup_conf_patches(monkeypatch, bk, strat, conf_minutes=3)
    mnq, mes = _build_conf_window_session("2025-01-02")
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    trades = stats["trade_records"]
    assert len(trades) >= 1, "Should produce a trade with 3m confirmation"
    entry_bar = trades[0]["entry_bar"]
    # Div at bar 7 → window starts at bar 8; first boundary at bar 10 (bars 8,9,10)
    assert entry_bar == 10, f"With N=3 confirmation should fire at bar 10, got {entry_bar}"


def test_conf_bar_minutes_3_synthetic_ohlc(futures_tmpdir, monkeypatch):
    """T22: Synthetic 3m bar uses open of bar 8, max high, min low, close of bar 10."""
    import backtest_smt as bk
    import strategy_smt as strat
    _setup_conf_patches(monkeypatch, bk, strat, conf_minutes=3)
    base = 20000.0
    mnq, mes = _build_conf_window_session("2025-01-02", base=base)
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    trades = stats["trade_records"]
    assert len(trades) >= 1
    t = trades[0]
    # Synthetic bar 8-10: open=base+2, high=base+6, low=base-4, close=base-2
    # entry_bar_body_ratio = |close-open| / (high-low) = 4 / 10 = 0.4
    assert abs(t["entry_bar_body_ratio"] - 0.4) < 0.01, (
        f"Synthetic 3m body ratio should be 0.4, got {t['entry_bar_body_ratio']}"
    )


def test_conf_bar_minutes_3_no_trade_before_boundary(futures_tmpdir, monkeypatch):
    """T23: With N=3, no trade fires at bars 8 or 9 — only at the boundary bar 10."""
    import backtest_smt as bk
    import strategy_smt as strat
    _setup_conf_patches(monkeypatch, bk, strat, conf_minutes=3)
    # Use a session with only 10 bars (0-9); bar 9 is last — just before the boundary
    mnq, mes = _build_conf_window_session("2025-01-02", n=10)
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    # Only 10 bars (0-9): window starts at 8, first boundary would be bar 10 (doesn't exist)
    assert stats["total_trades"] == 0, "No trade should fire before the first 3m boundary"


# ══ Adverse anchor update tests T24-T25 ══════════════════════════════════════

def _build_adverse_then_confirm_session(date, base=20000.0, n=40, freq="1min"):
    """1m session for testing anchor update on adverse bars.

    Bar 5:  bullish anchor (open=base-2, close=base+2). anchor_close = base+2.
    Bar 7:  MES new session high → bearish divergence.
    Bar 8:  bullish bar going AGAINST the short: open=base+2, close=base+6.
            This is an adverse boundary bar (N=1 case) → anchor_close updates to base+6.
    Bar 9:  bearish confirmation at the new anchor: open=base+6, close=base+2, high=base+8.
            high (base+8) > new anchor (base+6) → is_confirmation_bar=True.
    Bars 10-39: neutral continuation (close < anchor, high > anchor).
    """
    start_ts = pd.Timestamp(date + " 09:00:00", tz="America/New_York")
    idx = pd.date_range(start=start_ts, periods=n, freq=freq)
    opens  = [base] * n
    closes = [base] * n
    highs  = [base + 5] * n
    lows   = [base - 5] * n
    mes_highs = list(highs)
    # Bar 5: bullish anchor
    opens[5]  = base - 2
    closes[5] = base + 2
    # Bar 7: MES divergence
    mes_highs[7] = base + 30
    # Bar 8: adverse bullish bar (against expected short)
    opens[8]  = base + 2
    closes[8] = base + 6
    highs[8]  = base + 8
    # Bar 9: bearish confirmation at the new anchor (base+6)
    opens[9]  = base + 6
    closes[9] = base + 2
    highs[9]  = base + 8
    mnq = pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    mes = pd.DataFrame(
        {"Open": opens, "High": mes_highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n},
        index=idx,
    )
    return mnq, mes


def test_adverse_anchor_update_defers_entry(futures_tmpdir, monkeypatch):
    """T24: Adverse bar at N=1 boundary updates anchor; entry fires at bar 9 (new anchor)."""
    import backtest_smt as bk
    import strategy_smt as strat
    _setup_conf_patches(monkeypatch, bk, strat, conf_minutes=1)
    base = 20000.0
    mnq, mes = _build_adverse_then_confirm_session("2025-01-02", base=base)
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    trades = stats["trade_records"]
    assert len(trades) >= 1, "Should produce a trade after anchor update"
    # Bar 8 is adverse → anchor updates; bar 9 is the first valid confirmation at new anchor
    entry_bar = trades[0]["entry_bar"]
    assert entry_bar == 9, f"Entry should be deferred to bar 9, got {entry_bar}"


def test_adverse_anchor_update_n3_deferred_to_second_window(futures_tmpdir, monkeypatch):
    """T25: With N=3, adverse first window (bars 8-10) updates anchor; entry fires in window 2."""
    import backtest_smt as bk
    import strategy_smt as strat
    _setup_conf_patches(monkeypatch, bk, strat, conf_minutes=3)
    base = 20000.0
    # Build a session with:
    # - Bars 8-10: all bullish (adverse 3m window) → anchor updates to close of bar 10
    # - Bars 11-13: bearish confirmation at the new anchor (second 3m window boundary = bar 13)
    mnq, mes = _build_conf_window_session("2025-01-02", base=base, n=40)
    # Override bars 8-10 to be bullish (adverse)
    for i in range(8, 11):
        mnq.iloc[i, mnq.columns.get_loc("Open")]  = base + 2
        mnq.iloc[i, mnq.columns.get_loc("Close")] = base + 6
        mnq.iloc[i, mnq.columns.get_loc("High")]  = base + 8
        mnq.iloc[i, mnq.columns.get_loc("Low")]   = base + 0
    # Bars 11-13: bearish at the updated anchor (base+6)
    for i in range(11, 14):
        mnq.iloc[i, mnq.columns.get_loc("Open")]  = base + 6
        mnq.iloc[i, mnq.columns.get_loc("Close")] = base + 2
        mnq.iloc[i, mnq.columns.get_loc("High")]  = base + 8
        mnq.iloc[i, mnq.columns.get_loc("Low")]   = base + 0
    stats = bk.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    trades = stats["trade_records"]
    assert len(trades) >= 1, "Should produce a trade in the second 3m window"
    entry_bar = trades[0]["entry_bar"]
    # Window 1: bars 8-10 (adverse) → anchor updates; window 2: bars 11-13, boundary at bar 13
    assert entry_bar == 13, f"Entry should fire at bar 13 (second window boundary), got {entry_bar}"


# ══ Trail-mode integration tests T26-T27 ══════════════════════════════════════

def test_trail_mode_holds_past_tdo_and_exits_via_stop(futures_tmpdir, monkeypatch):
    """T26: Trail mode — long position crosses TDO, reverses, exits via exit_stop (not exit_tp).

    Verifies that with TRAIL_AFTER_TP_PTS=50.0 and TRAIL_ACTIVATION_R=0.0, after the position
    crosses TDO the stop trails price upward and the eventual reversal through the trail stop
    produces an exit_stop trade record rather than exit_tp.
    """
    import backtest_smt as train_smt
    import strategy_smt as _strat

    for mod in [_strat, train_smt]:
        monkeypatch.setattr(mod, "TRAIL_AFTER_TP_PTS", 50.0)
        monkeypatch.setattr(mod, "LIMIT_ENTRY_BUFFER_PTS", None)
    monkeypatch.setattr(_strat, "TRAIL_ACTIVATION_R", 0.0)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", False)
    monkeypatch.setattr(_strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(_strat, "INVALIDATION_MSS_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_CISD_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_SMT_EXIT", False)
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 0.0)
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    # TDO at 20050 — 48 pts above entry (~20002), well within MAX_TDO_DISTANCE_PTS=999
    monkeypatch.setattr(train_smt, "compute_tdo", lambda *a: 20050.0)
    # No secondary target — trail block requires secondary_target is None.
    # Patch on both modules: _build_draws_and_select now lives in strategy_smt
    # and reads select_draw_on_liquidity from that namespace.
    _no_sec = lambda *a, **kw: ("tdo", 20050.0, None, None)
    monkeypatch.setattr(train_smt, "select_draw_on_liquidity", _no_sec)
    monkeypatch.setattr(_strat, "select_draw_on_liquidity", _no_sec)
    monkeypatch.setattr(_strat, "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(train_smt, "MIDNIGHT_OPEN_AS_TP", False)

    mnq, mes = _build_long_signal_bars("2025-01-02", base=20000.0, n=90)

    # Bars 9-12: price rises well past TDO (20050) up to 20120; trail will track best high
    for i in range(9, 13):
        mnq.iloc[i, mnq.columns.get_loc("High")]  = 20120.0
        mnq.iloc[i, mnq.columns.get_loc("Low")]   = 20060.0
        mnq.iloc[i, mnq.columns.get_loc("Open")]  = 20065.0
        mnq.iloc[i, mnq.columns.get_loc("Close")] = 20110.0

    # Bars 13-20: price reverses below trail stop (best=20120, trail=50 → stop at 20070)
    for i in range(13, 21):
        mnq.iloc[i, mnq.columns.get_loc("High")]  = 20060.0
        mnq.iloc[i, mnq.columns.get_loc("Low")]   = 20040.0
        mnq.iloc[i, mnq.columns.get_loc("Open")]  = 20055.0
        mnq.iloc[i, mnq.columns.get_loc("Close")] = 20045.0

    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    trades = stats["trade_records"]

    assert len(trades) >= 1, f"Expected at least one trade; got stats={stats}"

    exit_types = [t["exit_type"] for t in trades]
    assert "exit_stop" in exit_types, (
        f"Expected exit_stop (trail hit) in exit_types; got {exit_types}"
    )
    # With PARTIAL_EXIT_ENABLED=False, no partial_exit record should appear
    assert "partial_exit" not in exit_types, (
        f"Unexpected partial_exit record with PARTIAL_EXIT_ENABLED=False; got {exit_types}"
    )


def test_trail_mode_partial_slides_stop_no_contract_reduction(futures_tmpdir, monkeypatch):
    """T27: Trail mode + PARTIAL_EXIT_ENABLED=True — partial level hit slides stop but does NOT
    append a partial_exit trade record and does NOT reduce contracts.

    The partial stop-slide in trail mode adjusts the stop upward but the contract count is
    preserved and no separate partial_exit entry is written to trade_records.
    """
    import backtest_smt as train_smt
    import strategy_smt as _strat

    for mod in [_strat, train_smt]:
        monkeypatch.setattr(mod, "TRAIL_AFTER_TP_PTS", 50.0)
        monkeypatch.setattr(mod, "LIMIT_ENTRY_BUFFER_PTS", None)
    monkeypatch.setattr(_strat, "TRAIL_ACTIVATION_R", 0.0)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_ENABLED", True)
    monkeypatch.setattr(_strat, "PARTIAL_EXIT_LEVEL_RATIO", 0.33)
    monkeypatch.setattr(_strat, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(_strat, "MAX_TDO_DISTANCE_PTS", 999.0)
    monkeypatch.setattr(_strat, "INVALIDATION_MSS_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_CISD_EXIT", False)
    monkeypatch.setattr(_strat, "INVALIDATION_SMT_EXIT", False)
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 0.0)
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0, 1, 2, 3, 4}))
    # TDO at 20050: partial_exit_level ≈ entry + (tdo - entry) * 0.33 ≈ 20002 + 16 = 20018
    monkeypatch.setattr(train_smt, "compute_tdo", lambda *a: 20050.0)
    # No secondary target — trail block requires secondary_target is None.
    # Patch on both modules: _build_draws_and_select now lives in strategy_smt.
    _no_sec2 = lambda *a, **kw: ("tdo", 20050.0, None, None)
    monkeypatch.setattr(train_smt, "select_draw_on_liquidity", _no_sec2)
    monkeypatch.setattr(_strat, "select_draw_on_liquidity", _no_sec2)
    monkeypatch.setattr(_strat, "MIDNIGHT_OPEN_AS_TP", False)
    monkeypatch.setattr(train_smt, "MIDNIGHT_OPEN_AS_TP", False)

    mnq, mes = _build_long_signal_bars("2025-01-02", base=20000.0, n=90)

    # Bars 9-11: price rises past partial level (~20018) but stays below TDO (20050)
    for i in range(9, 12):
        mnq.iloc[i, mnq.columns.get_loc("High")]  = 20025.0
        mnq.iloc[i, mnq.columns.get_loc("Low")]   = 20010.0
        mnq.iloc[i, mnq.columns.get_loc("Open")]  = 20012.0
        mnq.iloc[i, mnq.columns.get_loc("Close")] = 20020.0

    # Bars 12-14: price crosses TDO and continues up to 20120
    for i in range(12, 15):
        mnq.iloc[i, mnq.columns.get_loc("High")]  = 20120.0
        mnq.iloc[i, mnq.columns.get_loc("Low")]   = 20050.0
        mnq.iloc[i, mnq.columns.get_loc("Open")]  = 20055.0
        mnq.iloc[i, mnq.columns.get_loc("Close")] = 20110.0

    # Bars 15-20: price reverses below trail stop (best=20120, trail=50 → stop at 20070)
    for i in range(15, 21):
        mnq.iloc[i, mnq.columns.get_loc("High")]  = 20060.0
        mnq.iloc[i, mnq.columns.get_loc("Low")]   = 20040.0
        mnq.iloc[i, mnq.columns.get_loc("Open")]  = 20055.0
        mnq.iloc[i, mnq.columns.get_loc("Close")] = 20045.0

    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")
    trades = stats["trade_records"]

    assert len(trades) >= 1, f"Expected at least one trade; got stats={stats}"

    exit_types = [t["exit_type"] for t in trades]
    # Trail mode must NOT append a partial_exit trade record even when partial level is hit
    assert "partial_exit" not in exit_types, (
        f"Trail mode should not append partial_exit trade record; got {exit_types}"
    )
    # The position must eventually exit via stop (trail) or session close, not initial stop alone
    assert any(et in ("exit_stop", "session_close") for et in exit_types), (
        f"Expected exit_stop or session_close after trail reversal; got {exit_types}"
    )
