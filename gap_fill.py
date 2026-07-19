"""gap_fill.py — Standalone IB gap-fill up to now (1s + 1m).

Reuses the exact backfill the orchestrator runs at signal-mode startup:
``IbRealtimeSource.gap_fill()`` (1s via ``_gap_fill_1s_ib`` + 1m via
``gap_fill_1m_ib``), without opening live real-time subscriptions.

Invoked by ``orchestrator.main`` (pre-session IB-reachability check) and by
``trade.py gap-fill`` for a fill-only pass when the orchestrator is NOT running.

Requires ``MNQ_CONID``/``MES_CONID`` (and optionally ``IB_HOST``/``IB_PORT``,
``PRE_SESSION_IB_CLIENT_ID``) in the environment. Callers that load config from a
``.env`` file must call ``load_dotenv()`` before invoking ``gap_fill_until_now``.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import paths


def check_ib_reachable() -> None:
    """TCP-probe IB Gateway. Print an alert and exit the process if unreachable."""
    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", "4002"))
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except OSError:
        print(
            f"[gap_fill] FATAL: IB Gateway not reachable at {host}:{port} — "
            "open TWS / IB Gateway and retry. Exiting.",
            flush=True,
        )
        sys.exit(1)


def gap_fill_until_now(
    bar_data_dir: Path | None = None,
    *,
    check_reachable: bool = True,
    merge_sessions: bool = True,
) -> None:
    """Backfill the main 1s and 1m parquets from IB up to now.

    Same mechanism the orchestrator runs at signal-mode startup. Resolves
    ``bar_data_dir`` to ``paths.general_live_dir()`` (the shared global bar folder)
    when not given.

    check_reachable: TCP-probe IB Gateway first; exit if it is unreachable.
    merge_sessions:  fold any leftover per-session 1s parquet back into the main 1s
                     file (crash recovery) before filling — a no-op when none exists.

    Skips gracefully (no source constructed) when MNQ_CONID/MES_CONID are absent.
    """
    bar_data_dir = bar_data_dir or paths.general_live_dir()

    if check_reachable:
        check_ib_reachable()

    if merge_sessions:
        try:
            from data.parquet_maintenance import merge_session_1s_parquets
            merge_session_1s_parquets(bar_data_dir)
        except Exception as exc:
            print(
                f"[gap_fill] WARNING: session 1s merge (crash recovery) failed: {exc}",
                flush=True,
            )

    mnq_conid = os.environ.get("MNQ_CONID")
    mes_conid = os.environ.get("MES_CONID")
    if not mnq_conid or not mes_conid:
        print("[gap_fill] MNQ_CONID/MES_CONID not set — cannot gap-fill", flush=True)
        return

    from data.ib_realtime import IbRealtimeSource

    source = IbRealtimeSource(
        host=os.environ.get("IB_HOST", "127.0.0.1"),
        port=int(os.environ.get("IB_PORT", "4002")),
        client_id=int(os.environ.get("PRE_SESSION_IB_CLIENT_ID", "10")),
        mnq_conid=mnq_conid,
        mes_conid=mes_conid,
        bar_data_dir=bar_data_dir,
        on_bar=lambda *_: None,  # fill-only: no live subscriptions opened
    )
    from data.ib_realtime import run_gap_fill_with_retries
    run_gap_fill_with_retries(source.gap_fill, bar_data_dir)

    # The forward fill only extends from each parquet's last bar — interior holes
    # (e.g. a skipped throttled window from an older run) survive it indefinitely,
    # and IB's ~6-month 1s history horizon eventually makes them unrepairable.
    # Offline is the right place to spend budget on them: no session is imminent.
    try:
        repair_interior_1s_gaps(bar_data_dir)
    except Exception as exc:
        print(f"[gap_fill] WARNING: interior 1s gap repair failed: {exc}", flush=True)


# Max repair windows per file per run — bounds IB budget spend; anything beyond is
# logged and left for the next run.
_MAX_REPAIR_WINDOWS = 8


def find_interior_1s_gaps(df, threshold_s: float = 120.0) -> list:
    """Return [(start_ts, end_ts)] for index gaps > threshold_s that contain at least
    one open-market minute (data.trading_calendar) — i.e. repairable data holes, not
    maintenance breaks / weekends / holiday closures."""
    import pandas as pd
    from data.trading_calendar import is_market_closed

    if df.empty:
        return []
    diffs = df.index.to_series().diff().dt.total_seconds()
    holes = []
    for ts, s in diffs[diffs > threshold_s].items():
        start = ts - pd.Timedelta(seconds=s)
        probe = pd.date_range(start + pd.Timedelta(minutes=1), ts - pd.Timedelta(seconds=1),
                              freq="1min")
        if any(not is_market_closed(p) for p in probe):
            holes.append((start, ts))
    return holes


def _connect_ib():
    """Open the fill-only ib_insync connection (client id offset like the 1s fill)."""
    from ib_insync import IB
    ib = IB()
    ib.connect(
        os.environ.get("IB_HOST", "127.0.0.1"),
        int(os.environ.get("IB_PORT", "4002")),
        clientId=int(os.environ.get("PRE_SESSION_IB_CLIENT_ID", "10")) + 1,
    )
    return ib


def repair_interior_1s_gaps(bar_data_dir: Path | None = None) -> None:
    """Detect and backfill interior open-market holes in the main 1s parquets.

    Scans MNQ_1s/MES_1s for gaps that trading-calendar classification marks as real
    data holes, fetches each window via parquet_maintenance._fetch_gap_chunked, and
    merges the bars in with an atomic tmp+rename write. Connects to IB only when at
    least one hole exists.
    """
    import pandas as pd
    from uuid import uuid4

    bar_data_dir = bar_data_dir or paths.general_live_dir()
    files = [
        ("MNQ_1s.parquet", os.environ.get("MNQ_CONID")),
        ("MES_1s.parquet", os.environ.get("MES_CONID")),
    ]

    work = []
    for name, conid in files:
        path = bar_data_dir / name
        if not conid or not path.exists():
            continue
        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            print(f"[gap_fill] interior repair: could not read {name}: {exc}", flush=True)
            continue
        holes = find_interior_1s_gaps(df)
        if len(holes) > _MAX_REPAIR_WINDOWS:
            print(
                f"[gap_fill] interior repair: {name} has {len(holes)} holes — repairing "
                f"first {_MAX_REPAIR_WINDOWS} this run, rest deferred",
                flush=True,
            )
            holes = holes[:_MAX_REPAIR_WINDOWS]
        if holes:
            work.append((name, conid, path, df, holes))

    if not work:
        return

    from ib_insync import Contract
    import data.parquet_maintenance as _pm

    ib = _connect_ib()
    try:
        for name, conid, path, df, holes in work:
            contract = Contract(conId=int(conid), exchange="CME")
            fetched = []
            for start, end in holes:
                gap_df, ok = _pm._fetch_gap_chunked(ib, contract, start, end)
                print(
                    f"[gap_fill] interior repair: {name} "
                    f"{start.strftime('%m-%d %H:%M')} -> {end.strftime('%m-%d %H:%M')}: "
                    f"{len(gap_df)} bars (success={ok})",
                    flush=True,
                )
                if not gap_df.empty:
                    fetched.append(gap_df)
            if not fetched:
                continue
            combined = pd.concat([df] + fetched).sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
            tmp = path.with_name(f"{path.stem}.{uuid4().hex}.parquet.tmp")
            combined.to_parquet(tmp, use_dictionary=False)
            os.replace(tmp, path)
            print(
                f"[gap_fill] interior repair: {name} merged, rows {len(df)} -> {len(combined)}",
                flush=True,
            )
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass
