"""Continuous binding, guards, and a SIMULATED order lifecycle. Still no real orders.

Cycle 1 stopped at the intended-entry record: "where would I have entered, and when did
I refuse?". That is why nothing downstream of an entry could be built — §2's ladder acts
while a stop-entry rests UNFILLED, §2's cooldown starts after a stop-loss is HIT, and
§8's attempt counter counts STOP-OUTS. Cycle 3 adds `order_sim.OrderSim`, so the intent
becomes a resting stop-entry that fills, stops out, or reaches the DOL, and the plan's
attempt budget is actually spent. No broker is reached and the legacy order-placement
module stays unimported. (Its name is spelled out nowhere in this file, docstring
included: `test_executor.py` greps the whole source for it.)

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

import math
import os

import pandas as pd

from agent.facts.bars import resample
from agent.facts.detectors._common import normalize, truncate
from agent.facts.maintainer import FactsMaintainer
from agent.facts.records import FactClass, FactState
from agent.facts.requirements import EXECUTOR_REQUIREMENT
from agent.contracts.predicates import (MarketView, eval_predicate,
                                        validate_predicate_list)
from agent.trader.order_sim import OrderSim, RestingOrder
from agent.trader.retrace import RetraceGate
from agent.trader.takeover import deepest_penetrated, resolve_cooldown_end
from agent.trader.records import DecisionRecorder
from agent.trader.target import select_target, level_universe, target_menu, running_rows
from agent.trader.initial_target import (InitialTargetTracker, select_initial_target,
                                         minute_of, variant_label)
from agent.trader.arbiter import Arbiter
from agent.trader.market_mechanisms import MarketMechanisms
import agent.trader.micro_smt as micro_smt

# l2-mechanisms.md §9 starting values.
SETTLE_UNTIL_SECONDS = 30            # settle window ends at 09:30:30
SETTLE_END_HOUR = 9
SETTLE_END_MINUTE = 30
MAX_DISTANCE_PTS = 60.0              # trigger must sit within this of current price
DOL_FLOOR_PTS = 60.0                 # this much must REMAIN between trigger and DOL
ENTRY_BUFFER_PTS = 3.0               # beyond the gap's far end, the wick-deception guard
STOP_BUFFER_PTS = 3.0
STOP_CAP_PTS = 25.0                  # nearer of structural stop and this cap (2026-08-22)
TICK_PTS = 0.25                      # MNQ/MES tick; market fills snap to this grid
MIN_FVG_HEIGHT_PTS = 5.0             # §9; INITIAL binding only — ladder targets exempt
MAX_FVG_HEIGHT_PTS = 45.0            # §9; raised from 35 on 2026-08-22
DISTANCE_INVALIDATION_PTS = 60.0     # §9; PERMANENT, unlike the max-distance guard
NO_MOVE_ZONE_PTS = 15.0              # §9; see §8 — never re-bind this close to a trigger
MAX_ATTEMPTS = 3                     # §8; per plan_id, shared across mechanisms
# How much history the predicate view's higher timeframes are resampled from. Bounded:
# `n_closes_beyond` reads at most `n` closes, and 24h holds 6 completed 4h bars.
HTF_VIEW_TAIL = pd.Timedelta(hours=24)
PRIMARY_TF = "5min"
FALLBACK_TF = "1min"                 # §4's widened fallback, §6, §8
RTH_OPEN_HOUR = 9                    # §4: creating bar at or after 09:30
RTH_OPEN_MINUTE = 30

# §8's TEMPORARY live-rollout spine gates (2026-09-17). Both are tested in BAR time
# against the ARM's date, and each comes out by changing one line: `None` removes the
# cutoff, `False` removes the positive-trade rule.
ENTRY_CUTOFF_ET = (10, 30)           # no NEW entry at or after this; positions managed on
NO_ENTRY_AFTER_POSITIVE = True       # a plan that closed a winner takes no further entry
# §8's window end, and the ONE source of it: `replay.py` imports this constant. Replay's
# last bar is 12:59:59, so the rule below is unreachable there; live runs the whole CME
# session and needs it spelled out.
WINDOW_END_ET = (13, 0)
# Plan 35 §2.5: what happens once the initial target is REACHED (a completed 1m bar
# touched and closed beyond it). "record" (option C, the shipped default) changes no
# order: the flip and what A / B would have done are recorded so the 66-session rig can
# choose. "be_structure" (A) moves the stop to the initial price, once. "opp_close" (B)
# market-closes on the first opposite 1m close after the flip. A and B are unmeasured
# against cycle-5's stop-only control and must not be enabled without that study.
INITIAL_TARGET_ACTION = "record"
INITIAL_TARGET_ACTIONS = ("record", "be_structure", "opp_close")
if INITIAL_TARGET_ACTION not in INITIAL_TARGET_ACTIONS:      # a typo must not run as "record"
    raise ValueError(f"INITIAL_TARGET_ACTION={INITIAL_TARGET_ACTION!r} not in {INITIAL_TARGET_ACTIONS}")

_SHORT = ("DOWN", "SHORT")


class Executor:
    def __init__(self, state_dir, plan: dict, arm_ts: pd.Timestamp, *, recorder=None,
                 store=None, maintainer=None, requirement=EXECUTOR_REQUIREMENT,
                 ticker: str = "MNQ", order_port=None, htf_extremes=None) -> None:
        """`maintainer` is the session-scoped facts owner.

        OWNERSHIP RULE, and the reason it exists: whoever CREATED the maintainer drives
        it. When the graft injects one, the graft drives it (so it can keep running
        before a plan arms and after one dies) and this Executor only reads it —
        otherwise both would cascade on the same bar and detection would run twice.
        When no maintainer is injected (the standalone case), the Executor creates and
        drives its own, which is what every direct-construction test relies on.

        `store=` is kept as the injection seam it always was: pass a bare FactStore and
        it is wrapped in a maintainer this Executor owns.

        `order_port=` is the order book this Executor drives (`order_port.OrderPort`).
        The default is a plain `OrderSim`, which is what every replay uses; the live
        graft injects a port that mirrors the simulation's events outward. Either way
        the simulation stays the position model — this class never learns which it got.

        `htf_extremes=` (plan 40) is the session's unnested weekly/monthly extremes for
        THIS ticker, {"as_of", "extremes", "seed"}, computed once at the session open.
        Forwarded to both fill sites' target selection; `target.HTF_EXTREMES_IN_T2`
        decides whether it is used. None (every replay before plan 40, every direct-
        construction test) changes nothing.
        """
        self.state_dir = str(state_dir)
        self._htf = htf_extremes
        self._plan = dict(plan or {})
        # §8's attempt budget, defaulted AT THE ARM. `derive_plan` does not emit the
        # key, so without this the cap is None and the budget is never enforced.
        self._plan.setdefault("max_attempts", MAX_ATTEMPTS)
        self._arm_ts = arm_ts
        self._ticker = ticker
        self._req = requirement
        self._owns_maintainer = maintainer is None
        self._maint = maintainer if maintainer is not None else FactsMaintainer(
            store=store, requirement=requirement, ticker=ticker)
        self._rec = recorder if recorder is not None else DecisionRecorder(state_dir)
        # The simulated order lifecycle. A SIMULATION, never a broker call.
        #
        # NO TARGET AT CONSTRUCTION (plan 16). It used to be seeded with the plan's 09:20
        # DOL; the take-profit is now the T2 pick, which does not exist until a fill
        # gives it an instant to anchor on. `_target_for_fill` sets it there.
        self._sim = order_port if order_port is not None else OrderSim(dol=None)
        # §8's temporary spine gates and the window end. `_positive_close` latches on the
        # first profitable close; `_window_ended` makes the window end fire exactly once.
        self._positive_close = False
        self._window_ended = False
        # The bars handed to the CURRENT `on_bar` call, so a fill discovered inside
        # `_drive_orders` can build its menu at that instant. Set per bar and never read
        # outside one.
        self._bars = None
        # One-shot: `dol_reached` no longer kills the plan, and the condition stays true
        # for the rest of the session once met.
        self._dol_reached_recorded = False
        # The plan's OBJECTIVE: the most recent T2 pick, and the instant it was picked.
        # Reaching it kills the plan (`target_reached`) — the draw the plan existed to
        # trade has been delivered, so there is nothing left for it to do.
        self._target_price = None
        self._target_level = None
        self._target_since = None
        # Plan 41: the DEFAULT pick of the current fill (what `_set_target_on_fill`
        # chose), kept so `reset_target` can restore it after an operator override, and
        # the menu that pick came from, so `trade.py agent-target --list` can show the
        # operator the rows to choose between.
        self._default_pick = None
        self._menu_rows = None
        # Plan 35: the initial-target stage of the CURRENT position, or None. Armed at
        # the fill (`_arm_initial_target`), driven on completed 1m bars
        # (`_drive_initial_target`), dropped once the bar containing the exit has been
        # judged. Holds the tracker plus the fill / exit minutes it needs.
        self._it = None
        # The OUTGOING stage whose exit bar has not been judged yet when a new fill
        # arrives inside the same minute (exit + re-fill): parked here so its flip /
        # counterfactuals are still recorded once, then dropped.
        self._it_pending = None
        # §6/§7, finally reachable. Built here rather than lazily so `state()` is
        # inspectable from the first bar; §7's own machine is seeded on the first bar
        # that can measure its anchor (see `MarketMechanisms.seed_sec7`).
        self._market = MarketMechanisms(
            self._plan.get("direction"), arm_ts,
            max_attempts=int(self._plan.get("max_attempts") or MAX_ATTEMPTS))
        self._emitted: set = set()
        # One-shot: the falsifier is recorded the FIRST time it fires and never again.
        # It is not a state change, so re-recording it every bar would bury the session.
        self._falsify_recorded = False
        # O4 (`micro_smt_exit`): one refusal record per POSITION, not per bar — reset at
        # every fill (`_set_target_on_fill`), so a live session shows each time O4 would
        # have exited a DIFFERENT position, not the same observation repeated all day.
        self._micro_smt_exit_unwired_recorded = False
        self._vetoed: set = set()
        self._last_minute = None
        # The last bar instant this Executor was handed. BAR time, never a wall clock
        # (the gate in `test_executor.py` forbids one here). Only `mark_open_position`
        # reads it, and only after the loop has finished.
        self._last_ts = None
        # The stop-out whose §2 cooldown-end resolution has already run, so the
        # resolution fires exactly once per stop-out.
        self._cooldown_resolved_for = None
        # §5's two-phase gate. Lazy: the settle end is DATE-dependent and `arm_ts`
        # may be supplied later in replay, so it is built on the first bar.
        self._retrace = None
        # The bar row `_drive_orders` last saw, so the fresh-retrace path
        # can resolve an order it places against the SAME bar.
        self._last_row = None
        # §8: the open deeper-gap penetration window, or None.
        self._tk_window = None
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
            # `eval_predicate` maps anything malformed to False, so a typo'd falsifier
            # is indistinguishable from one that never triggers and the plan runs the
            # whole window. `derive_plan` copies the thesis's predicates verbatim and
            # validates nothing, so this is the only place it can be caught. Captured
            # as structured state rather than printed — production is silent.
            "predicate_errors": validate_predicate_list(
                self._plan.get("valid_while") or [], "valid_while"),
            "order_error": None,
            "retrace_error": None,
            "takeover_error": None,
            "cooldown_resolution": None,
            "takeover_id": None,
            "takeover_label": None,
            "initial_target": None,
            "initial_target_error": None,
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
        self._last_ts = now
        # Held for `_set_target_on_fill`: a fill surfaces inside `_drive_orders`, which
        # is handed only the MNQ frame, but the T2 menu needs BOTH tickers.
        self._bars = bars
        if self._arm_ts is not None and now >= self._arm_ts:
            self._state["tracking"] = True
        self._state["in_settle"] = self._in_settle(now)

        mnq = truncate(normalize((bars or {}).get(self._ticker)), now)
        if len(mnq):
            last = mnq.iloc[-1]
            self._state["now_price"] = float(last["Close"])
            # §11's market-fill convention is the 1s MID of the bar at placement, not
            # its close. The two differ by well under §11's own +/-2 pt tolerance, but
            # the convention is what the calibrated rows were measured against.
            self._state["now_mid"] = (float(last["High"]) + float(last["Low"])) / 2.0
            # The bar's EXTREMES, for the crossed-trigger test only. `OrderSim` fills a
            # resting order on `High >= trigger` / `Low <= trigger`, so testing
            # "already crossed" against the mid or the close leaves any trigger the bar
            # traded through — but did not close through — resting, to be filled AT the
            # trigger on a later bar, at a price the tape has already passed. The test
            # and the fill must read the same basis.
            self._state["now_high"] = float(last["High"])
            self._state["now_low"] = float(last["Low"])

        # 1. FACTS MAINTENANCE FIRST, and unconditionally — before the plan-death
        # check, so a dead plan does not freeze the store. Facts are SESSION state; a
        # plan is not. Skipped only when the graft owns the maintainer, in which case
        # the graft has already driven it this bar (see the ownership rule in
        # `__init__`) and running it here would cascade twice.
        if self._owns_maintainer:
            self._maint.on_bar(now, bars, bar_complete)
        if bar_complete:
            self._state["last_cascade"] = self._maint.last_cascade or str(now)

        # 2. THE ORDER LIFECYCLE, before the plan-death check and on EVERY call, not
        # only on a bar close.
        #
        # Before the death check for the same reason facts maintenance is: a position
        # is not plan state. `_death` early-returns once the plan dies, so a fill left
        # open at the DOL touch would never be booked and the attempt budget would be
        # silently wrong. Per-call rather than per-bar-close because the driver hands
        # over 1s bars: resolving fills and stop-outs only at the minute close would
        # mis-time the §2 cooldown by up to 59 s and lose intra-minute stop-outs
        # entirely — §10.2's own lesson that this tracking has to be tick-level.
        #
        # §8's temporary gates first: once entries are blocked, an order still resting
        # UNFILLED is withdrawn BEFORE this bar's tape can fill it — otherwise a stop
        # placed at 10:29 becomes a 10:31 entry. A position already open is untouched.
        self._state["entry_block"] = self._entry_block(now)
        if (self._state["entry_block"] is not None and self._sim.resting is not None
                and self._sim.position is None):
            self._sim.cancel()
        self._last_row = mnq.iloc[-1] if len(mnq) else None
        self._drive_orders(now, mnq)

        # 2b. §8's WINDOW END, once, at the first bar at or after it — and whether or
        # not the plan is still alive, because a position outlives its plan. After the
        # stop/target test above, so a bar that reaches both books the adverse one. The
        # position is MARKED, not exited (`order_sim.mark_open`); a mirroring port turns
        # that mark into the window-end close.
        if not self._window_ended and self._at_window_end(now):
            self._window_ended = True
            self.mark_open_position()
        # 2b. Plan 35: the initial-target stage, on the COMPLETED 1m bar only and
        # BEFORE the plan-death check, for the same reason the lifecycle above is: a
        # position is not plan state, and a plan that dies with a position open must
        # not blind its management. The order events of this call are already booked,
        # so a same-bar stop-out is visible here and wins (§2.4).
        self._drive_initial_target(now, mnq, bar_complete)

        # 2c. O4 (`micro_smt_exit`, ADOPTED, flag-gated ON by default): a counter-thesis
        # micro-SMT market-closes an OPEN position, T2 or no T2, regardless of plan life — same
        # reasoning as 2b, an open position is not plan state. Total for the same reason
        # `_drive_orders` is: a bug here must not take the bar loop down.
        try:
            self._drive_micro_smt_exit(now, mnq, bar_complete)
        except Exception as exc:
            self._state["market_mech_error"] = f"{type(exc).__name__}: {exc}"

        # 3. Plan death — evaluated every bar, with nothing open. Over bars SINCE THE
        # ARM only: the frame reaches back to the 18:00 session open, and overnight /
        # pre-RTH traversal of a level the plan later names as its DOL is routine.
        # Judging a plan by price action that predates it kills almost every plan on
        # its first bar.
        if self._state["plan_alive"]:
            reason, detail = self._spine_death()
            if reason is None:
                reason, detail = self._death(self._since_arm(mnq), now, bar_complete)
            if reason is not None:
                self._kill_plan(now, reason, detail)
        if not self._state["plan_alive"]:
            return                    # no more BINDING; facts above keep accumulating

        # 3b. §5 phase one, PER SECOND. The fresh-retrace precondition is a TICK
        # test: sampling it only on bar closes would miss every penetration that
        # opens and closes inside one minute, which is most of them.
        # §8's penetration window opens on the stop-out bar and closes on the first tick
        # at or after the cooldown expiry — the same tick §2's cooldown-end resolution
        # acts on, and therefore the next attempt. That boundary tick's own range is
        # deliberately NOT folded in: the scan must answer "what did price penetrate
        # BETWEEN the stop-out and the re-entry decision", and including the decision
        # tick itself would let the re-entry justify its own binding.
        if self._tk_window is not None:
            try:
                if self._in_cooldown(now):
                    self._extend_takeover_window(now)
                else:
                    self._tk_window = None
            except Exception as exc:
                self._tk_window = None
                self._state["takeover_error"] = f"{type(exc).__name__}: {exc}"

        # Both halves record their failures rather than swallowing them, for the reason
        # `_drive_orders` spells out: a raise on every tick makes the mechanism inert,
        # and inert reads EXACTLY like "the precondition was never met" — which is what
        # 08-21's and 08-11's FLAT verdicts also look like. Production is silent, so the
        # diagnostic goes into state.
        try:
            opened = self._note_retrace(now)
        except Exception as exc:
            opened = set()
            self._state["retrace_error"] = f"{type(exc).__name__}: {exc}"
        try:
            self._place_on_fresh_retrace(now, opened)
        except Exception as exc:
            self._state["retrace_error"] = f"{type(exc).__name__}: {exc}"

        # 3c. §6's TICK path. Clause 1 makes intra-bar ordering load-bearing — the
        # episode begins at the tick that first enters the gap and the runaway test runs
        # only over ticks at or after it — so this cannot wait for the bar close.
        try:
            self._drive_market_mechanisms(now, mnq, bar=None)
        except Exception as exc:
            self._state["market_mech_error"] = f"{type(exc).__name__}: {exc}"

        if not bar_complete:
            return                                    # per-second path ends here

        # 4. Re-bind, then guards. Coverage and the ATR proxy were refreshed by the
        # maintainer above.
        try:
            self._rebind_and_guard(now)
        except Exception:
            pass

        # 4b. §7 and §6's close verdict, AFTER the re-bind so `usable_5m_gaps` and the
        # resting slot reflect this bar. §6 is disarmed whenever a usable 5m gap exists
        # (`Arbiter.sec6_armed`), which is what keeps it from coexisting with a resting
        # 5m stop-entry.
        try:
            self._drive_market_mechanisms(now, mnq, bar=self._completed_1m(mnq, now))
        except Exception as exc:
            self._state["market_mech_error"] = f"{type(exc).__name__}: {exc}"

    # -- internals -------------------------------------------------------------- #

    def _label_for(self, artifact_id):
        """The human label for an artifact id. `records.py` states the two ALWAYS travel
        as a pair — the id alone is unreadable six weeks later."""
        try:
            fact = self._store.get(artifact_id) if artifact_id else None
            return fact.label if fact is not None else None
        except Exception:
            return None

    def _drive_orders(self, now: pd.Timestamp, mnq: pd.DataFrame) -> None:
        """Feed the simulated lifecycle this bar and record what it produced.

        Total: an order-book failure must never take the bar loop down, exactly like
        the facts cascade above it.
        """
        try:
            if not len(mnq):
                return
            for ev in self._sim.on_bar(now, mnq.iloc[-1]):
                # Recorded FIRST: the artifact must carry the event even if the
                # bookkeeping below raises.
                self._rec.order_event(
                    now=now, plan_id=self._plan.get("plan_id"),
                    mechanism=self._state.get("mechanism"),
                    artifact_label=self._label_for(ev.get("artifact_id")), **ev)
                if ev.get("kind") == "fill":
                    # The target is chosen HERE, one bar-event late by construction:
                    # `OrderSim.on_bar` fills and then tests the take-profit within the
                    # same call, so a bar that both fills and reaches the target books
                    # no take-profit. Accepted — the T2 pick is a menu row at least
                    # `max(5pts, 1.0 x avg_1h)` away (`DOL_MIN_DRAW_RATIO`), so a
                    # same-bar touch would mean a ~74 pt second, and resolving it the
                    # other way would be the free-points error §11 warns about.
                    self._set_target_on_fill(now)
                if ev.get("kind") in ("stop_out", "take_profit", "stop_out_initial"):
                    self._note_exit(ev)
                if ev.get("kind") == "stop_out":
                    self._on_stop_out(ev)
                if ev.get("kind") in ("stop_out", "take_profit"):
                    self._note_close(ev)
        except Exception as exc:
            # Swallowed so an order-book bug cannot take the bar loop down — but NOT
            # silently. A raise here on every bar makes the whole lifecycle inert:
            # nothing fills, nothing stops out, the attempt budget stays at zero, and
            # the artifact reads exactly like "no entry was ever triggered". The
            # diagnostic goes into state (project convention: production is silent).
            self._state["order_error"] = f"{type(exc).__name__}: {exc}"

    def mark_open_position(self) -> "dict | None":
        """Book a position still open when the RUN ends, at the last bar it was handed.

        Called by the replay runner AFTER the bar loop, never from inside it — which is
        why it takes no arguments: the instant and the price are the last ones this
        Executor actually saw, so the runner cannot invent either. Returns the recorded
        event, or None when nothing was open.

        A MARK is not an exit; `order_sim.mark_open` carries the reasoning.
        """
        try:
            price = self._state.get("now_price")
            if self._last_ts is None or price is None:
                return None
            ev = self._sim.mark_open(self._last_ts, price)
            if ev is None:
                return None
            self._rec.order_event(
                now=self._last_ts, plan_id=self._plan.get("plan_id"),
                mechanism=self._state.get("mechanism"),
                artifact_label=self._label_for(ev.get("artifact_id")), **ev)
            return ev
        except Exception as exc:
            self._state["order_error"] = f"{type(exc).__name__}: {exc}"
            return None

    def _kill_plan(self, now, reason, detail) -> None:
        self._state["plan_alive"] = False
        self._state["dead_reason"] = reason
        self._state["bound_id"] = None
        self._state["bound_label"] = None
        self._state["mechanism"] = None
        self._state["trigger"] = None
        self._state["stop"] = None
        self._rec.plan_dead(now=now, plan_id=self._plan.get("plan_id"),
                            reason=reason, detail=detail)
        # An unfilled order belongs to the dead plan and is withdrawn. An OPEN
        # position is not withdrawn — it keeps being managed by `_drive_orders`
        # until it stops out, reaches its target or the window ends.
        self._sim.cancel()

    def kill_plan(self, now, reason, detail=None) -> None:
        """Kill the plan FROM OUTSIDE the bar loop's own death rules. Idempotent.

        The graft's `external_kill` calls this first, before anything else it does, so
        that no re-entry is possible from the instant something outside this Executor
        is known to have changed the position."""
        if self._state["plan_alive"]:
            self._kill_plan(now, reason, detail or {})

    def void_position(self) -> None:
        """Drop the modelled position with NO event and NO record of an exit: the
        caller has established that it no longer exists. Not a close — nothing was
        decided here — so no attempt is spent and no cooldown starts."""
        void = getattr(self._sim, "void_position", None)
        if callable(void):
            void()
            return
        self._sim.position = None
        self._sim.set_target(None)

    def position(self) -> "dict | None":
        pos = self._sim.position
        return dict(pos) if pos else None

    def order_context(self) -> dict:
        """What a mirroring port stamps on every event it reports."""
        return {"plan_id": self._plan.get("plan_id"),
                "mechanism": self._state.get("mechanism")}

    def _day_ts(self, now: pd.Timestamp, hm) -> pd.Timestamp:
        """`hm` on the ARM's date, not on the date of `now`: the live bar loop runs the
        whole CME session, and an evening bar is past 13:00 on its own calendar day."""
        anchor = self._arm_ts if self._arm_ts is not None else now
        return anchor.normalize() + pd.Timedelta(hours=hm[0], minutes=hm[1])

    def _at_window_end(self, now: pd.Timestamp) -> bool:
        return now >= self._day_ts(now, WINDOW_END_ET)

    def _entry_block(self, now: pd.Timestamp):
        """Why NO NEW ENTRY may be taken at `now`, or None. §8's spine gates: they say
        nothing about a position already open, which keeps being managed."""
        if getattr(self._sim, "external", None):
            return "external_position_change"
        if NO_ENTRY_AFTER_POSITIVE and self._positive_close:
            return "after_positive_trade"
        if ENTRY_CUTOFF_ET is not None and now >= self._day_ts(now, ENTRY_CUTOFF_ET):
            return "entry_cutoff"
        return None

    def _micro_smt_entry_block(self, now: pd.Timestamp):
        """Why `micro_smt_reject` (O3) specifically may NOT enter at `now`, or None.

        O3 is an explicit operator EXEMPTION from `ENTRY_CUTOFF_ET` (2026-09-24) — it
        carries its own WINDOW instead of the shared cutoff (operator, 2026-09-26):
        `MICRO_SMT_ENTRY_WINDOW_ET` = 10:30 <= now < 11:00 ET, both ends read live off the
        module so a rollback or an A/B can move them between runs in one process. Every
        other block reason still applies to it exactly as it does to every other
        mechanism, and this is the ONLY gate consulted before O3's own detector runs — a
        `now` outside the window must never reach `micro_smt_entry_on_bar_close` at all.
        """
        if getattr(self._sim, "external", None):
            return "external_position_change"
        if NO_ENTRY_AFTER_POSITIVE and self._positive_close:
            return "after_positive_trade"
        window = micro_smt.MICRO_SMT_ENTRY_WINDOW_ET
        if window is None:
            return "micro_smt_entry_window_closed"
        start, end = window
        if now < self._day_ts(now, start):
            return "micro_smt_entry_before_window"
        if now >= self._day_ts(now, end):
            return "micro_smt_entry_after_window"
        return None

    def _spine_death(self):
        """Deaths the plan's own rules cannot see: the order port reporting that the
        position changed behind it, and the window ending."""
        ext = getattr(self._sim, "external", None)
        if ext:
            return "external_position_change", dict(ext)
        if self._window_ended:
            return "window_end", {"window_end": "%02d:%02d" % WINDOW_END_ET}
        return None, None

    @staticmethod
    def _is_profitable(ev: dict) -> bool:
        entry, price = ev.get("entry"), ev.get("price")
        if entry is None or price is None:
            return False
        sign = -1.0 if str(ev.get("direction") or "").upper() in _SHORT else 1.0
        return sign * (float(price) - float(entry)) > 0

    def _note_close(self, ev: dict) -> None:
        """Latch the first PROFITABLE close. Today that is a take-profit or a profitable
        `micro_smt_exit` (O4, operator decision) — a stop is never trailed, and the
        target kills the plan on the same bar; the latch is what keeps the rule true if
        either of those stops being so."""
        if self._is_profitable(ev):
            self._positive_close = True

    def _settle_end_ts(self, now: pd.Timestamp) -> pd.Timestamp:
        """The instant the settle window closes on `now`'s date: 09:30:30 ET.

        Factored out of `_in_settle`, which computed it inline. §5's fresh-retrace gate
        needs the same instant, and two copies of this arithmetic would be a defect
        waiting to happen — the whole 08-21 verdict turns on which side of it a tick
        falls.
        """
        return now.normalize() + pd.Timedelta(
            hours=SETTLE_END_HOUR, minutes=SETTLE_END_MINUTE,
            seconds=SETTLE_UNTIL_SECONDS)

    def _in_settle(self, now: pd.Timestamp) -> bool:
        """arm -> 09:30:30 inclusive of the arm, exclusive of the end instant."""
        if self._arm_ts is None or now < self._arm_ts:
            return True
        return now < self._settle_end_ts(now)

    def _note_retrace(self, now: pd.Timestamp) -> "set":
        """§5 phase one, on the PER-SECOND path: feed every eligible 5m gap this tick.

        Returns the gap ids whose fresh-retrace precondition became satisfied ON THIS
        TICK, so the caller can act at the instant it opens rather than at the next 1m
        close (see `_place_on_fresh_retrace`).

        Constructed lazily rather than in `__init__` because the settle end is
        DATE-dependent and `arm_ts` may be supplied later in replay.

        Range basis, not close basis: `OrderSim` fills a resting order against the bar's
        extremes, so a retrace test reading only the close would refuse to place an order
        for a penetration the fill model already believes was reachable.
        """
        if now is None:
            return set()
        if self._retrace is None:
            self._retrace = RetraceGate(self._settle_end_ts(now))
        price = self._state.get("now_price")
        if price is None:
            return set()
        low = self._state.get("now_low", price)
        high = self._state.get("now_high", price)
        opened = set()
        # `initial=False`, NOT the plain eligible set. §2 exempts ladder / deepest-
        # penetration targets from the MINIMUM height, so `_ladder_target` can select a
        # sub-5-pt gap that `_eligible_gaps` never returns — and a gap the gate was never
        # fed can never earn a fresh retrace, so §5's placement would be blocked forever
        # and the ladder deepening silently inert (07-15's 2.75-pt gap is the documented
        # instance). The gate must see everything the binding path can bind.
        mechanism = self._entry_mechanism()
        want = self._selection_direction(mechanism) if mechanism else None
        for f in self._gaps_on(PRIMARY_TF, now, initial=False, direction=want):
            if self._retrace.fresh_entry_seen(f.id):
                continue
            self._retrace.note_tick(f.id, price, f.price_low, f.price_high, now,
                                    low=low, high=high)
            if self._retrace.fresh_entry_seen(f.id):
                opened.add(f.id)
        return opened

    def _place_on_fresh_retrace(self, now: pd.Timestamp, opened: "set") -> None:
        """§5 phase two, AT THE TICK the precondition opens — not at the next bar close.

        This is a correctness requirement of the fill model, not a nicety. §5's
        precondition is tick-level, and on 08-18 the fresh retrace and the trigger cross
        both fall inside the 09:40 minute: deferring placement to the 09:41:00 close
        finds the trigger ALREADY crossed, so §2 converts the entry into a market fill at
        the 1s mid (29764.375) instead of a resting stop filling AT its trigger
        (29763.25) — 1.125 pts worse, and one minute late, on a named case the doc pins
        to the second. The plan for this cycle did not anticipate it; it is the same
        class of error as the phase-1 ladder defect, where the fill model and the
        decision layer disagreed about what price the tape had already passed.

        Deliberately narrow: it fires only on the TRANSITION, only for the gap already
        bound (the bar-close path owns SELECTION), and only when nothing rests and
        nothing is open. Everything else stays on the bar-close cascade.
        """
        bound_id = self._state.get("bound_id")
        if not opened or bound_id not in opened:
            return
        if self._sim.resting is not None or self._sim.position is not None:
            return
        if self._state.get("in_settle") or self._in_cooldown(now):
            return
        if self._cooldown_pending():
            return                 # §8's cooldown-end precedence owns this resolution
        mechanism = self._state.get("mechanism")
        if mechanism != "fvg_return_continuation":
            return
        gap = self._store.get(bound_id)
        if gap is None:
            return
        had = self._sim.resting
        self._guard_and_place(now, mechanism, gap, self._state.get("now_price"),
                              resting_only=True)
        placed = (self._sim.resting is not None and self._sim.resting is not had)
        if placed and self._sim.position is None and self._last_row is not None:
            # The order exists DURING this bar, and this bar's tape may already reach
            # it. Resolving only on the next call would stamp 08-18's fill 09:40:59
            # against the doc's 09:40:58 and leave a second of the move unaccounted.
            self._drive_orders(now, pd.DataFrame([self._last_row]))

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

    def _death(self, mnq: pd.DataFrame, now: pd.Timestamp, bar_complete: bool = True):
        """(reason, detail) or (None, None). Evaluated with nothing open, every bar.

        `dol_reached` and `attempts_exhausted` are TICK-level — a DOL touch is a
        price-reached test and the 08-13 gate pins its 09:36:43 second. Falsification is
        NOT: it is close-based by decision (see `_falsified`), and the 1s driver's last
        frame row is the IN-PROGRESS minute, whose `Close` is the current second's.
        Evaluating predicates there would fire on an intra-minute tick that reverts
        before the close — precisely the wick-based behaviour this cycle rejected, just
        arriving through the frame instead of through `High.max()`.
        """
        # THE DOL NO LONGER KILLS THE PLAN (plan 16). It is recorded once and stepped
        # over, on the same terms falsification already had below: the DOL stopped being
        # the target when selection moved to the fill (`agent/trader/target.py`), so
        # ending the session on it was both an effect on ENTRY — every later bind is
        # blocked — and incoherent, since the level no longer has any role. The fire
        # time alone reconstructs the counterfactual: everything the plan did afterwards
        # is what dying there would have forgone.
        dol = self._dol_price()
        if dol is not None and len(mnq) and not self._dol_reached_recorded:
            if self._is_short():
                reached = float(mnq["Low"].min()) <= float(dol)
            else:
                reached = float(mnq["High"].max()) >= float(dol)
            if reached:
                self._dol_reached_recorded = True
                self._rec.would_have_killed(
                    now=now, plan_id=self._plan.get("plan_id"),
                    reason="dol_reached", detail={"dol": float(dol)})

        # THE T2 TARGET KILLS THE PLAN. It replaces the DOL in the role the DOL used to
        # hold, and for the same reason: the plan exists to trade one draw, and once that
        # draw completes there is nothing left to trade toward. Measured over bars at or
        # after the PICK, never the whole frame — the target is a level price traded
        # through freely before it was ever chosen.
        tgt = self._target_price
        if tgt is not None and self._target_since is not None and len(mnq):
            since = mnq[mnq.index >= self._target_since]
            if len(since):
                hit = (float(since["Low"].min()) <= tgt if self._is_short()
                       else float(since["High"].max()) >= tgt)
                if hit:
                    return "target_reached", {"target": tgt, "level": self._target_level}

        cap = self._plan.get("max_attempts")
        if cap is not None and int(self._plan.get("attempts_used") or 0) >= int(cap):
            return "attempts_exhausted", {"attempts_used": self._plan.get("attempts_used")}

        # FALSIFICATION IS RECORDED, NEVER ACTED ON (2026-08-29).
        #
        # `l2-mechanisms.md` has lookahead BY DESIGN: it assumes L1's direction and DOL
        # were right, because the studies were done knowing what the graph did. A
        # falsifier only has work to do when the direction is WRONG, which cannot happen
        # inside that frame — so the doc never used one. §7's backcheck states its own
        # method plainly: "a fire cannot occur after the plan's DOL is touched". Even
        # §10.2, the one place with a deliberately inverted thesis, names its brakes as
        # the DOL-floor veto, early sweeps completing plans flat, and the §6/§7/§8 gates
        # — not predicates.
        #
        # So acting on falsification was OUR addition, not the spec's, and it put the
        # implementation ahead of every number the record contains. Recording it keeps
        # the evidence for a later decision (block or kill on falsification) without
        # changing a single outcome now: this branch returns None, so plan death is
        # exactly `dol_reached` / `attempts_exhausted`, which is what the studies used.
        if bar_complete and self._state["plan_alive"]:
            fired = self._falsified(mnq, now)
            if fired is not None and not self._falsify_recorded:
                self._falsify_recorded = True
                self._rec.would_have_falsified(
                    now=now, plan_id=self._plan.get("plan_id"),
                    predicate=fired.get("predicate") if isinstance(fired, dict) else fired,
                    detail=fired)
        return None, None

    def _market_view(self, now: pd.Timestamp, mnq: pd.DataFrame) -> MarketView:
        """The frozen view a predicate reads. Close-based by construction: `price` is
        the last COMPLETED close, and `closes_by_tf` carries completed closes only —
        which is why `_falsified` is only ever called on a bar close.

        EVERY field the closed vocabulary can read is populated. A view that answers
        only `1m` and hands back empty sweep sets does not fail loudly on a `5m` or a
        `level_swept` predicate — `eval_predicate` maps what it cannot answer to False,
        so such a falsifier would NEVER fire and the plan would run the whole window.
        That is the same silent-death-of-falsification bug as the `kind`/`type` key,
        one layer up, and `validate_predicate` accepts both shapes so nothing else
        catches it.

        `since` is the plan's arm, so `time_elapsed` measures plan age — the plan is
        the monitored decision here, exactly as the L1 thesis is in the bench.
        """
        closes = [float(c) for c in mnq["Close"].to_numpy()] if len(mnq) else []
        by_tf = {"1m": closes, "1min": closes}

        # Higher timeframes, off a bounded tail: `n_closes_beyond` reads at most `n`
        # closes, and resampling the whole session frame on every bar close was the
        # single largest cost the facts layer had to remove for exactly this reason.
        if len(mnq):
            tail = mnq[mnq.index >= mnq.index[-1] - HTF_VIEW_TAIL]
            for pred_tf, bars_tf in (("5m", "5min"), ("15m", "15min"),
                                     ("1h", "1h"), ("4h", "4h")):
                try:
                    # `resample` aggregates Volume, so a frame without that column
                    # raises here and the timeframe is simply absent from the view.
                    # Every production frame carries it; test fixtures must too.
                    frame = resample(tail, bars_tf)
                except Exception:
                    continue
                if len(frame):
                    by_tf[pred_tf] = [float(c) for c in frame["Close"].to_numpy()]

        swept, depleted = set(), set()
        try:
            for f in self._store.query(cls=FactClass.LEVEL, ticker=self._ticker):
                if not f.name:
                    continue
                if f.state is FactState.DEPLETED:
                    depleted.add(f.name)
                    swept.add(f.name)          # depletion implies the sweep happened
                elif f.state is FactState.SWEPT:
                    swept.add(f.name)
        except Exception:
            pass

        return MarketView(
            price=closes[-1] if closes else None,
            closes_by_tf=by_tf,
            swept=swept, depleted=depleted,
            now=now, since=self._arm_ts,
        )

    def _falsified(self, mnq: pd.DataFrame, now: pd.Timestamp):
        """Evaluate the plan's `valid_while` with the CANONICAL evaluator.

        The predicate key is `type` (schemas.py `{"type": {"const": ptype}}`), not
        `kind`. Reading `kind` made this dead code: verified at 00b2ac0, a plan whose
        falsifier sat 180 pts behind price stayed alive for the whole run.

        Close-based, not wick-based: `eval_predicate` reads `MarketView.price`, a single
        completed close. The earlier code compared `High.max()`/`Low.min()` since the
        arm and would have fired on a wick. Close-based is what `predicates.py`
        implements, what the manual L1 harness walks forward with, and what §2's
        close-based eligibility philosophy matches.
        """
        if not len(mnq):
            return None
        mv = self._market_view(now, mnq)
        for pred in self._plan.get("valid_while") or ():
            if eval_predicate(pred, mv):
                return {"predicate": pred}
        return None

    def _height_ok(self, gap, *, initial: bool) -> bool:
        """§2's height band.

        The MINIMUM filters drift-noise gaps that carry no displacement information, and
        applies to INITIAL binding only: in the ladder-target / deepest-penetration role
        a small gap is not displacement evidence, it refines the price and stop of an
        entry an already-bound gap justified (07-15's 2.75-pt gap was that day's true
        rejection level and its safest stop anchor).

        The MAXIMUM has no stated exemption and applies in both roles.
        """
        try:
            height = float(gap.price_high) - float(gap.price_low)
        except (TypeError, ValueError):
            return False
        if height > MAX_FVG_HEIGHT_PTS:
            return False
        if initial and height < MIN_FVG_HEIGHT_PTS:
            return False
        return True

    def _distance_dead(self, gap) -> bool:
        """§2's permanent distance invalidation.

        Once price has displaced beyond the gap in the ANTI-TRADE direction by more than
        the threshold, the gap is dead for entry for the rest of the session and does
        NOT revive when price returns. A real reversal builds gradually and leaves fresh
        structure; a violent return to a distant stale gap is the deception pattern.

        The threshold must stay >= 60: 07-23's ladder rally peaked 56.25 pts beyond its
        bound gap and its +304 winner has to survive, while the 07-21 / 08-05 judas
        excursions ran 68-90+ pts and must be killed.

        `max_anti_excursion` is maintained by `fvg.update_fvg_states` on every pass; it
        is a running maximum, which is what makes the kill permanent for free.
        """
        try:
            excursion = float(gap.extra.get("max_anti_excursion") or 0.0)
        except (TypeError, ValueError):
            return False
        return excursion > DISTANCE_INVALIDATION_PTS

    @staticmethod
    def _exists_by(fact, now) -> bool:
        """Has this fact's EXISTENCE instant arrived?

        §2 (pinned 2026-08-27): identity is the MIDDLE bar; EXISTENCE is the third bar's
        COMPLETION. Everything time-gated reads existence. Gating on `reference_ts`
        admits a 5m gap ten minutes early — the look-ahead the §11 08-21 erratum
        documents, where a 09:36:04 fill was recorded on a gap that existed at 09:40:00.

        The `or reference_ts` fallback exists only so facts predating the field stay
        readable; the detector always populates it.
        """
        if now is None:
            return True
        at = fact.extra.get("exists_from") or fact.reference_ts
        if at is None:
            return True
        # No unconditional `pd.Timestamp(at)`: this runs once per live gap per SECOND on
        # the retrace path, and the detector always stores a Timestamp already. The
        # re-wrap is kept only for facts loaded from a snapshot, where it may be a string.
        if not isinstance(at, pd.Timestamp):
            at = pd.Timestamp(at)
        return at <= now

    def _gaps_on(self, tf: str, now: pd.Timestamp, *, initial: bool = True,
                 direction: "str | None" = None) -> list:
        """Live MNQ gaps on ONE timeframe facing `direction` (default: the trade
        direction).

        §4's 1m fallback, §6 and §8's takeover all need 1m gaps. §11 records two
        independent manual walks that scanned only 5m and produced a wrong 07-21 ledger
        and a mis-bound 07-31 takeover — a timeframe-blind query reproduces exactly that
        failure, so the timeframe is an explicit argument with no default.

        Blacklisted artifacts are excluded — a gap that already produced a stop-out and
        whose edge a deeper penetration falsified is not re-bindable for this plan (§8).

        `initial=False` is the LADDER-TARGET / deepest-penetration role, exempt from
        §2's minimum height (but not its maximum).
        """
        want = direction or ("bear" if self._is_short() else "bull")
        black = set(self._plan.get("blacklist") or ())
        out = []
        for f in self._store.query(cls=FactClass.FVG, ticker=self._ticker,
                                   state=FactState.LIVE):
            if f.timeframe != tf or f.extra.get("direction") != want:
                continue
            if f.id in black:
                continue
            if not self._exists_by(f, now):
                continue
            if not self._height_ok(f, initial=initial):
                continue
            if tf == PRIMARY_TF and self._distance_dead(f):
                continue          # §4: 5m distance invalidation does NOT extend to 1m
            out.append(f)
        return out

    def _eligible_gaps(self, now: pd.Timestamp, *, initial: bool = True) -> list:
        """The 5m query. Kept as a named method because it is the primary-timeframe call
        site the ladder and the §8 recency selection both use."""
        return self._gaps_on(PRIMARY_TF, now, initial=initial)

    # -- §4 / §6: mechanism-specific gap selection ---------------------------- #

    def _selection_direction(self, mechanism) -> str:
        """Which gap DIRECTION a mechanism binds.

        §5 rides the thesis, so it binds thesis-direction gaps (bear for a short). §4
        REVERSES the last trend, so it binds the COUNTER-thesis gap — "a bullish FVG
        from the uptrend when expecting down". Getting this backwards makes §4 bind the
        continuation gap and silently turns it into a second §5.

        `_levels_for` is unaffected: the trigger and stop are derived from the TRADE
        direction, so a short below a bullish gap's lower bound comes out correctly.
        """
        thesis = "bear" if self._is_short() else "bull"
        if mechanism == "fvg_negation_reversal":
            return "bull" if self._is_short() else "bear"
        return thesis

    def _within_max_distance(self, gap, price) -> bool:
        """§6's USABLE part (d): the gap's FIXED trigger is inside the max-distance
        guard of current price. Unlike (a)-(c) this is MOMENTARY, not permanent."""
        if price is None:
            return True
        try:
            trigger, _ = self._levels_for(gap)
        except (TypeError, ValueError):
            return False
        return abs(float(trigger) - float(price)) <= MAX_DISTANCE_PTS

    def usable_5m_gaps(self, now: pd.Timestamp, price, *, direction=None) -> list:
        """§6's four-part USABLE test, SETTLED 2026-08-26 and exhaustive.

        A thesis-direction 5m gap is USABLE at a moment iff ALL of: (a) height in the
        [min, max] band, (b) NOT inverted — no 5m close through it in the anti-trade
        direction since creation, (c) NOT permanently distance-invalidated, (d) its fixed
        trigger within the max-distance guard of current price. (a)-(c) are what
        `_gaps_on` already enforces — the height band, `FactState.LIVE`, and
        `_distance_dead`; (d) is added here.

        The former "or offers bad risk:reward to the DOL" clause is DELETED. Solving for
        its threshold from the recorded days gives 0 <= R* < 3.57 with NO lower
        constraint at all — at every recorded §6 fire the count of usable 5m gaps was
        ZERO. R* = 0, the clause is inert, and rebuilding it is on §11.0's retired list.

        This is the state §6 arms on and §4's 1m fallback widens into: "the same
        5m-unusable state that arms §6".
        """
        return [g for g in self._gaps_on(PRIMARY_TF, now, direction=direction)
                if self._within_max_distance(g, price)]

    def _created_at_or_after_rth(self, gap, now: pd.Timestamp) -> bool:
        """§4's 1m-fallback filter: the gap's CREATING (third) bar at or after 09:30 ET.

        EXISTENCE-side, and §2 says so explicitly — read `exists_from`, never
        `reference_ts`. A 1m gap identified 09:29 exists at 09:31 and qualifies; one
        identified 09:28 exists at 09:30 and also qualifies. The pattern's EARLIER bars
        may be pre-open, which is load-bearing on 08-03, whose rescue gap builds on the
        09:29 bar. Reading the identity here silently changes which rescue gaps exist.
        """
        at = gap.extra.get("exists_from") or gap.reference_ts
        if at is None or now is None:
            return False
        open_ts = now.normalize() + pd.Timedelta(hours=RTH_OPEN_HOUR,
                                                 minutes=RTH_OPEN_MINUTE)
        return pd.Timestamp(at) >= open_ts

    def _selection_gaps(self, now: pd.Timestamp, mechanism, price) -> list:
        """The candidate set for `mechanism`, including §4's widened 1m fallback.

        §4's fallback (widened 2026-08-15) applies whenever NO USABLE 5m FVG exists for
        the reversal — the leg printed none, or every 5m candidate is
        distance-invalidated or beyond the max-distance guard. It then binds the most
        recently created eligible counter-thesis 1m FVG, requiring only that the gap's
        creating bar is at or after 09:30.

        §2's 5m distance invalidation does NOT extend to 1m-bound gaps (close-through
        eligibility only, for now) — that exemption lives in `_gaps_on`.
        """
        want = self._selection_direction(mechanism)
        if mechanism != "fvg_negation_reversal":
            return self._gaps_on(PRIMARY_TF, now, direction=want)

        usable = self.usable_5m_gaps(now, price, direction=want)
        if usable:
            return usable
        return [g for g in self._gaps_on("1min", now, direction=want)
                if self._created_at_or_after_rth(g, now)]

    def _in_no_move_zone(self, price) -> bool:
        """§8: do not cancel/replace while price is within ~15 pts of the resting
        trigger — re-binding there risks being flat during the exact displacement the
        order is waiting for."""
        trigger = self._state.get("trigger")
        if trigger is None or price is None:
            return False
        return abs(float(trigger) - float(price)) <= NO_MOVE_ZONE_PTS

    def _trigger_crossed(self, gap, price) -> bool:
        """Is this gap's entry trigger already beyond price in the trade direction?

        A long enters on a BUY stop above the gap, so it is crossed once price >=
        trigger; a short on a SELL stop below, crossed once price <= trigger. Binding
        such a gap is an immediate market entry at whatever price happens to be — never
        a selection we make on purpose.
        """
        trigger, _ = self._levels_for(gap)
        return (price <= trigger) if self._is_short() else (price >= trigger)

    def _is_deeper_or_same(self, gap, bound) -> bool:
        """Is `gap` at least as far along the ADVERSE path as `bound`?

        Adverse is down for a long and up for a short — the same direction of travel
        `_ladder_target` enforces. Re-selection shares the rule so the binding can only
        ever move away from price, never back toward it.
        """
        if gap.id == bound.id:
            return True
        if self._is_short():
            return float(gap.price_high) >= float(bound.price_high)
        return float(gap.price_low) <= float(bound.price_low)

    def _ladder_target(self, now: pd.Timestamp, price, *, bound_id=None,
                       direction=None):
        """§2's ladder re-bind: the DEEPEST eligible same-direction 5m gap along the
        adverse path that price has ticked INTO.

        Min-height EXEMPT (§2): in this role the gap is not displacement evidence, it
        refines the price and stop of an entry the bound gap already justified. The max
        height still applies — §2 states no exemption for it.

        Moves deeper only. A gap shallower than the current binding is never a ladder
        target; that direction of travel is what the deeper-gap takeover (§8, Phase 2)
        and ordinary re-binding handle.

        `bound_id` overrides "deeper than what". The binding layer churns freely, so by
        the time a stop-out is processed `_state["bound_id"]` may already name a gap the
        stopped-out position was never on — and §8's blacklist condition, the thing that
        keeps 07-23's validated same-gap re-entry alive, would then be judged against
        the wrong reference. `_on_stop_out` passes the FAILED gap explicitly.
        """
        if price is None:
            return None
        long_ = not self._is_short()
        anchor = bound_id if bound_id is not None else self._state.get("bound_id")
        bound = self._store.get(anchor) if anchor else None

        best = None
        # "the next eligible SAME-DIRECTION 5m FVG": same direction as the BINDING, which
        # for §4 is the counter-thesis one. Defaulting to the trade direction would make
        # the ladder search a gap set the negation binding is not even in.
        for f in self._gaps_on(PRIMARY_TF, now, initial=False, direction=direction):
            if not self._height_ok(f, initial=False):
                continue
            lo, hi = float(f.price_low), float(f.price_high)
            if not (lo <= float(price) <= hi):
                continue                       # price has not ticked INTO it
            if bound is not None:
                # deeper = further along the adverse path (down for a long, up for a short)
                if long_ and lo >= float(bound.price_low):
                    continue
                if not long_ and hi <= float(bound.price_high):
                    continue
            if best is None:
                best = f
            elif (lo < float(best.price_low)) if long_ else (hi > float(best.price_high)):
                best = f
        return best

    @staticmethod
    def _created(fact):
        """§8's recency key: EXISTENCE, never identity.

        Two gaps can rank in OPPOSITE orders under the two keys — an earlier-named gap
        whose third bar completes later is the newer artifact — so which key is used is a
        behaviour choice, not a formatting one.
        """
        return fact.extra.get("exists_from") or fact.reference_ts

    def _newest(self, gaps):
        """§8's binding preference: 'the most recently created eligible FVG'.

        A named method rather than an inline `max(...)` because it IS the selection rule,
        and a rule only a call site expresses cannot be tested without re-implementing it
        in the test — which asserts the test's arithmetic, not the engine's.
        """
        if not gaps:
            return None
        return max(gaps, key=lambda g: (self._created(g), g.id))

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

    def _on_stop_out(self, event: dict) -> None:
        """§8: consume an attempt, and blacklist the failed gap ONLY if a deeper gap
        was penetrated.

        The condition is not decoration. An unconditional blacklist breaks 07-23's
        validated same-gap re-entry; the blacklist exists because a stop-run THROUGH a
        gap's edge falsifies that edge, and that is only established when price has
        moved on to a deeper one.
        """
        self._plan["attempts_used"] = int(self._plan.get("attempts_used") or 0) + 1
        self._plan.setdefault("max_attempts", MAX_ATTEMPTS)
        # The SHARED per-plan budget, tallied per mechanism for the artifact only
        # (`Arbiter.spent_by` is "RECORDED, never scored"). Spent at the same instant as
        # the plan's own counter so the two can never disagree.
        try:
            self._market.arbiter.spend(self._state.get("mechanism"))
        except Exception:
            pass

        failed_id = event.get("artifact_id")
        if not failed_id:
            return
        now = pd.Timestamp(event.get("time"))
        # The penetration window opens on the STOP-OUT BAR ITSELF (§8) — on 08-14 the SL
        # tick and the takeover penetration are the SAME tick — and stays open until the
        # next attempt, so it is a running range, not a single reading.
        px = self._state.get("now_price")
        self._tk_window = {"low": self._state.get("now_low", px) or px,
                           "high": self._state.get("now_high", px) or px,
                           "failed_id": failed_id}
        self._scan_takeover(now)

    def _takeover_candidates(self, now: pd.Timestamp) -> list:
        """Every eligible thesis-appropriate gap on EVERY timeframe.

        §11 escalates this to an imperative — "Takeover scanner MUST enumerate gaps of
        ALL timeframes" — because two independent manual walks scanned only 5m and
        produced a wrong 07-21 ledger and a mis-bound 07-31 takeover. Before Task 2 the
        query could not express it: `_eligible_gaps` hard-filtered to 5m.

        `initial=False` is the min-height exemption: this is the deepest-penetration
        binding role, and 08-14's takeover gap is 3.0 pts tall. §4's creating-bar >= 09:30
        filter does not apply either — that gap was created 09:11.
        """
        out = []
        for tf in (PRIMARY_TF, "1min"):
            out.extend(self._gaps_on(tf, now, initial=False))
        return out

    def _scan_takeover(self, now: pd.Timestamp) -> None:
        """Re-run §8's deepest-penetration scan over the open window.

        Idempotent and monotone: the window only widens, so a later call can only find a
        DEEPER gap. Called at the stop-out and again on every tick until the cooldown
        resolves, because the window runs "between the stop-out and the next attempt".
        """
        win = self._tk_window
        if not win:
            return
        failed = self._store.get(win["failed_id"])
        if failed is None:
            return
        deeper = deepest_penetrated(self._takeover_candidates(now), failed,
                                    self._plan.get("direction"),
                                    low=win.get("low"), high=win.get("high"))
        if deeper is None:
            return                     # no deeper gap: the SAME gap may re-bind (07-23)
        self._state["takeover_id"] = deeper.id
        self._state["takeover_label"] = deeper.label
        # The blacklist is strictly CONDITIONAL on this penetration. An unconditional one
        # breaks 07-23's validated same-gap re-entry.
        bl = list(self._plan.get("blacklist") or ())
        if win["failed_id"] not in bl:
            bl.append(win["failed_id"])
        self._plan["blacklist"] = bl

    def _extend_takeover_window(self, now: pd.Timestamp) -> None:
        """Widen the open penetration window with this tick, then re-scan."""
        win = self._tk_window
        if not win:
            return
        lo, hi = self._state.get("now_low"), self._state.get("now_high")
        if lo is not None:
            win["low"] = lo if win.get("low") is None else min(win["low"], lo)
        if hi is not None:
            win["high"] = hi if win.get("high") is None else max(win["high"], hi)
        self._scan_takeover(now)

    def _in_cooldown(self, now: pd.Timestamp) -> bool:
        """§2: after a stop-out, no placement or triggering until the 1m bar in which
        the stop was hit CLOSES. State tracking continues throughout."""
        so = getattr(self._sim, "last_stop_out", None)
        if not so or so.get("time") is None:
            return False
        return now < pd.Timestamp(so["time"]).floor("1min") + pd.Timedelta(minutes=1)

    def _crossed(self, trigger, price) -> bool:
        """Is the trigger already beyond price in the TRADE direction?"""
        if trigger is None or price is None:
            return False
        return (float(price) >= float(trigger)) if not self._is_short() \
            else (float(price) <= float(trigger))

    def _snap_adverse(self, price: float) -> float:
        """Round a price onto the tick grid AGAINST the trade: up for a long, down for
        a short. A price already on the grid is returned unchanged — the epsilon guard
        is load-bearing, because `x / 0.25` on a float can land a hair under an
        integer and a bare floor() would then walk an exact tick backwards."""
        ticks = float(price) / TICK_PTS
        nearest = round(ticks)
        if abs(ticks - nearest) <= 1e-9:
            snapped = nearest
        else:
            snapped = math.floor(ticks) if self._is_short() else math.ceil(ticks)
        return round(snapped * TICK_PTS, 4)

    def _market_price(self):
        """§11's market-fill price: the 1s mid of the bar at placement, falling back to
        the close when no mid has been seen (direct-construction callers).

        SNAPPED TO THE TICK, ADVERSELY. The mid of a 1s bar spanning an ODD number of
        ticks lands halfway between two of them — 08-19's second entry booked
        **29672.875** on an instrument that trades in 0.25 — and every P&L quoted off a
        crossed trigger inherits a price no broker would give. The snap goes AWAY from
        the taker, which is `order_sim`'s standing rule that ambiguity resolves
        adversely applied to the one place the fill price itself was ambiguous. It
        moves a fill by at most half a tick, an order of magnitude inside §11's own
        ±2 pt market-fill tolerance, so no calibrated row moves.
        """
        mid = self._state.get("now_mid")
        if mid is None:
            return self._state.get("now_price")
        return self._snap_adverse(float(mid))

    def _crossing_price(self):
        """The price the CROSSED test reads: the bar's extreme in the trade direction,
        which is exactly what `OrderSim` fills a resting order against. Falls back to
        the close for direct-construction callers that set only `now_price`."""
        key = "now_low" if self._is_short() else "now_high"
        edge = self._state.get(key)
        return edge if edge is not None else self._state.get("now_price")

    def _enter(self, now: pd.Timestamp, trigger, stop, artifact_id) -> None:
        """§2: place a resting stop-entry, OR execute at market when the computed
        trigger is ALREADY CROSSED in the trade direction ("whenever L3 acts").

        Not an optimisation — a correctness requirement of the fill model. A buy-stop
        placed BELOW price is not a stop order: leaving it to rest lets the simulation
        book a fill at the trigger, i.e. at a price the tape had already passed. On
        08-13 that was a free 28.25 pts (trigger 29842.0 with price at 29870.25), an
        error five times §11's own market-fill tolerance and in the profitable
        direction — exactly the silent P&L drift Task 15 exists to catch.

        The CROSSED test reads the bar's extreme (`_crossing_price`) because that is
        what `OrderSim` fills against; the FILL reads the 1s mid (`_market_price`)
        because that is §11's calibrated market-fill convention. Using the mid for both
        would leave a trigger the bar traded through — but did not close through —
        resting, reintroducing the same free-points error one bar smaller.
        """
        if self._entry_block(now) is not None:
            return
        if self._crossed(trigger, self._crossing_price()):
            ev = self._sim.fill_market(now, direction=self._plan.get("direction"),
                                       price=self._market_price(), stop=stop,
                                       artifact_id=artifact_id)
            # Recorded HERE: a market fill never passes through `_drive_orders`, and an
            # unrecorded entry leaves the artifact showing an exit with no entry.
            self._rec.order_event(
                now=now, plan_id=self._plan.get("plan_id"),
                mechanism=self._state.get("mechanism"),
                artifact_label=self._label_for(artifact_id), **ev)
            # Same reason as the resting path: the target is anchored on the fill. A
            # fill the order port VOIDED is not one, and anchors nothing.
            if ev.get("kind") == "fill":
                self._set_target_on_fill(now)
            return
        self._sim.place(RestingOrder(direction=self._plan.get("direction"),
                                     trigger=float(trigger), stop=float(stop),
                                     artifact_id=artifact_id, placed_at=now))

    def _cooldown_pending(self) -> bool:
        """A stop-out whose cooldown has expired but whose resolution has not run."""
        so = getattr(self._sim, "last_stop_out", None)
        if not so or so.get("time") is None:
            return False
        return self._cooldown_resolved_for != so.get("time")

    def _resolve_cooldown_end(self, now: pd.Timestamp) -> None:
        """§2's cooldown-end resolution, under §8's precedence rule.

        NO fresh-precondition requirement — that is the difference from the settle
        window. Mid-session, post-stop state reflects a real displacement that just took
        the stop; demanding it repeat forfeits the move (07-24: the breakdown crossed the
        trigger 19 s after the stop, and a lockout watched the -280 collapse flat).

        §8 adds the precedence question this method now asks out loud. When a deeper-gap
        takeover HAS fired, the FAILED binding's trigger is checked FIRST: if price is
        already crossed beyond it, momentum resumed without us and §2's crossed-trigger
        market execution takes precedence — the takeover does NOT divert. Diverting there
        is what would have SKIPPED 07-24's +146.5 at 45.25 pts under the episode's SL-cap
        gate, recreating the exact lockout the cooldown rule was built to kill. If the
        trigger is UNCROSSED (07-21's three cooldown ends, and 07-31's), the
        takeover/episode path governs.

        The decision is delegated to `takeover.resolve_cooldown_end` rather than
        re-derived here: it is §8's rule, its ordering is load-bearing, and two copies of
        a three-way precedence rule is how they come to disagree.

        NOTE the third branch. `close_verdict` is passed as None because the episode
        machinery is not wired into this entry path in this cycle, so a completed close
        verdict of the stop-out bar can never be offered here; the resolution therefore
        collapses to crossed-trigger -> market, else resting. That is the FULL documented
        order minus a branch that cannot yet fire, not a different order.
        """
        trigger, stop = self._state.get("trigger"), self._state.get("stop")
        if trigger is None or stop is None or self._crossing_price() is None:
            return
        self._state["cooldown_resolution"] = resolve_cooldown_end(
            failed_trigger=trigger, price=self._crossing_price(),
            direction=self._plan.get("direction"), close_verdict=None)
        self._enter(now, trigger, stop, self._state.get("bound_id"))

    def _rebind_and_guard(self, now: pd.Timestamp) -> None:
        price = self._state["now_price"]
        mechanism = self._entry_mechanism()
        if price is None or mechanism is None:
            return

        # §2's stop-out cooldown. State tracking above continues; only placement and
        # triggering are suspended, and only until the stop-out's own 1m bar closes.
        if self._in_cooldown(now):
            return

        # §8's no-move zone: while an order rests within ~15 pts of price, nothing is
        # cancelled or replaced — re-binding there risks being flat during the exact
        # displacement the order is waiting for.
        if self._sim.resting is not None and self._in_no_move_zone(price):
            return

        # §2's ladder re-bind takes precedence over the closest-trigger selection while
        # a stop-entry rests UNFILLED: price ticking into a deeper gap along the adverse
        # path moves the order deeper, and only deeper.
        # Never re-bind while a position is open. §2 allows ONE stop-entry at a time,
        # and a bind recorded against an open position is a decision nothing can act on
        # — on 08-13 it produced a 09:33 bind that existed only to be vetoed at 09:36.
        if self._sim.position is not None:
            return

        want = self._selection_direction(mechanism)
        gap = (self._ladder_target(now, price, direction=want)
               if self._sim.resting is not None else None)
        if gap is None:
            gaps = self._selection_gaps(now, mechanism, price)
            if not gaps:
                self._clear_binding()
                return

            # The fallback is a SELECTION rule, and it used to be unconstrained: the
            # nearest trigger won outright. Two things follow from that, both observed
            # on 08-13 and both wrong:
            #
            #   1. It could move the binding BACKWARD — toward price, against the
            #      adverse path — which the ladder never does. (09:33: trigger 29842
            #      -> 29908.25 for a long.)
            #   2. It could select a gap price had never entered whose trigger was
            #      ALREADY CROSSED, so §2's crossed-trigger rule converted the bind
            #      into an instant market fill at the prevailing price. (09:32: bound
            #      [29808, 29839] with price at 29870.25, filling 29871.625 — 29.6 pts
            #      worse than the trigger, with 54.6 pts of risk instead of 25.)
            #
            # §8's actual preference is narrower: re-bind when a NEWER eligible gap
            # appears CLOSER to price while a stop-entry rests. So once a binding
            # exists and remains eligible, re-selection may only move DEEPER, and may
            # never choose an already-crossed trigger. §2's crossed-trigger EXECUTION
            # is untouched — it applies to a gap already bound that price later crosses
            # (the 08-14 cooldown-end case), not as a reason to pick a gap.
            bound = (self._store.get(self._state["bound_id"])
                     if self._state.get("bound_id") else None)
            still_eligible = bound is not None and any(g.id == bound.id for g in gaps)

            # §8: "the most recently created eligible FVG. When a NEWER eligible FVG
            # appears closer to price while a stop-entry rests, L3 re-binds."
            #
            # Cycle 1 substituted "nearest trigger wins" (its recorded decision A3), and
            # that is what produced both 08-13 defects: it re-bound BACKWARD toward price
            # at 09:33 (29842 -> 29908.25 on a long), and at 09:32 it jumped to a
            # 13-hour-old gap purely because its trigger was 28.25 pts away against 38 —
            # a gap price had never entered. Recency, the documented rule, does neither:
            # an older gap can never displace a newer one, so the binding cannot drift
            # backward or deepen into stale structure on distance alone.
            #
            # NOT re-added: a filter on already-crossed triggers. §2 is explicit that a
            # crossed trigger at INITIAL PLACEMENT executes as a market order — "the
            # resting stop-entry is the normal case, market the degenerate case of the
            # same mechanism". Excluding them silenced 08-25 completely.
            newest = self._newest(gaps)
            if still_eligible and self._created(newest) <= self._created(bound):
                gap = bound                # nothing newer has appeared; keep the binding
            else:
                gap = newest
        trigger, stop = self._levels_for(gap)

        if self._state["bound_id"] != gap.id:
            self._state.update({"bound_id": gap.id, "bound_label": gap.label,
                                "mechanism": mechanism})
            self._rec.bind(now=now, plan_id=self._plan.get("plan_id"),
                           mechanism=mechanism, artifact_id=gap.id,
                           artifact_label=gap.label, trigger=trigger, stop=stop)
        self._state.update({"trigger": trigger, "stop": stop})
        self._guard_and_place(now, mechanism, gap, price)

    def _guard_and_place(self, now, mechanism, gap, price,
                         *, resting_only: bool = False) -> None:
        """§2's guards, then §5's precondition, then the order.

        Split out of `_rebind_and_guard` so the per-second fresh-retrace path can reach
        exactly the same sequence — same guards, same veto records, same placement rules
        — without duplicating any of it. The bar-close path owns gap SELECTION; this owns
        what happens once a gap is selected.
        """
        if price is None:
            return
        trigger, stop = self._levels_for(gap)

        # --- guards, in order ------------------------------------------------- #
        distance = abs(trigger - price)
        if distance > MAX_DISTANCE_PTS:
            self._veto_once(now, mechanism, gap, "max_distance",
                            {"distance": round(distance, 4), "cap": MAX_DISTANCE_PTS,
                             "trigger": trigger, "price": price})
            return

        # THE DOL FLOOR NO LONGER VETOES (plan 16) — computed, recorded, stepped over.
        # It gated entries on room remaining to the 09:20 DOL, and that DOL is no longer
        # the target, so the quantity it measured no longer exists. Recorded rather than
        # deleted: the knob may come back against the T2 target instead.
        dol = self._dol_price()
        if dol is not None:
            remaining = (trigger - float(dol)) if self._is_short() else (float(dol) - trigger)
            if remaining < DOL_FLOOR_PTS:
                self._would_have_vetoed_once(
                    now, mechanism, gap, "dol_floor",
                    {"remaining": round(remaining, 4), "floor": DOL_FLOOR_PTS,
                     "trigger": trigger, "dol": float(dol)})

        if self._state["in_settle"]:
            return                                     # tracked, never entered (l2 §2)
        if self._entry_block(now) is not None:
            return                 # §8's temporary gates: bound and guarded, not entered

        # §5's two-phase binding: the order is placed only after a FRESH retrace INTO
        # the bound gap — a tick entering its range strictly after 09:30:30. Without
        # this the binding is a naked stop-entry on eligibility alone, which is the
        # LITERAL reading the 19-day A/B measured at +410.50 against FRESH's +570.50.
        #
        # Ahead of the `intended_entry` record, not after it: that record answers "where
        # would I have entered", and with the precondition unmet the answer is nowhere.
        # 08-21's named test asserts the absence of BOTH.
        #
        # Note this is NOT re-armed after a stop-out — §2's cooldown "acts on the current
        # state" with no fresh-precondition requirement, and re-arming here is the §5
        # re-entry churn guard §11.0 retired.
        if mechanism == "fvg_return_continuation" and (
                self._retrace is None or not self._retrace.fresh_entry_seen(gap.id)):
            return

        key = (mechanism, gap.id)
        if key not in self._emitted:
            self._emitted.add(key)
            self._rec.intended_entry(now=now, plan_id=self._plan.get("plan_id"),
                                     mechanism=mechanism, artifact_id=gap.id,
                                     artifact_label=gap.label, trigger=trigger,
                                     stop=stop, dol=self._dol_price(), price=price)

        # The intent becomes a RESTING STOP-ENTRY. Without this the lifecycle is
        # unreachable from a replay — nothing would ever rest, so nothing would fill,
        # and the ladder / cooldown / attempt budget would all be dead code. Placement
        # is NOT tied to the `intended_entry` dedup: the record is a readability
        # artifact, the order is state, and a ladder move re-places on a gap the record
        # has already named.
        #
        # Never while a position is already open: §2 allows one stop-entry at a time.
        if self._sim.position is not None:
            return
        if self._cooldown_pending():
            # First action at or after the cooldown expiry, on the state as re-bound
            # just above. Crossed trigger -> market; otherwise a resting placement.
            self._cooldown_resolved_for = (
                (self._sim.last_stop_out or {}).get("time"))
            self._resolve_cooldown_end(now)
            return
        cur = self._sim.resting
        if cur is None or cur.artifact_id != gap.id or cur.trigger != float(trigger):
            if resting_only:
                # §5's fresh-retrace path. NEVER the crossed-trigger market branch:
                # the precondition is "price is INSIDE the gap right now", and the
                # trigger sits one entry buffer beyond its far end, so price cannot
                # have crossed the trigger on an EARLIER bar and stayed there. What
                # can happen — and does on 08-18 — is that the same 1s bar both
                # enters the gap and reaches the trigger. §11's fill convention is
                # explicit for that: a resting stop fills AT ITS PRICE when the tape
                # reaches it, and filling it at the bar MID instead "mis-scores
                # 07-17 (-14.88 vs -17.75)". It is also `order_sim`'s stated
                # same-bar rule — resolve ADVERSELY — since for a short 29763.25 is
                # the worse fill of the two and 29764.875 the free 1.625 pts.
                self._sim.place(RestingOrder(
                    direction=self._plan.get("direction"), trigger=float(trigger),
                    stop=float(stop), artifact_id=gap.id, placed_at=now))
            else:
                self._enter(now, trigger, stop, gap.id)

    def _drive_market_mechanisms(self, now, mnq, *, bar) -> None:
        """§6 and §7: drive, arbitrate, and enter by MARKET if one fires.

        `bar=None` is the per-second path (§6's tick clauses only); a bar is the
        bar-close path (§7's machine plus §6's close verdict).

        Preconditions checked here rather than inside the machines, because they are the
        Executor's knowledge: nothing fires while a position is open (one position at a
        time), during the settle window, inside a stop-out cooldown (§6.1 clause 3, which
        ALSO resets every gap cycle), or with the shared attempt budget spent.
        """
        if not self._state["plan_alive"] or self._sim.position is not None:
            return
        if self._state["in_settle"] or not len(mnq):
            return
        block = self._entry_block(now)                 # §8's temporary spine gates
        micro_smt_only = block == "entry_cutoff" and micro_smt.MICRO_SMT_ENTRY_ENABLED
        if block is not None and not micro_smt_only:
            return
        if self._in_cooldown(now):
            # Clause 3: no cycle may complete while the cooldown is in force, and every
            # gap cycle resets across it.
            self._market.reset_cycles()
            return
        cap = self._plan.get("max_attempts")
        if cap is not None and int(self._plan.get("attempts_used") or 0) >= int(cap):
            return

        price = self._state.get("now_price")
        self._market.seed_sec7(self._since_arm(mnq))

        # §6's arming is the arbitration form of its own precondition.
        usable = self.usable_5m_gaps(now, price)
        armed6 = Arbiter.sec6_armed(usable_5m_gaps=len(usable or ()))
        self._market.sync_episodes(
            self._gaps_on(FALLBACK_TF, now) if armed6 else (), armed=armed6)

        fires = []
        if bar is None:
            # The tick path is §6 only, so it runs only when no spine gate blocked at
            # all: the `micro_smt_only` exemption is O3's alone (2026-09-03 A/B: without
            # this, §6 entered at 11:11 past the 10:30 cutoff whenever O3 was enabled).
            if block is not None:
                return
            fires.append(("fvg_1m_post_extreme",
                          self._market.sec6_on_tick(now, price,
                                                    bar_open=self._bar_open_of(mnq),
                                                    mid=self._market_price())))
        else:
            # `block is None` here means every ordinary spine gate passed (the
            # `micro_smt_only` branch above only lets O3 through the entry cutoff, and
            # every other mechanism must still respect it, so they run ONLY when there
            # was no block at all).
            if block is None:
                fires.append(("extreme_reject_close",
                              self._market.sec7_on_bar_close(now, bar)))
                fires.append(("fvg_1m_post_extreme",
                              self._market.sec6_on_bar_close(now, bar,
                                                             mid=self._market_price())))
                # CANDIDATE mechanism, armed like any other market mechanism: it fires
                # only when nothing is open and the budget allows, which is the "if we
                # didn't already enter" condition it was specified with.
                fires.append(("tmso_reject",
                              self._market.tmso_on_bar_close(now, bar, mnq)))
                fires.append(("fvg_1h_reject",
                              self._market.fvg1h_on_bar_close(now, bar, mnq)))
            if micro_smt.MICRO_SMT_ENTRY_ENABLED and self._micro_smt_entry_block(now) is None:
                mes = truncate(normalize((self._bars or {}).get("MES")), now)
                mes_bar = self._completed_1m(mes, now) if len(mes) else None
                if mes_bar is not None:
                    fires.append(("micro_smt_reject",
                                  self._market.micro_smt_entry_on_bar_close(
                                      now, bar, mes_bar, mnq, mes)))
        fire = self._market.pick(fires)
        if fire is None:
            return
        self._enter_by_market(now, fire)

    @staticmethod
    def _completed_1m(mnq, now):
        """The 1m bar that just COMPLETED, or None.

        §6's close verdict and §7 are state machines over completed 1m bars — both
        compare the bar's Close against its Open or against a level, and any later
        bar-close mechanism will too. The driver hands this Executor 1s bars, and
        the wiring's first cut passed
        `self._last_row`, the last 1s row: on a one-second bar Open and Close are the same
        print or one tick apart, so every colour test was noise and §7 could not fire at
        all. Left-labelled, so the minute that just closed at `now` carries label
        `now - 1min`.
        """
        try:
            if mnq is None or not len(mnq):
                return None
            label = now.floor("1min") - pd.Timedelta(minutes=1)
            seg = mnq[(mnq.index >= label) & (mnq.index < label + pd.Timedelta(minutes=1))]
            if not len(seg):
                return None
            return pd.Series({"Open": float(seg.iloc[0]["Open"]),
                              "High": float(seg["High"].max()),
                              "Low": float(seg["Low"].min()),
                              "Close": float(seg.iloc[-1]["Close"])}, name=label)
        except Exception:
            return None

    @staticmethod
    def _bar_open_of(mnq):
        """The in-progress minute's open — §6.1 clause 2's beyond-open condition."""
        try:
            return float(mnq.iloc[-1]["Open"])
        except Exception:
            return None

    def _enter_by_market(self, now, fire: dict) -> None:
        """A §6/§7 market entry. Same lifecycle as a resting fill: record, pick the
        target at the fill, spend one attempt from the SHARED budget."""
        mechanism = fire.get("mechanism")
        self._state["mechanism"] = mechanism
        ev = self._sim.fill_market(now, direction=fire.get("direction"),
                                   price=float(fire["price"]),
                                   stop=float(fire["stop"]),
                                   artifact_id=fire.get("gap_id") or mechanism)
        self._rec.order_event(now=now, plan_id=self._plan.get("plan_id"),
                              mechanism=mechanism,
                              artifact_label=self._label_for(ev.get("artifact_id")),
                              **ev)
        if ev.get("kind") == "fill":
            self._set_target_on_fill(now)
        # NO attempt is spent HERE. The budget counts STOP-OUTS, not entries
        # (`_on_stop_out`, and `order_sim`'s own "the attempt counter counts stop-outs"),
        # so incrementing on the fill double-counted every §6/§7 trade that then stopped
        # out — 09-03 read `attempts_used: 3` against two trades before this was fixed.

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

    def _would_have_vetoed_once(self, now, mechanism, gap, reason, detail) -> None:
        """Same per-(mechanism, gap, reason) dedupe as `_veto_once`, but the caller
        FALLS THROUGH. Shares `self._vetoed` so one observation cannot be recorded twice
        under two kinds."""
        key = (mechanism, gap.id, reason)
        if key in self._vetoed:
            return
        self._vetoed.add(key)
        self._rec.would_have_vetoed(
            now=now, plan_id=self._plan.get("plan_id"), mechanism=mechanism,
            reason=reason, detail=detail, artifact_id=gap.id, artifact_label=gap.label)

    def _set_target_on_fill(self, now) -> None:
        """Pick the T2 target at THIS fill and hand it to the simulated order book.

        Called from both fill sites — the resting-order path in `_drive_orders` and the
        crossed-trigger market path in `_enter`. Total: `select_target` swallows its own
        failures and returns None, and a None pick is recorded rather than retried, so a
        menu that cannot be built costs the target and nothing else.

        NOT re-run on later bars. T2 is "the nearest eligible draw AT THE FILL"; asking
        again every bar would be a different selector (continuous re-anchoring), which is
        unmeasured — see plan 16's out-of-scope list.
        """
        # A new fill is a new position: O4's unwired-refusal record is per-position, so
        # the next position that hits the same live-port gap is recorded again.
        self._micro_smt_exit_unwired_recorded = False
        pick = select_target(self._bars, now, self._plan.get("direction"), self._ticker,
                             **self._htf_kw())
        # Plan 41: the rows D1 was chosen from, and D1 itself, kept for the operator's
        # `agent-target` command (`--list` reads the file, `--reset` restores the pick).
        # Both are records of THIS fill, so they are replaced at every fill.
        self._default_pick = dict(pick) if isinstance(pick, dict) else None
        self._menu_rows = self._build_menu_rows(now)
        self._sim.set_target((pick or {}).get("price"))
        # Remembered BEYOND the position's life, unlike `OrderSim`'s copy: the target is
        # the plan's objective, so reaching it ends the plan even if the attempt that
        # chose it had already been stopped out. `_target_since` bounds the check to bars
        # at or after the pick — the same discipline `_since_arm` applies to the plan.
        price = (pick or {}).get("price")
        if price is not None:
            self._target_price = float(price)
            self._target_level = (pick or {}).get("level")
            self._target_since = now
        # After the bound target is assigned, so `active` names it (2026-09-23 D2).
        self._write_menu(now)
        self._rec.target_selected(
            now=now, plan_id=self._plan.get("plan_id"),
            mechanism=self._state.get("mechanism"), pick=pick)
        self._arm_initial_target(now, pick)

    # -- plan 41: the operator's target controls ---------------------------------- #

    MENU_FILE = "target_menu.json"

    def _write_menu(self, now) -> None:
        """The current menu, the bound target and the fill's default, on disk for
        `trade.py agent-target --list`. Best-effort: an artifact, never a guard."""
        try:
            import json as _json
            payload = {
                "time": str(now), "plan_id": self._plan.get("plan_id"),
                "direction": self._plan.get("direction"),
                "position": bool(self._sim.position),
                "default": self._default_pick,
                "active": {"price": self._target_price, "level": self._target_level},
                "rows": self._menu_rows or [],
            }
            os.makedirs(self.state_dir, exist_ok=True)
            with open(os.path.join(self.state_dir, self.MENU_FILE), "w",
                      encoding="utf-8") as fh:
                _json.dump(payload, fh, default=str, indent=1)
        except Exception:
            pass

    def refresh_menu(self, now) -> list:
        """Re-price the menu at THIS bar and rewrite the file. The rows a fill chose from
        age: distances change, a level can be swept or fall inside the draw floor. The
        operator is choosing now, so they see now."""
        self._menu_rows = self._build_menu_rows(now)
        self._write_menu(now)
        return self._menu_rows

    def _build_menu_rows(self, now) -> list:
        """The DOL menu (D rows, what the fill chose from) plus the running day / week /
        6h-block levels as display-only R rows, bindable by id through `agent-target`.
        The R rows never reach `select_target`."""
        direction = self._plan.get("direction")
        rows = target_menu(self._bars, now, direction, self._ticker, **self._htf_kw())
        return rows + running_rows(self._bars, now, direction, self._ticker,
                                   exclude_prices=[r.get("price") for r in rows])

    def target_state(self) -> dict:
        return {"active_price": self._target_price, "active_level": self._target_level,
                "default": self._default_pick, "rows": self._menu_rows or [],
                "has_position": bool(self._sim.position)}

    def set_target_override(self, now, price: float, level=None) -> dict:
        """Replace the bound target and re-derive the initial stage from it.

        Refused — with the reason, never silently — when there is no position, when the
        fill selected no default (there is nothing to override or reset to), or when the
        price sits on the wrong side of the entry: a target behind the entry would be hit
        immediately, which is an exit dressed as a target.
        """
        pos = self._sim.position
        if pos is None:
            return {"accepted": False, "reason": "no_position"}
        if self._default_pick is None:
            return {"accepted": False, "reason": "no_default_target"}
        try:
            price = float(price)
        except (TypeError, ValueError):
            return {"accepted": False, "reason": "bad_price"}
        entry = pos.get("entry")
        if entry is not None:
            ahead = (price > float(entry)) if not self._is_short() else (price < float(entry))
            if not ahead:
                return {"accepted": False, "reason": "wrong_side_of_entry",
                        "detail": {"entry": entry, "price": price}}
        pick = {"level": level or "operator", "price": price, "operator": True}
        self._sim.set_target(price)
        self._target_price = price
        self._target_level = pick["level"]
        self._target_since = now
        self._rec.target_selected(now=now, plan_id=self._plan.get("plan_id"),
                                  mechanism=self._state.get("mechanism"), pick=pick)
        self._arm_initial_target(now, pick)
        self._write_menu(now)
        return {"accepted": True, "detail": {"price": price, "level": pick["level"]}}

    def reset_target(self, now) -> dict:
        """Back to the pick this fill made. Same guards as an override."""
        if self._sim.position is None:
            return {"accepted": False, "reason": "no_position"}
        if self._default_pick is None:
            return {"accepted": False, "reason": "no_default_target"}
        price = self._default_pick.get("price")
        if price is None:
            return {"accepted": False, "reason": "no_default_target"}
        self._sim.set_target(float(price))
        self._target_price = float(price)
        self._target_level = self._default_pick.get("level")
        self._target_since = now
        self._rec.target_selected(now=now, plan_id=self._plan.get("plan_id"),
                                  mechanism=self._state.get("mechanism"),
                                  pick=dict(self._default_pick))
        self._arm_initial_target(now, dict(self._default_pick))
        self._write_menu(now)
        return {"accepted": True, "detail": {"price": float(price),
                                             "level": self._target_level}}

    def has_position(self) -> bool:
        return self._sim.position is not None

    def attempts_used(self) -> int:
        return int(self._plan.get("attempts_used") or 0)

    def _htf_kw(self) -> dict:
        """`htf=` for the target selectors, passed only when there is one so a stub with
        the pre-plan-40 signature keeps working."""
        return {"htf": self._htf} if self._htf is not None else {}

    # -- plan 35: the initial-target stage ---------------------------------------- #

    def _arm_initial_target(self, now, pick) -> None:
        """Select the initial target for the position just filled and start tracking it.

        Anchor = the fill; secondary = THIS fill's T2 pick (not a target remembered from
        an earlier attempt — a stage anchored to a stale objective is wrong by
        construction). Candidates = the named-level universe the T2 menu was built from
        (`target.level_universe`). Recorded on every fill, `price=None` when there is no
        stage. Total: a failure here costs the stage and nothing else.
        """
        if self._it is not None and self._it.get("exit_minute") is not None:
            self._it_pending = self._it          # its exit bar is still to be judged
        self._it = None
        self._state["initial_target"] = None
        self._state["initial_target_error"] = None
        try:
            pos = self._sim.position
            secondary = (pick or {}).get("price") if isinstance(pick, dict) else None
            sel = None
            if pos is not None and secondary is not None:
                # The universe carries the DOL menu's eligibility marks (swept /
                # depleted / suppressed-nested) per level; the v2 selector applies them
                # itself, the same way `derive_facts._dol_menu` does for T2.
                levels = level_universe(self._bars, now, self._ticker,
                                        **self._htf_kw())
                sel = select_initial_target(
                    self._plan.get("direction"), pos.get("entry"), secondary, levels,
                    attempts_used=self._plan.get("attempts_used"))
            self._rec.initial_target_selected(
                now=now, plan_id=self._plan.get("plan_id"),
                mechanism=self._state.get("mechanism"),
                price=(sel or {}).get("price"), level=(sel or {}).get("level"),
                secondary=(None if secondary is None else float(secondary)),
                anchor=(None if pos is None else pos.get("entry")),
                level_price=(sel or {}).get("level_price"),
                tier=(sel or {}).get("tier"),
                band=(sel or {}).get("band"),
                n_candidates=(sel or {}).get("n_candidates"),
                variant=(sel or {}).get("variant") or variant_label(),
                attempts_used=int(self._plan.get("attempts_used") or 0),
                action=INITIAL_TARGET_ACTION)
            if sel is None:
                return
            tracker = InitialTargetTracker(self._plan.get("direction"), sel["price"],
                                           level=sel.get("level"))
            self._it = {"tracker": tracker, "fill_minute": minute_of(now),
                        "opened_at": pos.get("opened_at"), "exit_minute": None,
                        "exit_kind": None, "stop_moved": False,
                        # Captured HERE: plan death clears `_state["mechanism"]`, and a
                        # flip judged on the exit bar would otherwise record None.
                        "mechanism": self._state.get("mechanism")}
            self._state["initial_target"] = tracker.state()
        except Exception as exc:
            self._it = None
            self._state["initial_target_error"] = f"{type(exc).__name__}: {exc}"

    def _note_exit(self, ev: dict) -> None:
        """Remember the minute and kind of the position's exit, so the completed bar
        that CONTAINS the exit is still judged once (the 10:01 bar on 09-18 both closed
        beyond the initial and swept the target) and every bar after it is not."""
        it = self._it
        if it is None or it.get("exit_minute") is not None:
            return
        it["exit_minute"] = minute_of(ev.get("time"))
        it["exit_kind"] = ev.get("kind")

    def _drive_initial_target(self, now, mnq, bar_complete) -> None:
        """Plan 35 §2.4 on the completed 1m bar; §2.5's action when it flips.

        Which bars are judged: every completed bar from the fill's minute through the
        bar containing the exit, inclusive. A bar before the exit minute was traded
        with the position open for its whole length even if the position is gone by
        the time the bar can be read (the exit came on a later tick). The exit bar
        itself is judged unless the exit was a STOP-OUT — the stop wins the bar (§2.4).
        Actions only ever touch an OPEN position; on a bar judged after the fact the
        flip is recorded and nothing else happens. A stage parked by a same-minute
        re-fill (`_it_pending`) is judged first, closed-position rules only.
        """
        if not bar_complete or (self._it is None and self._it_pending is None):
            return
        try:
            bar = self._completed_1m(mnq, now)
            if bar is None:
                return
            if self._it_pending is not None:
                if self._judge_initial_bar(now, bar, self._it_pending, open_=False):
                    self._it_pending = None
            if self._it is not None:
                pos = self._sim.position
                open_ = (pos is not None
                         and pos.get("opened_at") == self._it.get("opened_at"))
                if self._judge_initial_bar(now, bar, self._it, open_=open_, pos=pos):
                    self._it = None
        except Exception as exc:
            self._state["initial_target_error"] = f"{type(exc).__name__}: {exc}"

    def _judge_initial_bar(self, now, bar, it: dict, *, open_: bool, pos=None) -> bool:
        """One completed bar for one stage. Returns True when the stage is finished."""
        label = bar.name
        fill_minute = it.get("fill_minute")
        if fill_minute is not None and label < fill_minute:
            return False
        exit_minute = it.get("exit_minute")
        exit_bar = False
        if not open_:
            if exit_minute is None or label > exit_minute:
                return True                          # nothing left to judge
            exit_bar = label == exit_minute
            if exit_bar and it.get("exit_kind") == "stop_out":
                return True                          # the stop wins the bar
        tracker = it["tracker"]
        if not tracker.reached:
            ev = tracker.on_bar_close(bar, stop=(pos.get("stop") if open_ else None))
            if ev is not None:
                self._rec.initial_target_reached(
                    now=now, plan_id=self._plan.get("plan_id"),
                    mechanism=it.get("mechanism"), bar=label,
                    price=ev["price"], level=ev.get("level"), close=ev.get("close"),
                    position_open=open_, action=INITIAL_TARGET_ACTION)
                if open_:
                    self._apply_initial_action(now, it, pos)
        else:
            for cf in tracker.post_flip(bar):
                self._rec.order_event(
                    now=now, plan_id=self._plan.get("plan_id"),
                    mechanism=it.get("mechanism"),
                    kind=f"initial_target_{cf['kind']}", bar=label,
                    price=cf["price"], position_open=open_)
                if (open_ and cf["kind"] == "cf_opp_close"
                        and INITIAL_TARGET_ACTION == "opp_close"):
                    self._initial_opp_close(now, it)
                    open_ = False
        if it is self._it:
            self._state["initial_target"] = tracker.state()
        return exit_bar

    def _port_supports(self, op: str) -> bool:
        """True when the current order port implements `op`. A per-operation check, not
        a blanket "is this the bare simulation": `OrderSim` implements both `flatten` and
        `move_stop`, so a replay is unaffected either way, but `MirroringOrderPort` (the
        live port, plan 38) implements `flatten` — it forwards to a market close via
        `automation/agent_dispatch` — while deliberately NOT implementing `move_stop`.

        Plan 35 action A (`be_structure`) was built on the `live` branch against a mirror
        that turned a stop move into a legacy signal; plan 38's port speaks market entries
        and market closes ONLY, and there is no market order that moves a resting stop.
        Rather than move a SIMULATED stop while the broker keeps the original — a silent
        divergence nobody would see until the stop filled at the wrong price — action A
        refuses and says so; it stays refused on `MirroringOrderPort` until a stop-modify
        path exists. Action B (`opp_close`) and O4 (`micro_smt_exit`) both close outright,
        which the live port CAN do, so they are wired.
        """
        return hasattr(self._sim, op)

    def _apply_initial_action(self, now, it: dict, pos: dict) -> None:
        """§2.5 at the flip, position open. "record" does nothing. "be_structure" moves
        the stop to the initial price exactly once, and only if that tightens it.
        "opp_close" arms nothing here — its exit is the FIRST opposite close AFTER the
        flip bar, judged by `post_flip` on later bars."""
        if INITIAL_TARGET_ACTION != "be_structure" or it.get("stop_moved"):
            return
        if not self._port_supports("move_stop"):
            self._state["initial_action_unwired"] = INITIAL_TARGET_ACTION
            return
        tracker = it["tracker"]
        new_stop = float(tracker.initial)
        cur = float(pos.get("stop"))
        tighter = (new_stop < cur) if self._is_short() else (new_stop > cur)
        if not tighter:
            return
        it["stop_moved"] = True
        mover = getattr(self._sim, "move_stop", None)
        if mover is None:
            return
        ev = mover(now, new_stop, level_name=tracker.level)
        if ev is not None:
            self._rec.order_event(
                now=now, plan_id=self._plan.get("plan_id"),
                mechanism=self._state.get("mechanism"),
                artifact_label=self._label_for(ev.get("artifact_id")),
                reason="initial_target", level=tracker.level, **ev)

    def _initial_opp_close(self, now, it: dict) -> None:
        """§2.5 B: market-close at the current price, recorded like every other exit."""
        if not self._port_supports("flatten"):
            self._state["initial_action_unwired"] = INITIAL_TARGET_ACTION
            return
        price = self._market_price() if self._state.get("now_price") is not None else None
        if price is None:
            return
        ev = self._sim.flatten(now, float(price), kind="initial_opp_close")
        if ev is None:
            return
        self._rec.order_event(
            now=now, plan_id=self._plan.get("plan_id"),
            mechanism=self._state.get("mechanism"),
            artifact_label=self._label_for(ev.get("artifact_id")), **ev)
        self._note_exit(ev)

    def _drive_micro_smt_exit(self, now: pd.Timestamp, mnq: pd.DataFrame,
                              bar_complete: bool) -> None:
        """O4 (`micro_smt_exit`, flag-gated, default ON — adopted 2026-09-26, wired live
        2026-09-26): a counter-thesis micro-SMT confirmed on BOTH assets market-closes an
        OPEN position, whatever mechanism opened it, whether or not T2 has been reached.

        1m-bar-close only, like every other market mechanism's bar-close path. `T2 not
        reached` is implicit rather than checked: `_drive_orders` above already closed
        the position on a same-bar stop or take-profit touch, so `self._sim.position`
        is already None by the time this runs and there is nothing left to override.

        Operator decisions (2026-09-24, `l2-mechanisms.md` §7b):
          - A PROFITABLE O4 exit latches `NO_ENTRY_AFTER_POSITIVE` exactly like a T2
            touch (`_note_close`, unconditional — it is itself a no-op on a loser).
          - A LOSING O4 exit spends the shared attempt budget exactly as `_on_stop_out`
            does — same two lines, no more: there is no gap artifact behind a
            `micro_smt_exit`, so `_on_stop_out`'s takeover scan does not apply and is
            deliberately not called here.

        The detector runs BEFORE the live-wiring check (below), not after: only a real
        fire is worth recording as "O4 would have exited here" — the live port being
        unwired is true on every bar of every position, and recording that on its own
        would say nothing.
        """
        if not micro_smt.MICRO_SMT_EXIT_ENABLED:
            return
        if not bar_complete or self._sim.position is None:
            return
        bar = self._completed_1m(mnq, now)
        if bar is None:
            return
        mes = truncate(normalize((self._bars or {}).get("MES")), now)
        mes_bar = self._completed_1m(mes, now) if len(mes) else None
        if mes_bar is None:
            return
        fire = self._market.micro_smt_exit_on_bar_close(now, bar, mes_bar, mnq, mes)
        if fire is None:
            return
        if not self._port_supports("flatten"):
            # `MirroringOrderPort` now HAS `flatten` (wired 2026-09-26), so this no
            # longer fires against it — it is dead code for today's live port, kept for
            # any FUTURE port that does not implement `flatten`, so the gap is still
            # visible rather than crashing into a swallowed `market_mech_error` (or,
            # worse, silently doing nothing every bar). Recorded ONCE per position
            # (`_set_target_on_fill` resets the latch at every fill) so a live session
            # shows exactly when O4 WOULD have exited, without repeating the same
            # observation every later bar.
            self._state["micro_smt_exit_unwired"] = True
            if not self._micro_smt_exit_unwired_recorded:
                self._micro_smt_exit_unwired_recorded = True
                self._rec.veto(now=now, plan_id=self._plan.get("plan_id"),
                               mechanism=self._state.get("mechanism"),
                               reason="micro_smt_exit_unwired",
                               detail={"price": fire.get("price")})
            return
        ev = self._sim.flatten(now, float(fire["price"]), kind="micro_smt_exit")
        if ev is None:
            return
        self._rec.order_event(
            now=now, plan_id=self._plan.get("plan_id"),
            mechanism=self._state.get("mechanism"),
            artifact_label=self._label_for(ev.get("artifact_id")), **ev)
        self._note_exit(ev)
        self._note_close(ev)
        if not self._is_profitable(ev):
            self._plan["attempts_used"] = int(self._plan.get("attempts_used") or 0) + 1
            self._plan.setdefault("max_attempts", MAX_ATTEMPTS)
            try:
                self._market.arbiter.spend(self._state.get("mechanism"))
            except Exception:
                pass

    def _clear_binding(self) -> None:
        self._state.update({"bound_id": None, "bound_label": None, "mechanism": None,
                            "trigger": None, "stop": None})
        # A resting order with nothing eligible behind it is withdrawn. An open
        # position is not — it is managed to its stop or the DOL by `_drive_orders`.
        if self._sim.position is None:
            self._sim.cancel()
