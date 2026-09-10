"""Phase-2 TradeDirector state-machine tests (plan §Phase 2 — one per transition edge).

All deterministic: a FakeProvider records AI-call requests (no LLM), a FakeMechanism
records executor commands (no pipeline). Arrivals are delivered by the test to decouple
timing."""

import pandas as pd

from predicates import MarketView
from risk_gate import RiskGate, RiskGateConfig
from trade_director import State, TradeDirector


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
class FakeProvider:
    def __init__(self, raise_on=None):
        self.thesis_reqs = []
        self.plan_reqs = []
        self.raise_on = raise_on or set()

    def request_thesis(self, facts_ref):
        self.thesis_reqs.append(facts_ref)
        if "thesis" in self.raise_on:
            raise RuntimeError("provider down")

    def request_plan(self, facts_ref, thesis):
        self.plan_reqs.append((facts_ref, thesis))
        if "plan" in self.raise_on:
            raise RuntimeError("provider down")


class FakeMechanism:
    def __init__(self):
        self.armed = []
        self.disarmed = 0
        self.be = 0
        self.mgmt = []
        self.dol = []

    def arm(self, plan):
        self.armed.append(plan.plan_id)

    def disarm(self):
        self.disarmed += 1

    def raise_breakeven(self):
        self.be += 1

    def apply_management(self, m):
        self.mgmt.append(m.get("kind"))

    def execute_dol_falsified(self, action, params):
        self.dol.append(action)


# --------------------------------------------------------------------------- #
# Builders                                                                     #
# --------------------------------------------------------------------------- #
T0 = pd.Timestamp("2026-05-19 18:00", tz="America/New_York")


_DOL = {"level": "pdh", "price": 20000.0}


def _thesis(confidence="HIGH", falsified=None, exhausted=None, recall=None, tid="th1",
            bias="UP", dol=_DOL):
    return {"thesis_id": tid, "bias": bias, "regime": "TREND", "confidence": confidence,
            "dol": dol,
            "falsified_if": falsified or [],
            "recall": recall if recall is not None else {"events": [], "max_age_min": 0},
            "reasoning": "x"}


def _setup(pid="pl1", falsified=None, be=None):
    return {"plan_id": pid, "thesis_id": "th1", "verdict": "SETUP",
            "entry": {"direction": "LONG",
                      "mechanisms": [{"kind": "confirmation_bar", "params": {}, "valid_while": []}]},
            "stop": {"price": 19600.0},
            "breakeven": {"raise_to_be_if": be or []},
            "exit": {"target": {"level": "pdh", "price": 19950.0}, "management": []},
            "setup_falsified_if": falsified or [],
            "on_dol_falsified": {"action": "MARKET_CLOSE", "params": {}},
            "recall": None, "reasoning": "x"}


def _wait(recall):
    return {"plan_id": "plw", "thesis_id": "th1", "verdict": "WAIT",
            "recall": recall, "reasoning": "x"}


def _mv(price=19800.0, now=None, closes=None, swept=None):
    return MarketView(price=price, closes_by_tf=closes or {}, swept=set(swept or []),
                      depleted=set(), now=now)


def _director(**kw):
    return TradeDirector(FakeProvider(**kw.pop("prov", {})), FakeMechanism(), **kw)


# --------------------------------------------------------------------------- #
# NO_THESIS / session open / the actionability gate                           #
# --------------------------------------------------------------------------- #
def test_session_open_calls_l1_once():
    d = _director()
    d.on_session_open(T0, "facts")
    assert len(d.provider.thesis_reqs) == 1
    assert d.state == State.NO_THESIS
    assert d.provider.plan_reqs == []


def test_a_low_confidence_thesis_is_NOT_parked_and_calls_l2():
    """CONFIDENCE NO LONGER GATES (2026-09-10). It used to: LOW parked in what was then
    called THESIS_LOW_CONF and never reached L2.

    Two things retired that rule. The gate read the model's SELF-REPORTED tier, which
    `agent/confidence.py` records as batch-proven INVERTED — self-reported `high` was 0%
    correct and `low` 41%. And across the seven real-thesis replays of 2026-09-09/10 the
    tiers did not separate at all: LOW and MEDIUM days alike were dead by 09:52, and the
    two LOW days supplied both the only no-fill and a full attempt-budget burn
    (`move-the-needle.md` §4).

    This is also what the L2 replay path already does — `agent/trader/analyzer.stands`
    has never gated on confidence, and said so in a docstring. Two stacks now agree."""
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis(confidence="LOW"), ts=T0)
    assert d.state == State.AWAITING_SETUP
    assert len(d.provider.plan_reqs) == 1


