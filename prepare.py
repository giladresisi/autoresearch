"""
prepare.py — One-time stock data download and cache for the autoresearch backtester.

Usage:
    uv run prepare.py

Edit TICKERS, BACKTEST_START, and BACKTEST_END below before running.
Data is cached in ~/.cache/autoresearch/stock_data/{TICKER}.parquet.
Running again skips tickers whose file already exists (idempotent).
"""
import datetime
import os
import sys
import warnings

import pandas as pd
import yfinance as yf

# ── USER CONFIGURATION ──────────────────────────────────────────────────────
# These are the DEFAULT values used by the agent loop (see program.md).
# The agent loop setup will overwrite TICKERS, BACKTEST_START, and BACKTEST_END
# based on the parameters the user specifies in their request. Edit the values
# here directly when running prepare.py manually outside the agent loop.
TICKERS = [
    # Tech / high-beta (25) — high ATR names test stop/sizing logic under volatility
    "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN", "TSLA", "AVGO", "ORCL",
    "CRM", "ADBE", "QCOM", "MU", "AMAT", "NOW", "PLTR", "MSTR", "APP", "SMCI",
    "NFLX", "COIN", "CRWD", "ZS", "PANW",
    # Financials (12) — rate-sensitive, different volatility profile
    "JPM", "GS", "BAC", "WFC", "MS", "BLK", "SCHW", "AXP", "COF", "SPGI", "V", "MA",
    # Healthcare (12) — defensive, lower correlation to tech
    "UNH", "LLY", "ABBV", "JNJ", "MRK", "PFE", "TMO", "ISRG", "AMGN", "GILD", "REGN", "VRTX",
    # Energy (8) — commodity-driven, high sector correlation
    "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "VLO", "OXY",
    # Consumer Staples (8) — low-beta, regime stress test
    "WMT", "PG", "KO", "PEP", "COST", "TGT", "PM", "CL",
    # Industrials (8)
    "CAT", "DE", "UPS", "FDX", "GE", "HON", "RTX", "LMT",
    # Consumer Discretionary (7)
    "HD", "MCD", "NKE", "SBUX", "LOW", "F", "GM",
    # Materials (5)
    "LIN", "APD", "NEM", "FCX", "NUE",
]

BACKTEST_START = "2024-09-01"  # first day of the backtest window (inclusive)
BACKTEST_END   = "2026-03-20"  # last day of the backtest window (exclusive)
# ────────────────────────────────────────────────────────────────────────────

# Derived (do not modify)
# yfinance 1h data is limited to ~730 days from today. Cap HISTORY_START so the request
# is always within the available window. SMA50 needs ~50 bars (~2.5 months) of warmup;
# the cap gives ~5 months of pre-backtest data when BACKTEST_START = 2024-09-01.
HISTORY_START = max(
    (pd.Timestamp(BACKTEST_START) - pd.DateOffset(years=1)).strftime("%Y-%m-%d"),
    (pd.Timestamp(BACKTEST_END) - pd.DateOffset(days=720)).strftime("%Y-%m-%d"),
)
# Cache directory for parquet files. Override with AUTORESEARCH_CACHE_DIR env var
# to maintain independent datasets for different sessions or date ranges.
CACHE_DIR = os.environ.get(
    "AUTORESEARCH_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data"),
)


