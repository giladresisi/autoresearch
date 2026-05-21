"""
Restore MES_1m.parquet and MNQ_1s.parquet from backups + IB delta.

Both files were corrupted/reset. The backups in data/backup_parquets_until_19_5/
cover up to 2026-05-19 16:58. This script:
  1. Copies the backup as the base
  2. Fetches the IB delta from the backup's last bar to now
  3. Merges and writes atomically

For MES_1s (full rebuild from May 1), run scripts/rebuild_mes_1s.py separately
after this script completes.

Usage:
    uv run python scripts/restore_parquets.py [--dry-run]

    --dry-run   Fetch and print stats but do NOT write any parquet.

Reads IB_HOST, IB_PORT, MNQ_CONID, MES_CONID from .env.
"""
import argparse
import os
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

HOST      = os.environ.get("IB_HOST", "127.0.0.1")
PORT      = int(os.environ.get("IB_PORT", "4002"))
MNQ_CONID = int(os.environ.get("MNQ_CONID", "0"))
MES_CONID = int(os.environ.get("MES_CONID", "0"))

DATA_DIR  = Path(__file__).parent.parent / "data"
BACKUP    = DATA_DIR / "backup_parquets_until_19_5"

CHUNK_1S_S  = 1800   # 30-min chunks for 1s bars
CHUNK_1M_S  = 86400  # 1-day chunks for 1m bars
PACING_SLEEP_S = 660


def _req_historical(ib, contract, chunk_end: pd.Timestamp, chunk_s: int, bar_size: str) -> list:
    return ib.reqHistoricalData(
        contract,
        endDateTime=chunk_end.tz_convert("UTC").strftime("%Y%m%d-%H:%M:%S"),
        durationStr=f"{chunk_s} S",
        barSizeSetting=bar_size,
        whatToShow="TRADES",
        useRTH=False,
        formatDate=2,
        keepUpToDate=False,
    )


def fetch_range(ib, contract, start_dt: pd.Timestamp, end_dt: pd.Timestamp,
                chunk_s: int, bar_size: str, label: str) -> pd.DataFrame:
    from ib_insync import util as ib_util

    pacing_hit = False

    def _on_error(reqId, errorCode, errorString, contract):
        nonlocal pacing_hit
        if errorCode == 162 and "pacing" in errorString.lower():
            pacing_hit = True

    ib.errorEvent += _on_error

    all_bars: list = []
    chunk_end    = end_dt
    total_chunks = 0

    try:
        while chunk_end > start_dt:
            chunk_start = max(start_dt, chunk_end - pd.Timedelta(seconds=chunk_s))
            actual_s    = max(1, int((chunk_end - chunk_start).total_seconds()))

            pacing_hit = False
            bars = _req_historical(ib, contract, chunk_end, actual_s, bar_size)

            if not bars and pacing_hit:
                print(f"  [{label}] pacing — sleeping {PACING_SLEEP_S // 60} min ...", flush=True)
                time.sleep(PACING_SLEEP_S)
                pacing_hit = False
                bars = _req_historical(ib, contract, chunk_end, actual_s, bar_size)

            total_chunks += 1
            if bars:
                all_bars.extend(bars)
            print(f"  [{label}] chunk {total_chunks}: {chunk_start.strftime('%m-%d %H:%M')} -> "
                  f"{chunk_end.strftime('%m-%d %H:%M')}  ({len(bars)} bars)", flush=True)
            chunk_end = chunk_start
    finally:
        ib.errorEvent -= _on_error

    if not all_bars:
        return pd.DataFrame()

    df = ib_util.df(all_bars).rename(columns={
        "date": "datetime", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    }).set_index("datetime")

    if df.index.tzinfo is None:
        df.index = df.index.tz_localize("America/New_York")
    else:
        df.index = df.index.tz_convert("America/New_York")

    df = df[["Open", "High", "Low", "Close", "Volume"]].sort_index()
    return df[~df.index.duplicated(keep="last")]


