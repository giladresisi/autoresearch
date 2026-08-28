"""Simulated order lifecycle: rest -> fill -> stop-out / take-profit.

Cycle 1 stopped at an `intended_entry` RECORD, which is why nothing downstream of an
entry could be built. Every remaining §2/§8 spine rule presupposes a fill: the ladder
acts "while a stop-entry rests UNFILLED", the cooldown starts "after a stop-loss is
hit", the attempt counter counts stop-outs, and the blacklist triggers on one.

This is a SIMULATION inside the Executor. It never imports the legacy order module and
never reaches a broker; real order placement is a later cycle.

Fill conventions, pinned from §11's calibrated 1s replay (which reproduces every
recorded 5m binding's P&L exactly):
  - a resting stop-entry fills AT ITS TRIGGER PRICE when the tape reaches it;
  - a crossed-trigger market execution fills at the 1s mid at placement (caller-supplied);
  - take-profit is the plan's DOL.

Same-bar ambiguity always resolves ADVERSELY: a bar that touches both the stop and the
DOL books the stop, and a bar that both fills and stops books both. Booking the better
of the two would silently inflate every result, and §11's own errata show how easily an
optimistic reading survives review.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

_LONG = ("UP", "LONG")


@dataclass
class RestingOrder:
    direction: str
    trigger: float
    stop: float
    artifact_id: str
    placed_at: pd.Timestamp


def _is_long(direction: str) -> bool:
    return str(direction or "").upper() in _LONG


class OrderSim:
    """One resting order and at most one position — §2's single stop-entry policy."""

    def __init__(self, dol=None) -> None:
        self._dol = float(dol) if dol is not None else None
        self.resting: "RestingOrder | None" = None
        self.position: "dict | None" = None
        self.last_fill: "dict | None" = None
        self.last_stop_out: "dict | None" = None

    # -- order management ------------------------------------------------------ #

    def place(self, order: RestingOrder) -> None:
        """Replaces any existing resting order — only one exists at a time."""
        self.resting = order

    def cancel(self) -> None:
        self.resting = None

    def fill_market(self, now, *, direction, price, stop, artifact_id) -> dict:
        """Crossed-trigger execution. Fills at the caller-supplied price."""
        self.resting = None
        return self._open(now, direction, float(price), float(stop), artifact_id)

    # -- the tape -------------------------------------------------------------- #

    def on_bar(self, now, bar) -> "list[dict]":
        events: list[dict] = []
        hi, lo = float(bar["High"]), float(bar["Low"])

        if self.position is None and self.resting is not None:
            o = self.resting
            reached = (hi >= o.trigger) if _is_long(o.direction) else (lo <= o.trigger)
            if reached:
                self.resting = None
                events.append(self._open(now, o.direction, o.trigger, o.stop,
                                         o.artifact_id))

        if self.position is not None:
            p = self.position
            long_ = _is_long(p["direction"])
            stopped = (lo <= p["stop"]) if long_ else (hi >= p["stop"])
            tp = (self._dol is not None
                  and ((hi >= self._dol) if long_ else (lo <= self._dol)))
            # Adverse resolution first: a bar that reaches both books the stop.
            if stopped:
                events.append(self._close(now, "stop_out", p["stop"]))
            elif tp:
                events.append(self._close(now, "take_profit", self._dol))
        return events

    # -- internals ------------------------------------------------------------- #

    def _open(self, now, direction, price, stop, artifact_id) -> dict:
        self.position = {"direction": direction, "entry": float(price),
                         "stop": float(stop), "artifact_id": artifact_id,
                         "opened_at": now}
        self.last_fill = {"kind": "fill", "time": now, "price": float(price),
                          "direction": direction, "artifact_id": artifact_id}
        return dict(self.last_fill)

    def _close(self, now, kind, price) -> dict:
        p = self.position or {}
        ev = {"kind": kind, "time": now, "price": float(price),
              "direction": p.get("direction"), "artifact_id": p.get("artifact_id"),
              "entry": p.get("entry")}
        if kind == "stop_out":
            self.last_stop_out = dict(ev)
        self.position = None
        return ev
