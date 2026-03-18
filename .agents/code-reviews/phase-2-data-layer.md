# Code Review — Phase 2: Data Layer (`prepare.py`)

**Date:** 2026-03-18
**Files reviewed:** `prepare.py`, `tests/test_prepare.py`, `pyproject.toml`

---

## Stats

- Files Modified: 2 (`prepare.py`, `pyproject.toml`)
- Files Added: 1 (`tests/test_prepare.py`)
- New lines: 125
- Deleted lines: 369

---

## Issues Found

---

```
severity: high
file: tests/test_prepare.py
line: 120-122
issue: test_index_is_date_objects gives false confidence — passes for pd.Timestamp values
detail: The assertion `isinstance(d, datetime.date)` returns True for pd.Timestamp objects
        because pd.Timestamp subclasses datetime.datetime which subclasses datetime.date.
        Confirmed: isinstance(pd.Timestamp("2025-01-01"), datetime.date) == True.
        The test would pass even if resample_to_daily returned a Timestamp index, which
        would violate the data contract with train.py (df.loc[:today] slicing requires
        pure date objects).
suggestion: Use `type(d) is datetime.date` instead of `isinstance(d, datetime.date)` to
            reject pd.Timestamp and datetime.datetime values:
            assert all(type(d) is datetime.date for d in daily.index)
```

---

```
severity: medium
file: prepare.py
line: 14
issue: `from datetime import datetime` is imported but never used
detail: The module-level `from datetime import datetime` is never referenced anywhere in
        the file. The actual datetime usage in resample_to_daily (line 59) uses
        `import datetime as _dt` inside the function body — a workaround that exists
        precisely because of this conflicting top-level import.
suggestion: Remove line 14 (`from datetime import datetime`). Then move line 59
            (`import datetime as _dt`) to module level as `import datetime`, and update
            line 60 to `df.index.time == datetime.time(10, 0)`.
```

---

```
severity: medium
file: prepare.py
line: 59
issue: `import datetime as _dt` inside function body — symptom of conflicting top-level import
detail: Importing inside a function is unconventional and only exists here to avoid the
        name collision with `from datetime import datetime` at line 14. Readers unfamiliar
        with the codebase will find a module-level import for `datetime` that is unused,
        and a function-level import with an underscore alias that is used. This is the
        downstream consequence of the unused import at line 14.
suggestion: Fix the root cause (remove line 14); move this import to module level as
            `import datetime`. No alias needed once the conflict is gone.
```

---

```
severity: medium
file: prepare.py
line: 96-110
issue: process_ticker does not create CACHE_DIR before writing — fails when called as library function
detail: `df_daily.to_parquet(path)` at line 108 raises OSError if CACHE_DIR does not exist.
        CACHE_DIR is only created in the __main__ block (line 117), so any caller that
        invokes process_ticker directly (e.g. scripts, tests, future phases) will get:
        OSError: Cannot save file into a non-existent directory.
        Confirmed with: pd.DataFrame(...).to_parquet("nonexistent_dir/file.parquet")
        → OSError: Cannot save file into a non-existent directory.
suggestion: Add `os.makedirs(CACHE_DIR, exist_ok=True)` inside process_ticker, immediately
            before `df_daily.to_parquet(path)` (line 108). The __main__ block call can
            remain as-is (makedirs with exist_ok=True is idempotent).
```

---

```
severity: low
file: tests/test_prepare.py
line: 2, 4
issue: `import io` and `import sys` are unused
detail: Neither `io` nor `sys` are referenced anywhere in the test file body.
        `capsys` (used in warning tests) is a pytest fixture, not the sys module.
suggestion: Remove both import lines.
```

---

## Summary

| Severity | Count |
|----------|-------|
| High     | 1     |
| Medium   | 3     |
| Low      | 1     |
| **Total**| **5** |

The high severity issue (`test_index_is_date_objects`) is a test logic bug that gives false confidence in the data contract — the core correctness guarantee that `train.py` relies on. It should be fixed before this code ships. The medium issues are all in `prepare.py`: one unused import that causes a function-level workaround, and one missing `makedirs` that will surface as an OSError in any non-`__main__` caller.
