# Code Review: V4-A Strategy Quality

**Date**: 2026-03-24
**Branch**: master
**Plan**: `.agents/plans/v4-a-strategy-quality.md`

## Stats

- Files Modified: 5 (train.py, prepare.py, program.md, tests/test_screener.py, tests/test_backtester.py)
- Files Added: 1 (tests/test_v4_a.py)
- Files Deleted: 0
- New lines: ~114
- Deleted lines: ~16

## Test Run

All 64 tests pass (`pytest tests/test_v4_a.py tests/test_screener.py tests/test_backtester.py`).

---

## Issues Found

---

```
severity: high
file: tests/test_v4_a.py
line: 130-138
issue: test_manage_position_no_force_exit_when_profitable is a false negative — R10 fires but the test passes due to a weak assertion
detail: _make_long_position(n=50, entry_price=80.0, stop_price=75.0) creates a df via
  make_position_df(n=50, price=entry_price=80.0), so price_10am is 80.0 throughout.
  unrealised_pnl = (80.0 - 80.0) * 10 = 0.0, which is < 0.3 * RISK_PER_TRADE (15.0).
  R10 therefore fires and returns max(75.0, 80.0) = 80.0.
  The test comment claims "R10 should not fire" and entry=80, price=100, but those values
  are wrong — the df price equals entry_price. The assertion `assert result is not None`
  is always true (manage_position never returns None), so the test passes even though the
  scenario it describes is never exercised.
  The profitable case (price well above entry) is left completely untested.
suggestion: Set price > entry_price in the df. Use make_position_df(price=100.0) with
  entry_price=80.0 and stop_price=75.0 independently, so unrealised_pnl =
  (100.0 - 80.0) * 10 = 200.0 >= 15.0. Then assert result < price_10am (R10 did not
  set stop to current price).
```

---

```
severity: medium
file: prepare.py
line: 216-218
issue: stale cache silently disables R8 earnings filter for all pre-existing parquet files
detail: process_ticker() returns early on line 217 if the ticker's parquet already exists.
  Files cached before this changeset will not have the next_earnings_date column.
  screen_day() gracefully skips the R8 check when the column is absent (by design), so
  there is no runtime error — but R8 is silently inactive for the entire existing cache.
  Users who already ran prepare.py will see no benefit from R8 until they wipe the cache.
  There is no warning, no migration, and no documentation of this requirement.
suggestion: Add a column-presence check in process_ticker() (or in a separate migration
  helper) that reads the existing parquet, detects missing next_earnings_date, and
  re-fetches earnings data and re-saves. Alternatively, add a clear note in PROGRESS.md
  or program.md that prepare.py must be re-run (with cache deleted) after this change.
  At minimum, print a warning in load_ticker_data() when next_earnings_date is absent.
```

---

```
severity: low
file: prepare.py
line: 140
issue: bare except swallows all exceptions in _add_earnings_dates, masking API breakage
detail: The try/except block catches all exceptions silently and returns a column of NaT.
  If yfinance changes its earnings_dates API (e.g. renames the property, changes index
  timezone handling, or requires authentication), the function will silently produce NaT
  for every ticker, disabling R8 across the entire universe with no user-visible signal.
  The one-time prepare.py run is the only opportunity to diagnose this failure.
suggestion: Log a one-line warning to stdout (since prepare.py is an operator script
  where print() is appropriate) when the exception is not AttributeError/KeyError:
    except (AttributeError, KeyError):
        ...  # earnings_dates not available for this ticker
    except Exception as e:
        print(f"  WARNING: earnings_dates fetch failed for {ticker}: {e}")
        ...
  This preserves the best-effort design while making API failures visible.
```

---

```
severity: low
file: prepare.py
line: 224
issue: double yf.Ticker() instantiation per ticker in process_ticker()
detail: download_ticker() creates yf.Ticker(ticker) internally on line 69 to call
  .history(). Then process_ticker() creates a second yf.Ticker(ticker) on line 224
  for _add_earnings_dates(). This means two separate Ticker objects are created per
  ticker, each potentially triggering network round-trips or cached metadata fetches.
  Not a correctness issue, but it doubles Ticker object creation for 85 tickers.
suggestion: Return the ticker_obj from download_ticker() alongside the DataFrame (as a
  tuple), or accept a ticker_obj parameter in _add_earnings_dates() and pass the one
  created in download_ticker(). Lower priority since prepare.py is a one-time script.
```

---

```
severity: low
file: train.py
line: 312
issue: stale comment says stop_type is 'pivot' or 'fallback' but 'fallback' is now dead code
detail: After R9, the fallback-stop path (`stop = round(price_10am - 2.0 * atr, 2)`) was
  removed. The stop_type variable can now only ever equal 'pivot' at line 298. The comment
  `# R5: 'pivot' or 'fallback'` is now misleading.
suggestion: Update comment to: `# R5: always 'pivot' after R9 (fallback removed)`
```

---

## Summary

The structural logic in R8, R9, and R10 is correct and all 64 tests pass. The most significant
issue is a false-negative unit test (`test_manage_position_no_force_exit_when_profitable`) that
claims to verify R10 does not fire on profitable positions but actually exercises the R10-fires
path due to a fixture mismatch — the profitable scenario is never tested. The stale-cache issue
is the most likely to cause silent operational confusion: users with an existing cache will run
V4-A with R8 disabled and have no indication of this.

No security issues, no data races, no SQL/injection vectors, no exposed secrets.
