# Execution Report: SMT-v2 Phase 1 — Decouple Active-Position Management

**Date:** 2026-06-10
**Plan:** `.agents/plans/smt-v2-decouple-active-position.md`
**Executor:** sequential (single-context, wave-ordered)
**Outcome:** ✅ Success

---

## Executive Summary

Phase 1 of the 3-phase SMT-v2 redesign decouples an open trade's management from the live, mutable hypothesis. At every fill site the trade's management direction and cautious ladder are frozen into `position["active"]` (Contract A); `trend.py` Step-3 now manages off that frozen snapshot, and `strategy.py`'s automatic direction-mismatch force-close is removed so a hypothesis flip/none no longer flattens a live trade — cautious targets decide the exit. The common (non-flipping) trade is byte-equivalent to prior behavior, guarded by a regression test.

**Key Metrics:**
- **Tasks Completed:** 7/7 (100%)
- **Tests Added:** 22 net-new passing (16 in the new file + 6 across updated files)
- **Test Pass Rate:** plan files 193/193 (100%); full suite 1284 passed, 24 pre-existing fail (zero net-new)
- **Files Modified:** 4 production + 3 test, 1 new test file
- **Lines Changed:** production +151/-18; tests +688/-36 (incl. new file)
- **Alignment Score:** 10/10

---

## Implementation Summary

**Wave 1 — Freeze at fill + remove force-close:**
- `smt_state.freeze_active_mgmt(active, direction, hypothesis)` — pure, None-tolerant helper: normalizes long/short→up/down `mgmt_direction`, copies the four `cautious_*` ladder fields, derives `backing_tier` (`week_*`→week else day). `DEFAULT_POSITION` documents the frozen active-dict shape.
- `strategy.py` — freeze called POST-recompute at the stop-entry fill and market-entry paths; Section 3.1 mismatch block reduced to a single `return None` (manual + automatic both no-op); 2.1 pending-stop-entry cancel untouched.
- `live_orders.py` — freeze at `_register_downgraded_fill` (reloads hypothesis after `_recompute_cautious_at_fill`) and `place_market_entry` (loads live hypothesis; no recompute on the manual path, documented). `stop_entry_filled` documented as no-freeze.

**Wave 2 — Re-key trend.py Step-3:**
- A frozen-snapshot resolver placed before the Step-2 gates: `mgmt_direction`, `f_initial_raw`, `f_secondary_raw`, `f_lv1/f_lv2`, `f_cr1/f_cr2`, `f_ath_secondary`, `f_*mid_cross_guard`, all with back-compat fallback to the legacy `active["direction"]` and live hypothesis ladder.
- Inside `if active:` the live symbols are shadowed to the frozen variants before the closures/break-checks, so all of Step-3 manages off the frozen snapshot.
- `none` early-return and global-trend `trend-broken` reset both gated with `and not active` so they no longer short-circuit or mutate state under an open trade. Step-4 flat scan untouched.

**Wave 3 — Tests:** new `tests/test_smt_decouple_active.py`; `_active_position()` helper extended with frozen fields + byte-equivalence/flip regression in `test_smt_trend.py`; mismatch tests rewritten to assert preservation + freeze test in `test_smt_strategy_v2.py`; `freeze_active_mgmt` helper tests in `test_smt_state.py`.

---

## Divergences from Plan

### Divergence #1: trend.py `active`/resolver computed before the Step-2 gates (not "after L307")

**Classification:** ✅ GOOD
**Planned:** Plan placed the resolver "right after `active = position.get("active", {})` (≈L307)", which sits below the `none`/global-trend gates.
**Actual:** `active` and the resolver were moved up to just before the Step-2 gates (the duplicate later assignment removed).
**Reason:** The gates at L325/L336 must test `not active`, which requires `active` to be in scope before them.
**Root Cause:** Plan ordering gap — the gating edits the plan itself prescribes (`and not active`) require `active` earlier than the original line.
**Impact:** Positive — single source of `active`; gates read cleanly. Behavior unchanged.
**Justified:** Yes.

