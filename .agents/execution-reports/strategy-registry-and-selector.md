# Execution Report: Strategy Registry and LLM-Driven Strategy Selector

**Date:** 2026-03-21
**Plan:** `.agents/plans/strategy-registry-and-selector.md`
**Executor:** Sequential (single agent, waves merged where dependencies were satisfied)
**Outcome:** Success

---

## Executive Summary

Implemented a versioned `strategies/` package (REGISTRY + base_indicators + energy_momentum_v1), an extraction CLI (`scripts/extract_strategy.py`), and an LLM-backed selector (`strategy_selector.py`) using the Anthropic SDK. All 24 new tests pass; total suite count grew from 65 to 89 passing (1 skipped), with zero regressions.

**Key Metrics:**
- **Tasks Completed:** 8/8 (100%)
- **Tests Added:** 24 (10 in test_registry.py + 14 in test_selector.py)
- **Test Pass Rate:** 89/89 passing (100%), 1 skipped (pre-existing)
- **Files Modified:** 3 (PROGRESS.md, prd.md, pyproject.toml) + 7 new files
- **Lines Changed:** ~679 new implementation lines, ~387 new test lines
- **Alignment Score:** 9/10

---

## Implementation Summary

### Wave 1A — strategies/base_indicators.py (124 lines)
Eight shared indicator functions copied verbatim from `train.py` (lines 51–164): `calc_cci`, `calc_rsi14`, `calc_atr14`, `find_pivot_lows`, `zone_touch_count`, `find_stop_price`, `is_stalling_at_ceiling`, `nearest_resistance_atr`. Module docstring added; no logic changes.

### Wave 1B — pyproject.toml + scripts/__init__.py
Added `"anthropic>=0.40.0"` to the dependencies list in `pyproject.toml`. Created empty `scripts/__init__.py` to make `scripts/` a proper package.

### Wave 2A — strategies/energy_momentum_v1.py (262 lines)
Extracted from `git show e9886df:train.py`. Contains METADATA dict (all required keys), all indicator functions (self-contained, not importing from base_indicators), `screen_day()`, and `manage_position()`. Harness-only symbols (BACKTEST_START, BACKTEST_END, load_ticker_data, etc.) were excluded per plan.

### Wave 2B/3A — strategies/__init__.py (merged)
REGISTRY dict created with `energy-momentum-v1` already registered, merging what the plan split across waves 2B (skeleton) and 3A (register entry), since the dependency was already satisfied.

### Wave 3B — scripts/extract_strategy.py (106 lines)
CLI tool using `subprocess.run(["git", "show", f"{tag}:train.py"])`, slicing content above the BOUNDARY marker and writing to `strategies/<name>.py`. BOUNDARY set as `"DO NOT EDIT BELOW THIS LINE"` (substring, no Unicode prefix) for robustness across train.py variants.

### Wave 4A — strategy_selector.py (187 lines)
`select_strategy(ticker, recent_df, today)` builds a ticker snapshot (current_price, SMA20, RSI14, ATR14, vol_ratio, pct_change_30d, above_sma20), formats a structured prompt including REGISTRY metadata, calls claude-opus-4-6 (or model specified in env), parses JSON response, normalizes empty-string strategy to None, and returns `{strategy, explanation, confidence}`. Import-time API key guard with `_SELECTOR_SKIP_KEY_CHECK` escape hatch for tests.

### Wave 5A/5B — tests/test_registry.py + tests/test_selector.py
24 tests covering registry structure, METADATA validation, base_indicators callability and numerical correctness, screen_day behavior on short data, snapshot computation, select_strategy with mocked API, malformed JSON fallback, missing-key defaults, and extract_strategy boundary detection.

---

## Divergences from Plan

### Divergence #1: RSI test series — alternating up/down instead of monotone rising

**Classification:** GOOD

