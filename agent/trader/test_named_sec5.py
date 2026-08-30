"""§5's named regression cases (l2-mechanisms.md §11, fresh-tick resolution).

08-21 FLAT, 08-11 FLAT, 08-18 enters 09:40:58 at 29763.25.

Expectations are read FROM `named_cases.py`, never restated as literals here: the
registry is the single place every documented figure lives, and a test that restates one
is a second copy that can drift.
"""
import json
import os

import pytest

from agent.trader import named_cases as nc
from agent.trader.replay import run_replay

pytestmark = pytest.mark.timeout(1800)


def _decisions(entry):
    p = os.path.join(entry["run_dir"], "trader_decisions.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def _replay(case_key):
    case = nc.by_key(case_key)
    thesis = nc.thesis_for(case_key)
    assert thesis is not None, f"{case_key} has no oracle thesis in the registry"
    res = run_replay([case.date], allow_calls=False, thesis=thesis)
    return case, _decisions(res[case.date])


def _kinds(rec, *kinds):
    return [r for r in rec if r.get("kind") in kinds]


@pytest.fixture(scope="module")
def r0821():
    return _replay("sec5-0821-flat")


@pytest.fixture(scope="module")
def r0811():
    return _replay("sec5-0811-flat")


@pytest.fixture(scope="module")
def r0818():
    return _replay("sec5-0818-fresh-entry")


def test_0821_is_flat_prior_penetration_only(r0821):
    """The gap [29472.25, 29485.00] is penetrated BEFORE the window ends and never
    re-entered fresh. The literal reading fills 29442.50 at 09:30:30 for +169.00; the
    fresh reading takes nothing. FLAT is correct.

    This case cannot even be evaluated until Task 1 lands: under identity gating the gap
    is visible from its 09:30 name and a pre-existence retrace satisfies §5.
    """
    case, rec = r0821
    assert case.expect == "flat"
    assert _kinds(rec, "fill", "intended_entry") == []


def test_0811_is_flat_the_documented_accepted_skip(r0811):
    """§10's own 'accepted skip': the correct binding [29835.00, 29855.25] was only ever
    entered at or before the L1 arm, and price never retraced into it post-window."""
    case, rec = r0811
    assert case.expect == "flat"
    assert _kinds(rec, "fill", "intended_entry") == []


def test_0818_enters_on_its_fresh_tick_not_at_the_window_end(r0818):
    """Must enter 09:40:58 at 29763.25 — the fresh retrace — NOT at 09:30:30."""
    case, rec = r0818
    fills = _kinds(rec, "fill")
    assert len(fills) == 1, [f["time"] for f in fills]
    assert fills[0]["time"].endswith(f"{case.entry_time}-04:00"), fills[0]["time"]
    assert fills[0]["price"] == case.entry_price


def test_0818_binds_the_expected_artifact(r0818):
    """Artifact, not just price — the phase-1 ladder defect produced better P&L from the
    WRONG gap and only an artifact assertion caught it."""
    case, rec = r0818
    binds = _kinds(rec, "bind")
    assert binds, "nothing was ever bound"
    assert binds[-1]["artifact_label"].startswith(case.artifact), \
        binds[-1]["artifact_label"]


def test_0818_spends_exactly_one_attempt(r0818):
    """Attempts are ASSERTED, never scored. A row reproducing the right P&L via a
    different attempt count is a real discrepancy."""
    case, rec = r0818
    assert len(_kinds(rec, "stop_out")) == case.attempts_used - 1


def test_0818s_exit_falls_OUTSIDE_the_replay_window(r0818):
    """VALIDATION GAP, asserted rather than hidden. The documented +229.75 is taken at
    the 11:01:26 DOL touch, and `replay.py`'s window ends at 11:00 by construction —
    so this replay CANNOT book that exit. What it can prove, and does above, is that the
    entry is exact to the second, the price and the bound artifact.

    The assertion here is that the trade is still ALIVE at the window end: no stop-out
    and no take-profit. A stop-out inside the window would contradict the documented
    result outright and is the failure this guards.
    """
    _case, rec = r0818
    assert _kinds(rec, "stop_out") == []
    assert _kinds(rec, "take_profit") == []
