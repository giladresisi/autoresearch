# tests/test_parquet_maintenance.py
# Unit tests for data/parquet_maintenance.py — covers all main branches of
# backfill_parquets(): missing parquets, already-current parquets, None/empty
# response, and the merge+dedup path.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

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
    return patch("data.parquet_maintenance.DatabentSource", autospec=True)


class TestBackfillParquetsCreatesWhenMissing:
    def test_creates_mnq_parquet(self, bar_dir):
        new_df = _make_df("2026-05-01 10:00:00")
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = new_df
            from data.parquet_maintenance import backfill_parquets
            backfill_parquets(bar_dir)
        assert (bar_dir / "MNQ_1m.parquet").exists()

    def test_creates_mes_parquet(self, bar_dir):
        new_df = _make_df("2026-05-01 10:00:00")
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = new_df
            from data.parquet_maintenance import backfill_parquets
            backfill_parquets(bar_dir)
        assert (bar_dir / "MES_1m.parquet").exists()

    def test_saved_parquet_contains_fetched_rows(self, bar_dir):
        new_df = _make_df("2026-05-01 10:00:00")
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = new_df
            from data.parquet_maintenance import backfill_parquets
            backfill_parquets(bar_dir)
        result = pd.read_parquet(bar_dir / "MNQ_1m.parquet")
        assert len(result) == 1


class TestBackfillSkipsWhenCurrent:
    def test_no_fetch_when_last_bar_after_cutoff(self, bar_dir):
        now = pd.Timestamp.now(tz="America/New_York")
        recent_df = _make_df((now - pd.Timedelta(hours=1)).isoformat())
        recent_df.to_parquet(bar_dir / "MNQ_1m.parquet")
        recent_df.to_parquet(bar_dir / "MES_1m.parquet")
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            from data.parquet_maintenance import backfill_parquets
            backfill_parquets(bar_dir, ib_cutoff_days=2)
        MockSource.return_value.fetch.assert_not_called()


class TestBackfillHandlesEmptyResponses:
    def test_none_response_does_not_raise(self, bar_dir):
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = None
            from data.parquet_maintenance import backfill_parquets
            backfill_parquets(bar_dir)  # must not raise

    def test_empty_df_response_does_not_raise(self, bar_dir):
        from data.parquet_maintenance import _empty_df
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = _empty_df()
            from data.parquet_maintenance import backfill_parquets
            backfill_parquets(bar_dir)  # must not raise

    def test_none_response_does_not_create_parquet(self, bar_dir):
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = None
            from data.parquet_maintenance import backfill_parquets
            backfill_parquets(bar_dir)
        assert not (bar_dir / "MNQ_1m.parquet").exists()


class TestBackfillMergesAndDeduplicates:
    def test_new_rows_appended_to_existing(self, bar_dir):
        old_df = _make_df("2026-04-01 10:00:00")
        old_df.to_parquet(bar_dir / "MNQ_1m.parquet")
        old_df.to_parquet(bar_dir / "MES_1m.parquet")
        new_row = _make_df("2026-04-02 10:00:00")
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = new_row
            from data.parquet_maintenance import backfill_parquets
            backfill_parquets(bar_dir)
        result = pd.read_parquet(bar_dir / "MNQ_1m.parquet")
        assert len(result) == 2

    def test_duplicate_rows_removed(self, bar_dir):
        old_df = _make_df("2026-04-01 10:00:00")
        old_df.to_parquet(bar_dir / "MNQ_1m.parquet")
        old_df.to_parquet(bar_dir / "MES_1m.parquet")
        # Fetch returns the old row again plus a new one
        duplicate_plus_new = pd.concat([old_df, _make_df("2026-04-02 10:00:00")])
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = duplicate_plus_new
            from data.parquet_maintenance import backfill_parquets
            backfill_parquets(bar_dir)
        result = pd.read_parquet(bar_dir / "MNQ_1m.parquet")
        assert len(result) == 2  # deduplicated: old + new, not old + old + new


