"""tests/conftest.py — Session-scoped fixture providing a small, fixed test dataset.

pytest_configure hook (at bottom of this file) ensures CACHE_DIR contains a
manifest.json before any test module is imported, so `import train` works on
fresh checkouts and CI machines that haven't run prepare.py yet.


Downloads AAPL (training), MSFT (training), NVDA (test-only) for a fixed 2-month
backtest window (2024-09-01..2024-11-01) using yfinance + prepare.resample_to_daily().
History starts 2024-04-01 (yfinance 1h data is only available for the last 730 days).

Uses a persistent cache at ~/.cache/autoresearch/test_fixtures/ so downloads only
happen once per machine. Delete that directory to force a refresh.

Fixture: test_parquet_fixtures — session-scoped, yields dict:
  {
    "tmpdir":        pathlib.Path  — temp dir with all 3 parquets (for subprocess tests)
    "all_dfs":       dict[str, pd.DataFrame]  — all 3 tickers
    "train_dfs":     dict[str, pd.DataFrame]  — AAPL, MSFT (training tickers)
    "test_only_dfs": dict[str, pd.DataFrame]  — NVDA (test-only ticker)
  }
"""
import os
import pathlib
import shutil
import tempfile
import warnings

import pandas as pd
import pytest

# ── Fixed test dataset parameters ─────────────────────────────────────────────
# Tickers: 2 training + 1 test-only (≥1 test-only, ≤50% test-only = 33%)
TEST_TICKERS_TRAIN     = ["AAPL", "MSFT"]
TEST_TICKERS_TEST_ONLY = ["NVDA"]
TEST_ALL_TICKERS       = TEST_TICKERS_TRAIN + TEST_TICKERS_TEST_ONLY

# Date range: 1 year of warmup (for SMA100) + 2-month backtest window.
# TEST_BACKTEST_START matches train.py's current BACKTEST_START so in-process
# backtests using train.py's default constants work with this data.
TEST_HISTORY_START   = "2024-04-01"   # warmup start (yfinance 1h limit: last 730 days)
TEST_BACKTEST_START  = "2024-09-01"   # backtest window start (2 months)
TEST_BACKTEST_END    = "2025-11-01"   # backtest window end (~15 months of backtest data)

# Persistent cache: avoids re-downloading on every test run.
# Delete this directory to force a fresh download.
_FIXTURE_CACHE_DIR = pathlib.Path(os.path.expanduser("~")) / ".cache" / "autoresearch" / "test_fixtures"


def _download_ticker_df(ticker: str) -> pd.DataFrame | None:
    """Download and resample one ticker via yfinance + prepare.resample_to_daily."""
    import yfinance as yf
    import prepare
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        obj = yf.Ticker(ticker)
        df_h = obj.history(
            start=TEST_HISTORY_START,
            end=TEST_BACKTEST_END,
            interval="1h",
            auto_adjust=True,
            prepost=False,
        )
    if df_h is None or df_h.empty:
        return None
    return prepare.resample_to_daily(df_h)


