"""Plan 35 — the pre-boundary SESSION STRETCH as an L1 direction input.

D1 (`derive_facts._session_stretch` + `FactsBundle.session_stretch`), D2 (the S9
`PRE-BOUNDARY SESSION STRETCH` block) and D3 (code-derived P1/P2 tier in
`validate_contracts.score_thesis_evidence`).

The stretch is the CME-session move INTO the boundary: from the session open (18:00 ET
the prior day) to `now`, take the high and the low; whichever came LAST defines the
direction, and `age_minutes` is how long before the boundary that extreme formed. The
measured signal is RECENCY, not size — see `_session_stretch`'s own docstring for the
94-session numbers.

Test numbering follows the plan's own enumerated list (cases 1-10). Case 11 is added on
top: it pins the ONE real tier mis-declaration the D3 investigation actually found.
"""

import os
import sys

import pandas as pd
import pytest

import derive_facts
from derive_facts import TZ, _session_stretch

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "contracts"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from validate_contracts import score_thesis_evidence  # noqa: E402

BOUNDARY = "2026-09-01 09:20"


def _frame(rows):
    """A minimal normalised bar frame: rows of (ts, open, high, low, close), ET."""
    idx = pd.DatetimeIndex([pd.Timestamp(r[0], tz=TZ) for r in rows])
    return pd.DataFrame(
        {"open": [r[1] for r in rows], "high": [r[2] for r in rows],
         "low": [r[3] for r in rows], "close": [r[4] for r in rows]}, index=idx)


# --------------------------------------------------------------------------- #
# D1 — the derived fact                                                        #
# --------------------------------------------------------------------------- #

def test_1_direction_is_the_side_whose_extreme_came_last():
    """A session that made its high at 02:00 and its low at 09:18 is a DOWN stretch —
    the direction is the side of the LAST-formed extreme, not the larger excursion."""
    sess = _frame([
        ("2026-08-31 18:00", 100.0, 100.0, 100.0, 100.0),
        ("2026-09-01 02:00", 100.0, 120.0, 100.0, 118.0),   # high came FIRST
        ("2026-09-01 09:18", 118.0, 118.0, 90.0, 92.0),     # low came LAST
    ])
    st = _session_stretch(sess, pd.Timestamp("2026-09-01 09:19:59", tz=TZ), 92.0)
    assert st["direction"] == "DOWN"
    assert st["extreme_price"] == 90.0
    assert st["origin_price"] == 120.0
    assert st["size"] == 30.0

    # The mirror image is an UP stretch, on the same frame shape.
    sess_up = _frame([
        ("2026-08-31 18:00", 100.0, 100.0, 100.0, 100.0),
        ("2026-09-01 02:00", 100.0, 100.0, 90.0, 92.0),     # low came FIRST
        ("2026-09-01 09:18", 92.0, 120.0, 92.0, 118.0),     # high came LAST
    ])
    st_up = _session_stretch(sess_up, pd.Timestamp("2026-09-01 09:19:59", tz=TZ), 118.0)
    assert st_up["direction"] == "UP"
    assert st_up["extreme_price"] == 120.0


def test_1b_both_extremes_in_one_bar_break_the_tie_on_that_bars_own_body():
    """Neither extreme "came last" — so the move into the boundary is whatever that one
    bar did, not whichever of idxmax/idxmin happened to be evaluated first."""
    now = pd.Timestamp("2026-09-01 09:19:59", tz=TZ)
    up_body = _frame([("2026-08-31 18:00", 100.0, 100.0, 100.0, 100.0),
                      ("2026-09-01 09:18", 95.0, 120.0, 90.0, 110.0)])   # close > open
    assert _session_stretch(up_body, now, 110.0)["direction"] == "UP"
    down_body = _frame([("2026-08-31 18:00", 100.0, 100.0, 100.0, 100.0),
                        ("2026-09-01 09:18", 110.0, 120.0, 90.0, 95.0)])  # close < open
    assert _session_stretch(down_body, now, 95.0)["direction"] == "DOWN"

    # A duplicate index label must not blow up on the `sess.loc[ts]` DataFrame it returns.
    dup = _frame([("2026-09-01 09:18", 95.0, 120.0, 100.0, 110.0),
                  ("2026-09-01 09:18", 100.0, 110.0, 90.0, 96.0)])   # last row: down-body
    assert _session_stretch(dup, now, 96.0)["direction"] == "DOWN"


