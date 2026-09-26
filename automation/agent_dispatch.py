# automation/agent_dispatch.py
# The OUTER half of the agent stack's order port (plan 38): simulated order events in,
# dispatcher signals out, and a per-bar watchdog over position.json.
"""The agent stack's orders, on their way to the real dispatcher.

`agent/trader/order_port.MirroringOrderPort` reports every event the brain's position
model produces to a `sink`. This module IS that sink, and it lives outside `agent/` on
purpose: nothing under `agent/` may name the order-routing module, and the wall clock is
allowed here and nowhere inside the bar loop.

Three jobs:

  * `sink(event)`     translate a simulated `fill` into ONE market entry and every close
                      (`stop_out` / `take_profit` / `mark`) into ONE market close, emit it,
                      then ACK by reading position.json — the only rendezvous the
                      dispatcher, the post-entry broker verify and a manual `trade.py`
                      order all share. Deduped on `(plan_id, seq)`.
  * `supervise(now)`  the fail-closed watchdog, once per live bar: a position change the
                      agent did not cause kills the plan; a trader error with a position
                      open flattens it and disarms the session.
  * the record        one JSON line per event in `<session>/agent_dispatch.jsonl`.

Market kinds ONLY. Every exit is a market close carrying `skip_recon`: the agent always
sends a real flatten, so its state and the broker's converge at each close, and the
synchronous pre-close broker reconcile (a headed-browser login, seconds long) must not
delay an exit. Resting-order kinds and the legacy stop kinds are never built here — a
source-level gate in `tests/test_agent_dispatch.py` greps this file for them.

Signals are plain floats, ISO strings and ASCII: the emit path `json.dumps` a signal
BEFORE dispatching it, so one `pd.Timestamp` would raise and the order would never leave.
"""
from __future__ import annotations

import datetime
import json
import os
import time

import pandas as pd

ENTRY_KIND = "market-entry"
CLOSE_KIND = "market-close"
SOURCE = "agent"
RECORD_FILE = "agent_dispatch.jsonl"

#: Simulated close kinds -> the reason the market close carries.
CLOSE_REASONS = {"stop_out": "stop_out", "take_profit": "take_profit",
                 "mark": "window_end",
                 # O4 (`agent/trader/micro_smt.py`), ADOPTED 2026-09-26, flag-gated ON
                 # by default but REPLAY-ONLY: the live `MirroringOrderPort` has no
                 # `flatten`, so `_drive_micro_smt_exit` refuses before this mapping is
                 # ever exercised in live. Kept here so the mapping is ready the day
                 # that live wiring lands. Without this entry `_signal` returns None for
                 # it and `_sink` acks `untranslatable_event` — the simulation closes
                 # the position but no close order ever reaches the broker. Found, not
                 # assumed: checked by reading this module while implementing O4.
                 "micro_smt_exit": "micro_smt_exit"}
_DIRECTIONS = {"UP": "up", "LONG": "up", "DOWN": "down", "SHORT": "down"}


def refusal_reason(environ=None, *, pipeline=None) -> "str | None":
    """Why the agent must NOT own the dispatcher in this process, or None.

    Config preconditions only (plan 38 D16). A refusal never falls back to the legacy
    brain: the caller keeps it dark and runs with NO brain emitting."""
    env = os.environ if environ is None else environ
    if str(env.get("ACT_AI_MODE", "")).strip().lower() == "primary":
        return "ACT_AI_MODE=primary (a second brain would own the same dispatcher)"
    pipe = pipeline if pipeline is not None else env.get("SMT_PIPELINE", "v1")
    if str(pipe).strip().lower() != "v2":
        return "SMT_PIPELINE is not v2 (the agent runs on the v2 session pipeline only)"
    if str(env.get("FORCE_RESET", "")).strip().lower() == "true":
        return "FORCE_RESET=true (it wipes position.json, the watchdog's only witness)"
    return None


def refused_line(reason) -> str:
    """The ONE stdout line a refused start prints. ASCII: the relay's stdout is cp1252."""
    text = str(reason).encode("ascii", "replace").decode("ascii")
    return "[AGENT-LIVE] REFUSED: %s" % " ".join(text.split())


def _iso(ts):
    if ts is None:
        return None
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def _jsonable(event: dict) -> dict:
    out = {}
    for key, val in (event or {}).items():
        if val is None or isinstance(val, (bool, int, float, str)):
            out[key] = val
        else:
            out[key] = _iso(val)
    return out


