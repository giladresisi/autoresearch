# tests/test_check_session_parquets.py
# Tests for scripts/check_session_parquets.py
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pandas as pd
import pytest


@pytest.fixture
def bar_dir(tmp_path):
    return tmp_path


def _make_session_df(timestamps, price=27000.0):
    idx = pd.DatetimeIndex([
        pd.Timestamp(ts, tz="America/New_York") for ts in timestamps
    ])
    return pd.DataFrame({
        "Open": price, "High": price + 10, "Low": price - 10,
        "Close": price, "Volume": 100.0,
    }, index=idx)


def _make_ib_mock(bars=None):
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.reqHistoricalData.return_value = bars or []
    return ib


# ---------------------------------------------------------------------------
# TestValidateSessionDf
# ---------------------------------------------------------------------------

class TestValidateSessionDf:
    def test_ok_clean_df(self):
        from scripts.check_session_parquets import validate_session_df

        base = pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(100)])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)

        result = validate_session_df(df, price_lo=20000, price_hi=35000)
        assert result["severity"] == "ok"

    def test_minor_single_bad_price_row(self):
        from scripts.check_session_parquets import validate_session_df

        base = pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(1000)])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)
        # 1 bad row: High=999 (below price_lo=20000)
        df.iloc[5, df.columns.get_loc("High")] = 999.0
        df.iloc[5, df.columns.get_loc("Low")] = 989.0
        df.iloc[5, df.columns.get_loc("Close")] = 994.0
        df.iloc[5, df.columns.get_loc("Open")] = 994.0

        result = validate_session_df(df, price_lo=20000, price_hi=35000)
        # 1/1000 = 0.1% — below BAD_ROW_MINOR_FRAC (1%) but total_bad > 0 -> minor
        assert result["severity"] == "minor"

    def test_major_bad_row_fraction(self):
        from scripts.check_session_parquets import validate_session_df

        base = pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(100)])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)
        # 2 bad rows = 2% >= BAD_ROW_MINOR_FRAC=1% -> major
        for i in [0, 1]:
            df.iloc[i, df.columns.get_loc("High")] = 999.0
            df.iloc[i, df.columns.get_loc("Low")] = 989.0
            df.iloc[i, df.columns.get_loc("Close")] = 994.0
            df.iloc[i, df.columns.get_loc("Open")] = 994.0

        result = validate_session_df(df, price_lo=20000, price_hi=35000)
        assert result["severity"] == "major"

    def test_critical_bad_row_fraction(self):
        from scripts.check_session_parquets import validate_session_df

        base = pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(100)])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)
        # 6 bad rows = 6% >= BAD_ROW_CRITICAL_FRAC=5% -> critical
        for i in range(6):
            df.iloc[i, df.columns.get_loc("High")] = 999.0
            df.iloc[i, df.columns.get_loc("Low")] = 989.0
            df.iloc[i, df.columns.get_loc("Close")] = 994.0
            df.iloc[i, df.columns.get_loc("Open")] = 994.0

        result = validate_session_df(df, price_lo=20000, price_hi=35000)
        assert result["severity"] == "critical"

    def test_minor_small_gap(self):
        from scripts.check_session_parquets import validate_session_df

        # Monday 2026-05-18 10:00 ET, gap of ~3 minutes (180s) between index 300 and 480
        base = pd.Timestamp("2026-05-18 10:00:00", tz="America/New_York")
        timestamps = (
            [base + pd.Timedelta(seconds=i) for i in range(300)]
            + [base + pd.Timedelta(seconds=480 + i) for i in range(60)]
        )
        idx = pd.DatetimeIndex(timestamps)
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)

        result = validate_session_df(df, price_lo=20000, price_hi=35000)
        # gap = 180s > 90s but < SMALL_GAP_THRESHOLD=5min=300s -> minor
        assert result["severity"] == "minor"
        assert result["max_gap_s"] > 90
        assert result["max_gap_s"] < 300

    def test_major_large_gap(self):
        from scripts.check_session_parquets import validate_session_df

        # Monday 2026-05-18 10:00 ET, gap of ~30 minutes
        base = pd.Timestamp("2026-05-18 10:00:00", tz="America/New_York")
        timestamps = (
            [base + pd.Timedelta(seconds=i) for i in range(60)]
            + [base + pd.Timedelta(seconds=2000 + i) for i in range(60)]
        )
        idx = pd.DatetimeIndex(timestamps)
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)

        result = validate_session_df(df, price_lo=20000, price_hi=35000)
        # gap ~1940s >= SMALL_GAP_THRESHOLD=300s and < LARGE_GAP_THRESHOLD=3600s -> major
        assert result["severity"] == "major"

    def test_critical_very_large_gap(self):
        from scripts.check_session_parquets import validate_session_df

        # Monday 2026-05-18 10:00 ET, gap of ~83 minutes
        base = pd.Timestamp("2026-05-18 10:00:00", tz="America/New_York")
        timestamps = (
            [base + pd.Timedelta(seconds=i) for i in range(60)]
            + [base + pd.Timedelta(seconds=5000 + i) for i in range(60)]
        )
        idx = pd.DatetimeIndex(timestamps)
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)

        result = validate_session_df(df, price_lo=20000, price_hi=35000)
        # gap ~4940s >= LARGE_GAP_THRESHOLD=3600s -> critical
        assert result["severity"] == "critical"

    def test_empty_df_is_critical(self):
        from scripts.check_session_parquets import validate_session_df

        result = validate_session_df(pd.DataFrame(), price_lo=20000, price_hi=35000)
        assert result["severity"] == "critical"

    def test_none_df_is_critical(self):
        from scripts.check_session_parquets import validate_session_df

        result = validate_session_df(None, price_lo=20000, price_hi=35000)
        assert result["severity"] == "critical"

    def test_maintenance_gap_ignored(self):
        from scripts.check_session_parquets import validate_session_df

        # Monday 2026-05-18: maintenance window 17:00-18:00 ET
        # Gap from 17:01 to 17:59 is within maintenance -> expected -> no unexpected gaps
        base_before = pd.Timestamp("2026-05-18 16:57:00", tz="America/New_York")
        base_after  = pd.Timestamp("2026-05-18 18:02:00", tz="America/New_York")
        timestamps = (
            [base_before + pd.Timedelta(seconds=i) for i in range(60)]
            + [base_after + pd.Timedelta(seconds=i) for i in range(60)]
        )
        idx = pd.DatetimeIndex(timestamps)
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)

        result = validate_session_df(df, price_lo=20000, price_hi=35000)
        assert result["unexpected_gaps"] == []
        assert result["severity"] == "ok"

    def test_weekend_gap_ignored(self):
        from scripts.check_session_parquets import validate_session_df

        # Friday close 17:00 ET (at/after CLOSE_T) -> Monday open 18:02 ET
        # gap_start must be >= 17:00 for in_fri_close to be True
        base_fri = pd.Timestamp("2026-05-15 17:00:00", tz="America/New_York")
        base_mon = pd.Timestamp("2026-05-18 18:02:00", tz="America/New_York")
        timestamps = (
            [base_fri + pd.Timedelta(seconds=i) for i in range(30)]
            + [base_mon + pd.Timedelta(seconds=i) for i in range(30)]
        )
        idx = pd.DatetimeIndex(timestamps)
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)

        result = validate_session_df(df, price_lo=20000, price_hi=35000)
        assert result["unexpected_gaps"] == []
        assert result["severity"] == "ok"

    def test_late_start_returns_late_start_hours(self):
        from scripts.check_session_parquets import validate_session_df

        base = pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(10)])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)

        expected_start = pd.Timestamp("2026-05-19 18:00:00", tz="America/New_York")
        result = validate_session_df(df, price_lo=20000, price_hi=35000,
                                     expected_session_start=expected_start)
        # 09:30 on May 20 - 18:00 on May 19 = 15.5 hours
        assert result["late_start_hours"] >= 15.0
        # validate_session_df itself doesn't escalate for late_start_hours
        assert result["severity"] == "ok"

    def test_on_time_start_zero_late_hours(self):
        from scripts.check_session_parquets import validate_session_df

        base = pd.Timestamp("2026-05-20 18:05:00", tz="America/New_York")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(10)])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)

        expected_start = pd.Timestamp("2026-05-20 18:00:00", tz="America/New_York")
        result = validate_session_df(df, price_lo=20000, price_hi=35000,
                                     expected_session_start=expected_start)
        assert result["late_start_hours"] < 0.1

    def test_no_expected_start_zero_late_hours(self):
        from scripts.check_session_parquets import validate_session_df

        base = pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(10)])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)

        result = validate_session_df(df, price_lo=20000, price_hi=35000,
                                     expected_session_start=None)
        assert result["late_start_hours"] == 0.0


