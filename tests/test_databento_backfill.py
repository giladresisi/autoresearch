# tests/test_databento_backfill.py
# Unit tests for data/databento_backfill.py — covers all main branches of
# backfill_parquets(): missing parquets, already-current parquets, None/empty
# response, and the merge+dedup path.
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest


@pytest.fixture
def bar_dir(tmp_path: Path) -> Path:
    return tmp_path


def _make_df(last_ts: str) -> pd.DataFrame:
    ts = pd.Timestamp(last_ts, tz="America/New_York")
    return pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 500.0]],
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([ts]),
    )


def _mock_source_returning(df):
    """Return a context manager that patches DatabentSource with a given return value."""
    return patch("data.databento_backfill.DatabentSource", autospec=True)


class TestBackfillParquetsCreatesWhenMissing:
    def test_creates_mnq_parquet(self, bar_dir):
        new_df = _make_df("2026-05-01 10:00:00")
        with patch("data.databento_backfill.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = new_df
            from data.databento_backfill import backfill_parquets
            backfill_parquets(bar_dir)
        assert (bar_dir / "MNQ_1m.parquet").exists()

    def test_creates_mes_parquet(self, bar_dir):
        new_df = _make_df("2026-05-01 10:00:00")
        with patch("data.databento_backfill.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = new_df
            from data.databento_backfill import backfill_parquets
            backfill_parquets(bar_dir)
        assert (bar_dir / "MES_1m.parquet").exists()

    def test_saved_parquet_contains_fetched_rows(self, bar_dir):
        new_df = _make_df("2026-05-01 10:00:00")
        with patch("data.databento_backfill.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = new_df
            from data.databento_backfill import backfill_parquets
            backfill_parquets(bar_dir)
        result = pd.read_parquet(bar_dir / "MNQ_1m.parquet")
        assert len(result) == 1


class TestBackfillSkipsWhenCurrent:
    def test_no_fetch_when_last_bar_after_cutoff(self, bar_dir):
        now = pd.Timestamp.now(tz="America/New_York")
        recent_df = _make_df((now - pd.Timedelta(hours=1)).isoformat())
        recent_df.to_parquet(bar_dir / "MNQ_1m.parquet")
        recent_df.to_parquet(bar_dir / "MES_1m.parquet")
        with patch("data.databento_backfill.DatabentSource", autospec=False) as MockSource:
            from data.databento_backfill import backfill_parquets
            backfill_parquets(bar_dir, ib_cutoff_days=2)
        MockSource.return_value.fetch.assert_not_called()


class TestBackfillHandlesEmptyResponses:
    def test_none_response_does_not_raise(self, bar_dir):
        with patch("data.databento_backfill.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = None
            from data.databento_backfill import backfill_parquets
            backfill_parquets(bar_dir)  # must not raise

    def test_empty_df_response_does_not_raise(self, bar_dir):
        from data.databento_backfill import _empty_df
        with patch("data.databento_backfill.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = _empty_df()
            from data.databento_backfill import backfill_parquets
            backfill_parquets(bar_dir)  # must not raise

    def test_none_response_does_not_create_parquet(self, bar_dir):
        with patch("data.databento_backfill.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = None
            from data.databento_backfill import backfill_parquets
            backfill_parquets(bar_dir)
        assert not (bar_dir / "MNQ_1m.parquet").exists()


class TestBackfillMergesAndDeduplicates:
    def test_new_rows_appended_to_existing(self, bar_dir):
        old_df = _make_df("2026-04-01 10:00:00")
        old_df.to_parquet(bar_dir / "MNQ_1m.parquet")
        old_df.to_parquet(bar_dir / "MES_1m.parquet")
        new_row = _make_df("2026-04-02 10:00:00")
        with patch("data.databento_backfill.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = new_row
            from data.databento_backfill import backfill_parquets
            backfill_parquets(bar_dir)
        result = pd.read_parquet(bar_dir / "MNQ_1m.parquet")
        assert len(result) == 2

    def test_duplicate_rows_removed(self, bar_dir):
        old_df = _make_df("2026-04-01 10:00:00")
        old_df.to_parquet(bar_dir / "MNQ_1m.parquet")
        old_df.to_parquet(bar_dir / "MES_1m.parquet")
        # Fetch returns the old row again plus a new one
        duplicate_plus_new = pd.concat([old_df, _make_df("2026-04-02 10:00:00")])
        with patch("data.databento_backfill.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = duplicate_plus_new
            from data.databento_backfill import backfill_parquets
            backfill_parquets(bar_dir)
        result = pd.read_parquet(bar_dir / "MNQ_1m.parquet")
        assert len(result) == 2  # deduplicated: old + new, not old + old + new
