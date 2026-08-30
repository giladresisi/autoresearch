"""Task 5: §4 `fvg_negation_reversal` and its widened 1m fallback.

A SHORT plan expecting DOWN binds a BULLISH gap from the uptrend it is reversing. That
one line is the whole difference from §5, and getting it backwards silently turns §4 into
a second continuation mechanism.
"""
import pandas as pd
import pytest

from agent.facts.records import Fact, FactClass, FactState
from agent.trader.executor import MAX_DISTANCE_PTS, Executor

TZ = "America/New_York"
NOW = pd.Timestamp("2026-08-03 09:45", tz=TZ)


def _gap(lo, hi, *, tf="5min", direction="bull", ref="2026-08-03 09:30",
         exists=None, gid=None, excursion=0.0):
    exists = exists or ("2026-08-03 09:40" if tf == "5min" else "2026-08-03 09:32")
    return Fact(id=gid or f"{tf}-{direction}-{lo}", cls=FactClass.FVG, ticker="MNQ",
                label=f"MNQ {tf} {direction} FVG [{lo}, {hi}]", name=None,
                reference_ts=pd.Timestamp(ref, tz=TZ), price=None,
                price_low=lo, price_high=hi, timeframe=tf, resolution=tf,
                state=FactState.LIVE, state_ts=pd.Timestamp(ref, tz=TZ),
                provenance={}, extra={"direction": direction,
                                      "max_anti_excursion": excursion,
                                      "exists_from": pd.Timestamp(exists, tz=TZ)})


@pytest.fixture
def ex_short(tmp_path):
    """DOWN thesis with §4 armed. The counter-thesis direction is therefore BULL."""
    return Executor(str(tmp_path),
                    {"plan_id": "t", "direction": "DOWN",
                     "dol": {"price": 29000.0},
                     "armed_classes": ["fvg_negation_reversal"]},
                    arm_ts=pd.Timestamp("2026-08-03 09:20", tz=TZ))


PRICE = 29300.0


def _sel(ex, now=NOW, price=PRICE):
    return ex._selection_gaps(now, "fvg_negation_reversal", price)


# --- the binding ------------------------------------------------------------- #

def test_the_binding_is_a_counter_thesis_gap(ex_short):
    """Expecting down -> bind a BULLISH gap from the uptrend being reversed."""
    ex_short._store.upsert(_gap(29290.0, 29305.0, direction="bull"))
    ex_short._store.upsert(_gap(29290.0, 29305.0, direction="bear"))
    got = _sel(ex_short)
    assert {g.extra["direction"] for g in got} == {"bull"}


def test_sec5_still_binds_the_thesis_direction(ex_short):
    """The two mechanisms must not converge on the same candidate set."""
    ex_short._store.upsert(_gap(29290.0, 29305.0, direction="bull"))
    ex_short._store.upsert(_gap(29290.0, 29305.0, direction="bear"))
    got = ex_short._selection_gaps(NOW, "fvg_return_continuation", PRICE)
    assert {g.extra["direction"] for g in got} == {"bear"}


def test_the_direction_helper_states_the_rule_for_both_sides(ex_short):
    assert ex_short._selection_direction("fvg_negation_reversal") == "bull"
    assert ex_short._selection_direction("fvg_return_continuation") == "bear"


def test_the_trigger_and_stop_are_still_derived_from_the_TRADE_direction(ex_short):
    """Short 3 pts below a BULLISH gap's lower bound; stop above its top."""
    g = _gap(29290.0, 29305.0, direction="bull")
    trigger, stop = ex_short._levels_for(g)
    assert trigger == 29290.0 - 3.0
    assert stop == min(29305.0 + 3.0, trigger + 25.0)
    assert trigger < stop, "a SHORT enters below and stops above"


