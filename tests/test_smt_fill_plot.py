# tests/test_smt_fill_plot.py
# Verifies the regression / session plotters render an SMT-FILL mark (kind=="smt-div",
# type fill_a/fill_b, ref_name an fvg name) into the output HTML: a labelled "F" marker
# at the fill price, scoped to fvg_1hr_<HHMM>.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]

_FILL_EVENT = {
    "kind": "smt-div", "source": "v2", "timeframe": "1h",
    "type": "fill_a", "side": "bearish", "leader": "mes",
    "time": "2026-06-03T09:44:55-04:00",
    "ref_name": "fvg_20260601_2100_bear",
    "price": 30700.0, "mnq_div_price": None,
}


def _make_mnq_parquet(main_dir: Path, name: str) -> None:
    """Tiny MNQ parquet covering the session window the plot reads candles from."""
    idx = pd.date_range("2026-06-02 18:00", "2026-06-03 16:55",
                        freq="1min", tz="America/New_York")
    df = pd.DataFrame(
        {"Open": 30700.0, "High": 30710.0, "Low": 30690.0, "Close": 30700.0,
         "Volume": 100},
        index=idx,
    )
    main_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(main_dir / name)


def _assert_fill_in_html(html: str) -> None:
    # The fill renders as an smt-div mark scoped to its 1hr FVG.
    assert "fvg_1hr_2100" in html, "fill FVG scope missing from chart"
    # The label collapses fill_a/fill_b → 'F' (e.g. 1h↓F@fvg_1hr_2100); the arrow is
    # JSON-escaped (↓) in the embedded plotly data.
    assert ("1h\\u2193F@fvg_1hr_2100" in html or "1h↓F@fvg_1hr_2100" in html), \
        "fill 'F' label missing from chart"


def test_regression_plot_renders_fill_mark(tmp_path):
    """plot_regression.py renders the fill mark into chart_1s.html."""
    global_dir = tmp_path / "global"
    _make_mnq_parquet(global_dir / "general" / "main", "MNQ_1s.parquet")
    run_dir = global_dir / "regression" / "sessions" / "2026-06-03" / "run1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events_1s.jsonl").write_text(
        json.dumps(_FILL_EVENT, sort_keys=True) + "\n", encoding="utf-8")
    (run_dir / "trades_1s.tsv").write_text("", encoding="utf-8")

    env = dict(os.environ, ACT_GLOBAL_DIR=str(global_dir), PYTHONPATH=str(_ROOT))
    proc = subprocess.run(
        [sys.executable, "regression/plot_regression.py", "2026-06-03", "1s", str(run_dir)],
        cwd=str(_ROOT), capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"plot_regression failed: {proc.stderr}\n{proc.stdout}"
    chart = run_dir / "chart_1s.html"
    assert chart.exists(), f"no chart produced: {proc.stdout}"
    _assert_fill_in_html(chart.read_text(encoding="utf-8"))


def test_session_plot_renders_fill_mark(tmp_path):
    """plot_session.py renders the fill mark into its session chart HTML."""
    global_dir = tmp_path / "global"
    _make_mnq_parquet(global_dir / "general" / "main", "MNQ_1m.parquet")
    sess_dir = global_dir / "sessions" / "2026-06-03"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "events.jsonl").write_text(
        json.dumps(_FILL_EVENT, sort_keys=True) + "\n", encoding="utf-8")

    # ACT_NO_BROWSER=1 so plot_session never launches a real browser (BROWSER env is
    # ignored by webbrowser on Windows, so we use the script's own suppression hook).
    env = dict(os.environ, ACT_GLOBAL_DIR=str(global_dir),
               PYTHONPATH=str(_ROOT), ACT_NO_BROWSER="1")
    proc = subprocess.run(
        [sys.executable, "plot_session.py", "2026-06-03"],
        cwd=str(_ROOT), capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"plot_session failed: {proc.stderr}\n{proc.stdout}"
    charts = list(sess_dir.glob("chart_*.html"))
    assert charts, f"no session chart produced: {proc.stdout}"
    _assert_fill_in_html(charts[0].read_text(encoding="utf-8"))
