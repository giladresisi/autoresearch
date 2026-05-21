# scripts/check_session_parquets.py
# Validates, repairs, and merges 1s session parquet files at session end or orchestrator start.
# Outputs a JSON report to stdout; human-readable progress to stderr.

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

HOST      = os.environ.get("IB_HOST", "127.0.0.1")
PORT      = int(os.environ.get("IB_PORT", "4002"))
MNQ_CONID = int(os.environ.get("MNQ_CONID", "0"))
MES_CONID = int(os.environ.get("MES_CONID", "0"))

CHUNK_S          = 1800        # IB hard limit: seconds per request for 1s bars
PACING_SLEEP_S   = 660         # 11 min sleep on Error 162
IB_CLIENT_ID     = 17

PRICE_BOUNDS = {
    "MNQ": (20000.0, 35000.0),
    "MES": (5000.0,  9000.0),
}

SMALL_GAP_THRESHOLD    = pd.Timedelta("5min")
LARGE_GAP_THRESHOLD    = pd.Timedelta("60min")
BAD_ROW_MINOR_FRAC     = 0.01
BAD_ROW_CRITICAL_FRAC  = 0.05

DATA_DIR = Path(__file__).parent.parent / "data"

INSTRUMENTS = [
    ("MNQ", MNQ_CONID, "MNQ_1s.parquet", "MNQ_1s_session_*.parquet"),
    ("MES", MES_CONID, "MES_1s.parquet", "MES_1s_session_*.parquet"),
]


def _is_expected_closed(gap_start: pd.Timestamp, gap_end: pd.Timestamp) -> bool:
    """Return True if this gap falls within an expected closed window (maintenance or weekend)."""
    start_et = gap_start.tz_convert("America/New_York")
    end_et   = gap_end.tz_convert("America/New_York")
    duration = gap_end - gap_start

    dow_start = start_et.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    t_start   = start_et.hour * 3600 + start_et.minute * 60 + start_et.second
    t_end_h   = end_et.hour

    CLOSE_T     = 17 * 3600
    BREAK_END_T = 18 * 3600

    in_fri_close = dow_start == 4 and t_start >= CLOSE_T
    in_sat       = dow_start == 5
    in_sun_early = dow_start == 6 and t_start < BREAK_END_T
    if in_fri_close or in_sat or in_sun_early:
        return True

    in_maint_start = t_start >= CLOSE_T - 300
    in_maint_end   = t_end_h <= 18 and end_et.minute <= 5
    if in_maint_start and in_maint_end and duration <= pd.Timedelta("75min"):
        return True

    return False


def _safe_read(path: Path):
    """Read parquet; return None if file missing, empty DataFrame if unreadable."""
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        return pd.DataFrame()


def _prev_trading_ts(ts: pd.Timestamp) -> pd.Timestamp:
    """Return ts if it's a trading time, else the most recent trading close before ts."""
    ts_et = ts.tz_convert("America/New_York")
    dow   = ts_et.weekday()
    t     = ts_et.hour * 3600 + ts_et.minute * 60 + ts_et.second

    CLOSE     = 17 * 3600
    BREAK_END = 18 * 3600

    in_wknd  = (dow == 4 and t > CLOSE) or dow == 5 or (dow == 6 and t < BREAK_END)
    in_break = (not in_wknd) and CLOSE < t < BREAK_END

    if not (in_wknd or in_break):
        return ts

    if in_break:
        return ts_et.normalize() + pd.Timedelta(hours=17)

    days_to_fri = (dow - 4) % 7
    fri = ts_et.normalize() - pd.Timedelta(days=days_to_fri)
    return fri + pd.Timedelta(hours=17)


