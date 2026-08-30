"""Task 9: cross-mechanism arbitration."""
import pandas as pd

from agent.trader.arbiter import MAX_ATTEMPTS, Arbiter, Candidate

TZ = "America/New_York"


def _t(hhmmss):
    return pd.Timestamp(f"2026-08-13 {hhmmss}", tz=TZ)


def _stop(mech, trigger, tf="5min"):
    return Candidate(mechanism=mech, kind="stop", trigger=trigger, timeframe=tf)


def _market(mech):
    return Candidate(mechanism=mech, kind="market")


def test_only_one_resting_stop_entry_exists_at_a_time():
    a = Arbiter()
    cands = [_stop("fvg_return_continuation", 29900.0),
             _stop("fvg_negation_reversal", 29850.0, tf="1min")]
    chosen = a.select(cands, price=29890.0)
    assert chosen is not None
    assert len([c for c in cands if c.mechanism == chosen]) == 1


def test_the_closest_trigger_to_price_holds_the_resting_slot():
    a = Arbiter()
    cands = [_stop("fvg_return_continuation", 29900.0),
             _stop("fvg_negation_reversal", 29850.0, tf="1min")]
    assert a.select(cands, price=29890.0) == "fvg_return_continuation"
    assert a.select(cands, price=29855.0) == "fvg_negation_reversal"


def test_nothing_holds_the_slot_when_no_mechanism_wants_it():
    a = Arbiter()
    assert a.select([_market("extreme_reject_close")], price=29890.0) is None
    assert a.select([], price=29890.0) is None


def test_other_armed_mechanisms_may_still_fire_by_market():
    a = Arbiter()
    cands = [_stop("fvg_return_continuation", 29900.0),
             _market("extreme_reject_close"), _market("fvg_1m_post_extreme")]
    a.select(cands, price=29890.0)
    assert all(Arbiter.may_fire_by_market(c) for c in cands if c.kind == "market")
    assert not Arbiter.may_fire_by_market(cands[0])


def test_first_trigger_wins():
    """No ranking and no mechanism preference: whichever the TAPE reaches first — which
    is not the same as whichever the caller listed first, because the bar loop iterates
    mechanisms in a fixed order, not in tape order."""
    reached = [("fvg_return_continuation", _t("09:35:10")),
               ("extreme_reject_close", _t("09:33:02"))]
    assert Arbiter.first_trigger(reached) == "extreme_reject_close"
    assert Arbiter.first_trigger(list(reversed(reached))) == "extreme_reject_close"


def test_first_trigger_ignores_mechanisms_the_tape_has_not_reached():
    reached = [("fvg_return_continuation", None),
               ("extreme_reject_close", _t("09:33:02"))]
    assert Arbiter.first_trigger(reached) == "extreme_reject_close"
    assert Arbiter.first_trigger([("fvg_return_continuation", None)]) is None
    assert Arbiter.first_trigger([]) is None


def test_first_trigger_breaks_a_same_tick_tie_deterministically():
    at = _t("09:33:02")
    reached = [("fvg_return_continuation", at), ("extreme_reject_close", at)]
    assert (Arbiter.first_trigger(reached)
            == Arbiter.first_trigger(list(reversed(reached))))


def test_the_attempt_counter_is_shared_across_mechanisms():
    """A §5 stop-out and a §7 fire both spend from the same per-plan budget of 3."""
    a = Arbiter()
    a.spend("fvg_return_continuation")
    a.spend("extreme_reject_close")
    assert a.attempts_used == 2 and a.attempts_remaining == 1
    assert not a.exhausted()
    a.spend("fvg_1m_post_extreme")
    assert a.exhausted() and a.attempts_remaining == 0


def test_the_budget_is_the_spec_value():
    assert MAX_ATTEMPTS == 3


def test_sec6_never_coexists_with_a_resting_5m_stop_entry():
    """A usable 5m gap disqualifies §6 entirely, so no cross-mechanism cancel is ever
    needed."""
    assert Arbiter.sec6_armed(usable_5m_gaps=0)
    assert not Arbiter.sec6_armed(usable_5m_gaps=1)
    resting = _stop("fvg_return_continuation", 29900.0, tf="5min")
    assert not Arbiter.may_coexist(_market("fvg_1m_post_extreme"), resting)


def test_a_1m_negation_stop_may_coexist_with_an_armed_sec6():
    """The same 5m-unusable state arms both; single-stop-entry and first-trigger-wins
    govern from there."""
    resting = _stop("fvg_negation_reversal", 29850.0, tf="1min")
    assert Arbiter.may_coexist(_market("fvg_1m_post_extreme"), resting)


def test_sec7_coexists_with_anything():
    """§7 enters by market only, so it never competes for the resting slot."""
    for tf in ("5min", "1min"):
        resting = _stop("fvg_return_continuation", 29900.0, tf=tf)
        assert Arbiter.may_coexist(_market("extreme_reject_close"), resting)


def test_attempts_remaining_is_recorded_but_never_scored():
    """07-21 ends with one attempt in reserve; that is an assertion, not a P&L term."""
    a = Arbiter()
    a.spend("fvg_return_continuation")
    a.spend("fvg_1m_post_extreme")
    assert a.attempts_remaining == 1
    assert a.spent_by() == {"fvg_return_continuation": 1, "fvg_1m_post_extreme": 1}


def test_the_slot_holder_is_stable_under_reordering():
    """Two implementations must agree, so the tie-break is deterministic."""
    a = Arbiter()
    cands = [_stop("fvg_return_continuation", 29900.0),
             _stop("fvg_negation_reversal", 29900.0)]
    assert a.select(cands, 29890.0) == a.select(list(reversed(cands)), 29890.0)


def test_no_price_means_no_selection():
    assert Arbiter().select([_stop("fvg_return_continuation", 29900.0)], None) is None
