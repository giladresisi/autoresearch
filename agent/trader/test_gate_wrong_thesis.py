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
            "falsified_if": [{"type": "price_beyond", "price": 29950.0, "side": "above"}]}


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


def test_the_inverted_plan_is_bounded_by_attempts_and_records_the_falsifier(_inv):
    """The §10.2 stress asks whether the bleed is BOUNDED, not by what mechanism.

    Falsification is recorded and never acted on (2026-08-29), so the bound is the
    3-attempt counter — which is exactly §10.2's own structural ceiling, "3 x (bound-gap
    height + 10)". Falsification was never among the brakes §10.2 names (the DOL-floor
    veto, early sweeps completing plans flat, the §6/§7/§8 gates), so removing it from
    the death path cannot weaken that section's claim.

    Measured on this run: fill 09:31:34 -> stop 09:33:24, falsifier records 09:36:00,
    then two further attempts (09:52:21 and 10:17:15) that acting on the falsifier would
    have prevented. That two-stop-out difference is the counterfactual the record exists
    to produce, and it is why the record is a TIMESTAMP rather than a flag.
    """
    deaths = [d for d in _decisions(_inv["run_dir"]) if d["kind"] == "plan_dead"]
    assert deaths, "the inverted plan must still die — an unbounded wrong plan is the risk"
    assert deaths[0]["reason"] == "attempts_exhausted"
    assert deaths[0]["detail"]["attempts_used"] == 3

    fired = [d for d in _decisions(_inv["run_dir"]) if d["kind"] == "would_have_falsified"]
    assert len(fired) == 1, "recorded exactly once — a standing falsifier must not repeat"
    assert fired[0]["predicate"]["type"] == "price_beyond"

    # The counterfactual: entries the plan took AFTER the falsifier fired are precisely
    # what acting on it would have forgone.
    after = [d for d in _decisions(_inv["run_dir"])
             if d["kind"] == "fill" and d["time"] > fired[0]["time"]]
    assert after, "if nothing follows the falsifier, the record cannot measure anything"


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