**Planned:** Use a monotone rising close series to test that RSI > 50 on an upward series.
**Actual:** Used a 3-up : 1-down alternating series (+1.0, +1.0, +1.0, -0.5 cycle).
**Reason:** A monotone rising series has zero losses in every 14-bar window, causing the RSI denominator (average loss) to be zero and producing NaN for all bars. The test would then fail the `len(valid) > 0` assertion.
**Root Cause:** Plan gap — the RSI formula requires non-zero losses to produce finite values; a monotone series pathologically produces all-NaN output.
**Impact:** Neutral. The test now correctly validates RSI numerical range and directional signal rather than asserting on a degenerate series.
**Justified:** Yes.

---

### Divergence #2: BOUNDARY constant — substring match without Unicode prefix

**Classification:** GOOD

**Planned:** `BOUNDARY = "# DO NOT EDIT BELOW THIS LINE"` (exact prefix match).
**Actual:** `BOUNDARY = "DO NOT EDIT BELOW THIS LINE"` (substring, no `#` prefix and no Unicode dash decoration).
**Reason:** The actual `train.py` boundary line is `# ── DO NOT EDIT BELOW THIS LINE ───` (Unicode em-dashes). A prefix match on `"# DO NOT EDIT..."` fails because the Unicode dashes appear between `#` and the text.
**Root Cause:** Plan specified the boundary text from an earlier version of train.py that lacked the Unicode decoration added in the harness overhaul.
**Impact:** Positive — substring match is more robust and will survive future whitespace or decoration changes.
**Justified:** Yes.

---

### Divergence #3: extract_strategy.py help text — ASCII arrow instead of Unicode

**Classification:** ENVIRONMENTAL

**Planned:** Plan did not specify the help text character encoding.
**Actual:** Used `=>` (ASCII) instead of `→` (U+2192) in argparse help strings.
**Reason:** Python on Windows with code page cp1252 raises `UnicodeEncodeError` when `argparse` prints help text containing non-cp1252 characters to stdout.
**Root Cause:** Windows terminal encoding mismatch — cp1252 does not include U+2192.
**Impact:** Neutral. No behavioral change; only cosmetic difference in `--help` output.
**Justified:** Yes.

---

### Divergence #4: Waves 2B and 3A merged

**Classification:** GOOD

**Planned:** Wave 2B creates a skeleton `strategies/__init__.py` (empty REGISTRY); Wave 3A updates it to add the energy_momentum_v1 entry.
**Actual:** `strategies/__init__.py` was written with the entry already included, skipping the skeleton intermediate state.
**Reason:** Sequential execution — when Wave 2A (energy_momentum_v1.py) completes before __init__.py is written, there is no value in creating a blank REGISTRY first.
**Root Cause:** Plan was designed for parallel agent execution where 2B and 3A would be separate tasks; in sequential execution the two-step split is unnecessary.
**Impact:** Positive — fewer writes, simpler execution path.
**Justified:** Yes.

---

## Test Results

**Tests Added:**
- `tests/test_registry.py` — 10 tests
- `tests/test_selector.py` — 14 tests

**Test Execution (new files only):**
```
24 passed in 1.21s
```

**Full suite (excluding pre-existing broken integration tests):**
```
89 passed, 1 skipped in 1.67s
```

**Pre-existing collection errors (not in scope):**
- `tests/test_e2e.py` — requires live network/broker; ERROR at collection
- `tests/test_prepare.py` — requires live data fetch; ERROR at collection

**Pass Rate:** 89/89 (100%), 1 skipped (pre-existing)

---

## What was tested

