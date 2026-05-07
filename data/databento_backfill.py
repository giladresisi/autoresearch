from __future__ import annotations

from pathlib import Path

import pandas as pd

from data.sources import DatabentSource

MNQ_TICKER = "MNQ.v.0"
MES_TICKER  = "MES.v.0"


def backfill_parquets(
    bar_data_dir: Path,
    ib_cutoff_days: int = 2,
    max_lookback_days: int = 30,
) -> None:
    """Fetch Databento 1m bars from the last parquet bar up to `ib_cutoff_days` ago.

    Idempotent: skips tickers whose parquet is already current (last bar >= cutoff).
    Raises RuntimeError if DATABENTO_API_KEY is not set (via DatabentSource.__init__).
    """
    now    = pd.Timestamp.now(tz="America/New_York")
    cutoff = now - pd.Timedelta(days=ib_cutoff_days)
    floor  = now - pd.Timedelta(days=max_lookback_days)
    bar_data_dir.mkdir(parents=True, exist_ok=True)

    source = DatabentSource()

    for ticker, fname in [(MNQ_TICKER, "MNQ_1m.parquet"), (MES_TICKER, "MES_1m.parquet")]:
        path     = bar_data_dir / fname
        existing = pd.read_parquet(path) if path.exists() else _empty_df()
        last_bar = existing.index[-1] if not existing.empty else None
        start_ts = max(last_bar + pd.Timedelta(minutes=1), floor) if last_bar is not None else floor
        if start_ts >= cutoff:
            continue  # parquet already current up to cutoff — nothing to fetch
        # Convert to UTC so Databento range boundaries are unambiguous at DST transitions
        df_new = source.fetch(
            ticker,
            start_ts.tz_convert("UTC").isoformat(),
            cutoff.tz_convert("UTC").isoformat(),
            interval="1m",
        )
        if df_new is None or df_new.empty:
            continue
        combined = pd.concat([existing, df_new]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        combined.to_parquet(path)


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )
