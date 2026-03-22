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
BACKTEST_START = "2025-12-20"
BACKTEST_END   = "2026-03-20"

# Train/test split — last 14 calendar days of the backtest window are held out as test set.
# Written by the agent at setup time. Do NOT modify during the experiment loop.
TRAIN_END   = "2026-03-06"   # BACKTEST_END − 14 calendar days
TEST_START  = "2026-03-06"   # same date as TRAIN_END (test window starts here)

# Final output flag — set to True only for the special post-loop final test run.
# Agent sets True, runs train.py, then immediately restores to False.
WRITE_FINAL_OUTPUTS = False

# Risk-proportional sizing: dollar risk per trade (V3-A R3)
RISK_PER_TRADE = 50.0


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
    hist['_sma50']  = hist['close'].rolling(50).mean()
    hist['_vm30']   = hist['volume'].rolling(30).mean()
    hist['_atr14']  = calc_atr14(hist)
    hist['_rsi14']  = calc_rsi14(hist)

    sma50 = float(hist['_sma50'].iloc[-1])
    vm30  = float(hist['_vm30'].iloc[-1])
    atr   = float(hist['_atr14'].iloc[-1])
    rsi   = float(hist['_rsi14'].iloc[-1])
    # Today's observable data: only price_10am and partial-day volume
    price_10am = float(df['price_10am'].iloc[-1])
    today_vol  = float(df['volume'].iloc[-1])

    # Guard NaN/zero
    if pd.isna(price_10am) or pd.isna(sma50) or pd.isna(vm30) or pd.isna(atr) or pd.isna(rsi) or pd.isna(today_vol) or vm30 == 0 or atr == 0:
        return None

    # Rule 1: price above SMA50 (short-term uptrend)
    if price_10am <= sma50:
        return None

    # Rule 2a: price_10am breaks above the 20-day highest close (breakout)
    high20 = float(hist['close'].iloc[-20:].max())   # last 20 days of history
    if price_10am <= high20:
        return None

    # Rule 2b: price_10am also above yesterday's high (breakout continuation)
    prev_high = float(hist['high'].iloc[-1])          # yesterday's high
    if price_10am <= prev_high:
        return None

    # Rule 3: today's volume >= 1.0× MA30 (average or above)
    vol_ratio = today_vol / vm30
    if vol_ratio < 1.0:
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
    if price_10am >= entry_price + 1.5 * atr:
        return max(current_stop, entry_price)
    return current_stop


# ── DO NOT EDIT BELOW THIS LINE ───────────────────────────────────────────────
# run_backtest(), print_results(), _write_final_outputs(), load_ticker_data(),
# load_all_ticker_data(), and the __main__ block are the evaluation harness.
# They must not be modified.
#
# DEVELOPERS / MAINTAINERS: if you intentionally change anything below this
# line, you must also update GOLDEN_HASH in tests/test_optimization.py and
# rerun `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged`.
# ──────────────────────────────────────────────────────────────────────────────

def run_backtest(ticker_dfs: dict, start: str | None = None, end: str | None = None) -> dict:
    """
    Run chronological backtest over start..end (defaults to BACKTEST_START..BACKTEST_END).
    Returns stats dict: sharpe, total_trades, win_rate, avg_pnl_per_trade, total_pnl,
                        ticker_pnl, backtest_start, backtest_end.
    """
    s = date.fromisoformat(start or BACKTEST_START)
    e = date.fromisoformat(end or BACKTEST_END)

    # Collect all trading days that fall in [s, e)
    all_days: set = set()
    for df in ticker_dfs.values():
        for d in df.index:
            if s <= d < e:
                all_days.add(d)
    trading_days = sorted(all_days)

    if len(trading_days) < 2:
        return {"sharpe": 0.0, "total_trades": 0, "win_rate": 0.0,
                "avg_pnl_per_trade": 0.0, "total_pnl": 0.0,
                "ticker_pnl": {}, "backtest_start": start or BACKTEST_START,
                "backtest_end": end or BACKTEST_END, "trade_records": []}

    portfolio: dict = {}   # ticker -> position dict
    trades: list = []      # list of pnl floats per closed trade
    trade_records: list = []   # R5: per-trade attribution dicts
    daily_values: list = []
    ticker_pnl: dict[str, float] = {}

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
                        ticker_pnl[ticker] = ticker_pnl.get(ticker, 0.0) + pnl
                        trade_records.append({
                            "ticker":       ticker,
                            "entry_date":   str(pos["entry_date"]),
                            "exit_date":    str(prev_day),
                            "days_held":    (prev_day - pos["entry_date"]).days,
                            "stop_type":    pos.get("stop_type", "unknown"),
                            "entry_price":  round(pos["entry_price"], 4),
                            "exit_price":   round(pos["stop_price"], 4),
                            "pnl":          round(pnl, 2),
                        })
                        to_close.append(ticker)
            for t in to_close:
                del portfolio[t]

        # 2. Manage existing positions (before screening, so new entries are excluded)
        for ticker, pos in portfolio.items():
            df = ticker_dfs[ticker]
            hist = df.loc[:today]
            new_stop = manage_position(pos, hist)
            pos["stop_price"] = max(new_stop, pos["stop_price"])

        # 3. Screen for new entries
        for ticker, df in ticker_dfs.items():
            if ticker in portfolio:
                continue
            hist = df.loc[:today]
            signal = screen_day(hist, today)
            if signal is None:
                continue
            entry_price = signal["entry_price"] + 0.03
            risk = entry_price - signal["stop"]
            shares = RISK_PER_TRADE / risk if risk > 0 else RISK_PER_TRADE / entry_price
            portfolio[ticker] = {
                "entry_price": entry_price,
                "entry_date": today,
                "shares": shares,
                "stop_price": signal["stop"],
                "stop_type": signal.get("stop_type", "unknown"),   # R5
                "ticker": ticker,
            }

        # 4. Mark-to-market: portfolio value = sum of (price_10am × shares) for open positions
        portfolio_value = 0.0
        for ticker, pos in portfolio.items():
            df = ticker_dfs[ticker]
            if today in df.index:
                portfolio_value += float(df.loc[today, "price_10am"]) * pos["shares"]
        daily_values.append(portfolio_value)

    # End of backtest: close all remaining positions at last available price_10am
    last_day = trading_days[-1] if trading_days else None
    for ticker, pos in portfolio.items():
        df = ticker_dfs[ticker]
        last_price = float(df["price_10am"].iloc[-1])
        pnl = (last_price - pos["entry_price"]) * pos["shares"]
        trades.append(pnl)
        ticker_pnl[ticker] = ticker_pnl.get(ticker, 0.0) + pnl
        trade_records.append({
            "ticker":       ticker,
            "entry_date":   str(pos["entry_date"]),
            "exit_date":    str(last_day),
            "days_held":    (last_day - pos["entry_date"]).days if last_day else 0,
            "stop_type":    pos.get("stop_type", "unknown"),
            "entry_price":  round(pos["entry_price"], 4),
            "exit_price":   round(last_price, 4),
            "pnl":          round(pnl, 2),
        })

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
        "ticker_pnl":        ticker_pnl,
        "backtest_start":    start or BACKTEST_START,
        "backtest_end":      end or BACKTEST_END,
        "trade_records":     trade_records,   # R5
    }


