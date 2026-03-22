# Execution Report: V3-B Walk-Forward Evaluation Framework

**Date:** 2026-03-22
**Plan:** `.agents/plans/v3-b-walk-forward.md`
**Executor:** Sequential (single agent, 7 tasks across 3 waves)
**Outcome:** Success

---

## Executive Summary

V3-B replaces the single train/test split with a rolling walk-forward cross-validation loop (N=3 folds, 10-business-day test windows) and adds a silent holdout window. R7 diagnostics (max_drawdown, calmar, pnl_consistency) were added to `run_backtest()` and `print_results()`, and `program.md` was updated to reflect the new evaluation framework. All 7 tasks completed successfully; 9 new tests added and passing alongside the existing 4.

**Key Metrics:**
- **Tasks Completed:** 7/7 (100%)
- **Tests Added:** 9
- **Test Pass Rate:** 12/13 (92.3% — 1 pre-existing skip unrelated to this feature)
- **Files Modified:** 3 (train.py, tests/test_optimization.py, program.md)
- **Lines Changed:** +184/-1 (test file), +110/-55 (train.py estimated), +50/-70 (program.md estimated)
- **Execution Time:** ~45 minutes
- **Alignment Score:** 9/10

---

## Implementation Summary

### Wave 1 — Mutable section constants (Tasks 1 and 5, parallel)

`WALK_FORWARD_WINDOWS = 3` and `SILENT_END = "2026-02-20"` were added to `train.py`'s mutable section between `WRITE_FINAL_OUTPUTS` and `RISK_PER_TRADE`, exactly as specified.

`program.md` was updated across five sub-tasks (5a–5e): step 4b now instructs the agent to compute SILENT_END; the output format section was rewritten for N×2 fold blocks + min_test_pnl + silent_pnl; the keep/discard criterion changed from `train_total_pnl` to `min_test_pnl`; the note block was updated; the results.tsv schema gained `train_calmar` and `train_pnl_consistency` columns; and the final test run section was updated to reference holdout_ prefix.

### Wave 2 — Immutable zone changes (Tasks 2, 3, 4)

**Task 2 (run_backtest R7):** Added `cumulative_realized = 0.0` and `equity_curve: list[tuple] = []` before the main loop. `cumulative_realized` increments on each stop-triggered close and each end-of-backtest forced close. `equity_curve.append((today, cumulative_realized + portfolio_value))` appended in the mark-to-market step. Post-loop computation matches plan spec exactly: numpy accumulate for peak, max_drawdown, calmar guard, and per-month last-equity dictionary for pnl_consistency. Early-exit return dict updated with `max_drawdown: 0.0, calmar: 0.0, pnl_consistency: 0.0`.

**Task 3 (print_results R7):** Two lines appended after `backtest_end`: `calmar:` (4 decimal places) and `pnl_consistency:` (2 decimal places), using `.get()` with defaults to handle legacy callers.

**Task 4 (__main__ walk-forward loop):** Full replacement of the `__main__` block. Uses pandas `BDay` offsets to derive fold boundaries dynamically from `TRAIN_END` and `WALK_FORWARD_WINDOWS`. Underscore-prefixed locals prevent name collisions. `_write_trades_tsv` called with the last fold's train records. Silent holdout runs `[TRAIN_END, BACKTEST_END]`; prints `silent_pnl: HIDDEN` unless `WRITE_FINAL_OUTPUTS` is True.

### Wave 3 — Test updates (Tasks 6 and 7, parallel)

**Task 6:** GOLDEN_HASH recomputed to `796595717636d21cc47c897589e08fa8cf3c8d9c34cc8dedfc80957c7f64fe3d` after immutable zone changes.

**Task 7:** 9 new tests added under a `# V3-B Tests` section header.

---

## Divergences from Plan

### Divergence #1: Test 7.6 and 7.7 execution mechanism

**Classification:** GOOD

**Planned:** Tests 7.6 and 7.7 were specified in the plan using `runpy.run_path()` or a subprocess approach to execute the `__main__` block.

