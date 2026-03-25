# Code Review: train.py Performance Optimization

**Date:** 2026-03-25
**Plan:** `.agents/plans/train-py-performance-optimization.md`
**Execution Report:** `.agents/execution-reports/train-py-performance-optimization.md`

---

## Stats

- Files Modified: 4
- Files Added: 0
- Files Deleted: 0
- New lines: +160
- Deleted lines: -29

---

## Test Results

All 50 tests pass with 0 regressions. The GOLDEN_HASH update is correct.

---

## Issues Found

---

```
severity: medium
file: C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main\screener.py
line: 191
issue: "too_few_rows" rejection bucket is structurally dead — never populated
detail: The outer loop at line 191 does `if df.empty or len(df) < 102: continue` BEFORE
        `total_checked` is incremented and BEFORE `_rejection_reason()` is called.
        Additionally, `df_extended = pd.concat([df, synthetic_row])` adds one row,
        so df_extended always has >= 103 rows when it reaches screen_day.
        screen_day's own `len(df) < 102` check therefore also never fires.
        The "too_few_rows" label in `_RULES` will always show count=0 in the breakdown,
        silently hiding any tickers that are too short to screen.
suggestion: Move `total_checked += 1` and the rejection-count increment to BEFORE the
            `len(df) < 102` early-continue, or add a separate pre-filter counter for
            short-history tickers. Alternatively, remove "too_few_rows" from `_RULES`
            and from `_rejection_reason()` since it can never fire given current caller
            structure, and document the short-history pre-filter separately.
```

---

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main\screener.py
line: 266
issue: Variable `rejected` is computed but never used
detail: Line 266: `rejected = total_checked - len(armed) - len(gap_skipped)`
        This variable is assigned and then immediately falls out of scope. It is
        referenced nowhere in the print loop that follows. The print loop iterates
        `rejection_counts.items()` directly, making `rejected` dead code.
suggestion: Remove the line, or use it as the total in the header:
            `print(f"=== REJECTION BREAKDOWN ({rejected} rejected / {total_checked} evaluated) ===")`
            which would give users a clearer picture (rejected vs evaluated, not just evaluated).
```

---

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main\screener.py
line: 54-56
issue: "no_price" rejection bucket is structurally unreachable given current caller
detail: _rejection_reason receives current_price as a float argument. The caller
        (run_screener, lines 197-199) always ensures current_price is a float:
        it falls back to prev_close (also a float) when _fetch_last_price returns None.
        Therefore `pd.isna(current_price)` at line 55 is always False, and the
        "no_price" bucket accumulates 0 indefinitely.
        This creates a misleading gap in the diagnostic output — users may infer
        no tickers ever had a missing price, which may not reflect reality (failures
        during _fetch_last_price are silently swallowed by the except block at line 128).
suggestion: Either document in the function docstring that "no_price" only fires if
            the caller passes float('nan'), or rewrite to detect the fetch-failure case
            explicitly (e.g., track tickers where _fetch_last_price returned None before
            the fallback). The label could be renamed to "price_fetched_from_fallback"
            as an informational bucket instead.
```

---

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main\screener.py
line: 264-272
issue: Rejection breakdown prints when `not armed` even during normal no-signal days, leaking diagnostic noise to production output
detail: The condition `if diagnose or not armed:` means that on any legitimate quiet
        market day where no tickers produce a signal, the full rejection breakdown is
        printed unconditionally. This is probably intentional (helps debug why nothing
        fired), but the design conflates "no signals today" with "user wants diagnostics."
        On a systematically bear market with no setups for weeks, every run would print
        the breakdown — this is noisy for scheduled/automated runs.
suggestion: Consider separating the two triggers: `--diagnose` for explicit diagnostic
            mode, and a separate flag like `--verbose` or a log-level env var for the
            "no signals" case. Alternatively, document this behavior in the module
            docstring so operators know to expect it.
```

---

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main\screener.py
line: 98-103
issue: _rejection_reason conflates ATR computation failure with RSI out-of-range
detail: Lines 100-101: `if pd.isna(atr) or atr == 0 or pd.isna(rsi): return "rsi_out_of_range"`
        An ATR computation failure (pd.isna(atr) or atr == 0) is classified as
        "rsi_out_of_range", which is misleading in the diagnostic output. A broken
        ATR would surface as "rsi_out_of_range" even though RSI is irrelevant.
        This mirrors screen_day's own behavior exactly (same early-return), so it is
        not a correctness divergence — but it is a diagnostic accuracy issue.
suggestion: Add a separate label "atr_invalid" as the first check before the RSI
            range check, or document in the _RULES comment that "rsi_out_of_range"
            covers ATR failures too. Since _rejection_reason is a diagnostic mirror,
            even a comment in the code is sufficient.
```

---

## Vectorized Numpy Operations: Correctness Verified

The following correctness properties were verified analytically and with targeted Python assertions:

**`find_pivot_lows()` (train.py L138-151):**
- `sliding_window_view(lows, 2*bars+1)` produces `n - 2*bars` windows, one per center candidate.
- Center candidates span indices `bars` through `n-bars-1` inclusive, matching `range(bars, len(df)-bars)` exactly.
- `center <= window_min` is equivalent to `center == window_min` since `window_min <= center` always holds; this reproduces the original `all(l <= neighbor ...)` semantics including tie cases (two equal minima are both marked as pivots, matching the original `<=` comparator).
- Boundary alignment confirmed: correct offset `+ bars` applied to `np.where` result.
- **No issues.**

**`nearest_resistance_atr()` (train.py L209-225):**
- Same `sliding_window_view` pattern; `center >= window_max` (equivalent to `center == window_max`) reproduces the original `h >= all neighbors` condition.
- `center > entry_price` mirrors original `h > entry_price`.
- Removal of `.copy().reset_index(drop=True)` is safe: the vectorized path operates entirely on `.values` arrays.
- `pivot_highs.min()` correctly returns the nearest (lowest) overhead resistance.
- **No issues.**

**`zone_touch_count()` (train.py L154-157):**
- Single numpy boolean expression replaces generator sum; identical arithmetic.
- **No issues.**

**`manage_position()` tail-slice (train.py L356):**
- `calc_atr14` uses `rolling(14).mean()` on true range. TR at each bar requires only that bar's OHLC and previous bar's close. With 30 rows: 29 valid TR values, 16 valid rolling(14) values. The last value is identical to computing on the full df.
- **No issues.**

**`detect_regime()` (train.py L440-443):**
- `df.loc[:today]` on a sorted DatetimeIndex uses binary search (O(log N)), identical result to O(N) boolean mask.
- `hist['close'].iloc[-50:].mean()` is the definition of SMA50 at the last bar; identical to `rolling(50).mean().iloc[-1]`.
- **No issues.**

---

## Summary

The vectorized numpy implementations are all mathematically correct. The only bugs are in the diagnostic layer of `screener.py`: two rule buckets (`too_few_rows`, `no_price`) are structurally unreachable given the current caller, one variable is dead code, and the `rsi_out_of_range` label conflates ATR failures. None of these affect the correctness of `screen_day()`, the backtester, or any trading signal. The issues are all confined to the diagnostic/reporting path.

No security issues. No performance regressions. All 50 tests pass.
