# Execution Report: Session Hypothesis System

**Date:** 2026-04-19
**Plan:** `.agents/plans/session-hypothesis.md`
**Executor:** Sequential (single agent, wave-by-wave)
**Outcome:** Success

---

## Executive Summary

The session hypothesis system was implemented in full across all three waves: the deterministic rule engine and `HypothesisManager` class in `hypothesis_smt.py`, integration into both `signal_smt.py` (6 touch points) and `backtest_smt.py` (5 touch points), and 22 unit/integration tests covering all planned cases plus 5 additional edge cases. The test suite passed with zero regressions against the 488-test baseline, ending at 510 passed / 9 skipped.

**Key Metrics:**
- **Tasks Completed:** 6/6 (100%) — Tasks A, B, C, D, E, and .env.example update
- **Tests Added:** 22 (plan specified 17 minimum)
- **Test Pass Rate:** 22/22 (100%); full suite 510/510 passed (9 skipped, zero failed)
- **Files Modified:** 4 (2 new, 2 modified)
- **Lines Changed:** +853 `hypothesis_smt.py` (new), +532 `tests/test_hypothesis_smt.py` (new), +42 `signal_smt.py`, +22 `backtest_smt.py`
- **Execution Time:** ~1 session
- **Alignment Score:** 9/10

---

## Implementation Summary

### Wave 1 — `hypothesis_smt.py` (Tasks A + B)

`hypothesis_smt.py` (853 lines) was created containing:

- **Task A — Rule Engine:** Six private helper functions (`_compute_rule1` through `_compute_rule5`, `_compute_overnight`, `_compute_fvgs`) plus `_weighted_vote` and the public `compute_hypothesis_direction`. A private `_assign_case` helper was extracted from `_compute_rule1` to isolate the case-detection logic — this was an unplanned but justified refactor for testability. The function detects all five Rule 1 cases (1.1–1.5), computes multi-day trend (Rule 2), deception sweeps (Rule 3), session extremes (Rule 4), and TDO/TWO premium-discount zones (Rule 5). The weighted vote function applies weights as specified: Rule 1 = 3, week_zone = 2, day_zone = 1, trend = 1.

- **Task B — `HypothesisManager`:** Full lifecycle class with `generate()` (Claude Call 1), `evaluate_bar()` (no LLM, per-bar evidence classification), `_revise()` (Claude Call 2, triggered at contradiction threshold), and `finalize()` (Claude Call 3). Atomic file writes via `.tmp` → rename pattern. `_build_session_context()` helper assembles all rule outputs for Claude prompts. All three Claude system prompts (generation, revision, summary) are embedded as class constants. Max 3 API calls per session enforced by `_call_count` guard.

### Wave 2A — `signal_smt.py` Integration (Task C)

Six integration points were added (plan specified 4 modifications but the actual touch points were 6 logical locations):

1. Import `HypothesisManager` at module level
2. Module-level state variables `_hypothesis_manager` and `_hypothesis_generated`
3. `_load_hist_mnq()` helper added near `_load_parquets()`
4. `main()` — instantiate `HypothesisManager` after loading dataframes
5. `on_mnq_1m_bar()` — `generate()` on first bar at/after SESSION_START, `evaluate_bar()` on every bar
6. `_process_scanning()` — annotate signal with `matches_hypothesis`
7. `_process_managing()` — call `finalize()` on non-hold exits before `_position = None`

The plan described "4 modifications" but numbered through to Modification 6 in the spec — the count of 6 is accurate and consistent with the plan's actual numbered list.

### Wave 2B — `backtest_smt.py` Integration (Task D)

Five integration points added (22 lines):

1. Import `compute_hypothesis_direction` at module level
2. Load `_hist_mnq_df` from parquet (or empty DataFrame if absent) before day loop
3. Call `compute_hypothesis_direction` once per session day, storing `_session_hyp_dir`
4. Annotate signal with `matches_hypothesis` in both `WAITING_FOR_ENTRY` and `REENTRY_ELIGIBLE` state blocks
5. Propagate `matches_hypothesis` to trade records at all three exit paths (in-session, session_close, end_of_backtest)
6. Added `"matches_hypothesis"` to TSV fieldnames in `_write_trades_tsv()`

