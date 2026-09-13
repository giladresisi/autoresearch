import pandas as pd
import pytest

from agent.study.facts_source import StudyFacts

TZ = "America/New_York"


@pytest.fixture(scope="module")
def sf():
    return StudyFacts()


def test_it_reads_the_1m_parquet_not_the_1s_one(sf):
    """The 1s parquets start 2026-05-01 and would truncate the universe across three
    weeks of the corpus; the 1m ones carry history back to 2024."""
    for tk in ("MNQ", "MES"):
        assert sf.source_path(tk).endswith(f"{tk}_1m.parquet")
        assert "2026-09" in sf.source_path(tk)


def test_the_early_study_dates_get_a_full_universe(sf):
    """The whole reason for 1m: at this boundary the 1s path yields 4 levels."""
    bu = sf.bundle_at(pd.Timestamp("2026-05-01 09:35", tz=TZ))
    assert len(bu.levels["MNQ"]) >= 20
    assert len(bu.levels["MES"]) >= 20


@pytest.mark.timeout(900)   # four 1s bundles over a 17-day 1s window
def test_1m_and_1s_agree_where_both_have_history(sf):
    """Sweeps are wick tests and a 1m bar's high/low is the max/min of its 1s bars, so
    the crossing MINUTE is preserved. Locking this makes the 1m choice a verified
    equivalence rather than a convenience.

    Builds four 1s bundles over a 17-day 1s window; ~3 minutes is expected, not a hang.
    """
    from agent.bench.facts import ParquetFactsSource, bundle_for_boundary
    src = ParquetFactsSource()
    for when in ("2026-08-13 09:35", "2026-08-11 09:35"):
        ts = pd.Timestamp(when, tz=TZ)
        a, _ = bundle_for_boundary(src._raw_1s, src._norm_1s, ts)
        b = sf.bundle_at(ts)
        for tk in ("MNQ", "MES"):
            assert set(a.levels[tk]) == set(b.levels[tk]), (when, tk)
            for name, va in a.levels[tk].items():
                assert va[0] == pytest.approx(b.levels[tk][name][0]), (when, tk, name)
            for name, sa in a.swept_at[tk].items():
                sb = b.swept_at[tk].get(name)
                assert (sa is None) == (sb is None), (when, tk, name)
                if sa is not None:
                    assert abs((sa - sb).total_seconds()) <= 60, (when, tk, name)


def test_avg_range_1h_is_available_for_the_tolerance_band(sf):
    bu = sf.bundle_at(pd.Timestamp("2026-08-13 09:30", tz=TZ))
    assert bu.avg_range_1h["MNQ"] > 0
    assert bu.avg_range_1h["MES"] > 0


def test_the_boundary_is_exclusive(sf):
    """A candidate swept BY the move must still read unswept at move-start, or the
    labelling eats its own answer. prev1_day_high is swept at 09:36 on this date."""
    bu = sf.bundle_at(pd.Timestamp("2026-08-13 09:30", tz=TZ))
    assert bu.swept_at["MNQ"]["prev1_day_high"] is None


def test_bundles_are_cached_per_boundary(sf):
    ts = pd.Timestamp("2026-08-13 09:30", tz=TZ)
    assert sf.bundle_at(ts) is sf.bundle_at(ts)


def test_the_module_never_reads_a_wall_clock():
    import inspect

    import agent.study.facts_source as mod
    src = inspect.getsource(mod)
    assert "datetime.now" not in src
    assert "get_et_now" not in src
