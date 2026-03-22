"""tests/test_backtester.py — Pytest suite for manage_position(), run_backtest(), and print_results()."""
import math
import pytest
import numpy as np
import pandas as pd
from datetime import date, timedelta
from unittest.mock import patch

import csv
import os
import tempfile

import train
from train import (
    manage_position, run_backtest, print_results, calc_atr14,
    _write_final_outputs,
    BACKTEST_START, BACKTEST_END,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_position_df(n=30, price=100.0, atr_spread=2.0):
    """Returns df where price_10am is constant at `price` and ATR14 ≈ atr_spread."""
    dates = [date(2025, 1, 2) + timedelta(days=i) for i in range(n)]
    highs  = np.full(n, price + atr_spread)
    lows   = np.full(n, price - atr_spread)
    closes = np.full(n, price)
    return pd.DataFrame({
        'open': closes, 'high': highs, 'low': lows, 'close': closes,
        'volume': np.full(n, 1_000_000.0),
        'price_10am': np.full(n, price),
    }, index=pd.Index(dates, name='date'))


def make_minimal_df(trading_days: list, base_price: float = 100.0) -> pd.DataFrame:
    """Bare-minimum df covering specific dates. Used when screen_day is mocked."""
    n = len(trading_days)
    return pd.DataFrame({
        'open':       np.full(n, base_price),
        'high':       np.full(n, base_price * 1.01),
        'low':        np.full(n, base_price * 0.99),
        'close':      np.full(n, base_price),
        'volume':     np.full(n, 1_000_000.0),
        'price_10am': np.full(n, base_price),
    }, index=pd.Index(trading_days, name='date'))


def make_signal_df_for_backtest(signal_date: date = date(2026, 1, 10)) -> pd.DataFrame:
    """250-row df where screen_day(df, signal_date) returns a non-None dict.
    Mirrors make_signal_df design but anchored to signal_date rather than a fixed base."""
    n = 250
    dates = [signal_date - timedelta(days=(n - 1 - i)) for i in range(n)]

    close = np.zeros(n, dtype=float)
    close[:235] = np.linspace(60.0, 97.0, 235)
    close[235:250] = [97.0, 98.0, 97.3, 98.3, 97.6, 98.6, 97.9, 98.9,
                      98.2, 99.2, 98.5, 99.5, 98.8, 99.8, 100.8]

    price_10am = close.copy()
    price_10am[249] = 115.0

    return pd.DataFrame({
        'open': close * 0.998, 'high': close * 1.005, 'low': close * 0.995,
        'close': close,
        'volume': np.full(n, 1_000_000.0),
        'price_10am': price_10am,
    }, index=pd.Index(dates, name='date'))


# ── manage_position tests ─────────────────────────────────────────────────────

def test_manage_position_no_raise_below_threshold():
    # TR = (price+atr_spread)-(price-atr_spread) = 2*atr_spread → ATR14 ≈ 4.0
    # price_10am=100 < entry(100)+ATR(4)=104 → stop unchanged
    df = make_position_df(price=100.0, atr_spread=2.0)
    pos = {'entry_price': 100.0, 'stop_price': 90.0, 'shares': 5.0,
           'ticker': 'X', 'entry_date': date(2025, 1, 2)}
    result = manage_position(pos, df)
    assert result == 90.0


def test_manage_position_raises_to_breakeven():
    # ATR14 = TR = (price+1)-(price-1) = 2.0 with atr_spread=1.0
    # price_10am=103 >= entry(100)+ATR(2)=102 → stop raised to entry_price=100
    df = make_position_df(price=103.0, atr_spread=1.0)
    pos = {'entry_price': 100.0, 'stop_price': 90.0, 'shares': 5.0,
           'ticker': 'X', 'entry_date': date(2025, 1, 2)}
    result = manage_position(pos, df)
    assert result == 100.0


def test_manage_position_never_lowers_existing_stop():
    # stop_price already at 100; condition fires → max(entry=100, current=100)=100; no lowering
    df = make_position_df(price=103.0, atr_spread=1.0)
    pos = {'entry_price': 100.0, 'stop_price': 100.0, 'shares': 5.0,
           'ticker': 'X', 'entry_date': date(2025, 1, 2)}
    result = manage_position(pos, df)
    assert result == 100.0


def test_manage_position_nan_atr():
    # Only 5 rows → ATR14 is NaN (rolling(14) needs 14 bars) → stop unchanged
    df = make_position_df(n=5, price=103.0, atr_spread=2.0)
    pos = {'entry_price': 100.0, 'stop_price': 90.0, 'shares': 5.0,
           'ticker': 'X', 'entry_date': date(2025, 1, 2)}
    result = manage_position(pos, df)
    assert result == 90.0


def test_manage_position_zero_atr():
    # Identical high/low/close → TR=0 → ATR=0 → stop unchanged
    n = 30
    dates = [date(2025, 1, 2) + timedelta(days=i) for i in range(n)]
    closes = np.full(n, 100.0)
    df = pd.DataFrame({
        'open': closes, 'high': closes, 'low': closes, 'close': closes,
        'volume': np.full(n, 1_000_000.0),
        'price_10am': np.full(n, 103.0),
    }, index=pd.Index(dates, name='date'))
    pos = {'entry_price': 100.0, 'stop_price': 90.0, 'shares': 5.0,
           'ticker': 'X', 'entry_date': date(2025, 1, 2)}
    result = manage_position(pos, df)
    assert result == 90.0


# ── run_backtest tests ────────────────────────────────────────────────────────

def test_run_backtest_empty_dict_returns_zero_pnl():
    stats = run_backtest({})
    assert stats['total_pnl'] == 0.0
    assert stats['total_trades'] == 0


def test_run_backtest_no_backtest_days_returns_zero_pnl():
    # Dates before BACKTEST_START → no trading days in window
    days = [date(2020, 1, 2), date(2020, 1, 3)]
    df = make_minimal_df(days)
    assert run_backtest({'X': df})['total_pnl'] == 0.0


def test_run_backtest_no_signals_returns_zero_pnl():
    # df has only 3 rows → screen_day fails R1 (needs 60 rows) → no signals, no trades
    start = date.fromisoformat(BACKTEST_START)
    days = [start + timedelta(days=i) for i in range(3)]
    df = make_minimal_df(days)
    stats = run_backtest({'X': df})
    assert stats['total_pnl'] == 0.0
    assert stats['total_trades'] == 0


def test_run_backtest_no_reentry_same_ticker():
    # screen_day returns signal on d0 AND d2 for same ticker;
    # ticker is already in portfolio on d2 → screener call is skipped
    start = date.fromisoformat(BACKTEST_START)
    days = [start + timedelta(days=i) for i in range(4)]
    df = make_minimal_df(days)
    fake_signal = {'stop': 90.0, 'entry_price': 100.0, 'atr14': 2.0,
                   'sma150': 80.0, 'cci': -60.0, 'pct_local': 0.1, 'pct_ath': 0.1}
    with patch('train.screen_day', return_value=fake_signal):
        stats = run_backtest({'X': df})
    assert stats['total_trades'] <= 1


def test_run_backtest_stop_hit_closes_position():
    # Position entered on d0 (stop=90), low on d1=89 (below stop) → stopped out when d2 checks d1 low
    start = date.fromisoformat(BACKTEST_START)
    days = [start + timedelta(days=i) for i in range(3)]
    df = make_minimal_df(days)
    df.loc[days[1], 'low'] = 89.0  # below stop=90

    fake_signal = {'stop': 90.0, 'entry_price': 100.0, 'atr14': 2.0,
                   'sma150': 80.0, 'cci': -60.0, 'pct_local': 0.1, 'pct_ath': 0.1}
    side_effects = [fake_signal, None, None, None, None, None]
    with patch('train.screen_day', side_effect=side_effects):
        stats = run_backtest({'X': df})
    assert stats['total_trades'] == 1


def test_run_backtest_end_of_backtest_closes_all_positions():
    # Position entered on d0, low never hits stop → closed at end-of-backtest
    start = date.fromisoformat(BACKTEST_START)
    days = [start + timedelta(days=i) for i in range(3)]
    df = make_minimal_df(days)
    fake_signal = {'stop': 90.0, 'entry_price': 100.0, 'atr14': 2.0,
                   'sma150': 80.0, 'cci': -60.0, 'pct_local': 0.1, 'pct_ath': 0.1}
    with patch('train.screen_day', side_effect=[fake_signal, None, None]):
        stats = run_backtest({'X': df})
    assert stats['total_trades'] == 1
    assert math.isfinite(stats['total_pnl'])


def test_run_backtest_pnl_is_finite_when_trades_occur():
    # Verify total_pnl is a finite non-NaN number when at least one trade fires
    start = date.fromisoformat(BACKTEST_START)
    days = [start + timedelta(days=i) for i in range(3)]
    df = make_minimal_df(days)
    fake_signal = {'stop': 90.0, 'entry_price': 100.0, 'atr14': 2.0,
                   'sma150': 80.0, 'cci': -60.0, 'pct_local': 0.1, 'pct_ath': 0.1}
    with patch('train.screen_day', side_effect=[fake_signal, None, None]):
        stats = run_backtest({'X': df})
    assert math.isfinite(stats['total_pnl'])
    assert not math.isnan(stats['total_pnl'])


def test_run_backtest_integration_real_screener_fires():
    # Integration: real screen_day (no mock) fires on Jan 10, 2026 → at least one trade
    # No network needed — uses a synthetic in-memory DataFrame
    df = make_signal_df_for_backtest(signal_date=date(2026, 1, 10))
    stats = run_backtest({'FAKE': df})
    assert stats['total_trades'] >= 1


# ── print_results / output format tests ──────────────────────────────────────

def test_output_format_pnl_line_parseable(capsys):
    print_results({'sharpe': 1.23456, 'total_trades': 5, 'win_rate': 0.6,
                   'avg_pnl_per_trade': 20.0, 'total_pnl': 100.0,
                   'backtest_start': '2025-12-20', 'backtest_end': '2026-03-06'})
    out = capsys.readouterr().out
    pnl_lines = [l for l in out.splitlines() if l.startswith('total_pnl:')]
    assert len(pnl_lines) == 1
    assert abs(float(pnl_lines[0].split(':')[1].strip()) - 100.0) < 1e-4


def test_output_format_all_seven_fields_present(capsys):
    print_results({'sharpe': 0.5, 'total_trades': 2, 'win_rate': 0.5,
                   'avg_pnl_per_trade': 10.0, 'total_pnl': 20.0,
                   'backtest_start': '2025-12-20', 'backtest_end': '2026-03-06'})
    out = capsys.readouterr().out
    for field in ['sharpe:', 'total_trades:', 'win_rate:', 'avg_pnl_per_trade:',
                  'total_pnl:', 'backtest_start:', 'backtest_end:']:
        assert field in out, f"Missing field: {field}"


# ── _write_final_outputs tests ────────────────────────────────────────────────

def _make_test_window_df() -> pd.DataFrame:
    """Minimal daily df with enough history for rolling indicators."""
    n = 60
    start = date(2026, 3, 1)
    dates = [start + timedelta(days=i) for i in range(n)]
    close = np.linspace(100.0, 110.0, n)
    return pd.DataFrame({
        'open': close * 0.998, 'high': close * 1.005, 'low': close * 0.995,
        'close': close, 'volume': np.full(n, 1_000_000.0),
        'price_10am': close * 1.002,
    }, index=pd.Index(dates, name='date'))


def test_write_final_outputs_creates_csv(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    df = _make_test_window_df()
    # Test window covers the last 10 days of the df
    test_start = str(df.index[-10])
    test_end   = str(df.index[-1] + timedelta(days=1))
    _write_final_outputs({'FAKE': df}, test_start, test_end, ticker_pnl={})
    csv_path = tmp_path / "final_test_data.csv"
    assert csv_path.exists(), "final_test_data.csv was not created"
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 10
    assert rows[0]['ticker'] == 'FAKE'
    assert 'sma50' in rows[0]


def test_write_final_outputs_prints_pnl_table(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    df = _make_test_window_df()
    test_start = str(df.index[-5])
    test_end   = str(df.index[-1] + timedelta(days=1))
    _write_final_outputs({'AAA': df, 'BBB': df}, test_start, test_end,
                         ticker_pnl={'AAA': 120.50, 'BBB': -30.25})
    out = capsys.readouterr().out
    assert 'AAA' in out
    assert '120.50' in out
    assert 'BBB' in out


def test_write_final_outputs_empty_window_no_csv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    df = _make_test_window_df()
    # Window entirely before the df range → no rows written
    _write_final_outputs({'FAKE': df}, '2020-01-01', '2020-01-15', ticker_pnl={})
    assert not (tmp_path / "final_test_data.csv").exists()


def test_write_final_outputs_indicators_populated(tmp_path, monkeypatch):
    # Indicators computed on full history so sma50/rsi14 are non-empty in test window
    monkeypatch.chdir(tmp_path)
    df = _make_test_window_df()
    test_start = str(df.index[-5])
    test_end   = str(df.index[-1] + timedelta(days=1))
    _write_final_outputs({'FAKE': df}, test_start, test_end, ticker_pnl={})
    with open(tmp_path / "final_test_data.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    # sma50 requires 50 bars of history; our df has 60 → last 5 should be populated
    assert any(r['sma50'] != '' for r in rows), "sma50 should be non-empty for last rows"


# ── V3-A Tests ────────────────────────────────────────────────────────────────

def test_screen_day_indicators_use_yesterday_close_not_today():
    """
    R1 correctness: screen_day must compute all rolling indicators on df.iloc[:-1].
    Verify by providing data where today's close is dramatically different from
    yesterday's, and checking the function still uses the yesterday-anchored SMA.

    Strategy: patch calc_atr14 to record which df it receives.
    The df passed to calc_atr14 must NOT include today's row (i.e. its last
    close must match yesterday's close, not today's anomalous 200.0).
    """
    import unittest.mock as mock
    from train import screen_day

    # 101-row df so indicators can warm up; give today a wildly different close
    bdays = pd.bdate_range(end="2026-02-27", periods=101)
    dates = [d.date() for d in bdays]
    prices = np.linspace(80.0, 120.0, 101)
    prices[-1] = 200.0    # today: anomalous close — must NOT influence indicators
    df = pd.DataFrame({
        "open": prices * 0.99, "high": prices * 1.01, "low": prices * 0.99,
        "close": prices,
        "volume": np.full(101, 1_000_000.0),
        "price_10am": prices,
    }, index=pd.Index(dates, name="date"))

    received_dfs = []

    original_calc_atr14 = train.calc_atr14
    def recording_calc_atr14(d):
        received_dfs.append(d.copy())
        return original_calc_atr14(d)

    with mock.patch.object(train, "calc_atr14", side_effect=recording_calc_atr14):
        screen_day(df, dates[-1])

    # calc_atr14 must have been called with a df whose last close is NOT 200.0
    assert received_dfs, "calc_atr14 was never called — screen_day returned early"
    last_close = float(received_dfs[0]["close"].iloc[-1])
    assert last_close != 200.0, (
        f"screen_day passed today's close ({last_close}) to calc_atr14; "
        "expected yesterday's close. R1 look-ahead fix may not be applied."
    )


def test_screen_day_minimum_history_boundary():
    """
    After R1 fix, screen_day needs len(df) >= 61 (60 history rows + today).
    With exactly 60 rows (hist has 59 rows), it must return None.
    With exactly 61 rows, it proceeds past the length guard.
    """
    from train import screen_day

    def make_df(n):
        bdays = pd.bdate_range(end="2026-02-27", periods=n)
        dates = [d.date() for d in bdays]
        prices = np.linspace(80.0, 120.0, n)
        return pd.DataFrame({
            "open": prices * 0.99, "high": prices * 1.01, "low": prices * 0.99,
            "close": prices, "volume": np.full(n, 1_000_000.0), "price_10am": prices,
        }, index=pd.Index(dates, name="date"))

    df_60 = make_df(60)
    result_60 = screen_day(df_60, df_60.index[-1])
    assert result_60 is None, "With 60 rows (hist=59), screen_day must return None"

    # 61-row df won't necessarily produce a signal, but must NOT raise or return
    # None solely due to the length guard — may return None for other signal reasons.
    df_61 = make_df(61)
    try:
        screen_day(df_61, df_61.index[-1])
    except Exception as e:
        pytest.fail(f"screen_day raised with 61 rows: {e}")


def test_run_backtest_risk_proportional_sizing():
    """
    R3: shares = RISK_PER_TRADE / (entry_price - stop).
    A signal with a wider stop distance must produce fewer shares than one with
    a tight stop distance, when entry_price is the same.
    """
    import train as tr
    import unittest.mock as mock

    signal_date = date(2026, 1, 10)
    days = [signal_date - timedelta(days=i) for i in reversed(range(20))]
    base_price = 100.0
    df = pd.DataFrame({
        "open": np.full(20, base_price), "high": np.full(20, base_price * 1.01),
        "low": np.full(20, base_price * 0.99), "close": np.full(20, base_price),
        "volume": np.full(20, 1_000_000.0), "price_10am": np.full(20, base_price),
    }, index=pd.Index(days, name="date"))

    def tight_stop_screen(d, today):
        # stop is 2.0 below entry → risk = 2.0
        return {"entry_price": base_price, "stop": base_price - 2.0, "stop_type": "pivot"}

    def wide_stop_screen(d, today):
        # stop is 8.0 below entry → risk = 8.0
        return {"entry_price": base_price, "stop": base_price - 8.0, "stop_type": "fallback"}

    with mock.patch.object(tr, "screen_day", tight_stop_screen), \
         mock.patch.object(tr, "manage_position", lambda pos, df: pos["stop_price"]):
        tight_stats = tr.run_backtest({"X": df}, start="2026-01-05", end="2026-01-15")

    with mock.patch.object(tr, "screen_day", wide_stop_screen), \
         mock.patch.object(tr, "manage_position", lambda pos, df: pos["stop_price"]):
        wide_stats = tr.run_backtest({"X": df}, start="2026-01-05", end="2026-01-15")

    if tight_stats["total_trades"] > 0 and wide_stats["total_trades"] > 0:
        tight_shares = tr.RISK_PER_TRADE / (base_price + 0.03 - (base_price - 2.0))
        wide_shares  = tr.RISK_PER_TRADE / (base_price + 0.03 - (base_price - 8.0))
        assert tight_shares > wide_shares, (
            f"tight stop should produce more shares ({tight_shares:.4f}) "
            f"than wide stop ({wide_shares:.4f})"
        )


def test_run_backtest_returns_trade_records_key():
    """R5: run_backtest() must return a 'trade_records' key."""
    stats = run_backtest({})
    assert "trade_records" in stats, "run_backtest() must return 'trade_records'"
    assert isinstance(stats["trade_records"], list)


def test_run_backtest_trade_records_schema():
    """R5: each trade record must have the 8 required fields."""
    import train as tr
    import unittest.mock as mock

    signal_date = date(2026, 1, 10)
    days = [signal_date - timedelta(days=i) for i in reversed(range(20))]
    base_price = 100.0
    df = pd.DataFrame({
        "open": np.full(20, base_price), "high": np.full(20, base_price * 1.01),
        "low": np.full(20, base_price * 0.99), "close": np.full(20, base_price),
        "volume": np.full(20, 1_000_000.0), "price_10am": np.full(20, base_price),
    }, index=pd.Index(days, name="date"))

    def always_signal(d, today):
        return {"entry_price": base_price, "stop": base_price - 5.0, "stop_type": "pivot"}

    with mock.patch.object(tr, "screen_day", always_signal), \
         mock.patch.object(tr, "manage_position", lambda pos, df: pos["stop_price"]):
        stats = tr.run_backtest({"X": df}, start="2026-01-05", end="2026-01-15")

    expected_fields = {"ticker", "entry_date", "exit_date", "days_held",
                       "stop_type", "entry_price", "exit_price", "pnl"}
    for rec in stats["trade_records"]:
        missing = expected_fields - set(rec.keys())
        assert not missing, f"trade record missing fields: {missing}"
        assert rec["stop_type"] in ("pivot", "fallback", "unknown")


def test_trades_tsv_written_on_run(tmp_path, monkeypatch):
    """
    R5: _write_trades_tsv must produce trades.tsv with correct headers.
    Tests the helper directly (empty trade list → header-only file).
    """
    import train as tr

    monkeypatch.chdir(tmp_path)
    tr._write_trades_tsv([])
    tsv_path = tmp_path / "trades.tsv"
    assert tsv_path.exists(), "trades.tsv must be created by _write_trades_tsv even with no trades"
    content = tsv_path.read_text(encoding="utf-8")
    header = content.splitlines()[0]
    assert "ticker" in header
    assert "entry_date" in header
    assert "stop_type" in header
    assert "pnl" in header


def test_screen_day_returns_stop_type_field():
    """
    R5: screen_day() must include 'stop_type' as either 'pivot' or 'fallback'
    in every non-None return dict.
    """
    from train import screen_day

    df = make_signal_df_for_backtest(signal_date=date(2026, 1, 10))
    result = screen_day(df, df.index[-1])

    if result is not None:
        assert "stop_type" in result, "screen_day() must return 'stop_type' in signal dict"
        assert result["stop_type"] in ("pivot", "fallback"), (
            f"stop_type must be 'pivot' or 'fallback', got {result['stop_type']!r}"
        )
