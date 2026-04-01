"""prepare_futures.py — Download MNQ/MES 1m futures bars from IB-Gateway.

Usage:
    uv run prepare_futures.py

Requires IB-Gateway running on localhost:4002.
Data cached to ~/.cache/autoresearch/futures_data/1m/.
Running again skips tickers whose file already exists (idempotent).

IB LIMITATION — 1m ContFuture historical data:
IB rejects explicit endDateTime for ContFuture contracts (error 10339), so only
the most recent 29 calendar days of 1m data are available.  BACKTEST_START and
BACKTEST_END below are computed dynamically so the downloaded parquet always
contains valid data.  Delete the cached parquets to re-download with updated dates.

To backtest over a longer window, supply pre-downloaded parquet files in the
cache directory and update BACKTEST_START/BACKTEST_END in train_smt.py manually.
"""
import datetime
import json
import os
import sys
from pathlib import Path


import pandas as pd

from data.sources import IBGatewaySource

# ── USER CONFIGURATION ──────────────────────────────────────────────────────
# IB ContFuture symbols (without the TradingView 1! suffix)
TICKERS = ["MNQ", "MES"]

# Dynamic dates: IB only allows endDateTime='' for ContFuture 1m bars (error 10339 for
# explicit dates). Requests >7 calendar days reliably timeout. BACKTEST_START and
# BACKTEST_END are computed to match the 7-day fetch window so the parquet and manifest
# are always consistent with what was actually downloaded.
_TODAY          = datetime.date.today()
BACKTEST_END    = _TODAY.isoformat()
BACKTEST_START  = (_TODAY - datetime.timedelta(days=7)).isoformat()
# ────────────────────────────────────────────────────────────────────────────

# No warmup needed — SMT strategy uses no daily SMAs
HISTORY_START = BACKTEST_START

INTERVAL = "1m"

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
    """Fetch and cache 1m bars for one futures ticker. Returns True on success."""
    out_path = Path(CACHE_DIR) / INTERVAL / f"{ticker}.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        print(f"  {ticker}: already cached, skipping")
        return True
    # Use high client_ids (30+) to avoid collisions with test suite (clientId=1)
    # and any lingering connections from previous runs.
    client_id = 30 + TICKERS.index(ticker)
    source = IBGatewaySource(host=IB_HOST, port=IB_PORT, client_id=client_id)
    df = source.fetch(ticker, HISTORY_START, BACKTEST_END, INTERVAL, contract_type="contfuture")
    if df is None or df.empty:
        print(f"  {ticker}: no data returned")
        return False
    df.to_parquet(out_path)
    print(f"  {ticker}: saved {len(df)} bars to {out_path}")
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
                "source": "ib",
            },
            f,
            indent=2,
        )
    print(f"Manifest written to {manifest_path}")


if __name__ == "__main__":
    print(f"Downloading futures data to {CACHE_DIR}")
    # ib_insync uses asyncio (eventkit) which requires an event loop in the calling thread.
    # ThreadPoolExecutor worker threads have no event loop, so fetch sequentially.
    results = [process_ticker(ticker) for ticker in TICKERS]
    if not all(results):
        print("Some tickers failed. Check IB-Gateway connection.", file=sys.stderr)
        sys.exit(1)
    write_manifest()
    print("Done.")