class TestBackfill1sParquets:
    def test_backfill_1s_creates_mnq_1s_parquet(self, bar_dir):
        new_df = _make_df("2026-05-01 10:00:00")
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = new_df
            from data.parquet_maintenance import backfill_1s_parquets
            backfill_1s_parquets(bar_dir)
        assert (bar_dir / "MNQ_1s.parquet").exists()

    def test_backfill_1s_creates_mes_1s_parquet(self, bar_dir):
        new_df = _make_df("2026-05-01 10:00:00")
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = new_df
            from data.parquet_maintenance import backfill_1s_parquets
            backfill_1s_parquets(bar_dir)
        assert (bar_dir / "MES_1s.parquet").exists()

    def test_backfill_1s_no_cutoff_calls_with_end_near_now(self, bar_dir):
        """end argument must be within 60s of now â€” no artificial cutoff."""
        from data.parquet_maintenance import backfill_1s_parquets
        new_df = _make_df("2026-05-01 10:00:00")
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = new_df
            backfill_1s_parquets(bar_dir)
        now_utc = pd.Timestamp.now(tz="UTC")
        for call in MockSource.return_value.fetch.call_args_list:
            end_arg = call.args[2] if len(call.args) > 2 else call.kwargs.get("end")
            end_ts = pd.Timestamp(end_arg)
            if end_ts.tzinfo is None:
                end_ts = end_ts.tz_localize("UTC")
            delta = (now_utc - end_ts).total_seconds()
            assert abs(delta) < 60, f"end is {delta:.0f}s from now â€” expected â‰¤60s"

    def test_backfill_1s_calls_interval_1s(self, bar_dir):
        """fetch() must be called with interval='1s'."""
        from data.parquet_maintenance import backfill_1s_parquets
        new_df = _make_df("2026-05-01 10:00:00")
        with patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = new_df
            backfill_1s_parquets(bar_dir)
        for call in MockSource.return_value.fetch.call_args_list:
            interval = call.kwargs.get("interval") or (call.args[3] if len(call.args) > 3 else None)
            assert interval == "1s"


