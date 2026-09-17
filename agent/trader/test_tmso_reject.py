"""`tmso_reject`: the sweep-then-confirm state machine, including the 2026-09-17 reading
that lets the sweep bar confirm itself (`SWEEP_BAR_MAY_CONFIRM`)."""
import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import agent.trader.tmso_reject as tr
from agent.trader.tmso_reject import SL_CAP_PTS, TmsoReject

TZ = "America/New_York"
TMSO = 29717.75


def _ts(hhmm):
    return pd.Timestamp(f"2026-09-17 {hhmm}", tz=TZ)


def _bar(o, h, l, c):
    return {"Open": o, "High": h, "Low": l, "Close": c}


# The real 2026-09-17 09:30 and 09:31 MNQ bars.
BAR_0930 = _bar(29717.75, 29737.75, 29698.25, 29717.50)     # sweeps over, closes under, RED
BAR_0931 = _bar(29717.50, 29724.50, 29673.00, 29674.75)     # RED


def test_1_a_sweep_bar_that_closes_with_the_thesis_enters_at_its_own_close():
    m = TmsoReject("DOWN")
    fire = m.on_bar_close(_ts("09:31"), BAR_0930, TMSO)
    assert fire is not None
    assert fire["time"] == _ts("09:31")
    assert fire["price"] == 29717.50
    assert fire["direction"] == "DOWN" and fire["mechanism"] == "tmso_reject"
    # The stop is this bar's own swept extreme, capped: 29737.75 is 20.25 away, so the
    # 15-pt cap binds.
    assert fire["stop"] == 29717.50 + SL_CAP_PTS


def test_2_a_sweep_bar_that_closes_against_the_thesis_still_needs_the_next_bar():
    """The original two-bar signature, unchanged. 09-03's shape, mirrored for a short:
    the sweep bar closes back under TMSO but GREEN."""
    m = TmsoReject("DOWN")
    sweep = _bar(29700.00, 29737.75, 29698.25, 29717.50)     # closes under TMSO, GREEN
    assert m.on_bar_close(_ts("09:31"), sweep, TMSO) is None
    assert m.state()["armed"] is True
    fire = m.on_bar_close(_ts("09:32"), BAR_0931, TMSO)
    assert fire is not None and fire["time"] == _ts("09:32")
    assert fire["price"] == 29674.75
    # The stop anchors to the SWEEP bar's extreme (capped), not the confirming bar's.
    assert fire["stop"] == 29674.75 + SL_CAP_PTS


def test_3_a_missed_confirmation_spends_the_setup():
    m = TmsoReject("DOWN")
    sweep = _bar(29700.00, 29737.75, 29698.25, 29717.50)
    m.on_bar_close(_ts("09:31"), sweep, TMSO)
    green_no_sweep = _bar(29700.00, 29710.00, 29695.00, 29708.00)
    assert m.on_bar_close(_ts("09:32"), green_no_sweep, TMSO) is None
    assert m.state()["armed"] is False
    # A later with-thesis bar that does NOT touch TMSO has nothing to confirm. (The real
    # 09:31 bar would not do here: its high is back over TMSO, so it is a sweep bar itself.)
    red_below = _bar(29708.00, 29712.00, 29673.00, 29674.75)
    assert m.on_bar_close(_ts("09:33"), red_below, TMSO) is None


def test_4_a_sweep_without_a_close_back_never_arms_or_fires():
    m = TmsoReject("DOWN")
    through = _bar(29717.75, 29760.00, 29715.00, 29750.00)   # closes ABOVE: accepted, not rejected
    assert m.on_bar_close(_ts("09:31"), through, TMSO) is None
    assert m.state()["armed"] is False


def test_5_a_with_thesis_bar_that_never_touched_tmso_does_not_fire():
    m = TmsoReject("DOWN")
    assert m.on_bar_close(_ts("09:31"), _bar(29700.0, 29710.0, 29680.0, 29685.0), TMSO) is None


def test_6_one_fire_per_micro_session_either_way():
    m = TmsoReject("DOWN")
    assert m.on_bar_close(_ts("09:31"), BAR_0930, TMSO) is not None
    assert m.on_bar_close(_ts("09:45"), BAR_0930, TMSO) is None      # same 09:00 session
    # 10:30 opens the next micro-session: the latch is per session, not per day.
    assert m.on_bar_close(_ts("10:55"), BAR_0930, TMSO) is not None


def test_7_long_side_is_the_mirror():
    m = TmsoReject("UP")
    bar = _bar(29214.50, 29230.00, 29199.25, 29225.00)       # sweeps under, closes over, GREEN
    fire = m.on_bar_close(_ts("09:37"), bar, 29214.50)
    assert fire is not None and fire["direction"] == "UP"
    assert fire["price"] == 29225.00
    assert fire["stop"] == 29225.00 - SL_CAP_PTS             # wick is 25.75 away; cap binds


def test_8_no_tmso_yet_clears_any_armed_setup_and_never_fires():
    m = TmsoReject("DOWN")
    m.on_bar_close(_ts("09:31"), _bar(29700.00, 29737.75, 29698.25, 29717.50), TMSO)
    assert m.on_bar_close(_ts("09:32"), BAR_0931, None) is None
    assert m.state()["armed"] is False


def test_9_the_constant_restores_the_strict_two_bar_signature(monkeypatch):
    """One line, so the A/B is cheap: with it off the 09-17 sweep bar only ARMS, and the
    entry is the documented 09:32:00 @ 29674.75."""
    monkeypatch.setattr(tr, "SWEEP_BAR_MAY_CONFIRM", False)
    m = TmsoReject("DOWN")
    assert m.on_bar_close(_ts("09:31"), BAR_0930, TMSO) is None
    assert m.state()["armed"] is True
    fire = m.on_bar_close(_ts("09:32"), BAR_0931, TMSO)
    assert fire is not None and fire["price"] == 29674.75