def test_2_age_minutes_is_measured_to_the_boundary_not_the_session_open():
    """`age_minutes` is boundary-minus-extreme. The extreme below formed 15h18m after the
    session open and ~2 min before the boundary; the fact must carry the latter."""
    sess = _frame([
        ("2026-08-31 18:00", 100.0, 120.0, 100.0, 101.0),   # high, at the session open
        ("2026-09-01 09:18:30", 101.0, 101.0, 90.0, 92.0),  # low, 2 min before 09:20
    ])
    now = pd.Timestamp("2026-09-01 09:19:59", tz=TZ)
    st = _session_stretch(sess, now, 92.0)
    assert st["age_minutes"] == 2
    minutes_since_session_open = (now - sess.index[0]).total_seconds() / 60.0
    assert minutes_since_session_open > 900          # 15h18m — the wrong answer
    assert st["age_minutes"] != int(minutes_since_session_open)


def test_3_retrace_pct_is_zero_at_the_extreme_and_100_at_the_opposite_end():
    sess = _frame([
        ("2026-08-31 18:00", 100.0, 120.0, 100.0, 101.0),
        ("2026-09-01 09:18", 101.0, 101.0, 90.0, 92.0),
    ])
    now = pd.Timestamp("2026-09-01 09:19:59", tz=TZ)
    # DOWN stretch 120 -> 90. Price still AT the low: nothing retraced.
    assert _session_stretch(sess, now, 90.0)["retrace_pct"] == 0.0
    # Price back at the origin high: fully retraced.
    assert _session_stretch(sess, now, 120.0)["retrace_pct"] == 100.0
    # Halfway back is the mid.
    st = _session_stretch(sess, now, 105.0)
    assert st["retrace_pct"] == 50.0
    assert st["mid"] == 105.0


def test_4_beyond_mid_is_true_while_price_is_still_past_the_mid_on_the_stretch_side():
    sess_down = _frame([
        ("2026-08-31 18:00", 100.0, 120.0, 100.0, 101.0),
        ("2026-09-01 09:18", 101.0, 101.0, 90.0, 92.0),
    ])
    now = pd.Timestamp("2026-09-01 09:19:59", tz=TZ)
    # DOWN stretch, mid 105: below the mid is "still past it on the stretch's own side".
    assert _session_stretch(sess_down, now, 92.0)["beyond_mid"] is True
    assert _session_stretch(sess_down, now, 110.0)["beyond_mid"] is False
    # AT the mid is not past it.
    assert _session_stretch(sess_down, now, 105.0)["beyond_mid"] is False

    sess_up = _frame([
        ("2026-08-31 18:00", 100.0, 100.0, 90.0, 91.0),
        ("2026-09-01 09:18", 91.0, 120.0, 91.0, 118.0),
    ])
    # UP stretch 90 -> 120, mid 105: the side that counts is now ABOVE.
    assert _session_stretch(sess_up, now, 118.0)["beyond_mid"] is True
    assert _session_stretch(sess_up, now, 92.0)["beyond_mid"] is False


def test_5_a_monotone_session_yields_age_zero_and_no_retrace():
    """One straight move: the extreme IS the last bar, so nothing has aged and nothing
    has come back."""
    sess = _frame([
        ("2026-08-31 18:00", 100.0, 100.5, 100.0, 100.5),
        ("2026-09-01 04:00", 100.5, 110.0, 100.5, 110.0),
        ("2026-09-01 09:19:59", 110.0, 120.0, 110.0, 120.0),
    ])
    st = _session_stretch(sess, pd.Timestamp("2026-09-01 09:19:59", tz=TZ), 120.0)
    assert st["direction"] == "UP"
    assert st["age_minutes"] == 0
    assert st["retrace_pct"] == 0.0
    assert st["beyond_mid"] is True