# ---------------------------------------------------------------------------
# TestIsExpectedClosed
# ---------------------------------------------------------------------------

class TestIsExpectedClosed:
    def test_friday_close_expected(self):
        from scripts.check_session_parquets import _is_expected_closed

        gap_start = pd.Timestamp("2026-05-15 17:01:00", tz="America/New_York")
        gap_end   = pd.Timestamp("2026-05-15 23:00:00", tz="America/New_York")
        assert _is_expected_closed(gap_start, gap_end) is True

    def test_saturday_expected(self):
        from scripts.check_session_parquets import _is_expected_closed

        gap_start = pd.Timestamp("2026-05-16 12:00:00", tz="America/New_York")
        gap_end   = pd.Timestamp("2026-05-16 13:00:00", tz="America/New_York")
        assert _is_expected_closed(gap_start, gap_end) is True

    def test_sunday_before_18_expected(self):
        from scripts.check_session_parquets import _is_expected_closed

        gap_start = pd.Timestamp("2026-05-17 10:00:00", tz="America/New_York")
        gap_end   = pd.Timestamp("2026-05-17 11:00:00", tz="America/New_York")
        assert _is_expected_closed(gap_start, gap_end) is True

    def test_weekday_maint_expected(self):
        from scripts.check_session_parquets import _is_expected_closed

        # Monday 2026-05-19, maintenance window 17:01 -> 18:00 (< 75 min, ends at 18:00 ET)
        # The logic requires end_et.hour <= 18 AND end_et.minute <= 5
        gap_start = pd.Timestamp("2026-05-19 17:01:00", tz="America/New_York")
        gap_end   = pd.Timestamp("2026-05-19 18:00:00", tz="America/New_York")
        assert _is_expected_closed(gap_start, gap_end) is True

    def test_weekday_overnight_unexpected(self):
        from scripts.check_session_parquets import _is_expected_closed

        # Tuesday 2026-05-20 02:00 -> 03:00 — not a maintenance/closed window
        gap_start = pd.Timestamp("2026-05-20 02:00:00", tz="America/New_York")
        gap_end   = pd.Timestamp("2026-05-20 03:00:00", tz="America/New_York")
        assert _is_expected_closed(gap_start, gap_end) is False

    def test_maint_too_long_unexpected(self):
        from scripts.check_session_parquets import _is_expected_closed

        # Monday 2026-05-19, 17:00 -> 19:00 is 120 min > 75 min limit
        gap_start = pd.Timestamp("2026-05-19 17:00:00", tz="America/New_York")
        gap_end   = pd.Timestamp("2026-05-19 19:00:00", tz="America/New_York")
        assert _is_expected_closed(gap_start, gap_end) is False


