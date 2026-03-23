# Feature: V3-F Test-Universe Ticker Holdout and Per-Session Cache Path

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

V3-F adds two independent improvements to the harness:

1. **`TEST_EXTRA_TICKERS` mutable constant** — a list of tickers included in walk-forward TEST folds but never in training. Forces `min_test_pnl` to measure cross-ticker generalization, not just cross-time generalization. The agent cannot directly overfit to these tickers because it only sees their aggregate contribution via `min_test_pnl`.

2. **Per-session `CACHE_DIR` via environment variable** — replaces the hardcoded `~/.cache/autoresearch/stock_data` constant in both `train.py` and `prepare.py` with `os.environ.get("AUTORESEARCH_CACHE_DIR", <default>)`. Prevents silent data staleness when two sessions need different date ranges for overlapping tickers.

Both changes are backward-compatible: `TEST_EXTRA_TICKERS = []` produces identical fold behavior; the env-var fallback is the current hardcoded path.

**Pre-requisite:** V3-E complete (all prior V3 changes present in `train.py`). ✅ Confirmed by PROGRESS.md.

## User Story

As a developer running autoresearch optimization sessions,
I want the walk-forward test folds to include unseen tickers, and want independent sessions to use isolated caches,
So that `min_test_pnl` measures genuine out-of-universe generalization, and cache files from one session never silently corrupt another.

## Problem Statement

**Problem 1:** The walk-forward loop passes `_train_ticker_dfs` to both `_fold_train_stats` and `_fold_test_stats`. The test fold validates on the exact tickers it trained on, so `min_test_pnl` only measures cross-time generalization, not cross-ticker generalization. An agent can achieve high `min_test_pnl` by memorizing the specific behavior of the 85 training names.

**Problem 2:** `CACHE_DIR` is hardcoded to `~/.cache/autoresearch/stock_data/`. When two sessions use overlapping tickers with different date ranges, `prepare.py` skips existing files (idempotent), causing the second session to silently use stale data from the first.

## Solution Statement

**Solution 1:** Add `TEST_EXTRA_TICKERS: list = []` to the mutable section. In the immutable `__main__` block, after the train/holdout split, build `_extra_ticker_dfs` from `TEST_EXTRA_TICKERS`, construct `_test_ticker_dfs = {**_train_ticker_dfs, **_extra_ticker_dfs}`, and pass `_test_ticker_dfs` to the test call of each fold (training calls still use `_train_ticker_dfs`).

