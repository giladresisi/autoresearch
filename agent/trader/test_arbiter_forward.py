"""Task 9's added step: arbitration against the ONLY multi-mechanism record.

Every other named case in this cycle validates a mechanism IN ISOLATION. §7's own
backcheck says so — "a §7-ONLY backcheck, so nothing competes for the shared 3-attempt
counter" — and §6.2's records are single-mechanism too. §10's 08-11..08-14 forward test
is the one place the mechanisms' INTERACTION was measured, which is why it is here:
arbitration checked only against its own internal consistency is checked against
precisely the property a bug preserves.

ERA CAVEAT, and it is not optional. These figures predate the 2026-08-22 knob adoption
(entry buffer 7->3, SL cap 25, max height 35->45) and the §7 re-key. The SHAPE — which
mechanism binds, how many attempts, which vetoes fire, the entry ORDER — is asserted
exactly; each P&L is a target whose delta must be EXPLAINED before acceptance.

PLAN 42 (2026-09-27). The later rule changes (plan 16's T2 target, §6/§7 wiring,
`tmso_reject`, the 5m-FVG suspension) moved every one of these days; the operator confirmed
the new behaviour as intended. 08-13's documented §5 continuation still reproduces with
the suspended 5m mechanisms re-armed (ACT_TRADER_5M=1) and keeps its original tests. The
08-11/08-12/08-14 records are kept in the registry, marked SUPERSEDED, and each day is
asserted here against its current-era record (l2-mechanisms.md §11.3).
"""
import json
import os

import pytest

from agent.trader import named_cases as nc
from agent.trader.replay import run_replay

pytestmark = [pytest.mark.timeout(1800), pytest.mark.slow]

#: 08-13's documented delta resolves into this adoption and nothing else. (08-12's
#: 6.00-pt SL-cap saving lives in its SUPERSEDED registry record, `sec10-0812`.)
BUFFER_TRIM = 4.00                # entry buffer 7 -> 3


