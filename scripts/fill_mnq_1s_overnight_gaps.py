"""
Find overnight gaps in MNQ_1s.parquet, probe IB for each, and merge valid data.

Logic:
  1. Read current MNQ_1s.parquet, find all gaps > 90 seconds.
  2. Skip gaps that fall in expected closed windows:
       - Weekend: Fri 17:00 ET through Sun 18:00 ET
       - Daily maintenance break: 17:00–18:00 ET Mon–Thu
  3. For each unexpected gap: fetch the gap window from IB.
  4. Validate fetched data (price range, OHLC coherence, no dups).
  5. Preview what was found; auto-merge if --merge flag is given.

Usage:
    uv run python scripts/fill_mnq_1s_overnight_gaps.py           # probe + preview only
    uv run python scripts/fill_mnq_1s_overnight_gaps.py --merge   # probe + merge if valid
    uv run python scripts/fill_mnq_1s_overnight_gaps.py --dry-run # show gaps only, no IB calls

Reads IB_HOST, IB_PORT, MNQ_CONID from .env.
"""
import argparse
import os
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

HOST      = os.environ.get("IB_HOST", "127.0.0.1")
PORT      = int(os.environ.get("IB_PORT", "4002"))
MNQ_CONID = int(os.environ.get("MNQ_CONID", "0"))

CHUNK_S   = 1800
DATA_DIR  = Path(__file__).parent.parent / "data"
MAIN_FILE = DATA_DIR / "MNQ_1s.parquet"
PREVIEW   = DATA_DIR / "MNQ_1s_gaps_preview.parquet"

# Expected price range for MNQM6 (Jun 2026)
PRICE_LO = 24000.0
PRICE_HI = 32000.0


def _is_expected_closed(gap_start: pd.Timestamp, gap_end: pd.Timestamp) -> str | None:
    """Return a reason string if this gap is in a known closed window, else None."""
    start_et = gap_start.tz_convert("America/New_York")
    end_et   = gap_end.tz_convert("America/New_York")
    duration = gap_end - gap_start

    dow_start = start_et.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    t_start   = start_et.hour * 3600 + start_et.minute * 60 + start_et.second
    t_end_h   = end_et.hour

    CLOSE_T     = 17 * 3600  # 17:00 ET — daily close / break starts
    BREAK_END_T = 18 * 3600  # 18:00 ET — break ends / overnight opens

    # Weekend: gap starts on Fri at/after 17:00, or on Sat, or on Sun before 18:00
    in_fri_close = dow_start == 4 and t_start >= CLOSE_T
    in_sat       = dow_start == 5
    in_sun_early = dow_start == 6 and t_start < BREAK_END_T
    if in_fri_close or in_sat or in_sun_early:
        return f"weekend closure"

    # Daily maintenance break: starts at/after 16:55 ET, ends by 18:05 ET, < 70 min
    in_maint_start = t_start >= CLOSE_T - 300        # 16:55+ to allow for early-close rounding
    in_maint_end   = t_end_h <= 18 and end_et.minute <= 5
    if in_maint_start and in_maint_end and duration <= pd.Timedelta("75min"):
        return f"daily maintenance break"

    return None  # unexpected gap — probe IB


def fetch_gap(ib, contract, start_dt: pd.Timestamp, end_dt: pd.Timestamp) -> pd.DataFrame:
    from ib_insync import util as ib_util

    total_s = int((end_dt - start_dt).total_seconds())
    all_bars: list = []
    chunk_end = end_dt
    chunk_num = 0

    while chunk_end > start_dt:
        chunk_start = max(start_dt, chunk_end - pd.Timedelta(seconds=CHUNK_S))
        actual_s    = max(1, int((chunk_end - chunk_start).total_seconds()))
        bars = ib.reqHistoricalData(
            contract,
            endDateTime=chunk_end.tz_convert("UTC").strftime("%Y%m%d-%H:%M:%S"),
            durationStr=f"{actual_s} S",
            barSizeSetting="1 secs",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=2,
            keepUpToDate=False,
        )
        chunk_num += 1
        if not bars:
            break
        all_bars.extend(bars)
        chunk_end = chunk_start

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