**Solution 2:** Replace the hardcoded `CACHE_DIR` in both `train.py` (mutable section) and `prepare.py` with `os.environ.get("AUTORESEARCH_CACHE_DIR", os.path.join(...))`. Update `GOLDEN_HASH` after immutable-zone changes.

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: Low
**Primary Systems Affected**: `train.py` (mutable + immutable zones), `prepare.py`, `program.md`, `tests/test_optimization.py`
**Dependencies**: None (no new external libraries)
**Breaking Changes**: No — both changes have backward-compatible defaults

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` lines 1–62 (mutable section) — Where `CACHE_DIR`, `TEST_EXTRA_TICKERS` constant lives; also holds `TICKER_HOLDOUT_FRAC` and other V3 constants (pattern to follow)
- `train.py` lines 696–775 (`__main__` block) — Walk-forward loop where `_train_ticker_dfs` is built; where the fold-test call happens on line 741; where `_extra_ticker_dfs` and `_test_ticker_dfs` must be introduced
- `prepare.py` lines 19–57 (user config section) — Current `CACHE_DIR` definition to update
- `tests/test_optimization.py` lines 106–128 — `GOLDEN_HASH` test; must update hash after immutable-zone change
- `program.md` lines 40–68 — Session setup steps 4b/5; must add `TEST_EXTRA_TICKERS` and `AUTORESEARCH_CACHE_DIR` docs

### New Files to Create

- `tests/test_v3_f.py` — Dedicated test module for V3-F unit tests

### Patterns to Follow

- **Mutable constants**: match the inline-comment style of `TICKER_HOLDOUT_FRAC` (train.py:58–62) — capital name, doc comment explaining semantics and agent guidance
- **Immutable dict construction**: match the R6 holdout pattern (train.py:702–709) — build filtered dict via dict comprehension, guard against empty result
- **GOLDEN_HASH recompute**: run `python -c "import hashlib; s=open('train.py').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"` after editing immutable zone

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────────────┐
│ WAVE 1: Independent Parallel Changes                        │
├─────────────────────────────────────────────────┬──────────┤
│ Task 1.1: Mutable-zone changes (train.py)        │ Task 1.2: prepare.py CACHE_DIR │
│ + TEST_EXTRA_TICKERS constant                    │ env-var pattern                 │
│ + CACHE_DIR env-var pattern                      │                                 │
│ Agent: code-editor                               │ Agent: code-editor              │
└─────────────────────────────────────────────────┴──────────┘
                              ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 2: Immutable-zone change (train.py __main__)           │
├────────────────────────────────────────────────────────────┤
│ Task 2.1: Add _extra_ticker_dfs / _test_ticker_dfs logic    │
│ Deps: 1.1 (TEST_EXTRA_TICKERS constant must exist)          │
└────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 3: Downstream updates (parallel)                       │
├─────────────────────────────────────────────────┬──────────┤
│ Task 3.1: Recompute GOLDEN_HASH                  │ Task 3.2: program.md update    │
│ + update test_optimization.py                    │ + tests/test_v3_f.py           │
│ Deps: 2.1                                        │ Deps: 2.1                       │
└─────────────────────────────────────────────────┴──────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1 (train.py mutable changes) and 1.2 (prepare.py change) — no dependencies between them
**Wave 2 — Sequential after Wave 1**: Task 2.1 (immutable zone) — needs TEST_EXTRA_TICKERS constant from 1.1
**Wave 3 — Parallel after Wave 2**: Tasks 3.1 (GOLDEN_HASH) and 3.2 (program.md + tests) can proceed in parallel

### Interface Contracts

**Contract 1**: Task 1.1 provides `TEST_EXTRA_TICKERS: list = []` in mutable section of `train.py` → Task 2.1 consumes it in `__main__`
**Contract 2**: Task 2.1 provides updated `__main__` (immutable zone) → Task 3.1 provides new GOLDEN_HASH of that zone

### Synchronization Checkpoints

**After Wave 1**: `python -c "import train; print(train.TEST_EXTRA_TICKERS)"` → should print `[]`
**After Wave 2**: `python -c "import hashlib; s=open('train.py').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"` → prints new hash
**After Wave 3**: `uv run pytest tests/test_optimization.py tests/test_v3_f.py -v` → all pass

---

## IMPLEMENTATION PLAN

### Phase 1: Mutable-Zone and prepare.py Updates (parallel)

#### Task 1.1: ADD `TEST_EXTRA_TICKERS` constant and update `CACHE_DIR` in train.py (mutable zone)

**Location**: `train.py` lines 1–62 (before the `DO NOT EDIT` marker)

Two distinct changes:

**Change A — `TEST_EXTRA_TICKERS` constant:**
Add immediately after the `TICKER_HOLDOUT_FRAC` block (after line 61):

```python
# Tickers included in walk-forward TEST folds only — never in training.
# Used to measure out-of-universe generalization: min_test_pnl must hold on
# tickers the agent has never directly optimized for.
# These tickers must be downloaded by prepare.py before running.
# Set at session setup. Do NOT change during the loop.
# When using TEST_EXTRA_TICKERS, set TICKER_HOLDOUT_FRAC = 0 to avoid
# overlap between the two mechanisms.
TEST_EXTRA_TICKERS: list = []
```

**Change B — `CACHE_DIR` env-var pattern:**
Replace the current hardcoded `CACHE_DIR` on line 12:
```python
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")
```
with:
```python
# Cache directory for parquet files. Override with AUTORESEARCH_CACHE_DIR env var
# to maintain independent datasets for different sessions or date ranges.
CACHE_DIR = os.environ.get(
    "AUTORESEARCH_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data"),
)
```

**Validation**: `python -c "import train; print(train.TEST_EXTRA_TICKERS, train.CACHE_DIR)"`
Expected: `[] /home/user/.cache/autoresearch/stock_data` (or Windows equivalent)

#### Task 1.2: UPDATE `CACHE_DIR` in prepare.py

**Location**: `prepare.py` line 57

Replace:
```python
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")
```
with:
```python
# Cache directory for parquet files. Override with AUTORESEARCH_CACHE_DIR env var
# to maintain independent datasets for different sessions or date ranges.
CACHE_DIR = os.environ.get(
    "AUTORESEARCH_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data"),
)
```

Note: This is in the "Derived (do not modify)" section of prepare.py, not in the user config block. Keep the existing comment block around it; just replace the `CACHE_DIR =` line.

**Validation**: `python -c "import prepare; print(prepare.CACHE_DIR)"`

**Wave 1 Checkpoint**: Both tasks complete, `import train` and `import prepare` both succeed without errors.

---

### Phase 2: Immutable-Zone Update (sequential after Wave 1)

#### Task 2.1: ADD `_extra_ticker_dfs` / `_test_ticker_dfs` logic in train.py `__main__`

**Location**: `train.py` immutable zone, in the `__main__` block

This change is inside the `DO NOT EDIT` zone and **requires a GOLDEN_HASH update** (Task 3.1).

**Current code** (lines 702–709, after loading ticker data):
```python
    # R6: Ticker holdout — deterministic tail split of sorted ticker list
    _all_tickers_sorted = sorted(ticker_dfs.keys())
    _n_holdout = round(TICKER_HOLDOUT_FRAC * len(_all_tickers_sorted)) if TICKER_HOLDOUT_FRAC > 0 else 0
    _holdout_set = set(_all_tickers_sorted[-_n_holdout:]) if _n_holdout > 0 else set()
    _train_ticker_dfs = {t: df for t, df in ticker_dfs.items() if t not in _holdout_set}
    _holdout_ticker_dfs = {t: df for t, df in ticker_dfs.items() if t in _holdout_set}
    if not _train_ticker_dfs:
        _train_ticker_dfs = ticker_dfs  # safety: if holdout fraction is too large, fall back
