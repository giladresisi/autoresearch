import pytest

from agent.study.baseline import (
    already_reached, class_pull_by_distance, distance_buckets, eligible_by_segment,
    label_rank, null_cases, ordinal_baseline, reach_baseline)
from agent.study.nulls import null_a, null_b

TZ = "-04:00"


def _c(date, seg, tk, price, dist, outcome, cls="day_extreme", reached=None):
    return {"date": date, "segment": seg, "ticker": tk, "price": price,
            "dist_from_start": dist, "outcome": outcome, "cls": cls, "tier": "day",
            "reached_ts": reached, "avg_range_1h": {"MNQ": 100.0, "MES": 100.0}}


def _l(date, seg, tk, price, status="labelled", own_extreme=None, reached=None,
       start="2026-08-13T09:30:00" + TZ, cls="day_extreme"):
    return {"date": date, "segment": seg, "ticker": tk, "price": price,
            "status": status, "own_extreme": own_extreme, "reached_ts": reached,
            "start_ts": start, "cls": cls,
            "avg_range_1h": {"MNQ": 100.0, "MES": 100.0}}


# --------------------------------------------------------------------------- #
# A1 -- the ordinal baseline
# --------------------------------------------------------------------------- #
def test_rank_is_zero_for_the_nearest_eligible_pool():
    rows = [_c("d", 0, "MNQ", 110.0, 10.0, "draw"),
            _c("d", 0, "MNQ", 150.0, 50.0, "near_miss_out_of_band")]
    assert label_rank(eligible_by_segment(rows)[("d", 0, "MNQ")]) == 0


def test_ineligible_rows_do_not_consume_a_rank():
    """A pool already swept, or behind the origin, was never a choice the decision
    could have made -- counting it would inflate every rank."""
    rows = [_c("d", 0, "MNQ", 90.0, None, "ineligible"),
            _c("d", 0, "MNQ", 110.0, 10.0, "reached_passed"),
            _c("d", 0, "MNQ", 150.0, 50.0, "draw")]
    elig = eligible_by_segment(rows)[("d", 0, "MNQ")]
    assert len(elig) == 2
    assert label_rank(elig) == 1


def test_rank_is_none_when_no_candidate_is_the_draw():
    rows = [_c("d", 0, "MNQ", 110.0, 10.0, "near_miss_out_of_band")]
    assert label_rank(eligible_by_segment(rows)[("d", 0, "MNQ")]) is None


def test_a_corpus_where_the_draw_is_always_second_nearest_scores_1_at_k_eq_1():
    """Synthetic sanity: the baseline must be able to reach 100%."""
    labels, cands = [], []
    for i in range(10):
        d = "2026-08-%02d" % (i + 1)
        labels.append(_l(d, 0, "MNQ", 150.0))
        cands += [_c(d, 0, "MNQ", 110.0, 10.0, "reached_passed"),
                  _c(d, 0, "MNQ", 150.0, 50.0, "draw"),
                  _c(d, 0, "MNQ", 300.0, 200.0, "near_miss_out_of_band")]
    out = ordinal_baseline(labels, cands, tickers=("MNQ",))["MNQ"]
    assert out["n"] == 10
    assert out["fixed_k"][1]["acc"] == 1.0
    assert out["fixed_k"][0]["acc"] == 0.0
    assert out["median_rank"] == 1


def test_fixed_k_coverage_excludes_segments_with_too_few_pools():
    """Naming 'the 5th nearest' is impossible where four pools exist; scoring that as a
    miss would flatter larger k."""
    labels = [_l("d", 0, "MNQ", 150.0)]
    cands = [_c("d", 0, "MNQ", 110.0, 10.0, "reached_passed"),
             _c("d", 0, "MNQ", 150.0, 50.0, "draw")]
    out = ordinal_baseline(labels, cands, tickers=("MNQ",))["MNQ"]
    assert out["fixed_k"][1]["coverage"] == 1
    assert out["fixed_k"][5]["coverage"] == 0


