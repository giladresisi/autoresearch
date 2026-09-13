"""§8's named takeover cases, driven over the REAL 1s tape.

§11 escalates the all-timeframe scan to an imperative because two independent manual
walks scanned only 5m and produced a wrong 07-21 ledger and a mis-bound 07-31 takeover.
These two tests are that failure, made falsifiable: they detect the day's actual FVGs on
BOTH timeframes and assert which gap the scanner binds.
"""
import pandas as pd
import pytest

from agent.facts.detectors.fvg import detect_fvgs, update_fvg_states
from agent.facts.records import FactState
from agent.trader import named_cases as nc
from agent.trader import tape
from agent.trader.takeover import crossed, deepest_penetrated, resolve_cooldown_end

TZ = "America/New_York"


def _detect(date, at, *, start="02:00"):
    """(all gaps, live gaps) on BOTH timeframes as of `at`, existence-gated."""
    every, live = [], []
    for tf in ("5min", "1min"):
        bars = tape.bars(date, tf, start=start, end="12:00")
        if not len(bars):
            continue
        facts, _ = detect_fvgs(bars, tf, "MNQ", {})
        facts = [f for f in facts if pd.Timestamp(f.extra["exists_from"]) <= at]
        update_fvg_states(facts, bars[bars.index <= at], tf)
        every += facts
        live += [f for f in facts if f.state is FactState.LIVE]
    return every, live


def _find(gaps, lo, hi):
    return next((g for g in gaps if abs(g.price_low - lo) < 0.01
                 and abs(g.price_high - hi) < 0.01), None)


def _scan(date, at_str, direction, failed_bounds, window, *, want="bull"):
    if tape.tape_1s(date) is None:
        pytest.skip(f"no 1s tape for {date}")
    at = pd.Timestamp(f"{date} {at_str}", tz=TZ)
    every, live = _detect(date, at)
    failed = _find(every, *failed_bounds)
    assert failed is not None, f"the failed binding {failed_bounds} was not detected"
    cands = [g for g in live if g.extra["direction"] == want]
    return failed, cands, deepest_penetrated(cands, failed, direction,
                                             low=window[0], high=window[1])


# --- 07-21: the named 1m takeover -------------------------------------------- #

def test_0721_the_1m_gap_takes_over_at_the_stop_tick():
    """1m gap [29137.25, 29138.5] takes over at the 09:33:58 stop tick. §10.1: the stop
    tick ITSELF penetrates it, which is why the window includes the stop-out bar."""
    case = nc.by_key("sec8-0721-takeover")
    _failed, _cands, got = _scan("2026-07-21", "09:33:58", "UP",
                                 (29143.25, 29164.0), (29135.0, 29175.0))
    assert got is not None, "no takeover found — the 1m scan is the whole point"
    assert (got.price_low, got.price_high) == (29137.25, 29138.5)
    assert got.timeframe == "1min", case.artifact


def test_0721s_takeover_gap_is_invisible_to_a_5m_only_scan():
    """The §11 regression, stated as the failure it was: filter the candidate set to 5m
    and the takeover disappears — which is exactly the wrong ledger two manual walks
    produced."""
    failed, cands, _ = _scan("2026-07-21", "09:33:58", "UP",
                             (29143.25, 29164.0), (29135.0, 29175.0))
    five_only = [g for g in cands if g.timeframe == "5min"]
    assert deepest_penetrated(five_only, failed, "UP",
                              low=29135.0, high=29175.0) is None


def test_0721s_takeover_gap_carries_its_documented_identity():
    """§11 names it 'mid-bar 08:45'. Under §2's pinned convention the IDENTITY is the
    middle bar, so that is exactly what the detector must record."""
    _failed, _cands, got = _scan("2026-07-21", "09:33:58", "UP",
                                 (29143.25, 29164.0), (29135.0, 29175.0))
    assert pd.Timestamp(got.extra["creating_bar_ts"]).strftime("%H:%M") == "08:45"


# --- 07-31: the deepest gap, not the shallower 5m ---------------------------- #

def test_0731_binds_the_deepest_penetrated_gap():
    """Binds [28455.75, 28477] (1m), not the shallower 5m [28496.75, 28508.5]."""
    case = nc.by_key("sec8-0731-deepest")
    _failed, _cands, got = _scan("2026-07-31", "09:42:02", "UP",
                                 (28514.5, 28529.25), (28450.0, 28535.0))
    assert got is not None
    assert (got.price_low, got.price_high) == (28455.75, 28477.0)
    assert got.timeframe == "1min", case.artifact


def test_0731s_takeover_identity_is_the_middle_bar_not_its_existence_instant():
    """EXPLAINED DELTA. §11 records this gap as 'created 08:53'; the detector reports its
    IDENTITY as 08:51. Both are right and they are two different instants: 08:51 is the
    MIDDLE bar (the name), 08:53 is the third bar's COMPLETION (the earliest instant it
    may be acted on). §2 pins exactly this split, and its own reading note says older
    prose uses the third bar's label as the identity."""
    _failed, _cands, got = _scan("2026-07-31", "09:42:02", "UP",
                                 (28514.5, 28529.25), (28450.0, 28535.0))
    assert pd.Timestamp(got.extra["creating_bar_ts"]).strftime("%H:%M") == "08:51"
    assert pd.Timestamp(got.extra["exists_from"]).strftime("%H:%M") == "08:53"