# ---------------------------------------------------------------------------
# TestWriteAtomicAndBackup
# ---------------------------------------------------------------------------

class TestWriteAtomicAndBackup:
    def test_write_atomic_produces_correct_file(self, tmp_path):
        from scripts.check_session_parquets import write_atomic

        base = pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(3)])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)

        out_path = tmp_path / "test.parquet"
        write_atomic(df, out_path)

        assert out_path.exists()
        result = pd.read_parquet(out_path)
        assert len(result) == 3

    def test_write_atomic_no_tmp_left(self, tmp_path):
        from scripts.check_session_parquets import write_atomic

        base = pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(3)])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)

        out_path = tmp_path / "test.parquet"
        write_atomic(df, out_path)

        assert not (tmp_path / "test.parquet.tmp").exists()

    def test_backup_main_overwrites_bak(self, tmp_path):
        from scripts.check_session_parquets import backup_main

        main_path = tmp_path / "MNQ_1s.parquet"

        base = pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York")
        # Write 2-row df first
        idx2 = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(2)])
        df2 = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx2)
        df2.to_parquet(main_path)
        backup_main(main_path)

        # Now overwrite with 3-row df
        idx3 = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(3)])
        df3 = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx3)
        df3.to_parquet(main_path)
        backup_main(main_path)

        bak_path = tmp_path / "MNQ_1s.parquet.bak"
        assert bak_path.exists()
        result = pd.read_parquet(bak_path)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# TestProcessInstrumentSessionEnd
# ---------------------------------------------------------------------------

def _make_valid_session(bar_dir, inst="MNQ", n_rows=100, price=27000.0):
    """Write a valid session parquet to bar_dir."""
    idx = pd.DatetimeIndex([
        pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York") + pd.Timedelta(seconds=i)
        for i in range(n_rows)
    ])
    df = pd.DataFrame({
        "Open": price, "High": price + 10, "Low": price - 10,
        "Close": price, "Volume": 100.0,
    }, index=idx)
    fname = bar_dir / f"{inst}_1s_session_20260520.parquet"
    df.to_parquet(fname, use_dictionary=False)
    return fname, df