### Divergence #2: `test_fill_freezes_mgmt_fields` `backing_tier` assertion derives from the post-recompute level

**Classification:** ⚠️ ENVIRONMENTAL
**Planned:** Implicit expectation that a `week_high` secondary yields `backing_tier="week"`.
**Actual:** Asserted `backing_tier` against the *post-recompute* frozen `cautious_secondary_level` (the recompute may re-anchor the level).
**Reason:** The locked capture-timing decision freezes the post-recompute (fill-anchored) ladder, whose secondary level can differ from the pre-fill hypothesis level.
**Root Cause:** Correct application of the plan's capture-timing decision.
**Impact:** Neutral — the assertion verifies frozen == post-recompute hypothesis, which is the contract.
**Justified:** Yes.

---

## Test Results

**Test Execution:**
- `tests/test_smt_decouple_active.py tests/test_smt_trend.py tests/test_smt_strategy_v2.py tests/test_live_orders.py tests/test_smt_state.py` → 193 passed.
- `uv run python -m pytest tests/ -q` (integration excluded by addopts) → 1284 passed, 6 skipped, 24 failed (all pre-existing, unrelated files), 14 deselected.

**Pass Rate:** plan-touched files 193/193 (100%). Zero net-new failures vs the pre-edit baseline (which also had the same 24 failures).

---

## What was tested

- `freeze_active_mgmt` normalizes long→up / short→down, copies the four ladder fields, and derives `backing_tier`.
- `freeze_active_mgmt` is None-tolerant — missing/None hypothesis cautious fields store `""` and default `backing_tier="day"`.
- `backing_tier` derivation: `week_*`→week, `day_*`→day, `""`→day.
- A stop-entry fill writes all six frozen Contract-A fields, ladder == post-recompute hypothesis.
- A market-entry fill writes the frozen fields.
- `_register_downgraded_fill` and `place_market_entry` freeze the fields (source honored, incl. manual).
- trend.py manages an UP trade correctly when the live hypothesis is flipped to down (frozen-side arm).
- trend.py manages a DOWN trade when the live hypothesis is none (none early-return skipped).
- Initial-cautious break check fires on the frozen direction's comparator after a flip.
- Global-trend `trend-broken` reset is skipped while a position is active; the live direction is untouched.
- ATH-secondary break-even path keys off the frozen `cautious_secondary_level`.
- Frozen ladder is unchanged after a later `recompute_cautious_for_fill` and after a live-hypothesis flip+ladder rewrite.
- Pending stop-entry is cancelled on direction-changed and direction-none (cancel preserved).
- Automatic and manual positions are preserved (no market-close) on a direction mismatch.
- Legacy `active` lacking the frozen fields is managed via the back-compat fallback without crashing.
- Normal-trade management is byte-equivalent to baseline; flipping the live hypothesis yields identical signals.

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `python -c "import smt_state, strategy, live_orders, trend, hypothesis, session_pipeline"` | ✅ | Imports OK |
| 2 | `pytest tests/test_smt_decouple_active.py tests/test_smt_state.py -q` | ✅ | 50 passed |
| 2 | `pytest tests/test_smt_strategy_v2.py tests/test_smt_trend.py tests/test_live_orders.py -q` | ✅ | all passed |
| 3 | `pytest tests/ -q` | ✅ | 1284 passed; 24 pre-existing fail (unrelated); 0 net-new |

---

## Challenges & Resolutions

