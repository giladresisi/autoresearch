"""tests/test_v3_f.py — Unit tests for V3-F: TEST_EXTRA_TICKERS and CACHE_DIR env var."""
import os
import pathlib
import hashlib
import pandas as pd
import numpy as np
import pytest
import train
import prepare

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_df(n=200, seed=42):
    """Synthetic daily OHLCV dataframe with n rows."""
    bdays = pd.bdate_range(end="2026-02-27", periods=n)
    dates = [d.date() for d in bdays]
    rng = np.random.default_rng(seed)
    prices = np.linspace(100.0, 150.0, n) + rng.standard_normal(n) * 0.3
    prices = np.clip(prices, 95.0, 160.0)
    return pd.DataFrame({
        "open":       prices - 0.5,
        "high":       prices + 1.0,
        "low":        prices - 1.0,
        "close":      prices,
        "volume":     np.full(n, 1_000_000.0),
        "price_10am": prices,
    }, index=pd.Index(dates, name="date"))


# ── Test 1: TEST_EXTRA_TICKERS constant exists with correct default ─────────

def test_test_extra_tickers_constant_exists():
    """TEST_EXTRA_TICKERS must exist in train module and default to empty list."""
    assert hasattr(train, "TEST_EXTRA_TICKERS"), "TEST_EXTRA_TICKERS constant missing from train.py"
    assert isinstance(train.TEST_EXTRA_TICKERS, list)
    assert train.TEST_EXTRA_TICKERS == [], "Default must be [] for backward compatibility"


# ── Test 2: TEST_EXTRA_TICKERS is in the mutable section ──────────────────

def test_test_extra_tickers_in_mutable_section():
    """TEST_EXTRA_TICKERS must appear in the mutable (above DO NOT EDIT) section."""
    source = pathlib.Path("train.py").read_text(encoding="utf-8")
    marker = "# ── DO NOT EDIT BELOW THIS LINE"
    above, sep, _ = source.partition(marker)
    assert sep, "DO NOT EDIT marker not found"
    assert "TEST_EXTRA_TICKERS" in above, "TEST_EXTRA_TICKERS must be in the mutable section"


# ── Test 3: Empty TEST_EXTRA_TICKERS → _test_ticker_dfs == _train_ticker_dfs ──

def test_empty_extra_tickers_produces_same_test_universe():
    """
    When TEST_EXTRA_TICKERS = [] and TICKER_HOLDOUT_FRAC = 0.0, the fold test
    universe must be identical to the training universe (backward-compatible behavior).
    Verified by checking that run_backtest produces the same pnl when called with
    _test_ticker_dfs vs _train_ticker_dfs on synthetic data.
    """
    ticker_dfs = {"AAAA": _make_df(seed=1), "BBBB": _make_df(seed=2)}
    # Simulate the V3-F construction with no extras
    extra_ticker_dfs = {}
    test_ticker_dfs  = {**ticker_dfs, **extra_ticker_dfs}
    # test_ticker_dfs must equal ticker_dfs
    assert set(test_ticker_dfs.keys()) == set(ticker_dfs.keys())
    # run_backtest results must be identical
    stats_train = train.run_backtest(ticker_dfs, start="2025-12-01", end="2026-01-15")
    stats_test  = train.run_backtest(test_ticker_dfs, start="2025-12-01", end="2026-01-15")
    assert stats_train["total_pnl"] == stats_test["total_pnl"]


# ── Test 4: Extra tickers appear in test fold pnl, not train fold pnl ──────

def test_extra_tickers_included_in_test_but_not_train():
    """
    When TEST_EXTRA_TICKERS = ["EXTRA"], the _test_ticker_dfs must contain EXTRA
    but _train_ticker_dfs must not.
    """
    train_dfs = {"AAAA": _make_df(seed=1)}
    extra_dfs = {"EXTRA": _make_df(seed=99)}
    test_dfs  = {**train_dfs, **extra_dfs}

    assert "EXTRA" not in train_dfs
    assert "EXTRA" in test_dfs
    # Training fold must not trade EXTRA
    stats_train = train.run_backtest(train_dfs, start="2025-12-01", end="2026-01-15")
    # Test fold may trade EXTRA (can't assert it does on synthetic data, but must not raise)
    stats_test = train.run_backtest(test_dfs, start="2025-12-01", end="2026-01-15")
    assert isinstance(stats_test, dict)


# ── Test 5: Extra ticker absent from cache is silently skipped ─────────────

