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
"""
import json
import os

import pytest

from agent.trader import named_cases as nc
from agent.trader.replay import run_replay

pytestmark = pytest.mark.timeout(1800)

#: Every delta below resolves into these two adoptions and nothing else.
BUFFER_TRIM = 4.00                # entry buffer 7 -> 3
SL_CAP_SAVING_0812 = 6.00         # structural -31.00 -> capped -25.00


def _run(key):
    case = nc.by_key(key)
    thesis = nc.thesis_for(key)
    assert thesis is not None, f"{key} has no oracle thesis"
    res = run_replay([case.date], allow_calls=False, thesis=thesis)
    path = os.path.join(res[case.date]["run_dir"], "trader_decisions.jsonl")
    rows = ([json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
            if os.path.exists(path) else [])
    return case, rows


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
def r0811():
    return _run("sec10-0811-no-entry")


@pytest.fixture(scope="module")
def r0812():
    return _run("sec10-0812")


@pytest.fixture(scope="module")
def r0813():
    return _run("sec10-0813")


@pytest.fixture(scope="module")
def r0814():
    return _run("sec10-0814")


# --- 08-11: the accepted skip ------------------------------------------------ #

def test_0811_is_a_no_entry_day_with_every_mechanism_armed(r0811):
    """Open drive with no retrace: no adverse day extreme ever printed (§6/§7 cannot
    arm), and §5's correct binding was only ever entered at or before the L1 arm."""
    _case, rows = r0811
    assert _kind(rows, "fill", "intended_entry") == []
    assert _pnl(rows) == 0.0


def test_0811_binds_the_documented_artifact_and_then_declines_it(r0811):
    """The BOUND ARTIFACT, not merely the absence of a trade: a day that took nothing
    because it bound nothing would look identical and be a different failure."""
    _case, rows = r0811
    binds = _kind(rows, "bind")
    assert binds, "the day must still BIND [29835.00, 29855.25]"
    assert "[29835.0, 29855.25]" in binds[0]["artifact_label"], binds[0]["artifact_label"]
    assert [r["reason"] for r in _kind(rows, "veto")] == ["max_distance"]


def test_0811_dies_at_its_dol(r0811):
    dead = _kind(rows := r0811[1], "plan_dead")
    assert dead and dead[0]["reason"] == "dol_reached"
    assert _kind(rows, "stop_out") == []


# --- 08-12: the veto discriminating between candidate bindings --------------- #

def test_0812_spends_two_attempts_and_the_dol_floor_vetoes_the_first_fallback_candidate(
        r0812):
    """§10's shape, exactly: §4 negation stops out, the bound gap goes close-through
    dead, the widened §4-1m fallback arms, its FIRST candidate is vetoed and its SECOND
    is allowed. First forward instance of a veto discriminating between candidates."""
    _case, rows = r0812
    fills = _kind(rows, "fill")
    assert len(fills) == 2
    assert len(_kind(rows, "stop_out")) == 1
    assert [f["mechanism"] for f in fills] == ["fvg_negation_reversal"] * 2
    labels = [f["artifact_label"] for f in fills]
    assert "5min" in labels[0] and "1min" in labels[1], labels
    vetoes = _kind(rows, "veto")
    assert vetoes, "the first fallback candidate must be vetoed"
    assert vetoes[0]["artifact_label"].startswith("MNQ 1min")


def test_0812s_entry_prices_are_the_documented_ones_shifted_by_the_buffer_trim(r0812):
    """EXPLAINED DELTA. §10 records short 29933 then 29920.5 at buffer 7. At buffer 3 the
    triggers sit 4.00 pts deeper into each gap: 29937.0 and 29924.5. Nothing else moved —
    the gaps, their order and the veto in between are identical."""
    _case, rows = r0812
    fills = _kind(rows, "fill")
    assert fills[0]["price"] == 29933.0 + BUFFER_TRIM
    assert fills[1]["price"] == 29920.5 + BUFFER_TRIM


