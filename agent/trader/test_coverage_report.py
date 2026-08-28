import json
import os

import pytest

from agent.trader.replay import run_replay

DATE = "2026-08-13"
pytestmark = pytest.mark.timeout(900)


@pytest.fixture(scope="module")
def _run():
    return run_replay([DATE], allow_calls=False)[DATE]


def test_the_run_reports_coverage_per_class_and_ticker(_run):
    cov = _run["coverage"]
    assert set(cov) >= {"level:MNQ", "level:MES", "fvg:MNQ", "fvg:MES"}


def test_coverage_is_written_to_the_run_dir(_run):
    p = os.path.join(_run["run_dir"], "coverage_report.json")
    assert os.path.exists(p)
    with open(p, encoding="utf-8") as fh:
        assert set(json.load(fh)) >= {"level:MNQ", "fvg:MES"}


def test_no_class_is_permanently_insufficient_for_a_units_reason(_run):
    """The 08-13 diagnosis: MES was INSUFFICIENT on every bar because it was judged
    against MNQ's price. MES must now report a real verdict."""
    for key in ("level:MES", "fvg:MES"):
        row = _run["coverage"][key]
        assert row["need"] is not None
        lo, hi = row["need"]
        assert 7000.0 < lo < 9000.0, f"{key} judged against the wrong ticker's price"


def test_leg_and_extreme_are_absent_from_the_report(_run):
    """They are derived summaries; including them asks a question they cannot answer."""
    assert not [k for k in _run["coverage"] if k.startswith(("leg:", "extreme:"))]