def download_ticker(ticker: str) -> pd.DataFrame:
    """Fetch hourly OHLCV from yfinance for the full history + backtest window."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(
            start=HISTORY_START,
            end=BACKTEST_END,
            interval="1h",
            auto_adjust=True,
            prepost=False,
        )
    return df


def resample_to_daily(df_hourly: pd.DataFrame) -> pd.DataFrame:
    """
    Convert hourly yfinance data to daily OHLCV + price_10am.
    Index becomes Python date objects named 'date'; columns are all lowercase.
    """
    df = df_hourly.copy()

    # Normalize index to America/New_York — required for 10am bar extraction
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("America/New_York")

    # Extract the 9:30 AM ET open price for each trading day.
    # yfinance 1h bars are labeled at the start of each period (9:30, 10:30, ...),
    # so there is no 10:00 AM bar. Use the 9:30 AM bar (market open) as price_10am.
    mask = df.index.time == datetime.time(9, 30)
    df_10am = df[mask][["Open"]].copy()
    df_10am.index = pd.Index([ts.date() for ts in df_10am.index], name="date")
    price_10am_series = df_10am["Open"].rename("price_10am")

    # Resample to calendar-day OHLCV; drop non-trading days (NaN rows)
    daily = df.resample("D").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    })
    daily = daily.dropna(subset=["Open"])
    # Use list comprehension to produce date objects (not pd.Timestamp) to match train.py slicing
    daily.index = pd.Index([ts.date() for ts in daily.index], name="date")

    daily = daily.join(price_10am_series, how="left")
    daily.columns = [c.lower() for c in daily.columns]
    daily.index.name = "date"

    return daily


def validate_ticker_data(ticker: str, df: pd.DataFrame, backtest_start: str) -> None:
    """Print warnings for insufficient history or missing 10am bars in the backtest window."""
    if len(df) < 200:
        print(f"WARNING: {ticker} has only {len(df)} rows — insufficient indicator history (need ≥ 200)")

    backtest_mask = df.index >= pd.Timestamp(backtest_start).date()
    backtest_df = df[backtest_mask]
    n_missing = int(backtest_df["price_10am"].isna().sum())
    if n_missing > 0:
        print(f"WARNING: {ticker} has {n_missing} backtest days with missing price_10am")


def write_trend_summary(tickers: list, backtest_start: str, backtest_end: str, cache_dir: str) -> None:
    """Compute sector price behaviour for the backtest window and write data_trend.md."""
    records = []
    for ticker in tickers:
        path = os.path.join(cache_dir, f"{ticker}.parquet")
        if not os.path.exists(path):
            continue
        df = pd.read_parquet(path)
        start_dt = pd.Timestamp(backtest_start).date()
        end_dt   = pd.Timestamp(backtest_end).date()
        sub = df[(df.index >= start_dt) & (df.index < end_dt)]
        if len(sub) < 2:
            continue
        first_close = float(sub["close"].iloc[0])
        last_close  = float(sub["close"].iloc[-1])
        if first_close == 0:
            continue
        ret = (last_close - first_close) / first_close
        records.append((ticker, ret))

    if not records:
        with open("data_trend.md", "w", encoding="utf-8") as f:
            f.write("# Sector Trend Summary\n\nNo data available.\n")
        return

    records.sort(key=lambda x: x[1], reverse=True)
    returns = [r for _, r in records]
    n = len(returns)
    sorted_returns = sorted(returns)
    # True median: average the two middle values for even N
    median_ret = float((sorted_returns[n // 2 - 1] + sorted_returns[n // 2]) / 2 if n % 2 == 0
                       else sorted_returns[n // 2])
    n_up   = sum(1 for r in returns if r > 0)
    n_down = len(returns) - n_up
    top3   = records[:3]
    bot3   = records[-3:][::-1]

    if median_ret > 0.03:
        character = f"Broadly bullish: {n_up}/{len(records)} tickers rose, median {median_ret:+.1%}."
    elif median_ret < -0.03:
        character = f"Broadly bearish: {n_down}/{len(records)} tickers fell, median {median_ret:+.1%}."
    else:
        character = f"Mixed/flat: {n_up}/{len(records)} tickers rose, median {median_ret:+.1%}."

    lines = [
        "# Sector Trend Summary",
        "",
        f"**Window**: {backtest_start} → {backtest_end} | **Tickers**: {len(records)}",
        f"**Median return**: {median_ret:+.1%} | **Up**: {n_up} | **Down**: {n_down}",
        "",
        f"**Top gainers**: " + ", ".join(f"{t} ({r:+.1%})" for t, r in top3),
        f"**Bottom losers**: " + ", ".join(f"{t} ({r:+.1%})" for t, r in bot3),
        "",
        f"**Sector character**: {character}",
    ]
    with open("data_trend.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("data_trend.md written")


def process_ticker(ticker: str) -> bool:
    """Download, resample, validate, and cache one ticker. Returns True on success."""
    path = os.path.join(CACHE_DIR, f"{ticker}.parquet")
    if os.path.exists(path):
        print(f"  {ticker}: already cached, skipping")
        return True
    df_hourly = download_ticker(ticker)
    if df_hourly.empty:
        print(f"  {ticker}: no data returned — skipping")
        return False
    df_daily = resample_to_daily(df_hourly)
    validate_ticker_data(ticker, df_daily, BACKTEST_START)
    os.makedirs(CACHE_DIR, exist_ok=True)
    df_daily.to_parquet(path)
    print(f"  {ticker}: saved {len(df_daily)} days -> {path}")
    return True


if __name__ == "__main__":
    if not TICKERS:
        print("ERROR: TICKERS list is empty. Edit prepare.py and add ticker symbols before running.")
        sys.exit(1)
    os.makedirs(CACHE_DIR, exist_ok=True)
    print(f"Downloading {len(TICKERS)} tickers -> {CACHE_DIR}")
    print(f"Date range: {HISTORY_START} -> {BACKTEST_END} (1h bars, resampled to daily)")
    ok = 0
    for ticker in TICKERS:
        if process_ticker(ticker):
            ok += 1
    print(f"\nDone: {ok}/{len(TICKERS)} tickers cached successfully.")
    write_trend_summary(TICKERS, BACKTEST_START, BACKTEST_END, CACHE_DIR)
