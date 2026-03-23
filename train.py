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
BACKTEST_START = "2025-12-20"
BACKTEST_END   = "2026-03-20"

# Train/test split — last 14 calendar days of the backtest window are held out as test set.
# Written by the agent at setup time. Do NOT modify during the experiment loop.
TRAIN_END   = "2026-03-06"   # BACKTEST_END − 14 calendar days
TEST_START  = "2026-03-06"   # same date as TRAIN_END (test window starts here)

# Final output flag — set to True only for the special post-loop final test run.
# Agent sets True, runs train.py, then immediately restores to False.
WRITE_FINAL_OUTPUTS = False

# Walk-forward evaluation: number of rolling test folds (V3-B R2)
WALK_FORWARD_WINDOWS = 3

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
TICKER_HOLDOUT_FRAC = 0.0

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

def detect_regime(ticker_dfs: dict, today) -> str:
    """
    Classify today's market regime as 'bull' or 'bear' using cross-sectional SMA50 majority vote.
    A ticker votes 'bull' if today's price_10am > its 50-day SMA (computed on history up to and
    including today). Returns 'bull' if bull_count >= bear_count, else 'bear'.
    Returns 'unknown' if fewer than 2 tickers have valid data for today.
    Note: includes today's close in the SMA50 — one-bar look-ahead (~2% of the 50-bar window),
    acceptable in this EOD data context since entry decisions use price_10am, not the close.
    """
    bull_count = 0
    bear_count = 0
    for ticker, df in ticker_dfs.items():
        hist = df[df.index <= today]
        if len(hist) < 51:
            continue
        sma50 = float(hist['close'].rolling(50).mean().iloc[-1])
        price = float(hist['price_10am'].iloc[-1])
        if pd.isna(sma50) or pd.isna(price):
            continue
        if price > sma50:
            bull_count += 1
        else:
            bear_count += 1
    if bull_count + bear_count < 2:
        return 'unknown'
    return 'bull' if bull_count >= bear_count else 'bear'


def _compute_avg_correlation(ticker_dfs: dict, tickers: list, start, end) -> float:
    """
    Mean pairwise Pearson correlation of daily price_10am returns for the given tickers
    over [start, end). Returns 0.0 if fewer than 2 tickers have data.
    """
    series = []
    for t in tickers:
        if t not in ticker_dfs:
            continue
        df = ticker_dfs[t]
        sub = df[(df.index >= start) & (df.index < end)]["price_10am"].dropna()
        if len(sub) >= 2:
            series.append(sub.pct_change().dropna())
    if len(series) < 2:
        return 0.0
    # Align on common index
    aligned = pd.concat(series, axis=1).dropna()
    if aligned.shape[0] < 2 or aligned.shape[1] < 2:
        return 0.0
    corr_matrix = aligned.corr().values
    n = corr_matrix.shape[0]
    off_diag = [(corr_matrix[i, j]) for i in range(n) for j in range(i + 1, n)]
    return float(np.mean(off_diag)) if off_diag else 0.0