def fetch_range(ib, contract, start_dt: pd.Timestamp, end_dt: pd.Timestamp) -> pd.DataFrame:
    """Fetch historical 1s bars from IB in CHUNK_S-second chunks, with pacing retry."""
    from ib_insync import util as ib_util

    all_bars = []
    chunk_end = end_dt
    pacing_hit = False

    def _on_error(reqId, errorCode, errorString, contract):
        nonlocal pacing_hit
        if errorCode == 162 and "pacing" in errorString.lower():
            pacing_hit = True

    ib.errorEvent += _on_error
    try:
        while chunk_end > start_dt:
            adjusted = _prev_trading_ts(chunk_end)
            if adjusted < chunk_end:
                chunk_end = adjusted
                continue

            chunk_start = max(start_dt, chunk_end - pd.Timedelta(seconds=CHUNK_S))
            actual_s    = max(1, int((chunk_end - chunk_start).total_seconds()))

            pacing_hit = False
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

            if not bars and pacing_hit:
                import time
                print(f"[check] pacing — sleeping {PACING_SLEEP_S // 60} min", file=sys.stderr)
                time.sleep(PACING_SLEEP_S)
                pacing_hit = False
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

            if not bars:
                # Skip empty chunks (closed windows) instead of aborting the fetch
                chunk_end = chunk_start
                continue

            all_bars.extend(bars)
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


def validate_session_df(df, price_lo: float, price_hi: float, expected_session_start=None) -> dict:
    """Validate a session DataFrame and classify its severity."""
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return {
            "severity": "critical",
            "rows": 0,
            "first": None,
            "last": None,
            "bad_rows": 0,
            "bad_row_frac": 0.0,
            "unexpected_gaps": [],
            "max_gap_s": 0,
            "late_start_hours": 0.0,
        }

    bad_price = df[(df["Low"] < price_lo) | (df["High"] > price_hi) | (df["Close"] <= 0)]
    bad_ohlc  = df[
        (df["High"] < df["Low"]) | (df["Close"] > df["High"]) |
        (df["Close"] < df["Low"]) | (df["Open"] > df["High"]) | (df["Open"] < df["Low"])
    ]
    # Union for conservative estimate
    bad_idx   = bad_price.index.union(bad_ohlc.index)
    total_bad = len(bad_idx)
    bad_row_frac = total_bad / max(1, len(df))

    # Find unexpected gaps
    diffs = df.index.to_series().diff().dropna()
    raw_gaps = diffs[diffs > pd.Timedelta("90s")]
    unexpected_gaps = []
    for ts_end, duration in raw_gaps.items():
        loc = df.index.get_loc(ts_end)
        ts_start = df.index[loc - 1]
        if not _is_expected_closed(ts_start, ts_end):
            unexpected_gaps.append({
                "start": ts_start.isoformat(),
                "end": ts_end.isoformat(),
                "duration_s": int(duration.total_seconds()),
            })

    max_gap_s = max((g["duration_s"] for g in unexpected_gaps), default=0)

    # Severity
    if bad_row_frac >= BAD_ROW_CRITICAL_FRAC or max_gap_s >= int(LARGE_GAP_THRESHOLD.total_seconds()):
        severity = "critical"
    elif bad_row_frac >= BAD_ROW_MINOR_FRAC or (max_gap_s >= int(SMALL_GAP_THRESHOLD.total_seconds()) and max_gap_s < int(LARGE_GAP_THRESHOLD.total_seconds())):
        severity = "major"
    elif total_bad > 0 or (max_gap_s > 0 and max_gap_s < int(SMALL_GAP_THRESHOLD.total_seconds())):
        severity = "minor"
    else:
        severity = "ok"

    # late_start_hours
    late_start_hours = 0.0
    if expected_session_start is not None and not df.empty:
        late_start_hours = max(0.0, (df.index[0] - expected_session_start).total_seconds() / 3600)

    return {
        "severity": severity,
        "rows": len(df),
        "first": df.index[0].isoformat(),
        "last": df.index[-1].isoformat(),
        "bad_rows": total_bad,
        "bad_row_frac": bad_row_frac,
        "unexpected_gaps": unexpected_gaps,
        "max_gap_s": max_gap_s,
        "late_start_hours": late_start_hours,
    }