def test_the_most_recently_created_candidate_wins(ex_short):
    """§4 binds the most recently created eligible gap, matching §8's preference, and the
    key is EXISTENCE — so a gap created later wins even with an earlier identity.

    Asserted through `_newest`, the SELECTION rule itself. Recomputing `max(...)` inside
    the test and then asserting the result would only re-assert the test's own arithmetic:
    `_gaps_on` returns an unordered candidate SET and implements no ranking at all.
    """
    old = _gap(29290.0, 29305.0, ref="2026-08-03 09:40",
               exists="2026-08-03 09:41", gid="old")
    new = _gap(29292.0, 29307.0, ref="2026-08-03 09:31",
               exists="2026-08-03 09:42", gid="new")
    for g in (old, new):
        ex_short._store.upsert(g)
    assert ex_short._newest(_sel(ex_short)).id == "new"


def test_recency_reads_existence_not_identity(ex_short):
    """The two orderings DISAGREE here on purpose: `new` has the EARLIER identity (09:31
    vs 09:40) and the LATER existence (09:42 vs 09:41). Ranking on `reference_ts` would
    pick the other one."""
    old = _gap(29290.0, 29305.0, ref="2026-08-03 09:40",
               exists="2026-08-03 09:41", gid="old")
    new = _gap(29292.0, 29307.0, ref="2026-08-03 09:31",
               exists="2026-08-03 09:42", gid="new")
    for g in (old, new):
        ex_short._store.upsert(g)
    got = _sel(ex_short)
    assert ex_short._newest(got).id == "new"
    by_identity = max(got, key=lambda g: (g.reference_ts, g.id))
    assert by_identity.id == "old", "the two keys must disagree or this proves nothing"


def test_no_retrace_is_required(ex_short):
    """Unlike §5. §4 enters on traversal; requiring a retrace would forfeit the 07-16
    judas spike, where no 5m gap existed and the 1m gap gave short 29464.5 -> +159."""
    import inspect

    src = inspect.getsource(Executor._guard_and_place)
    assert 'mechanism == "fvg_return_continuation"' in src, \
        "the retrace precondition must be scoped to §5 alone"
    assert "fvg_negation_reversal" not in src


# --- the USABLE-5m test ------------------------------------------------------ #

def test_a_5m_gap_beyond_the_max_distance_guard_is_not_usable(ex_short):
    """Part (d) of §6's four-part test — the only momentary one."""
    far = _gap(29900.0, 29915.0, gid="far")
    ex_short._store.upsert(far)
    assert ex_short._gaps_on("5min", NOW, direction="bull") == [far]
    assert ex_short.usable_5m_gaps(NOW, PRICE, direction="bull") == []


def test_a_distance_invalidated_5m_gap_is_not_usable(ex_short):
    """Part (c): permanent, unlike (d)."""
    ex_short._store.upsert(_gap(29290.0, 29305.0, excursion=90.0))
    assert ex_short.usable_5m_gaps(NOW, PRICE, direction="bull") == []


def test_an_over_height_5m_gap_is_not_usable(ex_short):
    """Part (a): the [5, 45] band."""
    ex_short._store.upsert(_gap(29250.0, 29320.0))            # 70 pts
    assert ex_short.usable_5m_gaps(NOW, PRICE, direction="bull") == []


def test_an_inverted_5m_gap_is_not_usable(ex_short):
    """Part (b): a 5m close through it in the anti-trade direction since creation, which
    the detector records as a non-LIVE state."""
    g = _gap(29290.0, 29305.0)
    g.set_state(FactState.INVALIDATED, NOW)
    ex_short._store.upsert(g)
    assert ex_short.usable_5m_gaps(NOW, PRICE, direction="bull") == []


def test_the_retired_rr_to_dol_clause_is_not_part_of_the_usable_test():
    """R* = 0, provably inert; §11.0's retired list.

    The tokens are checked BARE, not with a trailing underscore: a rebuilt clause named
    `rr` or `reward` would slip straight past `"rr_" not in src`, which is the exact
    shape of grep that makes a retired-list guard useless.
    """
    import ast as _ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(Executor.usable_5m_gaps))
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and _ast.get_docstring(node):
            node.body = node.body[1:]
    code = _ast.unparse(tree).lower()
    for token in ("rr", "risk_reward", "reward", "rr_to_dol"):
        assert token not in code, f"the retired RR clause is back as {token!r}"


