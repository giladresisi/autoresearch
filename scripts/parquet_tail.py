# scripts/parquet_tail.py
# Cheap pyarrow metadata reads + tail materialization for large parquet files.
# WHAT: index bounds, row count, "rows after a watermark", and "bar at a position"
#       computed from parquet metadata / row-group statistics, reading only the
#       trailing row-group(s) that could hold the requested rows.
# WHY:  enables O(tail) incremental validation in check_session_parquets so the
#       main 1m parquet (~860k rows) is not fully re-read on every session boundary
#       just to confirm a ~30-bar appended tail (GIL-15). Mirrors the cheap-read
#       precedent in data/parquet_maintenance.py (index-only pd.read_parquet).

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

_NY_TZ = "America/New_York"


def _to_ny(ts) -> "pd.Timestamp | None":
    """Coerce a value to a tz-aware America/New_York pd.Timestamp.

    WHY: row-group statistics return tz-aware Timestamps in UTC (same instant,
    different wall clock) and pandas indices may be naive; we always present NY.
    """
    if ts is None:
        return None
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        # Naive -> assume it is already NY wall-clock (matches how these are written).
        return ts.tz_localize(_NY_TZ)
    return ts.tz_convert(_NY_TZ)


def _index_col_name(pf: pq.ParquetFile) -> str:
    """Resolve the column that stores the DatetimeIndex.

    WHY: pandas writes a DatetimeIndex as a regular column (typically
    '__index_level_0__', or the index's name). Read the pandas metadata's
    'index_columns' to find it robustly instead of assuming a fixed position.
    Falls back to the last schema column (pandas appends the index column last).
    """
    schema = pf.schema_arrow
    meta = schema.metadata or {}
    pandas_meta = meta.get(b"pandas")
    if pandas_meta:
        try:
            index_columns = json.loads(pandas_meta).get("index_columns") or []
            for col in index_columns:
                # index_columns entries are str names (range-index entries are dicts).
                if isinstance(col, str) and col in schema.names:
                    return col
        except (ValueError, KeyError):
            pass
    return schema.names[-1]


def _index_col_position(pf: pq.ParquetFile, col_name: str) -> int:
    return pf.schema_arrow.names.index(col_name)


def _df_from_table(tbl) -> pd.DataFrame:
    """Convert an arrow table (read with pandas metadata) to a NY-indexed frame."""
    df = tbl.to_pandas()
    # to_pandas() restores the DatetimeIndex from pandas metadata; normalize tz.
    if df.index.tz is None:
        df.index = df.index.tz_localize(_NY_TZ)
    else:
        df.index = df.index.tz_convert(_NY_TZ)
    return df


def index_bounds(path: Path) -> "tuple[pd.Timestamp | None, pd.Timestamp | None]":
    """Return (first_ts, last_ts) of the index, tz-aware America/New_York.

    Uses row-group statistics (no full materialization) when available; falls back
    to an index-only read. Returns (None, None) if missing/empty/unreadable.
    """
    path = Path(path)
    if not path.exists():
        return (None, None)
    try:
        pf = pq.ParquetFile(path)
        md = pf.metadata
        if md.num_rows == 0:
            return (None, None)
        col_name = _index_col_name(pf)
        col_pos = _index_col_position(pf, col_name)
        first = last = None
        for i in range(md.num_row_groups):
            stats = md.row_group(i).column(col_pos).statistics
            if stats is None or not stats.has_min_max:
                first = last = None
                break
            lo, hi = _to_ny(stats.min), _to_ny(stats.max)
            first = lo if first is None or lo < first else first
            last = hi if last is None or hi > last else last
        if first is not None and last is not None:
            return (first, last)
        # Fallback: index-only read (precedent parquet_maintenance.py:91).
        idx_only = pd.read_parquet(path, columns=[])
        if len(idx_only.index) == 0:
            return (None, None)
        return (_to_ny(idx_only.index[0]), _to_ny(idx_only.index[-1]))
    except Exception:
        return (None, None)


def row_count(path: Path) -> int:
    """Return the number of rows via parquet metadata; 0 if missing/unreadable."""
    path = Path(path)
    if not path.exists():
        return 0
    try:
        return pq.ParquetFile(path).metadata.num_rows
    except Exception:
        return 0


def read_after(path: Path, watermark: pd.Timestamp) -> pd.DataFrame:
    """Return only rows with index > watermark, reading minimal trailing row-groups.

    Selects row-groups whose max timestamp could exceed the watermark using
    row-group statistics; if stats are unavailable, reads all groups (correctness
    over minimization). The returned frame is tz-aware NY, sorted, deduplicated
    (keep last). Empty frame if nothing is past the watermark or file is missing.
    """
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    try:
        pf = pq.ParquetFile(path)
        md = pf.metadata
        if md.num_rows == 0:
            return pd.DataFrame()
        wm = _to_ny(watermark)
        col_name = _index_col_name(pf)
        col_pos = _index_col_position(pf, col_name)

        keep = []
        for i in range(md.num_row_groups):
            stats = md.row_group(i).column(col_pos).statistics
            # Include the group if its max could exceed the watermark, or if stats
            # are missing (then we cannot prove it can be skipped).
            if stats is None or not stats.has_min_max or _to_ny(stats.max) > wm:
                keep.append(i)
        if not keep:
            return pd.DataFrame()

        tbl = pf.read_row_groups(keep)
        df = _df_from_table(tbl)
        df = df[df.index > wm].sort_index()
        return df[~df.index.duplicated(keep="last")]
    except Exception:
        return pd.DataFrame()


def bar_at_position(path: Path, pos: int) -> "pd.Timestamp | None":
    """Return the index timestamp at 0-based row position `pos`, tz-aware NY.

    Reads only the row-group containing `pos` (walks row_group(i).num_rows to
    locate it). Returns None if out of range or the file is missing/unreadable.
    """
    path = Path(path)
    if not path.exists() or pos < 0:
        return None
    try:
        pf = pq.ParquetFile(path)
        md = pf.metadata
        if pos >= md.num_rows:
            return None
        col_name = _index_col_name(pf)
        # Walk row-groups to find the one holding `pos`.
        cum = 0
        for i in range(md.num_row_groups):
            n = md.row_group(i).num_rows
            if pos < cum + n:
                local = pos - cum
                tbl = pf.read_row_group(i, columns=[col_name])
                col = tbl.column(0)
                val = col[local].as_py()
                return _to_ny(val)
            cum += n
        return None
    except Exception:
        return None
