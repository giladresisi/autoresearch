import pandas as pd
import pytest
from agent.facts.store import FactStore, CoverageStatus
from agent.facts.records import Fact, FactClass, FactState

TS = pd.Timestamp("2026-08-18 09:20", tz="America/New_York")


def _f(fid, price, ref=TS, cls=FactClass.LEVEL, ticker="MNQ", state=FactState.LIVE):
    return Fact(id=fid, cls=cls, ticker=ticker, label="", name="x", reference_ts=ref,
                price=price, price_low=None, price_high=None, timeframe=None,
                resolution="1min", state=state, state_ts=ref, provenance={}, extra={})


def test_upsert_replaces_by_id_not_appends():
    s = FactStore()
    s.upsert(_f("a", 100.0))
    s.upsert(_f("a", 100.0, state=FactState.SWEPT))
    assert len(s.query()) == 1 and s.get("a").state is FactState.SWEPT


def test_query_filters_by_class_ticker_and_state():
    s = FactStore()
    s.upsert(_f("a", 100.0))
    s.upsert(_f("b", 101.0, ticker="MES"))
    s.upsert(_f("c", 102.0, cls=FactClass.FVG))
    assert len(s.query(ticker="MNQ")) == 2
    assert len(s.query(cls=FactClass.FVG)) == 1


def test_query_filters_by_price_range():
    s = FactStore()
    for i, p in enumerate([100.0, 200.0, 300.0]):
        s.upsert(_f(str(i), p))
    assert len(s.query(price_range=(150.0, 250.0))) == 1


def test_watermark_is_per_class_and_per_ticker():
    s = FactStore()
    s.extend_coverage(FactClass.LEVEL, "MNQ", TS - pd.Timedelta(days=14))
    s.extend_coverage(FactClass.FVG, "MNQ", TS - pd.Timedelta(days=1))
    assert s.covered_from(FactClass.LEVEL, "MNQ") < s.covered_from(FactClass.FVG, "MNQ")
    assert s.covered_from(FactClass.LEVEL, "MES") is None


def test_extend_coverage_only_moves_the_watermark_backward():
    s = FactStore()
    early = TS - pd.Timedelta(days=14)
    s.extend_coverage(FactClass.LEVEL, "MNQ", early)
    s.extend_coverage(FactClass.LEVEL, "MNQ", TS - pd.Timedelta(days=2))
    assert s.covered_from(FactClass.LEVEL, "MNQ") == early


def test_trim_only_from_the_far_end_and_moves_the_watermark_forward():
    """Trimming from the middle breaks contiguity and makes the watermark lie."""
    s = FactStore()
    old = _f("old", 100.0, ref=TS - pd.Timedelta(days=13))
    new = _f("new", 101.0, ref=TS - pd.Timedelta(days=1))
    s.upsert(old); s.upsert(new)
    s.extend_coverage(FactClass.LEVEL, "MNQ", TS - pd.Timedelta(days=14))
    s.trim_far_end(FactClass.LEVEL, "MNQ", keep_from=TS - pd.Timedelta(days=7))
    assert s.get("old") is None and s.get("new") is not None
    assert s.covered_from(FactClass.LEVEL, "MNQ") == TS - pd.Timedelta(days=7)


def test_coverage_insufficient_when_envelope_exceeds_known_price_span():
    s = FactStore()
    s.upsert(_f("a", 29000.0))
    st = s.coverage_status(FactClass.LEVEL, "MNQ", price=29000.0, envelope=200.0,
                           at_time_cap=False)
    assert st is CoverageStatus.INSUFFICIENT


def test_coverage_complete_at_cap_when_looked_as_far_as_policy_allows():
    """At an all-time high there are no levels above BY DEFINITION — that is complete, not degraded."""
    s = FactStore()
    s.upsert(_f("a", 29000.0))
    st = s.coverage_status(FactClass.LEVEL, "MNQ", price=29000.0, envelope=200.0,
                           at_time_cap=True)
    assert st is CoverageStatus.COMPLETE_AT_CAP


def test_coverage_ok_when_facts_span_the_envelope():
    s = FactStore()
    s.upsert(_f("lo", 28800.0)); s.upsert(_f("hi", 29200.0))
    st = s.coverage_status(FactClass.LEVEL, "MNQ", price=29000.0, envelope=150.0,
                           at_time_cap=False)
    assert st is CoverageStatus.OK


def test_health_reports_covered_from_and_status_per_class():
    s = FactStore()
    s.extend_coverage(FactClass.LEVEL, "MNQ", TS - pd.Timedelta(days=14))
    h = s.health(FactClass.LEVEL, "MNQ")
    assert "covered_from" in h and "complete_through" in h


# --- added during implementation (not in the plan) --------------------------- #

def test_replace_class_is_wholesale_not_a_top_up():
    s = FactStore()
    s.upsert(_f("a", 100.0))
    s.upsert(_f("b", 101.0, cls=FactClass.FVG))
    s.replace_class(FactClass.LEVEL, "MNQ", [_f("c", 102.0)])
    assert s.get("a") is None and s.get("c") is not None
    assert s.get("b") is not None, "another class must be untouched"


def test_store_round_trips_through_dict():
    s = FactStore()
    s.upsert(_f("a", 100.0))
    s.extend_coverage(FactClass.LEVEL, "MNQ", TS - pd.Timedelta(days=14))
    back = FactStore.from_dict(s.to_dict())
    assert back.get("a") is not None
    assert back.covered_from(FactClass.LEVEL, "MNQ") == TS - pd.Timedelta(days=14)
