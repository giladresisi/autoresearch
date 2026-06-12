# Execution Report: SMT V2 — three strategy updates

**Date:** 2026-06-09
**Plan:** .agents/plans/smt-v2-three-updates.md
**Executor:** sequential (single context, self-contained)
**Outcome:** ✅ Success

---

## Executive Summary

Implemented all three locked changes from the plan: (1) removed the inert 4hr-FVG detection/
plumbing while keeping the 4hr BOS/CHoCH score and the 1hr FVG path; (2) added a wall-clock
15:30 ET new-entry cutoff internal to the PMT executor; (3) added a dynamic, per-hypothesis
`cautious_dist_shrinks` counter that tightens both cautious max-distance thresholds 15% per
failed entry, floored at 40, byte-identical at shrinks=0. Req 4 was correctly left as no-code.
The full suite shows zero new failures vs the recorded baseline.

**Key Metrics:**
- **Tasks Completed:** 3/3 (100%) — Req 4 intentionally no-code
- **Tests Added:** 12 (6 PMT cutoff + 6 cautious-shrink), plus 3 existing tests extended
- **Test Pass Rate (plan-touched files):** 100% of new/changed tests pass
- **Files Modified:** 13 (6 production, 7 test)
- **Lines Changed (production):** +57 / −30
- **Execution Time:** ~1 session
- **Alignment Score:** 10/10

---

## Implementation Summary

### Change 1 — Remove 4hr FVGs (keep 4hr BOS/CHoCH)
- `daily.py`: removed the `hist_4hr` param from `run_daily_fixed` and the
  `fvgs_4hr = _detect_fvgs(hist_4hr,…); liquidities.extend(fvgs_4hr)` lines + docstring.
- `session_pipeline.py`: removed `_fvg_4hr` / `_fvg_done_4hr` members, the `_fvg_4hr_full`
  seeding block, the `("4h", …)` tuple in `_extend_fvg_frames`, and the 4th positional arg
  to `run_daily_fixed`. `_hist_4hr` (BOS frame) preserved.
- VERIFIED before deletion: in `hypothesis.py` and `live_orders.py` the 4hr frame feeds ONLY
  the BOS/CHoCH score (`b4hr` / `bos_score_4hr`, weight 0.65), never an FVG path — so those
  `hist_4hr` params correctly stay. No production reference to `_fvg_4hr`/`fvgs_4hr` remains.

### Change 2 — 15:30 ET new-entry cutoff (PMT executor)
- `execution/pickmytrade.py`: added `_NEW_ENTRY_CUTOFF = datetime.time(15, 30)` near `_ET`;
  hoisted `_now_et_time = datetime.datetime.now(_ET).time()` and added a strictly-after gate
  immediately following the existing `is_entry_allowed` block. On block: logs
  `"[PMT] new entry blocked after 15:30 ET"`, sets `_entry_is_live=False`, returns the same
  `status="blocked"` FillRecord shape. No HTTP submit. Wall-clock, not bar time.

### Change 3 — Dynamic cautious max-dist thresholds
- `smt_state.py`: `"cautious_dist_shrinks": 0` added to `DEFAULT_POSITION`.
- `hypothesis.py`: `CAUTIOUS_DIST_SHRINK_PCT = 0.15`; `compute_cautious_prices` gained
  `dist_shrinks: int = 0` and computes floored effective maxes used in all 5 threshold checks;
  `recompute_cautious_for_fill` gained `dist_shrinks` passthrough; formation site reads the
  loaded position's counter; skip_position_reset branch resets it.
- `strategy.py`: increment at the stop-out site (in lockstep with `failed_entries`); reset in
  both `reset_position_for_session` and `reset_position_for_new_hypothesis`; both
  `recompute_cautious_for_fill` call sites pass the counter.
- `session_pipeline.py`: increment at the same-bar-stop-check stop-out site. The
  liquidity-sweep decrement was intentionally left alone (separate counter keyed off
  increments only).

---

## Divergences from Plan

### Divergence #1: Plan named function `run_daily` / line `~L274-275`; actual is `run_daily_fixed`
**Classification:** ⚠️ ENVIRONMENTAL (stale line refs in plan prose)
**Planned:** "drop the `hist_4hr` param … in `daily.py` (~L274-275)"; "L902" for the session
increment; recompute sites "L284/405/497".
**Actual:** Function is `run_daily_fixed`; the `failed_entries` increment in session_pipeline
is at L894 (not 902); the 1228 reset shifted slightly. Edits applied at the true locations.
**Reason:** Plan line numbers were approximate; the surrounding code text matched exactly.
**Impact:** Neutral — all edits landed on the intended logical sites.
**Justified:** Yes.