def restore_file(ib, contract, backup_path: Path, main_path: Path,
                 chunk_s: int, bar_size: str, label: str, dry_run: bool) -> None:
    print(f"\n{'='*60}")
    print(f"Restoring {main_path.name}")

    # 1. Load backup
    print(f"  Loading backup: {backup_path.name} ...", flush=True)
    backup_df = pd.read_parquet(backup_path)
    print(f"  Backup: {len(backup_df):,} rows  {backup_df.index[0]} -> {backup_df.index[-1]}")

    backup_end = backup_df.index[-1]
    now_et     = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(seconds=10)

    # 2. Load existing main (may have a few today-bars we want to preserve)
    existing_today_df = pd.DataFrame()
    if main_path.exists():
        try:
            existing_today_df = pd.read_parquet(main_path)
            if not existing_today_df.empty:
                print(f"  Existing: {len(existing_today_df):,} rows  "
                      f"{existing_today_df.index[0]} -> {existing_today_df.index[-1]}")
        except Exception as e:
            print(f"  Existing file unreadable ({e}), ignoring.")

    # 3. Fetch IB delta: backup_end -> now
    print(f"  Fetching IB delta: {backup_end.strftime('%m-%d %H:%M')} -> "
          f"{now_et.strftime('%m-%d %H:%M ET')} ...", flush=True)
    delta_df = fetch_range(ib, contract, backup_end, now_et, chunk_s, bar_size, label)
    if delta_df.empty:
        print(f"  WARNING: IB returned no data for delta range — using backup only")

    # 4. Merge: backup + delta + today's existing rows
    parts = [backup_df]
    if not delta_df.empty:
        parts.append(delta_df)
    if not existing_today_df.empty:
        parts.append(existing_today_df)

    combined = pd.concat(parts).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]

    print(f"\n  Result: {len(combined):,} rows  {combined.index[0]} -> {combined.index[-1]}")

    diffs    = combined.index.to_series().diff().dropna()
    if bar_size == "1 secs":
        gap_threshold = pd.Timedelta("30min")
    else:
        gap_threshold = pd.Timedelta("2h")
    big_gaps = diffs[diffs > gap_threshold].sort_values(ascending=False).head(8)
    if not big_gaps.empty:
        print(f"  Gaps > {gap_threshold}:")
        for ts, gap in big_gaps.items():
            loc = combined.index.get_loc(ts)
            prev = combined.index[loc - 1]
            print(f"    {prev.strftime('%m-%d %H:%M')} -> {ts.strftime('%m-%d %H:%M')}  ({gap})")

    if dry_run:
        print(f"  --dry-run: {main_path.name} NOT written.")
        return

    # Atomic write
    tmp = main_path.with_suffix(".parquet.tmp")
    combined.to_parquet(tmp, use_dictionary=False)
    os.replace(tmp, main_path)
    print(f"  Written: {main_path.name}  ({main_path.stat().st_size // 1024} KB)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if MNQ_CONID == 0 or MES_CONID == 0:
        print("ERROR: MNQ_CONID and MES_CONID must be set in .env", file=sys.stderr)
        sys.exit(1)

    from ib_insync import IB, Contract

    ib = IB()
    ib.connect(HOST, PORT, clientId=96)
    mnq_contract = Contract(conId=MNQ_CONID, exchange="CME")
    mes_contract = Contract(conId=MES_CONID, exchange="CME")

    try:
        # MES_1m: restore from backup + IB 1m delta
        restore_file(
            ib, mes_contract,
            backup_path=BACKUP / "MES_1m.parquet",
            main_path=DATA_DIR / "MES_1m.parquet",
            chunk_s=CHUNK_1M_S,
            bar_size="1 min",
            label="MES_1m",
            dry_run=args.dry_run,
        )

        # MNQ_1s: restore from backup + IB 1s delta
        restore_file(
            ib, mnq_contract,
            backup_path=BACKUP / "MNQ_1s.parquet",
            main_path=DATA_DIR / "MNQ_1s.parquet",
            chunk_s=CHUNK_1S_S,
            bar_size="1 secs",
            label="MNQ_1s",
            dry_run=args.dry_run,
        )
    finally:
        if ib.isConnected():
            ib.disconnect()

    print("\nDone. Now run scripts/rebuild_mes_1s.py to restore MES_1s.parquet (2-3 hours).")


if __name__ == "__main__":
    main()
