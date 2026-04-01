# Code Review: databento-historical-pipeline

**Date:** 2026-04-01
**Plan:** `.agents/plans/databento-historical-pipeline.md`
**Reviewer:** Claude Sonnet 4.6

---

## Stats

- Files Modified: 4 (`data/sources.py`, `prepare_futures.py`, `tests/test_data_sources.py`, `pyproject.toml`)
- Files Added: 2 (`.gitignore` lines, `data/historical/.gitkeep`)
- Files Deleted: 0
- New lines: ~393 (code) + 849 (uv.lock)
- Deleted lines: ~32 (prepare_futures.py rewrites)

**Test suite:** 357 passed, 14 deselected (integration) — no regressions.

---

## Issues Found

---

```
severity: medium
file: data/sources.py
line: 195-198
issue: print() to stderr in production code path — violates codebase logging standards
detail: The CLAUDE.md global standards explicitly state "Production code is silent: No
  print/stdout logging in production paths." The except block in DatabentSource.fetch()
  uses print(..., file=sys.stderr), and requires importing sys inside the except block
  every call. The same pattern exists in IBGatewaySource.fetch() (line 376) but that is
  pre-existing. This is new code being added now and should conform to the standard.
suggestion: Capture the error in the return value (already returns None) and let the
  caller decide how to surface it. If logging is needed, use Python's logging module
  (logging.getLogger(__name__).warning(...)) rather than print. The import sys can also
  be moved to the module level if kept.
```

---

```
severity: medium
file: prepare_futures.py
line: 133
issue: Misleading error message refers to IB-Gateway when Databento is now the primary source
detail: Line 133 prints "Some tickers failed. Check IB-Gateway connection." but after
  this refactor, Level 3 failures are Databento download failures (missing API key,
  network error, bad credentials), not IB-Gateway issues. The IB cache path (Level 2)
  is a passive file-existence check — it never initiates an IB connection. A user
  seeing this message would be directed to debug the wrong system.
suggestion: Change to a message that reflects the actual failure modes, e.g.:
  "Some tickers failed. Check DATABENTO_API_KEY and network connectivity."
```

---

```
severity: medium
file: prepare_futures.py
line: 96
issue: KeyError on unknown ticker — DATABENTO_SYMBOLS lookup is unguarded
detail: Line 96: `db_ticker = DATABENTO_SYMBOLS[ticker]` will raise KeyError if ticker
  is not in DATABENTO_SYMBOLS. TICKERS is currently ["MNQ", "MES"] and DATABENTO_SYMBOLS
  covers exactly those two, so this is safe today. However, process_ticker() is a
  public function documented as taking any ticker string, and there is no guard. If
  a caller passes an unlisted ticker (e.g. "ES"), the function raises an uncaught
  KeyError instead of returning False — violating the documented contract ("Returns
  True on success").
suggestion: Add a guard before the lookup:
  if ticker not in DATABENTO_SYMBOLS:
      print(f"  {ticker}: no Databento symbol mapping defined", file=sys.stderr)
      return False
  db_ticker = DATABENTO_SYMBOLS[ticker]
```

---

```
severity: low
file: data/sources.py
line: 154
issue: Typo in class name: DatabentSource should be DatabentoSource
detail: The plan, all tests, the manifest, and the imports all consistently use
  "DatabentSource" (missing the trailing 'o'). This is a typo carried through from
  the plan spec. It appears everywhere uniformly so it is not a functional bug, but
  it is a permanent naming inconsistency that will cause confusion for anyone reading
  the code or searching for "Databento" in class names.
suggestion: Rename to DatabentoSource throughout (data/sources.py, prepare_futures.py,
  tests/test_data_sources.py, __init__.py if applicable). This is a one-time rename
  with no runtime impact.
```

---

```
severity: low
file: data/sources.py
line: 162-163
issue: os imported inside __init__ — should be a module-level import
detail: `import os` is executed inside __init__() on every instantiation. os is a
  stdlib module, already present in sys.modules, so the overhead is negligible, but
  it is non-idiomatic and inconsistent with the rest of the file which uses module-level
  imports. The lazy import rationale applies to the optional third-party library
  `databento`, not to stdlib modules like os.
suggestion: Move `import os` to the top of the file alongside the other stdlib imports.
  The lazy `import databento as db` inside fetch() is correct and should stay there.
```

---

```
severity: low
file: tests/test_data_sources.py
line: 556-560
issue: Redundant monkeypatch.setenv in test_raises_value_error_on_unsupported_interval
detail: The src fixture (line 471-475) already sets DATABENTO_API_KEY and returns an
  initialized DatabentSource. The test then calls monkeypatch.setenv("DATABENTO_API_KEY",
  "test-key-123") again — this is a no-op since the key is already set. It adds noise
  and misleadingly implies the env var is being reset to a different state.
suggestion: Remove the redundant monkeypatch.setenv call from
  test_raises_value_error_on_unsupported_interval — the src fixture handles it.
```

---

```
severity: low
file: tests/test_data_sources.py
line: 477-493
issue: sys.modules patch for databento may be order-sensitive with other tests
detail: monkeypatch.setitem(__import__("sys").modules, "databento", mock_db) replaces
  the "databento" entry in the global module registry. If databento is legitimately
  imported somewhere else in the process (e.g., if a future test triggers a real import),
  monkeypatch's teardown correctly restores the original. However, this approach patches
  the live sys.modules rather than targeting the import inside the function under test.
  A more robust pattern is mock.patch("data.sources.db") — but since the import is
  done with `import databento as db` inside fetch() on each call (no module-level alias),
  the sys.modules approach is the only workable method here.
  This is acceptable given the lazy import design, but worth documenting in a comment
  so future maintainers understand why sys.modules is patched directly.
suggestion: Add a short comment above each monkeypatch.setitem block:
  # patch sys.modules because databento is lazy-imported inside fetch()
  This is not a bug — just a maintainability note.
```

---

```
severity: low
file: prepare_futures.py
line: 127-129
issue: __main__ block prints Databento-unaware progress message
detail: Line 128 prints "Downloading futures data to {CACHE_DIR}" but CACHE_DIR is the
  IB cache path (~/.cache/autoresearch/futures_data). The actual Databento data is
  saved to HISTORICAL_DATA_DIR (data/historical/), not CACHE_DIR. CACHE_DIR is only
  used for the Level 2 IB cache check and the manifest file. The printed path will
  confuse users who run the script and check the wrong directory for their data.
suggestion: Change the print to:
  print(f"Downloading futures data to {HISTORICAL_DATA_DIR}")
  And if the manifest path is useful context, add:
  print(f"Manifest will be written to {Path(CACHE_DIR) / 'futures_manifest.json'}")
```

---

## Summary

No critical or high-severity bugs. The implementation is functionally correct — all 357 non-integration unit tests pass with zero regressions. The issues are:

- 1 medium: production `print()` to stderr violates the project's silence-in-production rule (new code, not pre-existing)
- 1 medium: misleading error message at script exit pointing to IB-Gateway when Databento is the failure point
- 1 medium: unguarded `DATABENTO_SYMBOLS[ticker]` dict lookup can raise KeyError for unlisted tickers
- 1 low: class name typo (`DatabentSource` instead of `DatabentoSource`) propagated from plan spec
- 3 low: minor code quality issues (os import location, redundant monkeypatch call, misleading print path)

The `DatabentSource` logic (resample, column rename, timezone conversion, lazy import, error containment) is correct. The 3-level lookup priority in `process_ticker()` is correctly implemented. The `.gitignore` entry and `data/historical/.gitkeep` are correct.