def _run(key, *, thesis_key=None, five_min=False, with_dir=False):
    """`thesis_key` borrows the documented record's oracle thesis for a current-era
    record of the same day; `five_min` re-arms the suspended 5m mechanisms (879032b)."""
    case = nc.by_key(key)
    thesis = nc.thesis_for(thesis_key or key)
    assert thesis is not None, f"{thesis_key or key} has no oracle thesis"
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
    run_dir = res[case.date]["run_dir"]
    path = os.path.join(run_dir, "trader_decisions.jsonl")
    rows = ([json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
            if os.path.exists(path) else [])
    return (case, rows, run_dir) if with_dir else (case, rows)


def _kind(rows, *kinds):
    return [r for r in rows if r.get("kind") in kinds]


def _pnl(rows):
    total, entry, direction = 0.0, None, None
    for r in rows:
        if r["kind"] == "fill":
            entry = float(r["price"])
            direction = str(r.get("direction") or "").upper()
        elif r["kind"] in ("stop_out", "take_profit") and entry is not None:
            sign = 1.0 if direction in ("UP", "LONG") else -1.0
            total += sign * (float(r["price"]) - entry)
            entry = None
    return round(total, 2)


@pytest.fixture(scope="module")
def r0813():
    """08-13's documented §5 continuation, with the suspended 5m mechanisms re-armed."""
    return _run("sec10-0813", five_min=True)


@pytest.fixture(scope="module")
def c0811():
    return _run("cur-0811", thesis_key="sec10-0811-no-entry", with_dir=True)


@pytest.fixture(scope="module")
def c0812():
    return _run("cur-0812", thesis_key="sec10-0812", with_dir=True)


@pytest.fixture(scope="module")
def c0813():
    return _run("cur-0813", thesis_key="sec10-0813", with_dir=True)


@pytest.fixture(scope="module")
def c0814():
    return _run("cur-0814", thesis_key="sec10-0814", with_dir=True)


def _total(run_dir):
    """Realised + MARKED: 08-12's current trade is open at the window end."""
    from scripts.report_replay_pnl import summarize
    return round(summarize(run_dir)["total_pts"], 2)


def _assert_first_fill(case, rows):
    fills = _kind(rows, "fill")
    assert len(fills) == case.attempts_used, [f["time"] for f in fills]
    if case.entry_time is not None:
        assert fills[0]["time"].endswith(f"{case.entry_time}-04:00"), fills[0]["time"]
        assert fills[0]["price"] == case.entry_price
    return fills


# --- current era (plan 42, l2-mechanisms.md §11.3) --------------------------- #

def test_0811_current_era_shape(c0811):
    """Documented FLAT (`sec10-0811-no-entry`, SUPERSEDED): `tmso_reject` takes the open
    and reaches its T2 target; the DOL touch is recorded, not lethal (plan 16)."""
    case, rows, run_dir = c0811
    fills = _assert_first_fill(case, rows)
    assert fills[0]["mechanism"] == case.mechanism
    tp = _kind(rows, "take_profit")
    assert len(tp) == 1 and tp[0]["time"].endswith(f"{case.exit_time}-04:00")
    assert tp[0]["price"] == case.exit_price
    assert _total(run_dir) == case.pnl


def test_0812_current_era_shape(c0812):
    """Documented +46.75 on two §4 attempts (`sec10-0812`, SUPERSEDED): `tmso_reject`
    confirms on its sweep bar at 09:31:00 and holds to the window end (MARKED)."""
    case, rows, run_dir = c0812
    fills = _assert_first_fill(case, rows)
    assert fills[0]["mechanism"] == case.mechanism
    assert _kind(rows, "stop_out", "take_profit") == [], "the position is only marked"
    assert _total(run_dir) == case.pnl


def test_0813_current_era_shape(c0813):
    """Default configuration: `tmso_reject` 09:35:00 to the SAME 30001.5 at the SAME
    09:36:43 the documented continuation reaches — a worse entry, same exit."""
    case, rows, run_dir = c0813
    fills = _assert_first_fill(case, rows)
    assert fills[0]["mechanism"] == case.mechanism
    tp = _kind(rows, "take_profit")
    assert len(tp) == 1 and tp[0]["time"].endswith(f"{case.exit_time}-04:00")
    assert tp[0]["price"] == case.exit_price
    assert _total(run_dir) == case.pnl


def test_0814_current_era_shape(c0814):
    """Documented +99.50 on 3 attempts (`sec10-0814`, SUPERSEDED; its §5 shape never
    reproduced): `tmso_reject` stops out, then §6 takes the T2 target."""
    case, rows, run_dir = c0814
    fills = _assert_first_fill(case, rows)
    assert [f["mechanism"] for f in fills] == ["tmso_reject", "fvg_1m_post_extreme"]
    assert len(_kind(rows, "stop_out")) == 1
    tp = _kind(rows, "take_profit")
    assert len(tp) == 1 and tp[0]["time"].endswith("10:59:23-04:00")
    assert _total(run_dir) == case.pnl


# --- 08-13: the worked precedent for the whole doctrine ---------------------- #

def test_0813_binds_the_5m_continuation_and_no_other_mechanism_preempts_it(r0813):
    case, rows = r0813
    fills = _kind(rows, "fill")
    assert len(fills) == 1
    assert fills[0]["mechanism"] == "fvg_return_continuation"
    assert fills[0]["artifact_label"] == case.artifact
    assert fills[0]["price"] == case.entry_price
    assert fills[0]["time"].endswith(f"{case.entry_time}-04:00")


def test_0813s_pnl_is_the_documented_figure_plus_exactly_the_buffer_trim(r0813):
    """The worked precedent: +89.25 -> +93.25, a difference of exactly 4.00 pts."""
    case, rows = r0813
    assert _pnl(rows) == case.pnl + BUFFER_TRIM
    assert nc.ENTRY_BUFFER_TRIM_PTS == BUFFER_TRIM


def test_0813_reaches_its_dol_at_the_documented_second(r0813):
    case, rows = r0813
    tp = _kind(rows, "take_profit")
    assert len(tp) == 1 and tp[0]["time"].endswith(f"{case.exit_time}-04:00")
    assert tp[0]["price"] == case.exit_price


# --- the shared counter ------------------------------------------------------ #

def test_the_shared_counter_is_consumed_across_DIFFERENT_mechanisms():
    """The budget is per `plan_id`, not per mechanism. Asserted on the Arbiter, because
    no replayed day in this set spends attempts across two mechanisms — which is itself
    worth knowing, and is why this is a unit assertion and not a day one."""
    from agent.trader.arbiter import Arbiter
    a = Arbiter()
    a.spend("fvg_return_continuation")
    a.spend("extreme_reject_close")
    a.spend("fvg_1m_post_extreme")
    assert a.exhausted() and a.attempts_remaining == 0
    assert set(a.spent_by()) == {"fvg_return_continuation", "extreme_reject_close",
                                 "fvg_1m_post_extreme"}


def test_attempts_are_asserted_against_the_documented_count_and_never_scored(
        r0813, c0811, c0812, c0813, c0814):
    """Attempts contribute NOTHING to the P&L arithmetic. A row reproducing the right
    P&L via a different attempt count is a real discrepancy. An attempt is a FILL — a
    position marked at the window end (08-12) spends one exactly like a closed one."""
    runs = [r0813] + [(c, r) for c, r, _ in (c0811, c0812, c0813, c0814)]
    for case, rows in runs:
        spent = len(_kind(rows, "fill"))
        assert spent == case.attempts_used, (
            f"{case.key}: documented {case.attempts_used} attempts, replay spent {spent}")
