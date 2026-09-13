"""Plan 31's baselines on plan 33's basis, and §6's metrics.

The behavioural tests use the same synthetic tape as `test_position_policy.py`, so the
difference between B-tight and clause 5 is visible directly: B-tight moves the stop TO a
far edge, the policy moves it 3 pts beyond the PREVIOUS one.
"""
import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study import policy_baselines as pb                     # noqa: E402
from agent.study.test_position_policy import _bars, ROWS, DATE, FILL_TS  # noqa: E402

TZ = "America/New_York"
FILL = 95.0


def _tight(**kw):
    base = dict(date=DATE, direction="UP", fill_ts=FILL_TS, fill_price=FILL,
                close_et=pb.POLICY_CLOSE_ET)
    base.update(kw)
    bars = base.pop("bars", None)
    return pb.b_tight(_bars(ROWS) if bars is None else bars, **base)


def _loose(**kw):
    base = dict(date=DATE, direction="UP", fill_ts=FILL_TS, fill_price=FILL,
                close_et=pb.POLICY_CLOSE_ET)
    base.update(kw)
    bars = base.pop("bars", None)
    return pb.b_loose(_bars(ROWS) if bars is None else bars, **base)


# --------------------------------------------------------------------------- #
# the two baselines                                                             #
# --------------------------------------------------------------------------- #

def test_the_initial_stop_is_productions_stop_cap_and_not_a_number_invented_here():
    from agent.trader.executor import STOP_CAP_PTS
    assert pb.initial_stop(100.0, "UP") == pytest.approx(100.0 - STOP_CAP_PTS)
    assert pb.initial_stop(100.0, "DOWN") == pytest.approx(100.0 + STOP_CAP_PTS)


def test_b_tight_moves_the_stop_TO_the_far_edge_with_no_buffer():
    """Plan 31 §3.3 against plan 33's clause 5: different rules, and B-tight must not
    quietly acquire the policy's 3-pt buffer or the comparison measures nothing."""
    from agent.study import position_policy as pp

    run = _tight()
    gaps = pp.continuation_fvgs(_bars(ROWS), "UP", FILL_TS)
    edges = [pp._far_edge(g, True) for g in gaps]          # 102, 113, 124
    # the standing stop starts at 95 - 25 = 70, so every edge tightens
    assert run.n_moves == 3
    assert edges == [102.0, 113.0, 124.0]

    policy = pp.run_policy(_bars(ROWS), date=DATE, track="t", direction="UP",
                           fill_ts=FILL_TS, fill_price=FILL, initial_stop=70.0,
                           dol=200.0, initial_target=125.0)
    trail = [m.price for m in policy.moves if m.clause == 5]
    assert trail == [pytest.approx(102.0 - 3.0), pytest.approx(113.0 - 3.0)]


def test_b_tight_never_loosens():
    rows = list(ROWS)
    run = _tight(bars=_bars(rows))
    assert run.n_moves >= 1
    assert run.exit_reason in (pb.EXIT_STOP, pb.EXIT_MARK)


def test_b_loose_never_moves_its_stop():
    run = _loose()
    assert run.n_moves == 0
    assert run.initial_stop == pytest.approx(FILL - 25.0)


def test_a_position_that_never_stops_is_marked_and_a_mark_is_not_an_exit():
    """Plan 31 §3.1. The distinction is what made an earlier 08-24 read look wrong."""
    run = _loose()
    assert run.exit_reason == pb.EXIT_MARK
    assert run.is_mark is True
    assert run.to_dict()["is_mark"] is True


def test_b_tight_stops_where_b_loose_marks_on_the_same_tape():
    """The two bracket the answer, which is why plan 31 §3 reports both."""
    rows = list(ROWS)
    rows[10] = ("09:45", 131, 131, 100, 104)     # a deep wick after the trail tightened
    tight, loose = _tight(bars=_bars(rows)), _loose(bars=_bars(rows))
    assert tight.exit_reason == pb.EXIT_STOP
    assert loose.exit_reason == pb.EXIT_MARK
    assert tight.pnl != loose.pnl


