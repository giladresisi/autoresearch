"""tests/test_screener.py — Pytest suite for screen_day() and helpers."""
import pytest
import numpy as np
import pandas as pd
from datetime import date, timedelta

from train import (
    screen_day, calc_cci, calc_atr14, find_pivot_lows, zone_touch_count,
    find_stop_price, is_stalling_at_ceiling, nearest_resistance_atr,
)


def make_passing_df(n: int = 250) -> pd.DataFrame:
    """Synthetic DataFrame that satisfies Rules 1-4 and R4 (wick).
    Does NOT satisfy Rule 5 (pullback >= 8%) or stop logic."""
    base = date(2024, 1, 2)
    dates = [base + timedelta(days=i) for i in range(n)]
    close = np.linspace(100.0, 200.0, n)
    # Last 3 closes strictly rising, last value pulled back 12% from peak
    close[-10:] = close[-10] * np.array([1.0, 0.98, 0.96, 0.94, 0.92, 0.90, 0.91, 0.92, 0.93, 0.94])
    close[-3] = close[-4] * 1.01
    close[-2] = close[-3] * 1.01
    close[-1] = close[-2] * 1.01
    return pd.DataFrame({
        'open':       close * 0.995,
        'high':       close * 1.005,
        'low':        close * 0.990,
        'close':      close,
        'volume':     np.full(n, 1_000_000.0),
        'price_10am': close * 0.998,
    }, index=pd.Index(dates, name='date'))


def make_pivot_df(n: int = 250) -> pd.DataFrame:
    """Like make_passing_df but with a clear pivot low ~35 bars from end for stop detection."""
    df = make_passing_df(n).copy()
    pivot_idx = n - 35
    pivot_price = float(df['close'].iloc[-1]) * 0.85
    df.iloc[pivot_idx, df.columns.get_loc('low')] = pivot_price
    touch_idx = pivot_idx - 10
    if touch_idx >= 0:
        df.iloc[touch_idx, df.columns.get_loc('low')] = pivot_price * 0.99
        df.iloc[touch_idx, df.columns.get_loc('high')] = pivot_price * 1.01
    return df


def make_signal_df(n: int = 250) -> pd.DataFrame:
    """Synthetic DataFrame where screen_day returns a non-None dict.

    Design satisfies the current momentum-breakout screener:
    - Bars 0-234: steady rise 60→97 (SMA50 ≈ 93, SMA20 ≈ 98 at end → SMA20 > SMA50)
    - Bars 235-249: 8 up-bars (+1) and 6 down-bars (−0.7), alternating
      → RSI14 ≈ 66 (within the required 50–75 band)
    - price_10am[-1] = 115: breaks above 20-day high close (~100) and yesterday high (~100.3)
    - volume[-1] = 2×VM30 → vol_ratio = 2.0 (passes ≥ 1.9 threshold)
    - No overhead pivot high above 115 → resistance check passes
    - Stop: ATR-fallback = price_10am − 2×ATR (always satisfies the 1.5×ATR buffer)
    """
    base = date(2024, 1, 2)
    dates = [base + timedelta(days=i) for i in range(n)]

    close = np.zeros(n, dtype=float)
    # Steady rise builds SMA50 to ~95 well below the breakout price_10am of 115
    close[:235] = np.linspace(60.0, 97.0, 235)
    # Alternating oscillation: 8 ups of +1, 6 downs of −0.7 → RSI14 ≈ 66
    close[235:250] = [97.0, 98.0, 97.3, 98.3, 97.6, 98.6, 97.9, 98.9,
                      98.2, 99.2, 98.5, 99.5, 98.8, 99.8, 100.8]

    high  = close * 1.005
    low   = close * 0.995
    open_ = close * 0.998

    price_10am = close.copy()
    # Big jump on the last bar to trigger the 20-day breakout rule
    price_10am[249] = 115.0

    volume = np.full(n, 1_000_000.0)
    volume[249] = 2_000_000.0   # last bar = 2× MA30 → vol_ratio = 2.0, passes ≥ 1.9

    return pd.DataFrame({
        'open':       open_,
        'high':       high,
        'low':        low,
        'close':      close,
        'volume':     volume,
        'price_10am': price_10am,
    }, index=pd.Index(dates, name='date'))


def make_pivot_signal_df(n: int = 250) -> pd.DataFrame:
    """Like make_signal_df but with a clear pivot-low structure so find_stop_price() succeeds.

    Adds a pivot dip ~35 bars from end (bar n-35) and a prior touch ~10 bars before it
    (bar n-45), satisfying find_stop_price's R2 (prior touch) and buffer requirements.
    After R9, screen_day() returns a non-None signal with stop_type == 'pivot'.
    """
    df = make_signal_df(n).copy()
    pivot_idx = n - 35
    # Dip well below price_10am=115 (at least 1.5×ATR≈1 away)
    pivot_price = float(df['close'].iloc[pivot_idx]) * 0.85
    df.iloc[pivot_idx, df.columns.get_loc('low')] = pivot_price
    # Prior touch satisfies find_stop_price's R2 requirement (at least 1 historical touch)
    touch_idx = pivot_idx - 10
    if touch_idx >= 0:
        df.iloc[touch_idx, df.columns.get_loc('low')] = pivot_price * 0.99
        df.iloc[touch_idx, df.columns.get_loc('high')] = pivot_price * 1.01
    return df


