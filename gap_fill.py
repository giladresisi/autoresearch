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
    source.gap_fill()
