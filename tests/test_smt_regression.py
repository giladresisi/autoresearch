# tests/test_smt_regression.py
# Tests for regression.py: parser, pass/fail diffs, update-baseline, CLI exit codes.

import json
import subprocess
import sys
from pathlib import Path

import pytest

import regression


# ── Parser tests ──────────────────────────────────────────────────────────────

def test_parser_strips_comments_and_ranges(tmp_path):
    md = tmp_path / "regression.md"
    md.write_text(
        "2026-01-08\n"
        "2026-02-15:2026-02-17  # range\n"
        "# skip\n"
        "\n"
        "2026-03-12\n",
        encoding="utf-8",
    )
    result = regression._parse_regression_md(str(md))
    assert result == [
        "2026-01-08",
        "2026-02-15",
        "2026-02-16",
        "2026-02-17",
        "2026-03-12",
    ]


# ── Regression pass/fail tests ─────────────────────────────────────────────────

def _write_baseline_pair(reg_dir: Path, events: list[dict], trades: list[dict]) -> None:
    reg_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, sort_keys=True) for e in events]
    (reg_dir / "baseline_events.jsonl").write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )
    if trades:
        headers = list(trades[0].keys())
        rows = ["\t".join(str(t.get(h, "")) for h in headers) for t in trades]
        (reg_dir / "baseline_trades.tsv").write_text(
            "\t".join(headers) + "\n" + "\n".join(rows) + "\n", encoding="utf-8"
        )
    else:
        (reg_dir / "baseline_trades.tsv").write_text("", encoding="utf-8")


def _md_with_date(tmp_path: Path, date: str = "2025-11-14") -> Path:
    md = tmp_path / "regression.md"
    md.write_text(f"{date}\n", encoding="utf-8")
    return md


@pytest.fixture()
def real_parquet_available():
    """Skip test if real parquet data is not present."""
    from pathlib import Path as _P
    if not _P("data/MNQ_1m.parquet").exists() or not _P("data/MES_1m.parquet").exists():
        pytest.skip("Real parquet data not available")


@pytest.fixture(autouse=True)
def _redirect_state(tmp_path, monkeypatch):
    """Redirect smt_state paths so regression writes go to tmp_path."""
    import smt_state
    monkeypatch.setattr(smt_state, "DATA_DIR",        tmp_path)
    monkeypatch.setattr(smt_state, "GLOBAL_PATH",     tmp_path / "global.json")
    monkeypatch.setattr(smt_state, "DAILY_PATH",      tmp_path / "daily.json")
    monkeypatch.setattr(smt_state, "HYPOTHESIS_PATH", tmp_path / "hypothesis.json")
    monkeypatch.setattr(smt_state, "POSITION_PATH",   tmp_path / "position.json")
    # Also redirect regression output dir by monkeypatching Path("data") references inside regression.py
    # We do this by patching run_regression's internal data dir reference via a fixture that
    # sets the working data path. Since regression.py uses Path("data") directly, we need to
    # patch it at the module level.
    monkeypatch.chdir(tmp_path)
    # Copy real parquets into tmp_path/data if they exist
    real_data = Path(__file__).parent.parent / "data"
    tmp_data = tmp_path / "data"
    tmp_data.mkdir(exist_ok=True)
    for parquet in ["MNQ_1m.parquet", "MES_1m.parquet"]:
        src = real_data / parquet
        if src.exists():
            import shutil
            shutil.copy2(src, tmp_data / parquet)
    # Copy futures_manifest.json
    import os
    futures_cache = os.path.join(
        os.path.expanduser("~"), ".cache", "autoresearch", "futures_data"
    )
    manifest_src = Path(futures_cache) / "futures_manifest.json"
    if manifest_src.exists():
        pass  # conftest.py bootstrap already handles this


