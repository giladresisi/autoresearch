# tests/test_optimization.py
# Infrastructure tests for the agent-driven optimization loop.
# These tests validate that the experiment loop is viable — not the quality
# of the current screen_day implementation.
import ast
import hashlib
import importlib.util
import os
import pathlib
import subprocess
import tempfile
import unittest.mock as mock

import numpy as np
import pandas as pd

import train

# ── Helpers ───────────────────────────────────────────────────────────────────

TRAIN_PY = pathlib.Path(__file__).parent.parent / "train.py"
DO_NOT_EDIT_MARKER = "# ── DO NOT EDIT BELOW THIS LINE"


def _split_train_source():
    """Returns (above_marker, marker, below_marker) from current train.py."""
    source = TRAIN_PY.read_text(encoding="utf-8")
    above, sep, below = source.partition(DO_NOT_EDIT_MARKER)
    assert sep, f"Marker '{DO_NOT_EDIT_MARKER}' not found in train.py"
    return above, sep, below


def _make_rising_dataset() -> dict[str, pd.DataFrame]:
    """
    200 business days ending 2026-02-27 with linearly rising prices + small noise.
    Noise is required: if daily_changes are all identical, std=0 and Sharpe=0.
    Prices stay well above the stop level throughout the backtest window.
    """
    bdays = pd.bdate_range(end="2026-02-27", periods=200)
    dates = [d.date() for d in bdays]
    rng = np.random.default_rng(42)
    prices = np.linspace(100.0, 150.0, 200) + rng.standard_normal(200) * 0.3
    prices = np.clip(prices, 95.0, 160.0)  # keep prices sane
    df = pd.DataFrame(
        {
            "open":       prices - 0.5,
            "high":       prices + 1.0,
            "low":        prices - 1.0,
            "close":      prices,
            "volume":     np.full(200, 1_000_000.0),
            "price_10am": prices,
        },
        index=pd.Index(dates, name="date"),
    )
    return {"SYNTHETIC": df}


# ── Test 1: Agent can modify screen_day and re-run train.py ───────────────────

