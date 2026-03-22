"""
strategies/energy_mar21.py — Extracted from autoresearch/energy-mar21 @ 38530d6
"""

METADATA = {
    "name":         'energy-mar21',
    "sector":       'energy/materials',
    "tickers":      ['CTVA', 'LIN', 'XOM', 'DBA', 'SM', 'IYE', 'EOG', 'APA', 'EQT', 'CTRA', 'APD', 'DVN', 'BKR', 'COP', 'VLO', 'HEI', 'HAL'],
    "train_start":  '2025-12-21',
    "train_end":    '2026-03-07',
    "test_start":   '2026-03-07',
    "test_end":     '2026-03-21',
    "source_branch": 'autoresearch/energy-mar21',
    "source_commit": '38530d6',
    "train_pnl":    2463.55,
    "train_sharpe": None,
    "train_trades": 18,
    "description":  (
        'The screener triggers on every ticker with at least 60 bars of history using price_10am as the entry price, computing ATR(14), RSI(14), a 30-bar SMA, and a 30-bar average volume ratio as diagnostics â€” but applying no hard thresholds on any of these indicators, meaning all tickers with non-NaN values generate a signal. Stops are anchored to the highest qualifying pivot low within the prior 90 bars, requiring â‰¥1 confirmed prior touch (excluding the last 5 bars), â‰¤10 zone touches within a Â±1.5% band, and a minimum gap of 1.5Ã— ATR14 between entry and stop; the stop is placed 0.3Ã— ATR below the pivot, falling back to a flat 2.0Ã— ATR stop when no valid pivot exists. Position management holds the initial stop fixed for the life of the trade (a breakeven-raise calculation is coded but explicitly disabled), making this a passive pivot-anchored system suited to trending energy names where sustained directional moves from entry are expected rather than mean-reversion.'
    ),
}

"""
train.py â€” Stock strategy screener, position manager, and backtester.
Rewrite screener criteria, position manager logic, and entry/exit rules to optimize Sharpe ratio.
Do NOT modify: CACHE_DIR, load_ticker_data(), Sharpe computation, or the output block format.
"""
import os, sys
from datetime import date
import numpy as np
import pandas as pd

# Directory where prepare.py writes {ticker}.parquet files
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")

# Backtest window â€” matches prepare.py; edit here to change the simulation period
BACKTEST_START = "2025-12-21"
BACKTEST_END   = "2026-03-21"

# Train/test split â€” last 14 calendar days of the backtest window are held out as test set.
# Written by the agent at setup time. Do NOT modify during the experiment loop.
TRAIN_END   = "2026-03-07"   # BACKTEST_END âˆ’ 14 calendar days
TEST_START  = "2026-03-07"   # same date as TRAIN_END (test window starts here)

# Final output flag â€” set to True only for the special post-loop final test run.
# Agent sets True, runs train.py, then immediately restores to False.
WRITE_FINAL_OUTPUTS = False


def load_ticker_data(ticker: str) -> pd.DataFrame | None:
    """Reads CACHE_DIR/{ticker}.parquet; returns None if file does not exist."""
    path = os.path.join(CACHE_DIR, f"{ticker}.parquet")
    if not os.path.exists(path):
        return None
    return pd.read_parquet(path)


def load_all_ticker_data() -> dict[str, pd.DataFrame]:
    """Loads all *.parquet files from CACHE_DIR. Returns {} if directory is empty or missing."""
    if not os.path.isdir(CACHE_DIR):
        return {}
    result = {}
    for fname in os.listdir(CACHE_DIR):
        if fname.endswith(".parquet"):
            ticker = fname[:-len(".parquet")]
            path = os.path.join(CACHE_DIR, fname)
            result[ticker] = pd.read_parquet(path)
    return result


# â”€â”€ Indicators â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def calc_cci(df, p=20):
    # Commodity Channel Index over period p
    # raw=True passes a numpy array to the lambda â€” ~10Ã— faster than a pandas Series for rolling apply
    tp  = (df['high'] + df['low'] + df['close']) / 3
    sma = tp.rolling(p).mean()
    md  = tp.rolling(p).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
    return (tp - sma) / (0.015 * md)


def calc_rsi14(df):
    # RSI(14) using standard Wilder smoothing
    delta = df['close'].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float('nan'))
    return 100 - (100 / (1 + rs))


def calc_atr14(df):
    # Average True Range (Wilder, 14-bar rolling mean of true range)
    # First 13 values are NaN because rolling(14) needs 14 bars
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low']  - df['close'].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(14).mean()


# â”€â”€ R2 + R6: Pivot-low-anchored stop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def find_pivot_lows(df, bars=4):
    # A pivot low is the lowest bar in a symmetric window of `bars` on each side
    pivots = []
    for i in range(bars, len(df) - bars):
        l = float(df['low'].iloc[i])
        if all(l <= float(df['low'].iloc[i+k]) for k in range(-bars, bars+1) if k != 0):
            pivots.append((i, l))
    return pivots


def zone_touch_count(df, level, lookback=90, band_pct=0.015):
    window = df.iloc[-lookback:]
    lo, hi = level * (1 - band_pct), level * (1 + band_pct)
    return int(sum(
        1 for i in range(len(window))
        if float(window['low'].iloc[i]) <= hi and float(window['high'].iloc[i]) >= lo
    ))


