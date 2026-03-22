# Feature: V3-E Configurable Walk-Forward Window Size and Rolling Training Windows

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Replace two hardcoded `10`-business-day values in the walk-forward evaluation loop with configurable constants (`FOLD_TEST_DAYS`, `FOLD_TRAIN_DAYS`), and add a rolling-training-window code path. This makes test folds statistically meaningful (enough trades per fold to distinguish signal from noise) and allows training windows to span genuinely different market regimes.

**Pre-requisite:** V3-D must be complete and present in `train.py`. Current `GOLDEN_HASH`:
```
9fba956b62e48a93d40a8ab6f386c6674bb96bd7efcfef793db198d4a078749e
```
This is the V3-D baseline hash. V3-E will produce a different hash because the immutable zone (walk-forward loop inside `__main__`) is modified.

## User Story

As an optimization-agent operator,
I want walk-forward folds that are long enough to be statistically meaningful and spread across diverse market regimes,
So that `min_test_pnl` measures "resilient across diverse conditions" rather than "consistent within a single recent 6-week window."

## Problem Statement

The V3-B/D walk-forward loop hardcodes `10` business days for both the test window width and the step between folds. On 85 tickers with a momentum screener, a 10-business-day window yields only ~10–30 trades — too thin to distinguish a 60% win rate from random noise (the 95% CI on a binomial proportion with n=20 spans ~30 percentage points). With `WALK_FORWARD_WINDOWS = 3`, all three test folds cluster within the most recent 6 weeks of the backtest window (3 × 10 = 30 business days ≈ 6 weeks), all in the same market regime. `min_test_pnl` therefore measures "consistent in recent 6 weeks" rather than "resilient across diverse conditions."

Two separate problems require two separate fixes:

| Problem | Fix |
|---------|-----|
| Test folds too short (~15 trades, wide CI, looks like noise) | `FOLD_TEST_DAYS` constant — set to 20 (≈1 calendar month, ~40–100 trades on 85 tickers) |
| All folds in same recent regime — training windows nearly identical to each other | `FOLD_TRAIN_DAYS` constant — `0` = expanding (simple, more training data); `120` = 6-month rolling (more diverse regimes per fold) |

## Solution Statement

1. Add two named constants to the mutable section of `train.py` (above the DO NOT EDIT line):
   - `FOLD_TEST_DAYS = 20` — test window width in business days per fold
   - `FOLD_TRAIN_DAYS = 0` — training window width; `0` = expanding from `BACKTEST_START`
2. Replace the two hardcoded `10` values in the walk-forward loop (immutable zone) with `FOLD_TEST_DAYS`.
3. Add a `FOLD_TRAIN_DAYS` branch: when `> 0`, compute `_fold_train_start` as
   `max(fold_train_end − FOLD_TRAIN_DAYS business days, BACKTEST_START)`.
   When `== 0`, use `BACKTEST_START` (expanding — matches current behavior).
4. Update `WALK_FORWARD_WINDOWS` recommendation and document both new constants in `program.md` step 4b.
5. Recompute and update `GOLDEN_HASH` in `tests/test_optimization.py`.

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: Low ✅
**Primary Systems Affected**:
- `train.py` — mutable constants block (above DO NOT EDIT), and walk-forward loop in `__main__` (below DO NOT EDIT)
- `program.md` — step 4b agent setup instructions
- `tests/test_optimization.py` — `GOLDEN_HASH` constant

**Dependencies**: None new — `BDay` is already imported inside the loop at line 701; `date` is already imported at line 7.

**Breaking Changes**: No. Setting `FOLD_TEST_DAYS = 10` and `FOLD_TRAIN_DAYS = 0` exactly reproduces V3-B/D fold boundaries. Default values in code are set to the new recommended values (`FOLD_TEST_DAYS = 20`).

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` lines 1–50 — Mutable constants block. `WALK_FORWARD_WINDOWS = 3` at line 28. New constants go immediately after this line.
- `train.py` line 296 — `# ── DO NOT EDIT BELOW THIS LINE ───...` boundary. Everything below is immutable zone.
- `train.py` lines 698–731 — Walk-forward loop in `__main__` (immutable zone):
  - Line 698: comment `# R2: Walk-forward CV — N folds with 10-business-day test windows`
  - Line 701: `from pandas.tseries.offsets import BDay as _BDay`
  - Line 708: `for _i in range(WALK_FORWARD_WINDOWS):`
  - Line 712: `_fold_test_end_ts   = _train_end_ts - _BDay(_steps_back * 10)` ← replace `10`
  - Line 713: `_fold_test_start_ts = _fold_test_end_ts - _BDay(10)` ← replace `10`
  - Line 714: `_fold_train_end_ts  = _fold_test_start_ts`
  - Line 721: `_fold_train_stats = run_backtest(_train_ticker_dfs, start=BACKTEST_START, end=_fold_train_end)` ← expand into if/else
