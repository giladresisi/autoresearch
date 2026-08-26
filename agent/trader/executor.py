"""Continuous binding and guards, stopping at the intended-entry RECORD. No orders.

Cycle 1 places nothing. It answers "where would I have entered, and when did I refuse?"
in a form a human can read now and a replay harness can diff later.

Order of work in `on_bar`, and the reasons:

1. **Facts maintenance runs FIRST and unconditionally** — before the plan-death check,
   so a dead plan cannot freeze the store. Facts are SESSION state; a plan is not.
   Maintenance is skipped only when the graft owns the maintainer and has already
   driven it this bar (see `FactsMaintainer` and this class's ownership rule).
2. **Plan death is evaluated every bar**, never gated on whether anything is bound or
   open. That is l2-mechanisms.md §7's scope lesson: an unscoped death check fires
   hours after the plan has already completed. Once dead, the plan is recorded once and
   the Executor stops BINDING for the rest of the session — while facts keep
   accumulating for whatever needs them next.
3. **The per-second path stays O(small)**: a partial bar only refreshes running
   extremes. Coverage, re-binding and guards run on BAR CLOSE only.

   "Bar close" is DERIVED from a minute rollover, not taken from the `bar_complete`
   flag alone. That flag means "this call delivers a completed 1m bar", and the only
   live driver (`automation/main.py`) and the 1s replay driver both hard-code it to
   `False` on every tick — they hand over an intra-minute partial every second and let
   the consumer notice the rollover itself, exactly as `session_pipeline` does for SMT
   detection. Trusting the flag alone leaves the whole chain inert in live.
4. **Settle window (arm -> 09:30:30)**: state tracking continues, but no
   `intended_entry` is emitted. In-window penetrations count for eligibility only
   (l2 §2) — the precondition must be satisfied fresh after the window ends.
5. **Guards, in order**: max-distance (60 pts), then the DOL-floor veto (>= 60 pts must
   remain between the trigger and the DOL). Every veto is recorded with its reason and
   the numbers behind it.

The cascade itself now lives in `agent.facts.maintainer.FactsMaintainer`, which this
class reads through `_store` / `_avg_range_1h`.
"""
from __future__ import annotations

import pandas as pd

from agent.facts.detectors._common import normalize, truncate
from agent.facts.maintainer import FactsMaintainer
from agent.facts.records import FactClass, FactState
from agent.facts.requirements import EXECUTOR_REQUIREMENT
from agent.trader.records import DecisionRecorder

# l2-mechanisms.md §9 starting values.
SETTLE_UNTIL_SECONDS = 30            # settle window ends at 09:30:30
SETTLE_END_HOUR = 9
SETTLE_END_MINUTE = 30
MAX_DISTANCE_PTS = 60.0              # trigger must sit within this of current price
DOL_FLOOR_PTS = 60.0                 # this much must REMAIN between trigger and DOL
ENTRY_BUFFER_PTS = 3.0               # beyond the gap's far end, the wick-deception guard
STOP_BUFFER_PTS = 3.0
STOP_CAP_PTS = 25.0                  # nearer of structural stop and this cap (2026-08-22)
PRIMARY_TF = "5min"

_SHORT = ("DOWN", "SHORT")


