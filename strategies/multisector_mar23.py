"""
strategies/multisector_mar23.py — Extracted from autoresearch/multisector-mar23 @ da23635
"""

METADATA = {
    "name":         'multisector-mar23',
    "sector":       'unknown',
    "tickers":      ['AAPL', 'MSFT', 'NVDA', 'AMD', 'META', 'GOOGL', 'AMZN', 'TSLA', 'AVGO', 'ORCL', 'CRM', 'ADBE', 'QCOM', 'MU', 'AMAT', 'NOW', 'PLTR', 'MSTR', 'APP', 'SMCI', 'NFLX', 'COIN', 'CRWD', 'ZS', 'PANW', 'JPM', 'GS', 'BAC', 'WFC', 'MS', 'BLK', 'SCHW', 'AXP', 'COF', 'SPGI', 'V', 'MA', 'UNH', 'LLY', 'ABBV', 'JNJ', 'MRK', 'PFE', 'TMO', 'ISRG', 'AMGN', 'GILD', 'REGN', 'VRTX', 'XOM', 'CVX', 'COP', 'SLB', 'EOG', 'MPC', 'VLO', 'OXY', 'WMT', 'PG', 'KO', 'PEP', 'COST', 'TGT', 'PM', 'CL', 'CAT', 'DE', 'UPS', 'FDX', 'GE', 'HON', 'RTX', 'LMT', 'HD', 'MCD', 'NKE', 'SBUX', 'LOW', 'F', 'GM', 'LIN', 'APD', 'NEM', 'FCX', 'NUE'],
    "train_start":  '2024-09-01',
    "train_end":    '2026-03-06',
    "test_start":   '2026-03-06',
    "test_end":     '2026-03-20',
    "source_branch": 'autoresearch/multisector-mar23',
    "source_commit": 'da23635',
    "train_pnl":    97.84,
    "train_sharpe": 0.117971,
    "train_trades": 43,
    "description":  (
        "This is a momentum breakout strategy that enters long when price_10am clears both the 20-day closing high and the prior day's high simultaneously, confirmed by volume at least 1.9× its 30-day average, RSI(14) between 50 and 75, price above SMA50, and SMA20 above SMA50 — with nearest overhead pivot resistance at least 2 ATR away and no ceiling-stalling pattern in the last three bars. Stops are placed at the highest qualifying pivot low minus 0.3×ATR(14) (requiring ≥1.5 ATR buffer to entry and at most 10 zone touches in 90 bars), falling back to entry minus 2.0×ATR if no valid pivot exists; the stop steps up to breakeven once price reaches entry +1.5 ATR, then trails 1.5×ATR below the 20-bar rolling high once the position is 2.0 ATR in profit. The strategy suits trending, risk-on markets with clear sector rotation and expanding volume — it will underperform in choppy, low-momentum, or mean-reverting regimes where breakouts frequently fail."
    ),
}

"""
train.py — Stock strategy screener, position manager, and backtester.
Rewrite screener criteria, position manager logic, and entry/exit rules to optimize Sharpe ratio.
Do NOT modify: CACHE_DIR (env-var driven; set AUTORESEARCH_CACHE_DIR instead), load_ticker_data(), Sharpe computation, or the output block format.
"""
import os, sys
from datetime import date
import numpy as np
import pandas as pd

# Cache directory for parquet files. Override with AUTORESEARCH_CACHE_DIR env var
# to maintain independent datasets for different sessions or date ranges.
CACHE_DIR = os.environ.get(
    "AUTORESEARCH_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data"),
)

# ══ SESSION SETUP — set once at session start; DO NOT change during experiments ══════
# These constants define the evaluation framework. Changing them mid-session
# invalidates comparisons across experiments.

# Backtest window — matches prepare.py; edit here to change the simulation period
BACKTEST_START = "2024-09-01"
BACKTEST_END   = "2026-03-20"

# Train/test split — last 14 calendar days of the backtest window are held out as test set.
# Written by the agent at setup time. Do NOT modify during the experiment loop.
TRAIN_END   = "2026-03-06"   # BACKTEST_END − 14 calendar days
TEST_START  = "2026-03-06"   # same date as TRAIN_END (test window starts here)

# Final output flag — set to True only for the special post-loop final test run.
# Agent sets True, runs train.py, then immediately restores to False.
WRITE_FINAL_OUTPUTS = False

