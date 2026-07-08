"""Shared helpers/fixtures for the shadow unit tests.

All tests run OFFLINE with the StubBackend (no keys, no network, no spend). Live frames
are simulated from the committed derive_facts golden slices (capitalised OHLCV columns,
tz-aware ET) — exactly the shape the pipeline's rolling frames carry.
"""

import os
import sys

import pandas as pd
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
for _p in (_HERE, _AGENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FIX = os.path.join(_AGENT, "fixtures", "derive_facts_golden")
DOCS_ROOT = os.path.join(os.path.dirname(_AGENT), "agent-docs", "strategy")
ATH_MNQ = 30077.0
ATH_MES = 7602.5


def fixtures_present() -> bool:
    return os.path.exists(os.path.join(FIX, "MNQ_1s_slice.parquet"))


def load_live_frames(now=None) -> dict:
    """Live-frame dict from the golden slices; `now` defaults to the last common bar."""
    mnq = pd.read_parquet(os.path.join(FIX, "MNQ_1s_slice.parquet"))
    mes = pd.read_parquet(os.path.join(FIX, "MES_1s_slice.parquet"))
    if now is None:
        now = min(mnq.index[-1], mes.index[-1])
    return {"mnq_today": mnq, "mes_today": mes, "hist_mnq": None, "hist_mes": None,
            "hist_1hr": None, "hist_4hr": None, "ath_mnq": ATH_MNQ, "ath_mes": ATH_MES,
            "now": now}


@pytest.fixture
def frames():
    if not fixtures_present():
        pytest.skip("derive_facts golden fixtures not present")
    return load_live_frames()
