"""
Rebuild MES_1s.parquet from IB historical data.

The file may be corrupted (partial write from a hard kill). This script fetches
all available IB 1s history back to _1S_EARLIEST and writes a fresh parquet.

IB pacing: 60 historical requests per 10 minutes (global, not per-contract).
This script detects Error 162 (pacing violation) and sleeps 11 min before
retrying, so it can run unattended even over hundreds of chunks.

Estimated run time: ~2-3 hours for 19 days of MES data.

Usage:
    uv run python scripts/rebuild_mes_1s.py [--dry-run]

    --dry-run   Fetch and report stats but do NOT write the parquet.

Reads IB_HOST, IB_PORT, MES_CONID from .env.
"""
import argparse
import os
import shutil
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

HOST      = os.environ.get("IB_HOST", "127.0.0.1")
PORT      = int(os.environ.get("IB_PORT", "4002"))
MES_CONID = int(os.environ.get("MES_CONID", "0"))

CHUNK_S          = 1800       # 30-min chunks — same as _gap_fill_1s_ib
EARLIEST         = "2026-05-01"
PACING_SLEEP_S   = 660        # 11 min: safe margin above 10-min IB window
DATA_DIR         = Path(__file__).parent.parent / "data"
OUT_FILE         = DATA_DIR / "MES_1s.parquet"
BACKUP           = DATA_DIR / "MES_1s.parquet.bak"


def _prev_trading_ts(ts: pd.Timestamp) -> pd.Timestamp:
    """Return ts if it's a trading time, else the most recent trading close before ts.

    CME Globex MNQ/MES schedule (ET):
      Opens:       Sunday 18:00
      Daily break: 17:00-18:00 Mon-Thu
      Weekend:     Friday 17:00 through Sunday 18:00
    """
    ts_et = ts.tz_convert("America/New_York")
    dow   = ts_et.weekday()  # 0=Mon..4=Fri, 5=Sat, 6=Sun
    t     = ts_et.hour * 3600 + ts_et.minute * 60 + ts_et.second

    CLOSE     = 17 * 3600  # 17:00:00 ET — market closes / break starts
    BREAK_END = 18 * 3600  # 18:00:00 ET — break ends / Sunday opens

    in_wknd  = (dow == 4 and t > CLOSE) or dow == 5 or (dow == 6 and t < BREAK_END)
    in_break = (not in_wknd) and CLOSE < t < BREAK_END  # Mon-Thu break

    if not (in_wknd or in_break):
        return ts  # already a trading time

    if in_break:
        # Mid-week daily break: snap to today's 17:00:00 ET
        return ts_et.normalize() + pd.Timedelta(hours=17)

    # Weekend: snap to previous Friday 17:00:00 ET
    days_to_fri = (dow - 4) % 7
    fri = ts_et.normalize() - pd.Timedelta(days=days_to_fri)
    return fri + pd.Timedelta(hours=17)


def _req_historical(ib, contract, chunk_end, chunk_s):
    return ib.reqHistoricalData(
        contract,
        endDateTime=chunk_end.tz_convert("UTC").strftime("%Y%m%d-%H:%M:%S"),
        durationStr=f"{chunk_s} S",
        barSizeSetting="1 secs",
        whatToShow="TRADES",
        useRTH=False,
        formatDate=2,
        keepUpToDate=False,
    )