```

**Add immediately after line 709** (after the `_train_ticker_dfs` safety guard):
```python

    # V3-F: Test-extra tickers — included in fold test calls only, never in training.
    _extra_ticker_dfs = {t: ticker_dfs[t] for t in TEST_EXTRA_TICKERS if t in ticker_dfs}
    _test_ticker_dfs  = {**_train_ticker_dfs, **_extra_ticker_dfs}
```

**Current fold test call** (line 741):
```python
        _fold_test_stats  = run_backtest(_train_ticker_dfs, start=_fold_test_start, end=_fold_test_end)
```

**Replace with**:
```python
        _fold_test_stats  = run_backtest(_test_ticker_dfs,  start=_fold_test_start, end=_fold_test_end)
```

That is the only change to the fold loop. Training calls (`_fold_train_stats`) continue to use `_train_ticker_dfs`. The R6 ticker holdout evaluation (line 755) and the silent holdout evaluation (line 767) are unchanged.

**Validation**: `python -c "import train"` — should import without error

**Wave 2 Checkpoint**: `python -c "import train; print('OK')"` succeeds; `grep '_test_ticker_dfs' train.py` shows the new construction and fold test call.

---

### Phase 3: GOLDEN_HASH + Tests + program.md (parallel after Wave 2)

#### Task 3.1: UPDATE GOLDEN_HASH in tests/test_optimization.py

**Why**: The immutable zone of `train.py` was changed in Task 2.1. The SHA-256 integrity test (`test_harness_below_do_not_edit_is_unchanged`) will fail with the stale hash.

**Steps**:
1. Recompute the new hash:
   ```bash
   python -c "import hashlib; s=open('train.py', encoding='utf-8').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"
   ```
2. Open `tests/test_optimization.py` line 118 and replace the `GOLDEN_HASH` value with the new hash.

**Validation**: `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v` → PASS

#### Task 3.2: CREATE tests/test_v3_f.py and UPDATE program.md

**Part A — tests/test_v3_f.py**

Create a new test file `tests/test_v3_f.py` with the following tests:

```python
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
    import importlib
    import sys
    if "train" in sys.modules:
        del sys.modules["train"]
    import train as train_fresh
    assert train_fresh.CACHE_DIR == custom, (
        f"Expected CACHE_DIR={custom!r} after env override, got {train_fresh.CACHE_DIR!r}"
    )
    # Restore
    del sys.modules["train"]
    import train  # re-import original for other tests


