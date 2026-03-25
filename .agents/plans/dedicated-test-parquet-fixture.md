# Feature: Dedicated Small Test Parquet Fixture for Integration & E2E Tests

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Integration and E2E tests currently depend on parquet files in the default CACHE_DIR
(`~/.cache/autoresearch/stock_data`), which may be absent (fresh clone, CI environment)
or too large (389 tickers) to be practical. Tests use `skip_if_cache_missing` to skip
when the cache is empty — making the integration suite unreliable.

This feature makes integration and E2E tests self-sufficient by downloading a small,
fixed 3-ticker dataset ad-hoc using yfinance and a persistent session-level cache.

Additionally, the fold/walk-forward logic in `train.py` is extended with auto-detection
for short timeframes: if the backtest window is < 6 months, it automatically uses 1
fold with a test window of min(2 weeks, 50% of total). A new helper function
`_compute_fold_params` is added in the mutable section of `train.py` to enable
independent unit testing.

The test dataset uses 3 tickers: AAPL + MSFT (training) and NVDA (test-only), satisfying
the ≥1 test-only and ≤50% test-only ticker constraints.

## User Story

As a developer running integration tests
I want the tests to download their own small, fixed dataset
So that integration tests run reliably in any environment without a pre-populated cache

## Problem Statement

Integration tests skip entirely when the parquet cache is empty. This means tests are
only exercised on developer machines with a full 389-ticker cache, hiding regressions
in CI and fresh environments. The walk-forward fold logic also fails silently with
short timeframes (0 valid folds).

## Solution Statement

1. Add `tests/conftest.py` with a session-scoped fixture that downloads AAPL, MSFT,
   NVDA for a fixed window (history: 2023-09-01; backtest: 2024-09-01..2024-11-01)
   using yfinance + `prepare.resample_to_daily()`. Uses a persistent cache at
   `~/.cache/autoresearch/test_fixtures/` to skip re-downloads.
2. Add `_compute_fold_params()` to `train.py` mutable section (above DO NOT EDIT line).
3. Modify the `train.py` `__main__` block to call `_compute_fold_params` — adapting
   fold count and test window for short timeframes.
4. Update `tests/test_e2e.py` to use conftest fixtures instead of CACHE_DIR.
5. Add `tests/test_fold_auto_detect.py` for unit tests of fold auto-detection and
   ticker split constraints.
6. Update GOLDEN_HASH in `tests/test_optimization.py`.

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: Medium
**Primary Systems Affected**: `tests/test_e2e.py`, `tests/conftest.py`, `train.py`, `tests/test_optimization.py`
**Dependencies**: yfinance (already in requirements), pytest session fixtures
**Breaking Changes**: No — fold logic is backward-compatible (only changes behavior when total bdays < 130)

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `tests/test_e2e.py` (lines 1–40) — Fixtures to replace: `all_parquet_paths`, `skip_if_cache_missing`, `all_ticker_dfs`, `backtest_stats`
- `tests/test_e2e.py` (lines 80–170) — Subprocess tests that need `AUTORESEARCH_CACHE_DIR` env injection
- `train.py` (lines 1–83) — Module constants; `BACKTEST_START="2024-09-01"` important for fixture alignment
- `train.py` (lines 868–942) — The `__main__` fold loop to be updated with `_compute_fold_params` call
- `prepare.py` (lines 105–157) — `download_ticker()` and `resample_to_daily()` to reuse in conftest
- `tests/test_optimization.py` (lines 106–128) — GOLDEN_HASH and recompute command
- `tests/test_v3_f.py` (lines 71–88) — Pattern for TEST_EXTRA_TICKERS unit tests

### New Files to Create

- `tests/conftest.py` — Session-scoped test data fixture
- `tests/test_fold_auto_detect.py` — Unit tests for `_compute_fold_params` and ticker split

### Patterns to Follow

