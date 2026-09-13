import pandas as pd
from agent.facts.requirements import (ANALYZER_REQUIREMENT, EXECUTOR_REQUIREMENT, Requirement)
from agent.facts.records import FactClass


def test_analyzer_window_is_exactly_17_days():
    """Must match agent/bench/facts.py LOOKBACK or the parity gate is meaningless."""
    from agent.bench.facts import LOOKBACK
    assert ANALYZER_REQUIREMENT.windows[FactClass.LEVEL] == LOOKBACK
    assert LOOKBACK == pd.Timedelta(days=17)


def test_executor_htf_window_is_14_days():
    assert EXECUTOR_REQUIREMENT.windows[FactClass.LEVEL] == pd.Timedelta(days=14)


def test_executor_ltf_window_is_24_hours():
    assert EXECUTOR_REQUIREMENT.windows[FactClass.FVG] == pd.Timedelta(hours=24)


def test_requirements_are_versioned():
    """Adding a fact class changes what past decisions would have seen."""
    assert ANALYZER_REQUIREMENT.version >= 1
    assert EXECUTOR_REQUIREMENT.version >= 1


def test_requirements_are_frozen_values_not_code():
    import dataclasses
    assert dataclasses.is_dataclass(Requirement)
    assert ANALYZER_REQUIREMENT.__dataclass_params__.frozen


def test_both_requirements_cover_both_tickers():
    assert set(ANALYZER_REQUIREMENT.tickers) == {"MNQ", "MES"}
    assert set(EXECUTOR_REQUIREMENT.tickers) == {"MNQ", "MES"}


def test_executor_requirement_excludes_smt_lifecycle():
    """l2 §6/§7: an SMT is corroborating context, never a requirement."""
    assert "smt" not in {c.value for c in EXECUTOR_REQUIREMENT.fact_classes}
