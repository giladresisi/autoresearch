"""
screener.py — Pre-market BUY signal scanner.

Reads all parquets from SCREENER_CACHE_DIR, fetches pre-market last_price from
yfinance for each ticker, appends a synthetic today row, and calls screen_day()
from train.py. Prints armed candidates sorted by prev_vol_ratio descending.

Gap filter: candidates with gap_pct < GAP_THRESHOLD are shown but NOT armed.
Set GAP_THRESHOLD after running analyze_gaps.py.

Usage:
    uv run screener.py

Override cache path:
    AUTORESEARCH_SCREENER_CACHE_DIR=/path/to/dir uv run screener.py
"""
import datetime
import os
import warnings

import pandas as pd
import yfinance as yf

from screener_prepare import SCREENER_CACHE_DIR
from train import screen_day

# Update this after running analyze_gaps.py to calibrate the gap filter
GAP_THRESHOLD = -0.03  # -3%: candidates with gap < this are flagged but not armed


def _fetch_last_price(ticker: str) -> float | None:
    """Fetch pre-market / last known price from yfinance fast_info."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            info = yf.Ticker(ticker).fast_info
            price = info.get("last_price", None)
            if price is not None and not pd.isna(price) and float(price) > 0:
                return float(price)
    except Exception:
        pass
    return None


def _check_staleness(parquet_paths: list) -> None:
    """Warn if the newest parquet last-row date is > 2 calendar days before today."""
    if not parquet_paths:
        return
    today = datetime.date.today()
    max_date = None
    for path in parquet_paths:
        try:
            df = pd.read_parquet(path)
            if df.empty:
                continue
            last = df.index[-1]
            if hasattr(last, "date"):
                last = last.date()
            if max_date is None or last > max_date:
                max_date = last
        except Exception:
            continue
    if max_date is not None:
        age = (today - max_date).days
        if age > 2:
            print(
                f"WARNING: screener cache is stale -- newest data is {age} days old "
                f"(last row: {max_date}). Run screener_prepare.py to refresh."
            )


def run_screener() -> None:
    """Load cached parquets, fetch live prices, run screen_day(), print results."""
    today = datetime.date.today()

    # Collect all parquet files from SCREENER_CACHE_DIR
    if not os.path.isdir(SCREENER_CACHE_DIR):
        print(f"No tickers in cache -- run screener_prepare.py first (dir: {SCREENER_CACHE_DIR})")
        return

    parquet_paths = [
        os.path.join(SCREENER_CACHE_DIR, f)
        for f in os.listdir(SCREENER_CACHE_DIR)
        if f.endswith(".parquet")
    ]

    if not parquet_paths:
        print(f"No tickers in cache -- run screener_prepare.py first (dir: {SCREENER_CACHE_DIR})")
        return

    _check_staleness(parquet_paths)

    armed = []
    gap_skipped = []

    for path in parquet_paths:
        ticker = os.path.splitext(os.path.basename(path))[0]
        try:
            df = pd.read_parquet(path)
            if df.empty or len(df) < 102:
                continue

            prev_close = float(df["close"].iloc[-1])

            # Fetch live / pre-market price; fall back to last close if unavailable
            current_price = _fetch_last_price(ticker)
            if current_price is None:
                current_price = prev_close

            # Pre-market gap vs prior close
            gap_pct = (current_price - prev_close) / prev_close if prev_close != 0 else 0.0

            # days_to_earnings: read from the row before the synthetic today row
            days_to_earnings = None
            if "next_earnings_date" in df.columns:
                ned = df["next_earnings_date"].iloc[-1]
                if pd.notna(ned):
                    ned_date = ned.date() if hasattr(ned, "date") else ned
                    days_to_earnings = (ned_date - today).days

            # Append synthetic today row so screen_day() sees today's data
            synthetic_row = pd.DataFrame(
                [{
                    "open":          current_price,
                    "high":          current_price,
                    "low":           current_price,
                    "close":         current_price,
                    "volume":        0,
                    "price_1030am":  current_price,
                }],
                index=pd.Index([today], name="date"),
            )
            # Preserve any extra columns that might exist in df
            for col in df.columns:
                if col not in synthetic_row.columns:
                    synthetic_row[col] = df[col].iloc[-1]

            df_extended = pd.concat([df, synthetic_row])

            signal = screen_day(df_extended, today, current_price=current_price)
            if signal is None:
                continue

            row = {
                "ticker":          ticker,
                "current_price":   current_price,
                "entry_threshold": round(signal["high20"] + 0.01, 2),
                "suggested_stop":  round(signal["stop"], 2),
                "atr14":           signal["atr14"],
                "rsi14":           signal["rsi14"],
                "prev_vol_ratio":  signal["prev_vol_ratio"],
                "vol_trend_ratio": signal["vol_trend_ratio"],
                "gap_pct":         round(gap_pct, 4),
                "res_atr":         signal["res_atr"],
                "days_to_earnings": days_to_earnings,
            }

            if gap_pct < GAP_THRESHOLD:
                gap_skipped.append(row)
            else:
                armed.append(row)

        except Exception:
            continue

    # Sort armed list by prev_vol_ratio descending
    armed.sort(key=lambda r: r["prev_vol_ratio"], reverse=True)

    # Print armed candidates table
    header = (
        f"{'TICKER':<7} {'PRICE':>8} {'ENTRY_THR':>10} {'STOP':>8} "
        f"{'ATR14':>7} {'RSI14':>6} {'VOL_PREV':>9} {'VOL_TRD':>8} "
        f"{'GAP%':>7} {'RES_ATR':>8} {'EARN_DAYS':>10}"
    )
    sep = "-" * len(header)

    if armed:
        print(f"\n=== ARMED BUY SIGNALS ({len(armed)}) ===")
        print(header)
        print(sep)
        for r in armed:
            res_atr_str = f"{r['res_atr']:>8.2f}" if r["res_atr"] is not None else f"{'None':>8}"
            earn_str = f"{r['days_to_earnings']:>10d}" if r["days_to_earnings"] is not None else f"{'None':>10}"
            print(
                f"{r['ticker']:<7} {r['current_price']:>8.2f} {r['entry_threshold']:>10.2f} "
                f"{r['suggested_stop']:>8.2f} {r['atr14']:>7.4f} {r['rsi14']:>6.2f} "
                f"{r['prev_vol_ratio']:>9.4f} {r['vol_trend_ratio']:>8.4f} "
                f"{r['gap_pct']*100:>6.2f}% {res_atr_str} {earn_str}"
            )
    else:
        print("\nNo armed BUY signals found.")

    # Print gap-skipped candidates separately
    if gap_skipped:
        print(f"\n=== SKIPPED (gap < {GAP_THRESHOLD*100:.0f}%) ({len(gap_skipped)}) ===")
        print(header)
        print(sep)
        for r in gap_skipped:
            res_atr_str = f"{r['res_atr']:>8.2f}" if r["res_atr"] is not None else f"{'None':>8}"
            earn_str = f"{r['days_to_earnings']:>10d}" if r["days_to_earnings"] is not None else f"{'None':>10}"
            print(
                f"{r['ticker']:<7} {r['current_price']:>8.2f} {r['entry_threshold']:>10.2f} "
                f"{r['suggested_stop']:>8.2f} {r['atr14']:>7.4f} {r['rsi14']:>6.2f} "
                f"{r['prev_vol_ratio']:>9.4f} {r['vol_trend_ratio']:>8.4f} "
                f"{r['gap_pct']*100:>6.2f}% {res_atr_str} {earn_str}"
            )


if __name__ == "__main__":
    run_screener()
