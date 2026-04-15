"""
position_monitor.py — RAISE-STOP signal scanner for open positions.

Reads portfolio.json, loads each ticker's parquet from SCREENER_CACHE_DIR,
appends a synthetic today row with current price, and calls manage_position()
from train.py. Prints signals where new_stop > current stop_price.

No writes -- user updates portfolio.json manually after confirming.

Usage:
    uv run position_monitor.py [--portfolio portfolio.json]
"""
import argparse
import datetime
import json
import os
import warnings

import pandas as pd
try:
    import yfinance as yf
except ImportError:
    import types as _types
    yf = _types.SimpleNamespace(Ticker=None)  # type: ignore[assignment]

from screener_prepare import SCREENER_CACHE_DIR
from train import manage_position


def _fetch_last_price(ticker: str) -> float | None:
    """Fetch pre-market / last known price via yfinance fast_info."""
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


def _infer_reason(new_stop: float, entry_price: float, current_price: float, atr: float) -> str:
    """
    Infer the reason for a stop raise from the magnitude of the move.
    This is a display heuristic -- manage_position() only returns the new stop value.
    """
    if current_price >= entry_price + 2.0 * atr:
        return "trail"
    if new_stop >= entry_price:
        return "breakeven"
    return "time/stall"


def run_monitor(portfolio_path: str) -> None:
    """Load portfolio.json, check each position for stop-raise signals."""
    today = datetime.date.today()

    # Load portfolio
    if not os.path.exists(portfolio_path):
        print(f"Portfolio file not found: {portfolio_path}")
        return

    with open(portfolio_path) as f:
        portfolio = json.load(f)

    positions = portfolio.get("positions", [])
    n = len(positions)
    signals = []

    for pos_data in positions:
        ticker = pos_data["ticker"]
        parquet_path = os.path.join(SCREENER_CACHE_DIR, f"{ticker}.parquet")

        if not os.path.exists(parquet_path):
            print(f"WARNING: {ticker} not in SCREENER_CACHE_DIR -- skipping")
            continue

        try:
            df = pd.read_parquet(parquet_path)
            if df.empty:
                print(f"WARNING: {ticker} parquet is empty -- skipping")
                continue

            prev_close = float(df["close"].iloc[-1])

            # Fetch live price; fall back to parquet close
            current_price = _fetch_last_price(ticker)
            if current_price is None:
                current_price = prev_close

            # Append synthetic today row
            synthetic_row = pd.DataFrame(
                [{
                    "open":         current_price,
                    "high":         current_price,
                    "low":          current_price,
                    "close":        current_price,
                    "volume":       0,
                    "price_1030am": current_price,
                }],
                index=pd.Index([today], name="date"),
            )
            for col in df.columns:
                if col not in synthetic_row.columns:
                    synthetic_row[col] = df[col].iloc[-1]

            df_extended = pd.concat([df, synthetic_row])

            # Build position dict for manage_position()
            entry_date = datetime.date.fromisoformat(pos_data["entry_date"])
            pos = {
                "entry_price": float(pos_data["entry_price"]),
                "entry_date":  entry_date,
                "stop_price":  float(pos_data["stop_price"]),
                "shares":      int(pos_data["shares"]),
            }

            new_stop = manage_position(pos, df_extended)

            if new_stop > pos["stop_price"]:
                # Estimate ATR for reason inference (rough: use last ATR from manage_position logic)
                from train import calc_atr14
                atr_series = calc_atr14(df_extended)
                atr = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 1.0
                reason = _infer_reason(new_stop, pos["entry_price"], current_price, atr)
                signals.append({
                    "ticker":        ticker,
                    "current_price": current_price,
                    "old_stop":      pos["stop_price"],
                    "new_stop":      round(new_stop, 2),
                    "delta":         round(new_stop - pos["stop_price"], 2),
                    "reason":        reason,
                })

        except Exception as e:
            print(f"WARNING: error processing {ticker}: {e}")
            continue

    if signals:
        for s in signals:
            print(
                f"RAISE-STOP  {s['ticker']}  current=${s['current_price']:.2f}  "
                f"stop: ${s['old_stop']:.2f} -> ${s['new_stop']:.2f}  "
                f"(+${s['delta']:.2f})  reason: {s['reason']}"
            )
    else:
        unit = "position" if n == 1 else "positions"
        print(f"No stop-raise signals for {n} open {unit}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAISE-STOP signal scanner")
    parser.add_argument(
        "--portfolio",
        default=os.environ.get("PORTFOLIO_PATH", "portfolio.json"),
        help="Path to portfolio.json (default: portfolio.json)",
    )
    args = parser.parse_args()
    run_monitor(args.portfolio)
