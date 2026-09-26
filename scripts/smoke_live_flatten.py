#!/usr/bin/env python3
# scripts/smoke_live_flatten.py
#
# ============================================================================
#  WARNING: WITH --live THIS SCRIPT PLACES REAL, LIVE BROKER ORDERS.
#  IT IS USER-OPERATED / SUPERVISED ONLY. NEVER run it from an agent or
#  unattended. --dry-run (the default) is safe to run any time -- it builds
#  every object and prints the two signals it WOULD emit, but never calls the
#  real emit, so nothing reaches PMT or the broker.
#
#  Run --live with the orchestrator RUNNING (it feeds the live price via
#  last_tick.json) but OUTSIDE the agent's 09:20-13:00 ET window -- e.g. at
#  08:45 ET, before the arm, when the watchdog does nothing yet. Inside the
#  window the watchdog would read this position as unexpected and kill the
#  day's plan, so the script refuses there. This script's own position is not
#  managed by the Executor -- YOU are the risk manager for the ~10 s it is open.
# ============================================================================
#
# Purpose (feat/live-flatten-exits): `MirroringOrderPort.flatten` is new --
# it lets O4 (`micro_smt_exit`) and plan 35 action B (`initial_opp_close`)
# close a live position instead of refusing. This script proves, against the
# REAL broker/PMT path with the operator watching, that an Executor-initiated
# `flatten` reaches the broker as ONE market close through the exact live
# chain:
#
#   MirroringOrderPort.flatten -> AgentDispatchPort.sink -> the SAME emit
#   `automation/main.py`'s `LiveAgentBridge._emit` uses
#   (`live_emit.emit_v2_signal` + `live_orders.dispatch`) -> the broker.
#
# It does NOT drive the Executor itself. `MirroringOrderPort.fill_market` /
# `.flatten` are called DIRECTLY here -- the same two calls the Executor
# makes (`_enter_by_market` / `_drive_micro_smt_exit` / `_initial_opp_close`)
# -- so this proves the PORT <-> DISPATCH <-> BROKER leg only. The Executor's
# own decision to call `flatten` (the flags, the gating, the per-position
# `micro_smt_exit_unwired` latch) is covered offline, in-process, by
# `agent/trader/test_executor_micro_smt.py` and
# `agent/trader/test_initial_target.py` -- not re-tested here.
#
# Procedure:
#   1. Refuse unless --i-am-watching is passed AND --live is passed (dry-run
#      needs neither) AND `now` (ET, via `orchestrator.scheduler.get_et_now`
#      -- NEVER the machine clock, which runs Bangkok time on this box) is on a
#      trading day, OUTSIDE the agent's window (`WINDOW_START_ET`..`WINDOW_END_ET`)
#      and the 16:55-18:00 session-end/maintenance window, and last_tick.json is <= 10 s old.
#   2. Refuse if position.json already shows an active position -- this
#      script must never stack onto, or silently adopt, an existing trade.
#   3. Build ONE fresh `OrderSim`, wrap it in a real `MirroringOrderPort`
#      whose sink is a real `AgentDispatchPort` (recording to a
#      SMOKE-SPECIFIC file, never a session's own `agent_dispatch.jsonl`),
#      whose `emit` is the exact pair `automation/main.py` wires live.
#   4. Send one `--contracts`-sized (default 1) MARKET entry via
#      `port.fill_market(...)`, sleep a few seconds, read position.json and
#      confirm it now shows the new position.
#   5. Call `port.flatten(now, price, kind="micro_smt_exit")`, sleep, confirm
#      position.json is cleared again and the ack was ok.
#   6. Print a PASS/FAIL summary.
#
# Run:
#   python scripts/smoke_live_flatten.py                     (dry-run; safe, any time)
#   LIVE_TRADING=true TRADING_CONTRACTS=1 python scripts/smoke_live_flatten.py \
#       --live --i-am-watching
#
# Read the dry-run's printed signals before ever passing --live.

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RECORD_FILE = "smoke_live_flatten_dispatch.jsonl"


def _refuse(reason: str) -> int:
    print("REFUSING: %s" % reason)
    return 2


