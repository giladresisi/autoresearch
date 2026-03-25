# Code Review: Parallel Download Thread Pool

**Plan**: `.agents/plans/parallel-download-threadpool.md`
**Date**: 2026-03-25
**Reviewer**: Claude Code (automated)

---

## Stats

- Files Modified: 4
- Files Added: 0
- Files Deleted: 0
- New lines: +219
- Deleted lines: -26

---

## Test Results

All 38 tests pass (9 new + 29 pre-existing). No regressions.

```
38 passed in 6.50s
```

---

## Issues Found

---

```
severity: low
file: screener_prepare.py
line: 17
issue: `import threading` is unused
detail: The plan specified adding `import threading` but the implementation never uses it —
        `_process_one` contains no locks, events, or threading primitives. The module relies
        entirely on `concurrent.futures`, which does not require an explicit `threading` import.
suggestion: Remove `import threading` from the import block. It is dead code and will trip
            a linter (e.g. flake8 F401).
```

---

```
severity: low
file: tests/test_prepare.py
line: 313-318
issue: test_parallel_loop_processes_all_tickers monkeypatches process_ticker but then calls prepare.process_ticker (the original) via executor.map
detail: The test sets `monkeypatch.setattr(prepare, "process_ticker", fake_process)` and
        appends to `called` via `fake_process`, but the executor.map call on line 318 passes
        `prepare.process_ticker` which resolves to the patched attribute — so the test does
        work correctly as written. However, the intent is obscured: the test appears to call
        `fake_process` through `prepare.process_ticker`, but actually it IS calling `fake_process`
        because monkeypatch replaced the attribute. If a future refactor reads the reference
        before monkeypatching (e.g. via a closure or import alias), the test would silently
        stop exercising the mock. The test also does not use `prepare.MAX_WORKERS` for
        `executor.map` — it uses the module attribute for the pool size but passes the
        (already-patched) `prepare.process_ticker` reference to the map, which works, but
        the relationship between the mock and the assertion is subtle.
suggestion: Make the intent explicit. Either:
            (a) call `fake_process` directly in executor.map (simpler — the test is really
                just testing that ThreadPoolExecutor.map runs all items), or
            (b) add a comment explaining that `prepare.process_ticker` is the patched fake
                at this point due to monkeypatch.
```

---

```
severity: low
file: screener_prepare.py
line: 209
issue: Progress counter `done` reflects completion order, not input order — printed index may be confusing
detail: Because `as_completed` yields futures in wall-clock completion order, the `[done/total]`
        counter is accurate (every ticker gets a unique sequential number), but the numbers
        are not aligned to the original universe list. This is the intended and documented
        behaviour (plan notes 2.1 "UX for a long-running script"), so it is not a bug.
        The only mild risk is that a user comparing logs across two runs will see tickers
        appear at different line numbers, making diff-based debugging slightly harder.
suggestion: Acceptable as-is. If reproducible ordering becomes important later, consider
            printing the original 1-based index alongside: store `{future: (i, t)}` in the
            futures dict and include `i` in the print statement.
```

---

```
severity: low
file: tests/test_screener_prepare.py
line: 244-274
issue: test_parallel_all_tickers_processed does not assert on status values — only that all keys are present
detail: The test confirms that all 3 tickers appear in `statuses` but does not verify that
        they were actually cached (i.e. status contains "cached"). A bug that returns "FAIL"
        for all tickers would still pass this test. The primary coverage for status values is
        in the three `test_process_one_*` tests, so this is a minor gap rather than a missing
        critical path.
suggestion: Add `assert all("cached" in s for s in statuses.values())` to confirm that the
            parallel loop produces the expected outcomes end-to-end, not just that all futures
            resolve.
```

---

## Summary

The implementation is clean and correct. All 9 new tests pass. The change is a pure loop
replacement with no behavioural change to download logic or caching logic, as scoped.

The only actionable fix is the unused `import threading` (line 17 of `screener_prepare.py`),
which is dead code introduced by following the plan spec literally. The three remaining items
are low-severity observations about test clarity and a cosmetic UX note, none of which affect
correctness.

**Recommended action before merge**: remove `import threading` from `screener_prepare.py`.
