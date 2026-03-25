"""
screener_prepare.py — Build and refresh the screener parquet cache.

Downloads last 90 days of 1h OHLCV from yfinance for the live ticker universe
(S&P 500 + Russell 1000) and resamples to daily using prepare.resample_to_daily().
Idempotent: skips tickers whose parquet already exists and is current (last row = yesterday).

Usage:
    uv run screener_prepare.py

Override cache path:
    AUTORESEARCH_SCREENER_CACHE_DIR=/path/to/dir uv run screener_prepare.py
"""
import datetime
import io
import os
import warnings

import pandas as pd
import yfinance as yf

from prepare import TICKERS as PREPARE_TICKERS, resample_to_daily

# Isolated from the harness cache so screener universe can grow independently
SCREENER_CACHE_DIR = os.environ.get(
    "AUTORESEARCH_SCREENER_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "screener_data"),
)
HISTORY_DAYS = 90  # calendar days of data to maintain in the screener cache


def fetch_screener_universe() -> list:
    """
    Fetch current S&P 500 (Wikipedia) + Russell 1000 (iShares IWB holdings).
    Falls back to prepare.py TICKERS list if both sources fail.
    Returns a deduplicated list of ticker strings.
    """
    import requests
    from bs4 import BeautifulSoup

    headers = {"User-Agent": "Mozilla/5.0"}
    tickers = []

    # S&P 500 from Wikipedia
    try:
        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers=headers,
            timeout=15,
        )
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", {"id": "constituents"})
        rows = table.find_all("tr")[1:]
        sp500 = [row.find_all("td")[0].text.strip().replace(".", "-") for row in rows]
        tickers += sp500
        print(f"  S&P 500:      {len(sp500)} tickers")
    except Exception as e:
        print(f"  S&P 500 fetch failed: {e}")

    # Russell 1000 from iShares IWB CSV
    try:
        url = (
            "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
            "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
        )
        r = requests.get(url, headers=headers, timeout=15)
        lines = r.text.splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("Ticker"))
        df = pd.read_csv(io.StringIO("\n".join(lines[start:])))
        df = df[df["Asset Class"] == "Equity"]
        r1000 = df["Ticker"].dropna().str.strip().tolist()
        r1000 = [t for t in r1000 if t.replace("-", "").isalpha() and len(t) <= 5]
        tickers += r1000
        print(f"  Russell 1000: {len(r1000)} tickers")
    except Exception as e:
        print(f"  Russell 1000 fetch failed: {e}")

    # Deduplicate, preserve order
    seen, unique = set(), []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    if not unique:
        print("  WARNING: all sources failed, using prepare.py TICKERS fallback")
        unique = list(PREPARE_TICKERS)

    return unique


def is_ticker_current(ticker: str) -> bool:
    """Return True if cache parquet exists and last row is from yesterday or later."""
    path = os.path.join(SCREENER_CACHE_DIR, f"{ticker}.parquet")
    if not os.path.exists(path):
        return False
    try:
        df = pd.read_parquet(path)
        if df.empty:
            return False
        last_date = df.index[-1]
        # Convert to date if it's a Timestamp
        if hasattr(last_date, "date"):
            last_date = last_date.date()
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        return last_date >= yesterday
    except Exception:
        return False


def download_and_cache(ticker: str, history_start: str) -> None:
    """Download hourly OHLCV, resample to daily, merge with existing parquet, write back.

    Incremental: if a parquet already exists, fetches only from (last_date - 1 day) forward
    and merges with the existing data, deduplicating on date index (newest wins). Falls back
    to a full history_start download if the existing parquet cannot be read.
    """
    path = os.path.join(SCREENER_CACHE_DIR, f"{ticker}.parquet")
    try:
        # Load existing data and determine fetch start for incremental update
        existing_df = None
        fetch_start = history_start
        if os.path.exists(path):
            try:
                existing_df = pd.read_parquet(path)
                if not existing_df.empty:
                    last_date = existing_df.index[-1]
                    if hasattr(last_date, "date"):
                        last_date = last_date.date()
                    # Overlap by 1 day so partial last-day data gets refreshed
                    fetch_start = (last_date - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            except Exception:
                existing_df = None
                fetch_start = history_start

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            obj = yf.Ticker(ticker)
            df_h = obj.history(
                start=fetch_start,
                interval="1h",
                auto_adjust=True,
                prepost=False,
            )
        if df_h is None or df_h.empty:
            return
        new_daily = resample_to_daily(df_h)
        if new_daily.empty:
            return

        if existing_df is not None and not existing_df.empty:
            # Merge old + new; keep newest data for any overlapping dates
            combined = pd.concat([existing_df, new_daily])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined = combined.sort_index()
            combined.to_parquet(path)
        else:
            new_daily.to_parquet(path)
    except Exception:
        return


if __name__ == "__main__":
    today = datetime.date.today()
    history_start = (today - datetime.timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")

    print("Fetching screener universe...")
    universe = fetch_screener_universe()
    print(f"  Total universe: {len(universe)} tickers")

    os.makedirs(SCREENER_CACHE_DIR, exist_ok=True)

    cached = skipped = failed = 0
    total = len(universe)
    for i, ticker in enumerate(universe, 1):
        if is_ticker_current(ticker):
            print(f"  [{i:4d}/{total}] {ticker:<6} -- SKIP (current)")
            skipped += 1
            continue
        # Download and cache this ticker
        path = os.path.join(SCREENER_CACHE_DIR, f"{ticker}.parquet")
        mtime_before = os.path.getmtime(path) if os.path.exists(path) else None
        download_and_cache(ticker, history_start)
        # Verify the file was actually written/updated (new or mtime changed)
        if os.path.exists(path) and (mtime_before is None or os.path.getmtime(path) > mtime_before):
            # Check if file was actually written/updated
            try:
                df_check = pd.read_parquet(path)
                if not df_check.empty:
                    print(f"  [{i:4d}/{total}] {ticker:<6} -- cached ({len(df_check)} rows)")
                    cached += 1
                    continue
            except Exception:
                pass
        print(f"  [{i:4d}/{total}] {ticker:<6} -- FAIL (error)")
        failed += 1

    print(f"\nDone. cached={cached}  skipped={skipped}  failed={failed}")