# --- the 1m fallback --------------------------------------------------------- #

def test_the_1m_fallback_arms_when_no_usable_5m_exists(ex_short):
    """The same 5m-unusable state that arms §6."""
    ex_short._store.upsert(_gap(29900.0, 29915.0, gid="far-5m"))      # beyond the guard
    ex_short._store.upsert(_gap(29290.0, 29305.0, tf="1min", gid="one"))
    got = _sel(ex_short)
    assert [g.id for g in got] == ["one"]


def test_the_1m_fallback_does_NOT_arm_while_a_usable_5m_exists(ex_short):
    ex_short._store.upsert(_gap(29290.0, 29305.0, gid="five"))
    ex_short._store.upsert(_gap(29292.0, 29307.0, tf="1min", gid="one"))
    assert [g.id for g in _sel(ex_short)] == ["five"]


def test_the_1m_fallback_requires_the_third_bar_at_or_after_0930(ex_short):
    """EXISTENCE-side, per §2. A gap whose third bar completes 09:29 does NOT qualify."""
    ex_short._store.upsert(_gap(29290.0, 29305.0, tf="1min",
                                ref="2026-08-03 09:27",
                                exists="2026-08-03 09:29", gid="pre-open"))
    assert _sel(ex_short) == []


def test_the_1m_fallback_admits_a_gap_whose_earlier_bars_are_pre_open(ex_short):
    """Load-bearing on 08-03, whose rescue gap builds on the 09:29 bar: identified 09:28,
    third bar completes 09:30 — the pattern's earlier bars are pre-open and it still
    qualifies."""
    ex_short._store.upsert(_gap(29290.0, 29305.0, tf="1min",
                                ref="2026-08-03 09:28",
                                exists="2026-08-03 09:30", gid="rescue"))
    assert [g.id for g in _sel(ex_short)] == ["rescue"]


def test_the_boundary_reads_existence_not_identity(ex_short):
    """A gap identified 09:29 exists at 09:31 and qualifies. Reading the identity here
    would silently change which rescue gaps are available."""
    ex_short._store.upsert(_gap(29290.0, 29305.0, tf="1min",
                                ref="2026-08-03 09:29",
                                exists="2026-08-03 09:31", gid="late"))
    assert [g.id for g in _sel(ex_short)] == ["late"]


def test_distance_invalidation_does_not_extend_to_1m_bound_gaps(ex_short):
    """§4: close-through eligibility only, for now."""
    ex_short._store.upsert(_gap(29290.0, 29305.0, tf="1min", excursion=90.0,
                                gid="one"))
    assert [g.id for g in _sel(ex_short)] == ["one"]


def test_the_1m_fallback_still_honours_the_counter_thesis_direction(ex_short):
    ex_short._store.upsert(_gap(29290.0, 29305.0, tf="1min", direction="bear",
                                gid="wrong-way"))
    assert _sel(ex_short) == []


def test_a_1m_negation_stop_may_coexist_with_an_armed_sec6(ex_short):
    """Single-stop-entry policy and first-trigger-wins govern; the 3-attempt counter is
    SHARED per plan across all mechanisms."""
    from agent.trader.arbiter import Arbiter, Candidate
    ex_short._store.upsert(_gap(29900.0, 29915.0, gid="far-5m"))
    ex_short._store.upsert(_gap(29290.0, 29305.0, tf="1min", gid="one"))
    got = _sel(ex_short)
    assert [g.id for g in got] == ["one"]
    # The same 5m-unusable state arms §6.
    assert Arbiter.sec6_armed(
        usable_5m_gaps=len(ex_short.usable_5m_gaps(NOW, PRICE, direction="bull")))
    resting = Candidate(mechanism="fvg_negation_reversal", kind="stop",
                        trigger=29287.0, timeframe="1min")
    assert Arbiter.may_coexist(
        Candidate(mechanism="fvg_1m_post_extreme", kind="market"), resting)


def test_the_max_distance_guard_is_the_spec_value():
    assert MAX_DISTANCE_PTS == 60.0
