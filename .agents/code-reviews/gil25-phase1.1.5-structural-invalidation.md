# Code Review — GIL-25 Phase 1.1.5 structural invalidation (smt-level-invalidation)

Branch: autoresearch/smt-level-invalidation (working tree vs HEAD). SMT layer is SHADOW.

**Stats:**
- Files Modified: 9 (4 source + 5 tests... actually 4 source + 6 tests)
- Files Added: 2 (tests/test_regression_carry_routing.py, .agents/plans/5.smt-structural-invalidation-phase1.1.5.md)
- Source files: smt_detect.py (+190/-...), smt_state.py, backtest_smt.py, regression.py
- New lines: ~540 across all files
- All changed test files pass (136 passed, 6 skipped); relevance tests pass (56).

---

## Findings

```
severity: high
file: smt_detect.py
line: 797-820
issue: Same-batch mutual supersession marks BOTH freshly-emitted same-direction records terminal (superseded=True), dropping both from the active set.
detail: The supersession pass iterates `for r in records` (this-batch fresh fires) and, for each, marks every OTHER same-rec_type same-direction fired state superseded. The only self-guard is `skey == fresh_key`. When TWO same-direction SMTs fire in the SAME bar/batch (e.g. a down-spike sweeps prev1_day_low AND prev2_day_low divergently on one 1m bar — both are real `long` level names), record A's pass supersedes B's state and record B's pass supersedes A's state, so BOTH end with superseded=True. Via smt_status (smt_detect.py:1083-1088) both then return "invalidated", and the active-set filter in session_pipeline.py:2141-2143 (`collapsed_relevance(...) == "unfulfilled"`) drops BOTH. The intended design (per docstring + tests) is "keep only the FRESHEST same-direction fired record" — here the freshest survivors are wrongly eliminated too. This alters the active set the change is meant to feed, so it can change downstream relevance/hypothesis behavior. Confirmed reproducible:
  records: ['prev1_day_low', 'prev2_day_low']
  prev1_day_low|long|wick fired=True superseded=True
  prev2_day_low|long|wick fired=True superseded=True
The existing supersession tests only exercise the cross-BATCH case (t1 then t2), so they do not catch this.
suggestion: Exclude records fired in the CURRENT batch from being superseded. E.g. build the set of this-batch fresh keys first (`fresh_keys = {_record_key(r) for r in records}`) and skip any candidate `skey in fresh_keys` (not just `skey == fresh_key`). That preserves "freshest survives" when multiple same-direction SMTs fire on one bar. Add a regression test for two same-direction fires in a single batch asserting neither is superseded.
```

```
severity: low
file: smt_detect.py
line: 1064-1067
issue: Stale docstring paragraph in smt_status referencing the removed adverse-run producer.
detail: The paragraph "The `invalidated` flag is produced by the SMT V2 Part A producer change. Until that flag is present ... NO drop, exactly the legacy behavior." now describes a mechanism that Phase 1.1.5 removed (st["invalidated"] is no longer produced by detection). The code is correct (it still reads the legacy flag for forward/backward compat), but the prose is misleading about where the flag comes from.
suggestion: Reword to note `invalidated` is now legacy-only (never produced post-1.1.5) and the live terminal producers are `superseded`/`retired_depleted`.
```

---

## Verified-correct items (no action)

- Depletion backstop sign/direction in `revalidate_and_filter_pending` (smt_detect.py:348-351): long swept `*_low` → `_wl <= _lvl - _dep`; short swept `*_high` → `_wh >= _lvl + _dep`. Mirrors the live latch (lines 748-757). Correct. `e["price"]` absent → skipped (keep). Float coercion guarded (no raise).
- `pending_smt_terminal` reduction to 2-state (fulfilled/unfulfilled): measured from fire_price; unknown tier falls back via `_fulfill_pts` (no raise). Total.
- Depletion-latch retirement (smt_detect.py:758-779): uses loop-local `direction` (correct swept-side mapping), marks both wick/body fired-open records, iterates a fixed `("wick","body")` tuple (no dict-mutation-during-iteration), `__level_retirements__` appended via setdefault. Idempotent (only on latch flip). Verified by tests.
- Supersession pass iterates `list(state.keys())` snapshot — safe from the `__supersessions__` setdefault mutation.
- Flag clearing on fire / dynamic re-arm / fixed re-arm / opposite-direction post-pass re-arm: `superseded` and `retired_depleted` are reset in all four sites (lines 692-693, 699-700, 726-727, 844-845) and initialized in the state template (636-637). No stale-flag carryover on re-arm.
- Change B (smt_state.set_in_memory_mode reset_pending; backtest_smt threading; regression contiguous-carry routing + bdate_range + _is_next_bday): default-True path is byte-identical; `_PENDING_STORE` preserved only when reset_pending=False while per-run `_STORE`/hyp cache still reset; `_is_next_bday` total (unparseable → False → no carry). Carry-routing tests are hermetic and thorough (Fri→Mon, gaps, weekend filtering, threading spy).
- Detection functions remain total (no new raises). No new `print`/stdout in smt_detect.py or smt_state.py (the pre-existing prints in regression.py/backtest_smt.py are CLI/backtest reporting paths, not production trading paths, and are untouched).

## Pre-existing failures
- Two known out-of-scope failing tests in tests/test_session_pipeline.py — intentionally excluded from this review per scope.
