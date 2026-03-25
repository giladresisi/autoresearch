# Code Review: Phase 1 — Pre-Market Signal CLI

**Date:** 2026-03-25
**Reviewer:** AI Code Review

## Stats

- Files Modified: 2 (train.py, tests/test_screener.py)
- Files Added: 8 (screener_prepare.py, screener.py, analyze_gaps.py, position_monitor.py, portfolio.json, tests/test_screener_prepare.py, tests/test_screener_script.py, tests/test_position_monitor.py, tests/test_analyze_gaps.py)
- Files Deleted: 0
- New lines: ~750
- Deleted lines: 2

## Test Results

Full test suite: **270 passed, 1 skipped** (pre-existing skip). No regressions introduced.
New tests for this feature: **52 passed**.

---

## Issues Found

---

```
severity: medium
file: screener_prepare.py
line: 155
issue: Tautological condition — mtime check is always True, masking failed downloads
detail: The condition `os.path.getmtime(path) > os.path.getmtime(path) - 1` is equivalent to
  `x > x - 1`, which is always True for any finite float. This means the entire OR expression
  short-circuits to True regardless of `existed_before`. Consequence: if `download_and_cache()`
  fails silently but a stale parquet from a prior run already exists at `path`, the code reads
  that stale file, finds it non-empty, prints "cached (N rows)", increments `cached`, and
  never reaches the FAIL branch. A previously-failed ticker is silently reported as cached.
  The original intent was to detect whether the file's mtime changed after the download.
suggestion: Capture mtime before the download call and compare after:
  ```python
  path = os.path.join(SCREENER_CACHE_DIR, f"{ticker}.parquet")
  mtime_before = os.path.getmtime(path) if os.path.exists(path) else 0.0
  download_and_cache(ticker, history_start)
  if os.path.exists(path) and os.path.getmtime(path) > mtime_before:
      ...
  ```
  Or simplify: just read and verify the parquet after calling download_and_cache() —
  `is_ticker_current()` already has the right freshness check.
```

---

```
severity: medium
file: tests/test_screener_script.py
line: 87-88
issue: Assertion is trivially True — test_screener_falls_back_to_close_on_nan_last_price does not verify the fallback value
detail: The assertion is:
  `assert prices_used[0] == prev_close or prices_used[0] is not None`
  The second clause (`is not None`) is always True when `prices_used` is non-empty (the list
  is populated with `current_price` which is always a float at that point). The test passes
  trivially for ANY non-None price, including a completely wrong value. The fallback-to-close
  behavior is not actually verified.
suggestion: Replace with a strict equality check:
  `assert prices_used[0] == pytest.approx(prev_close)`
```

---

```
severity: low
file: tests/test_screener.py
line: 280-285
issue: test_screen_day_current_price_overrides_df_value does not test override semantics
detail: The test passes `current_price=115.0`, which is the same value that `make_pivot_signal_df`
  already sets in `df['price_1030am']`. So both the override and the default paths receive
  identical inputs. The assertion `(result_with is None) == (result_without is None)` only
  confirms they agree — it passes even if `current_price` is completely ignored. A genuine
  override test needs a different price (e.g., a price that would NOT signal) to confirm the
  injected value is actually used.
suggestion: Use a price below the breakout threshold (e.g., `current_price=100.0`) and assert
  the result is None, while the no-override path returns a signal dict. This proves the override
  is actually applied.
```

---

```
severity: low
file: pyproject.toml
line: n/a
issue: Undeclared direct dependencies: beautifulsoup4 and requests
detail: `screener_prepare.fetch_screener_universe()` imports `requests` and `bs4` at call time
  (deferred import inside the function body). Neither package is declared in `pyproject.toml`
  dependencies. They are currently available as transitive dependencies of `yfinance==1.2.0`,
  but this is an implementation detail of the current yfinance version. A yfinance upgrade or
  a fresh environment install that resolves differently could break `fetch_screener_universe()`
  at runtime with a silent ImportError caught by the outer `except Exception`.
suggestion: Add explicit dependencies to pyproject.toml:
  ```toml
  "beautifulsoup4>=4.12",
  "requests>=2.31",
  ```
```

---

```
severity: low
file: screener.py
line: 100
issue: Off-by-one in minimum-rows guard — 1 valid ticker unnecessarily skipped
detail: The guard is `if len(df) < 102: continue`, but `df` here is the parquet BEFORE the
  synthetic today row is appended. After `pd.concat([df, synthetic_row])`, `df_extended` has
  `len(df) + 1` rows. `screen_day()` requires `len(df) >= 102` after its own `df.loc[:today]`
  slice. So a parquet with exactly 101 rows would yield a 102-row `df_extended` that would
  pass `screen_day`, but the guard skips it. Threshold should be `len(df) < 101`.
  In practice this never fires (90-day history ≈ 63 trading days), so it is not causing
  incorrect results.
suggestion: Change `if len(df) < 102` to `if len(df) < 101` to align with what screen_day
  actually needs after the synthetic row is appended.
```

---

## No Issues Found In

- **train.py changes** (lines 224, 241, 337-338): `current_price` parameter, `price_1030am` assignment, and `rsi14`/`res_atr` additions to the return dict are all correct. Backward compatibility is preserved — the default `None` means all existing callers are unaffected.
- **analyze_gaps.py**: Logic correct. `best_threshold` is updated (not short-circuited) on each passing threshold, so the most negative threshold with < 10% winner exclusion is returned as intended.
- **position_monitor.py**: `_infer_reason` uses the same `current_price` value that was written to the synthetic row's `price_1030am`, so the ATR-multiple comparison is consistent with what `manage_position()` evaluated.
- **portfolio.json**: Well-formed template. `_comment` field as instruction is the correct pattern for this use case.
- **Index type consistency**: `resample_to_daily()` (from `prepare.py`) produces a `datetime.date` object index. The synthetic row also uses `datetime.date`. So `pd.concat([df, synthetic_row])` produces a uniform object-dtype date index — no Timestamp/date mixed-type issue.
- **Security**: No hardcoded credentials, API keys, or sensitive data in any file.
- **CLAUDE.md production-silent rule**: All `print()` calls are inside `if __name__ == "__main__"` blocks or the top-level script body of CLI scripts. Library functions (`compute_gaps`, `print_analysis`, `run_screener`, `run_monitor`) that emit warnings/output do so as intended output for CLI use, not import-path side effects.