def print_results(stats: dict, prefix: str = "") -> None:
    """Print the fixed-format summary block. Agent parses this with grep."""
    print("---")
    print(f"{prefix}sharpe:              {stats['sharpe']:.6f}")
    print(f"{prefix}total_trades:        {stats['total_trades']}")
    print(f"{prefix}win_rate:            {stats['win_rate']:.3f}")
    print(f"{prefix}avg_pnl_per_trade:   {stats['avg_pnl_per_trade']:.2f}")
    print(f"{prefix}total_pnl:           {stats['total_pnl']:.2f}")
    print(f"{prefix}backtest_start:      {stats['backtest_start']}")
    print(f"{prefix}backtest_end:        {stats['backtest_end']}")


def _write_final_outputs(ticker_dfs: dict, test_start: str, test_end: str,
                         ticker_pnl: dict) -> None:
    """Write final_test_data.csv and print per-ticker P&L table for the test window."""
    import csv
    s = date.fromisoformat(test_start)
    e = date.fromisoformat(test_end)
    rows = []
    for ticker, df in ticker_dfs.items():
        # Compute rolling indicators on the full history so test-window values are meaningful.
        # Slicing first would leave sma50/rsi14/atr14/vm30 as NaN across the 14-day window.
        full = df.copy()
        full["sma50"] = full["close"].rolling(50).mean()
        full["rsi14"] = calc_rsi14(full)
        full["atr14"] = calc_atr14(full)
        full["vm30"]  = full["volume"].rolling(30).mean()
        sub = full[(full.index >= s) & (full.index < e)]
        if sub.empty:
            continue
        for idx_date, row in sub.iterrows():
            rows.append({
                "ticker": ticker,
                "date": str(idx_date),
                **{c: round(float(row[c]), 4) if not pd.isna(row[c]) else ""
                   for c in ["open", "high", "low", "close", "volume",
                              "price_10am", "sma50", "rsi14", "atr14", "vm30"]},
            })
    if rows:
        fieldnames = list(rows[0].keys())
        with open("final_test_data.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        print(f"final_test_data.csv written ({len(rows)} rows)")
    if ticker_pnl:
        sorted_pnl = sorted(ticker_pnl.items(), key=lambda x: x[1], reverse=True)
        print("\nPer-ticker P&L (test window):")
        print(f"  {'Ticker':<10} {'P&L':>10}")
        print("  " + "-" * 22)
        for t, p in sorted_pnl:
            print(f"  {t:<10} {p:>10.2f}")


def _write_trades_tsv(trade_records: list) -> None:
    """Write per-trade records to trades.tsv (tab-separated). Overwrites each run."""
    import csv
    fieldnames = ["ticker", "entry_date", "exit_date", "days_held",
                  "stop_type", "entry_price", "exit_price", "pnl"]
    with open("trades.tsv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")
        w.writeheader()
        w.writerows(trade_records)


if __name__ == "__main__":
    ticker_dfs = load_all_ticker_data()
    if not ticker_dfs:
        print(f"No cached data in {CACHE_DIR}. Run prepare.py first.", file=sys.stderr)
        sys.exit(1)
    train_stats = run_backtest(ticker_dfs, start=BACKTEST_START, end=TRAIN_END)
    print_results(train_stats, prefix="train_")
    _write_trades_tsv(train_stats["trade_records"])   # R5
    test_stats = run_backtest(ticker_dfs, start=TEST_START, end=BACKTEST_END)
    print_results(test_stats, prefix="test_")
    if WRITE_FINAL_OUTPUTS:
        _write_final_outputs(ticker_dfs, TEST_START, BACKTEST_END, test_stats["ticker_pnl"])