def _window_check(now_et):
    """(hour, minute) bounds straight from the modules that define the agent's real
    trading window -- never re-typed here, so a future window change cannot silently
    desync this script from what it is meant to smoke-test."""
    from agent.trader.executor import WINDOW_END_ET
    from agent.trader.replay import WINDOW_START_ET
    from orchestrator.scheduler import is_trading_day

    if not is_trading_day(now_et.date()):
        return "today (%s) is not a trading day" % now_et.date()
    # Inside the agent's window the live watchdog would read this script's position as
    # `unexpected_active_position` and kill the day's plan; before the arm it does nothing.
    start = now_et.replace(hour=WINDOW_START_ET[0], minute=WINDOW_START_ET[1],
                           second=0, microsecond=0)
    end = now_et.replace(hour=WINDOW_END_ET[0], minute=WINDOW_END_ET[1],
                         second=0, microsecond=0)
    if start <= now_et <= end:
        return ("now (%s ET) is INSIDE the agent's trading window (%02d:%02d-%02d:%02d ET); "
                "run before the arm or after the window"
                % (now_et.strftime("%H:%M:%S"), *WINDOW_START_ET, *WINDOW_END_ET))
    from session_times import SESSION_CLOSE, SESSION_OPEN
    if SESSION_CLOSE <= now_et.time() < SESSION_OPEN:
        return ("now (%s ET) is the session-end / CME maintenance window (%s-%s ET)"
                % (now_et.strftime("%H:%M:%S"), SESSION_CLOSE.strftime("%H:%M"),
                   SESSION_OPEN.strftime("%H:%M")))
    return None


def _fresh_tick_check(now_et, max_age_s: float = 10.0):
    """The stop is computed from the current price, so it must be LIVE: the running
    orchestrator writes `last_tick.json` every second. A stale fallback price (the last
    parquet close) could put the stop on the wrong side of the market."""
    import paths
    import pandas as pd
    p = paths.general_live_dir() / "last_tick.json"
    try:
        ts = pd.Timestamp(json.loads(p.read_text(encoding="utf-8"))["time"])
    except Exception:
        return "no readable %s -- start the orchestrator first (it feeds the price)" % p
    if ts.tzinfo is None:
        ts = ts.tz_localize(now_et.tzinfo)
    age = (pd.Timestamp(now_et) - ts).total_seconds()
    if age > max_age_s:
        return ("last tick is %.0fs old -- start the orchestrator first (it feeds the "
                "price)" % age)
    return None


def _real_emit(sig: dict) -> None:
    """The exact pair `automation/main.py::LiveAgentBridge._emit` calls for an
    agent-sourced signal: a stdout log line, then the real dispatch to the broker via
    PMT. Imported at call time, like every other order-routing use in this codebase."""
    from live_emit import emit_v2_signal
    import live_orders
    emit_v2_signal(sig)
    live_orders.dispatch(sig)


def _dry_run_emit(collected: list):
    def _emit(sig: dict) -> None:
        collected.append(sig)
        print("[DRY-RUN] would emit: %s" % json.dumps(sig))
    return _emit


