import pandas as pd
import pytest
from agent.facts.batch import run_batch
from agent.facts.incremental import run_incremental
from agent.facts.store import FactStore
from agent.facts.records import FactClass
from agent.facts.requirements import EXECUTOR_REQUIREMENT


def _session_bars(n=600, start="2026-08-13 00:00"):
    idx = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    base = pd.Series(range(n), index=idx).astype(float) * 0.25 + 29000
    return pd.DataFrame({"Open": base, "High": base + 3, "Low": base - 3,
                         "Close": base + 1, "Volume": 1.0}, index=idx)


NOW = pd.Timestamp("2026-08-13 09:30", tz="America/New_York")
BARS = {"MNQ": _session_bars(), "MES": _session_bars()}


def test_batch_populates_every_class_in_the_requirement():
    s = run_batch(FactStore(), BARS, EXECUTOR_REQUIREMENT, NOW)
    for cls in EXECUTOR_REQUIREMENT.fact_classes:
        assert s.covered_from(cls, "MNQ") is not None


def test_batch_is_symmetric_across_tickers():
    s = run_batch(FactStore(), BARS, EXECUTOR_REQUIREMENT, NOW)
    assert {f.ticker for f in s.query()} == {"MNQ", "MES"}


def test_batch_output_is_independent_of_prior_store_contents():
    """The canonicity property: a populated cache must not change the result."""
    empty = run_batch(FactStore(), BARS, EXECUTOR_REQUIREMENT, NOW)
    warm = run_batch(FactStore(), BARS, EXECUTOR_REQUIREMENT, NOW)
    warm = run_batch(warm, BARS, EXECUTOR_REQUIREMENT, NOW)
    assert sorted(f.id for f in empty.query()) == sorted(f.id for f in warm.query())


def test_batch_evaluates_newly_discovered_facts_forward_to_now():
    """A level found 8 days back must have its sweep/close-through state evaluated since."""
    s = run_batch(FactStore(), BARS, EXECUTOR_REQUIREMENT, NOW)
    lvls = s.query(cls=FactClass.LEVEL, ticker="MNQ")
    assert any(f.state_ts >= f.reference_ts for f in lvls)


def test_batch_is_price_unbounded_within_its_time_window():
    s = run_batch(FactStore(), BARS, EXECUTOR_REQUIREMENT, NOW)
    prices = [f.price for f in s.query(cls=FactClass.LEVEL) if f.price is not None]
    assert max(prices) - min(prices) > 0


@pytest.mark.slow
def test_incremental_matches_batch_for_the_same_window():
    """THE seam invariant: a fact near the boundary is identical from either producer.

    NOTE (divergence from the plan's draft): the loop stops at NOW rather than running
    to the end of BARS. The plan's draft drove the incremental driver 29 minutes PAST
    the `now` it gave `run_batch`, so the two producers covered different windows and
    the equality could never hold — see the test's own name. Aligning the windows is
    what makes this an actual seam test.
    """
    batch = run_batch(FactStore(), BARS, EXECUTOR_REQUIREMENT, NOW)
    inc, state = FactStore(), {}
    upto = int((BARS["MNQ"].index <= NOW).sum())
    for i in range(3, upto + 1):
        sub = {tk: df.iloc[:i] for tk, df in BARS.items()}
        state = run_incremental(inc, state, sub, sub["MNQ"].index[-1], bar_complete=True)
    b_ids = {f.id for f in batch.query(cls=FactClass.FVG, ticker="MNQ")}
    i_ids = {f.id for f in inc.query(cls=FactClass.FVG, ticker="MNQ")}
    assert b_ids == i_ids, f"seam mismatch: batch-only={b_ids - i_ids}, inc-only={i_ids - b_ids}"


def test_incremental_defers_detection_on_partial_bars():
    inc, state = FactStore(), {}
    sub = {tk: df.iloc[:100] for tk, df in BARS.items()}
    before = len(inc.query())
    run_incremental(inc, state, sub, sub["MNQ"].index[-1], bar_complete=False)
    assert len(inc.query(cls=FactClass.FVG)) == before


def test_incremental_never_raises_on_degenerate_input():
    inc, state = FactStore(), {}
    out = run_incremental(inc, state, {"MNQ": pd.DataFrame(), "MES": pd.DataFrame()},
                          NOW, bar_complete=True)
    assert isinstance(out, dict)


def test_batch_never_raises_on_empty_bars():
    s = run_batch(FactStore(), {"MNQ": pd.DataFrame(), "MES": pd.DataFrame()},
                  EXECUTOR_REQUIREMENT, NOW)
    assert isinstance(s, FactStore)


# --- added during implementation (not in the plan) --------------------------- #

def test_batch_never_reads_bars_after_now():
    """No-lookahead: bars past `now` must not enter the view."""
    early = pd.Timestamp("2026-08-13 04:00", tz="America/New_York")
    s = run_batch(FactStore(), BARS, EXECUTOR_REQUIREMENT, early)
    for f in s.query():
        if f.reference_ts is not None and f.cls is not FactClass.ANCHOR:
            assert f.reference_ts <= early


def test_incremental_state_is_json_serializable():
    import json
    inc, state = FactStore(), {}
    sub = {tk: df.iloc[:60] for tk, df in BARS.items()}
    state = run_incremental(inc, state, sub, sub["MNQ"].index[-1], bar_complete=True)
    json.dumps(state)