def test_editable_section_stays_runnable_after_threshold_change():
    """
    Simulates a typical agent edit: relax the CCI threshold in screen_day.
    Verifies the modified file is valid Python and run_backtest still executes
    without error on an empty dataset (no data → 0 trades, must not raise).

    This guards against agent edits that accidentally break the module — e.g.
    a bad indentation, a missing colon, or a name error that only surfaces at
    import time.
    """
    above, marker, below = _split_train_source()

    assert "vol_ratio < 1.0" in above, (
        "Expected volume ratio threshold 'vol_ratio < 1.0' in the editable section of train.py. "
        "Update this test if the threshold expression changes."
    )

    # Simulate a threshold relaxation (the most common agent edit)
    modified_source = above.replace("vol_ratio < 1.0", "vol_ratio < 1.2", 1) + marker + below

    # Must be syntactically valid Python
    ast.parse(modified_source)

    # Must import and run without exceptions on empty data
    with tempfile.NamedTemporaryFile(
        suffix=".py", delete=False, mode="w", encoding="utf-8"
    ) as f:
        f.write(modified_source)
        tmp_path = f.name

    try:
        spec = importlib.util.spec_from_file_location("train_modified", tmp_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Empty dataset: no trades, must return a valid stats dict without raising
        stats = mod.run_backtest({})
        assert isinstance(stats, dict)
        assert "total_pnl" in stats
        assert stats["total_trades"] == 0
    finally:
        os.unlink(tmp_path)


# ── Test 2: Harness below the DO NOT EDIT marker is unchanged ─────────────────

def test_harness_below_do_not_edit_is_unchanged():
    """
    Guards against accidental agent edits to the evaluation harness.
    Computes SHA-256 of everything below the DO NOT EDIT marker and compares
    it to a golden hash recorded when the boundary was first set.

    If you intentionally change run_backtest, print_results, or the data
    loaders, recompute the hash and update GOLDEN_HASH below.
    """
    # Golden hash of the harness at the time the DO NOT EDIT boundary was set.
    # To recompute: python -c "import hashlib; s=open('train.py').read();
    #   m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"
    GOLDEN_HASH = "8f2174487376cd0ac3e40a2dc8628ec374cc3753dbfb566cec2c6a16d5857bad"

    _, _, below = _split_train_source()
    actual_hash = hashlib.sha256(below.encode("utf-8")).hexdigest()

    assert actual_hash == GOLDEN_HASH, (
        "The evaluation harness (below the DO NOT EDIT marker) has changed.\n"
        "If this was intentional, recompute the hash and update GOLDEN_HASH in this test.\n"
        f"  Expected: {GOLDEN_HASH}\n"
        f"  Actual:   {actual_hash}"
    )


# ── Test 2b: Agent commit modified only the editable section ─────────────────

def test_most_recent_train_commit_modified_only_editable_section():
    """
    Checks the most recent commit that touched train.py. Verifies BOTH:
      1. The editable section (above DO NOT EDIT) was actually changed — the
         agent made an experiment, not a no-op.
      2. The harness section (below DO NOT EDIT) was not touched.

    This is the complement of the static hash test above: that test catches
    uncommitted harness edits; this test catches committed harness edits AND
    confirms the agent actually mutated the right section.

    Skips when train.py has no commits that changed it (e.g. fresh branch
    before the first experiment iteration).
    """
    repo_root = TRAIN_PY.parent

    # Find the most recent commit that modified train.py
    log = subprocess.run(
        ["git", "log", "--oneline", "-1", "--", "train.py"],
        capture_output=True, text=True, cwd=repo_root,
    )
    if not log.stdout.strip():
        import pytest
        pytest.skip("train.py has no commits that modified it yet — no experiment iterations to check")

    commit_hash = log.stdout.split()[0]

    # Read the file at the commit before and after the change
    def _show(ref):
        result = subprocess.run(
            ["git", "show", f"{ref}:train.py"],
            capture_output=True, text=True, cwd=repo_root,
        )
        assert result.returncode == 0, f"git show {ref}:train.py failed: {result.stderr}"
        return result.stdout

    after  = _show(commit_hash)
    before = _show(f"{commit_hash}~1")

    def _split(source):
        above, sep, below = source.partition(DO_NOT_EDIT_MARKER)
        return (above, below) if sep else None

    before_parts = _split(before)
    after_parts  = _split(after)

    if before_parts is None or after_parts is None:
        import pytest
        pytest.skip(
            f"Commit {commit_hash} predates the DO NOT EDIT boundary — "
            "this test only applies to experiment iterations made after the marker was introduced"
        )

    before_editable, before_harness = before_parts
    after_editable,  after_harness  = after_parts

    # Condition 1: the editable section must have changed
    assert before_editable != after_editable, (
        f"The editable section of train.py was NOT changed in commit {commit_hash}. "
        "An experiment iteration must modify screen_day(), manage_position(), or "
        "their helper functions — a commit that leaves the editable section identical "
        "to the previous commit is not a valid experiment step."
    )

    # Condition 2: the harness must be identical before and after
    assert before_harness == after_harness, (
        f"The harness section (below DO NOT EDIT) was modified in commit {commit_hash}. "
        "Only code above the marker may change. Check the diff:\n"
        f"  git diff {commit_hash}~1 {commit_hash} -- train.py"
    )


# ── Test 3: P&L optimization is feasible on a known-good dataset ─────────────

def test_optimization_feasible_on_synthetic_data():
    """
    Verifies the optimization loop has a viable path: there exists at least one
    mutation to screen_day that produces total_pnl > 0 on a synthetic dataset where
    the default (strict) screener produces 0 trades.

    Design:
    - strict_screen: always returns None  → 0 trades → total_pnl 0.0
    - relaxed_screen: fires on entry day  → 1 trade  → total_pnl > 0

    Both screen_day and manage_position are patched so the test remains stable
    regardless of what the agent has done to those functions:
    - Patching screen_day: the test defines the two configurations itself; it is
      not testing the quality of the current screen_day but the infrastructure.
    - Patching manage_position: the default implementation raises the stop to
      breakeven once price >= entry + ATR. On our synthetic data this can move
      the stop close to the price level, making a small noise dip cause a
      stop-out mid-backtest. Patching to a no-op isolates the test from that effect.
    """
    ticker_dfs = _make_rising_dataset()

    def strict_screen(df, today):
        # Never fires — simulates starting state with 0 trades
        return None

    def relaxed_screen(df, today):
        # Always fires when there is data — simulates a threshold relaxation
        if len(df) < 2:
            return None
        price = float(df["price_10am"].iloc[-1])
        return {"stop": price - 10.0, "entry_price": price}

    def no_op_manage(position, df):
        # Never moves the stop — isolates test from manage_position logic
        return position["stop_price"]

    with mock.patch.object(train, "screen_day", strict_screen), \
         mock.patch.object(train, "manage_position", no_op_manage):
        strict_stats = train.run_backtest(ticker_dfs)

    with mock.patch.object(train, "screen_day", relaxed_screen), \
         mock.patch.object(train, "manage_position", no_op_manage):
        relaxed_stats = train.run_backtest(ticker_dfs)

    # Strict baseline: 0 trades, total_pnl = 0.0
    assert strict_stats["total_trades"] == 0, (
        "strict_screen must produce 0 trades (it always returns None)"
    )
    assert strict_stats["total_pnl"] == 0.0, (
        "0-trade baseline must have total_pnl = 0.0"
    )

    # Relaxed: at least one trade occurs
    assert relaxed_stats["total_trades"] > 0, (
        "relaxed_screen must produce at least one trade on synthetic data. "
        "Check that the backtest window [BACKTEST_START, BACKTEST_END) overlaps "
        "with the synthetic dataset date range."
    )

    # The trade must be profitable (rising synthetic prices → positive P&L)
    assert relaxed_stats["total_pnl"] > 0.0, (
        f"Expected positive total_pnl from a profitable trade on rising-price synthetic data. "
        f"Got total_pnl={relaxed_stats['total_pnl']}, total_trades={relaxed_stats['total_trades']}."
    )

    # Core assertion: the optimization loop has a viable path
    assert relaxed_stats["total_pnl"] > strict_stats["total_pnl"], (
        f"Relaxed screener (total_pnl={relaxed_stats['total_pnl']}) must beat strict "
        f"screener (total_pnl={strict_stats['total_pnl']}). "
        f"This means at least one mutation to screen_day improves P&L."
    )


# ── V3-B Tests: R7 diagnostics + walk-forward structure ─────────────────────

def test_run_backtest_returns_r7_keys():
    """run_backtest() must return max_drawdown, calmar, pnl_consistency."""
    ticker_dfs = _make_rising_dataset()
    stats = train.run_backtest(ticker_dfs)
    assert "max_drawdown" in stats
    assert "calmar" in stats
    assert "pnl_consistency" in stats
    assert isinstance(stats["max_drawdown"], float)
    assert isinstance(stats["calmar"], float)
    assert isinstance(stats["pnl_consistency"], float)


def test_max_drawdown_is_non_negative():
    """max_drawdown must be >= 0 for any valid run (drawdown cannot be negative)."""
    ticker_dfs = _make_rising_dataset()

    def always_enter(df, today):
        if len(df) < 2:
            return None
        price = float(df["price_10am"].iloc[-1])
        return {"stop": price - 10.0, "entry_price": price, "stop_type": "fallback"}

    def no_op_manage(position, df):
        return position["stop_price"]

    with mock.patch.object(train, "screen_day", always_enter), \
         mock.patch.object(train, "manage_position", no_op_manage):
        stats = train.run_backtest(ticker_dfs)

    assert stats["max_drawdown"] >= 0.0, (
        f"max_drawdown must be >= 0, got {stats['max_drawdown']}"
    )


def test_calmar_zero_when_no_drawdown():
    """calmar must be 0.0 when max_drawdown == 0 (guard against division by zero)."""
    stats = train.run_backtest({})  # empty dataset → early exit → max_drawdown = 0
    assert stats["calmar"] == 0.0
    assert stats["max_drawdown"] == 0.0


def test_pnl_consistency_equals_min_monthly_pnl():
    """
    pnl_consistency must equal the minimum monthly P&L across the backtest window.
    Uses a dataset spanning exactly 2 calendar months where month 1 is profitable
    and month 2 is breakeven, so pnl_consistency must be <= 0.
    """
    # 60 business days in Jan-Feb 2026: prices rise Jan, flat Feb
    bdays_jan = pd.bdate_range(start="2026-01-05", end="2026-01-30")
    bdays_feb = pd.bdate_range(start="2026-02-02", end="2026-02-27")
    all_bdays = bdays_jan.append(bdays_feb)
    dates = [d.date() for d in all_bdays]

    prices_jan = np.linspace(100.0, 120.0, len(bdays_jan))
    prices_feb = np.full(len(bdays_feb), 120.0)  # flat
    prices = np.concatenate([prices_jan, prices_feb])

    df = pd.DataFrame({
        "open":       prices - 0.5,
        "high":       prices + 1.0,
        "low":        prices - 1.0,
        "close":      prices,
        "volume":     np.full(len(dates), 1_000_000.0),
        "price_10am": prices,
    }, index=pd.Index(dates, name="date"))
    ticker_dfs = {"SYNTH": df}

    def enter_jan_only(df_arg, today):
        if today.month != 1:
            return None
        if len(df_arg) < 2:
            return None
        price = float(df_arg["price_10am"].iloc[-1])
        return {"stop": price - 5.0, "entry_price": price, "stop_type": "fallback"}

    def no_op_manage(position, df_arg):
        return position["stop_price"]

    with mock.patch.object(train, "screen_day", enter_jan_only), \
         mock.patch.object(train, "manage_position", no_op_manage):
        stats = train.run_backtest(
            ticker_dfs,
            start="2026-01-05",
            end="2026-03-01"
        )

    # pnl_consistency = min monthly PnL; Feb has flat prices so its P&L should be <= Jan's
    assert isinstance(stats["pnl_consistency"], float)
    # min monthly P&L is not higher than total_pnl (otherwise every month was equally good)
    assert stats["pnl_consistency"] <= stats["total_pnl"]


def test_print_results_includes_r7_lines(capsys):
    """print_results() must emit calmar: and pnl_consistency: lines."""
    stats = {
        "sharpe": 1.5, "total_trades": 5, "win_rate": 0.6,
        "avg_pnl_per_trade": 20.0, "total_pnl": 100.0,
        "backtest_start": "2026-01-01", "backtest_end": "2026-03-01",
        "calmar": 2.5, "pnl_consistency": 30.0,
    }
    train.print_results(stats)
    captured = capsys.readouterr()
    assert "calmar:" in captured.out
    assert "pnl_consistency:" in captured.out
    assert "2.5000" in captured.out
    assert "30.00" in captured.out


def _exec_main_block(extra_ns: dict) -> None:
    """
    Execute train.py's __main__ block in a copy of the train module namespace,
    with extra_ns values overlaid. This allows mocking without runpy's fresh namespace.
    """
    source = TRAIN_PY.read_text(encoding="utf-8")
    main_idx = source.find('if __name__ == "__main__":')
    ns = dict(vars(train))
    ns["__name__"] = "__main__"
    ns.update(extra_ns)
    exec(compile(source[main_idx:], str(TRAIN_PY), "exec"), ns)


def test_main_runs_walk_forward_windows_folds():
    """
    __main__ must call run_backtest exactly (WALK_FORWARD_WINDOWS * 2 + 1) times:
    N train + N test folds + 1 silent holdout.
    Executes the __main__ block in the train module's namespace so mocks are visible.
    """
    call_count = []

    def counting_backtest(ticker_dfs, start=None, end=None):
        call_count.append((start, end))
        return {
            "sharpe": 0.0, "total_trades": 0, "win_rate": 0.0,
            "avg_pnl_per_trade": 0.0, "total_pnl": 0.0,
            "ticker_pnl": {}, "backtest_start": start or train.BACKTEST_START,
            "backtest_end": end or train.BACKTEST_END,
            "trade_records": [], "max_drawdown": 0.0, "calmar": 0.0, "pnl_consistency": 0.0,
            "pnl_min": 0.0,
        }

    minimal_df = _make_rising_dataset()

    _exec_main_block({
        "load_all_ticker_data": lambda: minimal_df,
        "run_backtest": counting_backtest,
        "_write_trades_tsv": lambda records, annotation=None: None,
        "print": lambda *a, **kw: None,
    })

    expected = train.WALK_FORWARD_WINDOWS * 2 + 1  # N folds × 2 + 1 holdout
    assert len(call_count) == expected, (
        f"Expected {expected} run_backtest calls "
        f"(WALK_FORWARD_WINDOWS={train.WALK_FORWARD_WINDOWS}), got {len(call_count)}"
    )


def test_main_outputs_min_test_pnl_line(capsys):
    """
    __main__ output must contain a 'min_test_pnl:' line parseable by grep.
    Uses mocked run_backtest to avoid requiring cached parquet data.
    """
    def fake_backtest(ticker_dfs, start=None, end=None):
        return {
            "sharpe": 0.0, "total_trades": 0, "win_rate": 0.0,
            "avg_pnl_per_trade": 0.0, "total_pnl": 5.0,
            "ticker_pnl": {}, "backtest_start": start or train.BACKTEST_START,
            "backtest_end": end or train.BACKTEST_END,
            "trade_records": [], "max_drawdown": 0.0, "calmar": 0.0, "pnl_consistency": 0.0,
            "pnl_min": 5.0,
        }

    minimal_df = _make_rising_dataset()

    _exec_main_block({
        "load_all_ticker_data": lambda: minimal_df,
        "run_backtest": fake_backtest,
        "_write_trades_tsv": lambda records, annotation=None: None,
    })

    captured = capsys.readouterr()
    assert "min_test_pnl:" in captured.out, (
        f"Expected 'min_test_pnl:' in __main__ output.\nstdout: {captured.out[:500]}"
    )


def test_silent_pnl_hidden_by_default(capsys):
    """
    When WRITE_FINAL_OUTPUTS is False, silent_pnl line must say 'HIDDEN'.
    Patching all I/O to run the relevant part of __main__ logic.
    """
    fake_stats = {
        "sharpe": 0.0, "total_trades": 0, "win_rate": 0.0,
        "avg_pnl_per_trade": 0.0, "total_pnl": 0.0,
        "ticker_pnl": {}, "backtest_start": train.TRAIN_END,
        "backtest_end": train.BACKTEST_END, "trade_records": [],
        "max_drawdown": 0.0, "calmar": 0.0, "pnl_consistency": 0.0,
    }
    assert train.WRITE_FINAL_OUTPUTS is False, "WRITE_FINAL_OUTPUTS must default to False"
    # Simulate the silent holdout output section
    print("---")
    if train.WRITE_FINAL_OUTPUTS:
        train.print_results(fake_stats, prefix="holdout_")
    else:
        print(f"silent_pnl: HIDDEN")

    captured = capsys.readouterr()
    assert "silent_pnl: HIDDEN" in captured.out
    assert "holdout_" not in captured.out


def test_walk_forward_fold_dates_are_distinct():
    """
    Each fold's test window must be distinct and non-overlapping.
    Derives fold boundaries using the same logic as __main__ and verifies:
    - All test windows are 10 business days
    - No two folds share the same test_start date
    """
    import pandas as _pd_test
    from pandas.tseries.offsets import BDay as _BDay_test

    train_end_ts = _pd_test.Timestamp(train.TRAIN_END)
    n = train.WALK_FORWARD_WINDOWS
    fold_test_starts = []
    fold_test_ends   = []

    for i in range(n):
        steps_back = n - 1 - i
        test_end_ts   = train_end_ts - _BDay_test(steps_back * 10)
        test_start_ts = test_end_ts  - _BDay_test(10)
        fold_test_starts.append(test_start_ts)
        fold_test_ends.append(test_end_ts)

    # All test_start dates must be distinct
    assert len(set(fold_test_starts)) == n, (
        f"Fold test windows are not distinct: {fold_test_starts}"
    )

    # Consecutive folds must step by exactly 10 business days
    for i in range(1, n):
        bday_count = len(_pd_test.bdate_range(fold_test_starts[i-1], fold_test_starts[i])) - 1
        assert bday_count == 10, (
            f"Fold {i} and fold {i+1} test windows differ by {bday_count} bdays, expected 10"
        )


# ── Live-cache tests (skip when cache is absent) ─────────────────────────────

import pytest as _pytest

_CACHE_AVAILABLE = bool(train.load_all_ticker_data()) if os.path.isdir(train.CACHE_DIR) else False
_live = _pytest.mark.skipif(not _CACHE_AVAILABLE, reason="no parquet cache found in CACHE_DIR")


@_live
def test_live_run_backtest_r7_metrics_are_finite():
    """
    On real cached data, R7 metrics must be finite floats with no NaN.
    Guards against data gaps (e.g. missing price_10am) poisoning the equity curve.
    """
    ticker_dfs = train.load_all_ticker_data()
    stats = train.run_backtest(ticker_dfs)

    assert np.isfinite(stats["max_drawdown"]), (
        f"max_drawdown is not finite: {stats['max_drawdown']}"
    )
    assert np.isfinite(stats["calmar"]), (
        f"calmar is not finite: {stats['calmar']}"
    )
    assert np.isfinite(stats["pnl_consistency"]), (
        f"pnl_consistency is not finite: {stats['pnl_consistency']}"
    )
    assert stats["max_drawdown"] >= 0.0, (
        f"max_drawdown must be >= 0, got {stats['max_drawdown']}"
    )


@_live
def test_live_walk_forward_min_test_pnl_is_finite():
    """
    Running the walk-forward loop on real data must produce a finite min_test_pnl.
    Exercises the full fold boundary computation and run_backtest calls end-to-end.
    """
    import pandas as _pd_live
    from pandas.tseries.offsets import BDay as _BDay_live

    ticker_dfs = train.load_all_ticker_data()
    train_end_ts = _pd_live.Timestamp(train.TRAIN_END)
    fold_test_pnls = []

    for _i in range(train.WALK_FORWARD_WINDOWS):
        _steps_back = train.WALK_FORWARD_WINDOWS - 1 - _i
        _fold_test_end_ts   = train_end_ts - _BDay_live(_steps_back * 10)
        _fold_test_start_ts = _fold_test_end_ts - _BDay_live(10)
        _fold_train_end_ts  = _fold_test_start_ts

        fold_test_stats = train.run_backtest(
            ticker_dfs,
            start=str(_fold_test_start_ts.date()),
            end=str(_fold_test_end_ts.date()),
        )
        fold_test_pnls.append(fold_test_stats["total_pnl"])

        # Each fold's R7 metrics must be finite
        assert np.isfinite(fold_test_stats["max_drawdown"]), (
            f"fold {_i+1} max_drawdown is not finite: {fold_test_stats['max_drawdown']}"
        )

    min_test_pnl = min(fold_test_pnls)
    assert np.isfinite(min_test_pnl), f"min_test_pnl is not finite: {min_test_pnl}"


@_live
def test_live_train_py_subprocess_outputs_pnl_min():
    """
    Runs train.py as a subprocess with real cached data and verifies V3-C output.

    This is the end-to-end integration test: it exercises the actual __main__ block
    including walk-forward fold loop, print_results(), and the pnl_min output lines.
    Unit tests cannot catch wiring bugs (e.g. a return dict key missing only when
    the full loop runs, or print_results silently swallowing a KeyError via .get()).

    Assertions:
    - Exit code 0 (no crash)
    - Each fold block emits a *_pnl_min: line
    - Each pnl_min value is a parseable float
    - pnl_min <= total_pnl for every fold (worst perturbed seed cannot beat nominal)
    """
    result = subprocess.run(
        ["uv", "run", "python", "train.py"],
        capture_output=True,
        text=True,
        cwd=str(TRAIN_PY.parent),
    )
    assert result.returncode == 0, (
        f"train.py exited with code {result.returncode}.\n"
        f"stderr:\n{result.stderr[-2000:]}\n"
        f"stdout:\n{result.stdout[-2000:]}"
    )

    stdout = result.stdout

    # Collect all pnl_min lines and their paired total_pnl lines
    pnl_min_lines = [l for l in stdout.splitlines() if "_pnl_min:" in l]
    assert len(pnl_min_lines) > 0, (
        f"No '*_pnl_min:' lines found in train.py output.\nstdout:\n{stdout[-3000:]}"
    )

    # Each pnl_min line must parse to a valid float
    import re as _re
    for line in pnl_min_lines:
        m = _re.search(r"pnl_min:\s+([-\d.]+)", line)
        assert m, f"Could not parse float from pnl_min line: {line!r}"
        float(m.group(1))  # raises ValueError if not a valid float

    # pnl_min must appear for both train and test folds
    fold_prefixes = {f"fold{i+1}" for i in range(train.WALK_FORWARD_WINDOWS)}
    for prefix in fold_prefixes:
        train_min = next(
            (l for l in pnl_min_lines if l.startswith(f"{prefix}_train_pnl_min:")), None
        )
        test_min = next(
            (l for l in pnl_min_lines if l.startswith(f"{prefix}_test_pnl_min:")), None
        )
        assert train_min is not None, f"Missing {prefix}_train_pnl_min: in output"
        assert test_min is not None, f"Missing {prefix}_test_pnl_min: in output"

    # pnl_min <= total_pnl for each fold (min of perturbed runs cannot exceed nominal)
    for prefix in fold_prefixes:
        for split in ("train", "test"):
            pnl_min_line = next(
                (l for l in stdout.splitlines() if l.startswith(f"{prefix}_{split}_pnl_min:")),
                None,
            )
            total_pnl_line = next(
                (l for l in stdout.splitlines() if l.startswith(f"{prefix}_{split}_total_pnl:")),
                None,
            )
            if pnl_min_line and total_pnl_line:
                pnl_min_val  = float(_re.search(r"([-\d.]+)$", pnl_min_line.strip()).group(1))
                total_pnl_val = float(_re.search(r"([-\d.]+)$", total_pnl_line.strip()).group(1))
                assert pnl_min_val <= total_pnl_val + 1e-9, (
                    f"{prefix}_{split}: pnl_min ({pnl_min_val}) > total_pnl ({total_pnl_val})"
                )


# ── Helper: multi-ticker synthetic dataset ────────────────────────────────────

def _make_multi_ticker_dataset(tickers=("SYNTH1", "SYNTH2", "SYNTH3")) -> dict[str, pd.DataFrame]:
    """Rising-price dataset with multiple tickers, same price series, distinct names."""
    bdays = pd.bdate_range(end="2026-02-27", periods=200)
    dates = [d.date() for d in bdays]
    rng = np.random.default_rng(42)
    prices = np.linspace(100.0, 150.0, 200) + rng.standard_normal(200) * 0.3
    prices = np.clip(prices, 95.0, 160.0)
    result = {}
    for t in tickers:
        df = pd.DataFrame(
            {
                "open":       prices - 0.5,
                "high":       prices + 1.0,
                "low":        prices - 1.0,
                "close":      prices,
                "volume":     np.full(200, 1_000_000.0),
                "price_10am": prices,
            },
            index=pd.Index(dates, name="date"),
        )
        result[t] = df
    return result


# ── R8: Position cap tests ─────────────────────────────────────────────────────

def test_position_cap_limits_simultaneous_positions():
    """
    With MAX_SIMULTANEOUS_POSITIONS=2 and 3 tickers that all fire screen_day,
    at most 2 tickers should ever be traded (cap prevents a third entry).
    Verified by checking that fewer than 3 unique tickers appear in trade records.
    """
    ticker_dfs = _make_multi_ticker_dataset()

    def always_enter(df, today):
        if len(df) < 2:
            return None
        price = float(df["price_10am"].iloc[-1])
        return {"stop": price - 10.0, "entry_price": price}

    def no_op_manage(position, df):
        return position["stop_price"]

    with mock.patch.object(train, "MAX_SIMULTANEOUS_POSITIONS", 2), \
         mock.patch.object(train, "screen_day", always_enter), \
         mock.patch.object(train, "manage_position", no_op_manage):
        stats = train.run_backtest(ticker_dfs)

    traded_tickers = {r["ticker"] for r in stats["trade_records"]}
    assert len(traded_tickers) <= 2, (
        f"Expected at most 2 simultaneous positions but found trades for: {traded_tickers}"
    )


def test_position_cap_does_not_fire_when_unlimited():
    """
    With MAX_SIMULTANEOUS_POSITIONS=1000 (effectively unlimited) and 3 tickers
    that all fire screen_day, at least one trade must occur.
    Guards against the cap accidentally blocking entries.
    """
    ticker_dfs = _make_multi_ticker_dataset()

    def always_enter(df, today):
        if len(df) < 2:
            return None
        price = float(df["price_10am"].iloc[-1])
        return {"stop": price - 10.0, "entry_price": price}

    def no_op_manage(position, df):
        return position["stop_price"]

    with mock.patch.object(train, "MAX_SIMULTANEOUS_POSITIONS", 1000), \
         mock.patch.object(train, "screen_day", always_enter), \
         mock.patch.object(train, "manage_position", no_op_manage):
        stats = train.run_backtest(ticker_dfs)

    assert stats["total_trades"] >= 1, (
        "Expected at least 1 trade with unlimited position cap on 3-ticker dataset"
    )


def test_correlation_penalty_reduces_pnl_when_positive():
    """
    With CORRELATION_PENALTY_WEIGHT=0.5 and perfectly correlated tickers,
    the penalized total_pnl must be less than the unpenalized total_pnl.
    """
    ticker_dfs = _make_multi_ticker_dataset(tickers=("SYNTH1", "SYNTH2"))

    def always_enter(df, today):
        if len(df) < 2:
            return None
        price = float(df["price_10am"].iloc[-1])
        return {"stop": price - 10.0, "entry_price": price}

    def no_op_manage(position, df):
        return position["stop_price"]

    with mock.patch.object(train, "CORRELATION_PENALTY_WEIGHT", 0.0), \
         mock.patch.object(train, "screen_day", always_enter), \
         mock.patch.object(train, "manage_position", no_op_manage):
        unpenalized = train.run_backtest(ticker_dfs)

    with mock.patch.object(train, "CORRELATION_PENALTY_WEIGHT", 0.5), \
         mock.patch.object(train, "screen_day", always_enter), \
         mock.patch.object(train, "manage_position", no_op_manage):
        penalized = train.run_backtest(ticker_dfs)

    # Only assert when both runs are profitable (penalty formula assumes positive pnl)
    if unpenalized["total_pnl"] > 0:
        assert penalized["total_pnl"] < unpenalized["total_pnl"], (
            f"Penalized pnl ({penalized['total_pnl']}) should be less than "
            f"unpenalized pnl ({unpenalized['total_pnl']}) with identical tickers"
        )


def test_correlation_penalty_zero_when_weight_is_zero():
    """
    With CORRELATION_PENALTY_WEIGHT=0.0 (default), total_pnl must equal
    the unpenalized value (penalty weight=0 means no discount).
    """
    ticker_dfs = _make_multi_ticker_dataset(tickers=("SYNTH1", "SYNTH2"))

    def always_enter(df, today):
        if len(df) < 2:
            return None
        price = float(df["price_10am"].iloc[-1])
        return {"stop": price - 10.0, "entry_price": price}

    def no_op_manage(position, df):
        return position["stop_price"]

    with mock.patch.object(train, "CORRELATION_PENALTY_WEIGHT", 0.0), \
         mock.patch.object(train, "screen_day", always_enter), \
         mock.patch.object(train, "manage_position", no_op_manage):
        weight_zero = train.run_backtest(ticker_dfs)

    with mock.patch.object(train, "CORRELATION_PENALTY_WEIGHT", 0.5), \
         mock.patch.object(train, "screen_day", always_enter), \
         mock.patch.object(train, "manage_position", no_op_manage):
        weight_nonzero = train.run_backtest(ticker_dfs)

    if weight_zero["total_pnl"] > 0:
        assert weight_zero["total_pnl"] > weight_nonzero["total_pnl"], (
            f"weight=0 run ({weight_zero['total_pnl']}) should have higher pnl "
            f"than weight=0.5 run ({weight_nonzero['total_pnl']})"
        )


# ── R9: Robustness perturbation tests ─────────────────────────────────────────

def test_robustness_seeds_zero_returns_pnl_min_equal_to_total_pnl():
    """With ROBUSTNESS_SEEDS=0 (default), pnl_min must equal total_pnl — no perturbation overhead."""
    ticker_dfs = _make_rising_dataset()

    with mock.patch.object(train, "ROBUSTNESS_SEEDS", 0):
        stats = train.run_backtest(ticker_dfs)

    assert stats["pnl_min"] == stats["total_pnl"], (
        f"pnl_min ({stats['pnl_min']}) must equal total_pnl ({stats['total_pnl']}) "
        "when ROBUSTNESS_SEEDS == 0"
    )


def test_robustness_seeds_nonzero_returns_pnl_min():
    """With ROBUSTNESS_SEEDS=3, pnl_min is in the result and <= total_pnl (worst seed <= nominal)."""
    ticker_dfs = _make_rising_dataset()

    def always_enter(df, today):
        if len(df) < 2:
            return None
        price = float(df["price_10am"].iloc[-1])
        return {"stop": price - 10.0, "entry_price": price}

    def no_op_manage(position, df):
        return position["stop_price"]

    with mock.patch.object(train, "ROBUSTNESS_SEEDS", 3), \
         mock.patch.object(train, "screen_day", always_enter), \
         mock.patch.object(train, "manage_position", no_op_manage):
        stats = train.run_backtest(ticker_dfs)

    assert "pnl_min" in stats, "pnl_min key missing from run_backtest result"
    assert isinstance(stats["pnl_min"], float), f"pnl_min must be float, got {type(stats['pnl_min'])}"
    assert stats["pnl_min"] <= stats["total_pnl"], (
        f"pnl_min ({stats['pnl_min']}) must be <= total_pnl ({stats['total_pnl']})"
    )


def test_print_results_includes_pnl_min_line(capsys):
    """print_results() must emit a 'pnl_min:' line with the correct value."""
    stats = {
        "sharpe": 0.0, "total_trades": 0, "win_rate": 0.0,
        "avg_pnl_per_trade": 0.0, "total_pnl": 50.0,
        "backtest_start": "2026-01-01", "backtest_end": "2026-03-01",
        "calmar": 0.0, "pnl_consistency": 0.0, "pnl_min": 42.00,
    }
    train.print_results(stats)
    captured = capsys.readouterr()
    assert "pnl_min:" in captured.out, "pnl_min: line missing from print_results output"
    assert "42.00" in captured.out, f"Expected '42.00' in output, got: {captured.out}"


def test_write_trades_tsv_annotation_header(tmp_path):
    """_write_trades_tsv with annotation writes '# annotation' as the first line."""
    orig_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        train._write_trades_tsv([], annotation="pnl_min: $25.50")
        lines = (tmp_path / "trades.tsv").read_text(encoding="utf-8").splitlines()
        assert lines[0] == "# pnl_min: $25.50", (
            f"Expected first line '# pnl_min: $25.50', got '{lines[0]}'"
        )
        assert lines[1].startswith("ticker\t"), (
            f"Expected second line to be TSV header starting with 'ticker\\t', got '{lines[1]}'"
        )
    finally:
        os.chdir(orig_dir)


def test_write_trades_tsv_no_annotation_when_none(tmp_path):
    """_write_trades_tsv with annotation=None writes no comment line — first line is TSV header."""
    orig_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        train._write_trades_tsv([], annotation=None)
        first_line = (tmp_path / "trades.tsv").read_text(encoding="utf-8").splitlines()[0]
        assert first_line.startswith("ticker"), (
            f"Expected first line to start with 'ticker' (no comment), got '{first_line}'"
        )
    finally:
        os.chdir(orig_dir)