- REGISTRY is a dict and contains the `"energy-momentum-v1"` key.
- Every module in REGISTRY exposes a `METADATA` dict with all 13 required keys.
- Every module in REGISTRY exposes callable `screen_day()` and `manage_position()` functions.
- `energy_momentum_v1.METADATA` values match the documented optimization context (commit, branch, sector, dates, pnl, sharpe, trade count).
- `screen_day()` returns `None` when the input DataFrame has fewer than 60 rows (insufficient indicator warmup history).
- `base_indicators` module is importable and all eight indicator functions are callable.
- `calc_rsi14` produces values in [0, 100] with a final value > 50 on a net-upward series, and does not produce all-NaN output.
- `calc_atr14` produces non-negative values after the warmup period.
- `_compute_ticker_snapshot` returns a dict with all seven expected keys.
- `_compute_ticker_snapshot` produces RSI values in [0, 100].
- `_compute_ticker_snapshot` returns `{"error": ...}` rather than crashing when the input is shorter than 14 bars.
- `_compute_ticker_snapshot` correctly reports `above_sma20 = True` for a rising price series.
- `_compute_ticker_snapshot` returns `vol_ratio_5d_30d = None` rather than raising `ZeroDivisionError` when all volume values are zero.
- `select_strategy` returns the correct strategy name and confidence when the mocked LLM returns a valid JSON match.
- `select_strategy` returns `strategy=None` with `confidence="low"` when the LLM finds no match.
- `select_strategy` does not crash and returns `strategy=None` when the LLM returns non-JSON text; the raw text is included in `explanation`.
- `select_strategy` return dict always contains exactly `{strategy, explanation, confidence}`.
- `select_strategy` normalizes an empty-string strategy to `None`.
- `select_strategy` defaults `explanation` to `""` when the LLM omits the key.
- `select_strategy` defaults `confidence` to `"low"` when the LLM omits the key.
- The `BOUNDARY` constant in `extract_strategy.py` is a substring of the current `train.py` boundary line (ensuring real extraction will not silently fail).
- `extract()` writes only content above the boundary and excludes everything below it, verified against a mock `git show` response.

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `pytest tests/test_registry.py tests/test_selector.py -v` | 24/24 passed | All new tests green |
| 2 | `pytest --ignore=tests/test_e2e.py --ignore=tests/test_prepare.py -q` | 89 passed, 1 skipped | Zero regressions |
| 3 | Live API call with real `ANTHROPIC_API_KEY` | Not run (optional) | API key not available in test environment |

---

## Challenges & Resolutions

**Challenge 1: RSI NaN on monotone series**
- **Issue:** Plan's suggested test used a monotone rising close array, which produces zero losses in every 14-bar EWM window — RSI denominator is zero, all values NaN.
- **Root Cause:** The RSI formula requires at least one down-day in the lookback window to compute a finite result. A perfectly smooth price series pathologically breaks the indicator.
- **Resolution:** Switched to a 3-up : 1-down alternating series ensuring non-zero average losses throughout.
- **Time Lost:** ~5 minutes diagnosing the NaN output.
- **Prevention:** Document in plan that RSI test series must contain non-monotone close sequences.

**Challenge 2: Unicode character in argparse help text crashes on Windows**
- **Issue:** `→` (U+2192) in `extract_strategy.py` argparse help string caused `UnicodeEncodeError: 'charmap' codec can't encode character` when running `--help` in a Windows cp1252 terminal.
- **Root Cause:** Python on Windows uses the system code page (cp1252 in this environment) for stdout unless overridden. U+2192 is not in cp1252.
- **Resolution:** Replaced `→` with ASCII `=>` in help text. No behavioral change.
- **Time Lost:** ~3 minutes.
- **Prevention:** Add to CLAUDE.md: "Avoid non-ASCII characters in CLI help text for cross-platform tools targeting Windows."

**Challenge 3: Boundary mismatch between plan spec and actual train.py**
- **Issue:** Plan specified `BOUNDARY = "# DO NOT EDIT BELOW THIS LINE"` but the actual line in train.py is `# ── DO NOT EDIT BELOW THIS LINE ───` (with Unicode em-dashes added during the harness overhaul).
- **Root Cause:** The plan was written before the harness overhaul decorated the boundary line with Unicode dashes.
- **Resolution:** Changed BOUNDARY to `"DO NOT EDIT BELOW THIS LINE"` (core phrase only, no decoration), then verified `BOUNDARY in train_py` in a dedicated test.
- **Time Lost:** ~5 minutes.
- **Prevention:** The `test_boundary_constant_matches_train_py` test now catches any future drift automatically.

---

## Files Modified

**New files — strategies package (3 files):**
- `strategies/__init__.py` — REGISTRY dict with energy_momentum_v1 entry (+12/-0)
- `strategies/base_indicators.py` — 8 shared indicator functions (+124/-0)
- `strategies/energy_momentum_v1.py` — first published strategy, self-contained (+262/-0)