def get_session_start_for_end_mode() -> pd.Timestamp:
    """Return the CME session open (18:00 ET) for the current trading day."""
    now_et = pd.Timestamp.now(tz="America/New_York")
    dow = now_et.weekday()

    # If it's Saturday, snap to previous Friday 18:00 is wrong — Friday 18:00 is actually
    # the PRIOR session. For CME, the session that ran today started at 18:00 YESTERDAY.
    # Special cases: weekend
    if dow == 5:  # Saturday — session started Friday 18:00
        base = now_et.normalize() - pd.Timedelta(days=1)
        return base.replace(hour=18, minute=0, second=0, microsecond=0)
    if dow == 6:  # Sunday
        if now_et.hour >= 18:
            # CME reopened this evening — session started today
            base = now_et.normalize()
        else:
            # Still in Friday's session window
            base = now_et.normalize() - pd.Timedelta(days=2)
        return base.replace(hour=18, minute=0, second=0, microsecond=0)

    # Weekday: session started YESTERDAY at 18:00 (or two days back if Monday = after weekend)
    if now_et.hour >= 18:
        # Current time is after 18:00 — new session started TODAY at 18:00
        base = now_et.normalize()
    else:
        # Before 18:00 — session started YESTERDAY at 18:00
        base = now_et.normalize() - pd.Timedelta(days=1)
        # If yesterday was Sunday, snap to Friday
        if base.weekday() == 6:
            base = base - pd.Timedelta(days=2)

    return base.replace(hour=18, minute=0, second=0, microsecond=0)


def write_atomic(df: pd.DataFrame, path: Path) -> None:
    """Write df to a .tmp file then atomically replace the target."""
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, use_dictionary=False)
    os.replace(tmp, path)


def backup_main(main_path: Path) -> None:
    """Overwrite the .bak file with the current main parquet."""
    bak = main_path.with_suffix(".parquet.bak")
    shutil.copy2(main_path, bak)
    print(f"[check] Backed up {main_path.name} -> {main_path.name}.bak", file=sys.stderr)


def targeted_fill(ib, contract, session_path: Path, unexpected_gaps: list, session_df: pd.DataFrame) -> pd.DataFrame:
    """Fetch missing bars for each unexpected gap and patch them into the session DataFrame."""
    patched = session_df.copy()
    for gap in unexpected_gaps:
        gap_start = pd.Timestamp(gap["start"])
        gap_end   = pd.Timestamp(gap["end"])
        fetched = fetch_range(ib, contract, gap_start, gap_end)
        if not fetched.empty:
            patched = pd.concat([patched, fetched]).sort_index()
            patched = patched[~patched.index.duplicated(keep="last")]
    return patched


def rebuild_session(ib, contract, session_start: pd.Timestamp, session_end: pd.Timestamp) -> pd.DataFrame:
    """Fetch the entire session window from IB, skipping non-trading windows."""
    return fetch_range(ib, contract, session_start, session_end)


def gap_fill_to_now(ib, contract, main_path: Path) -> pd.DataFrame:
    """Fetch bars from main parquet's last timestamp up to now."""
    main_df = _safe_read(main_path)
    if main_df is None or (isinstance(main_df, pd.DataFrame) and main_df.empty):
        return pd.DataFrame()
    gap_start = main_df.index[-1]
    gap_end   = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(seconds=2)
    if gap_end <= gap_start:
        return pd.DataFrame()
    return fetch_range(ib, contract, gap_start, gap_end)


