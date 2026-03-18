"""
train.py — Stock strategy screener, position manager, and backtester.
Rewrite screener criteria, position manager logic, and entry/exit rules to optimize Sharpe ratio.
Do NOT modify: CACHE_DIR, load_ticker_data(), Sharpe computation, or the output block format.
"""
import os, sys
from datetime import date
import numpy as np
import pandas as pd

# Directory where prepare.py writes {ticker}.parquet files
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")

# Backtest window — matches prepare.py; edit here to change the simulation period
BACKTEST_START = "2026-01-01"
BACKTEST_END   = "2026-03-01"


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
    df: full daily history up to and including today
    Returns None if no signal, or dict with at minimum {'stop': float}
    """
    # Ensure no look-ahead: slice to today
    df = df.loc[:today]

    # R1: minimum 150 rows for SMA150 to be defined
    if len(df) < 150:
        return None

    # Compute all indicators up front on a working copy
    df = df.copy()
    df['_sma150'] = df['close'].rolling(150).mean()
    df['_vm30']   = df['volume'].rolling(30).mean()
    df['_cci']    = calc_cci(df)
    df['_atr14']  = calc_atr14(df)

    # Extract scalar values from last row
    sma150     = float(df['_sma150'].iloc[-1])
    vm30       = float(df['_vm30'].iloc[-1])
    atr        = float(df['_atr14'].iloc[-1])
    c0         = float(df['_cci'].iloc[-1])
    c1         = float(df['_cci'].iloc[-2])
    c2         = float(df['_cci'].iloc[-3])
    price_10am = float(df['price_10am'].iloc[-1])

    # Guard NaN/zero before any rule evaluation
    if pd.isna(sma150) or pd.isna(vm30) or pd.isna(atr) or pd.isna(c1) or pd.isna(c2) or vm30 == 0 or atr == 0:
        return None

    # Rule 1: price_10am must be above SMA150
    if price_10am <= sma150:
        return None

    # Rule 2: 3 consecutive up-close days — compare indices [-4], [-3], [-2], [-1]
    close = df['close']
    if not (float(close.iloc[-1]) > float(close.iloc[-2]) > float(close.iloc[-3]) > float(close.iloc[-4])):
        return None

    # Rule 3: both last 2 days volume >= 0.85× MA30
    vol1 = float(df['volume'].iloc[-1]) / vm30
    vol2 = float(df['volume'].iloc[-2]) / vm30
    if not (vol1 >= 0.85 and vol2 >= 0.85):
        return None

    # Rule 4: CCI(20) < -50, rising 2 consecutive days
    if pd.isna(c0) or not (c0 < -50 and c0 > c1 > c2):
        return None

    # Rule 5: pullback >= 8% from 7-day local high AND all-time high
    # Uses price_10am (not close) so comparisons match actual entry conditions
    local_high = float(df['high'].iloc[-8:-1].max())
    ath        = float(df['high'].max())
    pct_local  = (local_high - price_10am) / local_high
    pct_ath    = (ath - price_10am) / ath
    if not (pct_local >= 0.08 and pct_ath >= 0.08):
        return None

    # R4: upper wick of entry candle must be strictly less than body
    last_c     = float(df['close'].iloc[-1])
    last_o     = float(df['open'].iloc[-1])
    last_h     = float(df['high'].iloc[-1])
    body       = abs(last_c - last_o)
    upper_wick = last_h - max(last_c, last_o)
    if body == 0 or upper_wick >= body:
        return None

    # R3: reject if bounce is stalling at a ceiling
    if is_stalling_at_ceiling(df):
        return None

    # R2+R6: a valid pivot-low-anchored stop must exist
    stop = find_stop_price(df, price_10am, atr)
    if stop is None:
        return None

    # 1.5× ATR buffer safety net (find_stop_price enforces it too, but belt-and-suspenders)
    if price_10am - stop < 1.5 * atr:
        return None

    # R5: nearest overhead resistance must be >= 2× ATR away
    # None means no overhead pivot exists — treat as passing (bullish, no resistance)
    res_atr = nearest_resistance_atr(df, price_10am, atr)
    if res_atr is not None and res_atr < 2.0:
        return None

    return {
        'stop':        stop,
        'entry_price': price_10am,
        'atr14':       round(atr, 4),
        'sma150':      round(sma150, 4),
        'cci':         round(c0, 2),
        'pct_local':   round(pct_local, 4),
        'pct_ath':     round(pct_ath, 4),
    }


def manage_position(position: dict, df: pd.DataFrame) -> float:
    """
    Raise stop to breakeven (entry_price) once price_10am >= entry_price + 1 × ATR14.
    Never lower the stop. Returns updated stop_price (>= position['stop_price']).
    """
    current_stop = position['stop_price']
    entry_price  = position['entry_price']
    atr_series   = calc_atr14(df)
    atr          = float(atr_series.iloc[-1])
    if pd.isna(atr) or atr == 0:
        return current_stop
    price_10am = float(df['price_10am'].iloc[-1])
    if price_10am >= entry_price + atr:
        return max(current_stop, entry_price)
    return current_stop


def run_backtest(ticker_dfs: dict) -> dict:
    """
    Run chronological backtest over BACKTEST_START..BACKTEST_END.
    ticker_dfs: {ticker: full history DataFrame with date index}
    Returns stats dict: sharpe, total_trades, win_rate, avg_pnl_per_trade, total_pnl.
    """
    start = date.fromisoformat(BACKTEST_START)
    end   = date.fromisoformat(BACKTEST_END)

    # Collect all trading days that fall in [start, end)
    all_days: set = set()
    for df in ticker_dfs.values():
        for d in df.index:
            if start <= d < end:
                all_days.add(d)
    trading_days = sorted(all_days)

    if len(trading_days) < 2:
        return {"sharpe": 0.0, "total_trades": 0, "win_rate": 0.0,
                "avg_pnl_per_trade": 0.0, "total_pnl": 0.0}

    portfolio: dict = {}   # ticker -> position dict
    trades: list = []      # list of pnl floats per closed trade
    daily_values: list = []

    for i, today in enumerate(trading_days):
        prev_day = trading_days[i - 1] if i > 0 else None

        # 1. Check stops using previous day's low
        if prev_day is not None:
            to_close = []
            for ticker, pos in portfolio.items():
                df = ticker_dfs[ticker]
                if prev_day in df.index:
                    prev_low = float(df.loc[prev_day, "low"])
                    if prev_low <= pos["stop_price"]:
                        pnl = (pos["stop_price"] - pos["entry_price"]) * pos["shares"]
                        trades.append(pnl)
                        to_close.append(ticker)
            for t in to_close:
                del portfolio[t]

        # 2. Screen for new entries
        for ticker, df in ticker_dfs.items():
            if ticker in portfolio:
                continue
            hist = df.loc[:today]
            signal = screen_day(hist, today)
            if signal is None:
                continue
            entry_price = signal["entry_price"] + 0.03
            shares = 500.0 / entry_price
            portfolio[ticker] = {
                "entry_price": entry_price,
                "entry_date": today,
                "shares": shares,
                "stop_price": signal["stop"],
                "ticker": ticker,
            }

        # 3. Manage positions (including new entries)
        for ticker, pos in portfolio.items():
            df = ticker_dfs[ticker]
            hist = df.loc[:today]
            new_stop = manage_position(pos, hist)
            pos["stop_price"] = max(new_stop, pos["stop_price"])

        # 4. Mark-to-market: portfolio value = sum of (price_10am × shares) for open positions
        portfolio_value = 0.0
        for ticker, pos in portfolio.items():
            df = ticker_dfs[ticker]
            if today in df.index:
                portfolio_value += float(df.loc[today, "price_10am"]) * pos["shares"]
        daily_values.append(portfolio_value)

    # End of backtest: close all remaining positions at last available price_10am
    for ticker, pos in portfolio.items():
        df = ticker_dfs[ticker]
        last_price = float(df["price_10am"].iloc[-1])
        pnl = (last_price - pos["entry_price"]) * pos["shares"]
        trades.append(pnl)

    # Sharpe computation (PRD Feature 5): annualised daily-changes Sharpe
    arr = np.array(daily_values, dtype=float)
    changes = np.diff(arr)
    if len(changes) == 0 or changes.std() == 0:
        sharpe = 0.0
    else:
        sharpe = float((changes.mean() / changes.std()) * np.sqrt(252))

    total_trades = len(trades)
    total_pnl    = float(sum(trades))
    wins         = sum(1 for p in trades if p > 0)
    win_rate     = (wins / total_trades) if total_trades > 0 else 0.0
    avg_pnl      = (total_pnl / total_trades) if total_trades > 0 else 0.0

    return {
        "sharpe":            round(sharpe, 6),
        "total_trades":      total_trades,
        "win_rate":          round(win_rate, 3),
        "avg_pnl_per_trade": round(avg_pnl, 2),
        "total_pnl":         round(total_pnl, 2),
    }


def print_results(stats: dict) -> None:
    """Print the fixed-format summary block. Agent parses this with grep."""
    print("---")
    print(f"sharpe:              {stats['sharpe']:.6f}")
    print(f"total_trades:        {stats['total_trades']}")
    print(f"win_rate:            {stats['win_rate']:.3f}")
    print(f"avg_pnl_per_trade:   {stats['avg_pnl_per_trade']:.2f}")
    print(f"total_pnl:           {stats['total_pnl']:.2f}")
    print(f"backtest_start:      {BACKTEST_START}")
    print(f"backtest_end:        {BACKTEST_END}")


if __name__ == "__main__":
    ticker_dfs = load_all_ticker_data()
    if not ticker_dfs:
        print(f"No cached data in {CACHE_DIR}. Run prepare.py first.", file=sys.stderr)
        sys.exit(1)
    stats = run_backtest(ticker_dfs)
    print_results(stats)
