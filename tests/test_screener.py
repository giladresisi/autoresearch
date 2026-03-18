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
    """Synthetic DataFrame where screen_day returns a non-None dict (all 11 rules pass).

    Design:
    - Bars 0-229: steady rise 60->120 (SMA150 at end ~105)
    - Bars 230-241: run-up to 180 (creates ATH ~181, puts high tp in 20-bar CCI window)
    - Bars 242-245: fast pullback (170, 150, 130, 110)
    - Bars 246-249: last 4 rising closes (100, 101, 102, 103)
    - price_10am[-1]=110: above SMA150~105 (Rule 1), ~35% below ATH (Rule 5)
    - CCI[-1] well below -50 and rising (Rule 4): 20-bar mean ~137, tp[-1]~103
    - Body >> wick at last bar (R4-wick): open[-1]=99, high[-1]=103.5
    - Explicit pivot low at bar 210 with prior touch (R2+R6 stop)
    """
    base = date(2024, 1, 2)
    dates = [base + timedelta(days=i) for i in range(n)]

    close = np.linspace(60.0, 120.0, n, dtype=float)

    # Big run-up bars 230-241: 116 -> 180 (ATH, and anchors 20-bar CCI mean high)
    for i, idx in enumerate(range(230, 242)):
        close[idx] = 116.0 + (180.0 - 116.0) * (i / 11)

    # Fast pullback
    close[242:246] = [170.0, 150.0, 130.0, 110.0]

    # Last 4 closes strictly rising (Rule 2)
    close[246] = 100.0  # close[-4]
    close[247] = 101.0  # close[-3]
    close[248] = 102.0  # close[-2]
    close[249] = 103.0  # close[-1]

    high  = close * 1.005
    low   = close * 0.995

    # Last bar: large body (open=99, close=103, body=4) and small wick (high=103.5, wick=0.5)
    # so upper_wick (0.5) < body (4) — R4-wick passes
    open_ = close * 0.998
    open_[249] = 99.0

    # price_10am[-1]=110: above SMA150~105, and ~35% below ATH~181 and local_high~171
    price_10am = close.copy()
    price_10am[249] = 110.0

    volume = np.full(n, 1_000_000.0)

    df = pd.DataFrame({
        'open':       open_,
        'high':       high,
        'low':        low,
        'close':      close,
        'volume':     volume,
        'price_10am': price_10am,
    }, index=pd.Index(dates, name='date'))

    # Pivot low at bar 210 (well below price_10am=110, gap ~20 >> 1.5*ATR)
    pivot_price = 90.0
    df.iloc[210, df.columns.get_loc('low')] = pivot_price

    # Prior touch of pivot zone (required by R2 in find_stop_price)
    df.iloc[195, df.columns.get_loc('low')]  = pivot_price * 0.99
    df.iloc[195, df.columns.get_loc('high')] = pivot_price * 1.01

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
    df = make_signal_df(250)
    today = df.index[-1]
    result = screen_day(df, today)
    assert result is not None, "make_signal_df must produce a passing signal"
    assert 'stop' in result
    assert isinstance(result['stop'], float)


def test_stop_always_below_entry():
    df = make_signal_df(250)
    today = df.index[-1]
    result = screen_day(df, today)
    assert result is not None, "make_signal_df must produce a passing signal"
    assert result['stop'] < result['entry_price']


def test_rule4_fail_cci_not_rising():
    # CCI is rising for 2 days means c0 > c1 > c2.
    # Set c1 <= c2 to break the rising condition — screen_day must return None.
    df = make_signal_df(250)
    # Raise tp at position -2 (c1) relative to -3 (c2) so c1 > c2 fails (c1 <= c2)
    # We do this by lowering close[-3] below close[-2], making tp[-2] > tp[-3]
    # so that when CCI is computed, c1 (index -2) is no longer < c2 (index -3)
    # Simpler: directly break the rising streak by making position -2 close > -1 close
    # so that CCI at -2 (c1) >= CCI at -1 (c0), violating c0 > c1
    close_m1 = float(df['close'].iloc[-1])
    close_m2 = float(df['close'].iloc[-2])
    # Raise close[-2] above close[-1]: tp[-2] > tp[-1] → c1 > c0, fails c0 > c1 > c2
    df.iloc[-2, df.columns.get_loc('close')] = close_m1 * 1.05
    df.iloc[-2, df.columns.get_loc('high')]  = close_m1 * 1.055
    df.iloc[-2, df.columns.get_loc('low')]   = close_m1 * 1.045
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
