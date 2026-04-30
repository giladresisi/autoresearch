"""prepare_futures_1m.py — Download MNQ/MES 1m futures bars from Databento.

Usage:
    uv run prepare_futures_1m.py

Downloads from Databento (GLBX.MDP3, MNQ.v.0/MES.v.0) to data/.
Requires DATABENTO_API_KEY in environment (or .env file).
Running again skips tickers whose file already exists (idempotent).
"""
import os
import sys
from pathlib import Path

import pandas as pd

# Load .env before importing DatabentSource (which reads DATABENTO_API_KEY at init)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

from data.sources import DatabentSource

# ── USER CONFIGURATION ──────────────────────────────────────────────────────
TICKERS = ["MNQ", "MES"]

BACKTEST_START = "2024-01-01"
BACKTEST_END   = "2026-04-15"

INTERVAL = "1m"

# Unified bar data store (also read by signal_smt and backtest)
HISTORICAL_DATA_DIR = Path("data")

# Databento continuous front-month symbols (volume-roll, stype_in="continuous")
DATABENTO_SYMBOLS = {
    "MNQ": "MNQ.v.0",
    "MES": "MES.v.0",
}
# ────────────────────────────────────────────────────────────────────────────


def process_ticker(ticker: str) -> bool:
    """Fetch and save 1m futures bars. Returns True on success."""
    historical_path = HISTORICAL_DATA_DIR / f"{ticker}_1m.parquet"
    if historical_path.exists():
        print(f"  {ticker}: found at {historical_path}, skipping download")
        return True

    HISTORICAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        source = DatabentSource()
    except RuntimeError as exc:
        print(f"  {ticker}: Databento init failed — {exc}", file=sys.stderr)
        return False

    db_ticker = DATABENTO_SYMBOLS.get(ticker)
    if db_ticker is None:
        print(f"  {ticker}: no Databento symbol mapping found", file=sys.stderr)
        return False

    print(f"  {ticker}: downloading {BACKTEST_START} to {BACKTEST_END} at 1m ...")
    df = source.fetch(db_ticker, BACKTEST_START, BACKTEST_END, INTERVAL)
    if df is None or df.empty:
        print(f"  {ticker}: no data returned from Databento", file=sys.stderr)
        return False

    df.to_parquet(historical_path)
    print(f"  {ticker}: saved {len(df):,} bars to {historical_path}")
    return True


if __name__ == "__main__":
    print(f"Downloading 1m futures data to {HISTORICAL_DATA_DIR}")
    results = [process_ticker(ticker) for ticker in TICKERS]
    if not all(results):
        print("Some tickers failed. Check DATABENTO_API_KEY and network connectivity.", file=sys.stderr)
        sys.exit(1)
    print("Done.")