def test_reach_baseline_names_the_furthest_pool_inside_the_multiple():
    labels = [_l("d", 0, "MNQ", 150.0)]
    cands = [_c("d", 0, "MNQ", 110.0, 10.0, "reached_passed"),
             _c("d", 0, "MNQ", 150.0, 50.0, "draw"),
             _c("d", 0, "MNQ", 400.0, 300.0, "near_miss_out_of_band")]
    out = reach_baseline(labels, cands, mults=(1.0,), tickers=("MNQ",))["MNQ"][1.0]
    assert out["coverage"] == 1 and out["correct"] == 1


def test_reach_baseline_names_nothing_when_no_pool_is_inside():
    labels = [_l("d", 0, "MNQ", 400.0)]
    cands = [_c("d", 0, "MNQ", 400.0, 300.0, "draw")]
    out = reach_baseline(labels, cands, mults=(1.0,), tickers=("MNQ",))["MNQ"][1.0]
    assert out["coverage"] == 0 and out["correct"] == 0
    assert out["total"] == 1          # the miss is visible, not dropped


def test_reach_baseline_reports_accuracy_over_all_segments_too():
    """Coverage can always be bought by predicting less; both numbers are shown."""
    labels = [_l("a", 0, "MNQ", 400.0), _l("b", 0, "MNQ", 50.0)]
    cands = [_c("a", 0, "MNQ", 400.0, 300.0, "draw"),
             _c("b", 0, "MNQ", 50.0, 20.0, "draw")]
    out = reach_baseline(labels, cands, mults=(1.0,), tickers=("MNQ",))["MNQ"][1.0]
    assert out["acc"] == 1.0 and out["acc_over_all"] == 0.5


# --------------------------------------------------------------------------- #
# A2 -- class pull conditioned on distance
# --------------------------------------------------------------------------- #
def test_distance_buckets_split_each_segment_evenly():
    assert distance_buckets(list(range(8)), 4) == {0: 0, 1: 0, 2: 1, 3: 1,
                                                   4: 2, 5: 2, 6: 3, 7: 3}
    assert distance_buckets([], 4) == {}


def test_class_pull_within_a_bucket_is_zero_when_only_distance_matters():
    """Class and distance perfectly correlated: near pools are always 'session', far
    ones always 'week', and the draw is always the nearest. Globally 'session' looks
    dominant; inside its own bucket it must show nothing."""
    labels, cands = [], []
    for i in range(20):
        d = "2026-08-%02d" % (i + 1)
        labels.append(_l(d, 0, "MNQ", 110.0))
        cands += [_c(d, 0, "MNQ", 110.0, 10.0, "draw", cls="session_extreme"),
                  _c(d, 0, "MNQ", 300.0, 200.0, "near_miss_out_of_band",
                     cls="week_extreme")]
    buckets = class_pull_by_distance(labels, cands, n_buckets=2, tickers=("MNQ",))["MNQ"]
    near = null_b(buckets[0]["draws"], buckets[0]["eligible"], n_boot=400)
    assert abs(near["session_extreme"]["delta"]) < 1e-9      # census IS 100% here


def test_a_genuine_class_effect_survives_bucketing():
    """Two classes interleaved at the same distances; one wins every draw."""
    labels, cands = [], []
    for i in range(20):
        d = "2026-08-%02d" % (i + 1)
        labels.append(_l(d, 0, "MNQ", 110.0))
        cands += [_c(d, 0, "MNQ", 110.0, 10.0, "draw", cls="open_price"),
                  _c(d, 0, "MNQ", 112.0, 12.0, "reached_passed", cls="week_extreme")]
    buckets = class_pull_by_distance(labels, cands, n_buckets=1, tickers=("MNQ",))["MNQ"]
    out = null_b(buckets[0]["draws"], buckets[0]["eligible"], n_boot=400)
    assert out["open_price"]["delta"] > 0.4
    assert out["open_price"]["lo"] > 0