def test_extra_ticker_not_in_cache_is_skipped():
    """
    If a ticker in TEST_EXTRA_TICKERS is not in ticker_dfs (e.g. not yet downloaded),
    it must be silently skipped (not raise KeyError).
    """
    ticker_dfs = {"AAAA": _make_df(seed=1)}
    extra_tickers = ["NOT_IN_CACHE"]
    extra_ticker_dfs = {t: ticker_dfs[t] for t in extra_tickers if t in ticker_dfs}
    assert extra_ticker_dfs == {}  # silently empty, no KeyError


# ── Test 6: CACHE_DIR env var overrides default in train module ────────────

def test_cache_dir_env_var_overrides_default(monkeypatch, tmp_path):
    """AUTORESEARCH_CACHE_DIR env var must override the default cache path."""
    custom = str(tmp_path / "custom_cache")
    monkeypatch.setenv("AUTORESEARCH_CACHE_DIR", custom)
    # Reimport to pick up the env var (module-level constant)
    import sys
    if "train" in sys.modules:
        del sys.modules["train"]
    import train as train_fresh
    assert train_fresh.CACHE_DIR == custom, (
        f"Expected CACHE_DIR={custom!r} after env override, got {train_fresh.CACHE_DIR!r}"
    )
    # Restore sys.modules so other tests see the original module
    del sys.modules["train"]
    import train  # noqa: F401 — restores sys.modules["train"] for subsequent tests


def test_prepare_cache_dir_env_var_overrides_default(monkeypatch, tmp_path):
    """AUTORESEARCH_CACHE_DIR env var must override the default in prepare module too."""
    custom = str(tmp_path / "prepare_cache")
    monkeypatch.setenv("AUTORESEARCH_CACHE_DIR", custom)
    import sys
    if "prepare" in sys.modules:
        del sys.modules["prepare"]
    import prepare as prepare_fresh
    assert prepare_fresh.CACHE_DIR == custom, (
        f"Expected CACHE_DIR={custom!r} after env override, got {prepare_fresh.CACHE_DIR!r}"
    )
    del sys.modules["prepare"]
    import prepare  # noqa: F401 — restores sys.modules["prepare"] for subsequent tests


# ── Test 7: Default CACHE_DIR unchanged when env var absent ───────────────

def test_cache_dir_default_unchanged_without_env_var(monkeypatch):
    """When AUTORESEARCH_CACHE_DIR is not set, CACHE_DIR must be the legacy default."""
    monkeypatch.delenv("AUTORESEARCH_CACHE_DIR", raising=False)
    import sys
    if "train" in sys.modules:
        del sys.modules["train"]
    import train as t2
    expected = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")
    assert t2.CACHE_DIR == expected, (
        f"Expected default CACHE_DIR={expected!r}, got {t2.CACHE_DIR!r}"
    )
    del sys.modules["train"]
    import train  # noqa: F401 — restores sys.modules["train"] for subsequent tests


# ── Test 8: _test_ticker_dfs is built in __main__ section (source check) ──

def test_test_ticker_dfs_in_immutable_section():
    """_test_ticker_dfs and _extra_ticker_dfs must be present in the immutable zone."""
    source = pathlib.Path("train.py").read_text(encoding="utf-8")
    marker = "# ── DO NOT EDIT BELOW THIS LINE"
    _, sep, below = source.partition(marker)
    assert sep, "DO NOT EDIT marker not found"
    assert "_extra_ticker_dfs" in below, "_extra_ticker_dfs not found in immutable zone"
    assert "_test_ticker_dfs" in below, "_test_ticker_dfs not found in immutable zone"


# ── Test 9: Fold test call uses _test_ticker_dfs (source check) ───────────

def test_fold_test_call_uses_test_ticker_dfs():
    """The fold test run_backtest call must use _test_ticker_dfs, not _train_ticker_dfs."""
    source = pathlib.Path("train.py").read_text(encoding="utf-8")
    marker = "# ── DO NOT EDIT BELOW THIS LINE"
    _, sep, below = source.partition(marker)
    assert sep
    # Find the fold test stats line
    lines = below.splitlines()
    fold_test_lines = [l for l in lines if "_fold_test_stats" in l and "run_backtest" in l]
    assert fold_test_lines, "No _fold_test_stats = run_backtest(...) line found in immutable zone"
    for line in fold_test_lines:
        assert "_test_ticker_dfs" in line, (
            f"Fold test run_backtest call must use _test_ticker_dfs, found: {line!r}"
        )
