"""tests/conftest.py — pytest configuration hook.

Creates minimal manifest.json files in CACHE_DIR and FUTURES_CACHE_DIR if they
don't exist, so `import train` works on fresh checkouts and CI machines that
haven't run prepare.py yet.
"""
import os

import pytest

# ── .env must not reach the test suite ──────────────────────────────────────────
# `live_orders` calls `load_dotenv()` at IMPORT time, so any value in the developer's
# `.env` silently becomes test configuration. `DISCONNECTED=true` (set for shadow-live
# sessions) makes `live_orders` short-circuit the broker reconcile path, which is exactly
# what `tests/test_reconcile_on_close.py` asserts on — two tests then fail for a reason
# that has nothing to do with the code under test, inflating the failure baseline from
# 2 to 4 and reading like a regression to whoever runs the suite next.
#
# Same family as the `_isolate_global_state` fixture below: ambient developer state
# leaking into tests. Set at conftest MODULE level because conftest is imported before
# any test module, and the read happens at `live_orders` import — an autouse fixture
# would run too late to help.
os.environ["DISCONNECTED"] = "false"
# Same leak, one more variable (plan 38). `ACT_AI_DECISIONS=1` in `.env` switches on the
# v1 decisions worker, and `decisions_config` reads it ONCE at import — so whichever test
# module imports `live_orders` first decides it for the whole run.
# `tests/test_agent_dispatch.py` sorts first and moved that import ahead of
# `decisions_config`'s, which hung `test_smt_regression` on the worker's queue join.
# NOT done here, deliberately: forcing `LIVE_TRADING`. It leaks the same way, but
# `orchestrator.main._pre_session_init` runs a REAL IB gap-fill when it is false, and at
# least one test reaches that function relying on the leaked `true`.
os.environ["ACT_AI_DECISIONS"] = "0"


@pytest.fixture(autouse=True)
def _isolate_global_state(tmp_path, monkeypatch):
    """Point ACT_GLOBAL_DIR at a per-test temp dir so NO test can read or, crucially, WRITE
    the real shared global state — above all `general/live/global.json`, the live orchestrator's
    all-time-high (O6).

    Background: smt_state resolves global.json to `paths.general_live_dir()/global.json` in
    live/disk mode (`_IN_MEMORY=False`, the default), and `general_live_dir()` derives from
    `ACT_GLOBAL_DIR` — independent of `_STATE_DIR`. So a test that calls `save_global()` without
    forcing in-memory mode (e.g. `test_smt_hypothesis`'s 999999 fixture) would otherwise clobber
    the RUNNING orchestrator's `global.json`, neutering rule2b's recovery guard mid-session.
    This redirects the global root into temp for every test.

    Purely additive: tests that manage `ACT_GLOBAL_DIR` themselves (test_smt_regression,
    test_smt_state, test_paths, …) use a function-scoped `monkeypatch.setenv`, which overrides
    this fixture inside their own scope — so their behavior is unchanged.
    """
    gdir = tmp_path / "_global"
    for sub in ("general/live", "general/main"):
        (gdir / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(gdir))
    # smt_state caches the resolved global.json path keyed on (_STATE_DIR, _IN_MEMORY) — NOT
    # ACT_GLOBAL_DIR. Invalidate it so the redirect takes effect even if a prior test left the
    # cache warm with an unchanged state-dir/mode.
    try:
        import smt_state
        monkeypatch.setattr(smt_state, "_PATH_CACHE_SD", None, raising=False)
    except Exception:
        pass


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
