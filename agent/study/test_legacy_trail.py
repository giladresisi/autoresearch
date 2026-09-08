"""The reconstructed incumbent trail — structure only. IT IS NOT FIDELITY-VALIDATED.

**Read this before using `legacy_trail` for anything.** The fidelity gate — reproducing
recorded `new-stop-exit` / `move-stop-exit` events from the two dates that have a baseline
event stream — **FAILED**: 0 of 39 recorded stops reproduced at the right price, and the
failure is at the LADDER layer (1 of 17 rung names matched), not in the state machine
below. Plan 33 §9 records the three causes, all of them inputs that cannot be
reconstructed: `cautious_dist_shrinks`, `invalidated_names`, and the liquidity list's own
ORDER deciding ties.

So these tests pin what the module IS — the branch structure of `trend.py:478-730` and the
fact that it borrows rather than copies — and deliberately do NOT assert that it matches
production. No corpus number was produced from it and none should be until the gate passes.
"""
import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study import legacy_trail as lt                          # noqa: E402

TZ = "America/New_York"
DATE = "2026-06-10"


def _bars(rows):
    idx = pd.DatetimeIndex([pd.Timestamp(f"{DATE} {r[0]}", tz=TZ) for r in rows])
    return pd.DataFrame(
        {"Open": [float(r[1]) for r in rows], "High": [float(r[2]) for r in rows],
         "Low": [float(r[3]) for r in rows], "Close": [float(r[4]) for r in rows],
         "Volume": [100.0] * len(rows)}, index=idx)


#: Rising 1m history BEFORE the fill. `_last_same_dir_ref_bar` skips the current bar and,
#: at `period_minutes=5`, any 5m bin not yet complete — so a fixture without prior
#: same-direction bars yields no reference bar, `_floored_break_price` returns None, and
#: production defers arming ("the trail will arm once price clears entry"). That is
#: faithful behaviour, so the fixture has to supply the history rather than the code lose
#: the branch.
_HISTORY = [("08:45", 60, 62, 59, 61), ("08:46", 61, 63, 60, 62),
            ("08:47", 62, 64, 61, 63), ("08:48", 63, 65, 62, 64),
            ("08:49", 64, 66, 63, 65), ("08:50", 65, 67, 64, 66),
            ("08:51", 66, 68, 65, 67), ("08:52", 67, 69, 66, 68),
            ("08:53", 68, 70, 67, 69), ("08:54", 69, 71, 68, 70),
            ("08:55", 70, 72, 69, 71), ("08:56", 71, 73, 70, 72),
            ("08:57", 72, 74, 71, 73), ("08:58", 73, 75, 72, 74),
            ("08:59", 74, 99, 73, 98)]


def _run(rows, **kw):
    base = dict(date=DATE, direction="up", fill_ts=pd.Timestamp(f"{DATE} 09:00", tz=TZ),
                fill_price=100.0, original_stop=75.0,
                cautious_initial=200.0, cautious_secondary=300.0,
                level_initial="lvl1", level_secondary="lvl2")
    base.update(kw)
    return lt.run_legacy_trail(_bars(_HISTORY + list(rows)), **base)


# --------------------------------------------------------------------------- #
# the rules are BORROWED, not restated                                          #
# --------------------------------------------------------------------------- #

def test_the_helpers_come_from_trend_itself():
    import trend
    assert lt._last_same_dir_ref_bar is trend._last_same_dir_ref_bar
    assert lt._floored_break_price is trend._floored_break_price
    assert lt.BREAKEVEN_BUFFER_PTS == trend.BREAKEVEN_BUFFER_PTS == 5.0
    assert lt.INITIAL_STOP_MIN_DIST_PTS == trend.INITIAL_STOP_MIN_DIST_PTS == 50.0


def test_it_writes_no_legacy_state(monkeypatch):
    import smt_state
    for name in ("save_daily", "save_position", "save_hypothesis", "save_smts",
                 "save_global"):
        if hasattr(smt_state, name):
            monkeypatch.setattr(smt_state, name,
                                _boom(name))
    _run([("09:01", 100, 101, 99, 100), ("09:02", 100, 101, 99, 100)])


# --------------------------------------------------------------------------- #
# the state machine's documented branches                                       #
# --------------------------------------------------------------------------- #

def test_the_original_stop_stands_until_something_arms():
    run = _run([("09:01", 100, 101, 74, 100)])
    assert run.exit_reason == lt.EXIT_ORIGINAL_STOP
    assert run.exit_price == pytest.approx(75.0)
    assert run.events == []


