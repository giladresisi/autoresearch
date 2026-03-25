"""tests/test_fold_auto_detect.py — Unit tests for _compute_fold_params and ticker split."""
import pytest
from train import _compute_fold_params
from tests.conftest import (
    TEST_TICKERS_TRAIN, TEST_TICKERS_TEST_ONLY, TEST_ALL_TICKERS,
)


# ── _compute_fold_params: short timeframe (< 130 bdays) ───────────────────────

def test_fold_params_short_returns_one_fold():
    """< 130 bdays → exactly 1 fold."""
    n_folds, _ = _compute_fold_params("2024-09-01", "2024-10-18", 7, 40)
    assert n_folds == 1


def test_fold_params_short_test_days_is_two_weeks():
    """~33 bdays → test_days = min(10, 33//2=16) = 10 (2 calendar weeks)."""
    _, test_days = _compute_fold_params("2024-09-01", "2024-10-18", 7, 40)
    assert test_days == 10


def test_fold_params_short_test_days_at_most_half():
    """test_days must be ≤ 50% of total bdays for the given short window."""
    import pandas as pd
    start, end = "2024-09-01", "2024-10-18"
    total = len(pd.bdate_range(start, end))
    _, test_days = _compute_fold_params(start, end, 7, 40)
    assert test_days <= total // 2


def test_fold_params_short_test_days_at_least_one():
    """test_days must be ≥ 1 even for very short windows."""
    # 3 bdays total → 50% = 1, min(10, 1) = 1
    _, test_days = _compute_fold_params("2024-09-02", "2024-09-05", 7, 40)
    assert test_days >= 1


def test_fold_params_very_short_obeys_fifty_pct_cap():
    """When 50% < 10 bdays, test_days ≤ 50% even though it drops below 2 weeks."""
    import pandas as pd
    start, end = "2024-09-02", "2024-09-16"  # ~10 bdays
    total = len(pd.bdate_range(start, end))
    _, test_days = _compute_fold_params(start, end, 7, 40)
    assert test_days <= max(1, total // 2)


# ── _compute_fold_params: normal timeframe (≥ 130 bdays) ─────────────────────

def test_fold_params_normal_preserves_walk_forward_windows():
    """≥ 130 bdays → returns original n_folds unchanged."""
    n_folds, _ = _compute_fold_params("2024-01-01", "2026-01-01", 7, 40)
    assert n_folds == 7


def test_fold_params_normal_preserves_fold_test_days():
    """≥ 130 bdays → returns original fold_test_days unchanged."""
    _, test_days = _compute_fold_params("2024-01-01", "2026-01-01", 7, 40)
    assert test_days == 40


def test_fold_params_boundary_130_bdays():
    """Exactly 130 bdays is NOT short — returns original values."""
    import pandas as pd
    # Find a window with exactly 130 bdays
    start = "2024-01-01"
    bdays = pd.bdate_range(start, periods=131)  # 131 bdays
    end = str(bdays[-1].date())
    n_folds, test_days = _compute_fold_params(start, end, 7, 40)
    assert n_folds == 7
    assert test_days == 40


# ── Ticker split constraints ──────────────────────────────────────────────────

def test_test_only_tickers_at_least_one():
    """At least 1 ticker must be designated test-only."""
    assert len(TEST_TICKERS_TEST_ONLY) >= 1


def test_test_only_tickers_at_most_fifty_pct():
    """Test-only tickers must be ≤ 50% of the total ticker universe."""
    pct = len(TEST_TICKERS_TEST_ONLY) / len(TEST_ALL_TICKERS)
    assert pct <= 0.5, (
        f"Test-only fraction {pct:.0%} exceeds 50%. "
        f"train={TEST_TICKERS_TRAIN}, test_only={TEST_TICKERS_TEST_ONLY}"
    )


def test_training_tickers_at_least_one():
    """Training ticker set must have at least 1 ticker."""
    assert len(TEST_TICKERS_TRAIN) >= 1


def test_no_overlap_between_train_and_test_only():
    """Training and test-only ticker sets must be disjoint."""
    overlap = set(TEST_TICKERS_TRAIN) & set(TEST_TICKERS_TEST_ONLY)
    assert not overlap, f"Tickers appear in both train and test-only: {overlap}"


def test_all_tickers_is_union_of_train_and_test_only():
    """TEST_ALL_TICKERS must equal the union of TRAIN + TEST_ONLY."""
    assert set(TEST_ALL_TICKERS) == set(TEST_TICKERS_TRAIN) | set(TEST_TICKERS_TEST_ONLY)