**Actual:** A `_exec_main_block(extra_ns: dict)` helper was introduced. It reads the source, slices from the `if __name__ == "__main__":` line, overlays `extra_ns` onto a copy of `vars(train)`, and executes via `exec(compile(...))`. This gives the executed block access to all mock objects in `extra_ns` without the namespace isolation problem of `runpy.run_path`.

**Reason:** `runpy.run_path` creates a fresh namespace, so `mock.patch.object(train, ...)` patches are invisible to the executed code — the code uses the module's original binding, not the patched one.

**Root Cause:** Plan gap — the plan did not account for `runpy` namespace isolation behavior.

**Impact:** Positive. The helper is more reliable, clearly documented, and reused across tests 7.6, 7.7, and 7.8. The behavior verified is identical to what the plan intended.

**Justified:** Yes

### Divergence #2: No live-cache validation

**Classification:** ENVIRONMENTAL

**Planned:** Plan item 4 (optional) — validate with populated parquet cache by running `uv run train.py` and checking real output.

**Actual:** Skipped. The plan explicitly marked this as optional, requiring a populated cache that is not available in the agent environment.

**Reason:** No parquet cache available; the plan flagged this as optional.

**Root Cause:** Environmental — agent does not have live market data cache.

**Impact:** Neutral. All behavior is covered by unit tests with mocked data.

**Justified:** Yes

---

## Test Results

**Tests Added:**
1. `test_run_backtest_returns_r7_keys` — presence and type of R7 keys in return dict
2. `test_max_drawdown_is_non_negative` — invariant: drawdown cannot be negative
3. `test_calmar_zero_when_no_drawdown` — division-by-zero guard for calmar
4. `test_pnl_consistency_equals_min_monthly_pnl` — min monthly PnL semantic correctness
5. `test_print_results_includes_r7_lines` — output format for calmar and pnl_consistency
6. `test_main_runs_walk_forward_windows_folds` — `__main__` calls run_backtest exactly N×2+1 times
7. `test_main_outputs_min_test_pnl_line` — `__main__` stdout contains parseable `min_test_pnl:` line
8. `test_silent_pnl_hidden_by_default` — silent holdout prints `HIDDEN` when WRITE_FINAL_OUTPUTS=False
9. `test_walk_forward_fold_dates_are_distinct` — fold boundaries are non-overlapping, 10 bdays apart

**Test Execution:**
```
pytest tests/test_optimization.py -v
12 passed, 1 skipped
```

**Pass Rate:** 12/13 (92.3%) — 1 skip is pre-existing (requires live cache data), unrelated to V3-B

---

## What was tested

- `run_backtest()` returns all three R7 keys (`max_drawdown`, `calmar`, `pnl_consistency`) as floats
- `max_drawdown` is always non-negative, even when positions are entered on every day of a rising dataset
- `calmar` is 0.0 when `max_drawdown` is zero, verifying the division-by-zero guard in the early-exit path
- `pnl_consistency` equals the minimum monthly P&L across the backtest window (verified via a synthetic 2-month dataset where one month is profitable and one is flat)
- `print_results()` emits `calmar:` and `pnl_consistency:` lines with correct numeric formatting (4 and 2 decimal places respectively)
- `__main__` calls `run_backtest()` exactly `WALK_FORWARD_WINDOWS * 2 + 1` times (N train folds + N test folds + 1 silent holdout)
- `__main__` stdout contains a `min_test_pnl:` line parseable by grep, using the correct keep/discard metric
- When `WRITE_FINAL_OUTPUTS = False`, the silent holdout output line is `silent_pnl: HIDDEN` with no `holdout_` prefix leaked
- Walk-forward fold test windows are mathematically distinct (no shared dates) and step by exactly 10 business days each

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `python -c "import ast; ast.parse(open('train.py').read())"` | Pass | Syntax valid |
| 1 | `python -c "import ast; ast.parse(open('tests/test_optimization.py').read())"` | Pass | Syntax valid |
| 2 | `python -c "import train"` | Pass | Module importable |
| 3 | `grep WALK_FORWARD_WINDOWS train.py` | Pass | Constant present in mutable section |
| 3 | `grep SILENT_END train.py` | Pass | Constant present in mutable section |
| 4 | GOLDEN_HASH verification | Pass | `796595717636d21cc47c897589e08fa8cf3c8d9c34cc8dedfc80957c7f64fe3d` |
| 5 | `python -m pytest tests/test_optimization.py -v` | Pass | 12 passed, 1 skipped |

