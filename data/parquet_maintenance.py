from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from data.sources import DatabentSource

MNQ_TICKER = "MNQ.v.0"
MES_TICKER  = "MES.v.0"

_GAP_FILL_CHUNK_S      = 1800   # IB hard limit: 1800 S per request for 1s bars
_GAP_FILL_PACING_SLEEP = 660    # 11 min — safe margin above IB's 10-min window
_GAP_FILL_MAX_RETRIES  = 3      # consecutive pacing failures before aborting


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
        last_bar = _safe_read_last_ts(path)
        start_ts = max(last_bar + pd.Timedelta(minutes=1), floor) if last_bar is not None else floor
        if start_ts >= cutoff:
            continue  # parquet already current up to cutoff — nothing to fetch
        # Only read the full parquet when we actually need to concat new data
        existing = _safe_read_parquet(path)
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
        tmp = path.with_suffix(".parquet.tmp")
        combined.to_parquet(tmp, use_dictionary=False)
        os.replace(tmp, path)


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )


def _safe_read_parquet(path: Path) -> pd.DataFrame:
    """Read a parquet file, returning an empty DF if missing or unreadable.

    Never modifies the file — corrupt files are left intact for manual recovery.
    """
    if not path.exists():
        return _empty_df()
    try:
        return pd.read_parquet(path)
    except Exception as exc:
        print(f"[parquet_maintenance] WARNING: {path.name} unreadable ({exc}); returning empty DF", flush=True)
        return _empty_df()


def _safe_read_last_ts(path: Path) -> "pd.Timestamp | None":
    """Return the last index value of a parquet without loading OHLCV columns.

    Returns None if the file is missing, empty, or corrupted.
    Does NOT recreate the file on corruption (read-only operation).
    """
    if not path.exists():
        return None
    try:
        idx_only = pd.read_parquet(path, columns=[])
        # Use len() not .empty — a zero-column DataFrame is "empty" even if rows exist
        return idx_only.index[-1] if len(idx_only.index) > 0 else None
    except Exception:
        return None


MNQ_1S_SEED_START = pd.Timestamp("2026-05-01", tz="America/New_York")


