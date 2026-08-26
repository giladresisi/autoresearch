import pandas as pd
import pytest
from agent.facts.store import FactStore
from agent.facts.batch import run_batch
from agent.facts.requirements import ANALYZER_REQUIREMENT, EXECUTOR_REQUIREMENT

NOW = pd.Timestamp("2026-08-13 09:20", tz="America/New_York")


def _bars(n=3000):
    idx = pd.date_range("2026-07-28 18:00", periods=n, freq="1min", tz="America/New_York")
    base = pd.Series(range(n), index=idx).astype(float) * 0.25 + 29000
    return pd.DataFrame({"Open": base, "High": base + 5, "Low": base - 5,
                         "Close": base, "Volume": 1.0}, index=idx)


BARS = {"MNQ": _bars(), "MES": _bars()}


@pytest.mark.parametrize("req", [ANALYZER_REQUIREMENT, EXECUTOR_REQUIREMENT])
def test_cache_disabled_equals_cache_enabled(req):
    """The acid test: if deleting the store changes a result, the design is wrong."""
    cold = run_batch(FactStore(), BARS, req, NOW)
    warm = run_batch(FactStore(), BARS, req, NOW)
    warm = run_batch(warm, BARS, req, NOW)
    assert sorted(f.id for f in cold.query()) == sorted(f.id for f in warm.query())
    for f in cold.query():
        assert warm.get(f.id).state == f.state


def test_analyzer_result_is_identical_with_and_without_a_prepopulated_store(tmp_path):
    from agent.facts.assemble import assemble_facts
    cold = assemble_facts(run_batch(FactStore(), BARS, ANALYZER_REQUIREMENT, NOW), BARS, NOW)
    seeded = run_batch(FactStore(), BARS, EXECUTOR_REQUIREMENT, NOW)
    warm = assemble_facts(run_batch(seeded, BARS, ANALYZER_REQUIREMENT, NOW), BARS, NOW)
    assert cold[2].keys() == warm[2].keys()


# --- added during implementation (not in the plan) --------------------------- #

@pytest.mark.parametrize("req", [ANALYZER_REQUIREMENT, EXECUTOR_REQUIREMENT])
def test_every_fact_field_not_just_the_id_survives_a_warm_store(req):
    """The plan's gate compares ids and states. Prices, windows and the derived extras
    are exactly where stale cached state would hide, so compare those too."""
    cold = run_batch(FactStore(), BARS, req, NOW)
    warm = run_batch(run_batch(FactStore(), BARS, req, NOW), BARS, req, NOW)
    for f in cold.query():
        g = warm.get(f.id)
        assert g is not None
        assert (g.price, g.price_low, g.price_high) == (f.price, f.price_low, f.price_high)
        assert g.name == f.name and g.reference_ts == f.reference_ts
        assert g.state_ts == f.state_ts
        assert g.extra == f.extra


@pytest.mark.parametrize("req", [ANALYZER_REQUIREMENT, EXECUTOR_REQUIREMENT])
def test_a_store_seeded_by_the_OTHER_requirement_does_not_leak_into_this_one(req):
    other = EXECUTOR_REQUIREMENT if req is ANALYZER_REQUIREMENT else ANALYZER_REQUIREMENT
    cold = run_batch(FactStore(), BARS, req, NOW)
    seeded = run_batch(FactStore(), BARS, other, NOW)
    mixed = run_batch(seeded, BARS, req, NOW)
    cold_ids = sorted(f.id for f in cold.query())
    # Only classes THIS requirement owns are guaranteed replaced; compare those.
    for cls in req.fact_classes:
        a = sorted(f.id for f in cold.query(cls=cls))
        b = sorted(f.id for f in mixed.query(cls=cls))
        assert a == b, f"{cls} differs after seeding with {other.name}"
    assert cold_ids  # the fixture must actually produce facts, or this proves nothing