---

## Challenges & Resolutions

**Challenge 1: runpy namespace isolation**
- **Issue:** Tests 7.6 and 7.7 need to exercise `__main__` with mocked `run_backtest` and `load_all_ticker_data`. `runpy.run_path` creates a fresh namespace, so `mock.patch.object` patches on the `train` module object are not visible to the freshly-imported names.
- **Root Cause:** `runpy.run_path` re-executes the file from scratch with a new `__builtins__` and no reference to the existing `train` module.
- **Resolution:** Introduced `_exec_main_block(extra_ns)` helper that slices the `__main__` block from the already-imported source, overlays mocks via `ns.update(extra_ns)`, and executes in the train module's existing namespace.
- **Time Lost:** ~10 minutes
- **Prevention:** Document in plan: "to test `__main__` with mocks, use `exec` in `vars(train)` namespace, not `runpy.run_path`."

---

## Files Modified

**Core implementation (2 files):**
- `train.py` — mutable section: +2 constants; immutable section: run_backtest R7 tracking, print_results R7 lines, full __main__ rewrite (+~140/-~55)
- `program.md` — five sub-sections updated: step 4b, output format, loop steps 5/8/9, note block, results.tsv schema, final test run section, goal section (+~50/-~70)

**Tests (1 file):**
- `tests/test_optimization.py` — GOLDEN_HASH updated, 9 new tests + `_exec_main_block` helper (+184/-1)

**Total:** ~376 insertions, ~126 deletions

---

## Success Criteria Met

- [x] `WALK_FORWARD_WINDOWS = 3` present in mutable section
- [x] `SILENT_END = "2026-02-20"` present in mutable section
- [x] `run_backtest()` returns `max_drawdown`, `calmar`, `pnl_consistency`
- [x] `print_results()` emits `calmar:` and `pnl_consistency:` lines
- [x] `__main__` runs N×2+1 `run_backtest()` calls (3 folds × 2 + 1 holdout = 7)
- [x] `min_test_pnl:` line present in `__main__` output
- [x] `silent_pnl: HIDDEN` when `WRITE_FINAL_OUTPUTS = False`
- [x] GOLDEN_HASH updated and verified
- [x] 9 new V3-B tests added and passing
- [x] `program.md` updated for new output format, keep/discard criterion, and results.tsv schema
- [ ] Live-cache validation (optional — skipped, requires populated parquet cache)

---

## Recommendations for Future

**Plan Improvements:**
- Specify the `__main__`-testing mechanism explicitly: note that `runpy.run_path` is incompatible with `mock.patch.object` due to namespace isolation; recommend the `_exec_main_block` / `exec(compile(...), vars(module))` pattern.
- For any task that modifies the immutable zone, include the GOLDEN_HASH recompute command inline in that task (not as a separate wave-3 task) so it can't be forgotten if tasks are partially executed.

**Process Improvements:**
- The `_exec_main_block` helper pattern should be documented as a project-level test utility for any future `__main__` block testing needs.

**CLAUDE.md Updates:**
- Add note under Testing: "To test a `__main__` block with mocks in a module-level namespace, use `exec(compile(source[main_idx:], file, 'exec'), dict(vars(module)))` with mock values overlaid, rather than `runpy.run_path`. The latter creates an isolated namespace where `mock.patch.object` patches are not visible."

---

## Conclusion

**Overall Assessment:** V3-B was implemented cleanly and completely. All 7 tasks across 3 waves completed with one non-breaking divergence (test execution mechanism) and one planned skip (live-cache validation). The walk-forward loop, R7 diagnostics, silent holdout, and program.md updates are all in place and covered by automated tests. The `_exec_main_block` helper resolves a real test infrastructure limitation and is more robust than the plan's suggested approach.

**Alignment Score:** 9/10 — the single divergence (runpy → exec helper) improves on the plan spec; no functional behavior was changed or omitted.

**Ready for Production:** Yes — all acceptance criteria met. Next step is a real optimization run using the new walk-forward framework.
