# tests/test_regression_run_dirs.py
# Wave 2.3 / 5.3 — per-run regression folders, info.md, the stable <date>/baseline/
# location, and the equivalence gate.
#
# No market data exists in this worktree, so run_backtest_v2 is mocked with canned
# trades/events. The equivalence gate is implemented as a LOCATION-INDEPENDENCE test:
# identical inputs run into two distinct regression dirs must yield byte-identical
# events/trades ledgers — the same "a path refactor is output-neutral" invariant the
# plan's byte-for-byte gate checks, made hermetic and data-free.

import pytest

import paths
import regression

_DATE = "2026-06-02"
_EVENTS = [
    {"kind": "session-start", "time": "2026-06-02T09:00:00", "price": 21000.0},
    {"kind": "entry", "direction": "long", "time": "2026-06-02T09:30:00", "price": 21010.0},
]
_TRADES = [
    {"entry_time": "2026-06-02T09:30:00", "entry_price": 21010.0, "direction": "long",
     "contracts": 2, "exit_time": "2026-06-02T10:00:00", "exit_price": 21030.0,
     "exit_reason": "tp", "pnl_points": 20.0, "pnl_dollars": 80.0},
]


def _fake_backtest(*_a, **_kw):
    return {"trades": _TRADES, "events": _EVENTS, "metrics": {"n_trades": 1, "total_pnl": 80.0}}


def _run_subdirs(reg_root):
    # Per-date run folders now live under <regression>/sessions/<date>/.
    date_dir = reg_root / "sessions" / _DATE
    return sorted(p for p in date_dir.glob("*") if p.is_dir() and p.name != "baseline")


def test_run_dir_holds_outputs_and_info_md(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_REGRESSION_DIR", str(tmp_path / "reg"))
    monkeypatch.setattr("backtest_smt.run_backtest_v2", _fake_backtest)

    regression.run_regression(dates=[_DATE], mode="1s", no_plot=True, skip_lock=True)

    runs = _run_subdirs(tmp_path / "reg")
    assert len(runs) == 1, "exactly one per-run folder expected"
    run = runs[0]
    # Folder name is HH-MM-SS (TH).
    assert len(run.name) == 8 and run.name.count("-") == 2
    assert (run / "events_1s.jsonl").exists()
    assert (run / "trades_1s.tsv").exists()

    info = (run / "info.md").read_text(encoding="utf-8")
    assert "mode: 1s" in info
    assert f"date: {_DATE}" in info
    assert "th_start:" in info
    assert "code_version:" in info


def test_baseline_lives_in_stable_baseline_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_REGRESSION_DIR", str(tmp_path / "reg"))
    monkeypatch.setattr("backtest_smt.run_backtest_v2", _fake_backtest)

    # record=True writes the baseline; it must land at sessions/<date>/baseline/, not a run folder.
    regression.run_regression(dates=[_DATE], mode="1s", no_plot=True, record=True)

    bl = tmp_path / "reg" / "sessions" / _DATE / "baseline"
    assert (bl / "baseline_events_1s.jsonl").exists()
    assert (bl / "baseline_trades_1s.tsv").exists()


def test_equivalence_location_independent(tmp_path, monkeypatch):
    """Same inputs, two different regression dirs → byte-identical ledgers."""
    monkeypatch.setattr("backtest_smt.run_backtest_v2", _fake_backtest)

    monkeypatch.setenv("ACT_REGRESSION_DIR", str(tmp_path / "regA"))
    regression.run_regression(dates=[_DATE], mode="1s", no_plot=True, skip_lock=True)
    run_a = _run_subdirs(tmp_path / "regA")[0]
    events_a = (run_a / "events_1s.jsonl").read_bytes()
    trades_a = (run_a / "trades_1s.tsv").read_bytes()

    monkeypatch.setenv("ACT_REGRESSION_DIR", str(tmp_path / "regB"))
    regression.run_regression(dates=[_DATE], mode="1s", no_plot=True, skip_lock=True)
    run_b = _run_subdirs(tmp_path / "regB")[0]
    events_b = (run_b / "events_1s.jsonl").read_bytes()
    trades_b = (run_b / "trades_1s.tsv").read_bytes()

    assert events_a == events_b, "events ledger must be byte-identical across locations"
    assert trades_a == trades_b, "trades ledger must be byte-identical across locations"