`strategy_smt.py` was confirmed untouched (`git diff strategy_smt.py` shows empty output).

### Wave 3 — Tests (Task E)

22 tests implemented in `tests/test_hypothesis_smt.py` (532 lines):

- 6 Rule 1 case tests (1.1–1.5, including both 1.3 sub-variants)
- 3 weighted vote tests
- 3 edge case tests (no overnight bars, no prior day, return type guard)
- 5 `evaluate_bar` tests (supports, contradicts, neutral level touch, extreme contradiction, revision trigger)
- 2 `generate()` tests with mocked Claude (success path, API failure fallback)
- 3 backtest integration tests (`matches_hypothesis` true/false/None)

### .env.example

`ANTHROPIC_API_KEY=<set-a-secret-here>` added.

---

## Divergences from Plan

### Divergence #1: `_assign_case` helper extracted from `_compute_rule1`

**Classification:** GOOD

**Planned:** `_compute_rule1` handles all case detection internally.
**Actual:** `_assign_case(crossed_mid, near_far_extreme, price_now_above_mid)` extracted as a separate private function; imported in test file alongside `_compute_rule1`.
**Reason:** Isolating the case-assignment logic made it easier to unit-test cases independently without needing to construct full bar DataFrames for every case variant.
**Root Cause:** Test design choice — the plan's test spec imports `_compute_rule1` directly, which still works, but `_assign_case` was also exposed to make isolated case-logic assertions simpler.
**Impact:** Positive — cleaner testability, smaller functions, no behavior change.
**Justified:** Yes

### Divergence #2: `signal_smt.py` description as "4 modifications" vs actual 6

**Classification:** GOOD

**Planned:** Plan header for Task C says "4 integration points" but the numbered modifications list runs 1–6.
**Actual:** 6 modifications were implemented, consistent with the numbered spec list.
**Reason:** Plan had an ambiguous header count; the numbered list was authoritative and all 6 were implemented.
**Root Cause:** Plan authoring inconsistency (header vs body).
**Impact:** Neutral — all specified behavior present; the 6-point count matches the plan body.
**Justified:** Yes

### Divergence #3: Test count 22 vs planned 17

**Classification:** GOOD

**Planned:** 17 test cases enumerated in the plan (test case names listed explicitly).
**Actual:** 22 tests implemented — all 17 planned plus 5 additional.
**Reason:** The two `test_compute_direction_case_1_3_*` tests were split into separate long/short variants (plan named them as one test in the comment but described two scenarios). Four additional edge-case and backtest variants were added for completeness.
**Root Cause:** Natural extension while implementing — adjacent behaviors warranted coverage.
**Impact:** Positive — broader coverage with no test-count inflation from duplicates.
**Justified:** Yes

---

## Test Results

**Tests Added:**
- `tests/test_hypothesis_smt.py` — 22 tests covering rule engine, weighted vote, evidence classification, generate() (mocked), and backtest integration

**Test Execution:**
```
tests/test_hypothesis_smt.py  22 passed
Full suite:                  510 passed, 9 skipped, 0 failed
Baseline before feature:     488 passed, 9 skipped
Delta:                       +22 new passing tests, 0 regressions
```

**Pass Rate:** 22/22 new tests (100%); 510/510 full suite (100%)

---

## What was tested

