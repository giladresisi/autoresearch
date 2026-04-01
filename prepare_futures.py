"""prepare_futures.py — Download MNQ/MES 5m futures bars from IB-Gateway.

Usage:
    uv run prepare_futures.py

Requires IB-Gateway running on localhost:4002.
Data cached to ~/.cache/autoresearch/futures_data/5m/.
Running again skips tickers whose file already exists (idempotent).

Uses specific quarterly contracts (MNQM6/MESM6) identified by IB conId with explicit
`endDateTime` pagination, giving ~6.5 months of 5m history (vs 45 days for ContFuture).
MNQM6/MESM6 expire 2026-06-18 — update CONIDS to MNQU6/MESU6 after rollover.
"""
import datetime
import json
import os
import sys
from pathlib import Path


import pandas as pd

from data.sources import IBGatewaySource

# ── USER CONFIGURATION ──────────────────────────────────────────────────────
TICKERS = ["MNQ", "MES"]

# IB conIds for the active quarterly contracts.
# MNQM6/MESM6 expire 2026-06-18 — update to MNQU6/MESU6 (793356225/793356217) after rollover.
CONIDS = {
    "MNQ": "770561201",   # MNQM6 (Jun 2026)
    "MES": "770561194",   # MESM6 (Jun 2026)
}

# Specific quarterly contracts allow explicit endDateTime — no 45-day cap.
# BACKTEST_START must be >= 2025-09-24 (earliest reliable bar in MNQM6/MESM6).
# Jun–Aug 2025 is thin (contract newly listed, not front-month) and Sep 1-23 is a
# known IB data gap — both periods produce 0 SMT signals in practice.
BACKTEST_START = "2025-09-24"
_TODAY         = datetime.date.today()
BACKTEST_END   = _TODAY.isoformat()
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
    """Fetch and cache futures bars at INTERVAL resolution for one ticker. Returns True on success."""
    out_path = Path(CACHE_DIR) / INTERVAL / f"{ticker}.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        print(f"  {ticker}: already cached, skipping")
        return True
    # Use high client_ids (30+) to avoid collisions with test suite (clientId=1)
    # and any lingering connections from previous runs.
    client_id = 30 + TICKERS.index(ticker)
    source = IBGatewaySource(host=IB_HOST, port=IB_PORT, client_id=client_id)
    df = source.fetch(
        CONIDS[ticker],
        HISTORY_START,
        BACKTEST_END,
        INTERVAL,
        contract_type="future_by_conid",
    )
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
