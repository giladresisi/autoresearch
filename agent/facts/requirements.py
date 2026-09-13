"""Requirements are declarative VALUES, not code.

That is what makes under-declaration detectable: a requirement can be logged with
each decision, diffed, and pinned in a test. Versioned, because adding a fact class
changes what every past decision would have seen.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from agent.facts.records import FactClass


@dataclass(frozen=True)
class Requirement:
    name: str
    version: int
    fact_classes: tuple
    windows: dict
    resolution: str
    tickers: tuple


ANALYZER_REQUIREMENT = Requirement(
    name="analyzer",
    version=1,
    fact_classes=(FactClass.LEVEL, FactClass.FVG, FactClass.ANCHOR, FactClass.EXTREME),
    windows={
        FactClass.LEVEL: pd.Timedelta(days=17),    # == agent/bench/facts.py LOOKBACK
        FactClass.FVG: pd.Timedelta(days=17),
        FactClass.ANCHOR: pd.Timedelta(days=17),
        FactClass.EXTREME: pd.Timedelta(days=17),
    },
    resolution="1min",
    tickers=("MNQ", "MES"),
)

EXECUTOR_REQUIREMENT = Requirement(
    name="executor",
    version=1,
    fact_classes=(FactClass.LEVEL, FactClass.FVG, FactClass.LEG, FactClass.EXTREME),
    windows={
        FactClass.LEVEL: pd.Timedelta(days=14),
        FactClass.ANCHOR: pd.Timedelta(days=14),
        FactClass.FVG: pd.Timedelta(hours=24),
        FactClass.LEG: pd.Timedelta(hours=24),
        FactClass.EXTREME: pd.Timedelta(hours=24),
    },
    resolution="1min",
    tickers=("MNQ", "MES"),
)