### Divergence #2: Test call-site churn beyond the two files the plan named
**Classification:** ✅ GOOD (required by the signature change)
**Planned:** Update `test_session_pipeline.py` and `test_smt_daily.py`.
**Actual:** Also updated `test_smt_dispatch_order.py` (2 `fake_run_daily_fixed` signatures)
because they mock `run_daily_fixed` with the old 5-arg positional shape; and
`test_smt_state.py` / `test_smt_strategy_v2.py` for the new `cautious_dist_shrinks` field.
**Reason:** Removing the `hist_4hr` param and adding the new default field necessarily touch
every exhaustive-shape assertion / positional mock.
**Impact:** Positive — keeps the suite consistent; no test was weakened.
**Justified:** Yes.

### Divergence #3: Change-3 counter-lifecycle test placed in test_smt_strategy_v2.py
**Classification:** ✅ GOOD
**Planned:** TDD bullet "increment on stop-out … via the existing reset paths" in
`test_smt_hypothesis.py`.
**Actual:** The stop-out increment assertion was added to the existing
`test_in_position_stop_crossed_emits_stopped_out_and_increments_failed` in
`test_smt_strategy_v2.py` (the canonical home of the stop-out path, with proper bar fixtures);
reset coverage added as two dedicated tests in `test_smt_hypothesis.py`.
**Reason:** `test_smt_hypothesis.py` lacks the strategy bar/position fixtures; reusing the
existing stop-out test is more robust than reconstructing them.
**Impact:** Positive — exercises the real increment path. **Justified:** Yes.

---

## Test Results

**Tests Added (12):**
- PMT: allowed before cutoff (15:29), blocked after (15:31 market), blocked after (15:31 stop),
  allowed exactly at 15:30:00 boundary, close allowed after cutoff, update_stop_loss allowed
  after cutoff.
- Cautious: shrinks=0 unchanged (+ default-kwarg equality), shrinks=1 excludes far level,
  shrinks=1 includes within-shrunk-max level, large-shrink clamps to 40, reset by session
  helper, reset by new-hypothesis helper, recompute honors dist_shrinks.

**Tests Extended (3):** stop-out increment now asserts `cautious_dist_shrinks==1`;
none→up transition reset now asserts `cautious_dist_shrinks==0`; position roundtrip includes
the new field.

**Test Execution:**
- Plan-touched files (hypothesis, strategy_v2, strategy, state, session_pipeline, daily,
  dispatch_order, v2_dispatcher, live_orders, state_prefix): all green.
- PMT cutoff subset: 6/6 pass.
- Full suite (minus known-hanging IB test): failure set byte-identical to baseline
  (28 pre-existing, environmental), 1193 passed.

---

## What was tested

