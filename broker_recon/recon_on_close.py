# broker_recon/recon_on_close.py
# Reconcile-on-close core (GIL-42): a PURE decision function + an impure, live-only applier.
#
# The position model is bar-assumed (PMT_FILLS_URL was dropped): when the IB bar feed prints
# a level Tradovate never traded through, the strategy records a PHANTOM close, marks itself
# flat, and re-enters while the broker is still in the position. New entries STACK; the FIFO
# match of a stale stop fill to the wrong lot produces an outsized realized loss (verified
# -$428 on 2026-06-22). This module is invoked SYNCHRONOUSLY inside live_orders.dispatch()
# BEFORE every close (market-close / stop-exit / stopped-out), so by the time any re-entry
# fires the broker is reconciled.
#
# `decide_correction` is pure (no I/O) and exhaustively unit-tested. `apply_correction` is
# impure + live-only and fails SAFE: any failure logs (ASCII-only) and returns False so the
# pending close proceeds exactly as the strategy intended. It NEVER sends a broker order
# (detection + file-state correction only).

from __future__ import annotations


def _norm_dir(d: str) -> str:
    """Map a position/broker direction to the canonical long/short/flat vocabulary."""
    d = (d or "").strip().lower()
    if d in ("up", "long"):
        return "long"
    if d in ("down", "short"):
        return "short"
    return "flat"


def decide_correction(strat_active: dict, broker: dict | None) -> dict:
    """Decide how to reconcile the strategy's `active` block against broker truth. PURE.

    `strat_active` is position.json["active"] (``{}`` when the strategy believes it is flat).
    `broker` is the broker_state snapshot (or None == "unknown").

    Returns one of:
      - {"action": "noop", "reason": ...}
      - {"action": "adopt", "direction", "size", "avg_entry", "stop"}
      - {"action": "suppress_close", "reason": "broker-flat"}
      - {"action": "resize", "size": M}
    """
    if broker is None:
        # Unknown broker state -> trust the strategy; the preventive layers remain primary.
        return {"action": "noop", "reason": "broker-unknown"}

    strat_dir = _norm_dir(strat_active.get("direction", "")) if strat_active else "flat"
    broker_dir = _norm_dir(broker.get("direction", "flat"))
    broker_size = abs(int(broker.get("net_position", 0) or 0))
    if broker_dir == "flat" or broker_size == 0:
        broker_dir = "flat"

    # 1) Strategy flat.
    if strat_dir == "flat":
        if broker_dir == "flat":
            return {"action": "noop", "reason": "confirmed-flat"}
        # Phantom close: strategy thinks it's flat but the broker still holds a position.
        return {
            "action": "adopt",
            "direction": broker_dir,
            "size": broker_size,
            "avg_entry": float(broker.get("avg_entry", 0.0) or 0.0),
            "stop": broker.get("stop_price"),
        }

    # 2) Strategy non-flat.
    if broker_dir == "flat":
        # Strategy thinks it holds a position the broker no longer has -> nothing to close.
        return {"action": "suppress_close", "reason": "broker-flat"}

    if broker_dir != strat_dir:
        # Direction divergence -> adopt broker truth (the broker is the source of record).
        return {
            "action": "adopt",
            "direction": broker_dir,
            "size": broker_size,
            "avg_entry": float(broker.get("avg_entry", 0.0) or 0.0),
            "stop": broker.get("stop_price"),
        }

    # Same direction, possibly different size.
    strat_size = abs(int(strat_active.get("contracts", 0) or 0))
    if strat_size != broker_size:
        return {"action": "resize", "size": broker_size}

    return {"action": "noop", "reason": "confirmed-match"}


def apply_correction(decision: dict, *, intended_close_event: dict) -> bool:
    """Apply a `decide_correction` result to live position.json. Impure, LIVE-ONLY.

    Returns True iff the caller must SUPPRESS the pending broker close (adopt / suppress_close);
    False for resize / noop / any failure. NEVER sends a broker order. Fails safe: on any
    exception it logs (ASCII) and returns False so the close proceeds as the strategy intended.
    """
    try:
        return _apply(decision, intended_close_event)
    except Exception as exc:  # pragma: no cover - defensive; must never crash the loop
        print(f"[broker_recon.recon_on_close] apply failed (ignored): {exc}", flush=True)
        return False