# ── Indicator unit tests ──────────────────────────────────────────────────────

def test_calc_cci_returns_series():
    df = make_passing_df(50)
    result = calc_cci(df)
    assert isinstance(result, pd.Series)
    assert len(result) == len(df)


def test_calc_atr14_nan_first_13_bars():
    df = make_passing_df(50)
    atr = calc_atr14(df)
    # rolling(14).mean() needs 14 rows: indices 0-12 are NaN, index 13 is first non-NaN
    assert pd.isna(atr.iloc[12])
    assert not pd.isna(atr.iloc[13])


def test_find_pivot_lows_detects_explicit_pivot():
    df = make_passing_df(60)
    # Insert a clear local low at row 20 — well below neighbours
    df.iloc[20, df.columns.get_loc('low')] = 50.0
    for i in range(16, 25):
        if i != 20:
            df.iloc[i, df.columns.get_loc('low')] = 100.0
    pivots = find_pivot_lows(df)
    indices = [idx for idx, _ in pivots]
    assert 20 in indices


def test_zone_touch_count_zero_for_far_level():
    df = make_passing_df(100)
    assert zone_touch_count(df, 1_000_000.0) == 0


def test_zone_touch_count_nonneg():
    df = make_passing_df(100)
    result = zone_touch_count(df, float(df['close'].mean()))
    assert result >= 0


def test_stalling_false_for_trending():
    # Explicitly spread highs — unambiguously not stalling
    df = make_passing_df(10)
    df.iloc[-3, df.columns.get_loc('high')] = 100.0
    df.iloc[-2, df.columns.get_loc('high')] = 120.0
    df.iloc[-1, df.columns.get_loc('high')] = 140.0
    assert not is_stalling_at_ceiling(df)


def test_stalling_true_when_constructed():
    df = make_passing_df(10)
    # Set last 3 highs tightly clustered and all closes below them
    for i in [-3, -2, -1]:
        df.iloc[i, df.columns.get_loc('high')] = 150.0
        df.iloc[i, df.columns.get_loc('close')] = 140.0
    assert is_stalling_at_ceiling(df)


def test_resistance_none_if_no_overhead():
    df = make_passing_df(50)
    # Set entry_price above all highs so no overhead pivot exists
    entry_price = float(df['high'].max()) * 2.0
    atr = 1.0
    result = nearest_resistance_atr(df, entry_price, atr)
    assert result is None


# ── screen_day rule tests ─────────────────────────────────────────────────────

def test_r1_fail_149_rows():
    df = make_passing_df(250)
    today = df.index[148]
    result = screen_day(df.iloc[:149], today)
    assert result is None


def test_r1_pass_150_rows():
    df = make_passing_df(250)
    today = df.index[149]
    # Should not raise — might return None or dict
    screen_day(df.iloc[:150], today)


def test_rule1_fail_below_sma():
    df = make_passing_df(250)
    # Set price_10am to 1.0 — far below any SMA150
    df.iloc[-1, df.columns.get_loc('price_10am')] = 1.0
    today = df.index[-1]
    assert screen_day(df, today) is None


def test_rule2_fail_not_3_up_days():
    df = make_passing_df(250)
    # Break the consecutive up-close streak: close[-2] < close[-3]
    df.iloc[-2, df.columns.get_loc('close')] = float(df['close'].iloc[-3]) * 0.99
    today = df.index[-1]
    assert screen_day(df, today) is None


def test_rule3_fail_low_volume():
    df = make_passing_df(250)
    # Set last day's volume to nearly zero — far below 0.85× MA30
    df.iloc[-1, df.columns.get_loc('volume')] = 1.0
    today = df.index[-1]
    assert screen_day(df, today) is None


def test_r4_fail_large_upper_wick():
    df = make_passing_df(250)
    # Make upper wick much larger than body by raising the high
    df.iloc[-1, df.columns.get_loc('high')] = float(df['close'].iloc[-1]) * 1.10
    today = df.index[-1]
    assert screen_day(df, today) is None


def test_screen_day_no_exception_on_pivot_df():
    df = make_pivot_df(250)
    today = df.index[-1]
    # Should complete without raising any exception
    screen_day(df, today)


def test_return_dict_has_stop_key():
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    result = screen_day(df, today)
    assert result is not None, "make_pivot_signal_df must produce a passing signal"
    assert 'stop' in result
    assert isinstance(result['stop'], float)


def test_stop_always_below_entry():
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    result = screen_day(df, today)
    assert result is not None, "make_pivot_signal_df must produce a passing signal"
    assert result['stop'] < result['entry_price']


def test_rule2b_fail_price_not_above_prev_high():
    # Rule 2b: price_10am must exceed yesterday's high.
    # Raise yesterday's high above price_10am (115) → Rule 2b fails → return None.
    df = make_signal_df(250)
    df.iloc[-2, df.columns.get_loc('high')] = 120.0  # prev_high=120 > price_10am=115
    today = df.index[-1]
    assert screen_day(df, today) is None


# ── Edge case tests ───────────────────────────────────────────────────────────

def test_missing_price_10am_raises():
    df = make_passing_df(250).drop(columns=['price_10am'])
    today = df.index[-1]
    with pytest.raises(KeyError):
        screen_day(df, today)


def test_returns_none_or_dict_only():
    df = make_pivot_df(250)
    today = df.index[-1]
    result = screen_day(df, today)
    assert result is None or isinstance(result, dict)
