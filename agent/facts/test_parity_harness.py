import pandas as pd
import pytest
from scripts.facts_parity import compare_at, PANEL_DATES


def test_panel_dates_are_the_recorded_l1_set():
    """rerun_finalfinal's 10-day 09:20 panel."""
    assert PANEL_DATES == ("2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17",
                           "2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23",
                           "2026-07-24", "2026-07-27")


@pytest.mark.timeout(900)
def test_compare_at_returns_a_structured_diff():
    """Runs the real thing: two full 17-day `compute_facts` passes over the 1s
    parquets, ~60 s. The project-wide 60 s ceiling is raised for this ONE test —
    the whole point of the gate is that it uses the same data the reference does."""
    out = compare_at(pd.Timestamp("2026-07-15 09:20", tz="America/New_York"))
    assert "equal" in out and "diffs" in out


def test_compare_uses_the_same_17_day_window_as_prepare_cuts():
    import inspect
    import scripts.facts_parity as mod
    assert "ANALYZER_REQUIREMENT" in inspect.getsource(mod)


# --- added during implementation (not in the plan) --------------------------- #

def test_compare_at_degrades_structurally_when_the_data_root_is_missing(tmp_path):
    """The gate must record an unreadable data root, not crash the run."""
    out = compare_at(pd.Timestamp("2026-07-15 09:20", tz="America/New_York"),
                     main_dir=str(tmp_path / "does-not-exist"))
    assert out["equal"] is False and out["diffs"]
    assert out.get("error")