def test_0812s_pnl_delta_is_fully_accounted_for(r0812):
    """+56.75 against the documented +46.75. Every one of the 10.00 pts is named: 6.00
    from the 25-pt SL cap turning attempt 1's structural -31.00 into -25.00, and 4.00
    from the buffer trim improving attempt 2's entry. An unexplained 10.00 would be a
    defect wearing the same clothes."""
    case, rows = r0812
    assert _pnl(rows) == case.pnl + SL_CAP_SAVING_0812 + BUFFER_TRIM


def test_0812s_first_fallback_veto_reason_moved_with_the_buffer_and_nothing_else(r0812):
    """A second-order delta, tracked rather than waved through. §10 records the first
    fallback candidate vetoed by the DOL FLOOR at 57.25 pts remaining. At buffer 3 the
    SAME gap's trigger sits 4.00 pts higher, which leaves 61.25 — just over the 60-pt
    floor — so the MAX-DISTANCE guard catches it instead, at 63.75. Same gap, same
    outcome (vetoed), different guard; the 4.00 pts are the whole cause."""
    _case, rows = r0812
    veto = _kind(rows, "veto")[0]
    assert veto["reason"] == "max_distance"
    assert veto["detail"]["trigger"] == 29900.0 + BUFFER_TRIM


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


# --- 08-14: the shape does NOT reproduce, and that is the finding ------------ #

def test_0814_reaches_its_dol_on_a_documented_second_but_NOT_the_documented_shape(r0814):
    """RECORDED DIVERGENCE — read this before trusting 08-14 anywhere.

    What reproduces: the plan reaches its DOL 30124.25 at **10:59:52**, the exact second
    §8's 1s-verified walk records, and the day is profitable.

    What does NOT: the documented walk is a §5 sequence — attempt-1 fill 30252.25 at
    09:30:33, stopped 09:31:05, the §8 takeover of the deeper 09:11 1m gap, market
    30245.5 at 09:32:00 by crossed-trigger precedence, then a 10:07 close-verdict entry —
    **on exactly 3 attempts**. This engine arms `fvg_negation_reversal`, not
    `fvg_return_continuation`, because §3's last-trend segmentation reads 08-14's last
    qualifying leg as UP and a DOWN thesis therefore REVERSES it. The mechanism the
    Planner arms is upstream of everything §8 does, so no takeover rule can recover the
    documented sequence from here.

    Two candidate causes, neither settled by the document: either §3's leg parameters
    differ from whatever the manual walk used, or the walk simply assumed §5 without
    re-deriving the leg. §10 records 08-14's DOL and P&L but never which mechanism armed.

    This test asserts the divergence explicitly so it cannot be mistaken for a pass.
    """
    case, rows = r0814
    tp = _kind(rows, "take_profit")
    assert tp and tp[0]["time"].endswith("10:59:52-04:00")
    assert tp[0]["price"] == 30124.25

    fills = _kind(rows, "fill")
    assert {f["mechanism"] for f in fills} == {"fvg_negation_reversal"}, \
        "if this becomes fvg_return_continuation the divergence is RESOLVED — re-pin"
    assert _pnl(rows) != case.pnl, "unexpected agreement — re-derive the whole row"


def test_0814_does_not_exhaust_the_shared_budget(r0814):
    """The documented row spends EXACTLY 3 attempts. This engine spends 1 (one stop-out
    plus the winner), so the attempt assertion CANNOT be made — attempts-used is a
    sharper change detector than P&L, and here it detects a real difference."""
    case, rows = r0814
    assert case.attempts_used == 3
    assert len(_kind(rows, "stop_out")) == 1


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
        r0811, r0812, r0813):
    """Attempts contribute NOTHING to the P&L arithmetic. A row reproducing the right
    P&L via a different attempt count is a real discrepancy."""
    for case, rows in (r0811, r0812, r0813):
        if case.attempts_used is None:
            continue
        spent = len(_kind(rows, "stop_out")) + len(_kind(rows, "take_profit"))
        assert spent == case.attempts_used, (
            f"{case.key}: documented {case.attempts_used} attempts, replay spent {spent}")
