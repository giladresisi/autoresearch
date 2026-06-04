# Execution Report: Gate chop-zone market entries (o5-fallback + STP→MKT)

**Date:** 2026-06-04
**Plan:** `.agents/plans/gate-chop-zone-market-entries.md`
**Executor:** sequential (waved, single worktree)
**Outcome:** ⚠️ Partial — implementation + unit tests complete; broader-window/1s backtest and user merge-approval still pending (by design)

---

## Executive Summary

Gated the live SMT strategy's chop-zone whipsaw entries via two mechanisms: a directional STP→MKT downgrade fix (R1, live + backtest mirror kept in sync) and an o5-fallback headroom gate (R2) that rejects same-bar pseudo-confirmation market entries with no room to run. A general headroom gate on all entries (R3) was built, backtested, found net-negative (killed winners on 05-18/05-27), and scoped down to o5-only per explicit user decision. All 34 new unit tests pass; zero new regressions. The symptom day (06-04) flipped from −8 to +100 with two o5 whipsaws gated, and known winners stayed intact.

**Key Metrics:**
- **Tasks Completed:** R1 (1.1/1.2), helpers (1.3), R2 (2.2), R6 (3.1), backtest (3.2) done; R3-general implemented then removed; R4 (3.3) deferred → effectively 6/8 plan tasks landed, 1 reverted by design, 1 deferred
- **Tests Added:** 34 (all passing)
- **Test Pass Rate (new):** 34/34 (100%)
- **Full Suite:** 1052 passed / 27 pre-existing failures / 14 deselected (baseline was 1018 passed / same 27 failed → +34 new passing, 0 new regressions)
- **Files Modified:** 6 (2 production, 2 test, PROGRESS.md, plan file)
- **Lines Changed:** +786 / −296 (incl. feature.md churn); production = strategy.py +136/−~, pickmytrade.py +18/−~
- **Alignment Score:** 8/10

---

## Implementation Summary

**Wave 1 — Foundation**
- **R1 live** (`execution/pickmytrade.py` `place_entry`): replaced `_too_close` (±5 pt proximity band) with `_trigger_reached` — downgrade STP→MKT only when market reaches/passes the trigger (long `_current >= entry_price`, short `_current <= entry_price`). Kept the `_current > 0` guard; updated the `[PMT]` print message and explanatory comment.
- **R1 backtest mirror** (`strategy.py` `will_market_fill`): dropped the `± STP_MKT_PROXIMITY_PTS` slack → `bar_mid >= entry_price` / `<= entry_price`. `STP_MKT_PROXIMITY_PTS` retained as a documented live↔backtest contract anchor, annotated as no longer slackening the condition. The #55 Fix1/Fix2/Fix3 fixtures were adjusted so bars now actually reach the trigger.
- **Shared helpers** (`strategy.py`): `MIN_HEADROOM_PTS=10.0`, `_session_mids`, `_first_target_ahead`, `_nearest_opposing_level`, `_headroom_ok` (reward:risk ≥ 1 with a fixed floor; passes when no opposing level is ahead). Refactored the Section-3 inline mid-derivation (formerly ~`strategy.py:505–513`) to call `_session_mids` (single source of truth).

**Wave 2 — Core gate**
- **R2 o5-fallback headroom gate** (`strategy.py`): tracked `_conf_is_o5` (set when `_find_last_bar` returns None but `_o5_fallback` supplies a bar). The `_headroom_ok` gate fires ONLY on the o5 pseudo-conf path, at both emission points (market-entry just before `position["active"]` mutation; stop-entry just before `conf_bar_entry` persist), returning a side-effect-free `entry-gated` signal via a local `_gated()` helper.

**Wave 3 — Attribution + backtest**
- **R6 attribution** (`strategy.py`): firing entries carry `conf` (`"o5"`/`"normal"`); gated entries emit kind `entry-gated` with `gated` reason + `conf`. Safety verified: `live_orders.dispatch_order` log-only's unknown kinds (no broker order, no position mutation).
- **Backtest (3.2)**: ran base-code vs final-code on identical fresh data (locked baselines were stale). R3-general found net −401 → scoped to o5-only. Final: net +205 over changed days; winners intact.

---

## Divergences from Plan