class Executor:
    def __init__(self, state_dir, plan: dict, arm_ts: pd.Timestamp, *, recorder=None,
                 store=None, maintainer=None, requirement=EXECUTOR_REQUIREMENT,
                 ticker: str = "MNQ") -> None:
        """`maintainer` is the session-scoped facts owner.

        OWNERSHIP RULE, and the reason it exists: whoever CREATED the maintainer drives
        it. When the graft injects one, the graft drives it (so it can keep running
        before a plan arms and after one dies) and this Executor only reads it —
        otherwise both would cascade on the same bar and detection would run twice.
        When no maintainer is injected (the standalone case), the Executor creates and
        drives its own, which is what every direct-construction test relies on.

        `store=` is kept as the injection seam it always was: pass a bare FactStore and
        it is wrapped in a maintainer this Executor owns.
        """
        self.state_dir = str(state_dir)
        self._plan = dict(plan or {})
        self._arm_ts = arm_ts
        self._ticker = ticker
        self._req = requirement
        self._owns_maintainer = maintainer is None
        self._maint = maintainer if maintainer is not None else FactsMaintainer(
            store=store, requirement=requirement, ticker=ticker)
        self._rec = recorder if recorder is not None else DecisionRecorder(state_dir)
        self._emitted: set = set()
        self._vetoed: set = set()
        self._last_minute = None
        self._state = {
            "tracking": False,
            "plan_alive": True,
            "dead_reason": None,
            "last_cascade": None,
            "bound_id": None,
            "bound_label": None,
            "mechanism": None,
            "trigger": None,
            "stop": None,
            "now_price": None,
            "in_settle": True,
        }

    # -- public ---------------------------------------------------------------- #

    @property
    def _store(self):
        return self._maint.store

    @property
    def _avg_range_1h(self):
        return self._maint.avg_range_1h

    @property
    def maintainer(self) -> FactsMaintainer:
        return self._maint

    def bind_state(self) -> dict:
        return dict(self._state)

    def bar_closed(self, now: pd.Timestamp, bar_complete=None) -> bool:
        """Whether THIS call should drive the bar-close cascade.

        True when the caller says so explicitly, or when the minute has rolled since
        the last call. The first call only seeds the minute — no bar has completed yet,
        so there is nothing to cascade on.
        """
        minute = now.floor("1min")
        rolled = self._last_minute is not None and minute != self._last_minute
        self._last_minute = minute
        return bool(bar_complete) or rolled

    def on_bar(self, now: pd.Timestamp, bars: dict, bar_complete=None) -> None:
        if now is None:
            return
        bar_complete = self.bar_closed(now, bar_complete)
        if self._arm_ts is not None and now >= self._arm_ts:
            self._state["tracking"] = True
        self._state["in_settle"] = self._in_settle(now)

        mnq = truncate(normalize((bars or {}).get(self._ticker)), now)
        if len(mnq):
            self._state["now_price"] = float(mnq["Close"].iloc[-1])

        # 1. FACTS MAINTENANCE FIRST, and unconditionally — before the plan-death
        # check, so a dead plan does not freeze the store. Facts are SESSION state; a
        # plan is not. Skipped only when the graft owns the maintainer, in which case
        # the graft has already driven it this bar (see the ownership rule in
        # `__init__`) and running it here would cascade twice.
        if self._owns_maintainer:
            self._maint.on_bar(now, bars, bar_complete)
        if bar_complete:
            self._state["last_cascade"] = self._maint.last_cascade or str(now)

        # 2. Plan death — evaluated every bar, with nothing open. Over bars SINCE THE
        # ARM only: the frame reaches back to the 18:00 session open, and overnight /
        # pre-RTH traversal of a level the plan later names as its DOL is routine.
        # Judging a plan by price action that predates it kills almost every plan on
        # its first bar.
        if self._state["plan_alive"]:
            reason, detail = self._death(self._since_arm(mnq))
            if reason is not None:
                self._state["plan_alive"] = False
                self._state["dead_reason"] = reason
                self._state["bound_id"] = None
                self._state["bound_label"] = None
                self._state["mechanism"] = None
                self._state["trigger"] = None
                self._state["stop"] = None
                self._rec.plan_dead(now=now, plan_id=self._plan.get("plan_id"),
                                    reason=reason, detail=detail)
        if not self._state["plan_alive"]:
            return                    # no more BINDING; facts above keep accumulating

        if not bar_complete:
            return                                    # per-second path ends here

        # 3. Re-bind, then guards. Coverage and the ATR proxy were refreshed by the
        # maintainer above.
        try:
            self._rebind_and_guard(now)
        except Exception:
            pass

    # -- internals -------------------------------------------------------------- #

    def _in_settle(self, now: pd.Timestamp) -> bool:
        """arm -> 09:30:30 inclusive of the arm, exclusive of the end instant."""
        if self._arm_ts is None or now < self._arm_ts:
            return True
        end = now.normalize() + pd.Timedelta(
            hours=SETTLE_END_HOUR, minutes=SETTLE_END_MINUTE, seconds=SETTLE_UNTIL_SECONDS)
        return now < end

    def _since_arm(self, mnq: pd.DataFrame) -> pd.DataFrame:
        if self._arm_ts is None or len(mnq) == 0:
            return mnq
        return mnq[mnq.index >= self._arm_ts]

    def _is_short(self) -> bool:
        return str(self._plan.get("direction") or "").upper() in _SHORT

    def _dol_price(self):
        dol = self._plan.get("dol")
        if isinstance(dol, dict):
            return dol.get("price")
        return dol if isinstance(dol, (int, float)) else None

    def _death(self, mnq: pd.DataFrame):
        """(reason, detail) or (None, None). Evaluated with nothing open, every bar."""
        dol = self._dol_price()
        if dol is not None and len(mnq):
            if self._is_short():
                reached = float(mnq["Low"].min()) <= float(dol)
            else:
                reached = float(mnq["High"].max()) >= float(dol)
            if reached:
                return "dol_reached", {"dol": float(dol)}

        cap = self._plan.get("max_attempts")
        if cap is not None and int(self._plan.get("attempts_used") or 0) >= int(cap):
            return "attempts_exhausted", {"attempts_used": self._plan.get("attempts_used")}

        fired = self._falsified(mnq)
        if fired is not None:
            return "falsified", fired
        return None, None

    def _falsified(self, mnq: pd.DataFrame):
        """Evaluate the plan's `valid_while` predicates.

        Cycle-1 scope: only the `price_beyond` predicate shape is evaluated. Any other
        predicate kind is treated as NOT fired and reported in the death detail if it
        ever matters — silently guessing at a predicate vocabulary the plan did not
        specify would be worse than leaving it unevaluated and saying so.
        """
        if not len(mnq):
            return None
        hi, lo = float(mnq["High"].max()), float(mnq["Low"].min())
        for pred in self._plan.get("valid_while") or ():
            if not isinstance(pred, dict) or pred.get("kind") != "price_beyond":
                continue
            price, side = pred.get("price"), str(pred.get("side") or "").lower()
            if price is None:
                continue
            if side == "above" and hi >= float(price):
                return {"predicate": pred}
            if side == "below" and lo <= float(price):
                return {"predicate": pred}
        return None

    def _eligible_gaps(self, now: pd.Timestamp) -> list:
        """Live MNQ gaps on the primary timeframe facing the trade direction.

        A SHORT plan trades from bear gaps (supply above), a LONG plan from bull gaps.
        Blacklisted artifacts are excluded — a gap that already produced a stop-out is
        not re-bindable for this plan.
        """
        want = "bear" if self._is_short() else "bull"
        black = set(self._plan.get("blacklist") or ())
        out = []
        for f in self._store.query(cls=FactClass.FVG, ticker=self._ticker,
                                   state=FactState.LIVE):
            if f.timeframe != PRIMARY_TF or f.extra.get("direction") != want:
                continue
            if f.id in black:
                continue
            if f.reference_ts is not None and now is not None and f.reference_ts > now:
                continue
            out.append(f)
        return out

    def _levels_for(self, gap):
        """(trigger, stop) for a gap, per l2 §2: stop-entry beyond the far end with the
        entry buffer; stop at the opposite end plus the stop buffer, capped at 25 pts
        from the entry."""
        if self._is_short():
            trigger = float(gap.price_low) - ENTRY_BUFFER_PTS
            structural = float(gap.price_high) + STOP_BUFFER_PTS
            stop = min(structural, trigger + STOP_CAP_PTS)
        else:
            trigger = float(gap.price_high) + ENTRY_BUFFER_PTS
            structural = float(gap.price_low) - STOP_BUFFER_PTS
            stop = max(structural, trigger - STOP_CAP_PTS)
        return trigger, stop

    def _rebind_and_guard(self, now: pd.Timestamp) -> None:
        price = self._state["now_price"]
        mechanism = self._entry_mechanism()
        if price is None or mechanism is None:
            return

        gaps = self._eligible_gaps(now)
        if not gaps:
            self._clear_binding()
            return

        # l2 §2 single stop-entry policy: the trigger closest to current price wins.
        scored = sorted(((abs(self._levels_for(g)[0] - price), g) for g in gaps),
                        key=lambda t: (t[0], t[1].id))
        _, gap = scored[0]
        trigger, stop = self._levels_for(gap)

        if self._state["bound_id"] != gap.id:
            self._state.update({"bound_id": gap.id, "bound_label": gap.label,
                                "mechanism": mechanism})
            self._rec.bind(now=now, plan_id=self._plan.get("plan_id"),
                           mechanism=mechanism, artifact_id=gap.id,
                           artifact_label=gap.label, trigger=trigger, stop=stop)
        self._state.update({"trigger": trigger, "stop": stop})

        # --- guards, in order ------------------------------------------------- #
        distance = abs(trigger - price)
        if distance > MAX_DISTANCE_PTS:
            self._veto_once(now, mechanism, gap, "max_distance",
                            {"distance": round(distance, 4), "cap": MAX_DISTANCE_PTS,
                             "trigger": trigger, "price": price})
            return

        dol = self._dol_price()
        if dol is not None:
            remaining = (trigger - float(dol)) if self._is_short() else (float(dol) - trigger)
            if remaining < DOL_FLOOR_PTS:
                self._veto_once(now, mechanism, gap, "dol_floor",
                                {"remaining": round(remaining, 4),
                                 "floor": DOL_FLOOR_PTS, "trigger": trigger,
                                 "dol": float(dol)})
                return

        if self._state["in_settle"]:
            return                                     # tracked, never entered (l2 §2)

        key = (mechanism, gap.id)
        if key in self._emitted:
            return
        self._emitted.add(key)
        self._rec.intended_entry(now=now, plan_id=self._plan.get("plan_id"),
                                 mechanism=mechanism, artifact_id=gap.id,
                                 artifact_label=gap.label, trigger=trigger, stop=stop,
                                 dol=self._dol_price(), price=price)

    def _entry_mechanism(self):
        for name in ("fvg_negation_reversal", "fvg_return_continuation"):
            if name in (self._plan.get("armed_classes") or ()):
                return name
        return None

    def _veto_once(self, now, mechanism, gap, reason, detail) -> None:
        key = (mechanism, gap.id, reason)
        if key in self._vetoed:
            return
        self._vetoed.add(key)
        self._rec.veto(now=now, plan_id=self._plan.get("plan_id"), mechanism=mechanism,
                       reason=reason, detail=detail, artifact_id=gap.id,
                       artifact_label=gap.label)

    def _clear_binding(self) -> None:
        self._state.update({"bound_id": None, "bound_label": None, "mechanism": None,
                            "trigger": None, "stop": None})
