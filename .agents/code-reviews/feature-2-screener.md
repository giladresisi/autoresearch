# Code Review: Feature 2 — Screener (`screen_day` in `train.py`)

**Review date:** 2026-03-18
**Plan:** `.agents/plans/feature-2-screener.md`
**Branch:** master (unstaged changes)

---

## Stats

- Files Modified: 1 (`train.py`)
- Files Added: 2 (`tests/__init__.py`, `tests/test_screener.py`)
- Files Deleted: 0
- New lines (approx): 247 (`train.py`) + 196 (`tests/test_screener.py`) + 0 (`__init__.py`)
- Deleted lines (approx): 627 (old GPU training code)

**Test suite:** 19/19 tests passing.

---

## Issues Found

---

```
severity: high
file: tests/test_screener.py
line: 165-179
issue: test_return_dict_has_stop_key, test_stop_always_below_entry, and test_returns_none_or_dict_only are vacuous — their assertions never execute
detail: Both make_passing_df and make_pivot_df fail Rule 5 (pullback >= 8%) every time.
  Verified: pct_local=0.0302 (< 0.08) and pct_ath=0.0690 (< 0.08) for both fixtures.
  screen_day returns None at Rule 5, so all three tests guard their assertions with
  `if result is not None:` — that branch is never taken. The dict content contract
  (stop key exists, stop is float, stop < entry_price) is never actually tested.
suggestion: Add a fixture that explicitly satisfies Rule 5 by setting
  price_10am to at least 8% below local_high and ath (e.g. price_10am = close * 0.88
  and forcing ath to be well above the last close). Alternatively, drop the
  `if result is not None:` guard and instead construct the fixture to guarantee a pass,
  then assert the dict is non-None before inspecting it.
```

---

```
severity: medium
file: tests/test_screener.py
line: 13-30
issue: make_passing_df docstring says "satisfies all screener pre-conditions" but the fixture fails Rule 5 (pullback >= 8%)
detail: The docstring parenthetical "(may not pass stop logic)" only mentions stop logic,
  not Rule 5. Readers will incorrectly assume the fixture produces a valid pass-through
  df. In practice, because close[-1] is approximately 0.94x of its recent peak and
  high = close * 1.005, the local high is less than 5% above price_10am — far below
  the 8% threshold. This misdocumentation caused the three vacuous tests above.
suggestion: Update the docstring to: "Synthetic DataFrame that satisfies Rules 1-4 and R4.
  Intentionally does NOT satisfy Rule 5 (pullback) or stop logic."
```

---

```
severity: medium
file: train.py
line: 103-108
issue: is_stalling_at_ceiling raises ZeroDivisionError if h_min == 0
detail: Line 108: `(h_max - h_min) / h_min` — if the minimum of the last 3 highs is 0.0,
  this raises ZeroDivisionError. While real stock prices are always positive, this
  function can be called with synthetic or corrupted data. The example-screener.py
  reference has the same issue, but train.py is meant to handle arbitrary DataFrames
  including test fixtures.
  In the current test suite, is_stalling_at_ceiling is called with df having high=0.0
  (possible if a test constructs such a df). Currently no test hits this path, but it
  is an unguarded crash in a production helper.
suggestion: Guard with: `if h_min == 0: return False` before the division, or use
  `(h_max / h_min - 1) <= band_pct if h_min > 0 else False`.
```

---

```
severity: low
file: train.py
line: 155-156
issue: c1 and c2 scalars are extracted before the NaN guard, but are not included in the guard
detail: Lines 155-156 extract c1 and c2 from CCI before the NaN guard on line 160.
  The guard only checks c0 (inline at line 179). With >= 150 rows, CCI(20) is always
  defined at positions -1, -2, -3 (all past bar 19), so NaN is structurally impossible
  here. However, if a future caller passes fewer than 20+2 bars, c1/c2 could be NaN
  and the comparison `c0 > c1 > c2` would silently return False (correct behavior due
  to Python's NaN comparison semantics, but not obviously intentional).
suggestion: Either add c1/c2 to the NaN guard on line 160, or add a comment:
  "# c1/c2 NaN-safe: NaN comparisons evaluate to False, causing Rule 4 to reject"
```

---

```
severity: low
file: train.py
line: 47-54
issue: find_pivot_lows uses <= (less-than-or-equal) which allows ties, creating duplicate pivot detections
detail: The pivot condition is `l <= float(df['low'].iloc[i+k])` — if two adjacent bars
  have the identical low, both will be detected as pivots. In synthetic fixture data
  with constant volume and proportional prices, ties are possible. In real OHLCV data
  this is rare but possible at penny-stock prices. The reference example-screener.py
  has the same logic.
  Consequence: find_stop_price may process multiple nearly-identical candidate pivots,
  which is harmless (first valid one is returned) but wastes iterations.
suggestion: Low priority — matches reference implementation intentionally. Document the
  tie behavior in the function comment if desired.
```

---

```
severity: low
file: tests/test_screener.py
line: 86-89
issue: test_stalling_false_for_trending relies on make_passing_df(10) not triggering stalling, but the test name is imprecise
detail: make_passing_df(10) sets close[-3], [-2], [-1] to close[-4]*1.01^n (rising), and
  high = close * 1.005. The last 3 highs are proportionally close — with only 10 rows
  the linspace spans close to the same range as the 3% band_pct check. The test passes
  currently, but the fixture relies on the specific linspace values making the highs
  spread more than 3%. If someone changes the fixture scaling, the test could silently
  reverse.
suggestion: Use a fixture with explicitly spread highs (e.g., high[-3]=100, high[-2]=120,
  high[-1]=140) to make "not stalling" unambiguous. Current test is fragile to fixture
  changes.
```

---

## Positive Observations

- All 11 rules are implemented in the exact order specified by the plan, verified by comparing against example-screener.py line-by-line.
- Column name adaptation (lowercase) is complete and consistent — no capitalized yfinance column names in train.py.
- `price_10am` is used correctly as the "current price" for SMA comparison, pullback %, ATR buffer, and stop distance (not `close`).
- `raw=True` is present in `calc_cci`'s rolling apply.
- No `print()` calls in any production function (only in the `__main__` block).
- NaN/zero guard covers the critical scalars (sma150, vm30, atr) before any rule evaluation.
- R5 None-means-pass logic is correctly implemented: `if res_atr is not None and res_atr < 2.0: return None`.
- Belt-and-suspenders 1.5x ATR check in `screen_day` after `find_stop_price` is consistent with spec.
- `manage_position` stub returns `position['stop_price']` with the correct key name per PRD interface.
- `load_ticker_data` returns None (not raises) for missing files.
- `screen_day` raises `KeyError` (not catches it) when `price_10am` is missing — correct per acceptance criteria.
- `CACHE_DIR` matches the specified path exactly.
