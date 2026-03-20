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
TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "TSLA"]

BACKTEST_START = "2026-01-01"  # first day of the backtest window (inclusive)
BACKTEST_END   = "2026-03-01"  # last day of the backtest window (exclusive)
# ────────────────────────────────────────────────────────────────────────────

# Derived (do not modify)
HISTORY_START = (pd.Timestamp(BACKTEST_START) - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")


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
