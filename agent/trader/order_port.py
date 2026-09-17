"""The order port: the surface the Executor drives, and a mirror of it.

`OrderSim` was constructed inline by the Executor, so the only thing that could ever
hold a position was the simulation. Going live does not change who decides — `OrderSim`
stays the brain's position model (plan 38 D2) — it adds a second party that has to be
TOLD what the model just did. That is all this module is.

`OrderPort` is exactly the surface the Executor uses, read off its call sites and nothing
more: five attributes and six methods. `OrderSim` satisfies it unchanged, which is what
keeps a replay byte-identical — the default port IS the simulation.

`MirroringOrderPort` wraps a simulation and, after every event the simulation RETURNS
(`fill`, `stop_out`, `take_profit`, `mark`), calls `sink(event)` once. The sink is
injected; this module does not know what is on the other side of it, imports nothing
outside the standard library, and names no order-routing module anywhere — docstrings
included, because the structural gates grep the whole source.

Three rules the mirror owns, because nobody else can:

  * **A per-event `seq`.** An artifact id is NOT unique per trade — a market mechanism
    with no gap reuses its own name as the id all session — so a dedupe key built from
    ids would suppress the session's second legitimate entry and its close. The sequence
    number is what makes `(plan_id, seq)` a key.
  * **A refused entry is voided, not booked.** The far side can decline an entry AFTER
    the simulation has filled it (entries paused, a window gate). An ack carrying
    `voided` undoes the fill and the plan lives: no attempt is spent, because nothing
    was ever at risk. The event handed back to the Executor is re-labelled
    `fill_voided` so the decision stream records what happened instead of a fill that
    never existed.
  * **Anything else that is not ok is an EXTERNAL change.** The position model and the
    far side no longer agree and this module cannot know who is right, so it voids a
    fill, sets `external`, and the Executor kills the plan (fail closed). A failed ack
    on a CLOSE never resurrects the position: the model already closed it, and
    re-opening it would have the brain managing something it decided to leave.

The sink must not raise; one that does is treated as `ok=False`.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

#: The kind a refused entry is handed back to the Executor as. Deliberately NOT `fill`:
#: everything downstream that pairs a fill with its close would otherwise read a trade.
VOIDED_KIND = "fill_voided"


@runtime_checkable
class OrderPort(Protocol):
    """What the Executor needs from an order book. `OrderSim` is the reference."""

    resting: object
    position: object
    last_fill: object
    last_stop_out: object

    @property
    def target(self): ...

    def place(self, order) -> None: ...

    def cancel(self) -> None: ...

    def set_target(self, price) -> None: ...

    def fill_market(self, now, *, direction, price, stop, artifact_id) -> dict: ...

    def on_bar(self, now, bar) -> list: ...

    def mark_open(self, now, price): ...


def _ack_ok(ack) -> bool:
    if ack is None:
        return True
    if isinstance(ack, dict):
        return bool(ack.get("ok", True))
    return bool(ack)


class MirroringOrderPort:
    """Delegates everything to `inner`; reports every returned event to `sink`.

    `context()` returns `{"plan_id", "mechanism"}` as of the call. The mechanism of an
    OPEN position is remembered from its fill, because the Executor clears its own
    mechanism state when the plan dies while the position is still being managed.
    """

    def __init__(self, inner, sink, context=None) -> None:
        self._inner = inner
        self._sink = sink
        self._context = context if context is not None else (lambda: {})
        self._seq = 0
        self._open_ctx: dict = {}
        #: Set once the two sides are known to disagree. Never cleared: the plan that
        #: reads it dies, and a new session builds a new port.
        self.external = None

    # -- the delegated surface -------------------------------------------------- #

    @property
    def resting(self):
        return self._inner.resting

    @property
    def position(self):
        return self._inner.position

    @property
    def last_fill(self):
        return self._inner.last_fill

    @property
    def last_stop_out(self):
        return self._inner.last_stop_out

    @property
    def target(self):
        return self._inner.target

    @property
    def inner(self):
        return self._inner

    def place(self, order) -> None:
        self._inner.place(order)

    def cancel(self) -> None:
        self._inner.cancel()

    def set_target(self, price) -> None:
        self._inner.set_target(price)

    def void_position(self) -> None:
        """Drop the modelled position WITHOUT an event. The outer side's call, made when
        it has established that the position no longer exists."""
        self._inner.position = None
        self._inner.set_target(None)
        self._open_ctx = {}

    # -- the mirrored events ---------------------------------------------------- #

    def fill_market(self, now, *, direction, price, stop, artifact_id) -> dict:
        before = self._snapshot()
        ev = self._inner.fill_market(now, direction=direction, price=price, stop=stop,
                                     artifact_id=artifact_id)
        return self._mirror_fill(ev, stop, before)

    def on_bar(self, now, bar) -> list:
        before = self._snapshot()
        resting = self._inner.resting
        stop = getattr(resting, "stop", None)
        out = []
        voided = False
        for ev in self._inner.on_bar(now, bar):
            kind = ev.get("kind")
            if kind == "fill":
                ev = self._mirror_fill(ev, stop, before)
                voided = ev.get("kind") == VOIDED_KIND
                out.append(ev)
            elif voided:
                # The same bar also closed a position that was never opened on the far
                # side. There is nothing to mirror and nothing to book.
                continue
            else:
                self._mirror_close(ev)
                out.append(ev)
        return out

    def mark_open(self, now, price):
        ev = self._inner.mark_open(now, price)
        if ev is not None:
            self._mirror_close(ev)
        return ev

    # -- internals -------------------------------------------------------------- #

    def _snapshot(self) -> dict:
        return {"last_fill": self._inner.last_fill,
                "last_stop_out": self._inner.last_stop_out}

    def _send(self, ev: dict, ctx: dict, **extra) -> dict:
        """One sink call. Returns a normalised ack; never raises."""
        self._seq += 1
        payload = dict(ev)
        payload.update(extra)
        payload["seq"] = self._seq
        payload["plan_id"] = ctx.get("plan_id")
        payload["mechanism"] = ctx.get("mechanism")
        try:
            ack = self._sink(payload)
        except Exception as exc:
            return {"ok": False, "reason": "sink_raised: %s" % type(exc).__name__}
        if _ack_ok(ack):
            return {"ok": True, "reason": ""}
        ack = ack if isinstance(ack, dict) else {}
        return {"ok": False, "reason": str(ack.get("reason") or "not_ok"),
                "voided": bool(ack.get("voided"))}

    def _ctx(self) -> dict:
        try:
            return dict(self._context() or {})
        except Exception:
            return {}

    def _mirror_fill(self, ev: dict, stop, before: dict) -> dict:
        ctx = self._ctx()
        self._open_ctx = ctx
        ack = self._send(ev, ctx, stop=(float(stop) if stop is not None else None))
        if ack["ok"]:
            return ev
        # Undo the fill completely: the position, its target, and the two "last event"
        # slots — `last_stop_out` drives the cooldown, and a same-bar stop-out of a
        # position that never existed must not start one.
        self.void_position()
        self._inner.last_fill = before["last_fill"]
        self._inner.last_stop_out = before["last_stop_out"]
        if not ack.get("voided"):
            self.external = {"reason": ack["reason"], "kind": "fill"}
        return dict(ev, kind=VOIDED_KIND, reason=ack["reason"])

    def _mirror_close(self, ev: dict) -> None:
        ctx = self._open_ctx or self._ctx()
        ack = self._send(ev, ctx)
        self._open_ctx = {}
        if not ack["ok"]:
            # The model has already closed; it stays closed. What is unknown is the far
            # side, and that is an external change.
            self.external = {"reason": ack["reason"], "kind": ev.get("kind")}


__all__ = ["OrderPort", "MirroringOrderPort", "VOIDED_KIND"]
