import pytest

from agent.study.hazard import (
    _bucket, bootstrap_delta, features, fit_hazard, pool_observations,
    predict_stop_rank, score_loso, PoolObs)


def _c(date, seg, tk, price, dist, outcome, tier="day", cls="day_extreme"):
    return {"date": date, "segment": seg, "ticker": tk, "price": price,
            "dist_from_start": dist, "outcome": outcome, "tier": tier, "cls": cls}


def _l(date, seg, tk, avg=100.0, direction="up"):
    return {"date": date, "segment": seg, "ticker": tk, "status": "labelled",
            "direction": direction, "avg_range_1h": {"MNQ": avg, "MES": avg}}


def test_the_counterpart_gap_is_none_when_the_other_instrument_is_absent():
    """A one-sided segment must still yield observations -- losing them would silently
    shrink the corpus rather than mark the feature unavailable."""
    obs = pool_observations([_l("d", 0, "MNQ")],
                            [_c("d", 0, "MNQ", 110.0, 10.0, "reached_passed"),
                             _c("d", 0, "MNQ", 150.0, 50.0, "draw")])
    assert len(obs) == 2
    assert all(o.alt_gap_ahead is None for o in obs)


def test_the_counterpart_gap_is_measured_at_the_translated_price():
    """MNQ 110 with origin 100 maps to MES 55 at a 2:1 origin ratio; the next MES pool
    above it is 60, so the gap is 5 / avg_1h."""
    labels = [_l("d", 0, "MNQ"), _l("d", 0, "MES")]
    cands = [_c("d", 0, "MNQ", 110.0, 10.0, "reached_passed"),
             _c("d", 0, "MNQ", 150.0, 50.0, "draw"),
             _c("d", 0, "MES", 60.0, 10.0, "reached_passed"),
             _c("d", 0, "MES", 75.0, 25.0, "draw")]
    obs = {(o.ticker, o.rank): o for o in pool_observations(labels, cands)}
    assert obs[("MNQ", 0)].alt_gap_ahead == pytest.approx(0.05)


def _obs(rank, dist, gap, stop, seg=("d", 0), tier="day"):
    return PoolObs(seg=seg, ticker="MNQ", rank=rank, dist=dist, gap_ahead=gap,
                   gap_behind=0.5, tier=tier, cls="day_extreme", stop=stop,
                   censored=gap is None)


def test_buckets_split_on_the_declared_edges():
    assert _bucket(0.4, (1.0, 2.0, 3.0)) == 0
    assert _bucket(1.0, (1.0, 2.0, 3.0)) == 0          # inclusive lower edge
    assert _bucket(2.5, (1.0, 2.0, 3.0)) == 2
    assert _bucket(9.0, (1.0, 2.0, 3.0)) == 3


def test_only_pools_the_move_reached_become_observations():
    """Pools beyond the stop are unobserved, not negative: the move never got to them, so
    whether it would have continued is not evidence of anything."""
    labels = [_l("d", 0, "MNQ")]
    cands = [_c("d", 0, "MNQ", 110.0, 10.0, "reached_passed"),
             _c("d", 0, "MNQ", 150.0, 50.0, "draw"),
             _c("d", 0, "MNQ", 400.0, 300.0, "near_miss_out_of_band")]
    obs = pool_observations(labels, cands)
    assert [o.rank for o in obs] == [0, 1]
    assert [o.stop for o in obs] == [False, True]


def test_gaps_are_measured_in_avg_range_units():
    labels = [_l("d", 0, "MNQ", avg=100.0)]
    cands = [_c("d", 0, "MNQ", 110.0, 10.0, "reached_passed"),
             _c("d", 0, "MNQ", 150.0, 50.0, "draw")]
    obs = pool_observations(labels, cands)
    assert obs[0].dist == pytest.approx(0.1)
    assert obs[0].gap_ahead == pytest.approx(0.4)
    assert obs[1].gap_ahead is None and obs[1].censored is True