def test_the_tier_is_still_computed_and_recorded_it_just_decides_nothing():
    """Audit-only, which is the status `agent/confidence.py` already claims for it. The
    field has to survive for the question to be settleable later on a real corpus — the
    change is that it stops deciding, not that it stops existing."""
    for tier in ("HIGH", "MEDIUM", "LOW"):
        d = _director()
        d.on_session_open(T0, "facts")
        d.on_thesis_arrived(_thesis(confidence=tier), ts=T0)
        assert d.thesis_tier == tier
        assert d.state == State.AWAITING_SETUP, f"{tier} must not change the state"


def test_a_neutral_thesis_parks_regardless_of_a_confident_tier():
    """The gate is ACTIONABILITY now, so it has to hold at the other extreme too: a
    HIGH-confidence NEUTRAL call is still nothing to trade."""
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis(confidence="HIGH", bias="NEUTRAL"), ts=T0)
    assert d.state == State.THESIS_NOT_ACTIONABLE
    assert d.provider.plan_reqs == []


def test_a_directional_thesis_with_no_dol_parks():
    """Directional but with nothing to draw to. `analyzer.stands` requires both, and the
    no-liquidity override in `validate_contracts.score_thesis_evidence` already turns this
    case NEUTRAL upstream — this is the downstream guard for when it does not."""
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis(confidence="HIGH", dol=None), ts=T0)
    assert d.state == State.THESIS_NOT_ACTIONABLE
    assert d.provider.plan_reqs == []


def test_confident_thesis_calls_l2():
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis(confidence="HIGH"), ts=T0)
    assert d.state == State.AWAITING_SETUP
    assert len(d.provider.plan_reqs) == 1


def test_parked_thesis_recall_on_event_recalls_l1():
    ev = [{"type": "price_beyond", "price": 20500, "side": "above"}]
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis(bias="NEUTRAL", recall={"events": ev, "max_age_min": 0}), ts=T0)
    before = len(d.provider.thesis_reqs)
    d.on_bar(T0, _mv(price=20600))                 # event fires
    assert len(d.provider.thesis_reqs) == before + 1
    assert d.state == State.THESIS_NOT_ACTIONABLE        # not dropped — a re-ask, not a death


def test_parked_thesis_max_age_recalls_l1():
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis(bias="NEUTRAL", recall={"events": [], "max_age_min": 30}), ts=T0)
    before = len(d.provider.thesis_reqs)
    d.on_bar(T0, _mv(now=T0 + pd.Timedelta(minutes=31)))
    assert len(d.provider.thesis_reqs) == before + 1
    assert d.state == State.THESIS_NOT_ACTIONABLE


def test_parked_thesis_default_max_age_recalls_l1():
    """Plan 12 Fix 1: a valid low-conf thesis with a 0/absent model-authored max_age (and no
    firing recall event) re-calls L1 at the code-enforced default (60m) instead of leaving the
    executor blind all session (the THESIS_NOT_ACTIONABLE analog of the 07-02 th_04 20.5h wait)."""
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis(bias="NEUTRAL", recall={"events": [], "max_age_min": 0}), ts=T0)
    assert d.state == State.THESIS_NOT_ACTIONABLE
    before = len(d.provider.thesis_reqs)
    d.on_bar(T0, _mv(now=T0 + pd.Timedelta(minutes=59)))
    assert len(d.provider.thesis_reqs) == before        # default TTL not yet reached
    d.on_bar(T0, _mv(now=T0 + pd.Timedelta(minutes=61)))
    assert len(d.provider.thesis_reqs) == before + 1     # re-asked at the default
    assert d.state == State.THESIS_NOT_ACTIONABLE              # a re-ask, not a death


