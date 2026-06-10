# Execution Report: SMT V2 — SMT & SMT-Fill Detection Redesign

**Date:** 2026-06-09
**Plan:** .agents/plans/smt-v2-detection-redesign.md
**Executor:** sequential (single-session, wave-ordered)
**Outcome:** ✅ Success

---

## Executive Summary

Implemented a new pure detection engine (`smt_detect.py`) — regular (wick) SMTs, hidden
(body) SMTs at 15m/30m, and SMT-fills against paired 1hr FVGs — plus an `SmtBuffer`
(per-minute + 5m accumulator) and a `PendingSmtWatch` reference consumer, wired
additively into `SessionPipeline.on_1m_bar`. `daily.json` gained an additive
`liquidities_mes` block (MNQ `liquidities` untouched) and a new `smts.json` store
persists edge/re-arm state + the retained set. All 43 new tests pass; the MNQ
liquidity output and the emitted-event list are regression-verified identical to baseline.

**Key Metrics:**
- **Tasks Completed:** 6/6 (100%)
- **Tests Added:** 43 (31 unit in test_smt_detect.py, 12 integration in test_session_pipeline.py, 2 store in test_smt_state.py)
- **Test Pass Rate:** 115/115 targeted (100%); full suite 1187 passed, 24 pre-existing failures unchanged
- **Files Modified:** 5 (+ 2 new: smt_detect.py, tests/test_smt_detect.py)
- **Lines Changed:** +734 / -45 (tracked files) + ~520 new smt_detect.py
- **Execution Time:** ~1 session
- **Alignment Score:** 9/10

---

## Implementation Summary

**Wave 1 — Foundation**
- *Task 1.1 (smt_detect.py):* Module constants (MIN_REARM_OPP_MOVE_PTS_MNQ/MES,
  WATCH_CONFIRM_PTS_MNQ/MES, HIDDEN_TFS, _SESSION_WINDOW). `eligible_levels` (completed-
  session + running day/week). Shared `_detect_level_smts` engine (wick & body) with the
  §3.1 dual re-arm state machine + batch opposite-SMT post-pass. `detect_fill_smts` with
  side-aware `_fvg_progress`, Fill-A/Fill-B + Fill-B-follow-on. `SmtBuffer` and
  `PendingSmtWatch` (copy-preserve-invalidate, to_dict/from_dict).
- *Task 1.2 (smt_state.py):* `DEFAULT_DAILY["liquidities_mes"]=[]`, `DEFAULT_SMTS`,
  `_smts_path`, `load_smts`/`save_smts`, `smts.json` added to `final_snapshot`.
- *Task 1.3 (session_pipeline.py):* Refactored `_update_dynamic_liquidities` into an
  instrument-generic `_update_instrument_liquidities` (attribute-driven dyn caches +
  FVG-frame specs); MNQ wrapper preserves exact behavior. Added `_update_mes_liquidities`
  + MES dyn caches + `_fvg_mes_1hr` frame. `_extend_fvg_frames` → generic
  `_extend_instrument_fvg_frames`. MES seed pass in `on_daily_or_startup` (additive
  `today_mes` param).

**Wave 2 — Wiring + Unit tests**
- *Task 2.1:* `_run_smt_v2_detection` + `_pair_fvgs` + `_completed_tf_bar`; per-bar block
  in `on_1m_bar` (detect → buffer.add → cadence → flat-gated consumer → drain → save_smts).
  smts.json reload in `on_session_start`.
- *Task 2.2:* 31 unit tests.

**Wave 3 — Integration:** 12 pipeline integration tests.

---

## Divergences from Plan

### Divergence #1: `on_daily_or_startup` gained an optional `today_mes` param
**Classification:** ✅ GOOD
**Planned:** Plan says seed MES "from `_hist_mes_1m` + today's MES bars" in
`on_daily_or_startup`, which only received `today_mnq`.
**Actual:** Added `today_mes: pd.DataFrame | None = None` (additive, defaults None).
**Reason:** The method's existing callers/tests pass one arg; an additive optional
keyword preserves them while letting the 09:20/midnight gate forward `today_mes`.
**Impact:** Neutral/positive — no caller breakage; MES seed gets today's bars when available.
**Justified:** Yes.

