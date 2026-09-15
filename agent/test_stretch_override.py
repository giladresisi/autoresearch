"""Plan 37 D1 — the deterministic direction override predicate."""
import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.stretch_override import (AGE_MAX_MIN, SIZE_FLOOR_PTS, stretch_override)

TZ = "America/New_York"


def _st(direction="DOWN", size=500.0, age=2.0, retrace=3.0):
    return {"MNQ": {"direction": direction, "size": size, "age_minutes": age,
                    "retrace_pct": retrace, "mid": 29326.88, "beyond_mid": True,
                    "extreme_price": 29082.75, "origin_price": 29571.0}}


def test_1_a_fresh_stretch_fires_and_returns_the_opposite_direction():
    """The load-bearing case: still extending into the boundary -> expect the reversal."""
    v = stretch_override(_st("DOWN", age=AGE_MAX_MIN - 1))
    assert v["fires"] is True
    assert v["direction"] == "UP"
    assert "still extending" in v["reason"]

    v = stretch_override(_st("UP", age=AGE_MAX_MIN - 1))
    assert v["fires"] is True and v["direction"] == "DOWN"


def test_2_a_halted_stretch_never_fires_however_little_it_retraced():
    """THE RETRACE ARM IS GONE, and this is the test that keeps it gone.

    The first cut fired on "still extending OR halted-but-barely-retraced". D0 over 94
    sessions says a stale stretch that has NOT retraced CONTINUES: the arm alone scores
    25% / 22% / 44% at retrace <= 5 / 10 / 25, against a 53% base rate, and including it
    dropped the whole rule from 68% (22 days) to 55% (47 days). It is an anti-signal, not
    a weak one."""
    for retrace in (0.0, 1.0, 5.0, 24.0):
        v = stretch_override(_st(age=AGE_MAX_MIN + 120, retrace=retrace))
        assert v["fires"] is False, f"retrace={retrace} must not resurrect the arm"
        assert "already halted" in v["reason"]


def test_3_a_stale_stretch_does_not_fire():
    v = stretch_override(_st(age=AGE_MAX_MIN + 120, retrace=80.0))
    assert v["fires"] is False
    assert v["direction"] is None
    assert "age" in v["reason"]


def test_4_a_stretch_below_the_size_floor_does_not_fire_however_fresh():
    """The floor keeps a flat overnight out. It is NOT the discriminator -- size inverts at
    the top end (>=450 pts unreversed reverses only 43%), so it must never do the work."""
    v = stretch_override(_st(size=SIZE_FLOOR_PTS - 1, age=0.0, retrace=0.0))
    assert v["fires"] is False
    assert "floor" in v["reason"]


def test_5_a_missing_or_unreadable_stretch_does_not_fire_and_does_not_raise():
    """The caller must fall THROUGH to the model on any of these, never guess."""
    for bad in (None, {}, {"MNQ": None}, {"MES": _st()["MNQ"]}, "nonsense"):
        v = stretch_override(bad)
        assert v["fires"] is False and v["direction"] is None
    assert stretch_override({"MNQ": {"direction": "DOWN"}})["fires"] is False
    assert stretch_override({"MNQ": {"direction": "NEUTRAL", "size": 500.0,
                                     "age_minutes": 1.0, "retrace_pct": 1.0}})["fires"] is False
    assert stretch_override({"MNQ": {"direction": "DOWN", "size": "big",
                                     "age_minutes": 1.0, "retrace_pct": 1.0}})["fires"] is False


def test_7_the_deciding_ticker_is_pinned_when_the_two_assets_disagree():
    """MNQ is the traded instrument and decides. 2026-09-01 cannot discriminate -- both
    assets fire there -- so the rule is pinned on a synthetic disagreement instead."""
    mixed = {"MNQ": {"direction": "DOWN", "size": 500.0, "age_minutes": 2.0,
                     "retrace_pct": 3.0},
             "MES": {"direction": "UP", "size": 60.0, "age_minutes": 2.0,
                     "retrace_pct": 3.0}}
    assert stretch_override(mixed)["direction"] == "UP"          # opposite MNQ's DOWN
    assert stretch_override(mixed, ticker="MES")["direction"] == "DOWN"


# --------------------------------------------------------------------------- #
# the real 2026-09-01 tape                                                     #
# --------------------------------------------------------------------------- #

try:
    from backtest_smt import _main_dir_for_date
    from agent.facts.assemble import assemble_facts
    from agent.trader.graft import EXECUTOR_HISTORY
    _MAIN = str(_main_dir_for_date("2026-09-01"))
except Exception:
    _main_dir_for_date = assemble_facts = EXECUTOR_HISTORY = None
    _MAIN = ""

_needs_tape = pytest.mark.skipif(not _MAIN or not os.path.isdir(_MAIN),
                                 reason="machine-local main parquets not available")


@pytest.fixture(scope="module")
def facts_0901():
    """The view the Analyzer actually receives: the graft splices 1m history onto the
    session's 1s frame, which is why `age_minutes` reads 2 here and not 1."""
    now = pd.Timestamp("2026-09-01 09:20", tz=TZ)
    mid = _main_dir_for_date("2026-09-01")
    bars = {}
    for tk in ("MNQ", "MES"):
        s1 = pd.read_parquet(mid / f"{tk}_1s.parquet")
        m1 = pd.read_parquet(mid / f"{tk}_1m.parquet")
        today = s1.loc[pd.Timestamp("2026-09-01 00:00", tz=TZ):now]
        hist = m1[(m1.index >= now - EXECUTOR_HISTORY) & (m1.index < today.index[0])]
        bars[tk] = pd.concat([hist, today])
    _ft, _ct, facts, _mag = assemble_facts(None, bars, now)
    return facts


@_needs_tape
@pytest.mark.timeout(300)
def test_6_the_real_2026_09_01_stretch_fires_and_forces_up(facts_0901):
    """The date this override was calibrated on. MNQ: DOWN 488.25 pts, extreme 09:18,
    age_minutes 2, retraced 3.0% -- the strongest bucket."""
    st = facts_0901["session_stretch"]["MNQ"]
    assert st["direction"] == "DOWN"
    assert st["size"] == 488.25
    assert st["age_minutes"] == 2
    assert st["retrace_pct"] == 3.0

    v = stretch_override(facts_0901["session_stretch"])
    assert v["fires"] is True
    assert v["direction"] == "UP"


@_needs_tape
@pytest.mark.timeout(300)
def test_the_l1_view_carries_session_stretch_for_both_assets(facts_0901):
    """The override reads the SAME view the model would have been sent, so the two can
    never disagree about what the stretch was."""
    ss = facts_0901["session_stretch"]
    assert set(ss) == {"MNQ", "MES"}
    assert ss["MES"]["size"] == 66.5          # an order of magnitude below MNQ's 488.25
    assert ss["MES"]["age_minutes"] == 15
