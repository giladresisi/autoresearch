"""prepare_futures.py — Download MNQ/MES 5m futures bars from Databento.

Usage:
    uv run prepare_futures.py

Downloads from Databento (GLBX.MDP3, MNQ.c.0/MES.c.0) to data/.
Requires DATABENTO_API_KEY in environment (or .env file).
Running again skips tickers whose file already exists (idempotent).

Lookup priority per ticker:
  1. data/{ticker}.parquet             — Databento permanent store (2024-01-01 Databento window)
  2. {CACHE_DIR}/5m/{ticker}.parquet   — IB ephemeral cache (legacy fallback)
  3. Live Databento download
"""
import datetime
import json
import os
import sys
from pathlib import Path


import pandas as pd

from data.sources import IBGatewaySource, DatabentSource

# ── USER CONFIGURATION ──────────────────────────────────────────────────────
TICKERS = ["MNQ", "MES"]

# IB conIds for the active quarterly contracts.
# MNQM6/MESM6 expire 2026-06-18 — update to MNQU6/MESU6 (793356225/793356217) after rollover.
CONIDS = {
    "MNQ": "770561201",   # MNQM6 (Jun 2026)
    "MES": "770561194",   # MESM6 (Jun 2026)
}

BACKTEST_START = "2024-01-01"
_TODAY         = datetime.date.today()
BACKTEST_END   = _TODAY.isoformat()

# Databento permanent store — survives cache clears
HISTORICAL_DATA_DIR = Path("data")

# Databento continuous front-month symbols (volume-roll, stype_in="continuous")
DATABENTO_SYMBOLS = {
    "MNQ": "MNQ.v.0",
    "MES": "MES.v.0",
}
# ────────────────────────────────────────────────────────────────────────────

# No warmup needed — SMT strategy uses no daily SMAs
HISTORY_START = BACKTEST_START

INTERVAL = "5m"

# Cache directory: separate from equity cache to avoid collisions
CACHE_DIR = os.environ.get(
    "FUTURES_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "futures_data"),
)

IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", "4002"))

# Only 2 tickers; sequential is fine but mirror prepare.py's threaded pattern
MAX_WORKERS = 2


def process_ticker(ticker: str) -> bool:
    """Fetch and cache futures bars. Returns True on success.

    Priority:
      1. data/{ticker}.parquet             — Databento permanent store
      2. {CACHE_DIR}/5m/{ticker}.parquet   — IB ephemeral cache
      3. Live Databento download
    """
    # Level 1: Databento permanent store
    historical_path = HISTORICAL_DATA_DIR / f"{ticker}.parquet"
    if historical_path.exists():
        print(f"  {ticker}: found in data/, skipping download")
        return True

    # Level 2: IB ephemeral cache (legacy data from previous runs)
    ib_path = Path(CACHE_DIR) / INTERVAL / f"{ticker}.parquet"
    if ib_path.exists():
        print(f"  {ticker}: found in IB cache ({ib_path}), skipping download")
        return True

    # Level 3: Download from Databento
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
    df = source.fetch(db_ticker, HISTORY_START, BACKTEST_END, INTERVAL)
    if df is None or df.empty:
        print(f"  {ticker}: no data returned from Databento", file=sys.stderr)
        return False

    df.to_parquet(historical_path)
    print(f"  {ticker}: saved {len(df)} bars to {historical_path}")
    return True


def write_manifest() -> None:
    """Write futures_manifest.json to CACHE_DIR root."""
    manifest_path = Path(CACHE_DIR) / "futures_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "tickers": TICKERS,
                "backtest_start": BACKTEST_START,
                "backtest_end": BACKTEST_END,
                "fetch_interval": INTERVAL,
                "source": "databento",
                "databento_symbols": DATABENTO_SYMBOLS,
            },
            f,
            indent=2,
        )
    print(f"Manifest written to {manifest_path}")


if __name__ == "__main__":
    print(f"Downloading futures data to {HISTORICAL_DATA_DIR}")
    # ib_insync uses asyncio (eventkit) which requires an event loop in the calling thread.
    # ThreadPoolExecutor worker threads have no event loop, so fetch sequentially.
    results = [process_ticker(ticker) for ticker in TICKERS]
    if not all(results):
        print("Some tickers failed. Check DATABENTO_API_KEY and network connectivity.", file=sys.stderr)
        sys.exit(1)
    write_manifest()
    print("Done.")