def test_b_loose_is_stop_only_by_default_and_that_is_NOT_productions_behaviour():
    """Plan 31 §3.4 is the initial stop never moved, with no take-profit. Production's
    `OrderSim` DOES take profit at the plan's DOL, so stop-only B-loose runs past it —
    booking MORE than production would on exactly the trending sessions where a trail is
    meant to win. The default stays §3.4; `dol=` is the production analogue."""
    rows = [("09:35", 100, 101, 99, 100),
            ("09:36", 100, 150, 99, 149),      # blows through a DOL at 120
            ("09:37", 149, 151, 148, 150)]
    plain = _loose(bars=_bars(rows))
    prod = pb.b_loose(_bars(rows), date=DATE, direction="UP", fill_ts=FILL_TS,
                      fill_price=FILL, close_et=pb.POLICY_CLOSE_ET, dol=120.0)

    assert plain.exit_reason == pb.EXIT_MARK
    assert plain.pnl == pytest.approx(150.0 - FILL)        # ran past the DOL
    assert prod.exit_reason == pb.EXIT_DOL
    assert prod.pnl == pytest.approx(120.0 - FILL)         # capped at it
    assert plain.pnl > prod.pnl                            # the direction of the bias
    assert prod.name == "B-loose+DOL" and plain.name == "B-loose"


def test_the_dol_variant_still_books_the_stop_first_on_a_bar_reaching_both():
    rows = [("09:35", 100, 101, 99, 100), ("09:36", 100, 150, 60, 100)]
    run = pb.b_loose(_bars(rows), date=DATE, direction="UP", fill_ts=FILL_TS,
                     fill_price=FILL, close_et=pb.POLICY_CLOSE_ET, dol=120.0)
    assert run.exit_reason == pb.EXIT_STOP
    assert run.exit_price == pytest.approx(FILL - 25.0)


def test_the_baseline_never_references_a_candidate_exit():
    """Plan 31 §2, asserted at the signature: there is nowhere to pass one."""
    import inspect
    for fn in (pb.b_tight, pb.b_loose):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"exit_ts", "exit_price", "candidate", "policy", "run"})


def test_the_window_is_an_argument_and_the_result_records_which_was_used():
    """A 16:00 MARK quoted against a 13:00 policy exit is the same category error as
    quoting a MARK as an exit, so the window travels with the number."""
    assert pb.PLAN31_CLOSE_ET == (16, 0)
    assert pb.POLICY_CLOSE_ET == (13, 0)
    assert _loose().to_dict()["close_et"] == [13, 0]
    assert _loose(close_et=pb.PLAN31_CLOSE_ET).to_dict()["close_et"] == [16, 0]


def test_the_touch_basis_is_recorded(monkeypatch):
    bars = _bars(ROWS)
    assert _loose().touch_basis == "1m"
    assert pb.b_loose(bars, bars, date=DATE, direction="UP", fill_ts=FILL_TS,
                      fill_price=FILL).touch_basis == "1s"


def test_both_baselines_mirror_for_a_short():
    from agent.study.test_position_policy import _mirror
    pivot = 300.0
    long_run = _tight()
    short_run = pb.b_tight(_mirror(_bars(ROWS), pivot), date=DATE, direction="DOWN",
                           fill_ts=FILL_TS, fill_price=pivot - FILL,
                           close_et=pb.POLICY_CLOSE_ET)
    assert short_run.n_moves == long_run.n_moves
    assert short_run.exit_reason == long_run.exit_reason
    assert short_run.pnl == pytest.approx(long_run.pnl)


# --------------------------------------------------------------------------- #
# the stop-width sweep                                                          #
# --------------------------------------------------------------------------- #

def test_the_width_override_reaches_BOTH_baselines():
    """The single thing most likely to go wrong in the sweep. B-loose IS the initial
    stop, and B-tight starts from it, so scoring a wide-stop policy against a
    25-pt-stop baseline would hand the policy a win it never earned — invisibly."""
    wide = pb.initial_stop(FILL, "UP", 100.0)
    assert wide == pytest.approx(FILL - 100.0)

    default = _loose()
    overridden = pb.b_loose(_bars(ROWS), date=DATE, direction="UP", fill_ts=FILL_TS,
                            fill_price=FILL, close_et=pb.POLICY_CLOSE_ET, initial=wide)
    assert default.initial_stop == pytest.approx(FILL - 25.0)
    assert overridden.initial_stop == pytest.approx(FILL - 100.0)

    t = pb.b_tight(_bars(ROWS), date=DATE, direction="UP", fill_ts=FILL_TS,
                   fill_price=FILL, close_et=pb.POLICY_CLOSE_ET, initial=wide)
    assert t.initial_stop == pytest.approx(FILL - 100.0)