**Challenge 1:** Extending `_active_position()` to write frozen ladder fields broke tests that relied on the live-hypothesis ladder.
- **Issue:** The resolver treats `active.get("cautious_initial", fallback)` — an explicit `""` is a present value, suppressing the live-hypothesis fallback.
- **Root Cause:** Writing `""` by default changed frozen==live to frozen-empty for tests that set only the hypothesis ladder.
- **Resolution:** Made the helper accept the ladder and had `_setup_active_cautious_no`/`_setup_cautious_yes` + the signal-shape test pass matching values, so frozen==live.
- **Prevention:** When a resolver distinguishes "absent" from "empty", test helpers must mirror real fill values, not blanket empties.

**Challenge 2:** Market-entry freeze test initially returned None.
- **Issue:** Strategy's market-entry path requires a 5m-boundary timestamp; the new file's `NOW` was 10:01, not 10:05.
- **Resolution:** Imported `NOW` (10:05) from `test_smt_strategy_v2` for that path; also isolated `ACT_GLOBAL_DIR` so strategy's `load_global` reads the test's global.
- **Prevention:** Reuse the donor test module's time/fixtures when driving the same code path.

---

## Files Modified

**Production (4 files, +151/-18):**
- `smt_state.py` — `freeze_active_mgmt` helper + `DEFAULT_POSITION` active-dict doc (+44/-0)
- `strategy.py` — freeze at both fills; mismatch close → `return None` (+19/-12)
- `live_orders.py` — freeze at downgrade + market-entry; no-freeze doc on `stop_entry_filled` (+16/-0)
- `trend.py` — Step-3 frozen resolver + re-key; none/global-trend gates skipped while active (+72/-6)

**Tests (3 modified + 1 new, +688/-36):**
- `tests/test_smt_decouple_active.py` — NEW, 451 lines, 16 tests
- `tests/test_smt_trend.py` — frozen helper + byte-equivalence/flip regression (+110/-8)
- `tests/test_smt_strategy_v2.py` — mismatch tests → preserved + freeze test (+70/-22)
- `tests/test_smt_state.py` — `freeze_active_mgmt` helper tests (+57/-0)

**Untouched (confirmed):** `hypothesis.py`, `session_pipeline.py` — `recompute_cautious_for_fill` already mutates only `hypothesis`; no edit needed.

---

## Success Criteria Met

- [x] AC1 Freeze at all four fill paths
- [x] AC2 trend.py manages off frozen snapshot; none/global-trend skipped while active
- [x] AC3 No force-close on mismatch (automatic)
- [x] AC4 Manual position untouched
- [x] AC5 Pending-stop-entry cancel preserved
- [x] AC6 Frozen ladder immutable
- [x] AC7 Correct-side management after flip
- [x] AC8 Normal-case byte-equivalence
- [x] AC9 Back-compat fallback
- [x] AC10 Production silence + scope
- [x] AC11 Suite green; named tests per coverage table

---

## Recommendations for Future

**Plan Improvements:**
- Pre-place the `active`/resolver computation ahead of the Step-2 gates in the plan text (the gating edits already require it).
- Note in the freeze tests that `backing_tier` follows the post-recompute level, not the pre-fill hypothesis level.

**Process Improvements:**
- When a resolver distinguishes absent-vs-empty keys, call it out so test helpers don't blanket-write empties.

**CLAUDE.md Updates:**
- None required.

---

## Conclusion

**Overall Assessment:** Phase 1 is complete and independently shippable. The decoupling is implemented exactly per Contract A, the highest-risk trend.py re-key is byte-equivalent on the common path (regression-guarded), and the behavior change (no force-close on mismatch) is covered by rewritten tests. Code review found no genuine issues; all 11 acceptance criteria PASS.
**Alignment Score:** 10/10 — all tasks/ACs met; the two divergences are a beneficial ordering fix and a correct application of the plan's capture-timing decision.
**Ready for Production:** Yes — pending the user's own decision to ship; changes left unstaged per instructions.

**Related artifacts:**
- Code review: `.agents/code-reviews/smt-v2-decouple-active-position.md`
- Acceptance validation: `.agents/acceptance-validations/smt-v2-decouple-active-position-validation.md`