def test_6_degraded_input_yields_no_stretch_block_rather_than_raising():
    now = pd.Timestamp("2026-09-01 09:19:59", tz=TZ)
    empty = _frame([]).astype(float)
    assert _session_stretch(empty, now, 100.0) is None
    assert _session_stretch(None, now, 100.0) is None
    # A flat session has no range to read a direction from.
    flat = _frame([("2026-08-31 18:00", 100.0, 100.0, 100.0, 100.0),
                   ("2026-09-01 09:18", 100.0, 100.0, 100.0, 100.0)])
    assert _session_stretch(flat, now, 100.0) is None
    # A missing or non-finite boundary price is degraded input too — asserted on a
    # PERFECTLY GOOD frame, so only the price guard can be what returns None (a NaN
    # price would otherwise render "retraced nan% off the extreme" into the L1 prompt).
    good = _frame([("2026-08-31 18:00", 100.0, 120.0, 100.0, 101.0),
                   ("2026-09-01 09:18", 101.0, 101.0, 90.0, 92.0)])
    assert _session_stretch(good, now, 92.0) is not None      # the frame itself is fine
    assert _session_stretch(good, now, None) is None
    assert _session_stretch(good, now, float("nan")) is None

    # A bundle with no stretch at all must not break any consumer that reads the field.
    bundle = derive_facts.FactsBundle()
    bundle.now = now
    assert (bundle.session_stretch or {}).get("MNQ") is None


# --------------------------------------------------------------------------- #
# D1 on the real 2026-09-01 tape                                            #
# --------------------------------------------------------------------------- #

try:                                                   # pragma: no cover - import guard
    from agent.bench.facts import DEFAULT_MAIN, ParquetFactsSource, bundle_for_boundary
except Exception:                                      # pragma: no cover
    DEFAULT_MAIN = ParquetFactsSource = bundle_for_boundary = None

_needs_tape = pytest.mark.skipif(
    ParquetFactsSource is None or not os.path.isdir(DEFAULT_MAIN),
    reason="machine-local main parquets not available")


@pytest.fixture(scope="module")
def source():
    return ParquetFactsSource()


def _bundle(source, date, hhmm="09:20"):
    raw_1s, norm_1s, _ = source.frames_for(date)
    bundle, _prim = bundle_for_boundary(
        raw_1s, norm_1s, pd.Timestamp(f"{date} {hhmm}", tz=TZ))
    return bundle


@pytest.fixture(scope="module")
def bundle_0901(source):
    return _bundle(source, "2026-09-01")


@pytest.fixture(scope="module")
def bundle_0904(source):
    """2026-09-04 is the one date in the 22 recorded boundaries where a declared tier and
    the code-derived one diverge, so both D3 tape tests share this build."""
    return _bundle(source, "2026-09-04")


@_needs_tape
@pytest.mark.timeout(300)
def test_7_the_2026_09_01_mnq_stretch_reproduces_the_hand_checked_values(bundle_0901):
    """The worked case the plan is built on: L1 called DOWN on twelve unanimous DOWN
    evidence items two minutes after the session low printed, and the tape then ran
    277.25 pts UP off the 09:41:36 low."""
    st = bundle_0901.session_stretch["MNQ"]
    assert st["direction"] == "DOWN"
    assert st["size"] == 488.25
    assert st["extreme_price"] == 29082.75
    assert st["extreme_ts"] == pd.Timestamp("2026-09-01 09:18:30", tz=TZ)
    assert st["origin_price"] == 29571.0
    assert st["origin_ts"] == pd.Timestamp("2026-09-01 01:57:55", tz=TZ)
    assert st["age_minutes"] == 2
    assert st["mid"] == 29326.88
    assert st["beyond_mid"] is True


@_needs_tape
@pytest.mark.timeout(300)
def test_8_mes_stretch_is_computed_independently_of_mnqs(bundle_0901):
    """MES's own stretch on 2026-09-01 is 66.50 pts against MNQ's 488.25 — an order of
    magnitude apart. The two assets are read independently, never collapsed into one.

    Plan 35's D2 renderer is REVERTED (see plan 37): a context-only block contributes
    0.000 to the evidence net score, so it can only push a call toward NEUTRAL and can
    never produce the opposite direction — and rendering it re-keyed every recorded
    thesis. The FACT it fed is kept; plan 37's deterministic override consumes it."""
    mes = bundle_0901.session_stretch["MES"]
    assert mes["size"] == 66.5
    assert mes["direction"] == "DOWN"
    assert mes["extreme_price"] == 7641.75
    assert mes["extreme_ts"] == pd.Timestamp("2026-09-01 09:05:16", tz=TZ)
    assert mes["origin_ts"] == pd.Timestamp("2026-08-31 20:22:16", tz=TZ)
    # Independently computed: MES's extreme is 13 minutes older than MNQ's.
    assert mes["age_minutes"] == 15
    assert mes["age_minutes"] != bundle_0901.session_stretch["MNQ"]["age_minutes"]