class TestMergeSession1sParquets:
    def _make_ib_mock(self, bars=None):
        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = True
        mock_ib.reqHistoricalData.return_value = bars or []
        return mock_ib

    def test_merge_session_integrates_into_main(self, bar_dir):
        """Session parquet rows must be appended to main and session file deleted."""
        old_df = _make_df("2026-05-08 09:30:00")
        old_df.to_parquet(bar_dir / "MNQ_1s.parquet")
        new_df = _make_df("2026-05-08 10:00:00")
        new_df.to_parquet(bar_dir / "MNQ_1s_session_20260508.parquet")

        mock_ib = self._make_ib_mock()
        with patch("ib_insync.IB", return_value=mock_ib):
            from data.parquet_maintenance import merge_session_1s_parquets
            merge_session_1s_parquets(bar_dir)

        result = pd.read_parquet(bar_dir / "MNQ_1s.parquet")
        assert len(result) == 2
        assert not (bar_dir / "MNQ_1s_session_20260508.parquet").exists()

    def test_merge_session_noop_when_no_session_files(self, bar_dir):
        """No IB connection opened when there are no session files."""
        with patch("ib_insync.IB") as mock_ib_cls:
            from data.parquet_maintenance import merge_session_1s_parquets
            merge_session_1s_parquets(bar_dir)  # must not raise
        mock_ib_cls.assert_not_called()

    def test_merge_session_deduplicates_overlapping_rows(self, bar_dir):
        """Duplicate timestamps across main and session parquets must be removed."""
        shared_ts = "2026-05-08 09:30:00"
        old_df = _make_df(shared_ts)
        old_df.to_parquet(bar_dir / "MNQ_1s.parquet")
        session_df = pd.concat([_make_df(shared_ts), _make_df("2026-05-08 09:30:01")])
        session_df.to_parquet(bar_dir / "MNQ_1s_session_20260508.parquet")

        mock_ib = self._make_ib_mock()
        with patch("ib_insync.IB", return_value=mock_ib):
            from data.parquet_maintenance import merge_session_1s_parquets
            merge_session_1s_parquets(bar_dir)

        result = pd.read_parquet(bar_dir / "MNQ_1s.parquet")
        assert result.index.duplicated().sum() == 0
        assert len(result) == 2  # shared_ts once + new ts

    def test_merge_session_gap_fill_called_with_correct_duration(self, bar_dir):
        """reqHistoricalData must be called with durationStr matching the gap size."""
        t_main = pd.Timestamp("2026-05-08 09:18:00", tz="America/New_York")
        t_session = pd.Timestamp("2026-05-08 09:20:00", tz="America/New_York")  # 120s gap
        main_df = pd.DataFrame(
            {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
            index=pd.DatetimeIndex([t_main]),
        )
        session_df = pd.DataFrame(
            {"Open": [2.0], "High": [2.0], "Low": [2.0], "Close": [2.0], "Volume": [2.0]},
            index=pd.DatetimeIndex([t_session]),
        )
        main_df.to_parquet(bar_dir / "MNQ_1s.parquet")
        session_df.to_parquet(bar_dir / "MNQ_1s_session_20260508.parquet")

        mock_ib = self._make_ib_mock()
        with patch("ib_insync.IB", return_value=mock_ib):
            from data.parquet_maintenance import merge_session_1s_parquets
            merge_session_1s_parquets(bar_dir)

        call_kwargs = mock_ib.reqHistoricalData.call_args.kwargs
        assert call_kwargs.get("durationStr") == "119 S"
        assert call_kwargs.get("barSizeSetting") == "1 secs"


class TestSafeReadLastTs:
    def test_safe_read_last_ts_returns_last_index(self, tmp_path):
        """_safe_read_last_ts returns the last index value without reading OHLCV columns."""
        from data.parquet_maintenance import _safe_read_last_ts

        ts0 = pd.Timestamp("2026-04-01 09:30:00", tz="America/New_York")
        ts1 = pd.Timestamp("2026-04-01 09:31:00", tz="America/New_York")
        df = pd.DataFrame(
            [[100.0, 101.0, 99.0, 100.5, 500.0],
             [101.0, 102.0, 100.0, 101.5, 600.0]],
            columns=["Open", "High", "Low", "Close", "Volume"],
            index=pd.DatetimeIndex([ts0, ts1]),
        )
        path = tmp_path / "test.parquet"
        df.to_parquet(path)

        result = _safe_read_last_ts(path)
        assert result == df.index[-1]

    def test_safe_read_last_ts_returns_none_for_missing_file(self, tmp_path):
        """_safe_read_last_ts returns None when the file does not exist."""
        from data.parquet_maintenance import _safe_read_last_ts

        result = _safe_read_last_ts(tmp_path / "nonexistent.parquet")
        assert result is None


class TestBackfillParquetsReadOptimization:
    def test_backfill_parquets_skips_full_read_when_current(self, tmp_path):
        """When the parquet is already current, _safe_read_parquet must NOT be called."""
        from data.parquet_maintenance import backfill_parquets

        # Return a timestamp well within the cutoff window (1 hour ago = current)
        now = pd.Timestamp.now(tz="America/New_York")
        recent_ts = now - pd.Timedelta(hours=1)

        full_read_mock = MagicMock(side_effect=AssertionError("full read should not happen"))

        with patch("data.parquet_maintenance._safe_read_last_ts", return_value=recent_ts), \
             patch("data.parquet_maintenance._safe_read_parquet", full_read_mock), \
             patch("data.parquet_maintenance.DatabentSource", autospec=False):
            backfill_parquets(tmp_path, ib_cutoff_days=2)

        full_read_mock.assert_not_called()

    def test_backfill_parquets_reads_full_parquet_when_stale(self, tmp_path):
        """When the parquet is stale, _safe_read_parquet MUST be called to load existing data."""
        from data.parquet_maintenance import backfill_parquets, _empty_df

        old_ts = pd.Timestamp("2020-01-01", tz="America/New_York")
        full_read_mock = MagicMock(return_value=_empty_df())

        with patch("data.parquet_maintenance._safe_read_last_ts", return_value=old_ts), \
             patch("data.parquet_maintenance._safe_read_parquet", full_read_mock), \
             patch("data.parquet_maintenance.DatabentSource", autospec=False) as MockSource:
            MockSource.return_value.fetch.return_value = None
            backfill_parquets(tmp_path)

        assert full_read_mock.called
