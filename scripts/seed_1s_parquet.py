#!/usr/bin/env python
"""Seed MNQ_1s.parquet and MES_1s.parquet from Databento starting 2026-05-01.

Usage:
    uv run python scripts/seed_1s_parquet.py [--dry-run]

Writes to data/ (BAR_DATA_DIR env var to override). Safe to re-run — resumes
from the last bar in each existing parquet.
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from data.sources import DatabentSource

SEED_START   = "2026-05-01"
BAR_DATA_DIR = Path(os.environ.get("BAR_DATA_DIR", "data"))
PAIRS = [
    ("MNQ", "MNQ.v.0", "MNQ_1s.parquet"),
    ("MES", "MES.v.0", "MES_1s.parquet"),
]

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = pd.Timestamp.now(tz="America/New_York")
    end = now.tz_convert("UTC").isoformat()

    # Defer DatabentSource construction so --dry-run works without DATABENTO_API_KEY
    source = None if args.dry_run else DatabentSource()

    for instrument, ticker, parquet_name in PAIRS:
        parquet_path = BAR_DATA_DIR / parquet_name
        if parquet_path.exists():
            existing = pd.read_parquet(parquet_path)
            start = (existing.index[-1] + pd.Timedelta(seconds=1)).tz_convert("UTC").isoformat() if not existing.empty else SEED_START
        else:
            existing = None
            start = SEED_START
        print(f"[seed] {instrument}: {start[:10]} -> {end[:10]}", flush=True)
        if args.dry_run:
            print(f"[seed] DRY RUN — skipping fetch", flush=True)
            continue
        df = source.fetch(ticker, start, end, interval="1s")
        if df is None or df.empty:
            print(f"[seed] {instrument}: no data returned", flush=True)
            continue
        if existing is not None and not existing.empty:
            combined = pd.concat([existing, df]).sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
            df = combined
        BAR_DATA_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(parquet_path)
        print(f"[seed] {instrument}: {len(df)} bars -> {parquet_path}", flush=True)

if __name__ == "__main__":
    main()
