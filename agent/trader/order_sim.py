"""Simulated order lifecycle: rest -> fill -> stop-out / take-profit / mark.

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
  - take-profit is the TARGET, set by the caller at the fill (`set_target`). It was the
    plan's 09:20 DOL until plan 16 made the DOL inert and moved selection to the fill.

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

    def set_target(self, price) -> None:
        """Set (or clear) the take-profit AFTER construction.

        The target used to be the plan's 09:20 DOL, known at construction; it is now the
        T2 pick, which does not exist until there is a fill to anchor it on (see
        `agent/trader/target.py`). `None` clears it, and a cleared target is a real
        state: the position is then managed by its stop and the window mark only.
        """
        self._dol = None if price is None else float(price)

    @property
    def target(self):
        """The current take-profit, or None. Named `target` rather than `dol` because it
        is no longer the L1 DOL — that field is now inert (plan 16)."""
        return self._dol

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

    def mark_open(self, now, price) -> "dict | None":
        """Book a still-open position at the tape's last print. A MARK, NOT an exit.

        The replay window is fixed by design (`replay.WINDOW_END_ET`), so a position
        still open when the window ends is not a decision anything took -- it is the run
        ending. Emitting nothing, which is what happened before this existed, makes a
        runner contribute ZERO to any P&L tally while every fast stop-out books in full:
        a silent bias toward losers in exactly the direction that flatters a tight stop.

        The `kind` stays distinct from `stop_out` / `take_profit` so nothing downstream
        can quote a mark as an exit -- plan 31 §3.1 records that conflating the two
        already produced one wrong reading in this project.
        """
        if self.position is None:
            return None
        return self._close(now, "mark", price)

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
        # THE TARGET BELONGS TO THE POSITION, and dies with it (plan 16).
        #
        # FOUND THE HARD WAY on the 08-18 named case. `on_bar` fills a resting order and
        # then tests the take-profit IN THE SAME CALL, while the Executor can only set
        # the new target after `on_bar` returns. A target left standing from the previous
        # position is therefore live for exactly that window — and on 08-18 it fired: a
        # DOWN position filled at 29564.5 booked a `take_profit` at 29625.0, the PRIOR
        # position's target, 60.5 pts the wrong way and recorded as a win.
        #
        # Clearing here makes the stale value unreachable by construction rather than by
        # timing. The cost is that a bar which both fills and reaches the FRESH target
        # books no take-profit; that is the correct side to err on (§11's free-points
        # rule) and the T2 pick is at least `max(5pts, 1.0 x avg_1h)` away anyway.
        self._dol = None
        return ev