def test_a_rung_within_the_minimum_distance_arms_nothing():
    """`INITIAL_STOP_MIN_DIST_PTS`: touching a rung that close to the entry confirms
    nothing, so the original stop stands."""
    run = _run([("09:01", 100, 130, 99, 129), ("09:02", 129, 131, 74, 100)],
               cautious_initial=120.0)          # only 20 pts from the 100 fill
    assert not run.events
    assert run.exit_reason == lt.EXIT_ORIGINAL_STOP


def test_a_wick_only_touch_of_the_initial_places_the_MIDPOINT_stop():
    run = _run([("09:01", 100, 205, 99, 150)], cautious_initial=200.0)
    assert [e.level for e in run.events] == ["initial_mid"]
    # midpoint of the original stop (75) and the rung (200)
    assert run.events[0].price == pytest.approx((75.0 + 200.0) / 2.0)


def test_a_close_beyond_the_initial_arms_the_structural_stop_floored_at_breakeven():
    rows = [("09:01", 100, 150, 99, 149), ("09:02", 149, 160, 148, 159),
            ("09:03", 159, 210, 158, 209)]
    run = _run(rows, cautious_initial=200.0, cautious_secondary=900.0)
    assert [e.level for e in run.events][:1] == ["initial"]
    # never worse than fill - BREAKEVEN_BUFFER_PTS
    assert run.events[0].price >= 100.0 - lt.BREAKEVEN_BUFFER_PTS


def test_the_secondary_takes_priority_over_the_initial_on_the_same_bar():
    run = _run([("09:01", 100, 400, 99, 350)], cautious_initial=200.0,
               cautious_secondary=300.0)
    assert [e.level for e in run.events] == ["secondary"]


def test_an_ath_secondary_puts_the_stop_at_the_fill_price_exactly():
    run = _run([("09:01", 100, 400, 99, 350)], cautious_secondary=300.0,
               level_secondary="day_high", session_ath=250.0)
    assert run.ath_secondary is True
    assert run.events[0].price == pytest.approx(100.0)


def test_a_non_ath_level_name_does_not_trigger_the_ath_branch():
    run = _run([("09:01", 100, 400, 99, 350)], cautious_secondary=300.0,
               level_secondary="london_high", session_ath=250.0)
    assert run.ath_secondary is False


def test_the_trail_only_ever_tightens():
    rows = [("09:01", 100, 150, 99, 149), ("09:02", 149, 160, 148, 159),
            ("09:03", 159, 210, 158, 209), ("09:04", 209, 215, 208, 214),
            ("09:05", 214, 220, 213, 219), ("09:06", 219, 225, 218, 224),
            ("09:07", 224, 230, 223, 229), ("09:08", 229, 235, 228, 234)]
    run = _run(rows, cautious_initial=200.0, cautious_secondary=900.0)
    prices = [e.price for e in run.events]
    assert prices == sorted(prices)


def test_every_clause_mirrors_for_a_short():
    pivot = 400.0
    rows = [("09:01", 100, 205, 99, 150)]
    long_run = _run(rows, cautious_initial=200.0)
    mirrored = [(r[0], pivot - r[1], pivot - r[3], pivot - r[2], pivot - r[4])
                for r in _HISTORY + list(rows)]
    short_run = lt.run_legacy_trail(
        _bars(mirrored), date=DATE, direction="down",
        fill_ts=pd.Timestamp(f"{DATE} 09:00", tz=TZ), fill_price=pivot - 100.0,
        original_stop=pivot - 75.0, cautious_initial=pivot - 200.0,
        cautious_secondary=pivot - 300.0, level_initial="lvl1", level_secondary="lvl2")
    assert [e.level for e in short_run.events] == [e.level for e in long_run.events]
    assert short_run.events[0].price == pytest.approx(pivot - long_run.events[0].price)


def test_the_run_records_that_it_is_a_reconstruction_not_a_replay():
    """A row must carry enough to be re-judged when the gate is revisited."""
    run = _run([("09:01", 100, 205, 99, 150)], cautious_initial=200.0)
    d = run.to_dict()
    assert set(d) >= {"cautious_initial", "cautious_secondary", "original_stop",
                      "events", "final_state", "ath_secondary"}


def _boom(name):
    def _f(*_a, **_kw):
        raise AssertionError(f"{name}() called from a read-only reconstruction")
    return _f