**New files — scripts package (2 files):**
- `scripts/__init__.py` — empty package marker (+0/-0)
- `scripts/extract_strategy.py` — extraction CLI (+106/-0)

**New files — selector + tests (3 files):**
- `strategy_selector.py` — LLM selector using Anthropic SDK (+187/-0)
- `tests/test_registry.py` — 10 registry/METADATA/base_indicators tests (+147/-0)
- `tests/test_selector.py` — 14 snapshot/selector/extract tests (+240/-0)

**Modified files (3 files):**
- `pyproject.toml` — added `anthropic>=0.40.0` dependency (+1/-0)
- `PROGRESS.md` — feature status update (+minor)
- `prd.md` — no substantive changes (pre-existing modification)

**Total (new files):** ~1,079 lines inserted, 0 deleted

---

## Success Criteria Met

- [x] `strategies/` package importable from project root
- [x] `REGISTRY["energy-momentum-v1"]` resolves to a module with METADATA, screen_day, manage_position
- [x] All 13 required METADATA keys present and correctly valued
- [x] `base_indicators.py` exports all 8 indicator functions
- [x] `scripts/extract_strategy.py` correctly splits on the BOUNDARY marker
- [x] BOUNDARY constant validated against actual train.py in a test
- [x] `strategy_selector.select_strategy()` returns `{strategy, explanation, confidence}`
- [x] Malformed LLM JSON handled gracefully (no crash, strategy=None)
- [x] Empty-string strategy normalized to None
- [x] Missing keys in LLM response defaulted safely
- [x] `anthropic>=0.40.0` added to pyproject.toml
- [x] 24 new tests, all passing
- [x] Zero regressions in existing 65-test baseline
- [ ] Live API integration test with real ANTHROPIC_API_KEY (marked optional in plan; not run — API key not in test environment)

---

## Recommendations for Future

**Plan Improvements:**
- Specify that RSI test series must use a non-monotone close sequence (at least one down-day per 14-bar window). Add a note: "A monotone rising series will produce all-NaN RSI and cause the test to fail."
- Include the exact BOUNDARY string as it appears in the current file at plan-writing time, and note which harness version introduced it, to avoid staleness.
- For Windows-targeted CLI tools, note: "Avoid non-ASCII characters (Unicode arrows, dashes, emoji) in argparse help text."

**Process Improvements:**
- When plan waves are designed for parallel agents but executed sequentially, merge consecutive dependent waves (e.g., 2B + 3A skeleton + populate) into a single write operation to reduce intermediate file churn.
- Add a `test_boundary_constant_matches_train_py` style guard test whenever a constant is extracted from another file's content — this catches drift early without manual review.

**CLAUDE.md Updates:**
- Add note under "Windows / CLI tools": avoid non-ASCII characters in argparse/click help strings when the tool must run on Windows with cp1252 terminal encoding.
- Add note under "Testing": RSI and similar ratio indicators require non-trivial series (at least one down-day) to produce finite values; monotone test series pathologically break ratio indicators.

---

## Conclusion

**Overall Assessment:** The feature was implemented cleanly and completely. All eight planned files were created, the `anthropic` dependency was added, 24 tests were written and pass, and the baseline test count grew from 65 to 89 with zero regressions. The four divergences from the plan were all improvements or environmental adaptations — none represent plan violations or quality regressions. The only unmet criterion (live API integration test) was explicitly marked optional in the plan and is blocked by environment, not by implementation.

**Alignment Score:** 9/10 — Complete delivery; minor deductions for BOUNDARY mismatch and Unicode encoding issue that required unplanned diagnosis, both of which were resolvable. The test for BOUNDARY drift now prevents recurrence.

**Ready for Production:** Yes, with the caveat that `ANTHROPIC_API_KEY` must be set in the runtime environment before `strategy_selector.select_strategy()` is called. The import-time guard will raise a `RuntimeError` with a clear message if the key is absent.