def validate(df: pd.DataFrame, label: str) -> bool:
    ok = True
    if df.empty:
        print(f"    [SKIP] no bars returned from IB")
        return False
    print(f"    bars : {len(df):,}  {df.index[0].strftime('%m-%d %H:%M:%S')} -> {df.index[-1].strftime('%m-%d %H:%M:%S')}")
    # Price range
    lo = df["Low"].min(); hi = df["High"].max()
    if lo < PRICE_LO or hi > PRICE_HI:
        print(f"    [WARN] price out of range: {lo:.2f}–{hi:.2f}  (expect {PRICE_LO}–{PRICE_HI})")
        ok = False
    else:
        print(f"    price range: {lo:.2f}–{hi:.2f}  OK")
    # OHLC coherence
    bad_ohlc = df[
        (df["High"] < df["Low"]) | (df["Close"] > df["High"]) |
        (df["Close"] < df["Low"]) | (df["Open"] > df["High"]) | (df["Open"] < df["Low"])
    ]
    if len(bad_ohlc):
        print(f"    [WARN] {len(bad_ohlc)} OHLC incoherent bars")
        ok = False
    else:
        print(f"    OHLC: OK")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--merge",   action="store_true", help="Merge valid gap data into main parquet")
    parser.add_argument("--dry-run", action="store_true", help="List gaps only, no IB calls")
    args = parser.parse_args()

    if MNQ_CONID == 0:
        print("ERROR: MNQ_CONID not set in .env", file=sys.stderr)
        sys.exit(1)

    df_main = pd.read_parquet(MAIN_FILE)
    print(f"MNQ_1s.parquet: {len(df_main):,} rows  {df_main.index[0]} -> {df_main.index[-1]}")
    print()

    # Find all gaps
    diffs = df_main.index.to_series().diff().dropna()
    all_gaps = diffs[diffs > pd.Timedelta("90s")]
    print(f"All gaps > 90s: {len(all_gaps)}")

    gaps_to_probe = []
    skipped = []
    for ts_end, duration in all_gaps.items():
        loc       = df_main.index.get_loc(ts_end)
        ts_start  = df_main.index[loc - 1]
        reason    = _is_expected_closed(ts_start, ts_end)
        if reason:
            skipped.append((ts_start, ts_end, duration, reason))
        else:
            gaps_to_probe.append((ts_start, ts_end, duration))

    print(f"  Expected (skipping): {len(skipped)}")
    for ts_s, ts_e, dur, reason in skipped:
        print(f"    {ts_s.strftime('%m-%d %H:%M')} -> {ts_e.strftime('%m-%d %H:%M')}  ({dur})  [{reason}]")
    print(f"  Unexpected (probing): {len(gaps_to_probe)}")
    for ts_s, ts_e, dur in gaps_to_probe:
        print(f"    {ts_s.strftime('%m-%d %H:%M')} -> {ts_e.strftime('%m-%d %H:%M')}  ({dur})")

    if not gaps_to_probe:
        print("\nNo unexpected gaps to probe.")
        return

    if args.dry_run:
        print("\n--dry-run: stopping before IB calls.")
        return

    from ib_insync import IB, Contract

    ib = IB()
    ib.connect(HOST, PORT, clientId=98)
    contract = Contract(conId=MNQ_CONID, exchange="CME")

    valid_dfs = []
    try:
        for idx, (ts_start, ts_end, duration) in enumerate(gaps_to_probe):
            print(f"\n[{idx+1}/{len(gaps_to_probe)}] Probing "
                  f"{ts_start.strftime('%m-%d %H:%M')} -> {ts_end.strftime('%m-%d %H:%M')}  ({duration})")
            fetched = fetch_gap(ib, contract, ts_start, ts_end)
            ok = validate(fetched, f"gap {idx+1}")
            if ok:
                # Check overlap with existing data
                overlap = fetched.index.intersection(df_main.index)
                if len(overlap):
                    print(f"    note: {len(overlap)} timestamps already in main (will keep latest)")
                valid_dfs.append(fetched)
            else:
                print(f"    [SKIP] validation failed — not merging this gap")
    finally:
        if ib.isConnected():
            ib.disconnect()

    if not valid_dfs:
        print("\nNo valid gap data found — main parquet unchanged.")
        return

    combined_gap = pd.concat(valid_dfs).sort_index()
    combined_gap = combined_gap[~combined_gap.index.duplicated(keep="last")]
    print(f"\n{'='*60}")
    print(f"Total valid gap bars: {len(combined_gap):,}")
    print(f"  {combined_gap.index[0]} -> {combined_gap.index[-1]}")

    if not args.merge:
        combined_gap.to_parquet(PREVIEW)
        print(f"\nSaved preview -> {PREVIEW.name}")
        print("Re-run with --merge to merge into MNQ_1s.parquet")
        return

    # Merge
    merged = pd.concat([df_main, combined_gap]).sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    print(f"\nMerging: {len(df_main):,} + {len(combined_gap):,} -> {len(merged):,} rows")

    tmp = MAIN_FILE.with_suffix(".parquet.tmp")
    merged.to_parquet(tmp, use_dictionary=False)
    os.replace(tmp, MAIN_FILE)
    print(f"Written: {MAIN_FILE.name}  ({MAIN_FILE.stat().st_size // 1024} KB)")

    if PREVIEW.exists():
        PREVIEW.unlink()


if __name__ == "__main__":
    main()