# Walk-forward evaluation: number of rolling test folds (V3-B R2)
WALK_FORWARD_WINDOWS = 9

# Test window width in business days per fold.
# 20 ≈ 1 calendar month → ~40–100 trades on 85 tickers; enough to distinguish skill from noise.
# Set at session setup. Do NOT change during the loop.
FOLD_TEST_DAYS = 20

# Training window width in business days.
# 0 = expanding: each fold trains from BACKTEST_START to its test window's start (all prior history).
# N > 0 = rolling: each fold trains on only the N most recent business days before its test window,
#   exposing successive folds to genuinely different market slices.
# Recommended: 0 (expanding) for simplicity; 120 (≈6 months) for maximum regime diversity.
# Set at session setup. Do NOT change during the loop.
FOLD_TRAIN_DAYS = 0

# Silent holdout boundary (V3-B R4-full): TRAIN_END − 14 calendar days.
# Walk-forward folds' test windows end at approximately this date.
# Set by the agent at session setup. Do NOT change during the loop.
SILENT_END = "2026-02-20"   # example; agent computes this at setup

# Risk-proportional sizing: dollar risk per trade (V3-A R3). DO NOT raise to inflate P&L.
RISK_PER_TRADE = 50.0

# R6: Ticker holdout — fraction of tickers withheld from all training folds
# Set to 0.0 to disable; 0.2 = hold out the last-sorted 20% of the universe.
# Holdout evaluation uses BACKTEST_START..TRAIN_END (same window as training folds).
TICKER_HOLDOUT_FRAC = 0.1

# Tickers included in walk-forward TEST folds only — never in training.
# Used to measure out-of-universe generalization: min_test_pnl must hold on
# tickers the agent has never directly optimized for.
# These tickers must be downloaded by prepare.py before running.
# Set at session setup. Do NOT change during the loop.
# When using TEST_EXTRA_TICKERS, set TICKER_HOLDOUT_FRAC = 0 to avoid
# overlap between the two mechanisms.
TEST_EXTRA_TICKERS: list = []

# ══ STRATEGY TUNING — agent may modify these freely during experiments ════════════════
# Only the constants below this line are valid optimization targets.

# R8: Position concentration controls
MAX_SIMULTANEOUS_POSITIONS = 5     # cap on open positions at any time (set to large int to disable)
CORRELATION_PENALTY_WEIGHT = 0.0   # penalty factor for correlated portfolios (0 = off)

# R9: Robustness perturbation (price/stop jitter)
ROBUSTNESS_SEEDS = 0               # 0 = off; 5 = recommended (runs 4 perturbed seeds + nominal)


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


# ── Indicators ────────────────────────────────────────────────────────────────

def calc_cci(df, p=20):
    # Commodity Channel Index over period p
    # raw=True passes a numpy array to the lambda — ~10× faster than a pandas Series for rolling apply
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


# ── R2 + R6: Pivot-low-anchored stop ─────────────────────────────────────────

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
    # with a 1.5× ATR buffer between entry_price and the derived stop level
    if len(df) < 60:
        return None
    window = df.iloc[-90:].copy().reset_index(drop=True)
    pivots = find_pivot_lows(window, bars=4)
    if not pivots:
        return None
    # Sort descending by price — consider nearest-to-entry pivot first
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


# ── R3: Bounce stalling at ceiling ───────────────────────────────────────────

def is_stalling_at_ceiling(df, band_pct=0.03):
    # Detects when the last 3 highs cluster tightly (< band_pct range) and all closes sit below them
    last3_highs  = [float(df['high'].iloc[i])  for i in [-3, -2, -1]]
    last3_closes = [float(df['close'].iloc[i]) for i in [-3, -2, -1]]
    h_max, h_min = max(last3_highs), min(last3_highs)
    if h_min == 0:
        return False
    return (h_max - h_min) / h_min <= band_pct and all(c < h_min for c in last3_closes)


# ── R5: Nearest pivot-high resistance >= 2x ATR ───────────────────────────────

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


# ── Screener, position manager, backtester stubs ─────────────────────────────