def _make_main_parquet(bar_dir, inst="MNQ"):
    main_df = pd.DataFrame(
        {"Open": [27000.], "High": [27010.], "Low": [26990.], "Close": [27005.], "Volume": [100.]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-05-19 09:30:00", tz="America/New_York")])
    )
    main_path = bar_dir / f"{inst}_1s.parquet"
    main_df.to_parquet(main_path)
    return main_path


class TestProcessInstrumentSessionEnd:
    def _call_process(self, bar_dir, inst="MNQ", conid=12345, mode="session-end",
                      dry_run=False, ib=None, extra_patches=None):
        from scripts.check_session_parquets import process_instrument

        patches = [
            patch("scripts.check_session_parquets.DATA_DIR", bar_dir),
            patch("data.parquet_maintenance.merge_session_1s_parquets"),
            patch("ib_insync.Contract", MagicMock()),
        ]
        if extra_patches:
            patches.extend(extra_patches)

        # Use contextlib-style manual entering
        entered = []
        mocks = []
        try:
            for p in patches:
                m = p.__enter__()
                entered.append(p)
                mocks.append(m)
            return process_instrument(
                inst, conid, f"{inst}_1s.parquet", f"{inst}_1s_session_*.parquet",
                mode, dry_run, ib or _make_ib_mock()
            ), mocks
        finally:
            for p in reversed(entered):
                p.__exit__(None, None, None)

    def test_ok_session_merges_and_backs_up(self, bar_dir):
        from scripts.check_session_parquets import process_instrument

        _make_valid_session(bar_dir)
        _make_main_parquet(bar_dir)

        with patch("scripts.check_session_parquets.DATA_DIR", bar_dir), \
             patch("data.parquet_maintenance.merge_session_1s_parquets") as mock_merge, \
             patch("ib_insync.Contract", MagicMock()), \
             patch("scripts.check_session_parquets.get_session_start_for_end_mode",
                   return_value=pd.Timestamp("2026-05-20 09:00:00", tz="America/New_York")):

            result = process_instrument(
                "MNQ", 12345, "MNQ_1s.parquet", "MNQ_1s_session_*.parquet",
                "session-end", False, _make_ib_mock()
            )

        assert result["merge_success"] is True
        assert result["backup_written"] is True
        assert mock_merge.called

    def test_minor_session_merges_as_is(self, bar_dir):
        from scripts.check_session_parquets import process_instrument

        # Build session df starting near expected_start (18:10 ET vs 18:00 ET = 10min late, OK)
        # with a 3-min (180s) gap in the middle -> minor severity
        base = pd.Timestamp("2026-05-18 18:10:00", tz="America/New_York")
        timestamps = (
            [base + pd.Timedelta(seconds=i) for i in range(300)]
            + [base + pd.Timedelta(seconds=480 + i) for i in range(60)]
        )
        idx = pd.DatetimeIndex(timestamps)
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)
        session_path = bar_dir / "MNQ_1s_session_20260518.parquet"
        df.to_parquet(session_path, use_dictionary=False)
        _make_main_parquet(bar_dir)

        with patch("scripts.check_session_parquets.DATA_DIR", bar_dir), \
             patch("data.parquet_maintenance.merge_session_1s_parquets"), \
             patch("ib_insync.Contract", MagicMock()), \
             patch("scripts.check_session_parquets.get_session_start_for_end_mode",
                   return_value=pd.Timestamp("2026-05-18 18:00:00", tz="America/New_York")):

            result = process_instrument(
                "MNQ", 12345, "MNQ_1s.parquet", "MNQ_1s_session_*.parquet",
                "session-end", False, _make_ib_mock()
            )

        assert result["action"] == "merge"
        assert result["merge_success"] is True

    def test_major_session_targeted_fill(self, bar_dir):
        from scripts.check_session_parquets import process_instrument

        # Build session starting near expected time (18:10 ET) with 30-min gap -> major severity
        base = pd.Timestamp("2026-05-18 18:10:00", tz="America/New_York")
        timestamps = (
            [base + pd.Timedelta(seconds=i) for i in range(60)]
            + [base + pd.Timedelta(seconds=2000 + i) for i in range(60)]
        )
        idx = pd.DatetimeIndex(timestamps)
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)
        session_path = bar_dir / "MNQ_1s_session_20260518.parquet"
        df.to_parquet(session_path, use_dictionary=False)
        _make_main_parquet(bar_dir)

        with patch("scripts.check_session_parquets.DATA_DIR", bar_dir), \
             patch("data.parquet_maintenance.merge_session_1s_parquets"), \
             patch("ib_insync.Contract", MagicMock()), \
             patch("scripts.check_session_parquets.get_session_start_for_end_mode",
                   return_value=pd.Timestamp("2026-05-18 18:00:00", tz="America/New_York")), \
             patch("scripts.check_session_parquets.targeted_fill", return_value=df):

            result = process_instrument(
                "MNQ", 12345, "MNQ_1s.parquet", "MNQ_1s_session_*.parquet",
                "session-end", False, _make_ib_mock()
            )

        assert result["action"] == "targeted_fill_then_merge"

    def test_critical_session_end_rebuilds(self, bar_dir):
        from scripts.check_session_parquets import process_instrument

        # 6% bad rows -> critical
        base = pd.Timestamp("2026-05-18 10:00:00", tz="America/New_York")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(100)])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)
        for i in range(6):
            df.iloc[i, df.columns.get_loc("High")] = 999.0
            df.iloc[i, df.columns.get_loc("Low")] = 989.0
            df.iloc[i, df.columns.get_loc("Close")] = 994.0
            df.iloc[i, df.columns.get_loc("Open")] = 994.0
        session_path = bar_dir / "MNQ_1s_session_20260518.parquet"
        df.to_parquet(session_path, use_dictionary=False)
        _make_main_parquet(bar_dir)

        # rebuild_session returns a valid df
        good_base = pd.Timestamp("2026-05-18 18:00:00", tz="America/New_York")
        good_idx = pd.DatetimeIndex([good_base + pd.Timedelta(seconds=i) for i in range(10)])
        good_df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=good_idx)

        with patch("scripts.check_session_parquets.DATA_DIR", bar_dir), \
             patch("data.parquet_maintenance.merge_session_1s_parquets"), \
             patch("ib_insync.Contract", MagicMock()), \
             patch("scripts.check_session_parquets.get_session_start_for_end_mode",
                   return_value=pd.Timestamp("2026-05-17 18:00:00", tz="America/New_York")), \
             patch("scripts.check_session_parquets.rebuild_session", return_value=good_df):

            result = process_instrument(
                "MNQ", 12345, "MNQ_1s.parquet", "MNQ_1s_session_*.parquet",
                "session-end", False, _make_ib_mock()
            )

        assert result["action"] == "rebuild_then_merge"

    def test_no_session_file_session_end_skip(self, bar_dir):
        from scripts.check_session_parquets import process_instrument

        # No session files
        with patch("scripts.check_session_parquets.DATA_DIR", bar_dir), \
             patch("ib_insync.Contract", MagicMock()):

            result = process_instrument(
                "MNQ", 12345, "MNQ_1s.parquet", "MNQ_1s_session_*.parquet",
                "session-end", False, None
            )

        assert result["action"] == "skip"
        assert result.get("merge_success") is None

    def test_dry_run_no_disk_writes(self, bar_dir):
        from scripts.check_session_parquets import process_instrument

        _make_valid_session(bar_dir)
        _make_main_parquet(bar_dir)

        files_before = set(bar_dir.glob("*.parquet"))

        with patch("scripts.check_session_parquets.DATA_DIR", bar_dir), \
             patch("data.parquet_maintenance.merge_session_1s_parquets"), \
             patch("ib_insync.Contract", MagicMock()), \
             patch("scripts.check_session_parquets.get_session_start_for_end_mode",
                   return_value=pd.Timestamp("2026-05-20 09:00:00", tz="America/New_York")):

            result = process_instrument(
                "MNQ", 12345, "MNQ_1s.parquet", "MNQ_1s_session_*.parquet",
                "session-end", True, _make_ib_mock()
            )

        files_after = set(bar_dir.glob("*.parquet"))
        assert files_before == files_after
        assert result["merge_success"] is None

    def test_late_start_escalates_to_rebuild(self, bar_dir):
        from scripts.check_session_parquets import process_instrument

        _make_valid_session(bar_dir)
        _make_main_parquet(bar_dir)

        # Session starts at 09:30, expected at 18:00 night before -> ~15.5h late -> critical
        good_base = pd.Timestamp("2026-05-19 18:00:00", tz="America/New_York")
        good_idx = pd.DatetimeIndex([good_base + pd.Timedelta(seconds=i) for i in range(10)])
        good_df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=good_idx)

        with patch("scripts.check_session_parquets.DATA_DIR", bar_dir), \
             patch("data.parquet_maintenance.merge_session_1s_parquets"), \
             patch("ib_insync.Contract", MagicMock()), \
             patch("scripts.check_session_parquets.get_session_start_for_end_mode",
                   return_value=pd.Timestamp("2026-05-19 18:00:00", tz="America/New_York")), \
             patch("scripts.check_session_parquets.rebuild_session", return_value=good_df):

            result = process_instrument(
                "MNQ", 12345, "MNQ_1s.parquet", "MNQ_1s_session_*.parquet",
                "session-end", False, _make_ib_mock()
            )

        assert result["action"] == "rebuild_then_merge"
        assert result["severity"] == "critical"


