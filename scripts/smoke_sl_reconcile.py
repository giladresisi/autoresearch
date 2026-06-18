#!/usr/bin/env python3
# scripts/smoke_sl_reconcile.py
#
# ============================================================================
#  WARNING: THIS SCRIPT PLACES REAL, LIVE BROKER ORDERS.
#  IT IS USER-OPERATED / SUPERVISED ONLY. NEVER run it from an agent or
#  unattended. It sends small, short-lived MNQ orders to the live account and
#  ALWAYS flattens at the end — but you MUST watch Tradovate while it runs.
# ============================================================================
#
# Purpose (GIL-36): resolve the OPEN QUESTION behind the corrective seam —
# does a PMT `update_sl=True` actually make a protective stop APPEAR at the
# broker when the original SL was rejected (none rests)?  That can only be
# proven against the live broker, so this one-time supervised smoke test is
# required before relying on the reconciler in a real session.
#
# Procedure (mirrors the 2026-06-17 10:11 case):
#   1. Send a small MARKET entry with a protective S/L deliberately on the
#      WRONG side of the market (for a long, an S/L a few ticks ABOVE the
#      market) so the broker FILLS the entry but REJECTS only the S/L leg.
#   2. Run the reconcile corrective via the seam
#      (broker_recon.reconcile.place_protective_stop) with a VALID corrective
#      stop below the market.
#   3. The USER inspects Tradovate and decides PASS/FAIL — is a valid
#      protective stop now resting at the corrective price?
#        PASS → keep the default seam (update_sl).
#        FAIL → switch the seam to the fresh-standalone-STP fallback and re-run.
#   4. Regardless of outcome, a CLOSE/FLATTEN is sent so nothing is left open.
#
# Run (only when you are watching the live account):
#   LIVE_TRADING=true python scripts/smoke_sl_reconcile.py --confirm-live

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required acknowledgement that this places REAL live orders.",
    )
    ap.add_argument("--ticks", type=int, default=8,
                    help="Wrong-side SL distance in ticks above the market (default 8).")
    ap.add_argument("--corrective-ticks", type=int, default=80,
                    help="Valid corrective stop distance in ticks below the market (default 80).")
    args = ap.parse_args()

    if not args.confirm_live:
        print("REFUSING: pass --confirm-live to acknowledge this sends REAL live orders.")
        return 2
    if os.getenv("LIVE_TRADING", "false").lower() != "true":
        print("REFUSING: set LIVE_TRADING=true to run the live smoke test.")
        return 2

    import live_orders
    import stop_utils
    from broker_recon import reconcile

    tick = 0.25
    px = live_orders._current_price()
    if not px or px <= 0.0:
        print("No current market price available — is the orchestrator/feed running? Aborting.")
        return 1

    direction = "long"
    # Step 1: WRONG-side SL (above the market for a long) → broker rejects the SL leg.
    #
    # IMPORTANT: we must send the wrong-side SL DIRECTLY through the executor, NOT via
    # live_orders.place_market_entry — that path now runs the preventive re-anchor
    # (stop_utils.valid_stop_for_fill), which would silently correct the deliberately
    # wrong-side SL to a valid below-market stop, so the broker would ACCEPT it and the
    # rejection this test exists to reproduce would never happen. Bypassing the preventive
    # layer here is the whole point of step 1; the preventive layer is exercised separately
    # by its own unit tests.
    wrong_sl = round(px + args.ticks * tick, 2)
    print(f"[smoke] market={px}  sending LONG market entry with WRONG-side SL={wrong_sl} "
          f"(above market → expect SL leg rejected, entry filled; PREVENTIVE LAYER BYPASSED)")
    try:
        live_orders._executor.place_entry(
            {"direction": direction, "entry_price": 0.0, "stop_price": wrong_sl}, None)
        time.sleep(5)

        # Step 2: corrective — a VALID stop below the market via the seam.
        intended_entry = round(px, 2)
        intended_stop = round(px - args.corrective_ticks * tick, 2)
        corrective = stop_utils.valid_stop_for_fill(
            direction, px, intended_stop, intended_entry)
        print(f"[smoke] running corrective via seam: place_protective_stop("
              f"{direction}, {corrective}) [intended_stop={intended_stop}]")
        reconcile.place_protective_stop(direction, corrective, reason="smoke-reconcile")
        time.sleep(3)

        # Step 3: USER decision.
        print("\n" + "=" * 70)
        print("INSPECT TRADOVATE NOW:")
        print(f"  Is there a WORKING protective SELL-stop resting at ~{corrective}?")
        print("    YES → PASS: keep the default seam (update_sl).")
        print("    NO  → FAIL: switch the seam to the fresh-standalone-STP fallback,")
        print("          then re-run this smoke test.")
        print("=" * 70 + "\n")
    finally:
        # Step 4: ALWAYS flatten so nothing is left open.
        print("[smoke] flattening the test position (always runs).")
        try:
            live_orders.close_position(0.0, reason="smoke-flatten")
        except Exception as exc:
            print(f"[smoke] WARNING: flatten failed — MANUALLY FLATTEN ON TRADOVATE NOW: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