def test_a_pool_with_nothing_beyond_it_is_flagged_censored():
    """The move had nowhere to continue to, so its stop is not evidence that the pool
    held it."""
    labels = [_l("d", 0, "MNQ")]
    cands = [_c("d", 0, "MNQ", 150.0, 50.0, "draw")]
    assert pool_observations(labels, cands)[0].censored is True


def test_b0_reads_distance_only_and_b8_adds_the_gap():
    o = _obs(0, 2.5, 0.3, False)
    assert features(o, "B0") == (2,)
    assert features(o, "B8") == (2, 0)
    with pytest.raises(KeyError):
        features(o, "B99")


def test_an_unknown_family_is_a_failure_not_a_default():
    """Adding a family means adding a branch, never silently widening one."""
    with pytest.raises(KeyError):
        features(_obs(0, 1.0, 1.0, False), "made_up")


def test_the_hazard_is_smoothed_toward_the_pooled_rate():
    """An unsmoothed 0/3 bucket would assert a hazard of exactly zero on evidence that
    cannot support it."""
    train = [_obs(0, 0.5, 0.2, False) for _ in range(3)] + \
            [_obs(0, 2.5, 3.0, True) for _ in range(3)]
    hz, pooled = fit_hazard(train, "B0")
    assert pooled == pytest.approx(0.5)
    assert 0.0 < hz[(0,)] < 0.5
    assert 0.5 < hz[(2,)] < 1.0


def test_compose_picks_the_most_likely_stopping_rank():
    """Survival form: a low hazard early must not win merely by being first."""
    seg = [_obs(0, 0.5, 0.2, False), _obs(1, 2.5, 3.0, True)]
    hz = {(0,): 0.05, (2,): 0.9}
    assert predict_stop_rank(seg, hz, 0.5, "B0") == 1


def test_compose_can_pick_the_first_pool_when_its_hazard_is_high():
    seg = [_obs(0, 2.5, 3.0, True), _obs(1, 2.8, 0.2, False)]
    hz = {(2,): 0.9}
    assert predict_stop_rank(seg, hz, 0.5, "B0") == 0


def test_loso_never_trains_on_the_session_it_scores():
    """With 45 sessions, in-sample scoring would be meaningless. A corpus where one
    session is the sole example of its bucket must not be predicted from itself."""
    obs = []
    for i in range(8):
        seg = ("2026-08-%02d" % (i + 1), 0)
        obs += [_obs(0, 0.5, 0.2, False, seg=seg), _obs(1, 2.5, 3.0, True, seg=seg)]
    odd = ("2026-08-09", 0)
    obs += [_obs(0, 0.5, 0.2, True, seg=odd), _obs(1, 2.5, 3.0, False, seg=odd)]
    r = score_loso(obs, "B0", "MNQ")
    assert r["n_segments"] == 9
    assert dict(r["per_seg"])[odd] == 0          # its own oddity cannot rescue it
    assert r["correct"] == 8


def test_loso_reports_a_brier_score_too():
    obs = []
    for i in range(6):
        seg = ("2026-08-%02d" % (i + 1), 0)
        obs += [_obs(0, 0.5, 0.2, False, seg=seg), _obs(1, 2.5, 3.0, True, seg=seg)]
    assert 0.0 <= score_loso(obs, "B0", "MNQ")["brier"] <= 1.0


def test_the_bootstrap_resamples_sessions_not_pool_observations():
    """106 observations from 48 sessions are not independent -- every pool before the stop
    is a 'continue' by construction, so an observation-level interval is far too narrow."""
    a = {"per_seg": [(("d%d" % i, 0), 1) for i in range(20)]}
    b = {"per_seg": [(("d%d" % i, 0), 0) for i in range(20)]}
    out = bootstrap_delta(a, b, n_boot=500)
    assert out["delta"] == pytest.approx(1.0)
    assert out["n_segments"] == 20
    assert out["lo"] == out["hi"] == 1.0        # no variation to resample