def test_0731s_shallower_5m_candidate_is_not_chosen():
    failed, cands, got = _scan("2026-07-31", "09:42:02", "UP",
                               (28514.5, 28529.25), (28450.0, 28535.0))
    assert got.timeframe != "5min"
    shallower = _find(cands, 28496.75, 28508.5)
    if shallower is not None:
        assert float(got.price_low) < float(shallower.price_low)


# --- close-through DEAD candidates ------------------------------------------- #

def test_a_close_through_dead_candidate_is_never_a_takeover():
    """08-12 and 08-05's verdict, asserted as the RULE rather than as their day figures:
    "deeper 1m candidates exist but are close-through DEAD -> no takeover".

    Where the exclusion lives matters. `deepest_penetrated` is pure geometry and knows
    nothing about eligibility; the close-through filter is `Executor._gaps_on`, which
    queries `FactState.LIVE`. So the claim under test is that a gap the tape closed
    through never reaches the scanner — and 07-31 supplies real dead candidates deeper
    than the failed binding, so it is not vacuous.

    The DAY-LEVEL reproduction of 08-12/08-05 is a recorded VALIDATION GAP (see
    `named_cases.NO_DAY_REPRODUCTION`): the document gives those days' outcomes but never
    the failed binding's bounds, so which gap failed cannot be recovered.
    """
    date, at_str = "2026-07-31", "09:42:02"
    if tape.tape_1s(date) is None:
        pytest.skip("no 1s tape")
    at = pd.Timestamp(f"{date} {at_str}", tz=TZ)
    every, live = _detect(date, at)
    failed = _find(every, 28514.5, 28529.25)

    dead_deeper = [g for g in every
                   if g.state is not FactState.LIVE
                   and g.extra["direction"] == "bull"
                   and float(g.price_low) < float(failed.price_low)]
    assert dead_deeper, "no dead deeper candidate on this date — re-pin this test"

    # Geometry alone WOULD bind one of them; eligibility is what keeps it out.
    assert deepest_penetrated(dead_deeper, failed, "UP",
                              low=28450.0, high=28535.0) is not None
    assert all(g.state is FactState.LIVE for g in live)
    assert not [g for g in live if g.id in {d.id for d in dead_deeper}]


def test_the_executor_scan_only_ever_sees_live_gaps(tmp_path):
    """The other half of the rule, at the seam that enforces it."""
    from agent.facts.records import Fact, FactClass
    from agent.trader.executor import Executor

    ex = Executor(str(tmp_path),
                  {"plan_id": "t", "direction": "UP", "dol": {"price": 29000.0},
                   "armed_classes": ["fvg_return_continuation"]},
                  arm_ts=pd.Timestamp("2026-07-31 09:20", tz=TZ))
    ref = pd.Timestamp("2026-07-31 08:51", tz=TZ)
    g = Fact(id="dead", cls=FactClass.FVG, ticker="MNQ", label="dead", name=None,
             reference_ts=ref, price=None, price_low=28455.75, price_high=28477.0,
             timeframe="1min", resolution="1min", state=FactState.LIVE, state_ts=ref,
             provenance={}, extra={"direction": "bull", "max_anti_excursion": 0.0,
                                   "exists_from": ref})
    ex._store.upsert(g)
    at = pd.Timestamp("2026-07-31 09:42", tz=TZ)
    assert [x.id for x in ex._takeover_candidates(at)] == ["dead"]
    g.set_state(FactState.INVALIDATED, at)
    ex._store.upsert(g)
    assert ex._takeover_candidates(at) == []


# --- crossed-trigger precedence at the cooldown end -------------------------- #

def test_0724_crossed_trigger_precedence_preserves_the_collapse_capture():
    """Two eligible deeper 1m gaps penetrated, but the crossed trigger at cooldown end
    wins -> market re-entry 28579, +146.5 preserved. The episode's SL-cap gate would have
    SKIPPED it at 45.25 pts, recreating the exact lockout the cooldown rule kills."""
    case = nc.by_key("sec8-0724-crossed")
    assert resolve_cooldown_end(failed_trigger=28598.5, price=case.entry_price,
                                direction="DOWN",
                                close_verdict={"price": 28575.0}) == \
        "crossed_trigger_market"


def test_0814_market_at_the_1s_mid_and_exactly_three_attempts():
    """Crossed trigger at 09:32:00 -> market 30245.5, SL 30268, day +99.50 on EXACTLY 3
    attempts. Assert the attempt count, do not score it."""
    case = nc.by_key("sec8-0814-three-attempts")
    assert case.attempts_used == 3
    assert crossed(30252.25, case.entry_price, "DOWN")
    assert resolve_cooldown_end(failed_trigger=30252.25, price=case.entry_price,
                                direction="DOWN") == "crossed_trigger_market"
    assert case.stop == 30268.0, "the failed binding's stop travels with the re-entry"


def test_0721_all_three_cooldown_ends_are_uncrossed():
    """07-21: 'all three cooldown ends uncrossed (price 29139.5 vs 29171)'; 07-31
    likewise. That is what lets the episode path govern on those days."""
    assert resolve_cooldown_end(failed_trigger=29171.0, price=29139.5,
                                direction="UP") == "resting"