# ---------------------------------------------------------------------------
# TestProcessInstrumentOrchestratorStart
# ---------------------------------------------------------------------------

class TestProcessInstrumentOrchestratorStart:
    def test_no_session_file_gap_fills(self, bar_dir):
        from scripts.check_session_parquets import process_instrument

        _make_main_parquet(bar_dir)

        good_base = pd.Timestamp("2026-05-20 18:00:00", tz="America/New_York")
        good_idx = pd.DatetimeIndex([good_base + pd.Timedelta(seconds=i) for i in range(10)])
        good_df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=good_idx)

        with patch("scripts.check_session_parquets.DATA_DIR", bar_dir), \
             patch("data.parquet_maintenance.merge_session_1s_parquets"), \
             patch("ib_insync.Contract", MagicMock()), \
             patch("scripts.check_session_parquets.gap_fill_to_now", return_value=good_df):

            result = process_instrument(
                "MNQ", 12345, "MNQ_1s.parquet", "MNQ_1s_session_*.parquet",
                "orchestrator-start", False, _make_ib_mock()
            )

        assert result["action"] == "gap_fill_created_session"
        assert result.get("gap_fill_bars", 0) > 0

    def test_critical_orch_start_gap_fills_not_rebuilds(self, bar_dir):
        from scripts.check_session_parquets import process_instrument

        # Session with 6% bad rows -> critical, but orch-start uses gap_fill_then_merge
        base = pd.Timestamp("2026-05-18 10:00:00", tz="America/New_York")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in range(100)])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)
        for i in range(6):
            df.iloc[i, df.columns.get_loc("High")] = 999.0
            df.iloc[i, df.columns.get_loc("Low")] = 989.0
            df.iloc[i, df.columns.get_loc("Close")] = 994.0
            df.iloc[i, df.columns.get_loc("Open")] = 994.0
        session_path = bar_dir / "MNQ_1s_session_20260518.parquet"
        df.to_parquet(session_path, use_dictionary=False)
        _make_main_parquet(bar_dir)

        good_base = pd.Timestamp("2026-05-18 18:00:00", tz="America/New_York")
        good_idx = pd.DatetimeIndex([good_base + pd.Timedelta(seconds=i) for i in range(10)])
        good_df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=good_idx)

        with patch("scripts.check_session_parquets.DATA_DIR", bar_dir), \
             patch("data.parquet_maintenance.merge_session_1s_parquets"), \
             patch("ib_insync.Contract", MagicMock()), \
             patch("scripts.check_session_parquets.gap_fill_to_now", return_value=good_df):

            result = process_instrument(
                "MNQ", 12345, "MNQ_1s.parquet", "MNQ_1s_session_*.parquet",
                "orchestrator-start", False, _make_ib_mock()
            )

        assert result["action"] == "gap_fill_then_merge"


# ---------------------------------------------------------------------------
# TestMainEntryPoint
# ---------------------------------------------------------------------------

