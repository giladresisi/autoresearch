# tests/test_seed_1s_parquet.py
# Unit tests for scripts/seed_1s_parquet.py
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


def _make_df(ts: str) -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": [100.0], "High": [101.0], "Low": [99.0], "Close": [100.5], "Volume": [500.0]},
        index=pd.DatetimeIndex([pd.Timestamp(ts, tz="America/New_York")]),
    )


def _run_main(args, tmp_path):
    """Import and run main() with the given arg list and BAR_DATA_DIR pointed at tmp_path."""
    import importlib
    import scripts.seed_1s_parquet as seed_mod
    importlib.reload(seed_mod)

    with patch.object(seed_mod, "BAR_DATA_DIR", tmp_path), \
         patch("sys.argv", ["seed_1s_parquet.py"] + args):
        seed_mod.main()


def test_seed_dry_run_prints_without_writing(tmp_path):
    """--dry-run must not create any parquet files."""
    with patch.dict("os.environ", {"DATABENTO_API_KEY": "test-key"}):
        mock_source = MagicMock()
        with patch("data.sources.DatabentSource", return_value=mock_source):
            _run_main(["--dry-run"], tmp_path)

    assert not (tmp_path / "MNQ_1s.parquet").exists()
    assert not (tmp_path / "MES_1s.parquet").exists()
    mock_source.fetch.assert_not_called()


def test_seed_creates_parquets_for_both_instruments(tmp_path):
    """Without --dry-run, parquets must be created for MNQ and MES."""
    df = _make_df("2026-05-01 09:30:00")
    with patch.dict("os.environ", {"DATABENTO_API_KEY": "test-key"}):
        mock_source = MagicMock()
        mock_source.fetch.return_value = df
        with patch("data.sources.DatabentSource", return_value=mock_source):
            _run_main([], tmp_path)

    assert (tmp_path / "MNQ_1s.parquet").exists()
    assert (tmp_path / "MES_1s.parquet").exists()


def test_seed_resumes_from_last_bar(tmp_path):
    """If a parquet already exists, fetch must be called with start reflecting last_bar + 1s."""
    last_ts = pd.Timestamp("2026-05-03 10:00:00", tz="America/New_York")
    existing = _make_df(last_ts.isoformat())
    existing.to_parquet(tmp_path / "MNQ_1s.parquet")
    existing.to_parquet(tmp_path / "MES_1s.parquet")

    expected_start_utc = (last_ts + pd.Timedelta(seconds=1)).tz_convert("UTC").isoformat()

    new_df = _make_df("2026-05-03 10:00:01")
    with patch.dict("os.environ", {"DATABENTO_API_KEY": "test-key"}):
        mock_source = MagicMock()
        mock_source.fetch.return_value = new_df
        with patch("data.sources.DatabentSource", return_value=mock_source):
            _run_main([], tmp_path)

    # First call is for MNQ — check the start argument
    first_call_start = mock_source.fetch.call_args_list[0].args[1]
    assert first_call_start == expected_start_utc


def test_seed_script_is_runnable(tmp_path):
    """seed_1s_parquet.py --dry-run must exit 0 from the CLI."""
    automation_root = Path(__file__).parent.parent
    env = {"DATABENTO_API_KEY": "test-key", "BAR_DATA_DIR": str(tmp_path)}
    import os
    full_env = {**os.environ, **env}
    result = subprocess.run(
        ["uv", "run", "python", "scripts/seed_1s_parquet.py", "--dry-run"],
        cwd=str(automation_root),
        env=full_env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