# --------------------------------------------------------------------------- #
# A3 -- the tighter null
# --------------------------------------------------------------------------- #
def test_reached_only_cases_exclude_pools_the_move_never_crossed():
    labels = [_l("d", 0, "MNQ", 150.0, own_extreme=200.0)]
    cands = [_c("d", 0, "MNQ", 110.0, 10.0, "reached_passed", reached="t1"),
             _c("d", 0, "MNQ", 150.0, 50.0, "draw", reached="t2"),
             _c("d", 0, "MNQ", 400.0, 300.0, "near_miss_out_of_band")]
    wide = null_cases(labels, cands, tickers=("MNQ",))["MNQ"]
    tight = null_cases(labels, cands, reached_only=True, tickers=("MNQ",))["MNQ"]
    assert len(wide[0]["candidate_dists"]) == 3
    assert len(tight[0]["candidate_dists"]) == 2


def test_the_tighter_null_can_still_say_no():
    """A corpus where the last-reached pool is a TYPICAL member of the pools the move
    crossed -- sometimes the closer one to the extreme, sometimes the further -- must
    not beat the null. Without this the tighter null could not say no to anything."""
    labels, cands = [], []
    for i in range(20):
        d = "2026-08-%02d" % (i + 1)
        near, far = 190.0, 150.0                     # extreme 200 -> dists 10 and 50
        win, lose = (near, far) if i % 2 else (far, near)
        labels.append(_l(d, 0, "MNQ", win, own_extreme=200.0))
        cands += [_c(d, 0, "MNQ", win, 50.0, "draw", reached="t1"),
                  _c(d, 0, "MNQ", lose, 60.0, "reached_passed", reached="t2")]
    cases = null_cases(labels, cands, reached_only=True, tickers=("MNQ",))["MNQ"]
    assert 0.1 < null_a(cases, n_draws=400)["beats_null_frac"] < 0.9


# --------------------------------------------------------------------------- #
# A4 -- is the decision instant viable?
# --------------------------------------------------------------------------- #
def test_a_draw_taken_inside_two_minutes_counts_as_already_reached():
    labels = [_l("d", 0, "MNQ", 150.0, reached="2026-08-13T09:31:00" + TZ)]
    cands = [_c("d", 0, "MNQ", 150.0, 50.0, "draw",
                reached="2026-08-13T09:31:00" + TZ)]
    out = already_reached(labels, cands, minutes=(2,), tickers=("MNQ",))["MNQ"][2]
    assert out["already_reached"] == 1 and out["frac"] == 1.0


def test_a_late_draw_is_ranked_among_the_pools_still_open():
    """The shifted target: if the nearest pool is already gone at the decision instant,
    the draw's rank among what remains is what a rule would have to name."""
    labels = [_l("d", 0, "MNQ", 150.0, reached="2026-08-13T09:50:00" + TZ)]
    cands = [_c("d", 0, "MNQ", 110.0, 10.0, "reached_passed",
                reached="2026-08-13T09:31:00" + TZ),
             _c("d", 0, "MNQ", 150.0, 50.0, "draw",
                reached="2026-08-13T09:50:00" + TZ)]
    out = already_reached(labels, cands, minutes=(2,), tickers=("MNQ",))["MNQ"][2]
    assert out["already_reached"] == 0
    assert out["median_rank_among_unreached"] == 0     # the nearer pool is gone


# --------------------------------------------------------------------------- #
# discipline
# --------------------------------------------------------------------------- #
def test_the_module_never_reads_a_result_field_as_a_predictor():
    """`overshoot`, `lag_min` and `extreme_ts` are computed from the move's end. A rule
    that reads one is not a rule (spec 26 §10). `own_extreme` appears only inside
    `null_cases`, which scores the null rather than predicting anything."""
    import inspect

    import agent.study.baseline as mod
    src = inspect.getsource(mod)
    # Field ACCESS, not the words: the docstring names them precisely to forbid them.
    for banned in ("overshoot", "lag_min", "extreme_ts"):
        assert '["%s"]' % banned not in src, banned
        assert ".get(\"%s\")" % banned not in src, banned
    assert src.count('["own_extreme"]') == 2       # both inside null_cases


def test_the_module_never_reads_a_wall_clock():
    import inspect

    import agent.study.baseline as mod
    src = inspect.getsource(mod)
    assert "datetime.now" not in src and "get_et_now" not in src
