"""Task 4: §7's state machine and its AGE-keyed stale-extreme anchor."""
import inspect

import pandas as pd
import pytest

from agent.trader.extreme_reject import (SL_CAP_PTS, ExtremeReject, choose_track)

TZ = "America/New_York"
ARM = pd.Timestamp("2026-08-10 09:20", tz=TZ)


def _t(i):
    return pd.Timestamp("2026-08-10 09:30", tz=TZ) + pd.Timedelta(minutes=i)


def _bar(*, high, close, open_, low=None):
    return {"Open": open_, "High": high,
            "Low": low if low is not None else min(open_, close) - 1.0,
            "Close": close}


def _short_machine(extreme=29400.0, arm=ARM, *, swept=True):
    """A machine whose standing counter-thesis extreme is `extreme`.

    `swept=True` drives ONE new-extreme bar first, because §7's first clause makes the
    sweep a PRECONDITION of the quiet counter: "After 09:30 ET price moves against the
    thesis and prints a NEW day extreme. From the NEXT 1m bar, count consecutive 1m
    closes...". A seeded overnight extreme that RTH never approaches must not arm the
    machine off three ordinary opening bars — on 08-10 that fired a spurious 09:36 entry
    two bars before the day's real new low printed.

    The sweep bar takes the extreme by ONE TICK (0.25) and no more, so every later bar in
    every test sits on the same side of the threshold it did before and each test's
    arithmetic is unchanged.
    """
    m = ExtremeReject("DOWN", arm, track="24h", extreme=extreme)
    if swept:
        m.on_bar_close(_t(-1), _bar(high=extreme + 0.25, close=extreme - 20.0,
                                    open_=extreme - 15.0))
        assert m.state()["swept"] and m.state()["extreme"] == extreme + 0.25
    return m


def _armed_short_machine(extreme=29400.0):
    m = _short_machine(extreme)
    for i in range(3):
        m.on_bar_close(_t(i), _bar(high=29390.0, close=29380.0, open_=29385.0))
    assert m.state()["armed"]
    return m


# --- the quiet counter ------------------------------------------------------- #

def test_three_quiet_closes_arm_the_machine():
    m = _short_machine(extreme=29400.0)
    for i in range(3):
        m.on_bar_close(_t(i), _bar(high=29390.0, close=29380.0, open_=29385.0))
    assert m.state()["armed"]


def test_two_quiet_closes_do_not_arm():
    m = _short_machine(extreme=29400.0)
    for i in range(2):
        m.on_bar_close(_t(i), _bar(high=29390.0, close=29380.0, open_=29385.0))
    assert not m.state()["armed"]


def test_a_new_extreme_tick_restarts_the_count_even_on_a_thesis_direction_close():
    """08-10's named behaviour: the 09:35 bar crossed the 09:34 low seconds in and
    restarted the count despite closing green."""
    m = _short_machine(extreme=29400.0)
    m.on_bar_close(_t(0), _bar(high=29390.0, close=29380.0, open_=29385.0))
    m.on_bar_close(_t(1), _bar(high=29405.0, close=29380.0, open_=29385.0))  # new extreme
    assert m.state()["quiet"] == 0


def test_the_count_is_tick_based_not_close_based():
    """A bar whose CLOSE is quiet but whose WICK takes the extreme is not a quiet close.
    §10.2's lesson: extreme tracking must be tick-level on both tracks."""
    m = _short_machine(extreme=29400.0)
    m.on_bar_close(_t(0), _bar(high=29401.0, close=29380.0, open_=29385.0))
    assert m.state()["quiet"] == 0 and m.state()["extreme"] == 29401.0


# --- arming and firing ------------------------------------------------------- #

def test_an_armed_new_extreme_bar_closing_in_the_thesis_direction_fires():
    m = _armed_short_machine(extreme=29400.0)
    fire = m.on_bar_close(_t(4), _bar(high=29410.0, close=29395.0, open_=29405.0))
    assert fire is not None and fire["price"] == 29395.0


def test_an_armed_new_extreme_bar_closing_adverse_does_not_fire_and_stays_armed():
    m = _armed_short_machine(extreme=29400.0)
    assert m.on_bar_close(_t(4), _bar(high=29410.0, close=29408.0, open_=29402.0)) is None
    assert m.state()["armed"], "the graph may still want to extend"


