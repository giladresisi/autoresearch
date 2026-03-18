"""tests/test_backtester.py — Pytest suite for manage_position(), run_backtest(), and print_results()."""
import math
import pytest
import numpy as np
import pandas as pd
from datetime import date, timedelta
from unittest.mock import patch

from train import (
    manage_position, run_backtest, print_results, calc_atr14,
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
    All simulation logic lives here in the test file — train.py is unmodified."""
    n = 250
    dates = [signal_date - timedelta(days=(n - 1 - i)) for i in range(n)]

    close = np.linspace(60.0, 120.0, n, dtype=float)
    for i, idx in enumerate(range(230, 242)):
        close[idx] = 116.0 + (180.0 - 116.0) * (i / 11)
    close[242:246] = [170.0, 150.0, 130.0, 110.0]
    close[246], close[247], close[248], close[249] = 100.0, 101.0, 102.0, 103.0

    high  = close * 1.005
    low   = close * 0.995
    open_ = close * 0.998
    open_[249] = 99.0
    price_10am = close.copy()
    price_10am[249] = 110.0

    df = pd.DataFrame({
        'open': open_, 'high': high, 'low': low, 'close': close,
        'volume': np.full(n, 1_000_000.0),
        'price_10am': price_10am,
    }, index=pd.Index(dates, name='date'))

    # Pivot low + prior touch required by R2+R6
    df.iloc[210, df.columns.get_loc('low')] = 90.0
    df.iloc[195, df.columns.get_loc('low')] = 89.1
    df.iloc[195, df.columns.get_loc('high')] = 90.9
    return df


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

def test_run_backtest_empty_dict_returns_zero_sharpe():
    stats = run_backtest({})
    assert stats['sharpe'] == 0.0
    assert stats['total_trades'] == 0


def test_run_backtest_no_backtest_days_returns_zero_sharpe():
    # Dates before BACKTEST_START → no trading days in window
    days = [date(2020, 1, 2), date(2020, 1, 3)]
    df = make_minimal_df(days)
    assert run_backtest({'X': df})['sharpe'] == 0.0


def test_run_backtest_no_signals_returns_zero_sharpe():
    # df has only 3 rows → screen_day fails R1 (needs 150 rows) → no signals, no trades
    start = date.fromisoformat(BACKTEST_START)
    days = [start + timedelta(days=i) for i in range(3)]
    df = make_minimal_df(days)
    stats = run_backtest({'X': df})
    assert stats['sharpe'] == 0.0
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


def test_run_backtest_sharpe_is_finite_when_trades_occur():
    # Same as above — verify Sharpe is a finite non-NaN number
    start = date.fromisoformat(BACKTEST_START)
    days = [start + timedelta(days=i) for i in range(3)]
    df = make_minimal_df(days)
    fake_signal = {'stop': 90.0, 'entry_price': 100.0, 'atr14': 2.0,
                   'sma150': 80.0, 'cci': -60.0, 'pct_local': 0.1, 'pct_ath': 0.1}
    with patch('train.screen_day', side_effect=[fake_signal, None, None]):
        stats = run_backtest({'X': df})
    assert math.isfinite(stats['sharpe'])
    assert not math.isnan(stats['sharpe'])


def test_run_backtest_integration_real_screener_fires():
    # Integration: real screen_day (no mock) fires on Jan 10, 2026 → at least one trade
    # No network needed — uses a synthetic in-memory DataFrame
    df = make_signal_df_for_backtest(signal_date=date(2026, 1, 10))
    stats = run_backtest({'FAKE': df})
    assert stats['total_trades'] >= 1


# ── print_results / output format tests ──────────────────────────────────────

def test_output_format_sharpe_line_parseable(capsys):
    print_results({'sharpe': 1.23456, 'total_trades': 5, 'win_rate': 0.6,
                   'avg_pnl_per_trade': 20.0, 'total_pnl': 100.0})
    out = capsys.readouterr().out
    sharpe_lines = [l for l in out.splitlines() if l.startswith('sharpe:')]
    assert len(sharpe_lines) == 1
    assert abs(float(sharpe_lines[0].split(':')[1].strip()) - 1.23456) < 1e-4


def test_output_format_all_seven_fields_present(capsys):
    print_results({'sharpe': 0.5, 'total_trades': 2, 'win_rate': 0.5,
                   'avg_pnl_per_trade': 10.0, 'total_pnl': 20.0})
    out = capsys.readouterr().out
    for field in ['sharpe:', 'total_trades:', 'win_rate:', 'avg_pnl_per_trade:',
                  'total_pnl:', 'backtest_start:', 'backtest_end:']:
        assert field in out, f"Missing field: {field}"