- `_compute_rule1` returns case "1.3" with long bias when overnight bars stay below pd_midpoint and price at 09:00 is below midpoint.
- `_compute_rule1` returns case "1.3" with short bias when overnight bars stay above pd_midpoint and price at 09:00 is above midpoint.
- `_compute_rule1` returns case "1.2" with short bias when overnight crosses midpoint AND approaches near the far extreme (pdh).
- `_compute_rule1` returns case "1.4" with long bias when overnight crosses midpoint but price settles above mid without approaching far extreme.
- `_compute_rule1` returns case "1.1" with short bias when overnight crosses midpoint but price settles below mid without approaching far extreme.
- `_compute_rule1` returns case "1.5" when price at 09:00 falls outside the previous-day [pdl, pdh] range.
- `_weighted_vote` returns "long" when Rule 1 long (weight 3) + discount week (2) + discount day (1) all align, regardless of trend.
- `_weighted_vote` returns "neutral" when all inputs are None or "neutral" (no side reaches threshold).
- `_weighted_vote` returns "long" when Rule 1 long (weight 3) overrides a bearish trend input (weight 1).
- `compute_hypothesis_direction` returns `None` when passed an empty DataFrame (no 09:00 bar → r5 returns None).
- `compute_hypothesis_direction` returns a valid string or None (never raises) when prior-day bars are absent.
- `compute_hypothesis_direction` always returns `"long"`, `"short"`, `"neutral"`, or `None` — never raises on well-formed input.
- `HypothesisManager.evaluate_bar` appends a "supports" entry when price moves 16+ pts in the hypothesis direction.
- `HypothesisManager.evaluate_bar` appends a "contradicts" entry when price moves 16+ pts against the hypothesis direction.
- `HypothesisManager.evaluate_bar` appends a "neutral" entry when price touches a key level within 5 points.
- `HypothesisManager.evaluate_bar` sets `_contradiction_count` to `CONTRADICTION_THRESHOLD` immediately on an extreme contradiction (close >20 pts beyond TDO in wrong direction).
- `HypothesisManager.evaluate_bar` sets `_revision_triggered = True` and calls `_revise()` after accumulating 3 contradiction entries.
- `HypothesisManager.generate()` writes a valid `hypothesis.json` file in `data/sessions/YYYY-MM-DD/` when Claude API call succeeds (mocked response).
- `HypothesisManager.generate()` writes a fallback `hypothesis.json` with a rule-engine direction when the Claude API raises an exception.
- `run_backtest()` sets `trade["matches_hypothesis"] = True` when signal direction matches the hypothesis direction returned by `compute_hypothesis_direction`.
- `run_backtest()` sets `trade["matches_hypothesis"] = False` when signal direction differs from the hypothesis direction.
- `run_backtest()` sets `trade["matches_hypothesis"] = None` when `compute_hypothesis_direction` returns `None`.

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `python -c "import hypothesis_smt"` | Pass | No import errors |
| 2 | `python -c "import signal_smt"` | Pass | No IB connection required |
| 3 | `python -c "import backtest_smt"` | Pass | No runtime errors |
| 4 | `git diff strategy_smt.py` | Pass | Empty diff — file untouched |
| 5 | `uv run pytest tests/test_hypothesis_smt.py -v` | Pass | 22/22 passed |
| 6 | `uv run pytest --tb=short -q` | Pass | 510 passed, 9 skipped, 0 failed |

---

## Challenges & Resolutions

**Challenge 1:** Rule 1 case detection requires careful precedence ordering

- **Issue:** Cases 1.1 and 1.4 share the condition `crossed_mid AND NOT near_far_extreme` but differ only on `price_now_above_mid`. The spec describes them in non-intuitive order (1.5 → 1.3 → 1.2 → 1.4 → 1.1).
- **Root Cause:** Plan spec is organized by conceptual grouping, not by evaluation priority.
- **Resolution:** Implemented evaluation as a priority chain in `_assign_case`: check 1.5 (outside range) first, then 1.3 (no crossing), then 1.2 (near far extreme), then split 1.4/1.1 on above/below mid.
- **Time Lost:** Minimal
- **Prevention:** Future rule implementations should include explicit evaluation order in plan rather than prose ordering.

**Challenge 2:** Backtest `matches_hypothesis` needed at 3 exit sites, not 1

- **Issue:** Plan's Task D identified one `_build_trade_record` call site for modification, but `backtest_smt.py` has three (in-session exit, session_close, end_of_backtest).
- **Root Cause:** Plan text said "There are TWO places where `_build_signal_from_bar` is called" (for signal annotation) but only one was mentioned for trade record annotation. In practice the trade record needs the field at all exits.
- **Resolution:** Added `trade["matches_hypothesis"] = position.get("matches_hypothesis")` at all three exit paths.
- **Time Lost:** Minimal
- **Prevention:** Plan should enumerate all exit paths when specifying trade record field additions.

