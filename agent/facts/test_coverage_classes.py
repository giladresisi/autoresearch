import pandas as pd
import pytest

from agent.facts.records import Fact, FactClass, FactState
from agent.facts.requirements import EXECUTOR_REQUIREMENT
from agent.facts.store import COVERAGE_CLASSES, CoverageStatus, FactStore

TZ = "America/New_York"


def _fact(cls, ticker, lo, hi, ts="2026-08-13 09:00"):
    return Fact(id=f"{cls.value}-{ticker}-{lo}-{hi}", cls=cls, ticker=ticker,
                label="x", name=None, reference_ts=pd.Timestamp(ts, tz=TZ),
                price=None, price_low=lo, price_high=hi, timeframe="5min",
                resolution="5min", state=FactState.LIVE,
                state_ts=pd.Timestamp(ts, tz=TZ), provenance={}, extra={})


def test_coverage_classes_are_level_and_fvg_only():
    """LEG and EXTREME spans are derived summaries, not coverage depth."""
    assert COVERAGE_CLASSES == (FactClass.LEVEL, FactClass.FVG)
    assert FactClass.LEG not in COVERAGE_CLASSES
    assert FactClass.EXTREME not in COVERAGE_CLASSES


def test_each_ticker_is_judged_against_its_own_price():
    """MES near 7790 must not be judged against MNQ's 30215 — the diagnosed bug."""
    s = FactStore()
    s.upsert(_fact(FactClass.LEVEL, "MNQ", 29900.0, 30500.0))
    s.upsert(_fact(FactClass.FVG, "MNQ", 29900.0, 30500.0))
    s.upsert(_fact(FactClass.LEVEL, "MES", 7600.0, 7900.0))
    s.upsert(_fact(FactClass.FVG, "MES", 7600.0, 7900.0))
    rep = s.coverage_report(EXECUTOR_REQUIREMENT,
                            prices={"MNQ": 30215.0, "MES": 7790.0},
                            atrs={"MNQ": 60.0, "MES": 15.0}, at_time_cap=False)
    assert rep["level:MES"]["status"] == CoverageStatus.OK.value
    assert rep["fvg:MES"]["status"] == CoverageStatus.OK.value


def test_a_ticker_with_no_facts_reports_insufficient_not_ok():
    s = FactStore()
    s.upsert(_fact(FactClass.LEVEL, "MNQ", 29900.0, 30500.0))
    s.upsert(_fact(FactClass.FVG, "MNQ", 29900.0, 30500.0))
    rep = s.coverage_report(EXECUTOR_REQUIREMENT,
                            prices={"MNQ": 30215.0, "MES": 7790.0},
                            atrs={"MNQ": 60.0, "MES": 15.0}, at_time_cap=False)
    assert rep["level:MES"]["status"] == CoverageStatus.INSUFFICIENT.value


def test_empty_leg_class_no_longer_pins_the_whole_status():
    """The regression this task exists for: LEG/MES is empty on every real day."""
    s = FactStore()
    for tkr, lo, hi in (("MNQ", 29900.0, 30500.0), ("MES", 7600.0, 7900.0)):
        s.upsert(_fact(FactClass.LEVEL, tkr, lo, hi))
        s.upsert(_fact(FactClass.FVG, tkr, lo, hi))
    # No LEG facts at all for either ticker.
    from agent.facts.store import ensure_coverage
    st = ensure_coverage(s, {}, EXECUTOR_REQUIREMENT, pd.Timestamp("2026-08-13 09:30", tz=TZ),
                         prices={"MNQ": 30215.0, "MES": 7790.0},
                         atrs={"MNQ": 60.0, "MES": 15.0})
    assert st is CoverageStatus.OK


def test_envelope_uses_each_tickers_own_atr():
    """MES's envelope must be MES-sized; MNQ's ATR would demand ~8x too much room."""
    s = FactStore()
    s.upsert(_fact(FactClass.LEVEL, "MES", 7770.0, 7810.0))
    s.upsert(_fact(FactClass.FVG, "MES", 7770.0, 7810.0))
    rep = s.coverage_report(EXECUTOR_REQUIREMENT, prices={"MES": 7790.0},
                            atrs={"MES": 8.0}, at_time_cap=False)
    assert rep["level:MES"]["status"] == CoverageStatus.OK.value
    rep_mnq_atr = s.coverage_report(EXECUTOR_REQUIREMENT, prices={"MES": 7790.0},
                                    atrs={"MES": 60.0}, at_time_cap=False)
    assert rep_mnq_atr["level:MES"]["status"] == CoverageStatus.INSUFFICIENT.value


def test_at_time_cap_reports_complete_not_insufficient():
    """New highs: nothing above exists to fetch. COMPLETE_AT_CAP is the right answer."""
    s = FactStore()
    s.upsert(_fact(FactClass.LEVEL, "MNQ", 28000.0, 30073.0))
    s.upsert(_fact(FactClass.FVG, "MNQ", 28000.0, 30073.0))
    rep = s.coverage_report(EXECUTOR_REQUIREMENT, prices={"MNQ": 30215.0},
                            atrs={"MNQ": 60.0}, at_time_cap=True)
    assert rep["level:MNQ"]["status"] == CoverageStatus.COMPLETE_AT_CAP.value


def test_the_report_carries_span_need_and_count_for_diagnosis():
    """The original bug was invisible because status was one collapsed value."""
    s = FactStore()
    s.upsert(_fact(FactClass.LEVEL, "MNQ", 29900.0, 30500.0))
    rep = s.coverage_report(EXECUTOR_REQUIREMENT, prices={"MNQ": 30215.0},
                            atrs={"MNQ": 60.0}, at_time_cap=False)
    row = rep["level:MNQ"]
    assert row["span"] == (29900.0, 30500.0)
    assert row["need"] == (30095.0, 30335.0)
    assert row["count"] == 1
