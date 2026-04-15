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
import sys
import warnings

import pandas as pd
try:
    import yfinance as yf
except ImportError:
    import types as _types
    yf = _types.SimpleNamespace(Ticker=None)  # type: ignore[assignment]

from screener_prepare import SCREENER_CACHE_DIR
from train import (
    screen_day, calc_atr14, calc_rsi14,
    is_stalling_at_ceiling, find_stop_price, nearest_resistance_atr,
)

# Update this after running analyze_gaps.py to calibrate the gap filter
GAP_THRESHOLD = -0.03  # -3%: candidates with gap < this are flagged but not armed

# Ordered list of rejection rule labels (matches screen_day filter chain).
_RULES = [
    "too_few_rows", "no_price", "earnings_soon",
    "sma_misaligned", "death_cross", "sma20_declining",
    "vol_trend_low", "prev_vol_dead",
    "no_breakout", "no_breakout_cont",
    "rsi_out_of_range", "ceiling_stall",
    "no_pivot_stop", "resistance_too_close",
]


def _rejection_reason(df: pd.DataFrame, today, current_price: float) -> str:
    """
    Mirror screen_day's filter chain and return the label of the first rule that rejects.
    Returns "unknown" if no rule fires (should not happen on a ticker that returned None).
    """
    df = df.loc[:today]
    if len(df) < 102:
        return "too_few_rows"

    price_1030am = current_price
    if pd.isna(price_1030am):
        return "no_price"

    if "next_earnings_date" in df.columns:
        ned = df["next_earnings_date"].iloc[-1]
        if pd.notna(ned):
            days_to_earnings = (ned - today).days
            if 0 <= days_to_earnings <= 14:
                return "earnings_soon"

    hist = df.iloc[:-1]
    close_hist = hist["close"]

    sma20  = float(close_hist.iloc[-20:].mean())
    sma50  = float(close_hist.iloc[-50:].mean())
    sma100 = float(close_hist.iloc[-100:].mean())
    if pd.isna(sma20) or pd.isna(sma50) or pd.isna(sma100):
        return "sma_misaligned"

    # Mirror screen_day's two-path check so rejection diagnosis matches the actual gate that fired.
    sma200 = float(close_hist.iloc[-200:].mean()) if len(hist) >= 200 else None
    bull_path = (
        price_1030am > sma50 and
        price_1030am > sma100 and
        sma20 > sma50 and
        sma50 > sma100
    )
    recovery_path = (
        sma200 is not None and
        sma50 > sma200 and
        price_1030am <= sma50 and
        price_1030am > sma20
    )
    if not bull_path and not recovery_path:
        # Both paths blocked: return the most specific alignment label.
        if sma200 is not None and sma50 <= sma200:
            return "death_cross"
        return "sma_misaligned"
    signal_path = "bull" if bull_path else "recovery"

    sma20_5d_ago = float(close_hist.iloc[-25:-5].mean())
    # Use the same slope tolerance as screen_day to avoid misattributing recovery rejections.
    slope_floor = 0.990 if signal_path == "recovery" else 0.995
    if sma20 < sma20_5d_ago * slope_floor:
        return "sma20_declining"

    vm30 = float(hist["volume"].iloc[-30:].mean())
    if pd.isna(vm30) or vm30 == 0:
        return "vol_trend_low"
    vol_trend_ratio = float(hist["volume"].iloc[-5:].mean()) / vm30
    prev_vol_ratio  = float(hist["volume"].iloc[-1]) / vm30
    if vol_trend_ratio < 1.0:
        return "vol_trend_low"
    if prev_vol_ratio < 0.8:
        return "prev_vol_dead"

    high20 = float(close_hist.iloc[-20:].max())
    if price_1030am <= high20:
        return "no_breakout"

    prev_high = float(hist["high"].iloc[-1])
    if price_1030am <= prev_high:
        return "no_breakout_cont"

    atr = float(calc_atr14(hist.iloc[-30:]).iloc[-1])
    rsi = float(calc_rsi14(hist.iloc[-60:]).iloc[-1])
    if pd.isna(atr) or atr == 0 or pd.isna(rsi):
        return "rsi_out_of_range"
    # Use path-aware RSI range matching screen_day.
    rsi_lo, rsi_hi = (40, 65) if signal_path == "recovery" else (50, 75)
    if not (rsi_lo <= rsi <= rsi_hi):
        return "rsi_out_of_range"

    if is_stalling_at_ceiling(hist):
        return "ceiling_stall"

    stop = find_stop_price(hist, price_1030am, atr)
    if stop is None:
        return "no_pivot_stop"

    res_atr = nearest_resistance_atr(hist, price_1030am, atr)
    if res_atr is not None and res_atr < 2.0:
        return "resistance_too_close"

    return "unknown"


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
    # Diagnostic counters — tallied in the main loop below via _rejection_reason()
    rejection_counts = {r: 0 for r in _RULES}
    total_checked = 0

    for path in parquet_paths:
        ticker = os.path.splitext(os.path.basename(path))[0]
        try:
            df = pd.read_parquet(path)
            total_checked += 1
            if df.empty or len(df) < 102:
                rejection_counts["too_few_rows"] += 1
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
                reason = _rejection_reason(df_extended, today, current_price)
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
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
                # signal_path distinguishes bull (price>SMA50) from recovery (price<=SMA50, SMA50>SMA200)
                "signal_path":     signal.get("signal_path", "bull"),
            }

            if gap_pct < GAP_THRESHOLD:
                gap_skipped.append(row)
            else:
                armed.append(row)

        except Exception:
            continue

    # Sort armed list by prev_vol_ratio descending
    armed.sort(key=lambda r: r["prev_vol_ratio"], reverse=True)

    # Print rejection breakdown when --diagnose is passed or no signals fire
    diagnose = "--diagnose" in sys.argv
    if diagnose or not armed:
        print(f"\n=== REJECTION BREAKDOWN ({total_checked} tickers evaluated) ===")
        sorted_reasons = sorted(rejection_counts.items(), key=lambda x: x[1], reverse=True)
        for rule, count in sorted_reasons:
            if count == 0:
                continue
            pct = 100 * count / total_checked if total_checked else 0
            print(f"  {rule:<25}: {count:>5}  ({pct:.1f}%)")

    # Print armed candidates table
    header = (
        f"{'TICKER':<7} {'PATH':<6} {'PRICE':>8} {'ENTRY_THR':>10} {'STOP':>8} "
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
                f"{r['ticker']:<7} {r['signal_path']:<6} {r['current_price']:>8.2f} {r['entry_threshold']:>10.2f} "
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
                f"{r['ticker']:<7} {r['signal_path']:<6} {r['current_price']:>8.2f} {r['entry_threshold']:>10.2f} "
                f"{r['suggested_stop']:>8.2f} {r['atr14']:>7.4f} {r['rsi14']:>6.2f} "
                f"{r['prev_vol_ratio']:>9.4f} {r['vol_trend_ratio']:>8.4f} "
                f"{r['gap_pct']*100:>6.2f}% {res_atr_str} {earn_str}"
            )


if __name__ == "__main__":
    run_screener()