def _default_active() -> dict:
    # Imported at CALL time: importing the order-routing module constructs its executor,
    # and nothing that merely imports this file may do that.
    import live_orders
    return dict(live_orders._load_pos().get("active") or {})


def _configured_contracts() -> int:
    return int(os.environ.get("TRADING_CONTRACTS", "2"))


class AgentDispatchPort:
    def __init__(self, emit, *, recorder_path, active_reader=None, contracts=None,
                 window_end=None) -> None:
        """`emit(sig)` is the dispatcher's emit. `active_reader()` returns position.json's
        `active` dict — injected by tests, the order module's own loader otherwise.
        `window_end` is an `(hour, minute)` pair; the Executor's constant by default."""
        self._emit = emit
        self._path = str(recorder_path)
        self._read_active = active_reader if active_reader is not None else _default_active
        self._contracts = int(contracts) if contracts is not None \
            else _configured_contracts()
        self._window_end = tuple(window_end) if window_end is not None else None
        self._acks: dict = {}
        # Whether the agent has SENT an entry this session, acked or not. A kill on such
        # a day may leave a broker position nobody manages; the window end flattens it.
        self._entry_sent = False
        self._stood_down = None          # why the watchdog stopped: "killed" | "disarmed"
        self._stood_down_at = None
        self._window_close_sent = False

    # -- the sink --------------------------------------------------------------- #

    def sink(self, event: dict) -> dict:
        """Never raises. `{"ok": bool, "reason": str}`, plus `voided` when an entry was
        refused by the dispatcher and the simulated fill should simply be undone."""
        try:
            return self._sink(event)
        except Exception as exc:
            ack = {"ok": False, "reason": "sink_error: %s" % type(exc).__name__}
            self._record(event, None, ack, None, None)
            return ack

    def _sink(self, event: dict) -> dict:
        key = (event.get("plan_id"), event.get("seq"))
        if key in self._acks:
            self._record(event, None, self._acks[key], None, "duplicate")
            return self._acks[key]

        sig = self._signal(event)
        if sig is None:
            ack = {"ok": False, "reason": "untranslatable_event"}
            self._acks[key] = ack
            self._record(event, None, ack, None, "untranslatable")
            return ack

        is_entry = sig["kind"] == ENTRY_KIND
        if is_entry:
            self._entry_sent = True
        started = time.perf_counter()
        error = None
        try:
            self._emit(sig)
        except Exception as exc:
            error = "emit_raised: %s" % type(exc).__name__
        dispatch_ms = round((time.perf_counter() - started) * 1000.0, 3)

        ack = {"ok": False, "reason": error} if error else self._ack(is_entry)
        self._acks[key] = ack
        self._record(event, sig, ack, dispatch_ms, None)
        return ack

    def _signal(self, event: dict) -> "dict | None":
        kind = event.get("kind")
        price = event.get("price")
        if price is None:
            return None
        base = {"time": _iso(event.get("time")), "source": SOURCE,
                "plan_id": event.get("plan_id"), "mechanism": event.get("mechanism"),
                "seq": event.get("seq")}
        if kind == "fill":
            direction = _DIRECTIONS.get(str(event.get("direction") or "").upper())
            if direction is None or event.get("stop") is None:
                return None
            sig = {"kind": ENTRY_KIND, "direction": direction, "price": float(price),
                   "stop": float(event["stop"]), "flatten_first": False}
        elif kind in CLOSE_REASONS:
            sig = {"kind": CLOSE_KIND, "price": float(price),
                   "reason": CLOSE_REASONS[kind], "skip_recon": True}
        else:
            return None
        sig.update(base)
        json.dumps(sig).encode("ascii")          # fail HERE, not inside the emit path
        return sig

    def _ack(self, is_entry: bool) -> dict:
        active = self._read_active()
        if is_entry:
            if active:
                return {"ok": True, "reason": ""}
            # Paused, a suppress sentinel, or the window gate: the dispatcher declined
            # and wrote nothing. Nothing is at the broker, so the fill is simply undone.
            return {"ok": False, "reason": "entry_refused", "voided": True}
        if not active:
            return {"ok": True, "reason": ""}
        if active.get("source") == "recon-adopt":
            return {"ok": False, "reason": "recon_adopt"}
        return {"ok": False, "reason": "close_not_confirmed"}

    # -- the watchdog ----------------------------------------------------------- #

    def supervise(self, now, graft) -> None:
        """Once per live bar, AFTER the pipeline has run it. Never raises."""
        try:
            self._supervise(now, graft)
        except Exception as exc:
            self._record({"kind": "supervise_error", "time": now,
                          "error": "%s: %s" % (type(exc).__name__, exc)},
                         None, None, None, None)

    def _supervise(self, now, graft) -> None:
        if graft is None:
            return
        if self._stood_down is not None:
            self._close_at_window_end(now)
            return

        health = graft.health() or {}
        error = health.get("last_error") or health.get("order_error")
        if error:
            # D11. The trader swallowed an exception: whatever it holds is unmanaged
            # from here. One flatten if it holds anything, then stand the session down.
            position = graft.position_view()
            if position:
                self._emit_close(now, "agent_error")
            graft.disarm(now, "agent_error")
            self._stand_down("disarmed", now)
            self._window_close_sent = True      # the flatten above already went out
            return

        if graft.plan() is None or now >= self._window_end_ts(now):
            return            # nothing of the agent's exists yet / the agent is done

        active = self._read_active()
        position = graft.position_view()
        reason, void = None, False
        if active.get("source") == "recon-adopt":
            reason = "recon_adopt"
        elif position and not active:
            reason, void = "active_cleared_externally", True
        elif active and not position:
            reason = "unexpected_active_position"
        elif active and int(active.get("contracts") or 0) != self._contracts:
            reason = "contracts_mismatch"
        if reason is None:
            return
        # No order. The position changed and the agent did not change it; sending
        # anything now would be acting on a state it has just learned it does not know.
        graft.external_kill(now, reason, void_position=void)
        self._stand_down("killed", now)
        self._record({"kind": "watchdog_kill", "time": now, "reason": reason,
                      "void_position": void, "active": _jsonable(active),
                      "position": _jsonable(position or {})}, None, None, None, None)

    def _stand_down(self, why, now) -> None:
        self._stood_down = why
        self._stood_down_at = now

    def _window_end_hm(self):
        if self._window_end is None:
            from agent.trader.executor import WINDOW_END_ET
            self._window_end = tuple(WINDOW_END_ET)
        return self._window_end

    def _window_end_ts(self, day):
        """The window end on `day`'s date. BAR time in, bar time out."""
        hour, minute = self._window_end_hm()
        return day.normalize() + pd.Timedelta(hours=hour, minutes=minute)

    def _close_at_window_end(self, now) -> None:
        """D22: after a KILL on a day the agent sent an entry, ONE market close at the
        window end, whatever position.json says — a close against a flat account is
        harmless, and an unmanaged broker position is not. Never after a kill that
        itself came after the window end: by then the agent was already flat and done,
        and whatever is open belongs to someone else."""
        if (self._window_close_sent or self._stood_down != "killed"
                or not self._entry_sent or self._stood_down_at is None):
            return
        end = self._window_end_ts(self._stood_down_at)
        if self._stood_down_at >= end:
            self._window_close_sent = True
            return
        if now < end:
            return
        self._window_close_sent = True
        self._emit_close(now, "window_end")

    def _emit_close(self, now, reason: str) -> None:
        # Price 0.0: the supervisor has no fill price of its own, and the order module
        # resolves 0.0 to the current market price for its log line.
        sig = {"kind": CLOSE_KIND, "price": 0.0, "reason": reason, "skip_recon": True,
               "time": _iso(now), "source": SOURCE}
        json.dumps(sig).encode("ascii")
        started = time.perf_counter()
        error = None
        try:
            self._emit(sig)
        except Exception as exc:
            error = "emit_raised: %s" % type(exc).__name__
        dispatch_ms = round((time.perf_counter() - started) * 1000.0, 3)
        ack = {"ok": False, "reason": error} if error else self._ack(False)
        self._record({"kind": "supervisor_close", "time": now, "reason": reason},
                     sig, ack, dispatch_ms, None)

    # -- ownership -------------------------------------------------------------- #

    def record_dropped(self, sig: dict) -> None:
        """A legacy order kind reached the emit while the agent owns the dispatcher.
        The caller dropped it; this is the evidence."""
        self._record({"kind": "legacy_signal", "time": (sig or {}).get("time")},
                     _jsonable(sig or {}), None, None, "legacy_order_kind")

    # -- the record ------------------------------------------------------------- #

    def _record(self, event, sig, ack, dispatch_ms, suppressed) -> None:
        """One line, one write. Best-effort: a recorder failure never reaches the sink."""
        try:
            event = event or {}
            line = json.dumps({
                "seq": event.get("seq"),
                "bar_time": _iso(event.get("time")),
                "wall_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "sim_event": _jsonable(event),
                "signal": sig,
                "ack": ack,
                "dispatch_ms": dispatch_ms,
                "suppressed": suppressed,
            }, default=str)
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
