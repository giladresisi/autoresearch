from agent.study.nulls import null_a, null_b


def _cases(label_dists, pools):
    return [{"label_dist": ld, "candidate_dists": p}
            for ld, p in zip(label_dists, pools)]


def test_null_a_returns_a_distribution_not_a_point():
    r = null_a(_cases([5.0] * 8, [[5.0, 50.0, 500.0]] * 8), n_draws=200)
    assert r["n"] == 8
    assert r["null_p10"] <= r["null_median"] <= r["null_p90"]
    assert 0.0 <= r["beats_null_frac"] <= 1.0


def test_null_a_is_deterministic_under_the_fixed_seed():
    args = (_cases([5.0] * 8, [[5.0, 50.0, 500.0]] * 8),)
    assert null_a(*args, n_draws=200) == null_a(*args, n_draws=200)


def test_a_perfectly_predictive_label_beats_the_null():
    """The label sits ON the extreme; every other eligible pool is far away."""
    r = null_a(_cases([0.0] * 12, [[0.0, 200.0, 400.0, 600.0]] * 12), n_draws=400)
    assert r["observed_median"] == 0.0
    assert r["beats_null_frac"] > 0.9


def test_a_label_drawn_at_random_does_not_beat_the_null():
    """The case that matters: the null must be able to say no. Labels here are just
    members of their own pools, so the observed median is a typical null draw."""
    pools = [[10.0, 20.0, 30.0, 40.0, 50.0]] * 30
    labels = [p[i % len(p)] for i, p in enumerate(pools)]
    r = null_a(_cases(labels, pools), n_draws=400)
    assert 0.15 < r["beats_null_frac"] < 0.85


def test_null_a_handles_an_empty_corpus_without_raising():
    r = null_a([], n_draws=10)
    assert r["n"] == 0 and r["observed_median"] is None


def test_null_b_share_deltas_sum_to_zero():
    out = null_b(["week", "day", "day"], ["week", "day", "day", "session"])
    assert abs(sum(v["delta"] for v in out.values())) < 1e-9


def test_null_b_flags_a_class_that_is_pure_census():
    """A class drawn exactly in proportion to its population shows ~0 pull, and its
    interval must straddle zero."""
    draws = ["week"] * 25 + ["day"] * 25
    elig = ["week"] * 100 + ["day"] * 100
    out = null_b(draws, elig, n_boot=800)
    for c in ("week", "day"):
        assert abs(out[c]["delta"]) < 0.05
        assert out[c]["lo"] < 0 < out[c]["hi"]


def test_null_b_flags_a_class_that_draws_beyond_its_census():
    draws = ["week"] * 45 + ["day"] * 5
    elig = ["week"] * 50 + ["day"] * 150
    out = null_b(draws, elig, n_boot=800)
    assert out["week"]["delta"] > 0.4
    assert out["week"]["lo"] > 0            # interval clear of zero
    assert out["day"]["hi"] < 0


def test_null_b_reports_every_class_that_appears_anywhere():
    out = null_b(["week"], ["week", "day", "session", "mid"])
    assert set(out) == {"week", "day", "session", "mid"}
    assert out["mid"]["n_draws"] == 0
