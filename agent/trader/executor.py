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

import pandas as pd

from agent.facts.bars import resample
from agent.facts.detectors._common import normalize, truncate
from agent.facts.maintainer import FactsMaintainer
from agent.facts.records import FactClass, FactState
from agent.facts.requirements import EXECUTOR_REQUIREMENT
from agent.contracts.predicates import (MarketView, eval_predicate,
                                        validate_predicate_list)
from agent.trader.order_sim import OrderSim, RestingOrder
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
MIN_FVG_HEIGHT_PTS = 5.0             # §9; INITIAL binding only — ladder targets exempt
MAX_FVG_HEIGHT_PTS = 45.0            # §9; raised from 35 on 2026-08-22
DISTANCE_INVALIDATION_PTS = 60.0     # §9; PERMANENT, unlike the max-distance guard
NO_MOVE_ZONE_PTS = 15.0              # §9; see §8 — never re-bind this close to a trigger
MAX_ATTEMPTS = 3                     # §8; per plan_id, shared across mechanisms
# How much history the predicate view's higher timeframes are resampled from. Bounded:
# `n_closes_beyond` reads at most `n` closes, and 24h holds 6 completed 4h bars.
HTF_VIEW_TAIL = pd.Timedelta(hours=24)
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
        self._sim = OrderSim(dol=self._dol_price())
        self._emitted: set = set()
        self._vetoed: set = set()
        self._last_minute = None
        # The stop-out whose §2 cooldown-end resolution has already run, so the
        # resolution fires exactly once per stop-out.
        self._cooldown_resolved_for = None
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
        self._drive_orders(now, mnq)

        # 3. Plan death — evaluated every bar, with nothing open. Over bars SINCE THE
        # ARM only: the frame reaches back to the 18:00 session open, and overnight /
        # pre-RTH traversal of a level the plan later names as its DOL is routine.
        # Judging a plan by price action that predates it kills almost every plan on
        # its first bar.
        if self._state["plan_alive"]:
            reason, detail = self._death(self._since_arm(mnq), now, bar_complete)
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
                # An unfilled order belongs to the dead plan and is withdrawn. An OPEN
                # position is not withdrawn — it keeps being managed by `_drive_orders`
                # above until it stops out or reaches the DOL.
                self._sim.cancel()
        if not self._state["plan_alive"]:
            return                    # no more BINDING; facts above keep accumulating

        if not bar_complete:
            return                                    # per-second path ends here

        # 4. Re-bind, then guards. Coverage and the ATR proxy were refreshed by the
        # maintainer above.
        try:
            self._rebind_and_guard(now)
        except Exception:
            pass

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
                if ev.get("kind") == "stop_out":
                    self._on_stop_out(ev)
        except Exception as exc:
            # Swallowed so an order-book bug cannot take the bar loop down — but NOT
            # silently. A raise here on every bar makes the whole lifecycle inert:
            # nothing fills, nothing stops out, the attempt budget stays at zero, and
            # the artifact reads exactly like "no entry was ever triggered". The
            # diagnostic goes into state (project convention: production is silent).
            self._state["order_error"] = f"{type(exc).__name__}: {exc}"

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

        if bar_complete:
            fired = self._falsified(mnq, now)
            if fired is not None:
                return "falsified", fired
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

    def _eligible_gaps(self, now: pd.Timestamp, *, initial: bool = True) -> list:
        """Live MNQ gaps on the primary timeframe facing the trade direction.

        A SHORT plan trades from bear gaps (supply above), a LONG plan from bull gaps.
        Blacklisted artifacts are excluded — a gap that already produced a stop-out is
        not re-bindable for this plan.

        `initial=False` is the LADDER-TARGET role, which is exempt from §2's minimum
        height (but not its maximum).
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
            if not self._height_ok(f, initial=initial):
                continue
            if self._distance_dead(f):
                continue
            out.append(f)
        return out

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

    def _ladder_target(self, now: pd.Timestamp, price, *, bound_id=None):
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
        for f in self._eligible_gaps(now, initial=False):
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

        failed_id = event.get("artifact_id")
        if not failed_id:
            return
        deeper = self._ladder_target(pd.Timestamp(event.get("time")),
                                     self._state.get("now_price"),
                                     bound_id=failed_id)
        if deeper is not None and deeper.id != failed_id:
            bl = list(self._plan.get("blacklist") or ())
            if failed_id not in bl:
                bl.append(failed_id)
            self._plan["blacklist"] = bl

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

    def _market_price(self):
        """§11's market-fill price: the 1s mid of the bar at placement, falling back to
        the close when no mid has been seen (direct-construction callers)."""
        mid = self._state.get("now_mid")
        return mid if mid is not None else self._state.get("now_price")

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
        """§2's cooldown-end resolution, acting on CURRENT state.

        NO fresh-precondition requirement — that is the difference from the settle
        window. Mid-session, post-stop state reflects a real displacement that just took
        the stop; demanding it repeat forfeits the move (07-24: the breakdown crossed the
        trigger 19 s after the stop, and a lockout watched the -280 collapse flat).

        Order: crossed trigger -> market (same FVG-derived stop); else resting placement.
        """
        trigger, stop = self._state.get("trigger"), self._state.get("stop")
        if trigger is None or stop is None or self._crossing_price() is None:
            return
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

        gap = (self._ladder_target(now, price)
               if self._sim.resting is not None else None)
        if gap is None:
            gaps = self._eligible_gaps(now)
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
            def _created(f):
                return f.extra.get("exists_from") or f.reference_ts

            newest = max(gaps, key=lambda g: (_created(g), g.id))
            if still_eligible and _created(newest) <= _created(bound):
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
            self._enter(now, trigger, stop, gap.id)

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
        # A resting order with nothing eligible behind it is withdrawn. An open
        # position is not — it is managed to its stop or the DOL by `_drive_orders`.
        if self._sim.position is None:
            self._sim.cancel()