def test_an_armed_bar_with_no_new_extreme_does_not_fire():
    m = _armed_short_machine(extreme=29400.0)
    assert m.on_bar_close(_t(4), _bar(high=29395.0, close=29390.0, open_=29393.0)) is None


def test_an_unarmed_new_extreme_bar_closing_with_thesis_does_not_fire():
    """Three quiet closes are a PRECONDITION, not decoration. 08-06 is the documented
    accepted miss: consecutive new lows 09:30-09:33 never arm before the bottom."""
    m = _short_machine(extreme=29400.0)
    assert m.on_bar_close(_t(0), _bar(high=29410.0, close=29395.0,
                                      open_=29405.0)) is None
    assert not m.state()["armed"]


def test_the_fire_carries_the_plans_direction():
    m = _armed_short_machine()
    fire = m.on_bar_close(_t(4), _bar(high=29410.0, close=29395.0, open_=29405.0))
    assert fire["direction"] == "DOWN"
    assert fire["mechanism"] == "extreme_reject_close"


def test_a_long_plan_mirrors_every_rule():
    m = ExtremeReject("UP", ARM, track="24h", extreme=29400.0)
    m.on_bar_close(_t(-1), _bar(high=29410.0, close=29405.0, open_=29402.0,
                                low=29399.75))
    for i in range(3):
        m.on_bar_close(_t(i), _bar(high=29420.0, close=29415.0, open_=29410.0,
                                   low=29405.0))
    assert m.state()["armed"]
    fire = m.on_bar_close(_t(4), _bar(high=29418.0, close=29412.0, open_=29402.0,
                                      low=29395.0))
    assert fire is not None and fire["price"] == 29412.0 and fire["direction"] == "UP"


# --- the stop ---------------------------------------------------------------- #

def test_the_stop_is_the_opposite_wick_capped_at_15():
    m = _armed_short_machine(extreme=29400.0)
    fire = m.on_bar_close(_t(4), _bar(high=29430.0, close=29395.0, open_=29405.0))
    assert fire["stop"] == 29410.0, "29430 wick is 35 pts away; the 15-pt cap binds"


def test_the_wick_is_used_when_nearer_than_the_cap():
    m = _armed_short_machine(extreme=29400.0)
    fire = m.on_bar_close(_t(4), _bar(high=29402.0, close=29395.0, open_=29399.0))
    assert fire["stop"] == 29402.0


def test_the_cap_is_the_spec_value_and_the_w3c30_alternative_is_not_implemented():
    """§9: the cap15-vs-w3c30 A/B is a knob VALUE and is DEFERRED. cap15 is the
    default; implementing the alternative here would be tuning, not rule-building."""
    assert SL_CAP_PTS == 15.0


# --- the AGE anchor ---------------------------------------------------------- #

def test_an_extreme_older_than_two_hours_selects_the_post_0930_track():
    """08-25: the counter-thesis 24h high was set 05:57, 3.55h before the arm."""
    assert choose_track(age=pd.Timedelta(hours=3.55)) == "post_0930"


def test_a_fresh_extreme_keeps_the_strict_24h_track():
    """08-07: 0.60h old. Must NOT activate the fallback — the documented wrong-arm
    case."""
    assert choose_track(age=pd.Timedelta(hours=0.60)) == "24h"


def test_the_threshold_is_not_fitted_anywhere_in_1h_to_3h():
    """Measured ages leave a gap from 0.60h to 1.90h with nothing in it, so any T in
    1.0h-3.0h gives identical totals. This test pins that insensitivity."""
    for hours in (1.0, 1.5, 2.0, 2.5, 3.0):
        assert choose_track(pd.Timedelta(hours=0.60), threshold_h=hours) == "24h"
        assert choose_track(pd.Timedelta(hours=3.55), threshold_h=hours) == "post_0930"


MEASURED_AGES_H = [0.02, 0.02, 0.07, 0.42, 0.55, 0.60, 1.90, 2.43, 3.55, 6.42, 7.28,
                   8.00, 12.65, 12.65, 13.65, 14.50, 14.92, 15.43, 15.50]


def test_no_measured_age_lies_inside_the_empty_gap():
    """§7's actual claim, stated precisely. The measured ages over 07-15..08-25 leave a
    WIDE GAP with nothing in it between 0.60h and 1.90h — that emptiness, not the value
    2.0, is what makes T unfitted, because the two days the key must separate (08-07 at
    0.60h and 08-25 at 3.55h) sit on opposite sides of the whole gap.

    Note what this does NOT claim: the 1.90h and 2.43h days DO change track across
    T = 1.0 .. 3.0. §7's claim is that the TOTALS are identical there, i.e. those days
    produce no fire either way — not that the classification is invariant. Asserting the
    stronger thing would be asserting something the document does not say.
    """
    assert not [a for a in MEASURED_AGES_H if 0.60 < a < 1.90]