def backfill_1s_parquets(
    bar_data_dir: Path,
    max_lookback_days: int = 10,
) -> None:
    """Fetch Databento 1s bars from the last parquet bar up to now.

    No cutoff: end=now so Databento returns the latest data it has available.
    IbRealtimeSource._gap_fill_1s_ib() fills any remaining gap at session start.
    max_lookback_days is kept small (10) because 1s data is ~60x larger than 1m.
    Raises RuntimeError if DATABENTO_API_KEY is not set (via DatabentSource.__init__).
    """
    now   = pd.Timestamp.now(tz="America/New_York")
    floor = max(now - pd.Timedelta(days=max_lookback_days), MNQ_1S_SEED_START)
    bar_data_dir.mkdir(parents=True, exist_ok=True)

    source = DatabentSource()

    for ticker, fname in [(MNQ_TICKER, "MNQ_1s.parquet"), (MES_TICKER, "MES_1s.parquet")]:
        path     = bar_data_dir / fname
        last_bar = _safe_read_last_ts(path)
        start_ts = max(last_bar + pd.Timedelta(seconds=1), floor) if last_bar is not None else floor
        # Skip if parquet is already within Databento's data lag window (~10 min)
        if last_bar is not None and start_ts >= now - pd.Timedelta(minutes=10):
            continue
        # Only read the full parquet when we actually need to concat new data
        existing = _safe_read_parquet(path)
        df_new = source.fetch(
            ticker,
            start_ts.tz_convert("UTC").isoformat(),
            now.tz_convert("UTC").isoformat(),
            interval="1s",
        )
        if df_new is None or df_new.empty:
            continue
        combined = pd.concat([existing, df_new]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        tmp = path.with_suffix(".parquet.tmp")
        combined.to_parquet(tmp, use_dictionary=False)
        os.replace(tmp, path)


def _prev_trading_ts_gap(ts: pd.Timestamp) -> pd.Timestamp:
    """Return ts if it's a trading time, else the most recent trading close before ts.

    CME Globex MNQ/MES schedule (ET):
      Opens:       Sunday 18:00
      Daily break: 17:00-18:00 Mon-Thu
      Weekend:     Friday 17:00 through Sunday 18:00
    """
    ts_et = ts.tz_convert("America/New_York")
    dow   = ts_et.weekday()  # 0=Mon..4=Fri, 5=Sat, 6=Sun
    t     = ts_et.hour * 3600 + ts_et.minute * 60 + ts_et.second

    CLOSE     = 17 * 3600  # 17:00:00 ET
    BREAK_END = 18 * 3600  # 18:00:00 ET

    in_wknd  = (dow == 4 and t > CLOSE) or dow == 5 or (dow == 6 and t < BREAK_END)
    in_break = (not in_wknd) and CLOSE < t < BREAK_END  # Mon-Thu break

    if not (in_wknd or in_break):
        return ts

    if in_break:
        return ts_et.normalize() + pd.Timedelta(hours=17)

    days_to_fri = (dow - 4) % 7
    fri = ts_et.normalize() - pd.Timedelta(days=days_to_fri)
    return fri + pd.Timedelta(hours=17)


def _fetch_gap_chunked(
    ib, contract, gap_start: pd.Timestamp, gap_end: pd.Timestamp
) -> tuple:
    """Fetch gap data in ≤1800 S chunks, retrying on IB pacing errors.

    Returns (gap_df, success). success=False if pacing retries exhausted.
    success=True even if 0 bars returned (valid for market-closed windows).
    """
    import time as _time
    from ib_insync import util as _util

    all_bars = []
    chunk_end = gap_end
    consecutive_pacing = 0
    pacing_hit = False

    def _on_error(reqId, errorCode, errorString, contract):
        nonlocal pacing_hit
        if errorCode == 162 and "pacing" in errorString.lower():
            pacing_hit = True

    ib.errorEvent += _on_error
    try:
        while chunk_end > gap_start:
            adjusted = _prev_trading_ts_gap(chunk_end)
            if adjusted < chunk_end:
                chunk_end = adjusted
                continue

            chunk_start = max(gap_start, chunk_end - pd.Timedelta(seconds=_GAP_FILL_CHUNK_S))
            chunk_s = max(1, int((chunk_end - chunk_start).total_seconds()))

            pacing_hit = False
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=chunk_end.tz_convert("UTC").strftime("%Y%m%d-%H:%M:%S"),
                durationStr=f"{chunk_s} S",
                barSizeSetting="1 secs",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=2,
            )

            if not bars and pacing_hit:
                consecutive_pacing += 1
                if consecutive_pacing > _GAP_FILL_MAX_RETRIES:
                    print(
                        f"[merge_session_1s] gap fill: pacing retries exhausted "
                        f"({_GAP_FILL_MAX_RETRIES} consecutive) — aborting",
                        flush=True,
                    )
                    return pd.DataFrame(), False
                wait_min = _GAP_FILL_PACING_SLEEP // 60
                print(
                    f"[merge_session_1s] gap fill: pacing — sleeping {wait_min} min "
                    f"(retry {consecutive_pacing}/{_GAP_FILL_MAX_RETRIES}) ...",
                    flush=True,
                )
                _time.sleep(_GAP_FILL_PACING_SLEEP)
                pacing_hit = False
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime=chunk_end.tz_convert("UTC").strftime("%Y%m%d-%H:%M:%S"),
                    durationStr=f"{chunk_s} S",
                    barSizeSetting="1 secs",
                    whatToShow="TRADES",
                    useRTH=False,
                    formatDate=2,
                )
                if not bars:
                    chunk_end = chunk_start
                    continue
                consecutive_pacing = 0  # retry succeeded — reset counter
            else:
                consecutive_pacing = 0

            if bars:
                all_bars.extend(bars)
            chunk_end = chunk_start
    finally:
        ib.errorEvent -= _on_error

    if not all_bars:
        return pd.DataFrame(), True

    df = _util.df(all_bars).rename(columns={
        "date": "datetime", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    }).set_index("datetime")
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize("America/New_York")
    else:
        df.index = df.index.tz_convert("America/New_York")
    df = df[["Open", "High", "Low", "Close", "Volume"]].sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df, True


