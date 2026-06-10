# tests/test_parquet_tail.py
# Tests for scripts/parquet_tail.py (cheap pyarrow metadata + tail reads).
from __future__ import annotations

import pandas as pd
import pytest


def _make_df(n=200, freq="1s", start="2026-05-20 09:30:00"):
    idx = pd.date_range(start=start, periods=n, freq=freq, tz="America/New_York")
    # Drop the DatetimeIndex.freq: parquet round-trips do not preserve it, so the
    # in-memory expected frame must match the freq-less read-back frame exactly.
    idx = pd.DatetimeIndex(idx.values, tz="America/New_York")
    return pd.DataFrame(
        {
            "Open": 27000.0,
            "High": 27010.0,
            "Low": 26990.0,
            "Close": 27000.0,
            "Volume": 100.0,
        },
        index=idx,
    )


def _write(df, path, row_group_size=None):
    if row_group_size is not None:
        df.to_parquet(path, row_group_size=row_group_size)
    else:
        df.to_parquet(path)
    return path


def test_index_bounds_multi_rowgroup(tmp_path):
    from scripts.parquet_tail import index_bounds

    df = _make_df(n=200)
    path = _write(df, tmp_path / "m.parquet", row_group_size=50)  # >=4 row groups
    first, last = index_bounds(path)
    assert first == df.index[0]
    assert last == df.index[-1]


def test_index_bounds_missing_file(tmp_path):
    from scripts.parquet_tail import index_bounds

    first, last = index_bounds(tmp_path / "does_not_exist.parquet")
    assert first is None
    assert last is None


def test_row_count_matches(tmp_path):
    from scripts.parquet_tail import row_count

    df = _make_df(n=137)
    path = _write(df, tmp_path / "m.parquet", row_group_size=50)
    assert row_count(path) == len(df)


def test_read_after_returns_only_tail(tmp_path):
    from scripts.parquet_tail import read_after

    df = _make_df(n=200)
    path = _write(df, tmp_path / "m.parquet", row_group_size=50)
    wm = df.index[120]
    result = read_after(path, wm)
    expected = df[df.index > wm]
    pd.testing.assert_frame_equal(result, expected)


def test_read_after_watermark_at_or_past_last(tmp_path):
    from scripts.parquet_tail import read_after

    df = _make_df(n=200)
    path = _write(df, tmp_path / "m.parquet", row_group_size=50)
    # exactly last
    assert read_after(path, df.index[-1]).empty
    # later than last
    assert read_after(path, df.index[-1] + pd.Timedelta(hours=1)).empty


def test_read_after_reads_minimal_rowgroups(tmp_path):
    from scripts.parquet_tail import read_after

    df = _make_df(n=200)
    path = _write(df, tmp_path / "m.parquet", row_group_size=50)
    # watermark inside the LAST row-group (rows 150..199)
    wm = df.index[180]
    result = read_after(path, wm)
    expected = df[df.index > wm]
    pd.testing.assert_frame_equal(result, expected)


def test_bar_at_position_correct(tmp_path):
    from scripts.parquet_tail import bar_at_position

    df = _make_df(n=200)
    path = _write(df, tmp_path / "m.parquet", row_group_size=50)
    for k in [0, 1, 49, 50, 51, 123, 199]:
        assert bar_at_position(path, k) == df.index[k]
    # out of range
    assert bar_at_position(path, 200) is None
    assert bar_at_position(path, -1) is None


def test_single_rowgroup_file(tmp_path):
    from scripts.parquet_tail import (
        bar_at_position,
        index_bounds,
        read_after,
        row_count,
    )

    df = _make_df(n=80)
    path = _write(df, tmp_path / "single.parquet")  # default single row group

    first, last = index_bounds(path)
    assert first == df.index[0]
    assert last == df.index[-1]
    assert row_count(path) == len(df)

    wm = df.index[40]
    pd.testing.assert_frame_equal(read_after(path, wm), df[df.index > wm])
    assert bar_at_position(path, 10) == df.index[10]


def test_tz_preserved(tmp_path):
    from scripts.parquet_tail import index_bounds, read_after

    df = _make_df(n=200)
    path = _write(df, tmp_path / "m.parquet", row_group_size=50)

    tail = read_after(path, df.index[100])
    assert tail.index.tz is not None
    assert str(tail.index.tz) == "America/New_York"

    first, last = index_bounds(path)
    assert str(first.tz) == "America/New_York"
    assert str(last.tz) == "America/New_York"