def run_backtest(ticker_dfs: dict, start: str | None = None, end: str | None = None,
                 _price_bias: float = 0.0, _stop_atr_bias: float = 0.0) -> dict:
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
                "backtest_end": end or BACKTEST_END, "trade_records": [],
                "max_drawdown": 0.0, "calmar": 0.0, "pnl_consistency": 0.0, "pnl_min": 0.0,
                "regime_stats": {}}

    portfolio: dict = {}   # ticker -> position dict
    trades: list = []      # list of pnl floats per closed trade
    trade_records: list = []   # R5: per-trade attribution dicts
    daily_values: list = []
    ticker_pnl: dict[str, float] = {}
    # R7: track cumulative realized P&L + open MTM for equity curve metrics
    cumulative_realized: float = 0.0
    equity_curve: list = []

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
                        cumulative_realized += pnl
                        ticker_pnl[ticker] = ticker_pnl.get(ticker, 0.0) + pnl
                        trade_records.append({
                            "ticker":       ticker,
                            "entry_date":   str(pos["entry_date"]),
                            "exit_date":    str(prev_day),
                            "days_held":    (prev_day - pos["entry_date"]).days,
                            "stop_type":    pos.get("stop_type", "unknown"),
                            "regime":       pos.get("regime", "unknown"),       # R11
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
        # Compute regime once per day — all entries on the same day share the same regime.
        _today_regime = detect_regime(ticker_dfs, today)
        for ticker, df in ticker_dfs.items():
            if ticker in portfolio:
                continue
            if len(portfolio) >= MAX_SIMULTANEOUS_POSITIONS:   # R8: position cap
                continue
            hist = df.loc[:today]
            signal = screen_day(hist, today)
            if signal is None:
                continue
            entry_price = (signal["entry_price"] + 0.03) * (1 + _price_bias)
            stop_raw    = signal["stop"] + _stop_atr_bias * signal.get("atr14", 0.0)
            risk = entry_price - stop_raw
            shares = RISK_PER_TRADE / risk if risk > 0 else RISK_PER_TRADE / entry_price
            _entry_regime = _today_regime
            portfolio[ticker] = {
                "entry_price": entry_price,
                "entry_date": today,
                "shares": shares,
                "stop_price": stop_raw,
                "stop_type": signal.get("stop_type", "unknown"),   # R5
                "ticker": ticker,
                "regime": _entry_regime,                            # R11
            }

        # 4. Mark-to-market: portfolio value = sum of (price_10am × shares) for open positions
        # Skip NaN prices (e.g. data gaps) to avoid poisoning the Sharpe and equity-curve metrics.
        portfolio_value = 0.0
        for ticker, pos in portfolio.items():
            df = ticker_dfs[ticker]
            if today in df.index:
                price = float(df.loc[today, "price_10am"])
                if not np.isnan(price):
                    portfolio_value += price * pos["shares"]
        daily_values.append(portfolio_value)
        # R7: equity = realized gains so far + current open position MTM
        equity_curve.append((today, cumulative_realized + portfolio_value))

    # End of backtest: close all remaining positions at last available price_10am
    last_day = trading_days[-1] if trading_days else None
    for ticker, pos in portfolio.items():
        df = ticker_dfs[ticker]
        last_price = float(df["price_10am"].iloc[-1])
        pnl = (last_price - pos["entry_price"]) * pos["shares"]
        trades.append(pnl)
        cumulative_realized += pnl
        ticker_pnl[ticker] = ticker_pnl.get(ticker, 0.0) + pnl
        trade_records.append({
            "ticker":       ticker,
            "entry_date":   str(pos["entry_date"]),
            "exit_date":    str(last_day),
            "days_held":    (last_day - pos["entry_date"]).days if last_day else 0,
            "stop_type":    pos.get("stop_type", "unknown"),
            "regime":       pos.get("regime", "unknown"),       # R11
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

    # R8: Correlation penalty — discounts PnL for concentrated correlated portfolios.
    # Only apply when total_pnl > 0: the formula total_pnl*(1 - weight*corr) inverts
    # the penalty sign on negative PnL, which would soften losses instead of penalising them.
    if CORRELATION_PENALTY_WEIGHT > 0.0 and len(ticker_pnl) >= 2 and total_pnl > 0:
        _traded_tickers = list(ticker_pnl.keys())
        _avg_corr = _compute_avg_correlation(ticker_dfs, _traded_tickers, s, e)
        total_pnl = round(total_pnl - CORRELATION_PENALTY_WEIGHT * _avg_corr * total_pnl, 2)

    # R7: Equity curve metrics
    if equity_curve:
        eq_values = np.array([v for _, v in equity_curve], dtype=float)
        peak = np.maximum.accumulate(eq_values)
        max_drawdown = float(np.max(peak - eq_values))
    else:
        max_drawdown = 0.0

    calmar = (total_pnl / max_drawdown) if max_drawdown > 0 else 0.0

    # R7: Monthly P&L consistency (min monthly P&L across all months in window)
    monthly_equity: dict = {}
    for dt, eq in equity_curve:
        key = (dt.year, dt.month)
        monthly_equity[key] = eq   # last equity value for that month
    month_keys = sorted(monthly_equity.keys())
    monthly_pnl_list = []
    prev_eq = 0.0
    for mk in month_keys:
        monthly_pnl_list.append(monthly_equity[mk] - prev_eq)
        prev_eq = monthly_equity[mk]
    pnl_consistency = min(monthly_pnl_list) if monthly_pnl_list else total_pnl

    # R9: Robustness perturbation — run additional seeds with jittered entries/stops
    # Only run from the nominal call (both biases == 0) to avoid infinite recursion.
    # Invariant: all vectors in _PERTURBATION_VECTORS must be non-zero so recursive
    # perturbed calls always have a non-zero bias and skip this block.
    _PERTURBATION_VECTORS = [
        (-0.005, -0.3), (-0.005, +0.3),
        (+0.005, -0.3), (+0.005, +0.3),
    ]
    pnl_min = total_pnl
    if ROBUSTNESS_SEEDS > 0 and _price_bias == 0.0 and _stop_atr_bias == 0.0:
        all_pnls = [total_pnl]
        n_extra = min(ROBUSTNESS_SEEDS - 1, len(_PERTURBATION_VECTORS))
        for _pb, _sb in _PERTURBATION_VECTORS[:n_extra]:
            _perturbed = run_backtest(ticker_dfs, start=start, end=end,
                                      _price_bias=_pb, _stop_atr_bias=_sb)
            all_pnls.append(_perturbed["total_pnl"])
        pnl_min = round(min(all_pnls), 2)

    # R11: Per-regime trade attribution
    regime_stats: dict = {}
    for _rec in trade_records:
        _r = _rec.get("regime", "unknown")
        if _r not in regime_stats:
            regime_stats[_r] = {"trades": 0, "wins": 0, "pnl": 0.0}
        regime_stats[_r]["trades"] += 1
        if _rec["pnl"] > 0:
            regime_stats[_r]["wins"] += 1
        # Both operands are already 2dp (trade records are pre-rounded), so intermediate
        # rounding is benign but kept for consistency with the pnl_min accumulation pattern.
        regime_stats[_r]["pnl"] = round(regime_stats[_r]["pnl"] + _rec["pnl"], 2)

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
        "max_drawdown":      round(max_drawdown, 2),
        "calmar":            round(calmar, 4),
        "pnl_consistency":   round(pnl_consistency, 2),
        "pnl_min":           pnl_min,
        "regime_stats":      regime_stats,   # R11
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
    print(f"{prefix}calmar:              {stats.get('calmar', 0.0):.4f}")
    print(f"{prefix}pnl_consistency:     {stats.get('pnl_consistency', 0.0):.2f}")
    print(f"{prefix}pnl_min:             {stats.get('pnl_min', stats['total_pnl']):.2f}")


def _bootstrap_ci(pnls: list, n_boot: int = 2000, ci: float = 0.90) -> tuple:
    """
    Bootstrap CI on total P&L by resampling closed trade P&Ls with replacement.
    Uses a fixed seed (42) for deterministic output.
    ci=0.90 → alpha=0.05 → 5th/95th percentile bounds, matching the p05/p95 print labels.
    Returns (p_low, p_high) percentile bounds. Returns (0.0, 0.0) if fewer than 2 trades.
    """
    if len(pnls) < 2:
        return (0.0, 0.0)
    rng = np.random.default_rng(42)
    pnl_arr = np.array(pnls, dtype=float)
    boot_totals = np.array([
        rng.choice(pnl_arr, size=len(pnl_arr), replace=True).sum()
        for _ in range(n_boot)
    ])
    alpha = (1.0 - ci) / 2.0
    return (float(np.percentile(boot_totals, 100.0 * alpha)),
            float(np.percentile(boot_totals, 100.0 * (1.0 - alpha))))


def _write_final_outputs(ticker_dfs: dict, test_start: str, test_end: str,
                         ticker_pnl: dict, trade_records: list | None = None) -> None:
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
    # R10: Bootstrap CI on final test P&L
    if trade_records:
        _pnls = [r["pnl"] for r in trade_records]
        _ci_low, _ci_high = _bootstrap_ci(_pnls)
        print(f"bootstrap_pnl_p05:       {_ci_low:.2f}")
        print(f"bootstrap_pnl_p95:       {_ci_high:.2f}")


def _write_trades_tsv(trade_records: list, annotation: str | None = None) -> None:
    """Write per-trade records to trades.tsv (tab-separated). Overwrites each run.
    If annotation is provided, writes it as a comment line before the header row."""
    import csv
    fieldnames = ["ticker", "entry_date", "exit_date", "days_held",
                  "stop_type", "regime", "entry_price", "exit_price", "pnl"]
    with open("trades.tsv", "w", newline="", encoding="utf-8") as f:
        if annotation:
            f.write(f"# {annotation}\n")
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t", restval="")
        w.writeheader()
        w.writerows(trade_records)


if __name__ == "__main__":
    ticker_dfs = load_all_ticker_data()
    if not ticker_dfs:
        print(f"No cached data in {CACHE_DIR}. Run prepare.py first.", file=sys.stderr)
        sys.exit(1)

    # R6: Ticker holdout — deterministic tail split of sorted ticker list
    _all_tickers_sorted = sorted(ticker_dfs.keys())
    _n_holdout = round(TICKER_HOLDOUT_FRAC * len(_all_tickers_sorted)) if TICKER_HOLDOUT_FRAC > 0 else 0
    _holdout_set = set(_all_tickers_sorted[-_n_holdout:]) if _n_holdout > 0 else set()
    _train_ticker_dfs = {t: df for t, df in ticker_dfs.items() if t not in _holdout_set}
    _holdout_ticker_dfs = {t: df for t, df in ticker_dfs.items() if t in _holdout_set}
    if not _train_ticker_dfs:
        _train_ticker_dfs = ticker_dfs  # safety: if holdout fraction is too large, fall back

    # V3-F: Test-extra tickers — included in fold test calls only, never in training.
    _extra_ticker_dfs = {t: ticker_dfs[t] for t in TEST_EXTRA_TICKERS if t in ticker_dfs}
    _test_ticker_dfs  = {**_train_ticker_dfs, **_extra_ticker_dfs}

    # R2: Walk-forward CV — N folds with FOLD_TEST_DAYS-business-day test windows (V3-E)
    # stepping back from TRAIN_END.
    import pandas as _pd
    from pandas.tseries.offsets import BDay as _BDay

    _train_end_ts = _pd.Timestamp(TRAIN_END)
    fold_test_pnls: list = []
    last_fold_train_records: list = []
    _fold_train_stats: dict = {}   # guard for WALK_FORWARD_WINDOWS=0

    for _i in range(WALK_FORWARD_WINDOWS):
        # Fold _i (0-indexed, oldest first).
        # Fold WALK_FORWARD_WINDOWS-1 (newest) has test window ending at TRAIN_END.
        _steps_back = WALK_FORWARD_WINDOWS - 1 - _i
        _fold_test_end_ts   = _train_end_ts - _BDay(_steps_back * FOLD_TEST_DAYS)
        _fold_test_start_ts = _fold_test_end_ts - _BDay(FOLD_TEST_DAYS)
        _fold_train_end_ts  = _fold_test_start_ts

        _fold_train_end   = str(_fold_train_end_ts.date())
        _fold_test_start  = str(_fold_test_start_ts.date())
        _fold_test_end    = str(_fold_test_end_ts.date())
        _fold_n           = _i + 1

        if FOLD_TRAIN_DAYS > 0:
            _fold_train_start_ts = _fold_train_end_ts - _BDay(FOLD_TRAIN_DAYS)
            _fold_train_start = str(max(_fold_train_start_ts.date(),
                                        date.fromisoformat(BACKTEST_START)))
        else:
            _fold_train_start = BACKTEST_START
        _fold_train_stats = run_backtest(_train_ticker_dfs, start=_fold_train_start, end=_fold_train_end)
        _fold_test_stats  = run_backtest(_test_ticker_dfs,  start=_fold_test_start, end=_fold_test_end)

        print_results(_fold_train_stats, prefix=f"fold{_fold_n}_train_")
        print_results(_fold_test_stats,  prefix=f"fold{_fold_n}_test_")

        fold_test_pnls.append(_fold_test_stats["total_pnl"])
        if _fold_n == WALK_FORWARD_WINDOWS:
            last_fold_train_records = _fold_train_stats["trade_records"]

    min_test_pnl = min(fold_test_pnls) if fold_test_pnls else 0.0
    print("---")
    print(f"min_test_pnl:            {min_test_pnl:.2f}")

    # R6: Evaluate on held-out tickers (generalization check)
    if _holdout_ticker_dfs:
        _holdout_stats = run_backtest(_holdout_ticker_dfs, start=BACKTEST_START, end=TRAIN_END)
        print("---")
        print(f"ticker_holdout_pnl:      {_holdout_stats['total_pnl']:.2f}")
        print(f"ticker_holdout_trades:   {_holdout_stats['total_trades']}")

    # Write trades.tsv from the most recent training fold
    _last_fold_pnl_min = _fold_train_stats.get("pnl_min", _fold_train_stats["total_pnl"])
    _annotation = f"pnl_min: ${_last_fold_pnl_min:.2f}" if ROBUSTNESS_SEEDS > 0 else None
    _write_trades_tsv(last_fold_train_records, annotation=_annotation)

    # R4-full: Silent holdout — [TRAIN_END, BACKTEST_END]
    _silent_stats = run_backtest(ticker_dfs, start=TRAIN_END, end=BACKTEST_END)
    print("---")
    if WRITE_FINAL_OUTPUTS:
        print_results(_silent_stats, prefix="holdout_")
        _write_final_outputs(ticker_dfs, TRAIN_END, BACKTEST_END,
                             _silent_stats["ticker_pnl"],
                             _silent_stats["trade_records"])   # R10: pass trade_records
    else:
        print(f"silent_pnl: HIDDEN")