def test_run_regression_pass_when_baselines_match(tmp_path, real_parquet_available):
    md = _md_with_date(tmp_path)
    # First update to create baselines
    regression.run_regression(str(md), update_baseline=True)
    # Then run and compare — should match
    results = regression.run_regression(str(md))
    assert "2025-11-14" in results
    r = results["2025-11-14"]
    assert r["events_match"] is True
    assert r["trades_match"] is True


def test_run_regression_fail_when_events_differ(tmp_path, real_parquet_available):
    md = _md_with_date(tmp_path)
    # Create baselines
    regression.run_regression(str(md), update_baseline=True)
    # Corrupt the baseline events file
    bl_events = tmp_path / "data" / "regression" / "2025-11-14" / "baseline_events.jsonl"
    if bl_events.exists():
        original = bl_events.read_text(encoding="utf-8")
        bl_events.write_text("CORRUPTED\n" + original, encoding="utf-8")
    else:
        # Create a fake baseline with wrong content
        bl_events.parent.mkdir(parents=True, exist_ok=True)
        bl_events.write_text('{"kind":"fake"}\n', encoding="utf-8")
    # Run should now detect mismatch
    results = regression.run_regression(str(md))
    assert results["2025-11-14"]["events_match"] is False


def test_run_regression_fail_when_trades_differ(tmp_path, real_parquet_available):
    md = _md_with_date(tmp_path)
    # Create baselines
    regression.run_regression(str(md), update_baseline=True)
    # Corrupt the baseline trades file
    bl_trades = tmp_path / "data" / "regression" / "2025-11-14" / "baseline_trades.tsv"
    if bl_trades.exists():
        bl_trades.write_text("CORRUPTED HEADER\nrow1\n", encoding="utf-8")
    else:
        bl_trades.parent.mkdir(parents=True, exist_ok=True)
        bl_trades.write_text("fake\tcolumns\nwrong\tdata\n", encoding="utf-8")
    results = regression.run_regression(str(md))
    assert results["2025-11-14"]["trades_match"] is False


def test_update_baseline_overwrites_and_skips_diff(tmp_path, real_parquet_available):
    md = _md_with_date(tmp_path)
    # Create initial baselines
    r1 = regression.run_regression(str(md), update_baseline=True)
    assert r1["2025-11-14"].get("updated") is True
    # Call again — should update again (no diff performed)
    r2 = regression.run_regression(str(md), update_baseline=True)
    assert r2["2025-11-14"].get("updated") is True
    # Verify baseline files exist
    bl_events = tmp_path / "data" / "regression" / "2025-11-14" / "baseline_events.jsonl"
    bl_trades  = tmp_path / "data" / "regression" / "2025-11-14" / "baseline_trades.tsv"
    assert bl_events.exists()
    assert bl_trades.exists()


# ── CLI exit code tests ─────────────────────────────────────────────────────────

def test_cli_exit_code_zero_on_pass(tmp_path, real_parquet_available):
    md = _md_with_date(tmp_path)
    # Update baselines first
    regression.run_regression(str(md), update_baseline=True)
    # Run CLI
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "regression.py"),
         "--regression-md", str(md)],
        capture_output=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, f"stdout: {result.stdout.decode()}\nstderr: {result.stderr.decode()}"


def test_cli_exit_code_one_on_fail(tmp_path, real_parquet_available):
    md = _md_with_date(tmp_path)
    # Update baselines first
    regression.run_regression(str(md), update_baseline=True)
    # Corrupt baseline events
    bl_events = tmp_path / "data" / "regression" / "2025-11-14" / "baseline_events.jsonl"
    if not bl_events.exists():
        bl_events.parent.mkdir(parents=True, exist_ok=True)
        bl_events.write_text('{"kind":"fake"}\n', encoding="utf-8")
    else:
        bl_events.write_text("CORRUPTED\n", encoding="utf-8")
    # CLI should exit 1
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "regression.py"),
         "--regression-md", str(md)],
        capture_output=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 1, f"stdout: {result.stdout.decode()}\nstderr: {result.stderr.decode()}"
