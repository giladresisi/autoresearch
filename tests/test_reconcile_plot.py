# tests/test_reconcile_plot.py
# Light Layer-3 check (GIL-36): the session + regression plotters render the new
# reconcile-stop-placed / reconcile-flat events without crashing, at their price, with the
# distinct reconcile marker symbol present in the output HTML.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]

_RECON_STOP = {
    "kind": "reconcile-stop-placed", "direction": "long",
    "time": "2026-06-03T10:11:30-04:00",
    "price": 30346.25, "fill": 30366.25, "intended_stop": 30368.75,
    "reason": "reconcile-rejected-sl",
}
_RECON_FLAT = {
    "kind": "reconcile-flat", "direction": "long",
    "time": "2026-06-03T20:00:30-04:00",
    "entry_price": 30400.0, "reason": "reconcile-entry-rejected",
}


def _make_mnq_parquet(main_dir: Path, name: str) -> None:
    idx = pd.date_range("2026-06-02 18:00", "2026-06-03 16:55",
                        freq="1min", tz="America/New_York")
    df = pd.DataFrame(
        {"Open": 30360.0, "High": 30410.0, "Low": 30340.0, "Close": 30360.0,
         "Volume": 100},
        index=idx,
    )
    main_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(main_dir / name)


def test_session_plot_renders_reconcile_events(tmp_path):
    global_dir = tmp_path / "global"
    _make_mnq_parquet(global_dir / "general" / "main", "MNQ_1m.parquet")
    sess_dir = global_dir / "sessions" / "2026-06-03"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "events.jsonl").write_text(
        json.dumps(_RECON_STOP, sort_keys=True) + "\n"
        + json.dumps(_RECON_FLAT, sort_keys=True) + "\n",
        encoding="utf-8")

    env = dict(os.environ, ACT_GLOBAL_DIR=str(global_dir),
               PYTHONPATH=str(_ROOT), ACT_NO_BROWSER="1")
    proc = subprocess.run(
        [sys.executable, "plot_session.py", "2026-06-03"],
        cwd=str(_ROOT), capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"plot_session failed: {proc.stderr}\n{proc.stdout}"
    charts = list(sess_dir.glob("chart_*.html"))
    assert charts, f"no session chart produced: {proc.stdout}"
    html = charts[0].read_text(encoding="utf-8")
    assert "reconcile stop placed" in html, "reconcile-stop-placed trace missing"
    assert "reconcile flat" in html, "reconcile-flat trace missing"
    assert "hexagram" in html, "reconcile marker symbol missing"


def test_regression_plot_renders_reconcile_events(tmp_path):
    global_dir = tmp_path / "global"
    _make_mnq_parquet(global_dir / "general" / "main", "MNQ_1s.parquet")
    run_dir = global_dir / "regression" / "sessions" / "2026-06-03" / "run1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events_1s.jsonl").write_text(
        json.dumps(_RECON_STOP, sort_keys=True) + "\n"
        + json.dumps(_RECON_FLAT, sort_keys=True) + "\n",
        encoding="utf-8")
    (run_dir / "trades_1s.tsv").write_text("", encoding="utf-8")

    env = dict(os.environ, ACT_GLOBAL_DIR=str(global_dir), PYTHONPATH=str(_ROOT))
    proc = subprocess.run(
        [sys.executable, "regression/plot_regression.py", "2026-06-03", "1s", str(run_dir)],
        cwd=str(_ROOT), capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"plot_regression failed: {proc.stderr}\n{proc.stdout}"
    chart = run_dir / "chart_1s.html"
    assert chart.exists(), f"no chart produced: {proc.stdout}"
    html = chart.read_text(encoding="utf-8")
    assert "reconcile stop placed" in html, "reconcile-stop-placed trace missing"
    assert "reconcile flat" in html, "reconcile-flat trace missing"
    assert "hexagram" in html, "reconcile marker symbol missing"