def process_instrument(inst: str, conid: int, main_name: str, session_glob: str,
                       mode: str, dry_run: bool, ib) -> dict:
    """Validate, repair, and merge one instrument's session files. Returns result dict."""
    from ib_insync import Contract as _IBContract

    result = {
        "severity": None,
        "action": None,
        "merge_success": None,
        "backup_written": False,
        "validation": None,
    }

    main_path     = DATA_DIR / main_name
    price_lo, price_hi = PRICE_BOUNDS[inst]
    session_files = sorted(DATA_DIR.glob(session_glob))
    now_et        = pd.Timestamp.now(tz="America/New_York")
    contract      = _IBContract(conId=int(conid), exchange="CME") if conid else None

    # ── No session files ─────────────────────────────────────────────────────
    if not session_files:
        if mode == "orchestrator-start" and ib is not None and conid:
            # Create a session file by gap-filling main[-1] → now
            fetched = gap_fill_to_now(ib, contract, main_path)
            if fetched.empty:
                result["action"] = "skip"
                result["reason"] = "no session file and gap fill returned no data"
                return result
            session_path = DATA_DIR / f"{inst}_1s_session_{now_et.strftime('%Y%m%d')}.parquet"
            if not dry_run:
                write_atomic(fetched, session_path)
            session_files = [session_path]
            result["action"] = "gap_fill_created_session"
            result["gap_fill_bars"] = len(fetched)
        else:
            result["action"] = "skip"
            result["reason"] = "no session file"
            return result

    # ── Process each session file ─────────────────────────────────────────────
    for session_path in session_files:
        df = _safe_read(session_path)

        expected_start = get_session_start_for_end_mode() if mode == "session-end" else None
        v = validate_session_df(df, price_lo, price_hi, expected_session_start=expected_start)
        severity = v["severity"]
        result["validation"] = v

        # Overnight coverage escalation (session-end only)
        if mode == "session-end" and v["late_start_hours"] > 2.0 and severity in ("ok", "minor", "major"):
            severity = "critical"
            result["reason"] = (
                f"overnight data missing: session started "
                f"{v['late_start_hours']:.1f}h after CME open"
            )

        if severity in ("ok", "minor"):
            if result["action"] is None:
                result["action"] = "merge"

        elif severity == "major":
            result["action"] = "targeted_fill_then_merge"
            if not dry_run and ib is not None and conid:
                patched = targeted_fill(ib, contract, session_path, v["unexpected_gaps"], df)
                write_atomic(patched, session_path)

        elif severity == "critical":
            if mode == "session-end":
                result["action"] = "rebuild_then_merge"
                if not dry_run and ib is not None and conid:
                    session_start = get_session_start_for_end_mode()
                    rebuilt = rebuild_session(ib, contract, session_start, now_et)
                    if rebuilt.empty:
                        result["merge_success"] = False
                        result["reason"] = "rebuild returned no data"
                        return result
                    write_atomic(rebuilt, session_path)
            else:
                result["action"] = "gap_fill_then_merge"
                if not dry_run and ib is not None and conid:
                    fetched = gap_fill_to_now(ib, contract, main_path)
                    if fetched.empty:
                        result["merge_success"] = False
                        result["reason"] = "gap fill returned no data"
                        return result
                    write_atomic(fetched, session_path)

        result["severity"] = severity

    # Merge all repaired session files into main in one call
    if not dry_run:
        try:
            from data.parquet_maintenance import merge_session_1s_parquets
            merge_session_1s_parquets(DATA_DIR)
            result["merge_success"] = True
            result["backup_written"] = False
            if main_path.exists():
                backup_main(main_path)
                result["backup_written"] = True
            merged_df = _safe_read(main_path)
            if merged_df is not None and not merged_df.empty:
                result["merged_rows"] = len(merged_df)
        except Exception as exc:
            result["merge_success"] = False
            result["reason"] = str(exc)
    else:
        result["merge_success"] = None  # dry-run: not executed

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Validate, repair, and merge 1s session parquet files."
    )
    parser.add_argument(
        "--mode",
        choices=["session-end", "orchestrator-start"],
        required=True,
        help="session-end: full rebuild on critical; orchestrator-start: gap-fill only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate only; no IB calls, no disk writes",
    )
    args = parser.parse_args()

    report = {
        "mode": args.mode,
        "dry_run": args.dry_run,
        "instruments": {},
        "exit_code": 0,
    }
    exit_code = 0

    ib = None
    try:
        if not args.dry_run:
            from ib_insync import IB
            try:
                ib = IB()
                ib.connect(HOST, PORT, clientId=IB_CLIENT_ID)
            except Exception as exc:
                print(
                    f"[check] IB unavailable ({exc}) — merge will proceed without gap fill",
                    file=sys.stderr,
                )
                ib = None

        for inst, conid, main_name, session_glob in INSTRUMENTS:
            result = process_instrument(
                inst, conid, main_name, session_glob, args.mode, args.dry_run, ib
            )
            report["instruments"][inst] = result

            if result.get("action") not in (None, "skip"):
                exit_code = max(exit_code, 1)
            if result.get("merge_success") is False:
                exit_code = max(exit_code, 2)

    except Exception as exc:
        report["error"] = str(exc)
        exit_code = 3

    finally:
        if ib is not None:
            try:
                if ib.isConnected():
                    ib.disconnect()
            except Exception:
                pass

    report["exit_code"] = exit_code
    print(json.dumps(report, indent=2))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