def screen_day(df: pd.DataFrame, today) -> "dict | None":
    """
    Momentum breakout strategy: enter when price_10am breaks above 20-day high
    with above-average volume, price above SMA50, and sufficient room above resistance.
    df: full daily history up to and including today
    Returns None if no signal, or dict with at minimum {'stop': float}
    """
    # Ensure no look-ahead: slice to today
    df = df.loc[:today]

    # Need at least 1 today row + 60 rows of history for indicators
    if len(df) < 61:
        return None

    # Compute all indicators on history up to yesterday (no look-ahead)
    hist = df.iloc[:-1].copy()
    hist['_sma20']  = hist['close'].rolling(20).mean()
    hist['_sma50']  = hist['close'].rolling(50).mean()
    hist['_vm30']   = hist['volume'].rolling(30).mean()
    hist['_atr14']  = calc_atr14(hist)
    hist['_rsi14']  = calc_rsi14(hist)

    sma20 = float(hist['_sma20'].iloc[-1])
    sma50 = float(hist['_sma50'].iloc[-1])
    vm30  = float(hist['_vm30'].iloc[-1])
    atr   = float(hist['_atr14'].iloc[-1])
    rsi   = float(hist['_rsi14'].iloc[-1])
    # Today's observable data: only price_10am and partial-day volume
    price_10am = float(df['price_10am'].iloc[-1])
    today_vol  = float(df['volume'].iloc[-1])

    # Guard NaN/zero
    if pd.isna(price_10am) or pd.isna(sma20) or pd.isna(sma50) or pd.isna(vm30) or pd.isna(atr) or pd.isna(rsi) or pd.isna(today_vol) or vm30 == 0 or atr == 0:
        return None

    # Rule 1: price above SMA50 and SMA20 > SMA50 (near-term trend stronger than medium-term)
    if price_10am <= sma50 or sma20 <= sma50:
        return None

    # Rule 2a: price_10am breaks above the 20-day highest close (breakout)
    high20 = float(hist['close'].iloc[-20:].max())   # last 20 days of history
    if price_10am <= high20:
        return None

    # Rule 2b: price_10am also above yesterday's high (breakout continuation)
    prev_high = float(hist['high'].iloc[-1])          # yesterday's high
    if price_10am <= prev_high:
        return None

    # Rule 3: today's volume >= 1.9× MA30 (high conviction required)
    vol_ratio = today_vol / vm30
    if vol_ratio < 1.9:
        return None

    # Rule 3b: RSI between 50 and 75 (momentum building, not overbought)
    if not (50 <= rsi <= 75):
        return None

    # Rule 4: not stalling at ceiling
    if is_stalling_at_ceiling(hist):
        return None

    # Stop: prefer pivot-low stop, fall back to 2.0 ATR
    stop = find_stop_price(hist, price_10am, atr)
    if stop is None:
        stop = round(price_10am - 2.0 * atr, 2)
        stop_type = 'fallback'
    else:
        stop_type = 'pivot'

    # 1.5 ATR buffer safety net
    if price_10am - stop < 1.5 * atr:
        return None

    # Resistance check: nearest overhead pivot >= 2 ATR away
    res_atr = nearest_resistance_atr(hist, price_10am, atr)
    if res_atr is not None and res_atr < 2.0:
        return None

    return {
        'stop':        stop,
        'entry_price': price_10am,
        'stop_type':   stop_type,   # R5: 'pivot' or 'fallback'
        'atr14':       round(atr, 4),
        'sma50':       round(sma50, 4),
        'vol_ratio':   round(vol_ratio, 4),
        'high20':      round(high20, 4),
    }


def manage_position(position: dict, df: pd.DataFrame) -> float:
    """
    Breakeven once price_10am >= entry + 1.5 ATR.
    Trail by 1.5 ATR below recent high once 2.5 ATR in profit.
    Never lower the stop.
    """
    current_stop = position['stop_price']
    entry_price  = position['entry_price']
    atr_series   = calc_atr14(df)
    atr          = float(atr_series.iloc[-1])
    if pd.isna(atr) or atr == 0:
        return current_stop
    price_10am = float(df['price_10am'].iloc[-1])

    # Breakeven trigger
    be_stop = entry_price if price_10am >= entry_price + 1.5 * atr else current_stop

    # Trailing stop: trail 1.5 ATR below recent high once 2.0 ATR in profit (earlier activation)
    recent_high = float(df['price_10am'].dropna().iloc[-20:].max())
    trail_stop = round(recent_high - 1.5 * atr, 2) if recent_high >= entry_price + 2.0 * atr else current_stop

    return max(current_stop, be_stop, trail_stop)