def test_parked_thesis_sooner_max_age_respected():
    """A declared max_age BELOW the default is respected (re-call sooner)."""
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis(bias="NEUTRAL", recall={"events": [], "max_age_min": 15}), ts=T0)
    before = len(d.provider.thesis_reqs)
    d.on_bar(T0, _mv(now=T0 + pd.Timedelta(minutes=14)))
    assert len(d.provider.thesis_reqs) == before
    d.on_bar(T0, _mv(now=T0 + pd.Timedelta(minutes=16)))
    assert len(d.provider.thesis_reqs) == before + 1


def test_failsafe_thesis_schedules_l1_recall_at_max_age():
    # Plan 11 Phase 4: the failsafe thesis now carries recall.max_age_min=60, so a
    # failsafed L1 call re-asks at +60 instead of leaving the executor blind all session.
    from schemas import failsafe_thesis
    fs = failsafe_thesis()
    assert fs["recall"]["max_age_min"] == 60
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(fs, ts=T0)                 # NEUTRAL → not actionable
    assert d.state == State.THESIS_NOT_ACTIONABLE
    before = len(d.provider.thesis_reqs)
    d.on_bar(T0, _mv(now=T0 + pd.Timedelta(minutes=59)))
    assert len(d.provider.thesis_reqs) == before   # not yet
    d.on_bar(T0, _mv(now=T0 + pd.Timedelta(minutes=61)))
    assert len(d.provider.thesis_reqs) == before + 1
    assert d.state == State.THESIS_NOT_ACTIONABLE


# --------------------------------------------------------------------------- #
# AWAITING_SETUP → SETUP / WAIT                                                #
# --------------------------------------------------------------------------- #
def test_setup_arms():
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis("HIGH"), ts=T0)
    d.on_plan_arrived(_setup(), ts=T0)
    assert d.state == State.SETUP_ARMED
    assert d.mechanism.armed == ["pl1"]


def test_wait_recalls_l2_on_event_and_ttl():
    ev = [{"type": "level_swept", "name": "pdl"}]
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis("HIGH"), ts=T0)
    d.on_plan_arrived(_wait({"events": ev, "max_age_min": 20}), ts=T0)
    assert d.state == State.AWAITING_SETUP
    n = len(d.provider.plan_reqs)
    d.on_bar(T0, _mv(swept=["pdl"]))               # recall event
    assert len(d.provider.plan_reqs) == n + 1
    d.on_bar(T0, _mv(now=T0 + pd.Timedelta(minutes=21)))  # TTL
    assert len(d.provider.plan_reqs) == n + 2


# --------------------------------------------------------------------------- #
# Thesis death in each pre-position state → plan dropped, L1 called           #
# --------------------------------------------------------------------------- #
def _armed_with_falsifiable_thesis():
    fals = [{"type": "price_beyond", "price": 19000, "side": "below"}]
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis("HIGH", falsified=fals), ts=T0)
    return d


def test_thesis_falsified_in_awaiting_setup():
    d = _armed_with_falsifiable_thesis()
    n1 = len(d.provider.thesis_reqs)
    d.on_bar(T0, _mv(price=18000))
    assert d.state == State.NO_THESIS and d.thesis is None
    assert len(d.provider.thesis_reqs) == n1 + 1


def test_thesis_falsified_while_parked():
    fals = [{"type": "price_beyond", "price": 19000, "side": "below"}]
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis(bias="NEUTRAL", falsified=fals), ts=T0)
    n1 = len(d.provider.thesis_reqs)
    d.on_bar(T0, _mv(price=18000))
    assert d.state == State.NO_THESIS
    assert len(d.provider.thesis_reqs) == n1 + 1


def test_thesis_falsified_in_setup_armed_drops_plan():
    d = _armed_with_falsifiable_thesis()
    d.on_plan_arrived(_setup(), ts=T0)
    assert d.state == State.SETUP_ARMED
    disarms = d.mechanism.disarmed
    d.on_bar(T0, _mv(price=18000))
    assert d.state == State.NO_THESIS and d.plan is None
    assert d.mechanism.disarmed == disarms + 1     # dependent plan dropped (disarmed)


