# broker_recon/reconcile.py
# Broker↔strategy reconciliation: pure classification + daemon-thread orchestration (GIL-36).
#
# PickMyTrade exposes no fill-confirmation, so when Tradovate rejects an order leg the
# strategy never learns and broker↔strategy state diverges. This module reads the Tradovate
# orders blotter (via a READ-ONLY reader behind an interface) and corrects the two failure
# modes seen live on 2026-06-17:
#   SL_REJECTED   — entry filled, protective S/L leg rejected/missing -> place a valid stop.
#   ENTRY_REJECTED— the entry order itself rejected -> reconcile position.json to flat.
#
# All correction goes through PMT (live_orders) — the browser is detection-only and NEVER
# places orders. The orchestration runs on a daemon thread and never blocks or crashes the
# trading loop; classification is pure and unit-tested with fixture blotter rows.

from __future__ import annotations

import datetime
import threading
import time
import zoneinfo

import stop_utils

# ── Classification result constants ────────────────────────────────────────────
OK = "OK"
SL_REJECTED = "SL_REJECTED"
ENTRY_REJECTED = "ENTRY_REJECTED"

_ET = zoneinfo.ZoneInfo("America/New_York")

# Terminal "rejected/cancelled" statuses (lower-cased substring match against the blotter
# status text — Tradovate uses "Rejected", "Canceled"/"Cancelled", "Expired").
_REJECTED_STATUSES = ("reject", "cancel", "expire")
# Statuses meaning an order is live/resting at the broker.
_WORKING_STATUSES = ("working", "accepted", "pending", "queued")
_FILLED_STATUSES = ("filled", "fill", "complete")

# Price tolerance (points) for matching a blotter row to the strategy's entry/fill.
_PRICE_TOL = 5.0

# ── Per-position idempotency lock ──────────────────────────────────────────────
# Both maps are keyed by a per-entry position-key, so they grow by at most one entry per
# trade for the life of the process. A trading session places on the order of tens–low
# hundreds of entries, so the footprint is negligible and they are intentionally not pruned
# (pruning would risk dropping a key while a slow verify is still in flight). `_handled_keys`
# is read/written only while holding the per-key lock (see _reconcile_worker), so it needs no
# separate guard; `_locks_guard` only protects the lock-map setdefault.
_locks_guard = threading.Lock()
_position_locks: dict = {}      # position-key -> threading.Lock
_handled_keys: set = set()      # position-keys already corrected (one correction each)


def _position_key(entry: dict) -> tuple:
    """Stable key identifying a single entry occurrence (for the idempotency lock)."""
    return (
        str(entry.get("symbol", "")),
        str(entry.get("direction", "")),
        round(float(entry.get("intended_entry", 0.0) or 0.0), 2),
        str(entry.get("time", "")),
    )


def _status_is(status: str, group) -> bool:
    s = (status or "").strip().lower()
    return any(tok in s for tok in group)


def _side_for_direction(direction: str) -> str:
    """The entry order side for a direction ('buy' opens a long, 'sell' opens a short)."""
    return "buy" if direction in ("up", "long") else "sell"


def _opposite_side(direction: str) -> str:
    """The protective-stop side (a long is protected by a SELL stop below; short by a BUY stop)."""
    return "sell" if direction in ("up", "long") else "buy"


def _price_near(a, b, tol: float = _PRICE_TOL) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def _is_protective_stop_row(row: dict, direction: str, fill: float) -> bool:
    """True if `row` is a working protective stop on the correct side of `fill`."""
    side = (row.get("side") or "").strip().lower()
    typ = (row.get("type") or "").strip().lower()
    if _opposite_side(direction) != side:
        return False
    if "stop" not in typ and "stp" not in typ:
        return False
    if not _status_is(row.get("status", ""), _WORKING_STATUSES):
        return False
    # Correct side of the fill: a long's sell-stop must rest BELOW the fill; a short's
    # buy-stop ABOVE it.
    try:
        sp = float(row.get("stop_price") or row.get("price") or 0.0)
    except (TypeError, ValueError):
        return False
    if sp <= 0.0:
        return False
    if direction in ("up", "long"):
        return sp < fill
    return sp > fill


