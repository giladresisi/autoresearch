"""
strategies/global_mar24.py — Extracted from autoresearch/global-mar24 @ 6ad6edd
"""

METADATA = {
    "name":         'global-mar24',
    "sector":       'unknown',
    "tickers":      ['NVDA', 'AMD', 'TSLA', 'PLTR', 'MSTR', 'APP', 'SMCI', 'COIN', 'CRWD', 'META', 'GOOGL', 'AMZN', 'NFLX', 'AAPL', 'MSFT', 'AVGO', 'ORCL', 'CRM', 'ADBE', 'NOW', 'INTU', 'IBM', 'QCOM', 'MU', 'AMAT', 'LRCX', 'KLAC', 'ADI', 'MRVL', 'MCHP', 'ON', 'MPWR', 'TXN', 'INTC', 'ZS', 'PANW', 'FTNT', 'OKTA', 'NET', 'DDOG', 'SNOW', 'MDB', 'TEAM', 'HUBS', 'DELL', 'HPQ', 'PSTG', 'AKAM', 'WDC', 'STX', 'VRT', 'KEYS', 'EPAM', 'RBLX', 'TTD', 'TWLO', 'U', 'ACLS', 'ONTO', 'MKSI', 'IPGP', 'COHU', 'WOLF', 'SLAB', 'LLY', 'ABBV', 'JNJ', 'MRK', 'PFE', 'AMGN', 'GILD', 'REGN', 'VRTX', 'BIIB', 'MRNA', 'ILMN', 'INCY', 'ALNY', 'BMRN', 'IONS', 'ARWR', 'RXRX', 'TMO', 'ISRG', 'ABT', 'DHR', 'SYK', 'BSX', 'MDT', 'EW', 'DXCM', 'PODD', 'IDXX', 'ALGN', 'HOLX', 'ZBH', 'BDX', 'GEHC', 'RMD', 'UNH', 'CI', 'HUM', 'ELV', 'MOH', 'CVS', 'MCK', 'IQV', 'HCA', 'THC', 'EXAS', 'ACAD', 'PTGX', 'TGTX', 'NKTR', 'PRGO', 'VTRS', 'JAZZ', 'NBIX', 'OMCL', 'JPM', 'GS', 'BAC', 'WFC', 'MS', 'C', 'USB', 'PNC', 'TFC', 'ALLY', 'COF', 'DFS', 'SYF', 'KEY', 'RF', 'FITB', 'MTB', 'CFG', 'BLK', 'SCHW', 'SPGI', 'MCO', 'ICE', 'CME', 'CBOE', 'MKTX', 'IBKR', 'LPLA', 'V', 'MA', 'AXP', 'PYPL', 'FI', 'FIS', 'GPN', 'HOOD', 'NU', 'AFRM', 'SOFI', 'SQ', 'WEX', 'EVTC', 'HD', 'LOW', 'TJX', 'ROST', 'BBY', 'ULTA', 'WSM', 'RH', 'ORLY', 'AZO', 'NKE', 'LULU', 'DECK', 'SKX', 'CROX', 'ONON', 'ELF', 'F', 'GM', 'RIVN', 'UBER', 'ABNB', 'BKNG', 'MGM', 'LVS', 'WYNN', 'DKNG', 'MCD', 'CMG', 'SBUX', 'YUM', 'DRI', 'QSR', 'TSCO', 'CPRI', 'RL', 'PVH', 'LYFT', 'WMT', 'PG', 'KO', 'PEP', 'COST', 'TGT', 'PM', 'CL', 'MO', 'EL', 'CHD', 'KMB', 'HRL', 'CPB', 'GIS', 'K', 'HSY', 'MDLZ', 'STZ', 'KDP', 'CELH', 'SYY', 'BJ', 'GO', 'USFD', 'PFGC', 'COTY', 'LMT', 'RTX', 'NOC', 'BA', 'LHX', 'GD', 'HII', 'TDG', 'TXT', 'HEI', 'CAT', 'DE', 'CMI', 'PCAR', 'ITW', 'EMR', 'ROK', 'PH', 'ETN', 'CARR', 'OTIS', 'UPS', 'FDX', 'DAL', 'UAL', 'AAL', 'LUV', 'JBHT', 'SAIA', 'ODFL', 'CSX', 'UNP', 'GE', 'HON', 'WM', 'RSG', 'VRSK', 'CTAS', 'ROP', 'FAST', 'IEX', 'XYL', 'DIS', 'CMCSA', 'VZ', 'T', 'TMUS', 'CHTR', 'WBD', 'PARA', 'SNAP', 'PINS', 'RDDT', 'SPOT', 'TTWO', 'EA', 'MTCH', 'IAC', 'FOX', 'SIRI', 'ZM', 'DOCU', 'XOM', 'CVX', 'COP', 'SLB', 'EOG', 'MPC', 'VLO', 'OXY', 'DVN', 'FANG', 'HES', 'APA', 'AR', 'EQT', 'RRC', 'HAL', 'BKR', 'MRO', 'WMB', 'KMI', 'OKE', 'LNG', 'TRGP', 'PSX', 'NOV', 'RIG', 'CTRA', 'LIN', 'APD', 'SHW', 'ECL', 'PPG', 'NEM', 'GOLD', 'AEM', 'WPM', 'FCX', 'SCCO', 'AA', 'ALB', 'SQM', 'MP', 'BALL', 'IP', 'PKG', 'NUE', 'CF', 'MOS', 'DD', 'EMN', 'PLD', 'AMT', 'CCI', 'EQIX', 'PSA', 'EXR', 'AVB', 'EQR', 'O', 'VICI', 'IRM', 'DLR', 'SBAC', 'WELL', 'INVH', 'CPT', 'MAA', 'ARE', 'KIM', 'STAG', 'GLPI', 'NLY', 'NEE', 'SO', 'DUK', 'D', 'AEP', 'EXC', 'SRE', 'PCG', 'ED', 'ES', 'ETR', 'FE', 'PPL', 'CMS', 'WEC', 'AWK', 'ATO', 'LNT', 'EVRG', 'SMH', 'SOXX', 'XBI', 'GDX', 'GDXJ', 'XOP', 'IBB', 'ARKK', 'KWEB', 'BOTZ'],
    "train_start":  '2024-09-01',
    "train_end":    '2026-03-06',
    "test_start":   '2026-03-06',
    "test_end":     '2026-03-20',
    "source_branch": 'autoresearch/global-mar24',
    "source_commit": '6ad6edd',
    "train_pnl":    335.73,
    "train_sharpe": 0.041287,
    "train_trades": 118,
    "description":  (
        "This strategy enters long positions when price_1030am breaks above both the 20-day highest close and the prior day's high, with volume at least 2.5× the 30-day average, RSI(14) between 50 and 75, and a bullish SMA stack (price > SMA50 > SMA100, SMA20 > SMA50, SMA20 slope not down more than 0.5% over 5 days). A structural pivot-low stop is required — placed 0.3 ATR(14) below the nearest prior-touch pivot low — and the entry is rejected if the entry-to-stop gap is under 1.5 ATR or if the nearest overhead resistance pivot is less than 2.0 ATR away. Stops trail to breakeven at +1.5 ATR profit, then trail 1.2 ATR below the 20-bar high once +2.0 ATR in profit, with a forced exit if the trade stalls below +0.5 ATR within the first 5 calendar days or holds beyond 30 business days with unrealized P&L under $15; this strategy suits trending, higher-momentum market regimes where mid-cap and large-cap stocks are making new 20-day highs on volume surges above established SMA support."
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
WALK_FORWARD_WINDOWS = 7

# Test window width in business days per fold.
# 20 ≈ 1 calendar month → ~40–100 trades on 85 tickers; enough to distinguish skill from noise.
# Set at session setup. Do NOT change during the loop.
FOLD_TEST_DAYS = 40

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
    Momentum breakout strategy: enter when price_1030am breaks above 20-day high
    with above-average volume, price above SMA50, and sufficient room above resistance.
    df: full daily history up to and including today
    Returns None if no signal, or dict with at minimum {'stop': float}
    """
    # Ensure no look-ahead: slice to today
    df = df.loc[:today]

    # Need at least 1 today row + 101 rows of history (SMA100 requires 100 history bars)
    if len(df) < 102:
        return None

    # Today's observable data: only price_1030am and partial-day volume
    price_1030am = float(df['price_1030am'].iloc[-1])
    today_vol  = float(df['volume'].iloc[-1])
    if pd.isna(price_1030am) or pd.isna(today_vol):
        return None

    # R8: skip entries within 14 calendar days of next earnings announcement
    if 'next_earnings_date' in df.columns:
        ned = df['next_earnings_date'].iloc[-1]
        if pd.notna(ned):
            days_to_earnings = (ned - today).days
            if 0 <= days_to_earnings <= 14:
                return None

    # History up to yesterday (no look-ahead). Use a view — no copy needed.
    hist = df.iloc[:-1]
    close_hist = hist['close']

    # Fast SMA checks using direct tail-slicing (equivalent to rolling().mean().iloc[-1]
    # but only computes the last window instead of the full series — much faster).
    sma20  = float(close_hist.iloc[-20:].mean())
    sma50  = float(close_hist.iloc[-50:].mean())
    sma100 = float(close_hist.iloc[-100:].mean())
    if pd.isna(sma20) or pd.isna(sma50) or pd.isna(sma100):
        return None

    # Rule 1: SMA alignment — most tickers fail here; check before volume/ATR/RSI
    if price_1030am <= sma50 or price_1030am <= sma100 or sma20 <= sma50 or sma50 <= sma100:
        return None

    # Rule 1b: SMA20 slope must not be materially declining (allow up to 0.5% dip vs 5d ago)
    # Filters hard corrections while allowing temporarily-flat uptrends to enter
    sma20_5d_ago = float(close_hist.iloc[-25:-5].mean())
    if sma20 < sma20_5d_ago * 0.995:
        return None

    # Volume check — second most selective filter
    vm30 = float(hist['volume'].iloc[-30:].mean())
    if pd.isna(vm30) or vm30 == 0:
        return None
    vol_ratio = today_vol / vm30
    if vol_ratio < 2.5:
        return None

    # Rule 2a: price_1030am breaks above the 20-day highest close (breakout)
    high20 = float(close_hist.iloc[-20:].max())
    if price_1030am <= high20:
        return None

    # Rule 2b: price_1030am also above yesterday's high (breakout continuation)
    prev_high = float(hist['high'].iloc[-1])
    if price_1030am <= prev_high:
        return None

    # Compute ATR14 and RSI14 only for tickers that pass the fast filters above.
    # Tail-slicing: rolling(14) needs ≥15 rows for ATR, ≥15 for RSI; use 30/60 for stability.
    atr = float(calc_atr14(hist.iloc[-30:]).iloc[-1])
    rsi = float(calc_rsi14(hist.iloc[-60:]).iloc[-1])
    if pd.isna(atr) or atr == 0 or pd.isna(rsi):
        return None

    # Rule 3b: RSI between 50 and 75 (momentum building, not overbought)
    if not (50 <= rsi <= 75):
        return None

    # Rule 4: not stalling at ceiling
    if is_stalling_at_ceiling(hist):
        return None

    # Stop: pivot-low required; no fallback (R9 — reject structurally unsupported entries)
    stop = find_stop_price(hist, price_1030am, atr)
    if stop is None:
        return None  # R9: reject entries with no structural pivot support
    stop_type = 'pivot'

    # 1.5 ATR buffer safety net
    if price_1030am - stop < 1.5 * atr:
        return None

    # Resistance check: nearest overhead pivot >= 2 ATR away
    res_atr = nearest_resistance_atr(hist, price_1030am, atr)
    if res_atr is not None and res_atr < 2.0:
        return None

    return {
        'stop':        stop,
        'entry_price': price_1030am,
        'stop_type':   stop_type,   # always 'pivot' after R9 (fallback path removed)
        'atr14':       round(atr, 4),
        'sma50':       round(sma50, 4),
        'vol_ratio':   round(vol_ratio, 4),
        'high20':      round(high20, 4),
    }


def manage_position(position: dict, df: pd.DataFrame) -> float:
    """
    Breakeven once price_1030am >= entry + 1.5 ATR.
    Trail by 1.2 ATR below recent high once 2.0 ATR in profit.
    Never lower the stop.
    """
    current_stop = position['stop_price']
    entry_price  = position['entry_price']
    atr_series   = calc_atr14(df)
    atr          = float(atr_series.iloc[-1])
    if pd.isna(atr) or atr == 0:
        return current_stop
    price_1030am = float(df['price_1030am'].iloc[-1])

    # R10: time-based capital-efficiency exit
    # If held >30 business days and unrealised P&L < 30% of RISK_PER_TRADE, force exit
    _today_date = df.index[-1]
    _bdays_held = int(np.busday_count(position['entry_date'], _today_date))
    _unrealised_pnl = (price_1030am - entry_price) * position['shares']
    if _bdays_held > 30 and _unrealised_pnl < 0.3 * RISK_PER_TRADE:
        return max(current_stop, price_1030am)  # force exit; never lower existing stop

    # R15: Early stall exit — force exit if price stalls in first 5 calendar days.
    # Targets the 6–14 day loss cluster where price never builds momentum.
    _cal_days_held = (_today_date - position['entry_date']).days
    if _cal_days_held <= 5 and price_1030am < entry_price + 0.5 * atr:
        return max(current_stop, price_1030am)  # force exit; never lower existing stop

    # Breakeven trigger
    be_stop = entry_price if price_1030am >= entry_price + 1.5 * atr else current_stop

    # Trailing stop: trail 1.2 ATR below recent high once 2.0 ATR in profit (earlier activation)
    recent_high = float(df['price_1030am'].dropna().iloc[-20:].max())
    trail_stop = round(recent_high - 1.2 * atr, 2) if recent_high >= entry_price + 2.0 * atr else current_stop

    return max(current_stop, be_stop, trail_stop)


