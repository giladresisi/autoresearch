# Code Review: V3-E Configurable Walk-Forward Window Size and Rolling Training Windows

**Date:** 2026-03-22
**Plan:** `.agents/plans/v3-e-configurable-walk-forward.md`
**Reviewer:** Claude Code (code-review skill)

---

## Stats

- Files Modified: 3
- Files Added: 0
- Files Deleted: 0
- New lines: +44
- Deleted lines: -6

---

## Pre-existing Failures

The following 3 test failures exist in the baseline (before V3-E) and are **not introduced by this changeset**. Confirmed by `git stash` / `git stash pop` baseline comparison:

- `tests/test_program_md.py::test_results_tsv_header`
- `tests/test_program_md.py::test_grep_train_pnl_command`
- `tests/test_program_md.py::test_grep_total_trades_command`

Root cause: these tests assert strings that appear to have been removed from `program.md` in an earlier commit (pre-V3-E).

---

## Issues Found

No new bugs or correctness issues were introduced by the V3-E changeset. The analysis below covers the areas checked:

### Logic Correctness

**Walk-forward fold window calculation (train.py lines 725–727):**

The two `10` replacements are correct:
- `_BDay(_steps_back * FOLD_TEST_DAYS)` — step between fold test windows; each fold steps back one additional `FOLD_TEST_DAYS`-width unit from `TRAIN_END`. Correct.
- `_BDay(FOLD_TEST_DAYS)` — width of each test window. Correct.
- `_fold_train_end_ts = _fold_test_start_ts` — training ends where test begins. Unchanged and correct.

**Rolling window branch (train.py lines 734–740):**

- `_fold_train_start_ts = _fold_train_end_ts - _BDay(FOLD_TRAIN_DAYS)` — computes rolling window start by subtracting `FOLD_TRAIN_DAYS` business days from the fold's training end (= fold test start). Correct direction.
- `str(max(_fold_train_start_ts.date(), date.fromisoformat(BACKTEST_START)))` — clamps to `BACKTEST_START` so early folds (where `FOLD_TRAIN_DAYS` lookback would precede available data) gracefully fall back to the start of available history. This is the correct guard per plan Scenario C.
- `str(...)` wrapping: `max(...)` returns a `datetime.date` object; `str()` produces ISO format (`YYYY-MM-DD`), which matches `run_backtest`'s `start: str | None` parameter type (parsed via `date.fromisoformat`). Correct.
- `else` branch: `_fold_train_start = BACKTEST_START` (the module-level string constant). Passed directly to `run_backtest`. Correct; preserves V3-D behavior exactly.

**`FOLD_TRAIN_DAYS = 0` default:** The `else` branch is taken by default, which means no behavioral change for existing sessions. Backward-compatible as specified.

**`FOLD_TEST_DAYS = 20` default:** Old sessions that previously ran with hardcoded `10` will now see wider folds. This is intentional and documented — the plan explicitly states setting `FOLD_TEST_DAYS = 10` restores V3-D behavior.

### Type Safety

- `_fold_train_start` in both branches is `str`. `run_backtest` takes `start: str | None`. No type mismatch.
- `date.fromisoformat(BACKTEST_START)` — `BACKTEST_START` is a module-level `str` constant defined as `"2025-12-20"`. Valid ISO date. No runtime error.

### Import Dependencies

- `date` from `datetime` is already imported at line 7. Used correctly.
- `_BDay` is already imported inside the loop at line 714. Used correctly in the new branch.
- No new imports required or added. Correct per plan.

### Constant Placement

- `FOLD_TEST_DAYS` at line 33, `FOLD_TRAIN_DAYS` at line 41 — both above the `# ── DO NOT EDIT BELOW THIS LINE` boundary at line 309. Correctly in the mutable section.
- Both constants follow the multi-line block comment style of surrounding constants (`WALK_FORWARD_WINDOWS`, `SILENT_END`). Standards-compliant.

### GOLDEN_HASH

- Hash test `test_harness_below_do_not_edit_is_unchanged` passes. The new hash `8e52c979a05340df9bef49dbfda0c7086621e6dd2ac2e7c3a9bf12772c04e0a7` correctly reflects the V3-E immutable zone content.
- Note: the recompute command in the plan/test file comment (`open('train.py').read()` without explicit encoding) would return the empty-string SHA-256 on Windows due to codec mismatch with the box-drawing characters in the marker. This is a cosmetic issue in the comment only — the test itself always uses `read_text(encoding="utf-8")` and passes correctly. The plan's recompute command should ideally include `open('train.py', encoding='utf-8').read()` to be portable, but this does not affect correctness.

### program.md Update

- `WALK_FORWARD_WINDOWS`, `BACKTEST_START`, `BACKTEST_END`, `min_test_pnl`, `run_backtest`, `uv run train.py > run.log 2>&1`, `git reset --hard HEAD~1` — all still present after step 4b rewrite (verified by the 18 passing `test_program_md.py` structural tests).
- New subsection accurately describes `FOLD_TEST_DAYS`, `FOLD_TRAIN_DAYS`, and updated `WALK_FORWARD_WINDOWS` recommendations. Documentation is correct relative to the implementation.

---

## Coverage Gaps (Accepted, Pre-documented)

The following gaps are **intentional and documented** in the plan's Coverage Review section:

| Gap | Reason | Risk |
|-----|--------|------|
| `FOLD_TRAIN_DAYS > 0` rolling-window branch execution | Requires live parquet cache | Low — 3-line conditional, hash-guarded text |
| `max(...)` clamping against `BACKTEST_START` | Same reason | Low — standard date comparison |

Both gaps are accepted per the plan. Automating them would require ~85 × 19-month parquet fixtures as test data, which is out of scope.

---

## Minor Observation (Non-blocking)

**GOLDEN_HASH recompute command portability (plan + test comment):**

```
severity: low
file: tests/test_optimization.py
line: 116–117
issue: Hash recompute command in comment omits explicit encoding parameter
detail: The comment at lines 116–117 shows:
  python -c "import hashlib; s=open('train.py').read(); ..."
  On Windows, open() defaults to the system locale codec (cp1252 or similar), which fails to
  decode the UTF-8 box-drawing characters in the DO NOT EDIT marker line. This causes
  s.partition(m) to return ('', '', '') and the command prints the SHA-256 of an empty string
  rather than the correct hash. The test itself is unaffected (it uses read_text(encoding="utf-8")),
  but the comment as written would mislead a developer trying to manually recompute the hash.
suggestion: Change the comment to:
  python -c "import hashlib; s=open('train.py', encoding='utf-8').read(); m='# ── DO NOT EDIT BELOW THIS LINE'; print(hashlib.sha256(s.partition(m)[2].encode()).hexdigest())"
  This is a comment-only change; the test logic itself is correct and passing.
```

---

## Conclusion

Code review passed. The V3-E implementation is correct, backward-compatible, and standards-compliant. All new code paths are logically sound. The one minor issue (hash recompute comment portability on Windows) is non-blocking and does not affect test correctness or runtime behavior.

**Automated test status:** 105 passing, 3 pre-existing failures (unrelated to this changeset), 1 pre-existing skip. Zero new failures.