def test_the_two_days_the_key_exists_to_separate_are_separated_at_every_T():
    lo = [a for a in MEASURED_AGES_H if a <= 0.60]
    hi = [a for a in MEASURED_AGES_H if a >= 3.55]
    for hours in (1.0, 1.5, 2.0, 2.5, 3.0):
        assert all(choose_track(pd.Timedelta(hours=a), threshold_h=hours) == "24h"
                   for a in lo)
        assert all(choose_track(pd.Timedelta(hours=a), threshold_h=hours) == "post_0930"
                   for a in hi)


def test_a_missing_extreme_age_keeps_the_strict_track():
    """The fallback is an escape hatch for a STALE pool, never a default."""
    assert choose_track(None) == "24h"


def test_the_age_is_measured_at_the_plan_arm_not_at_evaluation_time():
    """The old distance key straddled its threshold between a 09:20 and a 09:30 reading
    of 08-07 (135.75 vs 152.50 pts). Age must be pinned to the arm."""
    m = _short_machine(extreme=29400.0, arm=pd.Timestamp("2026-08-07 09:20", tz=TZ))
    assert m.state()["age_measured_at"] == pd.Timestamp("2026-08-07 09:20", tz=TZ)


def _executable_source(mod):
    """The module's source with every docstring and comment removed.

    The plan greps the raw source for "150", but this module NAMES the retired distance
    key in prose on purpose — §11.0 exists precisely so retired ideas are not rebuilt
    from stale memory, and deleting the explanation to satisfy a grep would throw away
    the reason. What must be absent is the key in the CODE.
    """
    import ast
    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and ast.get_docstring(node):
            node.body = node.body[1:]
    return ast.unparse(tree)


def test_the_distance_key_is_not_implemented():
    """§11.0 retired list. Its presence would resurrect the 08-07 wrong arm."""
    import agent.trader.extreme_reject as mod
    src = _executable_source(mod)
    assert "150" not in src, "the >150 pt distance key is RETIRED"


def test_the_6h_session_block_anchor_is_not_implemented():
    """Tested and REJECTED for §7 (+35.75, below strict): it needs a NEW extreme beyond
    the reference AFTER arming, and on 08-25 that never comes."""
    import agent.trader.extreme_reject as mod
    src = _executable_source(mod)
    assert "session_block" not in src and "6h" not in src


@pytest.mark.parametrize("track", ["24h", "post_0930"])
def test_both_tracks_run_the_same_state_machine(track):
    """The fallback changes WHICH pool is watched, never how the signature is read."""
    m = ExtremeReject("DOWN", ARM, track=track, extreme=29400.0)
    m.on_bar_close(_t(-1), _bar(high=29400.25, close=29380.0, open_=29385.0))
    for i in range(3):
        m.on_bar_close(_t(i), _bar(high=29390.0, close=29380.0, open_=29385.0))
    fire = m.on_bar_close(_t(4), _bar(high=29410.0, close=29395.0, open_=29405.0))
    assert fire is not None and m.state()["track"] == track


def test_the_quiet_counter_does_not_run_before_a_post_open_sweep():
    """§7's first clause, as its own assertion rather than as a side effect of the
    helpers. A seeded 24h extreme the RTH session never approaches must leave the machine
    unarmed no matter how many quiet closes print — on 08-10 the missing gate fired a
    spurious 09:36 entry two bars before the day's real new low.
    """
    m = _short_machine(extreme=29400.0, swept=False)
    for i in range(6):
        assert m.on_bar_close(_t(i), _bar(high=29390.0, close=29380.0,
                                          open_=29385.0)) is None
    st = m.state()
    assert not st["swept"] and not st["armed"] and st["quiet"] == 0


def test_the_post_0930_track_sweeps_on_its_very_first_bar():
    """It is unseeded by construction, and that first bar IS the first post-09:30
    extreme — so the counter starts immediately, which is what 08-18 and 08-25 need."""
    m = ExtremeReject("DOWN", ARM, track="post_0930")
    m.on_bar_close(_t(0), _bar(high=29390.0, close=29380.0, open_=29385.0))
    assert m.state()["swept"]