# --------------------------------------------------------------------------- #
# D3 — a P1 level's tier is code-derived, never model-declared                 #
# --------------------------------------------------------------------------- #
#
# What the investigation found, in three parts.
#
# 1. The plan's motivating observation (`london(cur)_low` tagged week/week/session on
#    2026-09-01/09-02/09-04) is NOT a defect: all three tags match that date's own
#    promotion map exactly — the level WAS the running week extreme on the first two days
#    and was unpromoted on the third (thesis.md §2.1 running-extreme promotion).
#
# 2. The tier on a DECLARED evidence item was indeed model-declared, and
#    `score_thesis_evidence` fed it straight into `_TIER_MULT` — and into the two
#    dominance pre-passes, the tf-dedup key and `_is_week_confluent`. That is the seam
#    the plan named, and the pre-pass now normalises it from `level_tiers`.
#
# 3. But on BOTH production call sites the 2026-08-02 auto-derivation is armed
#    (`level_htf_close_status` is supplied alongside `level_tiers`), and it already drops
#    every declared P1/P2 at a known level and re-injects it with the code tier. So the
#    hole was already shut on the live path; the pre-pass is defence in depth for callers
#    that pass `level_tiers` alone. `test_12` pins that production shape directly.
#
# P2 is deliberately NOT re-tiered — see the pre-pass comment in validate_contracts.py and
# `test_11`.

_TIERS_SESSION = {"MNQ": {"london(cur)_low": {"tier": "session", "price": 29000.0}}}
_TIERS_WEEK = {"MNQ": {"london(cur)_low": {"tier": "week", "price": 29000.0}}}


def _p1(level="london(cur)_low", tier="session", asset="MNQ"):
    return {"criterion": "P1", "asset": asset, "level": level, "tier": tier,
            "tf": "1h", "direction": "reject", "mature": True}


def test_9_london_cur_low_scores_at_the_tier_the_facts_declare_not_the_model():
    """An unpromoted `london(cur)_low` is a session-tier level. A model that declares it
    `week` must not buy itself week-tier weight for it."""
    declared_week = score_thesis_evidence([_p1(tier="week")], level_tiers=_TIERS_SESSION)
    honest = score_thesis_evidence([_p1(tier="session")], level_tiers=_TIERS_SESSION)
    assert declared_week["scored_evidence"][0]["tier"] == "session"
    assert declared_week["scored_evidence"][0]["tier_declared"] == "week"
    assert declared_week["net_score"] == honest["net_score"]
    # A rejected LOW sweep is bullish (thesis.md §2.1 P1), scored at session tier:
    # 2.0 base x 1.0 (1h) x 0.5 (session) = 1.0.
    assert declared_week["scored_evidence"][0]["points"] == 1.0

    # The converse holds too: where the facts DO say week (the level is the running week
    # extreme), an under-declaring model gets the week-tier weight it was owed.
    under = score_thesis_evidence([_p1(tier="session")], level_tiers=_TIERS_WEEK)
    assert under["scored_evidence"][0]["tier"] == "week"
    assert under["scored_evidence"][0]["points"] == 2.0

    # No facts at all -> the declared tier stands, byte-identical to before D3.
    bare = score_thesis_evidence([_p1(tier="week")])
    assert bare["scored_evidence"][0]["tier"] == "week"
    assert "tier_declared" not in bare["scored_evidence"][0]


@_needs_tape
@pytest.mark.timeout(300)
def test_10_a_levels_tier_is_a_fact_stable_across_two_boundaries_on_different_days(
        source, bundle_0904):
    """The plan asked for "stable across two boundaries on different days". Stated that
    literally it is false BY DESIGN: thesis.md §2.1's running-extreme promotion moves a
    session-tier level to day/week tier on the days it IS the running extreme, so the tier
    legitimately differs between days. What must hold — and now does — is that the tier is
    a deterministic function of the FACTS rather than of the model's declaration: two days
    on which `london(cur)_low` is unpromoted score the identical declared item identically,
    whatever the model called it.

    2026-09-04 and 2026-09-07 are both such days (verified against each date's own
    `promoted_session_levels`)."""
    from agent.bench.facts import bundle_to_l1_view

    scores = []
    for date in ("2026-09-04", "2026-09-07"):
        bundle = bundle_0904 if date == "2026-09-04" else _bundle(source, date)
        assert "london(cur)_low" not in (
            bundle.promoted_session_levels.get("MNQ") or {}), date
        tiers = bundle_to_l1_view(bundle)[0]["level_tiers"]
        assert tiers["MNQ"]["london(cur)_low"]["tier"] == "session", date
        # The model declares a DIFFERENT tier on each day; the score must not notice.
        declared = "week" if date == "2026-09-04" else "day"
        scoring = score_thesis_evidence([_p1(tier=declared)], level_tiers=tiers)
        assert scoring["scored_evidence"][0]["tier"] == "session", date
        scores.append(scoring["net_score"])
    assert scores[0] == scores[1]


