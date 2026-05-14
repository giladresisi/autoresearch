# Code Review: live-session-ram-reduction

**Stats:**
- Files Modified: 4
- Files Added: 0
- Files Deleted: 0
- New lines: +331
- Deleted lines: -56

---

## Pre-existing Failures

**Test**: `tests/test_ib_integration.py::test_ib_start_seeds_bars_no_gap_fill`
**Status**: Pre-existing — requires a live IB Gateway connection at 127.0.0.1:4002, which is unavailable in the test environment. Fails identically on the unmodified `HEAD`. Not introduced by this changeset.

---

## Issues Found

---

### Issue 1

```
severity: medium
file: data/ib_realtime.py
line: 313-315 (and 355-357)
issue: Trim cutoff comparison will raise TypeError if _mnq_1m_df has a tz-naive index
detail: The trim cutoff is `pd.Timestamp.now(tz="America/New_York")` (tz-aware). If the
  DataFrame was loaded from a corrupted parquet that lost its timezone info,
  `self._mnq_1m_df.index[0] < _cutoff` raises `TypeError: Cannot compare tz-naive and
  tz-aware timestamps`. _empty_bar_df() always produces a tz-aware index, and all
  normal write paths preserve tz, but the _load_parquets() fallback path writes
  empty.to_parquet() on corruption which could produce a tz-naive index if the read
  partially succeeded. This is a latent crash risk, not introduced by this PR but
  exposed by the new trim comparison.
suggestion: Wrap the comparison in a tz guard or normalize the index timezone before
  the comparison:
  if (not self._mnq_1m_df.empty
      and self._mnq_1m_df.index.tz is not None
      and self._mnq_1m_df.index[0] < _cutoff):
  Apply identically to the MES trim at line 355-357.
```

---

### Issue 2

```
severity: low
file: automation/main.py
line: 569
issue: bar_idx is computed from _mnq_o_vals length but arrays are truncated to _min_n,
  making bar_idx potentially out-of-bounds when MNQ and MES have different pre-loaded bar counts
detail: bar_idx = len(_mnq_o_vals) - 1 (line 569) is computed AFTER appending the
  current bar to both lists. _min_n = min(len(_mnq_o_vals), len(_mes_h_vals)) is then
  used to slice both lists before building numpy arrays and DataFrames. If the session
  init pre-loaded N_mnq MNQ bars and N_mes MES bars where N_mnq > N_mes (possible
  during contract rollover or when one instrument has a gap), then:
    bar_idx = N_mnq  (correct index into full MNQ arrays)
    _min_n  = N_mes + 1  (one less)
  process_scan_bar then calls mnq_highs[bar_idx] and mnq_reset.iloc[bar_idx] where
  the arrays/DF only have _min_n rows, causing IndexError.
  This was an identical latent bug in the pre-existing code (old code used the same
  pattern with _session_mnq_rows vs _session_mes_rows). The new code preserves the
  same behavior — it is not a regression. In practice N_mnq == N_mes because both
  instruments use the same day/time filter.
suggestion: (Pre-existing, not blocking this PR.) Add a guard after computing _min_n:
  bar_idx = min(bar_idx, _min_n - 1) if _min_n > 0 else 0
  to clamp bar_idx to the valid index range.
```

---

### Issue 3

```
severity: low
file: tests/test_ib_realtime.py
line: 660
issue: test_trim_does_not_run_when_all_bars_within_14_days assertion `len >= original_len`
  does not verify the trim was actually skipped — only that the DF was not shrunk
detail: The test assertion is `assert len(src._mnq_1m_df) >= original_len`, which passes
  as long as the DF grew or stayed the same. This is correct but does not catch the
  case where the trim ran but the new bar replenished the length. A stronger assertion
  would compare index[0] to confirm no old bars were dropped.
suggestion: Add: `assert src._mnq_1m_df.index[0] == recent_df.index[0], "Trim must not
  remove any bars when all are within 14 days"` after the existing assertion.
```

---

## Summary

The four RAM-reduction fixes are logically correct and well-implemented:

1. **Task 1.1 (free 1s DFs)**: The free happens immediately after `_gap_fill_1s_ib()` returns in `start()`. All error paths in `_gap_fill_1s_ib()` (partial instrument failure, connect failure) still return to `start()` where the free executes. Correct.

2. **Task 1.2 (clear session 1s DFs)**: Reset to `_empty_bar_df()` occurs after `to_parquet()` and before `pending.clear()`, which is the correct ordering. Both MNQ and MES handlers are symmetric. Correct.

3. **Task 1.3 (14-day trim)**: The `.copy()` to break the view chain is present and correctly placed after `to_parquet()`. The `not empty` guard prevents `index[0]` access on an empty DF. The only edge case is tz-naive index (Issue 1, rated medium).

4. **Task 2.1 (per-column lists)**: The replacement is complete — `_session_mnq_rows` and `_session_mes_rows` are removed from module-level state, global declarations, session init reset, historical pre-load loops, smt_cache update block, and the bar append block. The `bar_idx` computation uses `len(_mnq_o_vals) - 1` which preserves identical semantics to the old code. The MES Open/Volume zeroing is safe because `process_scan_bar` never reads those columns from `mes_reset`. The `numpy` import is at module level as required.

All 10 new tests pass. The full test suite passes (excluding the pre-existing integration test that requires a live IB Gateway connection).