---

## Files Modified

**New Files (2):**
- `hypothesis_smt.py` — Rule engine + HypothesisManager (853 lines, +853/0)
- `tests/test_hypothesis_smt.py` — 22 unit/integration tests (532 lines, +532/0)

**Modified Files (2):**
- `signal_smt.py` — 6 integration points (+42/0)
- `backtest_smt.py` — 5 integration points + TSV column (+22/0)

**Total:** +1449 insertions(+), 0 deletions(-)

---

## Success Criteria Met

- [x] `hypothesis_smt.py` imports cleanly in both realtime and backtest contexts
- [x] `compute_hypothesis_direction` never raises; returns `str|None`
- [x] `HypothesisManager.generate()` writes atomic JSON file before returning
- [x] `HypothesisManager.evaluate_bar()` never calls the API
- [x] Max 3 Claude API calls per session enforced by `_call_count` guard
- [x] `strategy_smt.py` is unmodified (`git diff strategy_smt.py` empty)
- [x] `matches_hypothesis` present in realtime signal dict (and propagated to position/finalize)
- [x] `matches_hypothesis` column in backtest TSV output
- [x] `data/sessions/YYYY-MM-DD/` subdirectory created automatically
- [x] All `test_hypothesis_smt.py` tests pass with zero regressions to existing suite
- [x] `.env.example` has `ANTHROPIC_API_KEY=<set-a-secret-here>`
- [x] All 5 Rule 1 cases (1.1–1.5) detected correctly
- [x] Weighted vote threshold correct (direction reaches score ≥ 2 to avoid "neutral")
- [x] Extreme contradiction sets `contradiction_count` to threshold immediately
- [ ] `matches_hypothesis` present in realtime `position.json` on disk (deferred — requires live IB session to validate)
- [ ] Rule engine produces a non-None direction when run against real `MNQ_1m.parquet` + `MNQ.parquet` (smoke test requires live data files)

---

## Recommendations for Future

**Plan Improvements:**
- When specifying trade record field additions, enumerate ALL exit paths in the backtest loop, not just the primary one. Three separate `_build_trade_record` call sites existed and all needed the annotation.
- Clarify "4 modifications" vs "6 numbered modifications" in wave 2A to avoid ambiguity in execution report counts.
- Include explicit evaluation order for multi-case rule detection (e.g., "evaluate in this order: 1.5 → 1.3 → 1.2 → 1.4/1.1") rather than relying on prose case descriptions.

**Process Improvements:**
- For complex rule engines with multiple case branches, consider including a truth table in the plan to make test fixture construction faster.
- The `_make_1m_df` fixture helper required careful parameterization to hit each case without ambiguity — a reference table of which flag combinations produce which cases would save iteration time.

**CLAUDE.md Updates:**
- None — existing patterns (lazy imports for optional deps, atomic write pattern, mocked Claude in tests) were all already covered.

---

## Conclusion

**Overall Assessment:** The session hypothesis system was implemented in full alignment with the plan. All 6 plan tasks were completed, all 22 tests pass, and the full test suite shows zero regressions. The two minor divergences (extracted `_assign_case` helper, 3 vs 1 exit sites for trade record annotation) were both improvements over the literal plan spec. The system correctly isolates LLM calls in the realtime path only, keeps backtest deterministic, and adds `matches_hypothesis` as a first-class field in both signal and trade records without touching `strategy_smt.py`.

**Alignment Score:** 9/10 — Full functional parity with plan; minor execution-level improvements only. Score reduced by 1 for the plan's ambiguous exit-site enumeration that required discovering a third site at implementation time.

**Ready for Production:** Yes (automated tests) / Deferred for live validation — `position.json` disk persistence of `matches_hypothesis` and the smoke test against real 1m/5m parquet files require a live IB-connected session to confirm.