- New market entry at 15:29 ET is submitted (status filled, one HTTP post, `_entry_is_live` true).
- New market entry at 15:31 ET is blocked (status blocked, zero HTTP posts, `_entry_is_live` false).
- New stop entry at 15:31 ET is blocked with `order_type=="stop"` preserved.
- Entry exactly at 15:30:00 ET is still allowed (strictly-after semantics).
- `place_close` and `update_stop_loss` still post at 15:31 ET (the cutoff does not gate them).
- `compute_cautious_prices(dist_shrinks=0)` reproduces the pre-change ladder and equals the default-kwarg call.
- `dist_shrinks=1` shrinks the secondary max to 127.5, excluding a 140pt level that qualified at 0.
- A 120pt level still qualifies at `dist_shrinks=1` (threshold shrank, didn't vanish).
- Large `dist_shrinks` clamps both maxes to 40 and never below.
- A stop-out increments `cautious_dist_shrinks` in lockstep with `failed_entries`.
- `reset_position_for_session`, `reset_position_for_new_hypothesis`, and the none→direction
  transition all reset `cautious_dist_shrinks` to 0.
- `recompute_cautious_for_fill` threads `dist_shrinks` into the recomputed ladder and still
  no-ops under the manual lock.
- A live `run_daily_fixed` writes the 1hr FVG and no 4hr FVG; the 1hr live-FVG extend + daily.json
  landing tests still pass; `bos_score_4hr` is still produced.

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `python -c "import daily, session_pipeline, hypothesis, live_orders"` | ✅ | imports clean after Change 1 |
| 2 | `pytest tests/test_pickmytrade_executor.py -k cutoff` | ✅ | 6/6 cutoff tests pass |
| 3 | `pytest tests/test_smt_hypothesis.py tests/test_smt_strategy_v2.py -k "shrink or cautious or failed_entries"` | ✅ | 13/13 pass |
| 4 | full suite vs baseline (`diff` of sorted FAILED lists) | ✅ | empty diff — zero new failures |

---

## Challenges & Resolutions

**Challenge 1:** Monkeypatching the PMT module clock caused `RecursionError`.
- **Issue:** `monkeypatch.setattr(_pmt_mod.datetime, "datetime", Frozen)` mutates the shared
  global `datetime` module, so the fallback `_dt.datetime.now(tz)` recursed into the patched class.
- **Root Cause:** `_pmt_mod.datetime` IS the global datetime module object.
- **Resolution:** Captured `_real_datetime = _dt.datetime` BEFORE patching and subclass/fall back to it.
- **Prevention:** When patching `module.datetime.datetime`, always bind the original class first.

**Challenge 2:** Plan line numbers didn't match (`run_daily` vs `run_daily_fixed`, L902 vs L894).
- **Resolution:** Located edits by surrounding code text, which matched exactly; verified each
  via grep before editing.

---

## Files Modified

**Production (6 files, +57/−30):**
- `daily.py` — drop 4hr-FVG param + detect/extend lines (−5)
- `execution/pickmytrade.py` — 15:30 ET cutoff constant + guard (+21/−1)
- `hypothesis.py` — shrink factor, param threading, reset (+31/−8)
- `session_pipeline.py` — remove 4hr-FVG state/seeding/loop; add shrink increment (+20/−14)
- `smt_state.py` — new DEFAULT_POSITION field (+1)
- `strategy.py` — increment + 2 resets + 2 recompute passthroughs (+9/−2)

**Tests (7 files):**
- `tests/test_pickmytrade_executor.py` — 6 cutoff tests + clock helper (+101)
- `tests/test_smt_hypothesis.py` — 6 shrink/reset tests + 2 assertions (+92)
- `tests/test_smt_daily.py` — drop 4hr-FVG test + 4hr scaffolding, update 14 call sites (−112 net)
- `tests/test_session_pipeline.py` — update Test 4, drop 4hr extend test + helper arg (−37 net)
- `tests/test_smt_dispatch_order.py` — 2 fake signatures (−4)
- `tests/test_smt_state.py` — roundtrip new field (+1)
- `tests/test_smt_strategy_v2.py` — stop-out shrink assertion (+1)

---

## Success Criteria Met

- [x] C1: no 4hr FVG in daily.json; 1hr FVGs intact; `bos_score_4hr` produced; 4hr-FVG tests removed
- [x] C2: entries blocked strictly after 15:30 ET; closes/stop-mods allowed; scope internal to PMT
- [x] C3: 15% shrink/tier floored at 40; increment-only counter; reset at all 3 points; shrinks=0 identical
- [x] Req 4: no code (effect pre-existing)
- [x] Zero new test failures vs baseline

---

## Recommendations for Future

**Plan Improvements:**
- Pin line refs to function/symbol names rather than line numbers (drifted by ~8 lines here).

**Process Improvements:**
- The 28 pre-existing failures are environmental (live IB connection on the machine). Consider
  marking the IB-connection and slippage-config tests `integration` so a clean baseline is
  reproducible without a live gateway.

**CLAUDE.md Updates:**
- Document the "capture the real class before monkeypatching `module.datetime.datetime`" pattern
  for tests that freeze wall-clock time.

---

## Conclusion

**Overall Assessment:** All three changes implemented exactly to the locked decisions, with the
mandatory verification steps (4hr BOS kept, 1hr FVGs intact, shrinks=0 byte-identical) confirmed
by tests and probes. Divergences were limited to line-number drift and necessary test-mock
updates. Full suite shows zero new failures.
**Alignment Score:** 10/10 — every touchpoint and acceptance criterion satisfied.
**Ready for Production:** Yes — pending the user's own commit/review (changes left unstaged).
