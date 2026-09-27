"""§5's named regression cases (l2-mechanisms.md §11, fresh-tick resolution).

08-18's §5 entry (09:40:58 at 29763.25) is asserted with the suspended 5m mechanisms
re-armed (ACT_TRADER_5M=1); 08-21 and 08-11, documented FLAT, and 08-18's default-config
day are asserted against their current-era records (plan 42, l2-mechanisms.md §11.3).

Expectations are read FROM `named_cases.py`, never restated as literals here: the
registry is the single place every documented figure lives, and a test that restates one
is a second copy that can drift.
"""
import json
import os

import pytest

from agent.trader import named_cases as nc
from agent.trader.replay import run_replay

pytestmark = [pytest.mark.timeout(1800), pytest.mark.slow]


def _decisions(entry):
    p = os.path.join(entry["run_dir"], "trader_decisions.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def _replay(case_key, *, thesis_key=None, five_min=False):
    """Replay a registered case with its oracle thesis. `thesis_key` borrows another
    record's thesis (a current-era record re-measures the SAME day under the SAME thesis);
    `five_min` re-arms the suspended 5m-FVG mechanisms (ACT_TRADER_5M=1, 879032b) so the
    §5 mechanism the documented entry belongs to stays covered."""
    case = nc.by_key(case_key)
    thesis = nc.thesis_for(thesis_key or case_key)
    assert thesis is not None, f"{thesis_key or case_key} has no oracle thesis in the registry"
    prev = os.environ.get("ACT_TRADER_5M")
    if five_min:
        os.environ["ACT_TRADER_5M"] = "1"
    else:
        os.environ.pop("ACT_TRADER_5M", None)
    try:
        res = run_replay([case.date], allow_calls=False, thesis=thesis)
    finally:
        if prev is None:
            os.environ.pop("ACT_TRADER_5M", None)
        else:
            os.environ["ACT_TRADER_5M"] = prev
    return case, _decisions(res[case.date]), res[case.date]["run_dir"]


def _kinds(rec, *kinds):
    return [r for r in rec if r.get("kind") in kinds]


def _assert_current(case, rec, run_dir):
    """A current-era record (§11.3), asserted whole: the bound mechanism, the entry
    second and price, the exit second, P&L and attempts — never P&L alone."""
    from scripts.report_replay_pnl import summarize
    fills = _kinds(rec, "fill")
    assert len(fills) == case.attempts_used, [f["time"] for f in fills]
    assert fills[0]["mechanism"] == case.mechanism, fills[0]["mechanism"]
    assert fills[0]["time"].endswith(f"{case.entry_time}-04:00"), fills[0]["time"]
    assert fills[0]["price"] == case.entry_price
    exits = _kinds(rec, "take_profit")
    assert exits and exits[-1]["time"].endswith(f"{case.exit_time}-04:00"), \
        [e["time"] for e in exits]
    if case.exit_price is not None:
        assert exits[-1]["price"] == case.exit_price
    assert round(summarize(run_dir)["total_pts"], 2) == case.pnl


@pytest.fixture(scope="module")
def r0821():
    return _replay("cur-0821", thesis_key="sec5-0821-flat")


@pytest.fixture(scope="module")
def r0811():
    return _replay("cur-0811", thesis_key="sec5-0811-flat")


@pytest.fixture(scope="module")
def r0818():
    """The §5 mechanism itself, re-armed: its documented entry still reproduces."""
    return _replay("sec5-0818-fresh-entry", five_min=True)



def test_0821_current_era_takes_tmso_reject(r0821):
    """Documented FLAT (§11 §5: the gap is penetrated before the window ends and never
    re-entered fresh — `sec5-0821-flat`, kept and marked SUPERSEDED). §5's own verdict is
    unchanged; the day now trades because other mechanisms arm first — each step is
    attributed in l2-mechanisms.md §11.3 and the record's explained delta."""
    case, rec, run_dir = r0821
    _assert_current(case, rec, run_dir)


def test_0811_current_era_takes_tmso_reject(r0811):
    """Documented FLAT (§10's accepted skip — `sec5-0811-flat`, SUPERSEDED). The DOL no
    longer ends the day (plan 16) and `tmso_reject` takes the open; §11.3."""
    case, rec, run_dir = r0811
    _assert_current(case, rec, run_dir)


def test_0818_enters_on_its_fresh_tick_not_at_the_window_end(r0818):
    """Must enter 09:40:58 at 29763.25 — the fresh retrace — NOT at 09:30:30."""
    case, rec, _run_dir = r0818
    fills = _kinds(rec, "fill")
    assert len(fills) == 1, [f["time"] for f in fills]
    assert fills[0]["time"].endswith(f"{case.entry_time}-04:00"), fills[0]["time"]
    assert fills[0]["price"] == case.entry_price


def test_0818_binds_the_expected_artifact(r0818):
    """Artifact, not just price — the phase-1 ladder defect produced better P&L from the
    WRONG gap and only an artifact assertion caught it."""
    case, rec, _run_dir = r0818
    binds = _kinds(rec, "bind")
    assert binds, "nothing was ever bound"
    assert binds[-1]["artifact_label"].startswith(case.artifact), \
        binds[-1]["artifact_label"]


def test_0818_spends_exactly_one_attempt(r0818):
    """Attempts are ASSERTED, never scored. A row reproducing the right P&L via a
    different attempt count is a real discrepancy."""
    case, rec, _run_dir = r0818
    assert len(_kinds(rec, "stop_out")) == case.attempts_used - 1


def test_0818s_exit_is_the_t2_pick_under_plan_16(r0818):
    """History: the documented +229.75 exits at the 09:20 DOL at 11:01:26 (booked to the
    cent once the window moved to 13:00 on 2026-09-09). Plan 16 (000dfb7) moved the
    take-profit to the T2 pick made at the fill (+138.25 at prev4_day_low 29625.0), and
    plan 40 (2da4d0b) added the nearer `htf_week_running_low` 29631.0, so the SAME §5
    entry now exits at 09:55:08 for +132.25 — `sec5-0818-5m-t2`. The entry assertions
    above are untouched."""
    _case, rec, _run_dir = r0818
    t2 = nc.by_key("sec5-0818-5m-t2")
    assert _kinds(rec, "stop_out") == []
    tps = _kinds(rec, "take_profit")
    assert len(tps) == 1, [t["time"] for t in tps]
    assert tps[0]["time"].endswith(f"{t2.exit_time}-04:00"), tps[0]["time"]
    # Short: entry - exit. Read off the record rather than restated as a literal.
    assert tps[0]["entry"] - tps[0]["price"] == t2.pnl
