"""TradeDirector — the executor-owned state machine (spec §5).

The deterministic Level-3 core: owns the state machine, per-bar predicate evaluation, the
attempt counter (3 entry attempts per setup, failed entries only), the session risk gate,
and every AI-call scheduling decision. It drives entries/management through an injected
*mechanism adapter* (Phase-4 wires the real code paths behind it) and requests AI decisions
through an injected *decision provider* (Phase-3 wires the async worker behind it). Both are
injected so this core is testable with no LLM and no pipeline.

Failure policy (spec §4): standing decisions that are still valid keep operating; with no
valid thesis/plan there are no new entries (flat); an open position is always managed by
its last valid plan; a thesis death while in a position runs `on_dol_falsified`
deterministically (no AI call). Never falls back to the hypothesis-engine rules.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
for _p in (_HERE, _AGENT, os.path.join(_AGENT, "contracts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schemas import Thesis, TradePlan          # noqa: E402
from predicates import MarketView, eval_any    # noqa: E402
from risk_gate import RiskGate, RiskGateConfig  # noqa: E402

MAX_ENTRY_ATTEMPTS = 3
# Effective-confidence tiers that count as "confident" (advance L1 -> L2). LOW parks in
# THESIS_LOW_CONF and recalls L1. (spec §2.2 / §8; threshold is config-overridable.)
_CONFIDENT_TIERS = {"HIGH", "MEDIUM"}


class State(str, Enum):
    NO_THESIS = "NO_THESIS"
    THESIS_LOW_CONF = "THESIS_LOW_CONF"
    AWAITING_SETUP = "AWAITING_SETUP"
    SETUP_ARMED = "SETUP_ARMED"
    IN_POSITION = "IN_POSITION"
    HALTED = "HALTED"


def _self_report_confidence(thesis, facts=None) -> str:
    """Default confidence gate for Phase 2 (self-report). Phase 5 replaces this with the
    code-derived calibration gate via the same `confidence(decision, facts) -> tier`
    signature."""
    d = thesis.to_dict() if isinstance(thesis, Thesis) else (thesis or {})
    return (d.get("confidence") or "LOW").upper()


@dataclass
class DirectorLog:
    """Structured, append-only trace of every scheduling decision + mechanism command —
    the audit substrate (Phase 3 persists it; Phase 2 asserts on it)."""

    ai_calls: list = field(default_factory=list)       # ("L1"|"L2", trigger, ts)
    mechanism_cmds: list = field(default_factory=list)  # (cmd, detail, ts)
    transitions: list = field(default_factory=list)     # (from, event, to, ts)

    def call(self, level, trigger, ts):
        self.ai_calls.append((level, trigger, ts))

    def cmd(self, name, detail, ts):
        self.mechanism_cmds.append((name, detail, ts))

    def transition(self, frm, event, to, ts):
        self.transitions.append((frm, event, to, ts))


class TradeDirector:
    def __init__(self, provider, mechanism, *, risk_gate: Optional[RiskGate] = None,
                 confidence_fn=_self_report_confidence,
                 risk_config: Optional[RiskGateConfig] = None):
        self.provider = provider          # request_thesis(facts_ref) / request_plan(...)
        self.mechanism = mechanism        # arm / disarm / raise_breakeven / execute_dol_falsified
        self.risk = risk_gate or RiskGate(risk_config)
        self._confidence_fn = confidence_fn

        self.state = State.NO_THESIS
        self.thesis: Optional[Thesis] = None
        self.thesis_tier: Optional[str] = None
        self.thesis_issued_at = None
        self.plan: Optional[TradePlan] = None
        self.plan_issued_at = None
        self.attempts: int = 0            # failed (stopped-out) entries for the current plan
        self.position_open: bool = False  # tracked independently of state (HALTED-with-position)
        self.log = DirectorLog()
        self._awaiting_thesis = False
        self._awaiting_plan = False

    # ------------------------------------------------------------------ #
    # AI-call scheduling (all provider calls funnel through here)          #
    # ------------------------------------------------------------------ #
    def _call_l1(self, facts_ref, trigger, ts) -> None:
        self._awaiting_thesis = True
        self.log.call("L1", trigger, ts)
        try:
            self.provider.request_thesis(facts_ref)
        except Exception:                 # provider failure → stay flat, keep standing state
            self._awaiting_thesis = False

    def _call_l2(self, facts_ref, trigger, ts) -> None:
        if self.thesis is None:
            return
        self._awaiting_plan = True
        self.log.call("L2", trigger, ts)
        try:
            self.provider.request_plan(facts_ref, self.thesis)
        except Exception:
            self._awaiting_plan = False

    def _goto(self, to: State, event: str, ts=None) -> None:
        self.log.transition(self.state.value, event, to.value, ts)
        self.state = to

    # ------------------------------------------------------------------ #
    # External events                                                      #
    # ------------------------------------------------------------------ #
    def on_session_open(self, ts, facts_ref) -> None:
        """The mandatory 18:00 ET session-open Level-1 call (spec §2 principle 2). Fires a
        fresh thesis regardless of any standing decision — the open is a regime boundary."""
        if self.state == State.HALTED:
            return
        self._drop_thesis()
        self._goto(State.NO_THESIS, "session_open", ts)
        self._call_l1(facts_ref, "session_open", ts)

    def on_thesis_arrived(self, thesis, ts=None, facts=None) -> None:
        """Level-1 result delivery. Confidence (code-derived tier) gates L1->L2."""
        self._awaiting_thesis = False
        if self.state == State.HALTED:
            return
        self.thesis = thesis if isinstance(thesis, Thesis) else Thesis.from_dict(thesis)
        self.thesis_issued_at = ts
        self.thesis_tier = (self._confidence_fn(self.thesis, facts) or "LOW").upper()
        # A fresh thesis invalidates any dependent plan / arming.
        self._drop_plan()
        if self.thesis_tier in _CONFIDENT_TIERS:
            self._enter_awaiting_setup("thesis_confident", ts, facts_ref=facts)
        else:
            self._goto(State.THESIS_LOW_CONF, "thesis_low_conf", ts)

    def on_thesis_failed(self, ts=None) -> None:
        """L1 call failed/timed out (spec §4): retain standing decisions, no new thesis."""
        self._awaiting_thesis = False

    def _enter_awaiting_setup(self, event, ts, facts_ref=None) -> None:
        self._goto(State.AWAITING_SETUP, event, ts)
        # On entering AWAITING_SETUP, call L2 (spec §5).
        self._call_l2(facts_ref, "awaiting_setup", ts)

    def on_plan_arrived(self, plan, ts=None) -> None:
        """Level-2 result delivery: SETUP arms; WAIT parks in AWAITING_SETUP for recall."""
        self._awaiting_plan = False
        if self.state not in (State.AWAITING_SETUP, State.SETUP_ARMED):
            return
        p = plan if isinstance(plan, TradePlan) else TradePlan.from_dict(plan)
        if p.is_setup():
            if self.state == State.SETUP_ARMED:
                try:                          # disarm the superseded plan's resting mechanism
                    self.mechanism.disarm()
                except Exception:
                    pass
            self.plan = p
            self.plan_issued_at = ts
            self.attempts = 0
            self._arm(ts)
        else:                              # WAIT — keep parked, schedule recall on events/TTL
            self.plan = p                  # standing WAIT (carries recall)
            self.plan_issued_at = ts
            self._goto(State.AWAITING_SETUP, "plan_wait", ts)

    def on_plan_failed(self, ts=None) -> None:
        self._awaiting_plan = False

    def _arm(self, ts) -> None:
        if self.risk.breached:             # no new entries once halted (spec §9)
            self._maybe_halt(ts)
            return
        self._goto(State.SETUP_ARMED, "setup", ts)
        self.log.cmd("arm", self.plan.plan_id, ts)
        try:
            self.mechanism.arm(self.plan)
        except Exception:
            pass

    def on_fill(self, ts=None) -> None:
        """A SETUP mechanism triggered and the order filled → IN_POSITION."""
        if self.state != State.SETUP_ARMED:
            return
        self.position_open = True
        self._goto(State.IN_POSITION, "filled", ts)

    def on_stop_out(self, ts=None, facts_ref=None, setup_still_valid: bool = True) -> None:
        """Stopped out of a position (spec §5): re-arm the same plan up to attempt 3; the
        3rd stop-out counts as a setup falsification → recall L2 for a different setup."""
        if self.state != State.IN_POSITION:
            return
        self.position_open = False
        self.risk.record_exit(-1.0)        # a stop-out is a losing close (magnitude fed by P4)
        if self._maybe_halt(ts):
            return
        self.attempts += 1
        if self.attempts < MAX_ENTRY_ATTEMPTS and setup_still_valid:
            self._arm(ts)                  # re-arm same plan (attempts carried)
        else:
            self._goto(State.AWAITING_SETUP, "attempts_exhausted", ts)
            self._call_l2(facts_ref, "setup_falsified_attempts", ts)

    def on_profitable_exit(self, ts=None, facts_ref=None, market_view=None,
                           pnl: float = 1.0) -> None:
        """Profitable exit (spec §5): thesis still valid → recall L2; thesis exhausted → L1."""
        if self.state != State.IN_POSITION:
            return
        self.position_open = False
        self.risk.record_exit(pnl)
        if self._maybe_halt(ts):
            return
        if self._thesis_exhausted(market_view):
            self._drop_thesis()
            self._goto(State.NO_THESIS, "profit_thesis_exhausted", ts)
            self._call_l1(facts_ref, "profit_thesis_exhausted", ts)
        else:
            self._goto(State.AWAITING_SETUP, "profit_thesis_valid", ts)
            self._call_l2(facts_ref, "profit_thesis_valid", ts)

    def on_risk_breach_check(self, ts=None) -> bool:
        """Force a gate re-evaluation from an external signal (e.g. the live loss-limit
        monitor). Returns True iff this transitioned to HALTED."""
        return self._maybe_halt(ts)

    def on_external_halt(self, reason: str, ts=None) -> bool:
        """Trip the risk gate from an external monitor (unrealized-drawdown loss-limit,
        manual halt) mid-position. The open position is preserved and still managed; no new
        entries follow. Returns True iff this transitioned to HALTED."""
        self.risk.trip(reason)
        return self._maybe_halt(ts)

    # ------------------------------------------------------------------ #
    # Per-bar predicate evaluation                                        #
    # ------------------------------------------------------------------ #
    _CONFIDENT_STATES = (State.AWAITING_SETUP, State.SETUP_ARMED, State.IN_POSITION)

    def on_bar(self, ts, market_view: MarketView, facts_ref=None) -> None:
        """Bar-close tick: evaluate the standing decisions' predicates and drive the
        state machine. HALTED still manages an open position but arms nothing new.

        A thesis falsified_if / exhausted_if fires in ANY state → thesis death. A recall
        TTL (max_age) is a HARD thesis expiry only while a CONFIDENT thesis stands
        (AWAITING_SETUP / SETUP_ARMED / IN_POSITION); in THESIS_LOW_CONF the same max_age
        is a re-ask (recall L1), not a death (spec §5)."""
        if self.thesis is not None and self._thesis_dead(market_view):
            self._on_thesis_death(ts, market_view, facts_ref)
            return
        if (self.state in self._CONFIDENT_STATES and self.thesis is not None
                and self._thesis_ttl_expired(market_view)):
            self._on_thesis_death(ts, market_view, facts_ref)
            return

        if self.state == State.THESIS_LOW_CONF:
            self._maybe_recall_l1(ts, market_view, facts_ref)
        elif self.state == State.AWAITING_SETUP:
            self._maybe_recall_l2(ts, market_view, facts_ref)
        elif self.state == State.SETUP_ARMED:
            self._bar_setup_armed(ts, market_view, facts_ref)
        elif self.state == State.IN_POSITION:
            self._bar_in_position(ts, market_view)
        elif self.state == State.HALTED and self.position_open:
            self._manage_position(ts, market_view)   # open position still managed

    def _thesis_dead(self, mv) -> bool:
        t = self.thesis
        return bool(t) and (eval_any(t.falsified_if, mv) or eval_any(t.exhausted_if, mv))

    def _thesis_ttl_expired(self, mv) -> bool:
        t = self.thesis
        return bool(t) and self._ttl_expired(t.recall, self.thesis_issued_at, mv)

    def _thesis_exhausted(self, mv) -> bool:
        return bool(self.thesis) and mv is not None and eval_any(self.thesis.exhausted_if, mv)

    def _on_thesis_death(self, ts, mv, facts_ref) -> None:
        """Thesis falsified/exhausted/TTL. With an open position, run on_dol_falsified
        deterministically first (no AI call), then drop + recall L1 (unless halted)."""
        prev_halted = self.state == State.HALTED
        if self.position_open:
            self._execute_on_dol_falsified(ts)
        self._drop_thesis()
        if prev_halted:
            # A halted session stays halted — no new thesis, no new entries (spec §9). The
            # open position (if any) was just closed by on_dol_falsified above.
            self._goto(State.HALTED, "thesis_dead_halted", ts)
            return
        self._goto(State.NO_THESIS, "thesis_dead", ts)
        self._call_l1(facts_ref, "thesis_dead", ts)

    def _execute_on_dol_falsified(self, ts) -> None:
        action = "MARKET_CLOSE"
        params = {}
        if self.plan is not None and isinstance(self.plan.on_dol_falsified, dict):
            action = self.plan.on_dol_falsified.get("action", "MARKET_CLOSE")
            params = self.plan.on_dol_falsified.get("params", {}) or {}
        self.log.cmd("on_dol_falsified", action, ts)
        self.position_open = False        # the deterministic close exits the position
        try:
            self.mechanism.execute_dol_falsified(action, params)
        except Exception:
            pass

    def _bar_setup_armed(self, ts, mv, facts_ref) -> None:
        p = self.plan
        if p is None:
            return
        # setup falsified / exhausted / TTL → recall L2.
        if (eval_any(p.setup_falsified_if, mv) or eval_any(p.setup_exhausted_if, mv)
                or self._ttl_expired(p.recall, self.plan_issued_at, mv)):
            self._drop_plan()
            self._goto(State.AWAITING_SETUP, "setup_invalidated", ts)
            self._call_l2(facts_ref, "setup_invalidated", ts)
            return
        # entry mechanisms whose valid_while has lapsed are disarmed (no entry).
        mechs = (p.entry or {}).get("mechanisms") or []
        if mechs and all(m.get("valid_while") and not eval_any(m.get("valid_while"), mv)
                         for m in mechs):
            self._drop_plan()
            self._goto(State.AWAITING_SETUP, "mechanisms_lapsed", ts)
            self._call_l2(facts_ref, "mechanisms_lapsed", ts)

    def _bar_in_position(self, ts, mv) -> None:
        # thesis death handled in on_bar; here only deterministic management (no AI calls).
        self._manage_position(ts, mv)

    def _manage_position(self, ts, mv) -> None:
        p = self.plan
        if p is None:
            return
        be = (p.breakeven or {}).get("raise_to_be_if")
        if be and eval_any(be, mv):
            self.log.cmd("raise_breakeven", None, ts)
            try:
                self.mechanism.raise_breakeven()
            except Exception:
                pass
        for m in (p.exit or {}).get("management") or []:
            if eval_any(m.get("when"), mv):
                self.log.cmd("management", m.get("kind"), ts)
                try:
                    self.mechanism.apply_management(m)
                except Exception:
                    pass

    def _maybe_recall_l1(self, ts, mv, facts_ref) -> None:
        t = self.thesis
        if t is None:
            return
        events = (t.recall or {}).get("events")
        if eval_any(events, mv) or self._ttl_expired(t.recall, self.thesis_issued_at, mv):
            self._call_l1(facts_ref, "recall", ts)

    def _maybe_recall_l2(self, ts, mv, facts_ref) -> None:
        p = self.plan
        if p is None:
            return
        events = (p.recall or {}).get("events")
        if eval_any(events, mv) or self._ttl_expired(p.recall, self.plan_issued_at, mv):
            self._call_l2(facts_ref, "recall", ts)

    def _ttl_expired(self, recall, issued_at, mv) -> bool:
        """max_age TTL: elapsed minutes since the decision issued >= max_age_min."""
        if not recall or issued_at is None or mv is None or mv.now is None:
            return False
        max_age = recall.get("max_age_min")
        if not isinstance(max_age, (int, float)) or max_age <= 0:
            return False
        elapsed = (mv.now - issued_at).total_seconds() / 60.0
        return elapsed >= max_age

    # ------------------------------------------------------------------ #
    # Risk gate                                                            #
    # ------------------------------------------------------------------ #
    def _maybe_halt(self, ts) -> bool:
        if self.risk.breached and self.state != State.HALTED:
            self._goto(State.HALTED, f"risk_breach:{self.risk.breach_reason}", ts)
            return True
        return False

    # ------------------------------------------------------------------ #
    # Housekeeping                                                         #
    # ------------------------------------------------------------------ #
    def _drop_thesis(self) -> None:
        self.thesis = None
        self.thesis_tier = None
        self.thesis_issued_at = None
        self._drop_plan()

    def _drop_plan(self) -> None:
        if self.plan is not None or self.state == State.SETUP_ARMED:
            try:
                self.mechanism.disarm()
            except Exception:
                pass
        self.plan = None
        self.plan_issued_at = None
        self.attempts = 0