@_needs_tape
@pytest.mark.timeout(300)
def test_11_a_p2_keeps_the_conservative_smt_tier_and_is_never_retiered(bundle_0904):
    """P2 must NOT be re-tiered from `level_tiers`.

    A P2/SMT candidate's tier is a PAIR-WISE fact, not a per-asset one: derive_facts
    promotes a session-tier candidate only when the level is the running extreme for BOTH
    assets, and resolves a mixed day/week qualification conservatively to `day`.
    2026-09-04 MNQ `london(cur)_high` is exactly that case — MNQ promoted to week, MES only
    to day, so the SMT candidate is `day` while `level_tiers` says `week`. Re-tiering it
    would silently reverse that rule (and cascade through P1/P2-dominates-P3), so the
    pre-pass is scoped to P1."""
    from agent.bench.facts import bundle_to_l1_view

    assert bundle_0904.promoted_session_levels["MNQ"]["london(cur)_high"] == "week"
    assert bundle_0904.promoted_session_levels["MES"]["london(cur)_high"] == "day"
    vd = bundle_to_l1_view(bundle_0904)[0]
    assert vd["level_tiers"]["MNQ"]["london(cur)_high"]["tier"] == "week"
    cand = next(c for c in vd["smt_candidates"]
                if c["level"] == "london(cur)_high" and c["swept_ticker"] == "MNQ")
    assert cand["tier"] == "day"          # the conservative pair-wise resolution

    item = {"criterion": "P2", "asset": "MNQ", "level": "london(cur)_high",
            "tier": "day", "tf": "1h", "direction": "reject", "mature": True}
    scored = score_thesis_evidence(
        [item], level_tiers=vd["level_tiers"])["scored_evidence"][0]
    assert scored["tier"] == "day"
    assert "tier_declared" not in scored
    # A rejected HIGH sweep is bearish: 2.0 x 1.0 (1h) x 0.75 (day) = 1.5, DOWN.
    assert scored["points"] == 1.5


@_needs_tape
@pytest.mark.timeout(300)
def test_12_on_the_production_call_shape_the_declared_tier_cannot_move_the_score(
        bundle_0904):
    """The property D3 exists to guarantee, asserted on the shape production actually uses
    (`level_htf_close_status` supplied alongside `level_tiers`, which arms the 2026-08-02
    auto-derivation): a deliberately mis-tiered declared ledger scores identically to an
    empty one. 2026-09-04 is the date where a declared tier and the code tier diverge, so
    if the declaration could leak into the score, it would leak here."""
    from agent.bench.facts import bundle_to_l1_view

    vd = bundle_to_l1_view(bundle_0904)[0]
    kw = dict(level_tiers=vd["level_tiers"],
              level_htf_close_status=vd["level_htf_close_status"],
              smt_candidates=vd["smt_candidates"],
              suppressed_p1_levels=vd.get("suppressed_p1_levels"),
              suppressed_p2_sites=vd.get("suppressed_p2_sites"),
              week_extremes=vd.get("week_extremes"),
              now_price=vd.get("now_price"),
              mid_position=vd.get("mid_position"),
              p1_stale_levels=vd.get("p1_stale_levels"),
              mid_reclaim=vd.get("mid_reclaim"),
              htf_reversal=vd.get("htf_reversal"))

    baseline = score_thesis_evidence([], **kw)
    mis_declared = score_thesis_evidence([
        {"criterion": "P1", "asset": "MNQ", "level": "london(cur)_low",
         "tier": "week", "tf": "1h", "direction": "reject", "mature": True},
        {"criterion": "P2", "asset": "MNQ", "level": "london(cur)_high",
         "tier": "session", "tf": "1h", "direction": "reject", "mature": True},
    ], **kw)
    assert mis_declared["net_score"] == baseline["net_score"]
    assert mis_declared["expected_bias"] == baseline["expected_bias"]
    # ... and every scored item carries a tier the FACTS produced, never a declared one.
    assert all("tier_declared" not in e for e in mis_declared["scored_evidence"])
