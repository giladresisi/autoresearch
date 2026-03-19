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

    assert "c0 < -50" in above, (
        "Expected CCI threshold 'c0 < -50' in the editable section of train.py. "
        "Update this test if the threshold expression changes."
    )

    # Simulate a threshold relaxation (the most common agent edit)
    modified_source = above.replace("c0 < -50", "c0 < -30", 1) + marker + below

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
        assert "sharpe" in stats
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
    GOLDEN_HASH = "dca8913befbad2fa16327bb639f06d5e8746115cb43f71f1c07759a4ea17cdbc"

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


# ── Test 3: Sharpe optimization is feasible on a known-good dataset ───────────

def test_optimization_feasible_on_synthetic_data():
    """
    Verifies the optimization loop has a viable path: there exists at least one
    mutation to screen_day that produces Sharpe > 0 on a synthetic dataset where
    the default (strict) screener produces 0 trades.

    Design:
    - strict_screen: always returns None  → 0 trades → Sharpe 0.0
    - relaxed_screen: fires on entry day  → 1 trade  → Sharpe > 0

    Both screen_day and manage_position are patched so the test remains stable
    regardless of what the agent has done to those functions:
    - Patching screen_day: the test defines the two configurations itself; it is
      not testing the quality of the current screen_day but the infrastructure.
    - Patching manage_position: the default implementation raises the stop to
      breakeven once price >= entry + ATR. On our synthetic data this can move
      the stop close to the price level, making a small noise dip cause a
      stop-out mid-backtest. A stop-out produces a large negative daily_change
      that swamps the positive trend and can yield negative Sharpe — making the
      test unreliable. Patching to a no-op isolates the test from that effect.
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

    # Strict baseline: 0 trades, Sharpe = 0.0
    assert strict_stats["total_trades"] == 0, (
        "strict_screen must produce 0 trades (it always returns None)"
    )
    assert strict_stats["sharpe"] == 0.0, (
        "0-trade baseline must have Sharpe = 0.0"
    )

    # Relaxed: at least one trade occurs
    assert relaxed_stats["total_trades"] > 0, (
        "relaxed_screen must produce at least one trade on synthetic data. "
        "Check that the backtest window [BACKTEST_START, BACKTEST_END) overlaps "
        "with the synthetic dataset date range."
    )

    # The trade must be profitable (rising synthetic prices → Sharpe > 0)
    assert relaxed_stats["sharpe"] > 0.0, (
        f"Expected positive Sharpe from a profitable trade on rising-price synthetic data. "
        f"Got sharpe={relaxed_stats['sharpe']}, total_trades={relaxed_stats['total_trades']}. "
        f"If Sharpe=0.0 with trades>0, check that std(daily_changes)>0 — "
        f"prices must have noise, not be perfectly linear."
    )

    # Core assertion: the optimization loop has a viable path
    assert relaxed_stats["sharpe"] > strict_stats["sharpe"], (
        f"Relaxed screener (sharpe={relaxed_stats['sharpe']}) must beat strict "
        f"screener (sharpe={strict_stats['sharpe']}). "
        f"This means at least one mutation to screen_day improves Sharpe."
    )