def test_prepare_cache_dir_env_var_overrides_default(monkeypatch, tmp_path):
    """AUTORESEARCH_CACHE_DIR env var must override the default in prepare module too."""
    custom = str(tmp_path / "prepare_cache")
    monkeypatch.setenv("AUTORESEARCH_CACHE_DIR", custom)
    import importlib
    import sys
    if "prepare" in sys.modules:
        del sys.modules["prepare"]
    import prepare as prepare_fresh
    assert prepare_fresh.CACHE_DIR == custom, (
        f"Expected CACHE_DIR={custom!r} after env override, got {prepare_fresh.CACHE_DIR!r}"
    )
    del sys.modules["prepare"]
    import prepare


# ── Test 7: Default CACHE_DIR unchanged when env var absent ───────────────

def test_cache_dir_default_unchanged_without_env_var(monkeypatch):
    """When AUTORESEARCH_CACHE_DIR is not set, CACHE_DIR must be the legacy default."""
    monkeypatch.delenv("AUTORESEARCH_CACHE_DIR", raising=False)
    import importlib
    import sys
    if "train" in sys.modules:
        del sys.modules["train"]
    import train as t2
    expected = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")
    assert t2.CACHE_DIR == expected, (
        f"Expected default CACHE_DIR={expected!r}, got {t2.CACHE_DIR!r}"
    )
    del sys.modules["train"]
    import train


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
```

**Part B — program.md update**

In `program.md`, within step 4b ("Compute train/test split and walk-forward boundaries"), add a new subsection after the `WALK_FORWARD_WINDOWS` guidance:

```markdown
    **V3-F — Test-extra tickers and cache path**:
    - `TEST_EXTRA_TICKERS` — tickers included in fold TEST calls only, never in training.
      These tickers must already be downloaded by `prepare.py` (add them to `TICKERS` in
      `prepare.py` before running; after caching, leave them in `prepare.py`'s `TICKERS`
      but also list them in `TEST_EXTRA_TICKERS` in `train.py`).
      Default `[]` (no extra tickers — fold test universe equals training universe).
      When using `TEST_EXTRA_TICKERS`, set `TICKER_HOLDOUT_FRAC = 0` to avoid overlap.
      Suggested extras (2 per sector, not in the default 85-ticker universe):
        Tech: INTC, CSCO | Financials: TFC, USB | Healthcare: BMY, CVS
        Energy: PSX, HES | Consumer Staples: MDLZ, SYY | Industrials: EMR, ITW
        Consumer Discretionary: DG, YUM | Materials: DD, PKG
    - `AUTORESEARCH_CACHE_DIR` — if two sessions need different date ranges for overlapping
      tickers, set this env var to an alternate cache directory before running both
      `prepare.py` and `train.py`:
        ```bash
        export AUTORESEARCH_CACHE_DIR=~/.cache/autoresearch/stock_data_alt
        uv run prepare.py
        uv run train.py
        ```
      Default (env var absent): `~/.cache/autoresearch/stock_data` (unchanged behavior).