def merge_session_1s_parquets(bar_data_dir: Path) -> None:
    """Merge session 1s parquets into main parquets, filling the gap via IB.

    For each instrument with a session file:
      1. IB-fill the gap: main_parquet[-1] → session_parquet[0]  (~2 min)
      2. Concat: existing + gap bars + session bars
      3. Write main parquet; delete session file

    Safe no-op if no session files exist (no IB connection opened).
    IB connection failure is non-fatal: merge proceeds without gap fill.
    IB params from env: IB_HOST, IB_PORT, MNQ_CONID, MES_CONID.
    """
    from ib_insync import IB, Contract as _IBContract

    bar_data_dir = Path(bar_data_dir)
    _host    = os.environ.get("IB_HOST", "127.0.0.1")
    _port    = int(os.environ.get("IB_PORT", "4002"))
    _mnq_con = os.environ.get("MNQ_CONID")
    _mes_con = os.environ.get("MES_CONID")
    _client  = 16  # strategy not connected at merge time; no conflict

    pairs = [
        ("MNQ", "MNQ_1s.parquet", "MNQ_1s_session_*.parquet", _mnq_con),
        ("MES", "MES_1s.parquet", "MES_1s_session_*.parquet", _mes_con),
    ]

    merges_needed = [
        (inst, main, sorted(bar_data_dir.glob(glob)), conid)
        for inst, main, glob, conid in pairs
        if list(bar_data_dir.glob(glob))
    ]
    if not merges_needed:
        return

    ib = IB()
    ib_ok = False
    try:
        ib.connect(_host, _port, clientId=_client)
        ib_ok = True
    except Exception as exc:
        print(f"[merge_session_1s] IB unavailable ({exc}) — merging without gap fill", flush=True)

    try:
        for instrument, main_name, session_files, conid in merges_needed:
            main_path = bar_data_dir / main_name
            existing  = _safe_read_parquet(main_path)
            abort_merge = False

            for session_path in session_files:
                session_df = _safe_read_parquet(session_path)
                if session_df.empty:
                    session_path.unlink()
                    continue

                # IB gap fill: main[-1] → session[0], chunked to respect IB's 1800 S limit
                if ib_ok and conid and not existing.empty:
                    gap_start = existing.index[-1]
                    gap_end   = session_df.index[0]
                    gap_s     = max(0, int((gap_end - gap_start).total_seconds()) - 1)
                    if gap_s > 1:
                        contract = _IBContract(conId=int(conid), exchange="CME")
                        gap_df, gap_ok = _fetch_gap_chunked(ib, contract, gap_start, gap_end)
                        if not gap_ok:
                            print(
                                f"[merge_session_1s] {instrument}: WARNING — gap fill failed after "
                                f"{_GAP_FILL_MAX_RETRIES} pacing retries. Gap "
                                f"{gap_start.strftime('%m-%d %H:%M')} → "
                                f"{gap_end.strftime('%m-%d %H:%M')} ({gap_s}s) not filled. "
                                f"Skipping merge to avoid writing incomplete data.",
                                flush=True,
                            )
                            abort_merge = True
                            break  # preserve session file and skip main parquet write
                        if not gap_df.empty:
                            existing = pd.concat(
                                [existing, gap_df[["Open", "High", "Low", "Close", "Volume"]]]
                            ).sort_index()
                            existing = existing[~existing.index.duplicated(keep="last")]
                            print(f"[merge_session_1s] {instrument}: +{len(gap_df)} gap bars", flush=True)

                existing = pd.concat([existing, session_df]).sort_index()
                existing = existing[~existing.index.duplicated(keep="last")]

            if abort_merge:
                continue  # skip to next instrument — do NOT write main parquet or delete session

            tmp = main_path.with_suffix(".parquet.tmp")
            existing.to_parquet(tmp, use_dictionary=False)
            os.replace(tmp, main_path)
            for session_path in session_files:
                if session_path.exists():
                    session_path.unlink()
            print(f"[merge_session_1s] {instrument}: merged {len(session_files)} session file(s)", flush=True)
    finally:
        try:
            if ib_ok and ib.isConnected():
                ib.disconnect()
        except Exception:
            pass