class TestMainEntryPoint:
    def test_main_outputs_valid_json(self, tmp_path):
        from scripts.check_session_parquets import main

        with patch("scripts.check_session_parquets.DATA_DIR", tmp_path), \
             patch("sys.argv", ["prog", "--mode", "session-end", "--dry-run"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                with pytest.raises(SystemExit):
                    main()

        output = captured.getvalue()
        result = json.loads(output)
        assert "mode" in result
        assert "instruments" in result
        assert "exit_code" in result

    def test_main_exit_code_0_when_no_sessions(self, tmp_path):
        from scripts.check_session_parquets import main

        with patch("scripts.check_session_parquets.DATA_DIR", tmp_path), \
             patch("sys.argv", ["prog", "--mode", "session-end", "--dry-run"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                with pytest.raises(SystemExit) as exc_info:
                    main()

        assert exc_info.value.code == 0

    def test_main_exit_code_1_when_fixed(self, tmp_path):
        from scripts.check_session_parquets import main

        # Create valid session file
        idx = pd.DatetimeIndex([
            pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York") + pd.Timedelta(seconds=i)
            for i in range(10)
        ])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)
        df.to_parquet(tmp_path / "MNQ_1s_session_20260520.parquet")

        with patch("scripts.check_session_parquets.DATA_DIR", tmp_path), \
             patch("sys.argv", ["prog", "--mode", "session-end", "--dry-run"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                with pytest.raises(SystemExit) as exc_info:
                    main()

        assert exc_info.value.code == 1

    def test_main_exit_code_2_when_unfixable(self, tmp_path):
        from scripts.check_session_parquets import main

        # Session file exists; mock process_instrument returning merge_success=False
        idx = pd.DatetimeIndex([
            pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York") + pd.Timedelta(seconds=i)
            for i in range(10)
        ])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=idx)
        df.to_parquet(tmp_path / "MNQ_1s_session_20260520.parquet")

        with patch("scripts.check_session_parquets.DATA_DIR", tmp_path), \
             patch("sys.argv", ["prog", "--mode", "session-end", "--dry-run"]), \
             patch("scripts.check_session_parquets.process_instrument",
                   return_value={"action": "merge", "severity": "critical",
                                 "merge_success": False, "backup_written": False}):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                with pytest.raises(SystemExit) as exc_info:
                    main()

        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# TestPromoteLiveToMain
# ---------------------------------------------------------------------------

class TestPromoteLiveToMain:
    """Covers promote_live_to_main(): the final live->main promotion step after a
    successful session-end merge. ACT_GLOBAL_DIR points paths.* at a tmp tree so the
    live/main dirs are isolated."""

    def _write_parquet(self, path: Path, close: float):
        idx = pd.DatetimeIndex([
            pd.Timestamp("2026-05-20 09:30:00", tz="America/New_York") + pd.Timedelta(seconds=i)
            for i in range(3)
        ])
        df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": close, "Volume": 100.0,
        }, index=idx)
        df.to_parquet(path)

    def test_promote_copies_and_backs_up(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path / "global"))
        import importlib
        import paths
        importlib.reload(paths)
        # Re-import the script module so its DATA_DIR / paths reference the patched env.
        import scripts.check_session_parquets as csp
        importlib.reload(csp)

        live_dir = paths.general_live_dir()
        main_dir = paths.general_main_dir()

        # Live has fresh (post-merge) parquets; main has a stale prior version.
        self._write_parquet(live_dir / "MNQ_1m.parquet", close=28000.0)
        self._write_parquet(live_dir / "MNQ_1s.parquet", close=28001.0)
        self._write_parquet(main_dir / "MNQ_1m.parquet", close=27000.0)  # stale prior main

        result = csp.promote_live_to_main()

        assert result.get("MNQ_1m.parquet") == "ok"
        assert result.get("MNQ_1s.parquet") == "ok"

        # main now reflects the live (promoted) data
        promoted_1m = pd.read_parquet(main_dir / "MNQ_1m.parquet")
        assert promoted_1m["Close"].iloc[0] == 28000.0
        # 1s file (no prior main) was created
        assert (main_dir / "MNQ_1s.parquet").exists()
        # prior main was backed up before being overwritten
        bak = pd.read_parquet(main_dir / "MNQ_1m.parquet.bak")
        assert bak["Close"].iloc[0] == 27000.0
        # no stray temp file left behind
        assert not (main_dir / "MNQ_1m.parquet.promote.tmp").exists()

    def test_promote_skips_missing_live_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path / "global"))
        import importlib
        import paths
        importlib.reload(paths)
        import scripts.check_session_parquets as csp
        importlib.reload(csp)

        live_dir = paths.general_live_dir()
        # Only MES_1m present in live; the others are absent and must be skipped silently.
        self._write_parquet(live_dir / "MES_1m.parquet", close=6000.0)

        result = csp.promote_live_to_main()

        assert result == {"MES_1m.parquet": "ok"}
        assert (paths.general_main_dir() / "MES_1m.parquet").exists()
        assert not (paths.general_main_dir() / "MNQ_1m.parquet").exists()


# ---------------------------------------------------------------------------
# TestCheck1mIncremental
# ---------------------------------------------------------------------------

def _make_1m_df(start, n, *, price=27000.0, step_min=1):
    """Build an n-row 1m OHLCV frame starting at `start` (NY tz)."""
    base = pd.Timestamp(start, tz="America/New_York")
    idx = pd.DatetimeIndex([base + pd.Timedelta(minutes=step_min * i) for i in range(n)])
    return pd.DataFrame({
        "Open": price, "High": price + 5, "Low": price - 5,
        "Close": price, "Volume": 100.0,
    }, index=idx)