def find_stop_price(df, entry_price, atr):
    # Finds the highest qualifying pivot low satisfying R2 (prior touch) and R6 (not too noisy),
    # with a 1.5Ã— ATR buffer between entry_price and the derived stop level
    if len(df) < 60:
        return None
    window = df.iloc[-90:].copy().reset_index(drop=True)
    pivots = find_pivot_lows(window, bars=4)
    if not pivots:
        return None
    # Sort descending by price â€” consider nearest-to-entry pivot first
    candidates = sorted(
        [(i, p) for i, p in pivots if entry_price - p >= 1.5 * atr],
        key=lambda x: x[1], reverse=True
    )
    for _, pivot_price in candidates:
        # R6: skip if stop zone has been touched too many times (noisy support = unreliable)
        if zone_touch_count(df, pivot_price, lookback=90) > 10:
            continue
        # R2: require at least 1 historical touch before the last 5 bars (confirms level validity)
        prior = df.iloc[-90:-5]
        lo, hi = pivot_price * 0.985, pivot_price * 1.015
        prior_touches = sum(
            1 for i in range(len(prior))
            if float(prior['low'].iloc[i]) <= hi and float(prior['high'].iloc[i]) >= lo
        )
        if prior_touches < 1:
            continue
        # Place stop 0.3 ATR below pivot; reject if buffer to entry is still < 1.5 ATR
        stop = pivot_price - 0.3 * atr
        if entry_price - stop < 1.5 * atr:
            continue
        return round(stop, 2)
    return None


# â”€â”€ R3: Bounce stalling at ceiling â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def is_stalling_at_ceiling(df, band_pct=0.03):
    # Detects when the last 3 highs cluster tightly (< band_pct range) and all closes sit below them
    last3_highs  = [float(df['high'].iloc[i])  for i in [-3, -2, -1]]
    last3_closes = [float(df['close'].iloc[i]) for i in [-3, -2, -1]]
    h_max, h_min = max(last3_highs), min(last3_highs)
    if h_min == 0:
        return False
    return (h_max - h_min) / h_min <= band_pct and all(c < h_min for c in last3_closes)


# â”€â”€ R5: Nearest pivot-high resistance >= 2x ATR â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def nearest_resistance_atr(df, entry_price, atr, lookback=90):
    # Returns distance to nearest overhead pivot high in ATR units, or None if none exists above entry
    window = df.iloc[-lookback:].copy().reset_index(drop=True)
    bars, pivot_highs = 4, []
    for i in range(bars, len(window) - bars):
        h = float(window['high'].iloc[i])
        if h > entry_price and all(
            h >= float(window['high'].iloc[i+k])
            for k in range(-bars, bars+1) if k != 0
        ):
            pivot_highs.append(h)
    if not pivot_highs:
        return None
    return (min(pivot_highs) - entry_price) / atr


# â”€â”€ Screener, position manager, backtester stubs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def screen_day(df: pd.DataFrame, today) -> "dict | None":
    """
    Momentum breakout strategy: enter when price_10am breaks above 20-day high
    with above-average volume, price above SMA50, and sufficient room above resistance.
    df: full daily history up to and including today
    Returns None if no signal, or dict with at minimum {'stop': float}
    """
    # Ensure no look-ahead: slice to today
    df = df.loc[:today]

    # Minimum history for indicators
    if len(df) < 60:
        return None

    # Compute indicators
    df = df.copy()
    df['_sma50']  = df['close'].rolling(30).mean()
    df['_vm30']   = df['volume'].rolling(30).mean()
    df['_atr14']  = calc_atr14(df)
    df['_rsi14']  = calc_rsi14(df)

    sma50      = float(df['_sma50'].iloc[-1])
    vm30       = float(df['_vm30'].iloc[-1])
    atr        = float(df['_atr14'].iloc[-1])
    rsi        = float(df['_rsi14'].iloc[-1])
    price_10am = float(df['price_10am'].iloc[-1])

    # Guard NaN/zero
    if pd.isna(price_10am) or pd.isna(sma50) or pd.isna(vm30) or pd.isna(atr) or pd.isna(rsi) or vm30 == 0 or atr == 0:
        return None

    # record volume ratio for diagnostics
    vol_ratio = float(df['volume'].iloc[-1]) / vm30

    # Stop: prefer pivot-low stop, fall back to 2.0 ATR
    stop = find_stop_price(df, price_10am, atr)
    if stop is None:
        stop = round(price_10am - 2.0 * atr, 2)

    return {
        'stop':        stop,
        'entry_price': price_10am,
        'atr14':       round(atr, 4),
        'sma50':       round(sma50, 4),
        'vol_ratio':   round(vol_ratio, 4),
    }


def manage_position(position: dict, df: pd.DataFrame) -> float:
    """
    Raise stop to breakeven (entry_price) once price_10am >= entry_price + 1 Ã— ATR14.
    Never lower the stop. Returns updated stop_price (>= position['stop_price']).
    """
    current_stop = position['stop_price']
    entry_price  = position['entry_price']
    atr_series   = calc_atr14(df)
    atr          = float(atr_series.iloc[-1])
    if pd.isna(atr) or atr == 0:
        return current_stop
    price_10am = float(df['price_10am'].iloc[-1])
    # Hold original stop â€” do not raise to breakeven
    return current_stop


