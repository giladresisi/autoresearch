"""
Validate parquet files: price ranges, OHLC coherence, duplicates, gaps, May 20 coverage.
"""
import pandas as pd
from pathlib import Path

DATA = Path(__file__).parent.parent / "data"

SPECS = {
    "MNQ_1m": ("MNQ_1m.parquet", 15000, 36000, "min"),
    "MES_1m": ("MES_1m.parquet",  4000,  9000, "min"),
    "MNQ_1s": ("MNQ_1s.parquet", 15000, 36000,   "s"),
    "MES_1s": ("MES_1s.parquet",  4000,  9000,   "s"),
}

# Recent valid MNQ price sample (from backup data) to cross-check contract
MNQ_CONID = 770561201  # MNQM6 June 2026

for label, (fname, price_lo, price_hi, freq) in SPECS.items():
    path = DATA / fname
    print(f"=== {fname} ===")
    if not path.exists():
        print("  MISSING")
        print()
        continue
    try:
        df = pd.read_parquet(path)
    except Exception as e:
        print(f"  UNREADABLE: {e}")
        print()
        continue

    if df.empty:
        print("  EMPTY")
        print()
        continue

    print(f"  rows  : {len(df):,}")
    print(f"  first : {df.index[0]}")
    print(f"  last  : {df.index[-1]}")

    # 1. Price sanity
    lo = df["Low"].min()
    hi = df["High"].max()
    bad_price = df[(df["Low"] < price_lo) | (df["High"] > price_hi) | (df["Close"] <= 0)]
    status = "OK" if len(bad_price) == 0 else f"BAD ({len(bad_price)} rows)"
    print(f"  price range: {lo:.2f} - {hi:.2f}  (expect {price_lo}-{price_hi})  [{status}]")
    if len(bad_price) > 0:
        print(f"    worst outliers:")
        for ts, row in bad_price.head(3).iterrows():
            print(f"      {ts}  O={row.Open:.2f} H={row.High:.2f} L={row.Low:.2f} C={row.Close:.2f}")

    # 2. Duplicate timestamps
    dups = df.index.duplicated().sum()
    print(f"  duplicates: {dups}  [{'OK' if dups == 0 else 'BAD'}]")

    # 3. OHLC coherence
    bad_ohlc = df[
        (df["High"] < df["Low"]) |
        (df["Close"] > df["High"]) |
        (df["Close"] < df["Low"]) |
        (df["Open"] > df["High"]) |
        (df["Open"] < df["Low"])
    ]
    print(f"  OHLC coherence: {len(bad_ohlc)} bad  [{'OK' if len(bad_ohlc) == 0 else 'BAD'}]")

    # 4. Negative/zero volume
    bad_vol = df[df["Volume"] < 0]
    print(f"  negative volume: {len(bad_vol)}  [{'OK' if len(bad_vol) == 0 else 'BAD'}]")

    # 5. Gap analysis
    gap_threshold = pd.Timedelta("2h") if freq == "min" else pd.Timedelta("30min")
    diffs = df.index.to_series().diff().dropna()
    big = diffs[diffs > gap_threshold].sort_values(ascending=False).head(10)
    print(f"  gaps > {gap_threshold}:")
    if big.empty:
        print("    none")
    for ts, gap in big.items():
        loc = df.index.get_loc(ts)
        prev = df.index[loc - 1]
        print(f"    {prev.strftime('%m-%d %H:%M')} -> {ts.strftime('%m-%d %H:%M')}  ({gap})")

    # 6. May 20 session coverage check
    may19_17 = pd.Timestamp("2026-05-19 17:00", tz="America/New_York")
    may20_17 = pd.Timestamp("2026-05-20 17:00", tz="America/New_York")
    may20_data = df[(df.index >= may19_17) & (df.index <= may20_17)]
    print(f"  May 19 17:00 -> May 20 17:00: {len(may20_data):,} rows")
    if not may20_data.empty:
        print(f"    first: {may20_data.index[0]}  last: {may20_data.index[-1]}")

    # 7. Timestamp timezone check
    tz = df.index.tz
    print(f"  timezone: {tz}  [{'OK' if tz is not None else 'BAD - missing tz'}]")

    # 8. Sample recent prices for contract ID sanity
    recent = df.tail(5)
    print(f"  last 5 close prices: {list(recent['Close'].round(2))}")

    print()