def _now_et_iso() -> str:
    import datetime
    import zoneinfo
    return datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York")).isoformat()


def _apply(decision: dict, intended_close_event: dict) -> bool:
    import live_orders
    import smt_state
    import stop_utils

    action = decision.get("action", "noop")

    if action == "noop":
        return False

    if action == "resize":
        pos = live_orders.get_position()
        active = pos.get("active") or {}
        if active:
            active["contracts"] = int(decision.get("size", active.get("contracts", 0)))
            pos["active"] = active
            live_orders._save_pos(pos)
            live_orders._log({
                "kind": "recon-resize",
                "time": _now_et_iso(),
                "size": int(decision.get("size", 0)),
                "reason": "broker-size-mismatch",
            })
        return False

    if action == "suppress_close":
        # The strategy holds a position the broker no longer has -> mirror close_position's
        # state clears WITHOUT dispatching a broker order (there is nothing to sell into).
        pos = live_orders.get_position()
        pos["active"] = {}
        pos["stop_entry"] = ""
        pos["stop_direction"] = ""
        pos["conf_bar_entry"] = {}
        pos["conf_bar_exit"] = {}
        live_orders._save_pos(pos)
        live_orders._log({
            "kind": "recon-flat",
            "time": _now_et_iso(),
            "reason": decision.get("reason", "broker-flat"),
        })
        return True  # suppress the close-MKT: no broker position to flatten

    if action == "adopt":
        now_iso = _now_et_iso()
        direction = _norm_dir(decision.get("direction", ""))
        size = int(decision.get("size", 0) or 0)
        avg_entry = float(decision.get("avg_entry", 0.0) or 0.0)
        broker_stop = decision.get("stop")

        pos = live_orders.get_position()
        # The working broker stop is authoritative; when it is unknown, floor a MIN-distance
        # protective stop off the adopted fill. NOTE the arg contract of valid_stop_for_fill
        # (direction, fill, intended_stop, intended_entry): risk = max(|stop-entry|, MIN), so
        # passing avg_entry for BOTH intended_stop and intended_entry yields risk == MIN and a
        # stop at fill -/+ MIN_STOP_DISTANCE — a real protective level. (Passing 0.0 here would
        # blow risk up to ~avg_entry and collapse the stop onto the fill — a degenerate stop.)
        stop = None
        if broker_stop is not None:
            try:
                stop = float(broker_stop)
            except (TypeError, ValueError):
                stop = None
        if stop is None:
            stop = stop_utils.valid_stop_for_fill(direction, avg_entry, avg_entry, avg_entry)
            print(f"[broker_recon.recon_on_close] adopt: broker stop unknown -> "
                  f"synthesized MIN-distance stop {stop} for {direction} @ {avg_entry}",
                  flush=True)

        active = {
            "time": now_iso,
            "fill_price": avg_entry,
            "direction": direction,
            "stop": stop,
            "contracts": size,
            "cautious": "no",
            "source": "recon-adopt",
        }
        # Revert the phantom close's side effects: only stopped-out / same-bar-stop closes
        # increment failed_entries / cautious_dist_shrinks (session_pipeline.py:1545-1546).
        close_kind = (intended_close_event or {}).get("kind", "")
        if close_kind == "stopped-out":
            pos["failed_entries"] = max(0, int(pos.get("failed_entries", 0) or 0) - 1)
            pos["cautious_dist_shrinks"] = max(
                0, int(pos.get("cautious_dist_shrinks", 0) or 0) - 1)
        # Re-arm the cautious ladder from the live hypothesis (immutable for the trade).
        hyp = smt_state.load_hypothesis()
        smt_state.freeze_active_mgmt(active, direction, hyp)
        pos["active"] = active
        # Disarm any pending re-entry force-eval on the pipeline (consumed in Step 6).
        pos["recon_suppress_force_entry"] = True
        live_orders._save_pos(pos)
        live_orders._log({
            "kind": "recon-adopt",
            "time": now_iso,
            "direction": direction,
            "fill_price": avg_entry,
            "size": size,
            "stop": stop,
            "reason": "phantom-close-broker-holds-position",
        })
        return True  # suppress the close: there IS a position; the close was a phantom

    return False