**Fixture scope**: Session-scoped for expensive downloads; module-scoped for derived fixtures
**Skip pattern**: `pytest.skip(f"message")` when yfinance is unavailable
**Persistent cache**: `~/.cache/autoresearch/test_fixtures/` (analogous to `prepare.py`'s `CACHE_DIR`)
**Hash recompute**: `python -c "import hashlib; s=open('train.py').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"`

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (All parallel)                   │
├─────────────────┬───────────────────┬───────────────┤
│ Task 1.1:       │ Task 1.2:         │ Task 1.3:     │
│ Add _compute_   │ Create            │ Create        │
│ fold_params to  │ tests/conftest.py │ test_fold_    │
│ train.py        │                   │ auto_detect.py│
│ Agent: harness  │ Agent: test-infra │ Agent: tests  │
└─────────────────┴───────────────────┴───────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ WAVE 2: Integration (Parallel after Wave 1)         │
├─────────────────────────┬───────────────────────────┤
│ Task 2.1:               │ Task 2.2:                 │
│ Modify __main__ block   │ Update test_e2e.py        │
│ to call _compute_fold_  │ fixtures to use conftest  │
│ params                  │                           │
│ Agent: harness          │ Agent: test-infra         │
└─────────────────────────┴───────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ WAVE 3: Sequential                                  │
├─────────────────────────────────────────────────────┤
│ Task 3.1: Recompute GOLDEN_HASH and update          │
│ test_optimization.py                                │
└─────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1, 1.2, 1.3 — no dependencies
**Wave 2 — Parallel after Wave 1**: Tasks 2.1, 2.2 — both depend on Wave 1 outputs
**Wave 3 — Sequential**: Task 3.1 — must run AFTER 2.1 (hash must reflect final __main__ code)

### Interface Contracts

- Task 1.1 provides `_compute_fold_params(backtest_start, train_end, n_folds, fold_test_days) -> (int, int)` → Task 2.1 calls it in `__main__`
- Task 1.2 provides `test_parquet_fixtures` session fixture → Task 2.2 uses it in test_e2e.py
- Task 1.3 imports `_compute_fold_params` from `train` → consistent signature from 1.1
- Task 2.1 modifies `__main__` → Task 3.1 recomputes hash of that section

### Synchronization Checkpoints

**After Wave 1**: `uv run pytest tests/test_fold_auto_detect.py -x` (unit tests for new function)
**After Wave 2**: `uv run pytest tests/test_e2e.py -x -m integration --no-header` (integration tests pass)
**After Wave 3**: `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -x`

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation — Add fold helper and conftest fixture

Phase 1 sets up the two core building blocks independently. No external service
verification needed — yfinance is already a project dependency.

### Phase 2: Integration — Modify __main__ and test_e2e.py

Phase 2 wires the new components into train.py's harness and the integration test suite.

### Phase 3: Hash Update

Phase 3 recomputes the GOLDEN_HASH for the modified __main__ section.

---

## STEP-BY-STEP TASKS

---

### WAVE 1: Foundation

#### Task 1.1: ADD `_compute_fold_params` function to `train.py` mutable section

- **WAVE**: 1
- **AGENT_ROLE**: harness-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: Testable helper function for fold auto-detection logic

**IMPLEMENT**:

Add the following function to `train.py` immediately BEFORE the `# ── DO NOT EDIT BELOW THIS LINE` marker. Place it after `nearest_resistance_atr` and before the DO NOT EDIT line.

```python
def _compute_fold_params(
    backtest_start: str,
    train_end: str,
    n_folds: int,
    fold_test_days: int,
) -> tuple:
    """
    Auto-detect short timeframes and return effective fold parameters.

    If total business days from backtest_start to train_end < 130 (~6 months):
      - effective_n_folds = 1
      - effective_fold_test_days = max(1, min(10, total_bdays // 2))
        (at least 1 bday, at most 50% of total, targeting 10 = 2 weeks)
    Otherwise returns (n_folds, fold_test_days) unchanged.

    Args:
        backtest_start: ISO date string for start of backtest window
        train_end: ISO date string for end of training window (= TRAIN_END)
        n_folds: WALK_FORWARD_WINDOWS constant
        fold_test_days: FOLD_TEST_DAYS constant
    Returns:
        (effective_n_folds, effective_fold_test_days)
    """
    import pandas as _pd_fold
    total_bdays = len(_pd_fold.bdate_range(backtest_start, train_end))
    short_threshold = 130  # ~6 calendar months of business days
    if total_bdays < short_threshold:
        effective_test_days = max(1, min(10, total_bdays // 2))
        return 1, effective_test_days
    return n_folds, fold_test_days
```

**VALIDATE**: `uv run python -c "from train import _compute_fold_params; print(_compute_fold_params('2024-09-01','2024-10-18',7,40))"`
Expected output: `(1, 10)` (33 bdays < 130 → 1 fold, min(10, 33//2=16)=10 ✓)

Also verify: `_compute_fold_params('2024-01-01','2026-01-01',7,40)` → `(7, 40)` (normal timeframe)

---

#### Task 1.2: CREATE `tests/conftest.py`

- **WAVE**: 1
- **AGENT_ROLE**: test-infrastructure
- **DEPENDS_ON**: []
- **BLOCKS**: [2.2]
- **PROVIDES**: `test_parquet_fixtures` session fixture with AAPL/MSFT/NVDA data

**IMPLEMENT**:

Create `tests/conftest.py`:

```python
"""tests/conftest.py — Session-scoped fixture providing a small, fixed test dataset.

Downloads AAPL (training), MSFT (training), NVDA (test-only) for a fixed 2-month
backtest window (2024-09-01..2024-11-01) using yfinance + prepare.resample_to_daily().

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
TEST_HISTORY_START   = "2023-09-01"   # warmup start (1 year before backtest)
TEST_BACKTEST_START  = "2024-09-01"   # backtest window start (2 months)
TEST_BACKTEST_END    = "2024-11-01"   # backtest window end (~43 bdays)

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
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="autoresearch_itest_"))
    for ticker, df in all_dfs.items():
        df.to_parquet(tmpdir / f"{ticker}.parquet")

    yield {
        "tmpdir":        tmpdir,
        "all_dfs":       all_dfs,
        "train_dfs":     train_dfs,
        "test_only_dfs": test_only_dfs,
    }

    # Cleanup tmpdir only (persistent cache is kept)
    shutil.rmtree(tmpdir, ignore_errors=True)
```

**VALIDATE**: `uv run pytest tests/conftest.py --collect-only` (no errors)
Also: `uv run python -c "import tests.conftest"` should not raise.

---

#### Task 1.3: CREATE `tests/test_fold_auto_detect.py`

- **WAVE**: 1
- **AGENT_ROLE**: test-author
- **DEPENDS_ON**: [] (reads train.py — Task 1.1 adds the function, but tests can be written first since the function signature is known)
- **BLOCKS**: []
- **PROVIDES**: Unit tests for `_compute_fold_params` and ticker split constraints

**IMPLEMENT**:

```python
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
```

**VALIDATE**: `uv run pytest tests/test_fold_auto_detect.py -x` (run AFTER Task 1.1 is done)

**Wave 1 Checkpoint**: `uv run pytest tests/test_fold_auto_detect.py -x`

---

### WAVE 2: Integration (After Wave 1)

#### Task 2.1: UPDATE `train.py` `__main__` block to use `_compute_fold_params`

- **WAVE**: 2
- **AGENT_ROLE**: harness-specialist
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1]
- **PROVIDES**: Short-timeframe-aware fold loop in the DO NOT EDIT zone

**IMPLEMENT**:

Locate the fold loop in the `__main__` block (currently around line 878). The current code:

```python
    for _i in range(WALK_FORWARD_WINDOWS):
        # Fold _i (0-indexed, oldest first).
        # Fold WALK_FORWARD_WINDOWS-1 (newest) has test window ending at TRAIN_END.
        _steps_back = WALK_FORWARD_WINDOWS - 1 - _i
        _fold_test_end_ts   = _train_end_ts - _BDay(_steps_back * FOLD_TEST_DAYS)
        _fold_test_start_ts = _fold_test_end_ts - _BDay(FOLD_TEST_DAYS)
        _fold_train_end_ts  = _fold_test_start_ts
```

Replace it with:

```python
    # Auto-detect short timeframes and adjust fold parameters.
    # If total bdays from BACKTEST_START to TRAIN_END < 130 (~6 months):
    #   uses 1 fold with test window = max(1, min(10, total//2)) bdays.
    _effective_n_folds, _effective_fold_test_days = _compute_fold_params(
        BACKTEST_START, TRAIN_END, WALK_FORWARD_WINDOWS, FOLD_TEST_DAYS
    )

    for _i in range(_effective_n_folds):
        # Fold _i (0-indexed, oldest first).
        # Fold _effective_n_folds-1 (newest) has test window ending at TRAIN_END.
        _steps_back = _effective_n_folds - 1 - _i
        _fold_test_end_ts   = _train_end_ts - _BDay(_steps_back * _effective_fold_test_days)
        _fold_test_start_ts = _fold_test_end_ts - _BDay(_effective_fold_test_days)
        _fold_train_end_ts  = _fold_test_start_ts
```

Also update the fold count reference later in the loop where `WALK_FORWARD_WINDOWS` appears:
- Line `if _fold_n == WALK_FORWARD_WINDOWS:` → change to `if _fold_n == _effective_n_folds:`

**IMPORTANT**: This change is in the DO NOT EDIT zone. After making this change, Task 3.1 MUST update GOLDEN_HASH.

**VALIDATE**: `uv run python -c "import train"` (module imports without error)
Then: `uv run pytest tests/test_optimization.py::test_editable_section_stays_runnable_after_threshold_change -x`
(Note: `test_harness_below_do_not_edit_is_unchanged` will FAIL until Task 3.1 updates the hash — that is expected.)

---

#### Task 2.2: UPDATE `tests/test_e2e.py` to use conftest fixtures

- **WAVE**: 2
- **AGENT_ROLE**: test-infrastructure
- **DEPENDS_ON**: [1.2]
- **BLOCKS**: []
- **PROVIDES**: Integration tests that use the small test dataset instead of CACHE_DIR

**IMPLEMENT**:

Make the following targeted changes to `tests/test_e2e.py`:

**Change 1** — Update imports at top of file:
- Remove: `from prepare import CACHE_DIR, BACKTEST_START`
- Add: `from prepare import BACKTEST_START`
- Add: `import os` (if not already present — needed for subprocess env injection)
- Keep all other imports unchanged

**Change 2** — Update `EXPECTED_TICKERS` constant:
```python
EXPECTED_TICKERS = ["AAPL", "MSFT", "NVDA"]
```

**Change 3** — Replace the `all_parquet_paths` fixture:
```python
@pytest.fixture(scope="module")
def all_parquet_paths(test_parquet_fixtures):
    """Returns parquet paths from the dedicated small test dataset."""
    tmpdir = test_parquet_fixtures["tmpdir"]
    return sorted(tmpdir.glob("*.parquet"))
```

**Change 4** — Replace the `skip_if_cache_missing` fixture:
```python
@pytest.fixture(scope="module")
def skip_if_cache_missing(test_parquet_fixtures):
    """No-op: integration tests always have data via test_parquet_fixtures."""
    pass
```

**Change 5** — Replace the `all_ticker_dfs` fixture:
```python
@pytest.fixture(scope="module")
def all_ticker_dfs(test_parquet_fixtures):
    """Returns all 3 test tickers (AAPL, MSFT, NVDA) from the small test dataset."""
    return test_parquet_fixtures["all_dfs"]
```

**Change 6** — Update `test_parquet_files_exist`: the existing test body uses
`EXPECTED_TICKERS` and `all_parquet_paths` — no change needed to the test body itself
since we updated those at the fixture/constant level.

**Change 7** — Update `test_train_exits_zero_with_pnl_output`:
Replace the current `skip_if_cache_missing` parameter with `test_parquet_fixtures`,
and inject `AUTORESEARCH_CACHE_DIR` into the subprocess environment:

```python
@pytest.mark.integration
def test_train_exits_zero_with_pnl_output(test_parquet_fixtures):
    """
    `uv run train.py` must exit with code 0 and print 'train_total_pnl: <float>' to stdout.
    Uses the small test dataset (AAPL, MSFT, NVDA) via AUTORESEARCH_CACHE_DIR.
    """
    env = {**os.environ, "AUTORESEARCH_CACHE_DIR": str(test_parquet_fixtures["tmpdir"])}
    result = subprocess.run(
        ["uv", "run", "train.py"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    assert result.returncode == 0, (
        f"train.py exited with code {result.returncode}.\n"
        f"stderr:\n{result.stderr[-2000:]}\n"
        f"stdout:\n{result.stdout[-500:]}"
    )
    pnl_lines = [
        line for line in result.stdout.splitlines()
        if "train_total_pnl:" in line
    ]
    assert len(pnl_lines) >= 1, (
        f"Expected at least one 'train_total_pnl:' line in output, got {len(pnl_lines)}.\n"
        f"stdout:\n{result.stdout}"
    )
    value_str = pnl_lines[0].split(":", 1)[1].strip()
    pnl_value = float(value_str)
    assert isinstance(pnl_value, float)
```

**Change 8** — Update `test_output_has_all_seven_fields` similarly:
Replace `skip_if_cache_missing` parameter with `test_parquet_fixtures`, inject env:

```python
@pytest.mark.integration
def test_output_has_all_seven_fields(test_parquet_fixtures):
    env = {**os.environ, "AUTORESEARCH_CACHE_DIR": str(test_parquet_fixtures["tmpdir"])}
    result = subprocess.run(
        ["uv", "run", "train.py"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    assert result.returncode == 0, f"train.py crashed: {result.stderr[-1000:]}"
    required_fields = [
        "train_sharpe:", "train_total_trades:", "train_win_rate:",
        "train_avg_pnl_per_trade:", "train_total_pnl:",
        "train_backtest_start:", "train_backtest_end:",
    ]
    for field in required_fields:
        assert any(field in line for line in result.stdout.splitlines()), (
            f"Missing output field '{field}' in train.py output.\n"
            f"stdout:\n{result.stdout}"
        )
```

**No changes needed** to:
- `test_parquet_schema_has_required_columns` (uses `first_parquet_df` which depends on updated `all_parquet_paths`)
- `test_parquet_index_is_date_objects` (same)
- `test_screen_day_on_real_parquet_data` (uses `all_parquet_paths` + imports `BACKTEST_START` from prepare which matches our data start)
- `test_pnl_self_consistency` (uses `backtest_stats` which uses `all_ticker_dfs`)
- `test_agent_loop_two_iterations_multi_ticker` (uses `all_ticker_dfs` — now 3 tickers, ≥2 ✓)
- `test_agent_loop_threshold_mutation_no_crash` (uses `all_ticker_dfs` — relaxed screener on Sep–Nov 2024 data with 3 tickers should produce ≥1 trade)

**Wave 2 Checkpoint**: `uv run pytest tests/test_e2e.py -x -m integration`

---

### WAVE 3: Hash Update (Sequential)

#### Task 3.1: RECOMPUTE GOLDEN_HASH and UPDATE `tests/test_optimization.py`

- **WAVE**: 3
- **AGENT_ROLE**: harness-specialist
- **DEPENDS_ON**: [2.1]
- **PROVIDES**: Passing golden hash test

**IMPLEMENT**:

Run the hash recompute command from the project root:

```bash
python -c "
import hashlib
s = open('train.py', encoding='utf-8').read()
m = '# ── DO NOT EDIT BELOW THIS LINE'
below = s.partition(m)[2]
print(hashlib.sha256(below.encode('utf-8')).hexdigest())
"
```

Take the printed hash value and replace `GOLDEN_HASH` in `tests/test_optimization.py` line 118:

```python
GOLDEN_HASH = "<new-hash-here>"
```

**VALIDATE**: `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -x`

**Final Checkpoint**: `uv run pytest tests/ -x --ignore=tests/test_selector.py -q`

---

## TESTING STRATEGY

**⚠️ ALL tests that can be automated MUST be automated.**

| What you're testing | Tool |
|---|---|
| `_compute_fold_params` logic | `pytest` (`tests/test_fold_auto_detect.py`) |
| Ticker split constraints | `pytest` (`tests/test_fold_auto_detect.py`) |
| Integration tests with real data | `pytest -m integration` (`tests/test_e2e.py`) |
| Harness hash integrity | `pytest` (`tests/test_optimization.py`) |

### Unit Tests — `_compute_fold_params`

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_fold_auto_detect.py` | **Run**: `uv run pytest tests/test_fold_auto_detect.py -v`

Test cases:
- `test_fold_params_short_returns_one_fold` — < 130 bdays → 1 fold ✅
- `test_fold_params_short_test_days_is_two_weeks` — 33 bdays → 10 test_days ✅
- `test_fold_params_short_test_days_at_most_half` — test_days ≤ 50% of total ✅
- `test_fold_params_short_test_days_at_least_one` — minimum 1 bday ✅
- `test_fold_params_very_short_obeys_fifty_pct_cap` — 50% cap for very short windows ✅
- `test_fold_params_normal_preserves_walk_forward_windows` — ≥130 bdays unchanged ✅
- `test_fold_params_normal_preserves_fold_test_days` — ≥130 bdays unchanged ✅
- `test_fold_params_boundary_130_bdays` — exactly 130 is not short ✅

### Unit Tests — Ticker Split

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_fold_auto_detect.py` | **Run**: `uv run pytest tests/test_fold_auto_detect.py::test_test_only_tickers_at_least_one -v`

- `test_test_only_tickers_at_least_one` — ≥1 test-only ticker ✅
- `test_test_only_tickers_at_most_fifty_pct` — ≤50% test-only ✅
- `test_training_tickers_at_least_one` — ≥1 training ticker ✅
- `test_no_overlap_between_train_and_test_only` — disjoint sets ✅
- `test_all_tickers_is_union_of_train_and_test_only` — union correctness ✅

### Integration Tests

**Status**: ✅ Automated | **Tool**: pytest -m integration | **Location**: `tests/test_e2e.py` | **Run**: `uv run pytest tests/test_e2e.py -m integration -v`

- `test_parquet_files_exist` — AAPL/MSFT/NVDA in tmpdir ✅
- `test_parquet_schema_has_required_columns` — all required columns ✅
- `test_parquet_index_is_date_objects` — datetime.date index ✅
- `test_train_exits_zero_with_pnl_output` — subprocess with AUTORESEARCH_CACHE_DIR ✅
- `test_output_has_all_seven_fields` — subprocess format check ✅
- `test_screen_day_on_real_parquet_data` — no exceptions on Sep-Nov 2024 data ✅
- `test_pnl_self_consistency` — total_pnl ≈ avg×n (handles 0-trade case) ✅
- `test_agent_loop_two_iterations_multi_ticker` — 3 tickers ≥ 2 ✓ ✅
- `test_agent_loop_threshold_mutation_no_crash` — relaxed screener on AAPL/MSFT/NVDA ✅

### Regression Tests

**Status**: ✅ Automated | **Tool**: pytest | **Run**: `uv run pytest tests/test_optimization.py -v`

- `test_harness_below_do_not_edit_is_unchanged` — updated GOLDEN_HASH ✅
- All other test_optimization tests — unchanged ✅

### Manual Tests

None — all test scenarios are fully automated.

### Edge Cases

- **0 trades from default screener**: `test_pnl_self_consistency` handles n=0 case ✅
- **yfinance down**: `test_parquet_fixtures` skips gracefully with informative message ✅
- **Persistent cache hit**: second run reuses cached parquets, no download ✅
- **Very short window (< 20 bdays)**: `_compute_fold_params` clamps to max(1, 50%) ✅
- **Boundary at 130 bdays**: treated as normal (≥130), uses WALK_FORWARD_WINDOWS ✅

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest — fold params + ticker split) | 13 | 59% |
| ✅ Integration (pytest -m integration) | 9 | 41% |
| ⚠️ Manual | 0 | 0% |
| **Total** | **22** | **100%** |

---

## VALIDATION COMMANDS

### Level 1: Syntax & Imports

```bash
uv run python -c "import train"
uv run python -c "import tests.conftest"
uv run python -c "from train import _compute_fold_params; print('OK')"
```

### Level 2: Unit Tests

```bash
uv run pytest tests/test_fold_auto_detect.py -v
```

Expected: 13 tests pass, 0 fail.

### Level 3: Integration Tests

```bash
uv run pytest tests/test_e2e.py -m integration -v
```

Expected: 9 tests pass, 0 fail. (First run may take ~30s for yfinance download)

### Level 4: Full Test Suite (excluding known unrelated failures)

```bash
uv run pytest tests/ --ignore=tests/test_selector.py -q
```

Expected: All passing tests before this change continue to pass. New tests also pass.

Also validate golden hash:
```bash
uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v
```

---

## ACCEPTANCE CRITERIA

### Functional

- [x] `tests/conftest.py` provides a session-scoped `test_parquet_fixtures` fixture yielding a dict with keys: `tmpdir`, `all_dfs`, `train_dfs`, `test_only_dfs`
- [x] Fixture uses tickers AAPL + MSFT as training and NVDA as test-only (3 tickers total)
- [x] `TEST_HISTORY_START = "2024-04-01"` (yfinance 1h limit is last 730 days; original "2023-09-01" is out of range as of 2026-03-25)
- [x] `TEST_BACKTEST_END = "2025-11-01"` (extended from "2024-11-01" to give ≥14 months of backtest coverage; required for relaxed screener to produce ≥1 trade)
- [x] Persistent cache at `~/.cache/autoresearch/test_fixtures/` is populated on first run and reused on subsequent runs without re-downloading
- [x] `train._compute_fold_params(backtest_start, train_end, n_folds, fold_test_days)` exists, is importable, and returns `(1, max(1, min(10, total_bdays // 2)))` when total bdays < 130
- [x] `_compute_fold_params` returns `(n_folds, fold_test_days)` unchanged when total bdays ≥ 130
- [x] `train.py` `__main__` block calls `_compute_fold_params` and uses the returned effective values for fold count and test window in the walk-forward loop
- [x] The fold loop uses `_effective_n_folds` and `_effective_fold_test_days` throughout (including the `if _fold_n == _effective_n_folds` guard)
- [x] `EXPECTED_TICKERS` in `test_e2e.py` is updated to `["AAPL", "MSFT", "NVDA"]`
- [x] `all_parquet_paths` fixture in `test_e2e.py` returns paths from `test_parquet_fixtures["tmpdir"]`
- [x] `all_ticker_dfs` fixture in `test_e2e.py` returns `test_parquet_fixtures["all_dfs"]` (all 3 tickers)
- [x] Subprocess tests in `test_e2e.py` inject `AUTORESEARCH_CACHE_DIR=<tmpdir>` into the subprocess environment
- [x] 3 `@_live` tests in `test_optimization.py` converted to `@pytest.mark.integration` using `test_parquet_fixtures` — no longer depend on 389-ticker CACHE_DIR

### Error Handling

- [ ] If yfinance returns empty data for any test ticker, `test_parquet_fixtures` calls `pytest.skip()` with a descriptive message — it does NOT raise an exception
- [ ] If yfinance raises a network exception, `test_parquet_fixtures` calls `pytest.skip()` — does NOT fail the test run

### Integration / E2E

- [x] All 9 `@pytest.mark.integration` tests in `test_e2e.py` pass with the small test dataset (no skips)
- [x] `uv run train.py` with `AUTORESEARCH_CACHE_DIR=<tmpdir>` exits with code 0
- [x] The subprocess stdout contains at least one line matching `*train_total_pnl:*`
- [x] The subprocess stdout contains all 7 required field names as substrings
- [x] `test_live_run_backtest_r7_metrics_are_finite` passes with AAPL/MSFT/NVDA fixture data
- [x] `test_live_walk_forward_min_test_pnl_is_finite` passes with AAPL/MSFT/NVDA fixture data
- [x] `test_live_train_py_subprocess_outputs_pnl_min` passes with `AUTORESEARCH_CACHE_DIR` injected

### Validation — verified by test commands

- [x] `uv run pytest tests/test_fold_auto_detect.py -v` → 13 tests pass, 0 fail
- [x] `uv run pytest tests/test_e2e.py -m integration -v` → 9 tests pass, 0 skips
- [x] GOLDEN_HASH in `test_optimization.py` updated to `efea3141a0df8870e77df15f987fdf61f89745225fcb7d6f54cff9c790779732`
- [x] 3 `@pytest.mark.integration` tests in `test_optimization.py` pass with fixture data
- [ ] Full suite shows no new failures — verified by: `uv run pytest tests/ --ignore=tests/test_selector.py -q`

### Ticker Split Constraints

- [x] `TEST_TICKERS_TEST_ONLY` in `tests/conftest.py` contains ≥ 1 ticker — verified by `test_test_only_tickers_at_least_one`
- [x] `TEST_TICKERS_TEST_ONLY` is ≤ 50% of `TEST_ALL_TICKERS` — verified by `test_test_only_tickers_at_most_fifty_pct`
- [x] `TEST_TICKERS_TRAIN` and `TEST_TICKERS_TEST_ONLY` are disjoint — verified by `test_no_overlap_between_train_and_test_only`

### Out of Scope

- Injecting `TEST_EXTRA_TICKERS = ["NVDA"]` into the `train.py` subprocess — NVDA is test-only only at the conftest level, not in `__main__` for subprocess tests
- Modifying `prepare.py` or the default `CACHE_DIR` behavior
- Adding tickers beyond AAPL, MSFT, NVDA to the test fixture
- Changing the default values of `WALK_FORWARD_WINDOWS` or `FOLD_TEST_DAYS` constants

---

## COMPLETION CHECKLIST

- [x] Task 1.1: `_compute_fold_params` added to train.py mutable section, validates correctly
- [x] Task 1.2: `tests/conftest.py` created with `test_parquet_fixtures` fixture
- [x] Task 1.3: `tests/test_fold_auto_detect.py` created with 13 unit tests
- [x] Wave 1 checkpoint: `uv run pytest tests/test_fold_auto_detect.py -x` passes — 13 passed
- [x] Task 2.1: `__main__` block updated to call `_compute_fold_params`
- [x] Task 2.2: `test_e2e.py` updated: fixtures, subprocess env injection, EXPECTED_TICKERS
- [x] Wave 2 checkpoint: `uv run pytest tests/test_e2e.py -m integration -x` passes — 9 passed
- [x] Task 3.1: GOLDEN_HASH recomputed and updated in test_optimization.py
- [x] **Post-plan scope addition**: 3 `@_live` tests in `test_optimization.py` converted to `@pytest.mark.integration` using `test_parquet_fixtures` — no longer depend on 389-ticker CACHE_DIR
- [x] Final validation: 222 passed, 1 pre-existing failure (`test_most_recent_train_commit_modified_only_editable_section` — commit ecbc2d2 predates this feature), 0 new failures introduced
- [x] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [x] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Ticker selection rationale

AAPL, MSFT, NVDA were chosen because:
1. All are large-cap S&P 500 members with complete hourly data in yfinance
2. High daily volume ensures reliable `price_1030am` extraction from the 9:30 bar
3. September–October 2024 was a trending period for tech stocks → relaxed screener should produce ≥1 trade for `test_agent_loop_*` tests
4. 3 tickers: 2 training (67%) + 1 test-only (33%) satisfies ≤50% test-only constraint

### Test window rationale

- `TEST_BACKTEST_START = "2024-09-01"` matches train.py's current `BACKTEST_START`
  so in-process `run_backtest(all_ticker_dfs)` with default args uses our data
- `TEST_BACKTEST_END = "2024-11-01"` gives ~43 bdays (2 months) — short enough to
  trigger fold auto-detection (if TRAIN_END is set ~14 calendar days before)
- `TEST_HISTORY_START = "2023-09-01"` provides 1 year of warmup → 252+ history rows
  before the backtest starts, satisfying `screen_day`'s `len(df) < 102` guard

### Subprocess test behavior with small dataset

The subprocess tests (`test_train_exits_zero_with_pnl_output`, `test_output_has_all_seven_fields`)
run `train.py` with its current `TRAIN_END="2026-03-06"` and `WALK_FORWARD_WINDOWS=7`.
The fold windows (in 2025–2026) don't overlap with the small dataset (Sep–Nov 2024),
so all folds produce 0 trades. The tests check output FORMAT only (field names present,
exit code 0) — not trade counts — so they pass correctly with 0-trade output.

### DO NOT EDIT zone modification

Task 2.1 intentionally modifies the DO NOT EDIT zone. This is a developer/maintainer
change to the harness infrastructure, not an agent optimization. The golden hash must
be updated (Task 3.1) to reflect the intentional change. The `test_most_recent_train_commit_modified_only_editable_section` test (in test_optimization.py) checks git blame — it will detect this __main__ block change. If that test fails after this change, it is expected and acceptable (we are intentionally modifying the harness).
