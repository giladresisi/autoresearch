# Code Review: Session Hypothesis System
**Date:** 2026-04-19
**Reviewer:** Claude Sonnet 4.6
**Branch:** master

---

## Stats

- Files Modified: 2 (`signal_smt.py`, `backtest_smt.py`)
- Files Added: 2 (`hypothesis_smt.py`, `tests/test_hypothesis_smt.py`)
- New lines: ~854 (hypothesis_smt.py) + ~533 (tests) + ~64 (modifications)
- Deleted lines: 0

---

## Test Results

- All 22 new tests in `tests/test_hypothesis_smt.py`: **PASS**
- Existing test suite (`tests/` directory): **488 passed, 9 skipped, 0 failures** — zero regressions
- `strategy_smt.py`: confirmed byte-for-byte unmodified (`git diff strategy_smt.py` is empty)
- `ib_insync` not imported at module level in `hypothesis_smt.py`: confirmed

---

## Issues Found

```
severity: low
file: hypothesis_smt.py
line: 608–654
issue: generate() does not guard against hypothesis_json=None before calling _print_hypothesis
detail: When _call_count >= 3 at the time generate() is called (a theoretically reachable state
        if HypothesisManager.generate() is called more than once on the same instance), hypothesis_json
        stays None (the 'if self._call_count < 3:' block is skipped entirely and no fallback is set
        for this path). session_data['hypothesis'] is then None, and _print_hypothesis(now_et, None)
        is called, which crashes with AttributeError: 'NoneType' object has no attribute 'get'.
        In practice this path is unreachable because signal_smt.py guards with _hypothesis_generated,
        but the HypothesisManager class itself has no internal guard.
suggestion: After 'hypothesis_json = None' and the 'if self._call_count < 3:' block, add a fallback:
        if hypothesis_json is None:
            hypothesis_json = {"direction_bias": direction, "confidence": "low",
                               "narrative": "Max API calls reached at generation time.", ...}
        Or add a guard in _print_hypothesis: if h is None: return
```

```
severity: low
file: hypothesis_smt.py
line: 333–336
issue: Dead import — numpy imported inside _compute_rule4 but 'np' is never referenced
detail: 'import numpy as np' appears inside the prior-week calculation block but the symbol
        'np' is never used. The code uses a Python list as a boolean indexer for pandas (which
        works without numpy). This is dead code that slightly inflates per-call overhead.
suggestion: Remove the 'import numpy as np' line. Alternatively, convert pw_mask to a
        proper numpy array: 'pw_mask = np.array([d in pw_bars_list for d in hist_dates])'
        for marginally better pandas indexing performance — but the list form is already correct.
```

```
severity: low
file: hypothesis_smt.py
line: 226
issue: days_analyzed returns n-1 (transition count) rather than actual day count
detail: 'n = len(daily) - 1' is computed as the number of day-to-day transitions,
        then returned as 'days_analyzed'. For 5 days of data, days_analyzed = 4.
        The field name implies actual days analyzed but the value is one less.
suggestion: Either rename to 'transitions_analyzed' or change the return to
        'days_analyzed': len(daily) to match the semantic name. This only affects
        downstream display/analysis consumers; it does not affect direction or strength calculation.
```

```
severity: low
file: .env.example
line: (line with ANTHROPIC_API_KEY)
issue: Placeholder format differs from project standard documented in the plan
detail: The plan specifies 'ANTHROPIC_API_KEY=<set-a-secret-here>' but the actual
        entry is 'ANTHROPIC_API_KEY=<your-anthropic-api-key>'. Not a security issue —
        both are clearly masked placeholder values — but inconsistent with the plan spec.
suggestion: Update to 'ANTHROPIC_API_KEY=<set-a-secret-here>' for consistency, or
        update the plan to reflect the actual placeholder. Either is acceptable.
```

---

## Non-Issues (Verified Correct)

- **Atomic writes**: `_write_file` correctly writes to `.tmp` then calls `.replace()`. Safe on all platforms.
- **Unicode safety in writes**: `json.dumps` uses `ensure_ascii=True` by default, producing pure-ASCII JSON. `write_text()` encoding issue does not apply to the file writes.
- **3-call cap enforcement**: `_call_count` guards in `generate()`, `_revise()`, and `finalize()` are all correct. Normal session flow uses exactly 3 slots: generate → revise → finalize.
- **`matches_hypothesis` is informational only**: Does not gate signals. Confirmed in both `signal_smt.py` (annotates after signal detected) and `backtest_smt.py` (annotates, not filters).
- **`ib_insync` free path**: `hypothesis_smt.py` imports only `datetime`, `json`, `pathlib`, `typing`, `pandas`, and `strategy_smt.compute_tdo`. `anthropic` is lazy-imported inside class methods. `import backtest_smt` and `import hypothesis_smt` both succeed without IB connection.
- **Empty DataFrame safety**: All calls to `_index_dates(hist_mnq_df)` are guarded by `if not hist_mnq_df.empty` checks. The `RangeIndex`-crash path on an empty fallback DataFrame is never reached.
- **`strategy_smt.py` untouched**: `git diff strategy_smt.py` is empty.
- **Prior week ISO edge case**: The `(iso_year - 1, 52)` fallback for week-1 dates is an accepted limitation. Some years have 53 ISO weeks; this would return no prior-week data for those edge cases, causing `prior_week_high/low` to be `None`. This degrades gracefully — `None` values are filtered in all downstream level comparisons.
- **Test conditional `if trades:`**: Backtest integration tests assert only when trades are present. Confirmed trades do fire for the synthetic bar data (tests pass 22/22).
- **TDO computation chain**: `compute_tdo` from `strategy_smt` is imported and called correctly with `(mnq_1m_df, date)` matching the confirmed signature `(mnq_bars: DataFrame, date: datetime.date)`.
