"""tests/conftest.py — pytest configuration hook.

Creates minimal manifest.json files in CACHE_DIR and FUTURES_CACHE_DIR if they
don't exist, so `import train` works on fresh checkouts and CI machines that
haven't run prepare.py yet.
"""
import os


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
                    "fetch_interval": "5m",
                    "source": "ib",
                },
                f,
                indent=2,
            )
