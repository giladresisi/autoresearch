# Code Review — Phase 5 End-to-End Integration

**Reviewer**: Claude Sonnet 4.6
**Date**: 2026-03-20
**Branch**: master (unstaged changes)

## Stats

- Files Modified: 2 (`prepare.py`, `tests/test_prepare.py`)
- Files Added: 1 (`tests/test_e2e.py`)
- Files Deleted: 0
- New lines: ~83
- Deleted lines: ~14

## Baseline

All 77 pre-existing passing tests continue to pass. 1 skipped test (pre-existing, `@pytest.mark.integration`). Zero regressions introduced.

---

## Issues Found

---

```
severity: low
file: tests/test_e2e.py
line: 2
issue: Unused import — `unittest.mock as mock`
detail: `mock` is imported at module level (`import ast, importlib.util, os, pathlib,
  subprocess, tempfile, unittest.mock as mock`) but is never referenced anywhere in the
  file. Likely a copy-paste from test_prepare.py which does use it.
suggestion: Remove `unittest.mock as mock` from the import line on line 2.
```

---

```
severity: low
file: tests/test_e2e.py
line: 2
issue: Top-level `os` import shadows without use; re-imported locally as `_os`
detail: `os` is imported at module level on line 2 but is never used at module level.
  Inside `test_agent_loop_threshold_mutation_no_crash` it is re-imported locally as
  `_os` (line 194). The top-level `os` import is dead code.
suggestion: Remove `os` from the top-level import on line 2. The local `import os as _os`
  inside the test function is sufficient.
```

---

```
severity: low
file: tests/test_e2e.py
line: 81
issue: Unused `tmp_path` fixture argument in `test_train_exits_zero_with_sharpe_output`
detail: The function signature is `def test_train_exits_zero_with_sharpe_output(skip_if_cache_missing, tmp_path):`
  but `tmp_path` is never referenced in the function body. pytest will still inject and
  create a temporary directory for nothing.
suggestion: Remove `tmp_path` from the function signature.
```

---

```
severity: low
file: tests/test_prepare.py
line: 213
issue: Temp file written into project root, not system temp directory
detail: `test_main_exits_1_when_tickers_empty` passes `dir=project_root` to
  `NamedTemporaryFile`, so the ephemeral `.py` file lands in the repository root
  (e.g. `C:\Users\gilad\projects\autoresearch\tmpXXXXXX.py`) rather than the system
  temp directory. The `finally: os.unlink()` cleans it up in normal operation, but if
  the Python process is killed before `finally` runs (e.g. SIGKILL, power loss), the
  file persists. It is not covered by `.gitignore` (which only ignores `*.py[oc]`, not
  `*.py` source files), so it could be accidentally staged and committed.
suggestion: Remove `dir=project_root` to use the system temp directory (the default).
  The `uv run python <path>` subprocess call works with absolute paths regardless of
  where the temp file lives, so no functional change is needed.
```

---

```
severity: low
file: tests/test_e2e.py (untracked file: pytest-e2e.log)
line: N/A
issue: `pytest-e2e.log` artifact not covered by `.gitignore`
detail: The plan instructs running `uv run pytest tests/ -v --tb=short 2>&1 | tee pytest-e2e.log`,
  producing `pytest-e2e.log` in the repo root. This file is currently untracked and is not
  listed in `.gitignore`. If someone runs `git add .` or `git add -A`, this build artifact
  will be staged and committed.
suggestion: Add `pytest-e2e.log` (or `*.log`) to `.gitignore`.
```

---

```
severity: low
file: prepare.py
line: 47-49
issue: Stale docstrings — `validate_ticker_data` and `resample_to_daily` still reference "10am"
detail: `resample_to_daily` docstring says "daily OHLCV + price_10am" without noting the
  9:30 AM source. `validate_ticker_data` docstring says "missing 10am bars". The inline
  comment inside `resample_to_daily` (line 58-60) correctly explains the 9:30 AM change,
  but the function-level docstring was not updated, so a reader of the docstring alone
  gets the wrong mental model.
  The PRD (prd.md:170) also still specifies `price_10am` as "Open of the 10:00 AM ET bar"
  — this is a PRD non-conformance, though the code change is pragmatically correct
  (yfinance 1h bars are labeled at bar-open: 9:30, not 10:00).
suggestion: Update `resample_to_daily` docstring to: "Convert hourly yfinance data to daily
  OHLCV + price_10am (9:30 AM ET open, the first 1h bar of each trading day)."
  Update `validate_ticker_data` docstring to replace "10am bars" with "9:30 AM bars".
  Optionally update prd.md:170 to reflect the yfinance behavior.
```

---

```
severity: low
file: tests/test_prepare.py
line: 69
issue: Test name `test_price_10am_is_open_of_10am_bar` is now misleading
detail: The test was renamed conceptually in comments but the function name still says
  "10am_bar". It now asserts the 9:30 AM bar, making the name incorrect. This misleads
  anyone reading test output or the test list.
suggestion: Rename to `test_price_10am_is_open_of_930am_bar` to match the updated logic
  and comments.
```

---

## No Issues (verified clean)

- **Logic of 9:30 AM fix** (`prepare.py:61`): `datetime.time(9, 30)` correctly extracts the market-open bar. Verified against synthetic data that `resample_to_daily` picks the 9:30 bar Open (100.0) not the 10:00 bar Open (100.1).

- **String replacement fragility** (`tests/test_prepare.py:226`): The `source.replace(...)` with `count=1` correctly finds and patches the TICKERS line. The exact string `'TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "TSLA"]'` appears exactly once in `prepare.py`. Verified.

- **try/finally cleanup in `test_agent_loop_threshold_mutation_no_crash`** (`tests/test_e2e.py:246-252`): `_os.unlink(tmp_path)` is inside `finally`, so the temp file is cleaned up even if `exec_module` or `run_backtest` raises. Correct.

- **Guard strings in `test_agent_loop`**: All four assertion guards (`c0 < -50`, `pct_local >= 0.08`, the stop-block, `res_atr < 2.0`) exist verbatim in `train.py`. The test will not false-fail due to string mismatches.

- **P&L tolerance math** (`tests/test_e2e.py:177`): Tolerance of `0.05 * n` is a 10× safety margin over the worst-case rounding error of `0.005 * n`. Arithmetic is sound.

- **`train.BACKTEST_END` reference** (`tests/test_e2e.py:261`): `BACKTEST_END` is a module-level constant in `train.py` and is accessible as `train.BACKTEST_END`. Confirmed.

- **`skip_if_cache_missing` fixture propagation**: The fixture is `scope="module"` and uses `pytest.skip()`, which correctly skips all dependent tests when the Parquet cache is absent. No silent failures.

- **Encoding fix (arrows)** (`prepare.py:111,121,122`): `→` replaced with `->` correctly resolves Windows cp1252 encoding errors on print.

- **Pre-existing test suite**: 77 pass, 1 skip — no regressions from Phase 5 changes.
