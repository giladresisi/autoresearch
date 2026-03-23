# Code Review: V3-F Test-Universe Holdout and Cache Path

**Date**: 2026-03-22
**Plan**: `.agents/plans/v3-f-test-universe-holdout-and-cache-path.md`

## Stats

- Files Modified: 4 (`train.py`, `prepare.py`, `tests/test_optimization.py`, `program.md`)
- Files Added: 1 (`tests/test_v3_f.py`)
- Files Deleted: 0
- New lines: +50 (code) / +124 (docs/tests)
- Deleted lines: -4

## Test Results

All tests pass:
- `tests/test_v3_f.py`: 10/10 passed
- `tests/test_optimization.py`: 39/39 passed, 1 skipped

---

## Issues Found

### Medium

```
severity: medium
file: tests/test_v3_f.py
line: 111-121 (also 129-137, 147-155)
issue: `import importlib` inside each env-var test is imported but never used
detail: Tests 6, 7, and 8 each contain `import importlib` inside the function body (lines 111, 129, 147). The `importlib` module is never called — the reimport is done with plain `import train`/`import prepare`. This is dead code that misleads readers into thinking `importlib.reload()` is being used.
suggestion: Remove the `import importlib` line from all three test functions.
```

```
severity: medium
file: tests/test_v3_f.py
line: 121, 137, 155
issue: Bare `import train` / `import prepare` at the end of env-var tests does not restore the module-level binding
detail: At the end of tests 6, 7, and 8, the final bare `import train` (or `import prepare`) executes inside the function scope. In Python, this rebinds the local name, not the module-level `train` name imported at line 8. Its only practical effect is restoring `sys.modules['train']` — but at that point monkeypatch has NOT yet reversed the env var (that happens after the test function returns). So the module placed back into `sys.modules['train']` at line 121 has the custom CACHE_DIR, not the default. Test 7 correctly deletes and reimports again, so there is no actual test failure, but the intent is misleading. A reader expects the line to "restore the original train module", which it does not do.
suggestion: Either remove the final `import train` / `import prepare` lines (they are not needed — pytest's test isolation handles cleanup), or add a comment explaining their limited purpose: `# restore sys.modules for subsequent tests (module-level binding is unaffected)`.
```

### Low

```
severity: low
file: train.py
line: 4
issue: Module docstring still says "Do NOT modify: CACHE_DIR" but CACHE_DIR is now env-var driven, not a user-editable constant
detail: The docstring at line 4 reads: "Do NOT modify: CACHE_DIR, load_ticker_data(), Sharpe computation, or the output block format." This instruction was accurate when CACHE_DIR was hardcoded. After V3-F it remains directionally correct (agents still must not hardcode it), but the phrase "Do NOT modify: CACHE_DIR" now conflicts with the new intent — which is that CACHE_DIR *is* configurable, just via env var rather than in-code edits. The program.md "Do NOT" list was updated to match, but the in-file docstring was not.
suggestion: Update line 4 to: "Do NOT modify: CACHE_DIR in code (set AUTORESEARCH_CACHE_DIR env var instead), load_ticker_data(), Sharpe computation, or the output block format."
```

```
severity: low
file: tests/test_v3_f.py
line: 2
issue: Top-level `import importlib` is absent; instead three in-function copies are imported — inconsistent with standard import organization
detail: `importlib` appears three times as a local import (lines 111, 129, 147) rather than once at the top of the file. All other imports in the file are top-level. Even though `importlib` is unused (see medium issue above), if it were needed it should be a single top-level import.
suggestion: Move to top-level if kept; remove entirely if unused (preferred fix, see medium issue above).
```

---

## Pre-existing Issues (not introduced by this changeset)

```
severity: medium (pre-existing, from V3-E)
file: train.py
line: 779
issue: KeyError when WALK_FORWARD_WINDOWS=0
detail: Line 779 evaluates `_fold_train_stats["total_pnl"]` as the default value in `.get("pnl_min", ...)`. When `WALK_FORWARD_WINDOWS=0`, the fold loop never runs and `_fold_train_stats` remains `{}` (the guard initialized at line 736). Python evaluates default arguments eagerly, so `{}["total_pnl"]` raises `KeyError` before `.get()` can return anything. This code path was present before V3-F.
```

---

## Summary

The V3-F implementation is correct and all tests pass. The two main features — `TEST_EXTRA_TICKERS` holdout and `AUTORESEARCH_CACHE_DIR` env-var override — are implemented as specified in the plan and are backward-compatible. The dict merge pattern (`{**_train_ticker_dfs, **_extra_ticker_dfs}`) is safe: if a ticker appears in both sides, the `_extra_ticker_dfs` value (same object from `ticker_dfs`) wins, which is benign.

The three issues are all code-quality concerns in the test file and one docstring inconsistency. No security issues, no logic errors in production code, no performance problems.