```

Also in `program.md`, add `TEST_EXTRA_TICKERS` to the "Agent CANNOT modify" list if present, or otherwise note in the "What the agent CANNOT modify" section:

```markdown
- `TEST_EXTRA_TICKERS` — set at session setup; do NOT change during the loop
- `CACHE_DIR` — determined at startup from env var; do NOT modify in code
```

**Validation**:
```bash
uv run pytest tests/test_v3_f.py -v         # all 9 tests pass
uv run pytest tests/test_optimization.py -v  # GOLDEN_HASH test and all others pass
```

---

## STEP-BY-STEP TASKS

Tasks organized by execution wave. Same wave = safe to run in parallel.

---

### WAVE 1: Foundation (Parallel)

#### Task 1.1: UPDATE train.py mutable section (CACHE_DIR + TEST_EXTRA_TICKERS)

- **WAVE**: 1
- **AGENT_ROLE**: code-editor
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: `TEST_EXTRA_TICKERS: list = []` constant; `CACHE_DIR` env-var pattern in mutable zone
- **IMPLEMENT**:
  1. Read `train.py` lines 1–62 to confirm current state
  2. Replace `CACHE_DIR = os.path.join(...)` (line 12) with env-var-with-fallback pattern (3-line form; see Phase 1 Task 1.1 above)
  3. Add `TEST_EXTRA_TICKERS: list = []` with full doc comment immediately after `TICKER_HOLDOUT_FRAC` block (after line 61)
- **VALIDATE**: `python -c "import train; print(train.TEST_EXTRA_TICKERS, train.CACHE_DIR)"`

#### Task 1.2: UPDATE prepare.py CACHE_DIR

- **WAVE**: 1
- **AGENT_ROLE**: code-editor
- **DEPENDS_ON**: []
- **BLOCKS**: [3.2]
- **PROVIDES**: `CACHE_DIR` env-var pattern in `prepare.py`
- **IMPLEMENT**:
  1. Read `prepare.py` lines 49–60 to confirm current state
  2. Replace `CACHE_DIR = os.path.join(...)` (line 57) with env-var-with-fallback pattern (same 3-line form as train.py)
  3. Keep surrounding comment block ("Derived (do not modify)") intact
- **VALIDATE**: `python -c "import prepare; print(prepare.CACHE_DIR)"`

**Wave 1 Checkpoint**: `python -c "import train, prepare; print(train.TEST_EXTRA_TICKERS, train.CACHE_DIR, prepare.CACHE_DIR)"` — no errors, outputs `[] <default_path> <default_path>`

---

### WAVE 2: Immutable Zone (After Wave 1)

#### Task 2.1: ADD _extra_ticker_dfs and _test_ticker_dfs in train.py __main__

- **WAVE**: 2
- **AGENT_ROLE**: code-editor
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1, 3.2]
- **PROVIDES**: `_extra_ticker_dfs` / `_test_ticker_dfs` construction; fold test call uses `_test_ticker_dfs`
- **IMPLEMENT**:
  1. Read `train.py` lines 700–745 to confirm current state
  2. After the `_train_ticker_dfs` safety guard (line 709: `_train_ticker_dfs = ticker_dfs`), add a blank line then the two V3-F lines:
     ```python

         # V3-F: Test-extra tickers — included in fold test calls only, never in training.
         _extra_ticker_dfs = {t: ticker_dfs[t] for t in TEST_EXTRA_TICKERS if t in ticker_dfs}
         _test_ticker_dfs  = {**_train_ticker_dfs, **_extra_ticker_dfs}
     ```
  3. On the fold test call line (currently line 741), change `_train_ticker_dfs` → `_test_ticker_dfs`:
     ```python
         _fold_test_stats  = run_backtest(_test_ticker_dfs,  start=_fold_test_start, end=_fold_test_end)
     ```
  4. **Do NOT** change: `_fold_train_stats` call, R6 holdout call, silent holdout call — these all stay on `_train_ticker_dfs` / `_holdout_ticker_dfs` / `ticker_dfs` respectively
- **VALIDATE**: `python -c "import train; print('immutable zone imports OK')"` — no error

**Wave 2 Checkpoint**: `grep -n "_test_ticker_dfs\|_extra_ticker_dfs" train.py` — shows construction (2 lines) and fold test call (1 line), total 3 hits

---

### WAVE 3: GOLDEN_HASH + Tests + program.md (Parallel after Wave 2)

#### Task 3.1: RECOMPUTE GOLDEN_HASH and update test_optimization.py

- **WAVE**: 3
- **AGENT_ROLE**: code-editor
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: []
- **PROVIDES**: Updated GOLDEN_HASH reflecting V3-F immutable zone changes
- **IMPLEMENT**:
  1. Run: `python -c "import hashlib; s=open('train.py', encoding='utf-8').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"`
  2. Copy the output hash
  3. Open `tests/test_optimization.py` line 118, replace the value of `GOLDEN_HASH` with the new hash
- **VALIDATE**: `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v` → PASS

#### Task 3.2: CREATE tests/test_v3_f.py and UPDATE program.md

- **WAVE**: 3
- **AGENT_ROLE**: test-writer + docs-writer
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: []
- **PROVIDES**: 9 automated unit tests for V3-F; program.md documentation for `TEST_EXTRA_TICKERS` and `AUTORESEARCH_CACHE_DIR`
- **IMPLEMENT**:
  1. Create `tests/test_v3_f.py` with all 9 tests from the TESTING STRATEGY section above (use exact code from Task 3.2 Part A)
  2. Read `program.md` lines 40–70 to find the correct insertion point for V3-F documentation
  3. In `program.md` step 4b, add the "V3-F — Test-extra tickers and cache path" block after the `WALK_FORWARD_WINDOWS` guidance (see Task 3.2 Part B above)
  4. Also update the "Agent CANNOT modify" list in `program.md` to include `TEST_EXTRA_TICKERS` and a note about `CACHE_DIR`
- **VALIDATE**: `uv run pytest tests/test_v3_f.py -v` → all 9 pass

**Wave 3 Checkpoint**: `uv run pytest tests/test_optimization.py tests/test_v3_f.py -v` — all pass

---

## TESTING STRATEGY

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_v3_f.py` | **Run**: `uv run pytest tests/test_v3_f.py -v`

