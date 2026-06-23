#!/usr/bin/env python
"""Verify IB realtime market data is flowing for MNQ/MES, then disconnect.

Used by the run-orchestrator maintenance-break cycle to confirm the CME feed is live
again after the 17:00-18:00 ET maintenance break, before the orchestrator is restarted
for the next session. It subscribes to realtime data, verifies a price tick exists, and
disconnects gracefully.

Connects on a dedicated client id (IB_VERIFY_CLIENT_ID, default 19 — distinct from the
gap-fill (17), pre-session, and automation (20/21) clients), subscribes to MNQ + MES via
reqMktData, waits up to --timeout seconds for a valid price tick on the required
instrument(s), prints a one-line status, and disconnects.

Exit codes:
    0  required data received (feed is live)
    2  connected but no data within the timeout (still in maintenance / market closed)
    3  could not connect / conids missing (e.g. IB Gateway mid-restart)

Run from the worktree root:
    uv run python .claude/skills/run-orchestrator/scripts/verify_ib_realtime.py --timeout 20
"""
import argparse
import math
import os
import sys
import time
from pathlib import Path

# scripts -> run-orchestrator -> skills -> .claude -> <project root>
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()


def _has_price(ticker) -> bool:
    """True if the ticker carries at least one real, positive price field."""
    for v in (
        getattr(ticker, "last", None),
        getattr(ticker, "bid", None),
        getattr(ticker, "ask", None),
        getattr(ticker, "close", None),
    ):
        if v is not None and not (isinstance(v, float) and math.isnan(v)) and v > 0:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify IB realtime data is flowing.")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="max seconds to wait for a price tick (default 30)")
    ap.add_argument("--require", choices=["both", "mnq", "any"], default="both",
                    help="which instruments must show data (default both)")
    args = ap.parse_args()

    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", "4002"))
    cid = int(os.environ.get("IB_VERIFY_CLIENT_ID", "19"))
    mnq_conid = os.environ.get("MNQ_CONID")
    mes_conid = os.environ.get("MES_CONID")
    if not mnq_conid or not mes_conid:
        print("VERIFY: MNQ_CONID/MES_CONID not set — cannot verify", flush=True)
        return 3

    from ib_insync import IB, Future

    ib = IB()
    try:
        ib.connect(host, port, clientId=cid, timeout=15)
    except Exception as exc:
        print(f"VERIFY: IB connect failed ({exc}) — likely mid-maintenance restart", flush=True)
        return 3

    mnq = Future(conId=int(mnq_conid), exchange="CME")
    mes = Future(conId=int(mes_conid), exchange="CME")
    got = {"MNQ": False, "MES": False}
    try:
        mnq_t = ib.reqMktData(mnq, "", False, False)
        mes_t = ib.reqMktData(mes, "", False, False)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            ib.sleep(0.5)  # pumps the ib_insync event loop so ticker fields update
            got["MNQ"] = got["MNQ"] or _has_price(mnq_t)
            got["MES"] = got["MES"] or _has_price(mes_t)
            if args.require == "both" and got["MNQ"] and got["MES"]:
                break
            if args.require == "mnq" and got["MNQ"]:
                break
            if args.require == "any" and (got["MNQ"] or got["MES"]):
                break
    finally:
        for c in (mnq, mes):
            try:
                ib.cancelMktData(c)
            except Exception:
                pass
        try:
            ib.disconnect()
        except Exception:
            pass

    if args.require == "both":
        ok = got["MNQ"] and got["MES"]
    elif args.require == "mnq":
        ok = got["MNQ"]
    else:
        ok = got["MNQ"] or got["MES"]

    print(
        f"VERIFY: MNQ={'live' if got['MNQ'] else 'none'} "
        f"MES={'live' if got['MES'] else 'none'} require={args.require} "
        f"-> {'DATA RECEIVED' if ok else 'NO DATA'}",
        flush=True,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
