"""Gate: §10.2 wrong-thesis stress is a first-class run, not a hand-rolled simulator."""
import json
import os

import pytest

from agent.trader.replay import run_replay

DATE = "2026-08-13"
pytestmark = pytest.mark.timeout(900)

# 08-13 recorded: UP, DOL prev1_day_high 30001.5, touched 09:36:07.
# §10.2 inversion: DOWN with a symmetric opposite-side DOL, and a falsifier price
# ABOVE the day's range so the inverted plan is provably wrong all session.
INVERTED = {"bias": "DOWN", "regime": "TREND", "confidence": "HIGH",
            "dol": {"level": "inverted_dol", "price": 29700.0},
            "falsified_if": [{"type": "price_beyond", "price": 29950.0, "side": "above"}],
            "exhausted_if": [{"type": "price_beyond", "price": 29700.0, "side": "below"}]}


@pytest.fixture(scope="module")
def _inv():
    return run_replay([DATE], thesis=INVERTED)[DATE]


def _decisions(run_dir):
    p = os.path.join(run_dir, "trader_decisions.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def test_the_inverted_run_completes_without_a_model_call(_inv):
    assert _inv["cache"]["calls"] == 0


def test_the_inverted_plan_arms(_inv):
    with open(os.path.join(_inv["run_dir"], "plans.json"), encoding="utf-8") as fh:
        assert json.load(fh), "a wrong thesis must still arm — that is the point"


def test_the_inverted_plan_dies_by_falsification_not_by_dol(_inv):
    """08-13 rallied to ~30215; a DOWN plan falsified at 29950 must die falsified.
    Before Task 2 this was impossible — the plan would have run to the window end."""
    deaths = [d for d in _decisions(_inv["run_dir"]) if d["kind"] == "plan_dead"]
    assert deaths, "the inverted plan must die"
    assert deaths[0]["reason"] == "falsified"


def test_the_plan_dies_before_the_window_end(_inv):
    import pandas as pd
    deaths = [d for d in _decisions(_inv["run_dir"]) if d["kind"] == "plan_dead"]
    assert pd.Timestamp(deaths[0]["time"]) < pd.Timestamp(
        f"{DATE} 11:00", tz="America/New_York")


def test_the_run_still_covers_the_whole_window_after_the_death(_inv):
    """Plan death stops BINDING, never the run — the §7 scope lesson, and what keeps
    A/B windows comparable."""
    import pandas as pd
    assert pd.Timestamp(_inv["last_bar"]) >= pd.Timestamp(
        f"{DATE} 10:58", tz="America/New_York")


def test_the_inverted_run_is_deterministic():
    a = run_replay([DATE], thesis=INVERTED)[DATE]
    b = run_replay([DATE], thesis=INVERTED)[DATE]
    assert a["run_dir"] != b["run_dir"]
    assert _decisions(a["run_dir"]) == _decisions(b["run_dir"])


def test_the_correct_and_inverted_runs_differ(_inv):
    """A stress run that matched the real one would be measuring nothing."""
    real = run_replay([DATE], allow_calls=False)[DATE]
    assert _decisions(real["run_dir"]) != _decisions(_inv["run_dir"])