| # | Test | Coverage |
|---|------|----------|
| 1 | `test_test_extra_tickers_constant_exists` | Constant present, type list, default [] |
| 2 | `test_test_extra_tickers_in_mutable_section` | Constant in above-marker section |
| 3 | `test_empty_extra_tickers_produces_same_test_universe` | Backward compat: [] → no change to test universe |
| 4 | `test_extra_tickers_included_in_test_but_not_train` | Extra ticker in test_ticker_dfs, absent from train_ticker_dfs |
| 5 | `test_extra_ticker_not_in_cache_is_skipped` | KeyError protection: `if t in ticker_dfs` guard |
| 6 | `test_cache_dir_env_var_overrides_default` (train) | AUTORESEARCH_CACHE_DIR → train.CACHE_DIR |
| 7 | `test_prepare_cache_dir_env_var_overrides_default` | AUTORESEARCH_CACHE_DIR → prepare.CACHE_DIR |
| 8 | `test_cache_dir_default_unchanged_without_env_var` | Absent env var → legacy default path |
| 9 | `test_test_ticker_dfs_in_immutable_section` | Source check: _extra_ticker_dfs / _test_ticker_dfs in below-marker |
| 10 | `test_fold_test_call_uses_test_ticker_dfs` | Source check: fold test call uses _test_ticker_dfs |