def test_the_bootstrap_interval_straddles_zero_for_a_coin_flip():
    a = {"per_seg": [(("d%d" % i, 0), i % 2) for i in range(30)]}
    b = {"per_seg": [(("d%d" % i, 0), (i + 1) % 2) for i in range(30)]}
    out = bootstrap_delta(a, b, n_boot=800)
    assert out["lo"] < 0 < out["hi"]


def test_the_module_never_reads_a_result_field_or_a_wall_clock():
    import inspect

    import agent.study.hazard as mod
    src = inspect.getsource(mod)
    for banned in ("overshoot", "lag_min", "own_extreme", "extreme_ts"):
        assert '["%s"]' % banned not in src, banned
    assert "datetime.now" not in src and "get_et_now" not in src


# --------------------------------------------------------------------------- #
# B9 -- the abstain rule
# --------------------------------------------------------------------------- #
def test_d0_is_the_distance_to_the_nearest_eligible_pool():
    from agent.study.hazard import segment_d0
    d = segment_d0([_l("d", 0, "MNQ", avg=100.0)],
                   [_c("d", 0, "MNQ", 150.0, 50.0, "draw"),
                    _c("d", 0, "MNQ", 110.0, 10.0, "reached_passed")])
    assert d[("d", 0, "MNQ")] == (pytest.approx(0.1), False)


def test_an_unexplained_segment_is_flagged_for_the_abstain_target():
    from agent.study.hazard import segment_d0
    lab = _l("d", 0, "MNQ")
    lab["status"] = "unexplained"
    d = segment_d0([lab], [_c("d", 0, "MNQ", 150.0, 50.0, "near_miss_out_of_band")])
    assert d[("d", 0, "MNQ")][1] is True


def test_act_accuracy_counts_an_unexplained_session_as_a_miss():
    """The honest production number. `acc | labelled` hides the sessions where no pool was
    the draw at all -- in production we would have named a target there and been wrong."""
    from agent.study.hazard import abstain_curve
    labels, cands, obs = [], [], []
    for i in range(4):
        seg = ("2026-08-0%d" % (i + 1), 0)
        lab = _l(seg[0], 0, "MNQ")
        if i == 3:
            lab["status"] = "unexplained"
        labels.append(lab)
        cands += [_c(seg[0], 0, "MNQ", 110.0, 10.0, "reached_passed"),
                  _c(seg[0], 0, "MNQ", 150.0, 50.0,
                     "draw" if i < 3 else "near_miss_out_of_band")]
        obs += [_obs(0, 0.1, 0.4, False, seg=seg), _obs(1, 0.5, None, True, seg=seg)]
    rows = {r["threshold"]: r for r in abstain_curve(labels, cands, obs, "MNQ")}
    allrow = rows[None]
    assert allrow["n_covered"] == 4
    assert allrow["p_labelled"] == pytest.approx(0.75)
    assert allrow["act_accuracy"] <= allrow["acc_given_labelled"]


def test_a_tighter_threshold_trades_coverage_for_purity():
    from agent.study.hazard import abstain_curve
    labels, cands, obs = [], [], []
    for i in range(6):
        seg = ("2026-08-0%d" % (i + 1), 0)
        far = i >= 3
        labels.append(dict(_l(seg[0], 0, "MNQ"),
                           status="unexplained" if far else "labelled"))
        d0 = 300.0 if far else 10.0
        cands += [_c(seg[0], 0, "MNQ", 100.0 + d0, d0,
                     "near_miss_out_of_band" if far else "draw")]
        obs += [_obs(0, d0 / 100.0, None, True, seg=seg)]
    rows = {r["threshold"]: r for r in abstain_curve(labels, cands, obs, "MNQ")}
    assert rows[2.0]["coverage"] == pytest.approx(0.5)
    assert rows[2.0]["p_labelled"] == 1.0
    assert rows[None]["p_labelled"] == pytest.approx(0.5)
