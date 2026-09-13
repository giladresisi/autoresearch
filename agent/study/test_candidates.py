import pandas as pd
import pytest

from agent.study.candidates import Candidate, classify, universe
from agent.study.facts_source import StudyFacts

TZ = "America/New_York"


@pytest.fixture(scope="module")
def bundle():
    return StudyFacts().bundle_at(pd.Timestamp("2026-08-13 09:30", tz=TZ))


def test_classify_maps_every_name_family():
    assert classify("prev1_week_high") == ("week_extreme", "week")
    assert classify("prev6_day_low") == ("day_extreme", "day")
    assert classify("asia(cur)_high") == ("session_extreme", "session")
    assert classify("london(cur)_low") == ("session_extreme", "session")
    assert classify("TDO") == ("open_price", "session")
    assert classify("daily_mid") == ("mid", "derived")


def test_an_unknown_name_is_reported_not_guessed():
    """A new level family must surface as a failure, never as a silently missing class
    that dilutes every share in the report."""
    with pytest.raises(KeyError):
        classify("some_new_level_2027")


def test_classify_covers_every_name_the_facts_layer_emits(bundle):
    for tkr in ("MNQ", "MES"):
        for name in bundle.levels[tkr]:
            classify(name)          # must not raise


def test_both_tickers_are_present(bundle):
    assert {c.ticker for c in universe(bundle)} == {"MNQ", "MES"}


def test_names_sharing_one_price_collapse_to_one_candidate(bundle):
    """2026-08-13: prev1_week_high and prev6_day_high are both 30073.25."""
    hits = [c for c in universe(bundle)
            if c.ticker == "MNQ" and c.price == pytest.approx(30073.25)]
    assert len(hits) == 1
    assert set(hits[0].names) == {"prev1_week_high", "prev6_day_high"}


def test_a_collapsed_candidate_takes_the_highest_tier(bundle):
    """week outranks day outranks session. A week pool that happens to coincide with a
    day pool is a week pool -- misclassifying it corrupts the per-class rates the
    attractiveness hypothesis is measured on."""
    hit = next(c for c in universe(bundle)
               if c.ticker == "MNQ" and c.price == pytest.approx(30073.25))
    assert (hit.cls, hit.tier) == ("week_extreme", "week")


def test_the_mids_are_included_as_derived_candidates(bundle):
    mids = [c for c in universe(bundle) if c.cls == "mid"]
    assert {c.ticker for c in mids} == {"MNQ", "MES"}
    mnq_daily = next(c for c in mids if c.ticker == "MNQ" and "daily_mid" in c.names)
    assert mnq_daily.price == pytest.approx(bundle.mid_price["MNQ"]["daily_mid"])


def test_sweep_state_at_the_boundary_is_carried(bundle):
    u = {c.names[0]: c for c in universe(bundle) if c.ticker == "MNQ"}
    assert u["prev1_day_high"].swept_before is False    # swept 09:36, after the boundary
    assert u["prev2_day_high"].swept_before is True     # swept 00:06


def test_every_candidate_is_a_frozen_record_with_a_bool_sweep_flag(bundle):
    for c in universe(bundle):
        assert isinstance(c, Candidate)
        assert isinstance(c.swept_before, bool)
        assert isinstance(c.names, tuple) and c.names


def test_prices_are_unique_per_ticker(bundle):
    u = universe(bundle)
    for tkr in ("MNQ", "MES"):
        prices = [c.price for c in u if c.ticker == tkr]
        assert len(prices) == len(set(prices))


def test_the_universe_is_deterministically_ordered(bundle):
    a = [(c.ticker, c.price) for c in universe(bundle)]
    assert a == sorted(a)
    assert a == [(c.ticker, c.price) for c in universe(bundle)]


def test_fvg_zones_are_deliberately_absent(bundle):
    assert bundle.fvg_zones                     # they exist in the bundle...
    assert not [c for c in universe(bundle) if "fvg" in c.cls]   # ...and are not candidates