def test_thesis_ttl_drops_when_confident():
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis("HIGH", recall={"events": [], "max_age_min": 15}), ts=T0)
    assert d.state == State.AWAITING_SETUP
    n1 = len(d.provider.thesis_reqs)
    d.on_bar(T0, _mv(now=T0 + pd.Timedelta(minutes=16)))
    assert d.state == State.NO_THESIS
    assert len(d.provider.thesis_reqs) == n1 + 1


# --------------------------------------------------------------------------- #
# IN_POSITION                                                                  #
# --------------------------------------------------------------------------- #
def _in_position(falsified=None, be=None):
    fals = falsified or []
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis("HIGH", falsified=fals), ts=T0)
    d.on_plan_arrived(_setup(be=be), ts=T0)
    d.on_fill(ts=T0)
    return d


def test_in_position_no_ai_calls_on_quiet_bar():
    d = _in_position()
    n_t, n_p = len(d.provider.thesis_reqs), len(d.provider.plan_reqs)
    d.on_bar(T0, _mv(price=19810))                 # nothing fires
    assert len(d.provider.thesis_reqs) == n_t and len(d.provider.plan_reqs) == n_p
    assert d.state == State.IN_POSITION


def test_in_position_thesis_falsified_runs_on_dol_then_l1():
    fals = [{"type": "price_beyond", "price": 19000, "side": "below"}]
    d = _in_position(falsified=fals)
    n_t = len(d.provider.thesis_reqs)
    d.on_bar(T0, _mv(price=18000))
    assert d.mechanism.dol == ["MARKET_CLOSE"]     # deterministic close FIRST
    assert d.state == State.NO_THESIS
    assert len(d.provider.thesis_reqs) == n_t + 1  # THEN L1
    assert not d.position_open


def test_in_position_breakeven_management():
    be = [{"type": "price_beyond", "price": 19900, "side": "above"}]
    d = _in_position(be=be)
    d.on_bar(T0, _mv(price=19950))
    assert d.mechanism.be == 1


# --------------------------------------------------------------------------- #
# Attempt counter                                                             #
# --------------------------------------------------------------------------- #
def test_stopout_rearm_then_third_recalls_l2():
    d = _in_position()
    n_plan = len(d.provider.plan_reqs)
    d.on_stop_out(ts=T0)                            # attempt 1
    assert d.state == State.SETUP_ARMED and d.attempts == 1
    d.on_fill(ts=T0)
    d.on_stop_out(ts=T0)                            # attempt 2
    assert d.state == State.SETUP_ARMED and d.attempts == 2
    d.on_fill(ts=T0)
    d.on_stop_out(ts=T0)                            # attempt 3 → setup falsified → L2
    assert d.state == State.AWAITING_SETUP
    assert len(d.provider.plan_reqs) == n_plan + 1
    assert d.mechanism.armed == ["pl1", "pl1", "pl1"]  # re-armed same plan twice


# --------------------------------------------------------------------------- #
# Profitable exit                                                             #
# --------------------------------------------------------------------------- #
def test_profitable_exit_valid_thesis_calls_l2():
    d = _in_position()
    n = len(d.provider.plan_reqs)
    d.on_profitable_exit(ts=T0, market_view=_mv(price=19810), pnl=50.0)
    assert d.state == State.AWAITING_SETUP
    assert len(d.provider.plan_reqs) == n + 1


def test_profitable_exit_recalls_l2_because_a_thesis_can_no_longer_be_exhausted():
    """Inverted 2026-08-29. This asserted that a fired `exhausted_if` routed a profitable
    exit to NO_THESIS + an L1 call. EXHAUSTION REMOVED: reaching the DOL *is* the
    exhaustion (every recorded thesis set `exhausted_if` to exactly the DOL price), so
    `_thesis_exhausted` is now always False and a profitable exit always recalls L2 with
    the thesis intact. The DOL-drawn path is what routes back to L1."""
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis("HIGH"), ts=T0)
    d.on_plan_arrived(_setup(), ts=T0)
    d.on_fill(ts=T0)
    n_l1 = len(d.provider.thesis_reqs)
    d.on_profitable_exit(ts=T0, market_view=_mv(price=19950), pnl=50.0)
    assert d.state == State.AWAITING_SETUP
    assert len(d.provider.thesis_reqs) == n_l1, "no L1 recall — the thesis still stands"