def test_every_width_spec_resolves_the_way_it_reads():
    long_kw = dict(avg_1h=100.0, structural=FILL - 10.0)
    assert pb.stop_for_width(FILL, "UP", ("pts", 50.0), **long_kw) \
        == pytest.approx(FILL - 50.0)
    assert pb.stop_for_width(FILL, "UP", ("avg", 1.6), **long_kw) \
        == pytest.approx(FILL - 160.0)
    assert pb.stop_for_width(FILL, "DOWN", ("avg", 1.6), **long_kw) \
        == pytest.approx(FILL + 160.0)
    assert pb.stop_for_width(FILL, "UP", ("mechanism", None), **long_kw) \
        == pytest.approx(FILL - 10.0)


def test_struct_or_width_keeps_the_anchor_and_only_refuses_to_be_tighter():
    """It separates two variables a fixed width conflates: widening, and discarding the
    structural anchor. Whichever is WIDER wins."""
    # structural is tighter than the width -> the width governs
    assert pb.stop_for_width(FILL, "UP", ("struct_or_avg", 1.0), avg_1h=100.0,
                             structural=FILL - 10.0) == pytest.approx(FILL - 100.0)
    # structural is already wider -> the anchor is KEPT, not overridden
    assert pb.stop_for_width(FILL, "UP", ("struct_or_avg", 1.0), avg_1h=100.0,
                             structural=FILL - 150.0) == pytest.approx(FILL - 150.0)
    # mirrored for a short
    assert pb.stop_for_width(FILL, "DOWN", ("struct_or_avg", 1.0), avg_1h=100.0,
                             structural=FILL + 150.0) == pytest.approx(FILL + 150.0)


def test_a_width_that_cannot_be_resolved_is_absent_rather_than_defaulted():
    """A missing `avg_1h` or structural stop must not silently fall back to 25 pts —
    that would put a control-width row under a wide-width label."""
    assert pb.stop_for_width(FILL, "UP", ("avg", 1.0), avg_1h=None) is None
    assert pb.stop_for_width(FILL, "UP", ("struct_or_avg", 1.0), avg_1h=100.0,
                             structural=None) is None
    assert pb.stop_for_width(FILL, "UP", ("mechanism", None), structural=None) is None
    with pytest.raises(ValueError):
        pb.stop_for_width(FILL, "UP", ("nonsense", 1.0))


def test_points_per_point_risked_divides_by_the_risk_capture_ignores():
    """Raw capture ranks the widest stop first by construction; this does not."""
    tight = [{"pnl": 50.0, "fill_price": 100.0, "initial_stop": 75.0}]
    wide = [{"pnl": 60.0, "fill_price": 100.0, "initial_stop": 0.0}]
    assert pb.per_point_risked(tight)["pooled"] == pytest.approx(2.0)
    assert pb.per_point_risked(wide)["pooled"] == pytest.approx(0.6)
    # the wide arm captures MORE points and LESS per point risked
    assert wide[0]["pnl"] > tight[0]["pnl"]
    assert pb.per_point_risked(wide)["pooled"] < pb.per_point_risked(tight)["pooled"]


def test_a_zero_risk_row_is_dropped_rather_than_dividing_by_zero():
    assert pb.per_point_risked(
        [{"pnl": 5.0, "fill_price": 100.0, "initial_stop": 100.0}])["n"] == 0