### Divergence #2: 31 unit tests (plan said 27) + 2 extra integration coverage
**Classification:** ✅ GOOD
**Planned:** 41 tests total (27 unit / 12 integration / 2 store).
**Actual:** 43 total (31 unit / 12 integration / 2 store). The 4 extra unit tests cover
the plan's own Edge Cases section (test_empty_frames_no_crash, test_level_one_sided_no_fire)
and two review-surfaced bugs (test_asia_not_eligible_while_forming + boundary/30m tags).
**Impact:** Positive — higher path coverage; the 41-test floor is exceeded.
**Justified:** Yes.

### Divergence #3: `_fvg_progress` made side-aware (bull vs bear geometry)
**Classification:** ✅ GOOD (correctness)
**Planned:** Pseudocode treated entered/passed without an explicit side parameter.
**Actual:** `passed` is direction-aware (bull → high≥top; bear → low≤bottom) so a bar far
on the approach side is not falsely "passed".
**Reason:** A side-agnostic far-edge test mislabels an approach-side bar as passed.
**Impact:** Positive — fixes a fill mis-fire. **Justified:** Yes.

---

## Test Results

**Test Execution (targeted):**
`python -m pytest tests/test_smt_detect.py tests/test_session_pipeline.py tests/test_smt_state.py -q`
→ **115 passed**.

**Full suite:** `python -m pytest tests/ -q --ignore=tests/test_ib_realtime.py`
→ **1187 passed, 24 failed (pre-existing), 6 skipped**. The 24 failures are byte-identical
to the recorded baseline (hypothesis_smt, pickmytrade_executor, automation_main,
check_session_parquets, orchestrator_main, smt_humanize) — none introduced here.

**Pass Rate:** 43/43 new (100%); 0 new regressions.

---

## What was tested

- A wick SMT fires exactly once when one instrument's wick touches its level and the other does not (high→short, low→long), symmetric across leader.
- No SMT fires when both or neither instrument touches; persistent divergence re-emits only on the rising edge.
- Re-arm via a ≥-threshold opposite move OR an intervening opposite-direction SMT, and via a running day/week level advance; below-threshold retreat does not re-fire.
- Completed-session eligibility: ny_morning not eligible before 12:00 ET; asia not eligible while forming (18:00–24:00), eligible after midnight.
- Hidden SMT fires on close-vs-level tagged body + 15m/30m timeframe and is distinct from the wick SMT.
- Fill-A (leader entered/passed, laggard not reached) and Fill-B (both entered, one passed far edge) fire; Fill-B follows Fill-A on one continuous move without re-arm; independent Fill-B; entered/passed edge inclusivity; fill re-arm via the dual gate; one-sided FVG never fills.
- SmtBuffer per-minute overwrite, 5m accumulation, and drain only when the 5m floor advances (per-minute untouched).
- PendingSmtWatch copy-preserves across a buffer drain, invalidates on a confirming trend move or a contradicting opposite SMT, and round-trips via to_dict/from_dict.
- Integration: liquidities_mes populated alongside an unchanged MNQ liquidities; detection runs every 1m; hidden only on the 15m boundary; cadence boundaries (09:29/09:30/10:30/10:31) and morning-1m / offhours-5m ingest; flat-gating suppresses ingest with an active position; accumulator drains after the 5m consumer; fill pairing end-to-end; restart reload of edge-state + retained set from smts.json; emitted-event list unchanged (no smt/fill leakage).
- Store: smts.json file-mode roundtrip and in-memory (backtest) mode using _STORE.

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `python -c "import smt_detect, smt_state, session_pipeline"` | ✅ | clean |
| 2 | `pytest tests/test_smt_detect.py tests/test_smt_state.py -q` | ✅ | all pass |
| 3 | `pytest tests/test_session_pipeline.py -q` | ✅ | 54 passed |
| 4 | `pytest tests/ -q --ignore=tests/test_ib_realtime.py` | ✅ | no new failures vs baseline |