def fetch_all(end_dt: pd.Timestamp, start_dt: pd.Timestamp) -> pd.DataFrame:
    from ib_insync import IB, Contract, util as ib_util

    ib = IB()
    ib.connect(HOST, PORT, clientId=99)
    contract     = Contract(conId=MES_CONID, exchange="CME")
    all_bars: list = []
    chunk_end    = end_dt
    total_chunks = 0
    pacing_hit   = False

    def _on_error(reqId, errorCode, errorString, contract):
        nonlocal pacing_hit
        if errorCode == 162 and "pacing" in errorString.lower():
            pacing_hit = True

    ib.errorEvent += _on_error

    try:
        while chunk_end > start_dt:
            # Skip non-trading windows without making an IB request
            adjusted = _prev_trading_ts(chunk_end)
            if adjusted < chunk_end:
                chunk_end = adjusted
                continue

            chunk_start = max(start_dt, chunk_end - pd.Timedelta(seconds=CHUNK_S))
            chunk_s     = max(1, int((chunk_end - chunk_start).total_seconds()))

            pacing_hit = False
            bars = _req_historical(ib, contract, chunk_end, chunk_s)

            if not bars and pacing_hit:
                wait_min = PACING_SLEEP_S // 60
                print(f"  [pacing] sleeping {wait_min} min before retry ...", flush=True)
                time.sleep(PACING_SLEEP_S)
                pacing_hit = False
                bars = _req_historical(ib, contract, chunk_end, chunk_s)

            total_chunks += 1
            if bars:
                all_bars.extend(bars)
                print(f"  chunk {total_chunks}: {chunk_start.strftime('%m-%d %H:%M')} -> "
                      f"{chunk_end.strftime('%m-%d %H:%M')}  ({len(bars)} bars)", flush=True)
            else:
                print(f"  chunk {total_chunks}: {chunk_start.strftime('%m-%d %H:%M')} -> "
                      f"{chunk_end.strftime('%m-%d %H:%M')}  (0 bars)", flush=True)
            chunk_end = chunk_start

    finally:
        ib.errorEvent -= _on_error
        if ib.isConnected():
            ib.disconnect()

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
    df = df[~df.index.duplicated(keep="last")]
    return df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if MES_CONID == 0:
        print("ERROR: MES_CONID not set in .env", file=sys.stderr)
        sys.exit(1)

    # Target: fill up to the end of yesterday's MNQ session so both parquets align
    mnq_path = DATA_DIR / "MNQ_1s.parquet"
    if mnq_path.exists():
        try:
            mnq_df = pd.read_parquet(mnq_path)
            end_dt = mnq_df.index[-1] if not mnq_df.empty else pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(minutes=2)
        except Exception:
            end_dt = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(minutes=2)
    else:
        end_dt = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(minutes=2)

    start_dt = pd.Timestamp(EARLIEST, tz="America/New_York")

    print(f"Rebuilding MES_1s.parquet")
    print(f"  range  : {start_dt.date()} -> {end_dt.strftime('%Y-%m-%d %H:%M')} ET")
    print(f"  conid  : {MES_CONID}  host: {HOST}:{PORT}  chunk: {CHUNK_S}s")
    print(f"  pacing : sleep {PACING_SLEEP_S}s on Error 162 (retry once)")
    print()

    df = fetch_all(end_dt, start_dt)

    if df.empty:
        print("No bars returned — parquet not modified.", file=sys.stderr)
        sys.exit(1)

    print()
    print(f"Fetched {len(df):,} bars")
    print(f"  first : {df.index[0]}")
    print(f"  last  : {df.index[-1]}")

    diffs    = df.index.to_series().diff().dropna()
    big_gaps = diffs[diffs > pd.Timedelta("30min")].sort_values(ascending=False).head(8)
    if not big_gaps.empty:
        print("\nLargest gaps in fetched data:")
        for ts, gap in big_gaps.items():
            loc = df.index.get_loc(ts)
            prev = df.index[loc - 1]
            print(f"  {prev.strftime('%m-%d %H:%M')} -> {ts.strftime('%m-%d %H:%M')}  ({gap})")

    if args.dry_run:
        print("\n--dry-run: parquet NOT written.")
        return

    if OUT_FILE.exists():
        shutil.copy2(OUT_FILE, BACKUP)
        print(f"\nBacked up existing file -> {BACKUP.name}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_FILE.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, use_dictionary=False)
    os.replace(tmp, OUT_FILE)
    print(f"Written: {OUT_FILE}  ({OUT_FILE.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