def _build_port(record_path: Path, emit, contracts: int):
    from agent.trader.order_port import MirroringOrderPort
    from agent.trader.order_sim import OrderSim
    from automation.agent_dispatch import AgentDispatchPort

    ctx = {"plan_id": "smoke_live_flatten", "mechanism": "smoke_live_flatten"}
    port_holder = {}

    def _context():
        return dict(ctx)

    dispatch_port = AgentDispatchPort(emit, recorder_path=record_path, contracts=contracts)
    port = MirroringOrderPort(OrderSim(dol=None), dispatch_port.sink, context=_context)
    port_holder["dispatch"] = dispatch_port
    return port, dispatch_port


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="Actually dispatch to the broker. Without it, this is a "
                         "no-op dry run regardless of the other flags.")
    ap.add_argument("--i-am-watching", action="store_true",
                    help="Required with --live: you are watching Tradovate right now.")
    ap.add_argument("--direction", choices=["long", "short"], default="long")
    ap.add_argument("--contracts", type=int, default=1)
    ap.add_argument("--stop-ticks", type=int, default=40,
                    help="Protective stop distance in ticks (default 40, MNQ tick 0.25).")
    ap.add_argument("--record-path", default=None,
                    help="Where to write this smoke test's OWN dispatch records "
                         "(default: scripts/%s, NEVER a session's agent_dispatch.jsonl)."
                         % RECORD_FILE)
    args = ap.parse_args()

    record_path = Path(args.record_path) if args.record_path else \
        Path(__file__).resolve().parent / RECORD_FILE

    if not args.live:
        print("[smoke] DRY RUN (pass --live --i-am-watching to actually trade).")
        print("[smoke] Building the real port/dispatch/emit chain, contracts=%d, "
              "direction=%s ..." % (args.contracts, args.direction))
        collected: list = []
        port, dispatch_port = _build_port(record_path, _dry_run_emit(collected),
                                          args.contracts)
        px = 20000.0          # placeholder: no live price feed is touched in a dry run
        stop = px - args.stop_ticks * 0.25 if args.direction == "long" \
            else px + args.stop_ticks * 0.25
        fill_ev = port.fill_market(_now_ts(), direction=args.direction.upper(),
                                   price=px, stop=stop, artifact_id="smoke_live_flatten")
        print("[DRY-RUN] fill_market returned: %s" % fill_ev)
        # The dry-run's own fake sink never acks "ok", so the mirror voids the fill and
        # there is no OPEN position left for a second flatten to act on -- exactly the
        # shape a real run would NOT have (a real sink acks ok). Print the close signal
        # that a WIRED flatten would build by reasoning about it directly instead of
        # forcing a fake ack through the port (which would misrepresent the real ack
        # path this script exists to prove).
        would_close = {"kind": "market-close", "direction": args.direction,
                      "price": px, "reason": "micro_smt_exit", "skip_recon": True,
                      "source": "agent"}
        print("[DRY-RUN] would ALSO emit (on a real flatten): %s"
              % json.dumps(would_close))
        print("\n[DRY-RUN] Nothing was sent. %d signal(s) printed above." % len(collected))
        return 0

    if not args.i_am_watching:
        return _refuse("pass --i-am-watching to acknowledge you are watching Tradovate "
                       "right now (--live sends REAL orders).")
    if os.getenv("LIVE_TRADING", "false").strip().lower() != "true":
        return _refuse("set LIVE_TRADING=true to run the live smoke test.")

    from orchestrator.scheduler import get_et_now
    now_et = get_et_now()
    reason = _window_check(now_et) or _fresh_tick_check(now_et)
    if reason:
        return _refuse(reason)

    import live_orders
    if live_orders.has_active_position():
        return _refuse("position.json already shows an active position -- flatten it "
                       "manually first; this script must never stack onto one.")

    configured = int(os.environ.get("TRADING_CONTRACTS", "0") or 0)
    if configured != args.contracts:
        return _refuse("TRADING_CONTRACTS=%r does not match --contracts=%d -- set them "
                       "equal so position.json's recorded size matches what this script "
                       "opens." % (os.environ.get("TRADING_CONTRACTS"), args.contracts))

    px = live_orders._current_price()
    if not px or px <= 0.0:
        return _refuse("no current market price available -- is the feed running?")

    print("=" * 70)
    print("[smoke] LIVE RUN. market=%.2f  direction=%s  contracts=%d"
          % (px, args.direction, args.contracts))
    print("[smoke] Will send ONE market entry, wait, verify position.json, then send "
          "ONE market close (kind=micro_smt_exit), wait, verify position.json is flat.")
    print("[smoke] Dispatch records: %s" % record_path)
    print("=" * 70)

    tick = 0.25
    stop = round(px - args.stop_ticks * tick, 2) if args.direction == "long" \
        else round(px + args.stop_ticks * tick, 2)
    port, dispatch_port = _build_port(record_path, _real_emit, args.contracts)

    ok = True
    try:
        fill_ev = port.fill_market(_now_ts(), direction=args.direction.upper(),
                                   price=px, stop=stop, artifact_id="smoke_live_flatten")
        print("[smoke] fill_market -> %s" % fill_ev)
        if fill_ev.get("kind") != "fill":
            print("[smoke] FAIL: the entry was voided/refused (kind=%s) -- see %s"
                  % (fill_ev.get("kind"), record_path))
            return 1
        time.sleep(5)
        pos = live_orders.get_position()
        if not pos.get("active"):
            print("[smoke] FAIL: position.json shows no active position after the entry.")
            return 1
        print("[smoke] position.json active: %s" % pos["active"])

        close_px = live_orders._current_price() or px
        close_ev = port.flatten(_now_ts(), close_px, kind="micro_smt_exit")
        print("[smoke] flatten -> %s" % close_ev)
        if close_ev is None:
            print("[smoke] FAIL: flatten saw nothing open (position was already gone).")
            return 1
        time.sleep(5)
        pos = live_orders.get_position()
        if pos.get("active"):
            print("[smoke] FAIL: position.json STILL shows an active position after "
                  "flatten -- MANUALLY FLATTEN ON TRADOVATE NOW: %s" % pos["active"])
            ok = False
        if port.external is not None:
            print("[smoke] FAIL: the port recorded an external/ack failure: %s"
                  % port.external)
            ok = False
    finally:
        # Belt-and-suspenders: if anything above left a position open, close it for
        # real regardless of what the port's own bookkeeping believes.
        try:
            if live_orders.has_active_position():
                print("[smoke] safety-net flatten (always runs if something is still open).")
                live_orders.close_position(0.0, reason="smoke-live-flatten-safety-net")
        except Exception as exc:
            print("[smoke] WARNING: safety-net flatten failed -- "
                 "MANUALLY FLATTEN ON TRADOVATE NOW: %s" % exc)
            ok = False

    print("=" * 70)
    print("[smoke] %s -- see %s for the full dispatch record."
          % ("PASS" if ok else "FAIL", record_path))
    print("=" * 70)
    return 0 if ok else 1


def _now_ts():
    from orchestrator.scheduler import get_et_now
    import pandas as pd
    return pd.Timestamp(get_et_now())


if __name__ == "__main__":
    raise SystemExit(main())