def classify(entry: dict, fill: float, orders: list) -> str:
    """Classify the broker state for an entry against the orders blotter.

    `entry` carries at least: direction, intended_entry, symbol.
    `fill`  is the assumed/observed fill price.
    `orders` is the list of blotter rows from the reader (each a dict — see reader.py).

    Returns one of OK / SL_REJECTED / ENTRY_REJECTED:
      - ENTRY_REJECTED: the entry order (matching side, ~price) is in a rejected/cancelled
        terminal status and there is NO filled entry -> the position does not exist.
      - SL_REJECTED:    the entry IS filled but no working protective stop rests on the
        correct side of the fill (the SL leg was rejected/absent).
      - OK:             entry filled AND a working protective stop rests correctly.
    """
    orders = orders or []
    direction = entry.get("direction", "")
    entry_side = _side_for_direction(direction)
    intended_entry = float(entry.get("intended_entry", 0.0) or 0.0)

    # Collect the entry-side rows that match this entry by price (an entry order is
    # buy-for-long / sell-for-short; the protective leg is the OPPOSITE side, which this
    # side filter already excludes). Match at ~the intended entry OR the fill price.
    entry_rows = []
    for row in orders:
        side = (row.get("side") or "").strip().lower()
        if side != entry_side:
            continue
        if _price_near(row.get("price", 0.0), intended_entry) or \
           _price_near(row.get("avg_fill", 0.0), fill) or \
           _price_near(row.get("price", 0.0), fill):
            entry_rows.append(row)

    entry_filled = any(_status_is(r.get("status", ""), _FILLED_STATUSES) for r in entry_rows)
    entry_rejected = (
        bool(entry_rows)
        and not entry_filled
        and all(_status_is(r.get("status", ""), _REJECTED_STATUSES) for r in entry_rows)
    )

    if entry_rejected:
        return ENTRY_REJECTED

    if entry_filled:
        if any(_is_protective_stop_row(r, direction, fill) for r in orders):
            return OK
        return SL_REJECTED

    # No matching entry row at all, or an ambiguous/working entry: treat as OK (nothing
    # actionable — the preventive layer is primary; we never act on an uncertain state).
    return OK


# ── Correction seam ─────────────────────────────────────────────────────────────

def place_protective_stop(direction: str, corrective: float,
                          reason: str = "reconcile-rejected-sl") -> None:
    """SEAM: place/repair the protective stop at `corrective` for an open position.

    DEFAULT implementation — the PMT update_sl path: live_orders.update_stop_loss dispatches
    a PMT `update_sl=True` alert (execution/pickmytrade.update_stop_loss) and sets
    position.json active.stop = corrective.

    OPEN QUESTION (resolved by scripts/smoke_sl_reconcile.py): PMT's `update_sl=True` is
    built to *replace* an existing SL; when the original SL was *rejected*, none rests, so
    update_sl may have nothing to modify and may not CREATE one. If the smoke test shows it
    does not create an SL, switch this seam to the FALLBACK below (a fresh standalone
    protective stop, e.g. a sell-STP for a long at `corrective`) — a new PMT order path plus
    a live_orders wrapper. The reconcile logic above is unchanged either way; only this call
    swaps.
    """
    import live_orders
    live_orders.update_stop_loss(corrective, reason=reason, direction=direction)

    # ----------------------------------------------------------------------------
    # FALLBACK (DO NOT enable until the smoke test selects it): place a FRESH standalone
    # protective stop instead of update_sl. Requires a new PickMyTradeExecutor method
    # (e.g. place_protective_stop_order) + a live_orders wrapper. Left here as a clearly
    # marked seam so wiring it in is a one-line swap.
    #
    #   import live_orders
    #   live_orders.place_fresh_protective_stop(direction, corrective, reason=reason)
    # ----------------------------------------------------------------------------


# ── comments.md note helper ─────────────────────────────────────────────────────

def _append_comment(note: str) -> None:
    """Append a timestamped `- ...` line to the live session's comments.md (best-effort)."""
    try:
        import paths
        session_date = _SESSION_DATE() or datetime.datetime.now(_ET).date().isoformat()
        path = paths.sessions_dir() / session_date / "comments.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(_ET).strftime("%H:%M:%S ET")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"- {ts} {note}\n")
    except Exception:  # pragma: no cover - a note must never break reconciliation
        pass


def _SESSION_DATE() -> str:
    try:
        import live_orders
        return live_orders._SESSION_DATE
    except Exception:  # pragma: no cover
        return ""


def _emit(event: dict) -> None:
    """Write a reconcile event through live_orders._log (same events.jsonl)."""
    import live_orders
    live_orders._log(event)


# ── Orchestration ───────────────────────────────────────────────────────────────

# Timing knobs (overridable for tests).
SETTLE_S = 3.0
POLL_INTERVAL_S = 2.0
POLL_CAP_S = 30.0


def _entry_still_active(entry: dict) -> bool:
    """Re-read position.json: True iff a matching active position is still open.

    Never act on a stale position — if the strategy already exited/flipped/changed, the
    `active` block is gone or now describes a different trade, so we SKIP.
    """
    import live_orders
    pos = live_orders.get_position()
    active = pos.get("active") or {}
    if not active:
        return False
    # Direction must still match (a flip means a different trade).
    if entry.get("direction") and active.get("direction") and \
            active.get("direction") != entry.get("direction"):
        return False
    return True


def _now_et_iso() -> str:
    return datetime.datetime.now(_ET).isoformat()