### Divergence #1: R3 scoped from "general headroom gate" to "o5-only"
**Classification:** ✅ GOOD (evidence-driven scope reduction)
**Planned:** Task 2.1 — a general headroom gate on ALL entry paths (market + stop), with the o5 gate (R2) as an additional earlier check.
**Actual:** The general gate was implemented and backtested, found net-negative (−401 on the sample; killed legitimate breakout winners on 05-18 and 05-27 — exactly the anticipated risk in the plan's NOTES/risks). Per explicit user decision it was removed; the headroom gate is now guarded by `_conf_is_o5` at both call sites, so only o5 pseudo-conf entries are gated. Normal entries fire regardless of headroom.
**Reason:** The plan itself flagged this risk ("R3 risk of over-rejecting breakouts that run through a nearby mid") and made Task 3.2 a tuning/decision gate. Backtest evidence triggered the documented fallback.
**Root Cause:** Designed-in decision gate; backtest produced the answer.
**Impact:** Positive — preserves winners while still fixing the symptom. `_headroom_ok` + its 13 unit tests are retained.
**Justified:** Yes.

### Divergence #2: R4 (mid/equilibrium suppression) not implemented
**Classification:** ✅ GOOD (conditional task, condition not met)
**Planned:** Task 3.3 — CONDITIONAL: add R4 only if the 3.2 backtest shows residual mid-straddle whipsaws after R3.
**Actual:** Not implemented and documented as deferred. Since R3-general was already shown unhelpful and R4 is a blunter version of the same idea, building it was not justified.
**Reason:** Task was explicitly conditional; condition not satisfied.
**Root Cause:** Plan design (conditional task).
**Impact:** Neutral — no residual whipsaws observed in the sample warranting it.
**Justified:** Yes.

### Divergence #3: Stale locked baselines → fresh base-vs-final A/B instead
**Classification:** ⚠️ ENVIRONMENTAL
**Planned:** Task 3.2 — compare against committed baseline artifacts in `data/regression/<date>/`.
**Actual:** Those baselines were found STALE (underlying data backfilled after they were locked). The valid A/B was a fresh base-code run vs the final code on identical data.
**Reason:** Data was backfilled after baseline lock; comparing against stale artifacts would be invalid.
**Root Cause:** Environmental (data lifecycle), not a plan or code gap.
**Impact:** Neutral — A/B still valid, just recomputed; documented in plan NOTES.
**Justified:** Yes.

### Divergence #4: R1 live tests added to `test_pickmytrade_executor.py` (not `test_live_orders.py`)
**Classification:** ✅ GOOD (better test placement)
**Planned:** Task 1.1 — add R1 downgrade tests to `tests/test_live_orders.py` (or a new `tests/test_pickmytrade_downgrade.py`).
**Actual:** Added to `tests/test_pickmytrade_executor.py`, which already exercises the real `PickMyTradeExecutor.place_entry` and has the `_make_executor`/`_ok_response`/`_drain` harness the R1 tests need.
**Reason:** That file already had the exact fixtures; reusing them is cleaner than a parallel mock setup in test_live_orders.py.
**Root Cause:** Plan listed candidate locations; the better-fitting existing file was chosen.
**Impact:** Positive — tests exercise the real executor with minimal new scaffolding.
**Justified:** Yes.

---

## Test Results

**Tests Added:** 34 total, all passing.
**Test Execution (full suite, excluding `tests/test_ib_realtime.py` which does a real 20s sleep):** 1052 passed, 27 failed (pre-existing environmental), 14 deselected.
**Baseline before changes:** 1018 passed / same 27 failed. → +34 new passing, **0 new regressions**.

The 27 failures are all pre-existing and environmental: IB-gateway unreachable (test_ib_connection / test_live_run collection errors; time-of-day slippage + modify_stop_entry in test_pickmytrade_executor), API-key/network (test_hypothesis_smt / test_orchestrator_main), and test_smt_humanize / test_session_pipeline / test_check_session_parquets / test_automation_main.

---

## What was tested

**R1 — live STP→MKT downgrade** (`tests/test_pickmytrade_executor.py`, real `place_entry`):
- A long stop with market BELOW the trigger (the 00:30 case) is NOT downgraded — `order_type == "stop"`.
- A long stop with market AT/ABOVE the trigger downgrades to `order_type == "market"`.
- A short stop with market ABOVE the trigger stays a resting `"stop"`.
- A short stop with market AT/BELOW the trigger downgrades to `"market"` (boundary `==` downgrades).

**R1 — backtest mirror `will_market_fill`** (`tests/test_smt_strategy_v2.py`):
- A long stop whose `bar_mid` is below the trigger rests at the natural entry/stop (no re-anchor to bar_mid).
- A short stop whose `bar_mid` is above the trigger rests unchanged.
- Re-validated #55 Fix1/Fix2 (re-anchor stop to bar_mid ± risk) with fixtures adjusted so bar_mid now actually reaches the trigger; Fix2 floor (stop distance = `MKT_FILL_MIN_STOP_DISTANCE`) preserved.
- Far-resting and un-reached-with-too-close-stop cases still rest / reject as before.

**Headroom helpers** (pure functions, `tests/test_smt_strategy_v2.py`):
- `_session_mids` computes daily/weekly mids from level liquidities; a missing week bound yields `weekly_mid is None`.
- `_first_target_ahead` picks the nearest target ahead (min for up, max for down) and returns None when all targets are behind.
- `_nearest_opposing_level` picks the nearest of {daily_mid, weekly_mid, first target ahead} that lies ahead, ignores levels behind, and returns None when all are behind.
- `_headroom_ok` passes when no level is ahead (open road), rejects when headroom < risk, rejects when headroom < the `MIN_HEADROOM_PTS` floor, passes when headroom ≥ max(risk, floor), and is symmetric for short.

**R2/R3 scope — market & stop entry gating** (`tests/test_smt_strategy_v2.py`):
- A NORMAL (non-o5) long market entry with low headroom still fires (gate is o5-only) and carries `conf == "normal"`.
- A normal long market entry with ample headroom fires.
- A normal SHORT market entry with low headroom still fires (o5-only gate).
- A market entry with no liquidities at all fires (open road).
- A normal resting stop-entry with low headroom still fires (`new-stop-entry`, `conf == "normal"`); same with ample headroom.

**R2 — o5-fallback gate** (`tests/test_smt_strategy_v2.py`):
- An o5 pseudo-conf entry with headroom < max(risk, floor) is gated → `entry-gated`, `gated == "r2-o5-no-headroom"`, `conf == "o5"`, no position.
- An o5 entry with ample headroom still fires as `market-entry`, `conf == "o5"`.
- Existing `_O5_FALLBACK_DIST` guard still rejects (entry range too close → no conf bar → None).
- Existing `MAX_CONFIRMATION_BODY_PTS` guard still rejects oversized bodies → None.

**R6 — attribution** (`tests/test_smt_strategy_v2.py`):
- A non-o5 market entry is tagged `conf == "normal"`.
- An o5 entry is tagged `conf == "o5"`.
- A headroom-gated o5 entry emits kind `entry-gated` with reason `r2-o5-no-headroom`, `conf == "o5"`, and leaves `position["active"] == {}`.
- A resting stop-entry is tagged `conf == "normal"`.

**Anti-kill regression** (`tests/test_smt_strategy_v2.py`):
- A clear origin-coil breakout with ample headroom to the next opposing level still enters as `market-entry` (the gates must not suppress legitimate winners).

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `uv run python -c "import strategy, execution.pickmytrade"` | ✅ | Import-clean |
| 2 | `uv run pytest tests/test_smt_strategy_v2.py tests/test_pickmytrade_executor.py -q` | ✅ | 34 new tests pass; Fix1/2/3 re-validated |
| 3 | `uv run python regression.py --dates ... --no-plot` (1m, base vs final) | ✅ | net +205 over changed days; symptom day −8→+100 |
| 3 | 1s-mode regression | ⚠️ | NOT run (symptom is sub-minute; 1m used for speed) |
| 4 | `uv run pytest -q` (full suite, ex test_ib_realtime) | ✅ | 1052 passed / 27 pre-existing failed / 14 deselected; 0 new regressions |

---

## Challenges & Resolutions

**Challenge 1:** R1 tightening broke the #55 Fix1/Fix2/Fix3 fixtures.
- **Issue:** Those tests placed `bar_mid` within ±5 of entry but NOT past it; with the slack removed, `will_market_fill` no longer triggered, so the re-anchor path didn't run.
- **Root Cause:** The proximity slack was load-bearing in the old fixtures.
- **Resolution:** Adjusted each fixture so `bar_mid` reaches/passes the trigger (documented in each test docstring with the new geometry); added explicit "stays resting stop" tests for the un-reached side.
- **Time Lost:** ~minor.
- **Prevention:** When tightening a threshold, audit fixtures that sat in the removed slack band.

**Challenge 2:** General R3 gate killed winners.
- **Issue:** Backtest showed −401 net, with winners lost on 05-18/05-27.
- **Root Cause:** Legitimate breakouts run THROUGH a nearby mid; a generic headroom gate rejects them.
- **Resolution:** Per user decision, scoped the gate to o5-only (guard both call sites with `_conf_is_o5`).
- **Time Lost:** ~one backtest cycle (anticipated by the plan).
- **Prevention:** The plan's built-in 3.2 decision gate worked as intended.

**Challenge 3:** Stale locked regression baselines.
- **Issue:** `data/regression/` artifacts predated a data backfill, so A/B against them would be invalid.
- **Root Cause:** Data lifecycle (backfill after lock).
- **Resolution:** Ran fresh base-code vs final-code on identical data.
- **Prevention:** Re-lock baselines after any backfill, or always A/B base-vs-final on the same snapshot.

---

## Files Modified

**Production (2 files):**
- `strategy.py` — `MIN_HEADROOM_PTS` constant; `_session_mids`/`_first_target_ahead`/`_nearest_opposing_level`/`_headroom_ok` helpers; `_conf_is_o5` tracking + `_gated()` local; o5-only headroom gate at both emission points; `conf` tags on firing entries; R1 `will_market_fill` tightening; Section-3 mid-derivation refactored to `_session_mids` (+136 / −~)
- `execution/pickmytrade.py` — `_too_close` → `_trigger_reached` directional downgrade; updated `[PMT]` print + comments (+18 / −~)

**Tests (2 files):**
- `tests/test_smt_strategy_v2.py` — `write_daily`/`_levels` fixtures; 13 helper + 2 R1 will_market_fill + 10 gate + 4 R6 + 1 anti-kill tests; Fix1/2/3 fixtures adjusted (+427 / −~)
- `tests/test_pickmytrade_executor.py` — 4 R1 live-downgrade tests + `_stop_signal` helper (+54)

**Docs (2 files):**
- `PROGRESS.md` — feature planning entry (+9)
- `.agents/plans/gate-chop-zone-market-entries.md` — acceptance criteria + NOTES updated to reflect the o5-only decision and backtest outcome
- (`feature.md` — large churn, pre-existing working-copy state)

**Total:** +786 / −296 across all files.

---

## Success Criteria Met

- [x] R1 live — directional downgrade (00:30 case stays a resting stop)
- [x] R1 backtest — `will_market_fill` keys off the trigger; proximity slack removed; Contract 1 sync
- [x] Helpers — `_session_mids` / `_nearest_opposing_level` / `_headroom_ok` per spec
- [x] R3 — scoped to o5-only (general gate removed after backtest; helper + tests retained)
- [x] R2 — o5-fallback path gated by explicit early headroom check; existing guards respected
- [x] R6 — `conf` on firing entries; `entry-gated` kind with `gated` reason; `position["active"] == {}`
- [x] Edge cases — missing mids no-crash; resting-stop fills not gated; R1 `==` boundary downgrades
- [x] `entry-gated` is log-only in `dispatch_order` (verified)
- [x] Fix1/Fix2/Fix3 re-validated under R1; origin-coil anti-kill test passes
- [x] Backtest before/after produced; `MIN_HEADROOM_PTS` kept at 10.0
- [x] No regressions vs baseline suite count
- [ ] R4 — deferred (condition not met; documented)
- [ ] Broader-window backtest — pending (only ~17 days / 4 changed sampled)
- [ ] 1s-mode backtest — not run (symptom is sub-minute)
- [ ] User merge-approval — pending (changes UNSTAGED, not merged)

---

## Recommendations for Future

**Plan Improvements:**
- The conditional/decision-gate structure (Task 3.2 → scope R3, Task 3.3 conditional R4) worked well; keep this pattern for risk-flagged gating features.
- Add an explicit "re-lock baselines if data backfilled" precondition to any task that A/Bs against `data/regression/`.

**Process Improvements:**
- For sub-minute symptoms, run the 1s-mode regression before declaring the backtest conclusion (1m was used for speed but the symptom resolution is 1s).
- Sample a broader date window (weeks, not ~4 changed days) before drawing P&L conclusions.

**CLAUDE.md Updates:**
- Note that this repo's `data/regression/` baselines can go stale on backfill — prefer base-vs-final on an identical snapshot.

---

## Conclusion

**Overall Assessment:** The core symptom (chop-zone whipsaw entries) is addressed by R1 (directional STP→MKT) and R2 (o5-only headroom gate), with full attribution (R6) and a clean unit-test surface (34/34, zero new regressions). The plan's own decision gate correctly steered R3 from a winner-killing general gate down to an o5-only scope, and R4 was rightly deferred. The remaining open items (broader-window + 1s backtest, user merge-approval) are gating the MERGE, not the implementation — and were explicitly out of scope for code completion per the plan.

**Alignment Score:** 8/10 — implementation faithful to plan; the two largest divergences (R3 scope, R4 deferral) were anticipated and authorized by the plan's own conditional structure. Score held below 9 only because the validation is incomplete (1s + broader-window backtest still pending) and the P&L sample is small.

**Ready for Production:** No — changes are intentionally UNSTAGED and NOT merged, gated on the broader-window/1s backtest and explicit user merge-approval.
