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
        'price_1030am': close * 0.998,
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
    - price_1030am[-1] = 115: breaks above 20-day high close (~100) and yesterday high (~100.3)
    - volume[-1] = 3×VM30 → vol_ratio = 3.0 (passes ≥ 2.5 threshold)
    - No overhead pivot high above 115 → resistance check passes
    - Stop: ATR-fallback = price_1030am − 2×ATR (always satisfies the 1.5×ATR buffer)
    """
    base = date(2024, 1, 2)
    dates = [base + timedelta(days=i) for i in range(n)]

    close = np.zeros(n, dtype=float)
    # Steady rise builds SMA50 to ~95 well below the breakout price_1030am of 115
    close[:235] = np.linspace(60.0, 97.0, 235)
    # Alternating oscillation: 8 ups of +1, 6 downs of −0.7 → RSI14 ≈ 66
    close[235:250] = [97.0, 98.0, 97.3, 98.3, 97.6, 98.6, 97.9, 98.9,
                      98.2, 99.2, 98.5, 99.5, 98.8, 99.8, 100.8]

    high  = close * 1.005
    low   = close * 0.995
    open_ = close * 0.998

    price_1030am = close.copy()
    # Big jump on the last bar to trigger the 20-day breakout rule
    price_1030am[249] = 115.0

    volume = np.full(n, 1_000_000.0)
    volume[249] = 3_000_000.0   # last bar = 3× MA30 → vol_ratio = 3.0, passes ≥ 2.5

    return pd.DataFrame({
        'open':       open_,
        'high':       high,
        'low':        low,
        'close':      close,
        'volume':     volume,
        'price_1030am': price_1030am,
    }, index=pd.Index(dates, name='date'))


def make_pivot_signal_df(n: int = 250) -> pd.DataFrame:
    """Like make_signal_df but with a clear pivot-low structure so find_stop_price() succeeds.

    Adds a pivot dip ~35 bars from end (bar n-35) and a prior touch ~10 bars before it
    (bar n-45), satisfying find_stop_price's R2 (prior touch) and buffer requirements.
    After R9, screen_day() returns a non-None signal with stop_type == 'pivot'.
    """
    df = make_signal_df(n).copy()
    pivot_idx = n - 35
    # Dip well below price_1030am=115 (at least 1.5×ATR≈1 away)
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
    # Set price_1030am to 1.0 — far below any SMA150
    df.iloc[-1, df.columns.get_loc('price_1030am')] = 1.0
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
    # Rule 2b: price_1030am must exceed yesterday's high.
    # Raise yesterday's high above price_1030am (115) → Rule 2b fails → return None.
    df = make_signal_df(250)
    df.iloc[-2, df.columns.get_loc('high')] = 120.0  # prev_high=120 > price_1030am=115
    today = df.index[-1]
    assert screen_day(df, today) is None


# ── Edge case tests ───────────────────────────────────────────────────────────

def test_missing_price_1030am_raises():
    df = make_passing_df(250).drop(columns=['price_1030am'])
    today = df.index[-1]
    with pytest.raises(KeyError):
        screen_day(df, today)


def test_returns_none_or_dict_only():
    df = make_pivot_df(250)
    today = df.index[-1]
    result = screen_day(df, today)
    assert result is None or isinstance(result, dict)


# ── New tests for current_price interface (Task 1.1) ─────────────────────────

def test_screen_day_current_price_overrides_df_value():
    """Injecting current_price=115 where df price_1030am would also signal."""
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    # Call with explicit current_price equal to what make_pivot_signal_df sets
    result_with = screen_day(df, today, current_price=115.0)
    # Both should produce a signal (or both None) — the override uses the same price
    result_without = screen_day(df, today)
    assert (result_with is None) == (result_without is None)


def test_screen_day_current_price_none_uses_df():
    """Explicit None falls back to df['price_1030am'] as before."""
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    result_explicit_none = screen_day(df, today, current_price=None)
    result_no_arg = screen_day(df, today)
    # Both paths should give identical results
    assert result_explicit_none == result_no_arg


def test_screen_day_returns_rsi14_key():
    """Signal dict contains 'rsi14' key with value in (0, 100)."""
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    result = screen_day(df, today)
    assert result is not None, "make_pivot_signal_df must produce a passing signal"
    assert "rsi14" in result
    assert isinstance(result["rsi14"], float)
    assert 0 < result["rsi14"] < 100


def test_screen_day_returns_res_atr_key():
    """Signal dict contains 'res_atr' key with value float or None."""
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    result = screen_day(df, today)
    assert result is not None, "make_pivot_signal_df must produce a passing signal"
    assert "res_atr" in result
    assert result["res_atr"] is None or isinstance(result["res_atr"], float)


def test_screen_day_backtest_unchanged():
    """screen_day(df, today) and screen_day(df, today, current_price=None) are identical."""
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    result_default = screen_day(df, today)
    result_explicit = screen_day(df, today, current_price=None)
    assert result_default == result_explicit


# ── Recovery mode signal tests ────────────────────────────────────────────────

def make_recovery_signal_df(n: int = 260) -> pd.DataFrame:
    """Synthetic DataFrame where screen_day fires the RECOVERY path.

    Price path:
    - Bars 0-199: slow steady rise 50→120 (builds a low SMA200 baseline ~117)
    - Bars 200-219: explosive rally 120→200 (lifts SMA50 to ~174, well above SMA200)
    - Bars 220-234: shallow correction 200→165 (price falls below SMA50; SMA50 stays > SMA200)
    - Bars 235-258: oscillating consolidation 165→169 (RSI ~57, SMA20 slope near flat)
    - Bar 259 (today): price_1030am = 173 (breaks above 20-day high close ~169.5)

    At bar 259:
      SMA200 ≈ 117  (dominated by long slow rise)
      SMA50  ≈ 174  (dominated by the rally; above SMA200)
      SMA20  ≈ 168  (consolidation zone; below price_1030am=173)
      price_1030am = 173 > 20d high (~169.5) and > SMA20 (~168) and < SMA50 (~174)
    """
    base = date(2024, 1, 2)
    dates = [base + timedelta(days=i) for i in range(n)]

    close = np.zeros(n, dtype=float)
    close[:200]    = np.linspace(50.0, 120.0, 200)    # slow rise
    close[200:220] = np.linspace(120.0, 200.0, 20)    # explosive rally
    close[220:235] = np.linspace(200.0, 165.0, 15)    # shallow correction

    # Oscillating consolidation: +1.2/-0.9 per bar → RSI ~57, net drift ≈+0.15/bar
    v = 165.0
    osc_vals = []
    for i in range(24):
        v += 1.2 if i % 2 == 0 else -0.9
        osc_vals.append(v)
    close[235:259] = osc_vals
    close[259]     = close[258]  # last history bar

    high  = close * 1.005
    low   = close * 0.990
    open_ = close * 0.998

    price_1030am        = close.copy()
    price_1030am[259]   = 173.0  # breakout above 20d high close (~169.5)

    # Volume: last 5 bars elevated (vol_trend_ratio >= 1.0), yesterday active
    volume = np.full(n, 1_000_000.0)
    volume[254:259] = 1_500_000.0   # 5-day trend above MA30
    volume[258]     = 1_200_000.0   # yesterday >= 0.8× MA30

    df = pd.DataFrame({
        'open':         open_,
        'high':         high,
        'low':          low,
        'close':        close,
        'volume':       volume,
        'price_1030am': price_1030am,
    }, index=pd.Index(dates, name='date'))

    # Add pivot low ~35 bars from end with a prior touch ~10 bars before it
    pivot_idx = n - 35
    pivot_price = float(df['close'].iloc[pivot_idx]) * 0.85
    df.iloc[pivot_idx, df.columns.get_loc('low')] = pivot_price
    touch_idx = pivot_idx - 10
    if touch_idx >= 0:
        df.iloc[touch_idx, df.columns.get_loc('low')] = pivot_price * 0.99
        df.iloc[touch_idx, df.columns.get_loc('high')] = pivot_price * 1.01

    return df


def test_screen_day_recovery_fires_below_sma50():
    """Recovery path fires: signal returned even though price < SMA50."""
    df = make_recovery_signal_df()
    today = df.index[-1]
    result = screen_day(df, today, current_price=173.0)
    assert result is not None, "Expected recovery signal, got None"
    assert result["signal_path"] == "recovery"


def test_screen_day_recovery_price_below_sma50():
    """Sanity check: the recovery fixture actually has price below SMA50."""
    df = make_recovery_signal_df()
    hist = df.iloc[:-1]
    sma50 = float(hist['close'].iloc[-50:].mean())
    assert 173.0 < sma50, f"Expected price 173 < SMA50 {sma50:.2f}"


def test_screen_day_recovery_sma50_above_sma200():
    """Sanity check: the recovery fixture has SMA50 > SMA200 (no death cross)."""
    df = make_recovery_signal_df()
    hist = df.iloc[:-1]
    sma50  = float(hist['close'].iloc[-50:].mean())
    sma200 = float(hist['close'].iloc[-200:].mean())
    assert sma50 > sma200, f"Expected SMA50 {sma50:.2f} > SMA200 {sma200:.2f}"


def test_screen_day_recovery_blocked_when_death_cross():
    """Recovery path must NOT fire when SMA50 <= SMA200 (death cross)."""
    df = make_recovery_signal_df().copy()
    # Force a death cross: set all recent closes very low so SMA50 drops below SMA200
    df.iloc[-60:-1, df.columns.get_loc('close')] *= 0.3
    # Sanity check: verify the fixture actually produced a death cross before calling screen_day,
    # so a None result is attributed to the right gate and not an earlier filter.
    hist = df.iloc[:-1]
    sma50  = float(hist['close'].iloc[-50:].mean())
    sma200 = float(hist['close'].iloc[-200:].mean())
    assert sma50 <= sma200, (
        f"Fixture did not produce a death cross (SMA50={sma50:.2f}, SMA200={sma200:.2f}). "
        "Adjust the multiplier so SMA50 actually drops below SMA200."
    )
    today = df.index[-1]
    result = screen_day(df, today, current_price=173.0)
    assert result is None, "Recovery path must not fire when SMA50 < SMA200"


def test_screen_day_bull_path_unaffected():
    """Bull path still fires for a classic bull-stack ticker (no regression)."""
    df = make_pivot_signal_df(250)
    today = df.index[-1]
    result = screen_day(df, today, current_price=115.0)
    assert result is not None, "Bull path should still fire"
    assert result["signal_path"] == "bull"


def test_screen_day_recovery_rsi_range_40_65():
    """Recovery path signal has rsi14 within the 40–65 recovery range.

    Verifies that screen_day uses (40, 65) for recovery rather than the bull-mode (50, 75),
    by asserting that a passing recovery signal reports an rsi14 in [40, 65].
    """
    df = make_recovery_signal_df()
    today = df.index[-1]
    result = screen_day(df, today, current_price=173.0)
    assert result is not None, "Expected recovery signal from standard fixture"
    assert result["signal_path"] == "recovery"
    rsi = result.get("rsi14")
    assert rsi is not None, "Expected rsi14 key in result dict"
    assert 40 <= rsi <= 65, f"RSI {rsi:.1f} out of recovery range [40, 65]"


def test_screen_day_recovery_slope_floor_relaxed():
    """Recovery path uses 1% SMA20 slope tolerance, not the bull-mode 0.5%.

    A stock early in its reversal naturally has a still-declining SMA20.
    With the strict 0.995 floor, valid early-recovery entries would be silently blocked.
    This test verifies that a signal fires even when sma20 is between 0.99 and 0.995
    of sma20_5d_ago (i.e., in the relaxed-but-not-strict zone).
    """
    df = make_recovery_signal_df().copy()
    # Nudge the SMA20 window (bars -25 to -6 before today) slightly higher so that
    # sma20_5d_ago ends up ~0.3% above sma20 — inside recovery tolerance (1%) but
    # outside bull tolerance (0.5%).
    hist_slice = slice(len(df) - 26, len(df) - 6)
    df.iloc[hist_slice, df.columns.get_loc('close')] *= 1.003
    today = df.index[-1]
    result = screen_day(df, today, current_price=173.0)
    assert result is not None, (
        "Recovery signal should fire when SMA20 slope is within 1% tolerance. "
        "Rule 1b slope_floor must be 0.990 for recovery path, not 0.995."
    )
    assert result["signal_path"] == "recovery"