def test_avg_range_1h_mirrors_the_maintainers_own_constants_and_truncates():
    from agent.facts.maintainer import ATR_BARS, ATR_TAIL
    assert ATR_BARS == 20 and ATR_TAIL == pd.Timedelta(hours=30)

    idx = pd.date_range(f"{DATE} 00:00", periods=60 * 6, freq="1min", tz=TZ)
    # first three hours span 10 points, the last three span 50
    hi = [105.0] * (60 * 3) + [125.0] * (60 * 3)
    lo = [95.0] * (60 * 3) + [75.0] * (60 * 3)
    frame = pd.DataFrame({"Open": 100.0, "High": hi, "Low": lo, "Close": 100.0,
                          "Volume": 1.0}, index=idx)

    assert pb.avg_range_1h(frame, idx[-1]) == pytest.approx(30.0)     # (10+10+10+50+50+50)/6
    # LOOKAHEAD: at the end of hour three only the narrow bars exist
    assert pb.avg_range_1h(frame, idx[60 * 3 - 1]) == pytest.approx(10.0)
    assert pb.avg_range_1h(frame.iloc[0:0], idx[-1]) is None


# --------------------------------------------------------------------------- #
# §6's metrics                                                                  #
# --------------------------------------------------------------------------- #

def test_the_ceiling_is_the_segment_extreme_and_is_absent_when_the_fill_is_past_it():
    assert pb.ceiling(100.0, 150.0, "UP") == pytest.approx(50.0)
    assert pb.ceiling(100.0, 50.0, "DOWN") == pytest.approx(50.0)
    assert pb.ceiling(100.0, 90.0, "UP") is None       # nothing left to capture
    assert pb.ceiling(100.0, 100.0, "UP") is None


def test_capture_is_pnl_over_the_ceiling_and_absent_when_there_is_no_ceiling():
    assert pb.capture(25.0, 50.0) == pytest.approx(0.5)
    assert pb.capture(25.0, None) is None
    assert pb.capture(None, 50.0) is None


def test_a_scratch_is_a_stop_BEFORE_the_extreme_and_not_merely_a_stop():
    ex = pd.Timestamp(f"{DATE} 10:00", tz=TZ)
    early = pd.Timestamp(f"{DATE} 09:50", tz=TZ)
    late = pd.Timestamp(f"{DATE} 10:10", tz=TZ)
    assert pb.is_scratch(pb.EXIT_STOP, early, ex) is True
    assert pb.is_scratch(pb.EXIT_STOP, late, ex) is False   # the trail doing its job
    assert pb.is_scratch(pb.EXIT_MARK, early, ex) is False
    assert pb.is_scratch("dol_touch", early, ex) is False


def test_pooled_capture_reports_the_median_beside_the_points_weighted_figure():
    rows = [{"pnl": 10.0, "ceiling": 100.0}, {"pnl": 90.0, "ceiling": 100.0},
            {"pnl": 50.0, "ceiling": 1000.0}]
    got = pb.pooled_capture(rows)
    assert got["n"] == 3
    assert got["pooled"] == pytest.approx(150.0 / 1200.0)
    assert got["median"] == pytest.approx(0.1)             # they disagree, as intended
    assert got["pooled"] != pytest.approx(got["median"])


def test_sessions_with_no_ceiling_are_dropped_rather_than_scored_as_zero():
    rows = [{"pnl": 10.0, "ceiling": 100.0}, {"pnl": -5.0, "ceiling": None}]
    assert pb.pooled_capture(rows)["n"] == 1


def test_the_bootstrap_resamples_sessions_and_is_deterministic():
    rows = [{"pnl": float(i), "ceiling": 100.0} for i in range(20)]
    a = pb.session_bootstrap(rows, n_boot=300)
    b = pb.session_bootstrap(rows, n_boot=300)
    assert a == b                                          # seeded
    assert a["lo"] < pb.pooled_capture(rows)["pooled"] < a["hi"]
    assert pb.session_bootstrap(rows[:1])["lo"] is None     # n<2 has no interval


def test_extent_terciles_split_by_extent_and_report_their_ranges():
    rows = [{"pnl": 10.0, "ceiling": 100.0, "extent": float(i)} for i in range(1, 10)]
    got = pb.extent_terciles(rows)
    assert set(got) == {"small", "mid", "large"}
    assert got["small"]["extent_range"] == [1.0, 3.0]
    assert got["large"]["extent_range"] == [7.0, 9.0]
    assert sum(g["n"] for g in got.values()) == 9