### Integration Test (GOLDEN_HASH)

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged` | **Run**: `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v`

### Regression Test

**Status**: ✅ Automated | **Tool**: pytest | **Run**: `uv run pytest -v --tb=short`

Full test suite must pass with no new failures. Pre-existing failures (3 pre-existing failures in test_registry.py, 1 pre-existing skip per PROGRESS.md V3-E) are acceptable as long as count does not increase.

### Manual Tests

**Status**: ⚠️ Manual — requires live parquet cache

#### Manual Test 1: TEST_EXTRA_TICKERS integration

**Why Manual**: Requires real parquet cache files for extra tickers (INTC, CSCO or similar) that are not committed to the repo
**Steps**:
1. Ensure INTC and CSCO are in `prepare.py`'s `TICKERS` and have been downloaded
2. Set `TEST_EXTRA_TICKERS = ["INTC", "CSCO"]` in `train.py` mutable section
3. Run `uv run train.py 2>&1 | tee run.log`
4. Verify fold test result blocks show trades from INTC/CSCO (check via grep or trades.tsv)
5. Verify fold train result blocks do NOT include INTC/CSCO trades
6. Restore `TEST_EXTRA_TICKERS = []`

**Expected**: Extra tickers contribute to fold test P&L; training fold output unchanged vs baseline

#### Manual Test 2: AUTORESEARCH_CACHE_DIR path isolation

**Why Manual**: Requires two separate parquet cache directories, which requires running prepare.py twice — environment-dependent, not feasible in unit test
**Steps**:
1. `mkdir ~/.cache/autoresearch/stock_data_alt`
2. `AUTORESEARCH_CACHE_DIR=~/.cache/autoresearch/stock_data_alt uv run prepare.py`
3. Verify parquet files appear in the alt directory: `ls ~/.cache/autoresearch/stock_data_alt/`
4. `AUTORESEARCH_CACHE_DIR=~/.cache/autoresearch/stock_data_alt uv run train.py`
5. Verify output block appears; verify it reads from alt cache (pnl may differ from default cache if different date range)

**Expected**: prepare.py writes to alt dir; train.py reads from alt dir; default cache unaffected

### Coverage Review

| Code path | Test type | Status |
|---|---|---|
| `TEST_EXTRA_TICKERS` constant default | Unit (test 1) | ✅ |
| Constant in mutable section | Unit (test 2) | ✅ |
| Empty list → backward compat | Unit (test 3) | ✅ |
| Extra ticker in test_dfs only | Unit (test 4) | ✅ |
| Missing cache file → silent skip | Unit (test 5) | ✅ |
| train.CACHE_DIR env override | Unit (test 6) | ✅ |
| prepare.CACHE_DIR env override | Unit (test 7) | ✅ |
| CACHE_DIR default without env | Unit (test 8) | ✅ |
| `_extra_ticker_dfs` in immutable zone | Source check (test 9) | ✅ |
| Fold test uses `_test_ticker_dfs` | Source check (test 10) | ✅ |
| GOLDEN_HASH integrity | Integration | ✅ |
| Full test suite regression | Integration | ✅ |
| Real parquet cache, extra tickers | Manual 1 | ⚠️ requires live cache |
| Real alternate cache dir | Manual 2 | ⚠️ requires env + prepare run |

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest) | 10 | 83% |
| ✅ Integration (GOLDEN_HASH + full suite) | 2 | 17% |
| ⚠️ Manual | 2 | — (supplemental only) |
| **Total automated** | 12 | 100% (code paths) |

Manual tests require a live parquet cache that cannot be committed to the repo. All code paths exercisable with synthetic data are automated.

---

## VALIDATION COMMANDS

### Level 1: Import checks

```bash
python -c "import train; print(train.TEST_EXTRA_TICKERS, train.CACHE_DIR)"
python -c "import prepare; print(prepare.CACHE_DIR)"
```

### Level 2: New unit tests

```bash
uv run pytest tests/test_v3_f.py -v
```

### Level 3: GOLDEN_HASH test

```bash
uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v
```

### Level 4: Full regression suite

```bash
uv run pytest -v --tb=short 2>&1 | tail -20
```

Expected: all V3-F tests pass; pre-existing failures/skips unchanged.

---

## ACCEPTANCE CRITERIA

- [ ] `TEST_EXTRA_TICKERS: list = []` constant added to train.py mutable section with full doc comment
- [ ] `CACHE_DIR` in train.py uses `os.environ.get("AUTORESEARCH_CACHE_DIR", <default>)` pattern
- [ ] `CACHE_DIR` in prepare.py uses `os.environ.get("AUTORESEARCH_CACHE_DIR", <default>)` pattern
- [ ] `_extra_ticker_dfs` and `_test_ticker_dfs` built in `__main__` immutable zone after holdout split
- [ ] Fold test call uses `_test_ticker_dfs`; fold train call still uses `_train_ticker_dfs`
- [ ] GOLDEN_HASH in `tests/test_optimization.py` updated to match new immutable zone
- [ ] `tests/test_v3_f.py` created with 10 tests (9 V3-F + fold-test-uses-test-ticker-dfs source check)
- [ ] `program.md` documents `TEST_EXTRA_TICKERS` setup and `AUTORESEARCH_CACHE_DIR` env var
- [ ] `uv run pytest tests/test_v3_f.py -v` — all pass
- [ ] `uv run pytest tests/test_optimization.py -v` — GOLDEN_HASH test passes
- [ ] `uv run pytest -v --tb=short` — no new failures beyond pre-existing baseline

---

## COMPLETION CHECKLIST

- [ ] Wave 1 tasks complete (train.py mutable + prepare.py)
- [ ] Wave 2 task complete (immutable zone _test_ticker_dfs)
- [ ] Wave 3 tasks complete (GOLDEN_HASH + tests + program.md)
- [ ] Each task validation command passed
- [ ] All validation levels executed (1–4)
- [ ] Full test suite passes (no new failures)
- [ ] ⚠️ Debug logs added during execution REMOVED (keep pre-existing)
- [ ] ⚠️ CRITICAL: Changes UNSTAGED — NOT committed

---

## NOTES

### Interaction with R6 (TICKER_HOLDOUT_FRAC)

`TEST_EXTRA_TICKERS` and `TICKER_HOLDOUT_FRAC` are orthogonal but can conflict:
- R6 removes tickers from `_train_ticker_dfs` (time-axis holdout: train period, withheld tickers)
- V3-F adds tickers to `_test_ticker_dfs` (ticker-axis: test folds, extra tickers)
- When both are active, a ticker in `TEST_EXTRA_TICKERS` that happens to also be in the R6 holdout set would be excluded from training AND appear in test folds via the extra path — which is fine mathematically, but confusing conceptually. The doc comment recommends `TICKER_HOLDOUT_FRAC = 0` when using `TEST_EXTRA_TICKERS` to keep mechanisms clean.

### CACHE_DIR placement in train.py

`CACHE_DIR` is currently at line 12, before `BACKTEST_START` / `BACKTEST_END`. This is in the mutable zone but is listed as "Do NOT modify" in the original file docstring. The env-var pattern makes the constant technically still present in the mutable zone but its effective value is now driven by the environment. The agent instructions in `program.md` (and the existing "CANNOT modify" list) should note that `CACHE_DIR` must not be hardcoded during optimization runs.

### Why _test_ticker_dfs does not affect the silent holdout

The silent holdout call on line 767 uses `ticker_dfs` (all loaded tickers), not `_test_ticker_dfs`. This is intentional: the silent holdout is a full-universe final evaluation, not a walk-forward fold. `TEST_EXTRA_TICKERS` tickers, once downloaded, will automatically appear in `ticker_dfs` and therefore already participate in the silent holdout — no code change needed there.

### Why env-var is read at module import time

Python module-level constants are evaluated once at import. This means `CACHE_DIR` captures `AUTORESEARCH_CACHE_DIR` at the moment `import train` / `import prepare` executes. Shell sessions must set the env var before spawning the Python process. Tests that need to override `CACHE_DIR` must reload the module (delete from `sys.modules` then re-import), as demonstrated in tests 6–8.