def _write_1m(bar_dir, df, inst="MNQ"):
    """Write a 1m main parquet for `inst` into bar_dir; return its path."""
    path = bar_dir / f"{inst}_1m.parquet"
    df.to_parquet(path, use_dictionary=False)
    return path


def _append_1m(bar_dir, tail_df, inst="MNQ"):
    """Append tail_df to the existing 1m main parquet (concat + rewrite)."""
    path = bar_dir / f"{inst}_1m.parquet"
    existing = pd.read_parquet(path)
    combined = pd.concat([existing, tail_df])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined.to_parquet(path, use_dictionary=False)
    return path


def _run_check_1m(bar_dir, inst="MNQ", dry_run=False, **kwargs):
    from scripts.check_session_parquets import check_1m_parquet
    with patch("scripts.check_session_parquets.DATA_DIR", bar_dir):
        return check_1m_parquet(
            inst, f"{inst}_1m.parquet", f"{inst}_1s_session_*.parquet",
            dry_run, **kwargs,
        )


class TestCheck1mIncremental:
    def test_first_run_no_watermark_full_then_seeds(self, bar_dir):
        df = _make_1m_df("2026-05-18 18:00:00", 30)
        _write_1m(bar_dir, df)

        result = _run_check_1m(bar_dir)

        assert result["validation_scope"] == "full"
        assert result["full_reason"] == "no-watermark"
        sidecar = bar_dir / ".validation_state.json"
        assert sidecar.exists()
        state = json.loads(sidecar.read_text())
        assert state["MNQ_1m.parquet"]["validated_through"] == df.index[-1].isoformat()

    def test_second_run_clean_tail_incremental(self, bar_dir):
        df = _make_1m_df("2026-05-18 18:00:00", 30)
        _write_1m(bar_dir, df)
        _run_check_1m(bar_dir)  # seed

        tail = _make_1m_df("2026-05-18 18:30:00", 10)  # contiguous after bar 29 (18:29)
        _append_1m(bar_dir, tail)

        result = _run_check_1m(bar_dir)

        assert result["validation_scope"] == "incremental"
        assert result["tail_rows"] > 0
        assert result["validation"]["severity"] == "ok"
        state = json.loads((bar_dir / ".validation_state.json").read_text())
        assert state["MNQ_1m.parquet"]["validated_through"] == tail.index[-1].isoformat()

    def test_incremental_empty_tail_ok(self, bar_dir):
        df = _make_1m_df("2026-05-18 18:00:00", 30)
        _write_1m(bar_dir, df)
        _run_check_1m(bar_dir)  # seed
        before = json.loads((bar_dir / ".validation_state.json").read_text())

        result = _run_check_1m(bar_dir)  # no new bars

        assert result["validation_scope"] == "incremental"
        assert result["validation"]["severity"] == "ok"
        assert result["tail_rows"] == 0
        after = json.loads((bar_dir / ".validation_state.json").read_text())
        assert after == before  # watermark unchanged

    def test_incremental_bad_price_in_tail_surfaced(self, bar_dir):
        df = _make_1m_df("2026-05-18 18:00:00", 30)
        _write_1m(bar_dir, df)
        _run_check_1m(bar_dir)  # seed

        tail = _make_1m_df("2026-05-18 18:30:00", 5)
        tail.iloc[2, tail.columns.get_loc("Close")] = 0.0  # Close<=0 in tail
        _append_1m(bar_dir, tail)

        result = _run_check_1m(bar_dir)

        assert result["validation_scope"] == "incremental"
        assert result["validation"]["severity"] in ("minor", "major", "critical")
        assert result["validation"]["severity"] != "ok"

    def test_seam_overlap_flagged(self, bar_dir):
        df = _make_1m_df("2026-05-18 18:00:00", 30)
        _write_1m(bar_dir, df)
        _run_check_1m(bar_dir)  # seed; watermark = 18:29

        # Craft a tail whose first ts <= watermark by re-running with --since earlier,
        # forcing read_after to include a bar at/just before the watermark.
        tail = _make_1m_df("2026-05-18 18:30:00", 5)
        _append_1m(bar_dir, tail)

        # since=18:28 -> read_after returns bar 18:29 (== prev_last) -> overlap seam.
        result = _run_check_1m(bar_dir, since="2026-05-18 18:28:00")

        assert result.get("seam_issue") is not None
        assert result["validation"]["severity"] != "ok"

    def test_seam_unexpected_gap_flagged(self, bar_dir):
        df = _make_1m_df("2026-05-18 18:00:00", 30)
        _write_1m(bar_dir, df)
        _run_check_1m(bar_dir)  # seed; watermark = 18:29 (Monday)

        # Append a tail 2 hours later same weekday -> unexpected weekday hole at seam.
        tail = _make_1m_df("2026-05-18 20:30:00", 5)
        _append_1m(bar_dir, tail)

        result = _run_check_1m(bar_dir)

        assert result.get("seam_issue") is not None
        # A real missing-bars hole at the seam escalates to major so the watermark
        # is NOT advanced past the unfilled gap.
        assert result["validation"]["severity"] == "major"

    def test_seam_weekend_gap_ok(self, bar_dir):
        # Seed at a Friday close bar (2026-05-22 is a Friday). The seam's prev_last
        # must be >= 17:00 ET for _is_expected_closed's Friday-close branch.
        df = _make_1m_df("2026-05-22 16:56:00", 5)  # ends 17:00 Fri
        _write_1m(bar_dir, df)
        _run_check_1m(bar_dir)  # seed

        # Sunday reopen (2026-05-24 is a Sunday) at 18:00 -> expected weekend closure.
        tail = _make_1m_df("2026-05-24 18:00:00", 5)
        _append_1m(bar_dir, tail)

        result = _run_check_1m(bar_dir)

        assert result["validation_scope"] == "incremental"
        assert result.get("seam_issue") is None
        assert result["validation"]["severity"] == "ok"

    def test_rewritten_body_falls_back_to_full(self, bar_dir):
        df = _make_1m_df("2026-05-18 18:00:00", 30)
        _write_1m(bar_dir, df)
        _run_check_1m(bar_dir)  # seed

        # Rewrite the body so the first bar moves earlier (body-rewritten) and count grows.
        new_df = _make_1m_df("2026-05-18 17:00:00", 90)
        _write_1m(bar_dir, new_df)

        result = _run_check_1m(bar_dir)

        assert result["validation_scope"] == "full"
        assert result["full_reason"] in ("body-rewritten", "truncation")

    def test_interior_insert_falls_back_to_full(self, bar_dir):
        # first_bar unchanged + row_count grows, so needs_full_validation() alone would
        # NOT catch it — only the positional body-integrity guard (bar_at_position) does.
        df = _make_1m_df("2026-05-18 18:00:00", 30)
        _write_1m(bar_dir, df)
        _run_check_1m(bar_dir)  # seed; validated_through = 18:29, rows = 30

        # Insert an interior bar (same first bar, count -> 31). The bar at position 29
        # is now 18:28, no longer the watermark's 18:29 -> positional guard trips.
        interior = pd.DataFrame(
            {"Open": 27000.0, "High": 27005.0, "Low": 26995.0, "Close": 27000.0, "Volume": 100.0},
            index=pd.DatetimeIndex([pd.Timestamp("2026-05-18 18:14:30", tz="America/New_York")]),
        )
        new_df = pd.concat([df, interior]).sort_index()
        _write_1m(bar_dir, new_df)

        result = _run_check_1m(bar_dir)

        assert result["validation_scope"] == "full"
        assert result["full_reason"] == "body-rewritten"

    def test_full_validate_flag_forces_full(self, bar_dir):
        df = _make_1m_df("2026-05-18 18:00:00", 30)
        _write_1m(bar_dir, df)
        _run_check_1m(bar_dir)  # seed valid watermark

        result = _run_check_1m(bar_dir, force_full_validate=True)

        assert result["validation_scope"] == "full"
        assert result["full_reason"] == "forced-full"

    def test_dry_run_no_sidecar_write(self, bar_dir):
        df = _make_1m_df("2026-05-18 18:00:00", 30)
        _write_1m(bar_dir, df)

        result = _run_check_1m(bar_dir, dry_run=True)

        assert not (bar_dir / ".validation_state.json").exists()
        assert result["validation_scope"] == "full"

    def test_repair_sets_watermark(self, bar_dir):
        from scripts.check_session_parquets import check_1m_parquet

        # Valid backup alongside the (about-to-be-corrupt) main 1m.
        backup_df = _make_1m_df("2026-05-19 18:00:00", 10)
        backup_df.to_parquet(bar_dir / "MNQ_1m.parquet.bak", use_dictionary=False)

        # 1s session file with bars after the backup's last bar, so resample succeeds.
        sess_base = pd.Timestamp("2026-05-19 18:10:00", tz="America/New_York")
        sess_idx = pd.DatetimeIndex([sess_base + pd.Timedelta(seconds=i) for i in range(180)])
        sess_df = pd.DataFrame({
            "Open": 27000.0, "High": 27010.0, "Low": 26990.0,
            "Close": 27000.0, "Volume": 100.0,
        }, index=sess_idx)
        sess_df.to_parquet(bar_dir / "MNQ_1s_session_20260519.parquet", use_dictionary=False)

        # Corrupt the main 1m with garbage bytes.
        (bar_dir / "MNQ_1m.parquet").write_bytes(b"not a parquet file")

        with patch("scripts.check_session_parquets.DATA_DIR", bar_dir):
            result = check_1m_parquet(
                "MNQ", "MNQ_1m.parquet", "MNQ_1s_session_*.parquet", False,
            )

        assert result["repair_success"] is True
        sidecar = bar_dir / ".validation_state.json"
        assert sidecar.exists()
        repaired = pd.read_parquet(bar_dir / "MNQ_1m.parquet")
        state = json.loads(sidecar.read_text())
        assert state["MNQ_1m.parquet"]["validated_through"] == repaired.index[-1].isoformat()

    def test_report_has_scope_fields(self, bar_dir):
        df = _make_1m_df("2026-05-18 18:00:00", 30)
        _write_1m(bar_dir, df)

        result = _run_check_1m(bar_dir)

        assert "validation_scope" in result
        assert "validated_through" in result