---

## Challenges & Resolutions

**Challenge 1:** Per-bar dynamic-liquidity pass overwrote test-injected daily.json.
- **Resolution:** Added a `_freeze_liquidities` test helper that monkeypatches both
  instrument liquidity passes to no-ops so detection sees the crafted levels/FVGs.

**Challenge 2:** Shared `self._detect_state` for both level and fill detection risked
cross-contamination in the batch opposite-SMT post-pass.
- **Resolution:** Guarded the post-pass to keys containing "|" (level-SMT format only).

**Challenge 3:** Fill re-arm mixed MNQ/MES scales when MES led the fire.
- **Resolution:** fire_price standardized to MNQ close; re-arm measures opposite move
  against MNQ uniformly.

**Challenge 4:** asia session level was eligible while still forming (close_h=0 always true).
- **Resolution:** Switched to per-session forming windows; eligible only outside the window.

---

## Files Modified

**New (2):**
- `smt_detect.py` — detection engine + SmtBuffer + PendingSmtWatch (~520 lines)
- `tests/test_smt_detect.py` — 31 unit tests (~430 lines)

**Modified (4):**
- `session_pipeline.py` — MES liquidity pass + instrument-generic refactor + on_1m_bar SMT V2 block (+402/-~30)
- `smt_state.py` — liquidities_mes default, DEFAULT_SMTS, load_smts/save_smts, final_snapshot (+21)
- `tests/test_session_pipeline.py` — 12 integration tests + spy/roundtrip updates (+318)
- `tests/test_smt_state.py` — smts roundtrip + in-memory tests, liquidities_mes in daily roundtrip (+29)

*(PROGRESS.md carries a pre-existing planning-phase entry from worktree setup — not part of this implementation.)*

**Total (tracked):** +734 / -45.

---

## Success Criteria Met

- [x] Regular wick SMT (symmetric, edge-once, directions)
- [x] Hidden body SMT (15m/30m only, tagged)
- [x] Dual-gate re-arm + level-advance re-arm
- [x] SMT-fills paired by 1hr bar; Fill-A/Fill-B; follow-on; one-sided no-fill
- [x] Detection every on_1m_bar, hypothesis/position-independent
- [x] Buffers + cadence (09:30–10:30→1m else 5m) + flat-gated consumer + drain-after
- [x] PendingSmtWatch copy-preserve / invalidate
- [x] Additive liquidities_mes; MNQ liquidities unchanged
- [x] smts.json persistence (file + in-memory) + restart reload
- [x] No new event leakage; detection total; no prints
- [x] 43 new tests pass; full suite no new failures; clean imports

---

## Recommendations for Future

**Plan Improvements:**
- Call out that `on_daily_or_startup` needs a `today_mes` to seed MES with today's bars.
- Pseudocode for fill `passed` should be explicitly side-aware.

**Process Improvements:**
- The `_freeze_liquidities` helper pattern is reusable for any test that needs to inject
  daily.json state past the per-bar recompute — worth documenting.

**CLAUDE.md Updates:**
- None required; existing state-store and None-tolerance patterns were followed.

---

## Conclusion

**Overall Assessment:** The feature is implemented per spec — fully additive, with the
MNQ path proven byte-identical and the new detection/buffer/consumer machinery covered by
43 passing tests. Three genuine bugs found in self-review were fixed with regression tests.
**Alignment Score:** 9/10 — minor, well-justified additive divergences only.
**Ready for Production:** Yes for review/merge — changes are UNSTAGED and uncommitted per
the execution rules; downstream consumer (trade/hypothesis) logic is intentionally out of scope.