@pytest.fixture(scope="session")
def test_parquet_fixtures():
    """
    Session-scoped fixture: provides AAPL (train), MSFT (train), NVDA (test-only)
    for integration and E2E tests. Downloads once, caches to disk, reuses on re-runs.

    Yields:
        dict with keys: tmpdir, all_dfs, train_dfs, test_only_dfs
    Skips (does not fail) if yfinance is unavailable or returns empty data.
    """
    _FIXTURE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Load from persistent cache, download any missing tickers
    all_dfs: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for ticker in TEST_ALL_TICKERS:
        cache_path = _FIXTURE_CACHE_DIR / f"{ticker}.parquet"
        if cache_path.exists():
            all_dfs[ticker] = pd.read_parquet(cache_path)
        else:
            missing.append(ticker)

    if missing:
        try:
            for ticker in missing:
                df = _download_ticker_df(ticker)
                if df is None or df.empty:
                    pytest.skip(
                        f"yfinance returned empty data for {ticker}. "
                        "Check network or delete ~/.cache/autoresearch/test_fixtures/ to retry."
                    )
                cache_path = _FIXTURE_CACHE_DIR / f"{ticker}.parquet"
                df.to_parquet(cache_path)
                all_dfs[ticker] = df
        except Exception as exc:
            pytest.skip(
                f"yfinance download failed ({exc}). "
                "Integration tests require network access on first run."
            )

    # Validate: all expected tickers must be present
    for ticker in TEST_ALL_TICKERS:
        if ticker not in all_dfs:
            pytest.skip(f"Test fixture missing ticker {ticker}.")

    train_dfs     = {t: all_dfs[t] for t in TEST_TICKERS_TRAIN}
    test_only_dfs = {t: all_dfs[t] for t in TEST_TICKERS_TEST_ONLY}

    # Write all 3 parquets to a fresh tmpdir for subprocess tests that need
    # AUTORESEARCH_CACHE_DIR pointing to a real directory of parquet files.
    # Flat layout (tmpdir/*.parquet): used by in-process tests (all_parquet_paths fixture).
    # Interval subdir (tmpdir/1h/*.parquet): required by train.py which expects CACHE_DIR/{interval}/*.
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="autoresearch_itest_"))
    interval_dir = tmpdir / "1h"
    interval_dir.mkdir()
    for ticker, df in all_dfs.items():
        df.to_parquet(tmpdir / f"{ticker}.parquet")
        df.to_parquet(interval_dir / f"{ticker}.parquet")
    # train.py loads manifest.json at module level; write one so subprocess tests don't crash.
    import json as _json
    (tmpdir / "manifest.json").write_text(
        _json.dumps({
            "tickers": TEST_ALL_TICKERS,
            "backtest_start": TEST_BACKTEST_START,
            "backtest_end": TEST_BACKTEST_END,
            "fetch_interval": "1h",
            "source": "yfinance",
        }),
        encoding="utf-8",
    )

    yield {
        "tmpdir":        tmpdir,
        "all_dfs":       all_dfs,
        "train_dfs":     train_dfs,
        "test_only_dfs": test_only_dfs,
    }

    # Cleanup tmpdir only (persistent cache is kept)
    shutil.rmtree(tmpdir, ignore_errors=True)


# ── Manifest bootstrap ────────────────────────────────────────────────────────

def pytest_configure(config):
    """Create a minimal manifest.json in CACHE_DIR if one doesn't exist.

    train.py calls _load_manifest() at module level, so this must run before
    any test file is collected (pytest_configure fires before collection).
    Without this, `import train` raises FileNotFoundError on any machine that
    hasn't run prepare.py — breaking CI and fresh worktrees.
    """
    import json
    cache_dir = os.environ.get(
        "AUTORESEARCH_CACHE_DIR",
        os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data"),
    )
    manifest_path = os.path.join(cache_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        os.makedirs(cache_dir, exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({
                "tickers": [],
                "backtest_start": "2024-09-01",
                "backtest_end": "2026-03-20",
                "fetch_interval": "1h",
                "source": "yfinance",
            }, f, indent=2)

    # ── Futures manifest bootstrap ────────────────────────────────────────────
    futures_cache_dir = os.environ.get(
        "FUTURES_CACHE_DIR",
        os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "futures_data"),
    )
    futures_manifest_path = os.path.join(futures_cache_dir, "futures_manifest.json")
    if not os.path.exists(futures_manifest_path):
        os.makedirs(futures_cache_dir, exist_ok=True)
        with open(futures_manifest_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "tickers": ["MNQ", "MES"],
                    "backtest_start": "2024-09-01",
                    "backtest_end": "2026-03-20",
                    "fetch_interval": "1m",
                    "source": "ib",
                },
                f,
                indent=2,
            )