- `tests/test_optimization.py` lines 104–128 — `test_harness_below_do_not_edit_is_unchanged`; `GOLDEN_HASH` at line 118.
- `tests/test_optimization.py` lines 116–117 — Hash recompute command (already in source as a comment).
- `program.md` lines 44–53 — Step 4b walk-forward boundary setup; replace with expanded version that documents `FOLD_TEST_DAYS` and `FOLD_TRAIN_DAYS`.
- `tests/test_program_md.py` lines 1–146 — Structural tests for `program.md`. Review all assertions so that no required string is removed during the `program.md` update.

### New Files to Create

None.

### Patterns to Follow

**Constant placement**: All mutable constants live above line 296 (`# ── DO NOT EDIT BELOW THIS LINE`). New constants go directly after `WALK_FORWARD_WINDOWS = 3` at line 28, following the existing comment-per-constant style (multi-line block comment above each constant).

**BDay usage in immutable zone**: Already imported as `_BDay` at line 701 (inside the for loop's scope). Use the same alias. No new import needed.

**date.fromisoformat usage**: `date` is imported at line 7 (`from datetime import date`). The immutable zone already uses it at line 366 (`s = date.fromisoformat(start or BACKTEST_START)`). Use `date.fromisoformat(BACKTEST_START)` for the clamping comparison.

**GOLDEN_HASH recompute command** (paste output into test file after the immutable zone change):
```
python -c "import hashlib; s=open('train.py').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"
```
Run from the repo root directory. The output is a 64-character hex string.

---

## PARALLEL EXECUTION STRATEGY

**Total tasks**: 5
**Parallel (Wave 1)**: 3 tasks — 1.1 (add mutable constants), 1.2 (edit immutable loop), 1.3 (update program.md) are fully independent.
**Sequential after Wave 1**: 1 task — 2.1 (recompute GOLDEN_HASH) depends on 1.2 being complete.
**Sequential after Wave 2**: 1 task — 3.1 (run full test suite) depends on all edits being complete.
**Max speedup**: ~2.5×

**Interface Contracts**:

| Wave | Provides | Required by |
|------|----------|-------------|
| 1.1 | `FOLD_TEST_DAYS`, `FOLD_TRAIN_DAYS` constants in mutable section | 3.1 (tests read the file) |
| 1.2 | Updated walk-forward loop referencing `FOLD_TEST_DAYS`/`FOLD_TRAIN_DAYS` | 2.1 (hash computed from this content) |
| 1.3 | Updated `program.md` step 4b | 3.1 (test_program_md.py reads it) |
| 2.1 | New `GOLDEN_HASH` in `tests/test_optimization.py` | 3.1 (test asserts this hash) |

---

### WAVE 1: Independent Edits (All Parallel)

#### Task 1.1: Add FOLD_TEST_DAYS and FOLD_TRAIN_DAYS constants to train.py mutable section

- **WAVE**: 1
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: Two new named constants in the mutable block, available for the walk-forward loop to reference at runtime.

**IMPLEMENT**:

Open `train.py`. Locate line 28:
```python
WALK_FORWARD_WINDOWS = 3
```

After that line, insert a blank line and then the following block. Preserve the same indentation style (no indent — module-level constants):

```python

# Test window width in business days per fold.
# 20 ≈ 1 calendar month → ~40–100 trades on 85 tickers; enough to distinguish skill from noise.
# Set at session setup. Do NOT change during the loop.
FOLD_TEST_DAYS = 20

# Training window width in business days.
# 0 = expanding: each fold trains from BACKTEST_START to its test window's start (all prior history).
# N > 0 = rolling: each fold trains on only the N most recent business days before its test window,
#   exposing successive folds to genuinely different market slices.
# Recommended: 0 (expanding) for simplicity; 120 (≈6 months) for maximum regime diversity.
# Set at session setup. Do NOT change during the loop.
FOLD_TRAIN_DAYS = 0
```

**Do NOT** change `WALK_FORWARD_WINDOWS`, `SILENT_END`, or any other constant. Do NOT touch anything below line 296.

- **VALIDATE**: `grep -n "FOLD_TEST_DAYS\|FOLD_TRAIN_DAYS" train.py` — both constants must appear at line numbers below 50 and above 296.
- **IF_FAILS**: If the constants appear in the wrong location, check that the insertion point is immediately after `WALK_FORWARD_WINDOWS = 3` and not inside a function or class.

---

#### Task 1.2: Replace hardcoded 10 values and add rolling-window branch in walk-forward loop (immutable zone)

- **WAVE**: 1
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: Updated walk-forward loop that uses `FOLD_TEST_DAYS` for all window calculations and supports `FOLD_TRAIN_DAYS` rolling-window mode.

**IMPLEMENT**:

Open `train.py`. The walk-forward loop is in the `__main__` block (immutable zone, below line 296). Make exactly these four edits:

**Edit 1** — Update comment at ~line 698:

Old:
```python
    # R2: Walk-forward CV — N folds with 10-business-day test windows
```
New:
```python
    # R2: Walk-forward CV — N folds with FOLD_TEST_DAYS-business-day test windows (V3-E)
```

**Edit 2** — Replace two hardcoded `10` values in fold window calculation (~lines 712–713):

Old:
```python
        _fold_test_end_ts   = _train_end_ts - _BDay(_steps_back * 10)
        _fold_test_start_ts = _fold_test_end_ts - _BDay(10)
        _fold_train_end_ts  = _fold_test_start_ts
```
New:
```python
        _fold_test_end_ts   = _train_end_ts - _BDay(_steps_back * FOLD_TEST_DAYS)
        _fold_test_start_ts = _fold_test_end_ts - _BDay(FOLD_TEST_DAYS)
        _fold_train_end_ts  = _fold_test_start_ts
```

**Edit 3** — Replace the single `run_backtest` call for training with the `FOLD_TRAIN_DAYS` if/else branch (~line 721):

Old:
```python
        _fold_train_stats = run_backtest(_train_ticker_dfs, start=BACKTEST_START, end=_fold_train_end)
```
New:
```python
        if FOLD_TRAIN_DAYS > 0:
            _fold_train_start_ts = _fold_train_end_ts - _BDay(FOLD_TRAIN_DAYS)
            _fold_train_start = str(max(_fold_train_start_ts.date(),
                                        date.fromisoformat(BACKTEST_START)))
        else:
            _fold_train_start = BACKTEST_START
        _fold_train_stats = run_backtest(_train_ticker_dfs, start=_fold_train_start, end=_fold_train_end)
```

**Critical details**:
- Use 8 spaces of indentation (inside the `for _i in range(WALK_FORWARD_WINDOWS):` loop).
- The `if FOLD_TRAIN_DAYS > 0:` block adds 4 lines. The `else:` adds 1 line. The `_fold_train_stats = ...` line moves down by 5 lines.
- `date` is already imported at the module level (`from datetime import date`). `_BDay` is already imported inside the loop body. No new imports needed.
- `_fold_train_end` (the string) is already computed at line 716: `_fold_train_end = str(_fold_train_end_ts.date())`. The new `_fold_train_start` replaces the hardcoded `BACKTEST_START` argument.
- Do NOT touch the `_fold_test_stats` call, the `print_results` calls, the `fold_test_pnls` list, or any other line in the loop.

- **VALIDATE**:
  ```
  grep -n "FOLD_TEST_DAYS\|FOLD_TRAIN_DAYS\|_fold_train_start" train.py
  ```
  Expected: all three names appear at line numbers above 750 (inside the immutable zone loop).

- **IF_FAILS**: If the indentation is wrong (Python SyntaxError), count the spaces carefully — the `for` loop body uses 8-space indent, the `if` body uses 12-space indent.

---

#### Task 1.3: Update program.md step 4b to document new constants

- **WAVE**: 1
- **AGENT_ROLE**: docs-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: Updated agent instructions that include `FOLD_TEST_DAYS`, `FOLD_TRAIN_DAYS`, and updated `WALK_FORWARD_WINDOWS` recommendation.

**IMPLEMENT**:

Open `program.md`. Locate step **4b** (around line 44). The current text:

```
4b. **Compute train/test split and walk-forward boundaries**:
    - `TRAIN_END = BACKTEST_END − 14 calendar days`
      (e.g. BACKTEST_END `2026-03-20` → TRAIN_END `2026-03-06`)
    - `TEST_START = TRAIN_END` (same date — kept for backward reference)
    - `SILENT_END = TRAIN_END − 14 calendar days`
      (e.g. TRAIN_END `2026-03-06` → SILENT_END `2026-02-20`)

    Write `TRAIN_END`, `TEST_START`, and `SILENT_END` into the mutable section of `train.py`.
    `WALK_FORWARD_WINDOWS` is already set to 3 — leave it unless the user specifies otherwise.
```

Replace the entire block with:

```
4b. **Compute train/test split and walk-forward boundaries**:
    - `TRAIN_END = BACKTEST_END − 14 calendar days`
      (e.g. BACKTEST_END `2026-03-20` → TRAIN_END `2026-03-06`)
    - `TEST_START = TRAIN_END` (same date — kept for backward reference)
    - `SILENT_END = TRAIN_END − 14 calendar days`
      (e.g. TRAIN_END `2026-03-06` → SILENT_END `2026-02-20`)

    Write `TRAIN_END`, `TEST_START`, and `SILENT_END` into the mutable section of `train.py`.

    **Walk-forward fold constants** (V3-E — set once at session setup, do NOT change during the loop):
    - `FOLD_TEST_DAYS` — test window width in business days per fold.
      Default `20` (≈1 calendar month, ~40–100 trades on 85 tickers). Set to `10` to reproduce
      legacy V3-B/D behavior (not recommended — too few trades per fold to be meaningful).
    - `FOLD_TRAIN_DAYS` — training window width in business days.
      `0` = expanding (train from `BACKTEST_START`; more training data per fold; simpler).
      `120` = 6-month rolling window (exposes successive folds to genuinely different market
      slices; recommended when maximizing regime diversity is the goal).
      Default `0` (expanding).
    - `WALK_FORWARD_WINDOWS` — recommended values for the 19-month window (2024-09 → 2026-03):
      - `9` with `FOLD_TEST_DAYS=20, FOLD_TRAIN_DAYS=0`: 9 months of test coverage (~Jun 2025
        → Mar 2026), ~18 backtest calls/iteration, ~30–60 s per iteration.
      - `13` with `FOLD_TEST_DAYS=20, FOLD_TRAIN_DAYS=120`: full 19-month coverage, ~26
        backtest calls/iteration, ~45–90 s per iteration.
      Leave at `3` only if the user explicitly specifies legacy configuration.
```

**Do NOT** remove or change any other part of `program.md`. Before saving, verify that none of the strings checked by `tests/test_program_md.py` are missing. Key strings that must remain present:
- `# autoresearch`
- `~/.cache/autoresearch/stock_data`
- `uv run train.py > run.log 2>&1`
- `git reset --hard HEAD~1`
- `min_test_pnl`
- `run_backtest`
- `BACKTEST_START`, `BACKTEST_END`
- `WALK_FORWARD_WINDOWS`

- **VALIDATE**: `grep -n "FOLD_TEST_DAYS\|FOLD_TRAIN_DAYS" program.md` — both constant names must appear in step 4b.
- **IF_FAILS**: If `test_program_md.py` tests fail after this change, compare each failing assertion against the removed text and restore the missing required string.

---

**Wave 1 Checkpoint**: After all three Wave 1 tasks complete, run:
```bash
grep -n "FOLD_TEST_DAYS\|FOLD_TRAIN_DAYS" train.py
grep -n "FOLD_TEST_DAYS\|FOLD_TRAIN_DAYS" program.md
python -c "import py_compile; py_compile.compile('train.py'); print('syntax OK')"
```
Confirm: constants appear in both mutable section and immutable zone of `train.py`, both appear in `program.md`, and `train.py` has no syntax errors.

---

### WAVE 2: Recompute GOLDEN_HASH (After Task 1.2)

#### Task 2.1: Recompute and update GOLDEN_HASH in tests/test_optimization.py

- **WAVE**: 2
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: [1.2]
- **BLOCKS**: [3.1]
- **PROVIDES**: Updated `GOLDEN_HASH` constant that reflects the new immutable-zone content, allowing `test_harness_below_do_not_edit_is_unchanged` to pass.

**Background**: The test at `tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged` computes SHA-256 of everything in `train.py` below the `# ── DO NOT EDIT BELOW THIS LINE` marker and asserts it equals `GOLDEN_HASH`. V3-E changes the walk-forward loop (which is inside `__main__`, below the marker), so the hash changes. The test intentionally fails to alert maintainers — we must update the hash to acknowledge the intentional change.

**IMPLEMENT**:

Step 1 — Recompute the hash. From the repo root, run:
```
python -c "import hashlib; s=open('train.py').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"
```
This prints a 64-character hex string. Copy it.

Step 2 — Update `tests/test_optimization.py` line 118. Replace:
```python
    GOLDEN_HASH = "9fba956b62e48a93d40a8ab6f386c6674bb96bd7efcfef793db198d4a078749e"
```
with:
```python
    GOLDEN_HASH = "<hex-string-from-step-1>"
```
where `<hex-string-from-step-1>` is the 64-character hex string printed in step 1.

Step 3 — Verify: run the hash command a second time and confirm the output still matches the value you just pasted. (File must not have changed between the two runs.)

- **VALIDATE**: Run `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v` — must PASS.
- **IF_FAILS**: If the hash test still fails, the most common cause is that `train.py` was modified after the hash was computed (e.g., an editor auto-saved whitespace). Rerun the hash command and paste the new value.

---

**Wave 2 Checkpoint**: Confirm `GOLDEN_HASH` in test file is exactly 64 hex characters, differs from the V3-D value (`9fba956b62e48a93d40a8ab6f386c6674bb96bd7efcfef793db198d4a078749e`), and the hash test passes.

---

### WAVE 3: Test Suite Validation (After All Waves)

#### Task 3.1: Run full test suite and verify all tests pass

- **WAVE**: 3
- **AGENT_ROLE**: tester
- **DEPENDS_ON**: [1.1, 1.2, 1.3, 2.1]
- **BLOCKS**: []
- **PROVIDES**: Confirmation that all existing tests pass with the V3-E changes in place.

**IMPLEMENT**:

Run:
```bash
uv run pytest tests/ -v 2>&1 | tail -50
```

**Expected results by test**:

| Test | Expected outcome | Notes |
|------|-----------------|-------|
| `test_harness_below_do_not_edit_is_unchanged` | PASS | Requires updated `GOLDEN_HASH` from task 2.1 |
| `test_most_recent_train_commit_modified_only_editable_section` | SKIP | No new commit yet — pre-existing skip condition |
| `test_program_md_*` (all in `test_program_md.py`) | PASS | No required strings removed from `program.md` |
| All `test_backtester.py`, `test_screener.py`, `test_selector.py` | PASS | Unaffected files |
| All `test_prepare.py`, `test_registry.py`, `test_e2e.py` | PASS | Unaffected files |
| `test_optimization.py` functional tests | PASS | `run_backtest` logic unchanged |

**Zero FAILED or ERROR lines are acceptable.** The one pre-existing SKIP (`test_most_recent_train_commit_modified_only_editable_section`) is acceptable.

**If `test_harness_below_do_not_edit_is_unchanged` fails**: Go back to task 2.1 and recompute the hash.

**If any `test_program_md.py` test fails**: Go back to task 1.3 and restore the missing required string.

**If any other test fails**: Investigate before declaring done. Check whether the failure is pre-existing (run `git stash && uv run pytest tests/ -v` to compare against the baseline, then `git stash pop`).

- **VALIDATE**: `uv run pytest tests/ -v --tb=short 2>&1 | grep -E "^(PASSED|FAILED|ERROR|tests/)" | grep -v PASSED | grep -v SKIP` — output should be empty (no failures or errors).

---

**Wave 3 Checkpoint**: All tests pass. Implementation complete.

---

## Validation Scenarios

The following end-to-end scenarios validate V3-E correctness at the `train.py` execution level. They require a live parquet cache (`~/.cache/autoresearch/stock_data/`) and are therefore manual — not part of the automated test suite.

### Scenario A: Backward Compatibility

Configure:
```python
FOLD_TEST_DAYS       = 10
FOLD_TRAIN_DAYS      = 0
WALK_FORWARD_WINDOWS = 3
```
Run `uv run python train.py > run.log 2>&1`. Compare fold boundary lines in `run.log` against a V3-D baseline run with the same constants. Fold dates must match exactly.

### Scenario B: New Default (9 Folds, Expanding)

Configure:
```python
FOLD_TEST_DAYS       = 20
FOLD_TRAIN_DAYS      = 0
WALK_FORWARD_WINDOWS = 9
```
Run `uv run python train.py > run.log 2>&1`. Expected:
- 9 × `fold{N}_train_...` and 9 × `fold{N}_test_...` blocks appear
- `fold9_test_backtest_end:` ≈ `TRAIN_END`
- `fold1_test_backtest_start:` ≈ `TRAIN_END − 180 business days` (~9 months earlier)
- `fold{N}_train_backtest_start:` is `BACKTEST_START` for all folds (expanding)

### Scenario C: Rolling Training Window

Configure:
```python
FOLD_TEST_DAYS       = 20
FOLD_TRAIN_DAYS      = 120
WALK_FORWARD_WINDOWS = 9
```
Run `uv run python train.py > run.log 2>&1`. Expected:
- `fold1_train_backtest_start:` is `BACKTEST_START` (clamped — 120 bdays before fold 1 train end would precede `BACKTEST_START`)
- `fold9_train_backtest_start:` is `fold9_test_start − 120 business days`
- No fold's train start precedes `BACKTEST_START`

---

## Performance Impact

| Configuration | Walk-forward calls/iteration | Estimated time/iteration |
|---------------|------------------------------|--------------------------|
| V3-D baseline (`WALK_FORWARD_WINDOWS=3, FOLD_TEST_DAYS=10`) | 6 | ~10–15 s |
| New default (`WALK_FORWARD_WINDOWS=9, FOLD_TEST_DAYS=20, FOLD_TRAIN_DAYS=0`) | 18 | ~30–60 s |
| Maximum diversity (`WALK_FORWARD_WINDOWS=13, FOLD_TRAIN_DAYS=120`) | 26 | ~45–90 s |

A 30-iteration session with the new default runs in ~15–30 minutes — viable overnight. Operators can always reduce `WALK_FORWARD_WINDOWS` to trade off coverage for speed.

---

## Backward Compatibility Notes

- `FOLD_TEST_DAYS = 10, FOLD_TRAIN_DAYS = 0` reproduces V3-B/D fold boundaries exactly.
- The default values in code (`FOLD_TEST_DAYS = 20`) are the new recommended values; the agent sets them at session setup per `program.md` step 4b.
- All existing optimization sessions (branches) that have already been running with the V3-D harness will continue to work: they only need to add the two constants to the mutable section and update `WALK_FORWARD_WINDOWS` if desired.

---

## Test Automation Summary

| Test | Type | File | Run Command |
|------|------|------|-------------|
| `test_harness_below_do_not_edit_is_unchanged` | Automated ✅ | `tests/test_optimization.py` | `uv run pytest tests/test_optimization.py::test_harness_below_do_not_edit_is_unchanged -v` |
| Full test suite regression (all 39+ tests) | Automated ✅ | `tests/` | `uv run pytest tests/ -v` |
| `program.md` structural integrity | Automated ✅ | `tests/test_program_md.py` | `uv run pytest tests/test_program_md.py -v` |
| Backward-compat fold boundaries (Scenario A) | Manual ⚠️ | N/A | Requires live parquet cache — cannot automate without bundling ~85 × 19-month parquet fixtures as test data (out of scope) |
| New default 9-fold output (Scenario B) | Manual ⚠️ | N/A | Same reason |
| Rolling window clamping (Scenario C) | Manual ⚠️ | N/A | Same reason |

**Automated**: 3 test groups, 39+ individual tests (all existing), 100%  coverage of code paths reachable without live data.

**Manual**: 3 scenarios — all require live parquet cache. The `FOLD_TRAIN_DAYS > 0` branch is a 3-line conditional that is low-risk and straightforward; the golden-hash test guards the exact text of the surrounding loop, providing strong structural assurance even without an E2E data test.

---

## Coverage Review

### New code paths introduced by V3-E

| Code path | Automated coverage | Gap? |
|-----------|-------------------|------|
| `FOLD_TEST_DAYS` substitution in fold step (`_steps_back * FOLD_TEST_DAYS`) | `test_harness_below_do_not_edit_is_unchanged` (hash guards exact text) | ✅ Covered |
| `FOLD_TEST_DAYS` substitution in fold window (`_BDay(FOLD_TEST_DAYS)`) | Same hash test | ✅ Covered |
| `FOLD_TRAIN_DAYS == 0` branch: `_fold_train_start = BACKTEST_START` | Hash test + existing functional tests | ✅ Covered |
| `FOLD_TRAIN_DAYS > 0` branch: rolling window calculation | Manual Scenario C | ⚠️ Manual only — live data required |
| `max(...)` clamping against `BACKTEST_START` | Manual Scenario C | ⚠️ Manual only — live data required |
| New mutable constants `FOLD_TEST_DAYS`, `FOLD_TRAIN_DAYS` accessible in immutable zone | Python syntax check + runtime load | ✅ Covered |
| `program.md` step 4b documentation update | `tests/test_program_md.py` structural tests | ✅ Covered |

### Existing tests re-validated by this change

- `test_optimization.py::test_harness_below_do_not_edit_is_unchanged` — directly updated (new hash)
- `test_optimization.py::test_most_recent_train_commit_modified_only_editable_section` — skips (no commit); when next commit happens, it will verify the correct sections were modified
- `test_program_md.py::*` — all 18 structural assertions re-verified; no required strings removed from `program.md` during step 4b rewrite
- All other test files — no tested files changed; pass unchanged

### Gap analysis conclusion

Two manual-only gaps exist (rolling-window code path, clamping logic). Both require live parquet data to exercise. Automating them would require committing ~85 × 19-month parquet files as test fixtures, which is impractical and out of scope. The gaps are accepted and documented.

---

## Acceptance Criteria

- [ ] `FOLD_TEST_DAYS = 20` appears in the mutable section of `train.py` (above line 296), immediately after `WALK_FORWARD_WINDOWS`.
- [ ] `FOLD_TRAIN_DAYS = 0` appears in the mutable section of `train.py`, immediately after `FOLD_TEST_DAYS`.
- [ ] Each new constant has a multi-line block comment matching the style of `WALK_FORWARD_WINDOWS` and `SILENT_END`.
- [ ] In the walk-forward loop (immutable zone), `_BDay(_steps_back * FOLD_TEST_DAYS)` replaces `_BDay(_steps_back * 10)`.
- [ ] In the walk-forward loop, `_BDay(FOLD_TEST_DAYS)` replaces the second `_BDay(10)`.
- [ ] The `FOLD_TRAIN_DAYS > 0` rolling-window if/else branch is present with `max(...)` clamping against `date.fromisoformat(BACKTEST_START)`.
- [ ] The `FOLD_TRAIN_DAYS == 0` path calls `run_backtest(..., start=BACKTEST_START, ...)` — identical to V3-D behavior.
- [ ] `GOLDEN_HASH` in `tests/test_optimization.py` is updated to the new 64-hex-character value (different from V3-D hash).
- [ ] `program.md` step 4b documents `FOLD_TEST_DAYS`, `FOLD_TRAIN_DAYS`, and updated `WALK_FORWARD_WINDOWS` recommendations for the 19-month window.
- [ ] `uv run pytest tests/ -v` — zero FAILED or ERROR lines (the pre-existing git-state SKIP is acceptable).
- [ ] `python -c "import py_compile; py_compile.compile('train.py')"` exits with no error (syntax valid).
- [ ] No changes made to any part of `train.py` above `WALK_FORWARD_WINDOWS` or to any line in the immutable zone other than the explicit walk-forward loop substitution described in task 1.2.
