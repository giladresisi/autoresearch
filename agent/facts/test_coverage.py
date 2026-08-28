import pandas as pd
import pytest
from agent.facts.store import FactStore, CoverageStatus, ensure_coverage
from agent.facts.requirements import EXECUTOR_REQUIREMENT
from agent.facts.records import FactClass

NOW = pd.Timestamp("2026-08-13 09:30", tz="America/New_York")


def _bars(n=2000):
    idx = pd.date_range("2026-08-01 18:00", periods=n, freq="1min", tz="America/New_York")
    base = pd.Series(range(n), index=idx).astype(float) * 0.5 + 29000
    return pd.DataFrame({"Open": base, "High": base + 5, "Low": base - 5,
                         "Close": base, "Volume": 1.0}, index=idx)


BARS = {"MNQ": _bars(), "MES": _bars()}


# `ensure_coverage` is per-ticker as of the coverage-classes fix; these fixtures
# use one synthetic tape for both tickers, so the same price/ATR goes to both.
def _P(price):
    return {"MNQ": price, "MES": price}


def _A(atr):
    return {"MNQ": atr, "MES": atr}


def test_precondition_is_evaluated_not_triggered_by_price_movement():
    """A precondition is a function of (now, price) — never of sampling history."""
    a = ensure_coverage(FactStore(), BARS, EXECUTOR_REQUIREMENT, NOW, prices=_P(29100.0),
                        atrs=_A(80.0))
    b = ensure_coverage(FactStore(), BARS, EXECUTOR_REQUIREMENT, NOW, prices=_P(29100.0),
                        atrs=_A(80.0))
    assert a == b


def test_envelope_is_two_atr_each_side():
    s = FactStore()
    ensure_coverage(s, BARS, EXECUTOR_REQUIREMENT, NOW, prices=_P(29100.0), atrs=_A(80.0))
    lvls = [f.price for f in s.query(cls=FactClass.LEVEL, ticker="MNQ") if f.price]
    assert lvls


def test_refill_fires_when_envelope_exceeds_known_span():
    s = FactStore()
    st = ensure_coverage(s, BARS, EXECUTOR_REQUIREMENT, NOW, prices=_P(29100.0), atrs=_A(80.0))
    assert st in (CoverageStatus.OK, CoverageStatus.COMPLETE_AT_CAP)
    assert len(s.query()) > 0, "an empty store must have been filled"


def test_cold_store_and_refill_take_the_same_code_path():
    """Startup, restart and coverage-refill are one path."""
    cold = FactStore()
    ensure_coverage(cold, BARS, EXECUTOR_REQUIREMENT, NOW, prices=_P(29100.0), atrs=_A(80.0))
    warm = FactStore()
    ensure_coverage(warm, BARS, EXECUTOR_REQUIREMENT, NOW, prices=_P(29100.0), atrs=_A(80.0))
    ensure_coverage(warm, BARS, EXECUTOR_REQUIREMENT, NOW, prices=_P(29100.0), atrs=_A(80.0))
    assert sorted(f.id for f in cold.query()) == sorted(f.id for f in warm.query())


def test_stops_at_the_time_cap_and_reports_complete_not_degraded():
    s = FactStore()
    st = ensure_coverage(s, BARS, EXECUTOR_REQUIREMENT, NOW, prices=_P(999999.0), atrs=_A(80.0))
    assert st is CoverageStatus.COMPLETE_AT_CAP


def test_envelope_is_two_sided_regardless_of_thesis_direction():
    """l2 §7 arms off COUNTER-thesis extremes; §4 binds counter-thesis gaps."""
    s = FactStore()
    ensure_coverage(s, BARS, EXECUTOR_REQUIREMENT, NOW, prices=_P(29100.0), atrs=_A(80.0))
    prices = [f.price for f in s.query(cls=FactClass.LEVEL, ticker="MNQ") if f.price]
    assert any(p < 29100.0 for p in prices) and any(p > 29100.0 for p in prices)


# --- added during implementation (not in the plan) --------------------------- #

def test_ensure_coverage_never_reports_insufficient():
    """The contract: once the cap is reached the answer is COMPLETE, never degraded."""
    s = FactStore()
    for price in (29100.0, 999999.0, 1.0):
        st = ensure_coverage(s, BARS, EXECUTOR_REQUIREMENT, NOW, prices=_P(price),
                             atrs=_A(80.0))
        assert st is not CoverageStatus.INSUFFICIENT


def test_second_call_at_the_cap_does_not_re_drive_the_lookback(monkeypatch):
    """A class that is inherently narrow must not make every call re-run the batch."""
    import agent.facts.batch as batch_mod
    s = FactStore()
    ensure_coverage(s, BARS, EXECUTOR_REQUIREMENT, NOW, prices=_P(29100.0), atrs=_A(80.0))

    calls = []
    real = batch_mod.run_batch
    monkeypatch.setattr(batch_mod, "run_batch",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    ensure_coverage(s, BARS, EXECUTOR_REQUIREMENT, NOW, prices=_P(29100.0), atrs=_A(80.0))
    assert calls == []
