"""Plan 46 T6 — the classifier reproduces the study on real data (`-m slow`).

Reads `<global>/general/main/2026-12/MNQ_1m.parquet`, the back-adjusted file the study
(`<global>/studies/premove_mid_retrace/context.py`) ran on. Skipped when it is absent.
Expectations come from the registry (`named_cases.PREMOVE_CONTEXT_LABELLED`) and from the
frozen copy of the study's `context.csv` (`fixtures/premove_context_study.csv`).
"""
import os

import pandas as pd
import pytest

from agent.trader import named_cases as nc
from agent.trader import premove_context as pc

pytestmark = [pytest.mark.timeout(300), pytest.mark.slow]

TZ = "America/New_York"
_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                        "premove_context_study.csv")


def _parquet():
    import paths
    return os.path.join(str(paths.global_root()), "general", "main", "2026-12",
                        "MNQ_1m.parquet")


@pytest.fixture(scope="module")
def mnq():
    p = _parquet()
    if not os.path.exists(p):
        pytest.skip(f"{p} is absent")
    return pd.read_parquet(p)


def _at(date):
    return pd.Timestamp(date, tz=TZ) + pd.Timedelta(hours=9, minutes=20)


def test_reproduces_the_study_on_the_operator_labelled_days(mnq):
    assert len(nc.PREMOVE_CONTEXT_LABELLED) == 24
    bad = []
    for date, case in sorted(nc.PREMOVE_CONTEXT_LABELLED.items()):
        c = pc.classify(mnq, _at(date))
        got = (c.status, c.leg_direction,
               None if c.origin_ts is None else c.origin_ts.strftime("%Y-%m-%d %H:%M"))
        want = (case["status"], case["direction"], case["origin_ts"])
        if got != want or c.size is None or abs(c.size - case["size"]) > 0.01:
            bad.append((date, want, case["size"], got, c.size, c.reason))
    assert not bad, bad


def test_reproduces_the_study_on_all_77_big_rows(mnq):
    rows = pd.read_csv(_FIXTURE)
    assert len(rows) == 77
    mapping = {"COUNTER": pc.UNRELATED, "?": pc.UNKNOWN}
    bad = []
    for _, r in rows.iterrows():
        c = pc.classify(mnq, _at(r["date"]))
        want = mapping.get(r["ctx"], r["ctx"])
        ok = (c.status == want and c.leg_direction == r["dir"]
              and c.size is not None and round(c.size) == r["size"]
              and c.origin_ts.strftime("%m-%d %H:%M") == r["A_ts"])
        if not ok:
            bad.append((r["date"], r["ctx"], r["dir"], r["size"], r["A_ts"],
                        c.status, c.leg_direction, c.size, c.reason))
    assert not bad, bad
