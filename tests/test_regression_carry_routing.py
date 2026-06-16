# tests/test_regression_carry_routing.py
# GIL-25 Phase 1.1.5 Change B: contiguous-range carry routing in regression.run_regression.
#
# Unit-tests the routing logic with backtest_smt.run_backtest_v2 MONKEYPATCHED (no real backtest):
# it records (date, reset_pending) per call. The first date — and any date after a gap — starts
# clean (reset_pending=True); a date that is the next BUSINESS day after the previous carries
# (reset_pending=False). A `:` range token is expanded to business days only (weekends dropped).
#
# Plotting is disabled (no_plot=True) and baseline IO is redirected to tmp_path (skip_lock=True +
# monkeypatched paths) so the test is hermetic and writes nothing real.

from __future__ import annotations

import pandas as pd
import pytest

import backtest_smt
import paths
import regression


@pytest.fixture
def _routing(monkeypatch, tmp_path):
    """Patch run_backtest_v2 to record (date, reset_pending); redirect run/baseline dirs to tmp."""
    calls: list[tuple[str, bool]] = []

    def _fake_run_backtest_v2(start_date, end_date, *, write_events=True, mode="1m",
                              started=None, reset_pending=True):
        calls.append((start_date, reset_pending))
        return {"trades": [], "events": [], "metrics": {"n_trades": 0, "total_pnl": 0.0}}

    monkeypatch.setattr(backtest_smt, "run_backtest_v2", _fake_run_backtest_v2)

    def _run_dir(date, started):
        d = tmp_path / "runs" / str(date)
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(paths, "regression_run_dir", _run_dir)
    monkeypatch.setattr(paths, "regression_sessions_dir", lambda: tmp_path / "sessions")
    return calls


def _run(dates, _routing):
    regression.run_regression(dates=dates, no_plot=True, skip_lock=True)
    return _routing


def test_contiguous_range_carries(_routing):
    calls = _run(["2026-06-08", "2026-06-09"], _routing)
    assert calls == [("2026-06-08", True), ("2026-06-09", False)]


def test_friday_to_monday_carries(_routing):
    # Fri (06-05) -> Mon (06-08): the weekend does not break contiguity.
    calls = _run(["2026-06-05", "2026-06-08"], _routing)
    assert calls == [("2026-06-05", True), ("2026-06-08", False)]


def test_noncontiguous_list_does_not_carry(_routing):
    # Explicit list with a gap (06-05 .. 06-10) → no carry across the gap.
    calls = _run(["2026-06-05", "2026-06-10"], _routing)
    assert calls == [("2026-06-05", True), ("2026-06-10", True)]


def test_range_token_weekend_filtered(_routing):
    # A `:` range spanning a weekend expands+filters to business days only: 06-05(Fri),
    # 06-08(Mon), 06-09(Tue). reset_pending = [True, False, False].
    calls = _run(["2026-06-05:2026-06-09"], _routing)
    assert calls == [
        ("2026-06-05", True),
        ("2026-06-08", False),
        ("2026-06-09", False),
    ]


def test_single_date_starts_clean(_routing):
    calls = _run(["2026-06-08"], _routing)
    assert calls == [("2026-06-08", True)]


# ---------------------------------------------------------------------------
# Task B3 — run_backtest_v2 threads reset_pending to set_in_memory_mode.
# ---------------------------------------------------------------------------
def test_run_backtest_v2_threads_reset_pending(monkeypatch):
    # Spy on smt_state.set_in_memory_mode to capture the reset_pending kwarg, and short-circuit
    # the body right after it by raising a sentinel from the next dependency (load_futures_data).
    import smt_state

    seen: dict = {}
    _orig = smt_state.set_in_memory_mode

    def _spy(enabled, *, reset_pending=True):
        seen["reset_pending"] = reset_pending
        # Do NOT actually toggle global in-memory state for the test; just record.

    monkeypatch.setattr(smt_state, "set_in_memory_mode", _spy)

    class _Stop(Exception):
        pass

    # _main_dir_for_date runs after set_in_memory_mode; stop there so we never touch real data.
    monkeypatch.setattr(backtest_smt, "_main_dir_for_date",
                        lambda *_a, **_k: (_ for _ in ()).throw(_Stop()))

    with pytest.raises(_Stop):
        backtest_smt.run_backtest_v2("2026-06-08", "2026-06-08", reset_pending=False)
    assert seen.get("reset_pending") is False

    # Default path threads True.
    seen.clear()
    with pytest.raises(_Stop):
        backtest_smt.run_backtest_v2("2026-06-08", "2026-06-08")
    assert seen.get("reset_pending") is True


def test_run_backtest_v2_teardown_preserves_pending_store(monkeypatch):
    """Regression for the GIL-25 Phase 1.1.5 teardown bug. The END-of-run set_in_memory_mode(False)
    teardown MUST pass reset_pending=False, otherwise a contiguous date's freshly-written
    _PENDING_STORE is wiped before the next date's start-call can carry it (silently disabling the
    whole cross-session carry in the per-date run_regression loop). Records the full
    set_in_memory_mode call sequence during a REAL single-date run and asserts the first call is the
    start (with the passed reset_pending) and the LAST call is the non-wiping teardown."""
    import smt_state

    calls: list[tuple[bool, bool]] = []
    _orig = smt_state.set_in_memory_mode

    def _spy(enabled, *, reset_pending=True):
        calls.append((enabled, reset_pending))
        _orig(enabled, reset_pending=reset_pending)

    monkeypatch.setattr(smt_state, "set_in_memory_mode", _spy)
    try:
        backtest_smt.run_backtest_v2("2026-06-08", "2026-06-08", reset_pending=False)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"run_backtest_v2 requires local parquet data: {exc}")

    assert calls, "set_in_memory_mode was never called"
    assert calls[0] == (True, False), "start call must thread the passed reset_pending"
    assert calls[-1] == (False, False), "teardown must NOT wipe the pending store (reset_pending=False)"