# --------------------------------------------------------------------------- #
# Session risk gate                                                           #
# --------------------------------------------------------------------------- #
def test_risk_breach_halts_keeps_position_no_new_entries():
    gate = RiskGate(RiskGateConfig(daily_loss_limit=-300.0))
    d = TradeDirector(FakeProvider(), FakeMechanism(),
                      risk_gate=gate, confidence_fn=lambda t, f=None: "HIGH")
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis("HIGH"), ts=T0)
    d.on_plan_arrived(_setup(be=[{"type": "price_beyond", "price": 19900, "side": "above"}]), ts=T0)
    d.on_fill(ts=T0)
    assert d.position_open
    d.on_external_halt("loss_limit", ts=T0)
    assert d.state == State.HALTED and d.position_open   # position preserved
    # open position still managed (breakeven fires under HALTED)
    d.on_bar(T0, _mv(price=19950))
    assert d.mechanism.be == 1
    # no new entries: a plan arrival is ignored while halted
    armed_before = list(d.mechanism.armed)
    d.on_plan_arrived(_setup(pid="pl2"), ts=T0)
    assert d.mechanism.armed == armed_before
    # breach cannot be reset by any decision input (a winning close stays breached)
    gate.record_exit(10_000.0)
    assert gate.breached


def test_risk_gate_breach_on_daily_loss_limit():
    gate = RiskGate(RiskGateConfig(daily_loss_limit=-50.0))
    gate.record_exit(-60.0)
    assert gate.breached and not gate.can_enter()


def test_thesis_death_while_halted_stays_halted():
    # A halted session that then sees its thesis falsified must run on_dol_falsified but
    # remain HALTED (no new thesis, no new entries).
    fals = [{"type": "price_beyond", "price": 19000, "side": "below"}]
    d = _in_position(falsified=fals)
    d.on_external_halt("loss_limit", ts=T0)
    assert d.state == State.HALTED and d.position_open
    n_thesis = len(d.provider.thesis_reqs)
    d.on_bar(T0, _mv(price=18000))                 # thesis falsified while halted
    assert d.mechanism.dol == ["MARKET_CLOSE"]     # on_dol_falsified still ran
    assert d.state == State.HALTED                 # stays halted (not NO_THESIS)
    assert len(d.provider.thesis_reqs) == n_thesis  # no L1 call
    assert not d.position_open


def test_new_setup_disarms_prior_armed_mechanism():
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis("HIGH"), ts=T0)
    d.on_plan_arrived(_setup(pid="pl1"), ts=T0)
    assert d.state == State.SETUP_ARMED
    disarms = d.mechanism.disarmed
    d.on_plan_arrived(_setup(pid="pl2"), ts=T0)     # a new SETUP supersedes while armed
    assert d.mechanism.disarmed == disarms + 1      # prior resting mechanism disarmed
    assert d.mechanism.armed[-1] == "pl2"


def test_risk_gate_max_consecutive_losers():
    gate = RiskGate(RiskGateConfig(max_consecutive_losers=2))
    gate.record_exit(-5.0)
    assert not gate.breached
    gate.record_exit(-5.0)
    assert gate.breached


# --------------------------------------------------------------------------- #
# Failure policy                                                              #
# --------------------------------------------------------------------------- #
def test_provider_failure_keeps_flat_no_crash():
    d = TradeDirector(FakeProvider(raise_on={"thesis"}), FakeMechanism())
    d.on_session_open(T0, "facts")                 # provider raises inside — swallowed
    assert d.state == State.NO_THESIS and d.thesis is None


def test_standing_decision_retained_on_recall_failure():
    d = _director()
    d.on_session_open(T0, "facts")
    d.on_thesis_arrived(_thesis("HIGH"), ts=T0)
    d.on_plan_arrived(_setup(), ts=T0)
    assert d.state == State.SETUP_ARMED
    # a failed L1 re-call must not wipe the standing thesis/plan.
    d.on_thesis_failed(ts=T0)
    assert d.thesis is not None and d.plan is not None and d.state == State.SETUP_ARMED