def reconcile_after_entry(entry: dict, fill: float, *, reader=None,
                          _sync: bool = False) -> None:
    """Spawn (or, with `_sync=True`, run inline for tests) the reconcile verify+correct flow.

    `entry` carries: direction, intended_entry, intended_stop, symbol, time.
    `fill`  is the assumed fill estimate.
    Never blocks or raises into the caller: the worker swallows all exceptions and the
    spawn itself is guarded by the caller.
    """
    if _sync:
        _reconcile_worker(entry, float(fill), reader)
        return
    t = threading.Thread(
        target=_reconcile_worker, args=(entry, float(fill), reader),
        name="broker-recon", daemon=True)
    t.start()


def _reconcile_worker(entry: dict, fill: float, reader) -> None:
    """The daemon-thread body. Settle -> poll blotter to terminal -> classify -> act.

    Idempotent per position (a module lock + handled-set), graceful on every failure.
    """
    key = _position_key(entry)
    # Per-position lock: prevent double-correction / concurrent verifies on rapid re-entries.
    with _locks_guard:
        lock = _position_locks.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        return  # another worker is already handling this exact position
    try:
        if key in _handled_keys:
            return
        try:
            _run_classification_and_act(entry, fill, reader)
        except Exception as exc:  # pragma: no cover - defensive; never crash the loop
            print(f"[broker_recon.reconcile] worker error (ignored): {exc}", flush=True)
    finally:
        lock.release()


def _run_classification_and_act(entry: dict, fill: float, reader) -> None:
    direction = entry.get("direction", "")
    intended_stop = float(entry.get("intended_stop", 0.0) or 0.0)
    intended_entry = float(entry.get("intended_entry", 0.0) or 0.0)
    symbol = entry.get("symbol", "")

    if reader is None or getattr(reader, "disabled", False):
        return  # no detection source -> degrade gracefully (preventive layer is primary)

    time.sleep(SETTLE_S)

    # Poll until the entry order reaches a terminal verdict (filled, or rejected) or cap.
    deadline = time.monotonic() + POLL_CAP_S
    verdict = OK
    orders: list = []
    avg_fill = fill
    while True:
        orders = reader.query_orders(symbol, None) or []
        verdict = classify(entry, fill, orders)
        # A definitive verdict (something to correct) ends the poll early.
        if verdict in (SL_REJECTED, ENTRY_REJECTED):
            avg_fill = _resolve_avg_fill(entry, fill, orders)
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_INTERVAL_S)

    if verdict == OK:
        return

    # Stale-position guard: re-read position.json at correction time.
    if not _entry_still_active(entry):
        return

    key = _position_key(entry)
    if verdict == SL_REJECTED:
        corrective = stop_utils.valid_stop_for_fill(
            direction, avg_fill, intended_stop, intended_entry)
        place_protective_stop(direction, corrective, reason="reconcile-rejected-sl")
        _emit({
            "kind": "reconcile-stop-placed",
            "time": _now_et_iso(),
            "direction": direction,
            "price": corrective,
            "fill": avg_fill,
            "intended_stop": intended_stop,
            "reason": "reconcile-rejected-sl",
        })
        _append_comment(
            f"reconcile: SL rejected (entry filled @ {avg_fill}) — placed protective "
            f"stop @ {corrective} (intended {intended_stop})")
        _handled_keys.add(key)
        return

    if verdict == ENTRY_REJECTED:
        _clear_position_to_flat()
        _emit({
            "kind": "reconcile-flat",
            "time": _now_et_iso(),
            "direction": direction,
            "entry_price": intended_entry,
            "reason": "reconcile-entry-rejected",
        })
        _append_comment(
            f"reconcile: entry rejected @ {intended_entry} — position.json cleared to flat")
        _handled_keys.add(key)
        return


def _resolve_avg_fill(entry: dict, fill: float, orders: list) -> float:
    """Prefer the broker's reported average fill on a filled entry row, else the estimate."""
    entry_side = _side_for_direction(entry.get("direction", ""))
    for row in orders or []:
        if (row.get("side") or "").strip().lower() != entry_side:
            continue
        if not _status_is(row.get("status", ""), _FILLED_STATUSES):
            continue
        try:
            af = float(row.get("avg_fill") or 0.0)
        except (TypeError, ValueError):
            af = 0.0
        if af > 0.0:
            return af
    return float(fill)


def _clear_position_to_flat() -> None:
    """File-only clear of position.json `active` (ENTRY_REJECTED) — NO broker order.

    Mirrors live_orders.close_position's state clears but dispatches nothing: there is no
    position at the broker to flatten (the entry was rejected).
    """
    import live_orders
    pos = live_orders.get_position()
    pos["active"] = {}
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos["conf_bar_entry"] = {}
    pos["conf_bar_exit"] = {}
    live_orders._save_pos(pos)


def _reset_state_for_tests() -> None:
    """Clear the module-level idempotency state (test helper only)."""
    with _locks_guard:
        _position_locks.clear()
        _handled_keys.clear()
