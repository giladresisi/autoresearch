# Execution Report: GIL-32 — SMT-conviction override on rule2b direction (Phase 1, standalone, ungated)

**Date:** 2026-06-16
**Plan:** `.agents/plans/gil32-smt-conviction-override.md`
**Executor:** lightweight / sequential (single agent, no team)
**Outcome:** ✅ Success

---

## Executive Summary

Implemented a standalone, ungated "standing SMT conviction" layer (GIL-32 Phase 1): a new pure module (`smt_conviction.py`) maintains a separate standing-SMT set with its own lifecycle (decayed residual after fulfillment, birth grace, adverse-run sustain), scores it into a single signed conviction in `[-1, +1]`, and lets a strong conviction flip rule2b's direction to the SMT side. The legacy relevance path (`smt_active_set` / `_compute_smt_score_v2` / `_co_evaluate_with_smt` confidence path / GIL-19 relevance) is untouched, `INVALIDATE_PTS` is not widened, and the default (no conviction) path is byte-identical to today. All 5 tasks complete; 54 named tests pass; changes UNSTAGED.

**Key Metrics:**
- **Tasks Completed:** 5/5 (100%)
- **Tests Added:** 23 (18 new in `test_smt_conviction.py` + 5 in `test_smt_hypothesis.py`)
- **Test Pass Rate (named suites):** 54/54 (100%)
- **Files Modified:** 6 (2 new, 4 edited)
- **Lines Changed:** +611 new files + 261 inserted into edited files (0 deletions)
- **Execution Time:** ~ implementation session (single sequential pass)
- **Alignment Score:** 10/10

---

## Implementation Summary

**Task 1 — `smt_conviction.py` (new, pure, no IO, never raises).**
Two total entry points:
- `update_standing(prev, new_divs, status_map, mnq_close, now_iso) -> list[dict]` — advances the standing set one bar: (1) adds new divs collapsed by `(ref_name, direction)` (wick supersedes body, newer wins — mirrors `ingest_active_set`); (2) drops `gone` keys, stamps `fulfilled_iso` on first fulfillment, drops a fulfilled record once `age > CONVICTION_RESIDUAL_MIN`, and applies an own adverse-run drop (grace within `CONVICTION_GRACE_MIN`, `CONVICTION_SUSTAIN` consecutive adverse closes required) reusing `smt_detect._invalidate_pts` (not widened). Returns a fresh list; fail-safe returns `prev` on any error.
- `conviction_score(standing, now_iso) -> (float, dict)` — defensive collapse by `(ref_name, direction)`, per-record `weight = tier_weight × residual_factor` (`residual_factor = 1.0` if unfulfilled else `max(0, 1 - age/RESIDUAL_MIN)`), signed (`short → −`, `long → +`), `score = sum(signed)/sum(|w|)` clamped `[-1, 1]`; returns `(score, {n, n_bear, n_bull, top_tier, refs})` for event logging.

Tunable constants documented together: `CONVICTION_STRONG=0.5`, `CONVICTION_RESIDUAL_MIN=180`, `CONVICTION_GRACE_MIN=5`, `CONVICTION_SUSTAIN=2`; tier weights reuse ATH/week 3, day 2, fill 1.5, session 1.

**Task 2 — maintain the standing set in the detection path (`session_pipeline.py`).**
Inside the exception-isolated shadow block of `_run_smt_v2_detection`, after the `smt_active_set` update, the standing set is advanced via `smt_conviction.update_standing(...)`. It is fed this bar's fired divs (`records`), a `smt_detect.smt_status(...)` map built over the standing records' folded detect keys (so fulfilled/gone are observed), and the current `mnq_close`, then persisted into `_hyp2["smt_conviction_set"]`. `smt_active_set` and every other field are left untouched.

**Task 3 — consume in the direction engine (`hypothesis.py`).**
- `run_hypothesis` reads `hypothesis["smt_conviction_set"]`, scores it via `conviction_score`, and passes `smt_conviction` / `smt_conviction_inputs` to `_determine_direction` (defaults `0.0` / `None`).
- `_determine_direction` gained the two keyword-only args and a single ungated override branch after rule2b sets `r2b_dir` (before `return r2b_dir`): when `abs(conv) >= CONVICTION_STRONG` and the conviction sign contradicts `r2b_dir`, it flips `r2b_dir` to the SMT side and tags `reason["smt_override"]` / `["smt_conviction"]` / `["smt_conviction_inputs"]`. No daily-trend gate. rule1/rule2/rule3_4 and triggers untouched.

**Task 4 — state default (`smt_state.py`).**
`DEFAULT_HYPOTHESIS` gains `"smt_conviction_set": []` (additive, back-compatible).

**Task 5 — verify.**
Named suites green; back-compat invariant asserted; touched suites and full suite checked for no new breakage.

---

## Divergences from Plan

### Divergence #1: Folded detect-key union (`keys`) on standing records

**Classification:** ✅ GOOD
**Planned:** Records carry `ref_name, direction, side, tier, type, fire_iso, fire_close, adverse_streak, fulfilled_iso`; status looked up via a single reconstructed detect key.
**Actual:** Each standing record also carries a folded `keys` union of the underlying wick/body detect keys; `_collapsed_status` aggregates status over both variants (fulfilled if ANY fulfilled; gone only if ALL present statuses gone), with a back-compat fallback that reconstructs the single key for legacy records lacking `keys`.
**Reason:** A code-review finding (LOW #1): a collapsed wick+body logical SMT could mis-read its lifecycle status when only one variant was fulfilled/gone in `detect_state`. Mirrors `ingest_smts`/`collapsed_relevance` semantics.
**Root Cause:** Plan gap — the single-key reconstruction didn't account for the wick+body collapse it itself mandated.
**Impact:** Positive — correct lifecycle aggregation for collapsed SMTs; no behavior change for single-variant records.
**Justified:** Yes.

### Divergence #2: Two extra tests beyond the named set

**Classification:** ✅ GOOD
**Planned:** The named tests in Task 4.
**Actual:** Added `test_update_never_raises_on_garbage` (totality on degenerate input), `test_update_collapsed_union_status_any_fulfilled` (verifies the Divergence #1 union behavior), and `test_gil32_conviction_set_flows_through_run_hypothesis` (Task 2 + Task 3 end-to-end integration).
**Reason:** Cover the "never raises" contract, the union-status fix, and the full state→score→override flow that the unit tests alone don't exercise.
**Root Cause:** Plan enumerated unit cases but not an integration case nor the totality guard.
**Impact:** Positive — higher confidence; no scope creep.
**Justified:** Yes.

---

## Test Results

**Tests Added:** 23

`tests/test_smt_conviction.py` (18):
- conviction_score: empty/None → 0.0; one-sided bearish → −1.0; one-sided bullish → +1.0; equal-and-opposite week-tier cancel → 0.0; tier weighting (week beats session) → −0.5; collapse by (ref,dir) counts wick+body once; normalized within `[-1,1]`; residual halves weight at half-window; fulfilled past window → 0 weight.
- update_standing: add+collapse wick/body; collapsed union status (any-fulfilled → stamp, all-gone → drop); gone key dropped; fulfilled stamps residual then drops past window; grace blocks adverse within GRACE_MIN; sustain requires consecutive adverse closes; never raises on garbage.

`tests/test_smt_hypothesis.py` (5): override flips contradicting rule2b; no-op when conviction aligns; no-op when weak; default `0.0` byte-identical to legacy call; conviction set flows through `run_hypothesis` end-to-end.

**Test Execution:** `python -m pytest tests/test_smt_conviction.py tests/test_smt_hypothesis.py -q` → `54 passed in 2.12s`.

**Pass Rate:** 54/54 (100%) on the named suites.

---

## What was tested

- An empty or `None` standing set scores to exactly `0.0` with zeroed inputs.
- Three same-side bearish SMTs score to −1.0 (and bullish to +1.0), one-sidedness saturating the clamp.
- A week-tier short and week-tier long of equal weight cancel exactly to `0.0`.
- Tier weighting is respected: a week short (3) against a session long (1) nets −0.5 with `top_tier="week"`.
- Wick and body variants of the same `(ref_name, direction)` collapse and count once in both the score and `inputs["n"]`.
- A fulfilled SMT's residual weight decays linearly — ~0.5 at half the residual window — and falls to 0 (record ignored) past the window.
- `update_standing` adds a new div, collapses wick+body to one record with wick superseding body.
- A collapsed record aggregates lifecycle status over both folded detect keys: ANY-fulfilled stamps the residual; ALL-gone drops the record.
- A standing record whose detect key is `gone` in the status map is dropped.
- A newly fulfilled record is stamped and kept within the residual window, then dropped once age exceeds `CONVICTION_RESIDUAL_MIN`.
- An adverse close within `CONVICTION_GRACE_MIN` of the fire never arms the adverse streak (birth grace).
- The adverse-run drop requires `CONVICTION_SUSTAIN` consecutive adverse closes — one adverse then a benign close resets the streak.
- `update_standing` never raises on `None`/garbage input and returns a list.
- In `_determine_direction`, a conviction with `|conv| >= CONVICTION_STRONG` whose sign contradicts rule2b flips the direction and tags `smt_override`/`smt_conviction`/`smt_conviction_inputs`.
- A conviction that aligns with rule2b's direction is a no-op (no flip, no tag).
- A conviction weaker than `CONVICTION_STRONG` never flips, even when contradicting.
- The default `smt_conviction=0.0` path returns a direction and `reason` dict byte-identical to the legacy no-kwargs call (back-compat).
- End-to-end: a seeded strongly-bearish `smt_conviction_set` flips a rule2b "up" to "down" through `run_hypothesis` with `smt_override` set.

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `pytest tests/test_smt_conviction.py tests/test_smt_hypothesis.py -q` | ✅ | 54 passed |
| 2 | Touched suites (conviction, hypothesis, session_pipeline, smt_detect, smt_relevance, smt_relevance_rules, smt_strategy_v2) | ✅ | all pass |
| 3 | `pytest tests/ -q` (full suite) | ⚠️ | 1156 passed, 6 skipped, 1 failed + 16 errors — all PRE-EXISTING (present in baseline; not introduced) |

Pre-existing failures, confirmed independent of this work:
- `test_smt_warmup_startup::test_warmup_catches_june8_bullish_smt` (1 failure)
- `test_smt_decouple_active` (16 errors — a fixture references `trend.load_daily`, which does not exist)

---

## Challenges & Resolutions

**Challenge 1:** Collapsed wick+body standing records can mis-read lifecycle status from a single reconstructed detect key.
- **Issue:** When only one variant (e.g. body) is `fulfilled`/`gone` in `detect_state`, a single-key lookup would either miss the fulfillment or wrongly drop the record.
- **Root Cause:** The plan's single-key reconstruction conflicted with the wick+body collapse it required.
- **Resolution:** Each record carries a folded `keys` union; `_collapsed_status` aggregates (any-fulfilled → fulfilled; all-gone → gone; else unfulfilled), mirroring `collapsed_relevance`. Back-compat fallback reconstructs the single key when `keys` is absent.
- **Time Lost:** Minimal (caught in code review, fixed in place).
- **Prevention:** When a plan mandates collapse semantics, specify the matching status-aggregation rule alongside.

**Challenge 2:** Guaranteeing the no-conviction path is byte-identical.
- **Issue:** Any reordering of `reason` keys or an unconditional tag would break back-compat.
- **Root Cause:** Override branch sits inside the shared rule2b return path.
- **Resolution:** Branch guarded by `if smt_conviction and abs(...) >= STRONG`; tags only set on an actual flip. Asserted by `test_gil32_override_absent_when_no_flip_default_is_back_compat` (full `reason` dict equality vs the legacy call).
- **Time Lost:** None.
- **Prevention:** Keep an explicit byte-identical default-path test for any new optional kwarg in the direction engine.

---

## Files Modified

**New (2 files):**
- `smt_conviction.py` — pure conviction module (`update_standing`, `conviction_score`, helpers, constants) (369 lines)
- `tests/test_smt_conviction.py` — 18 unit tests (242 lines)

**Edited (4 files):**
- `hypothesis.py` — import `smt_conviction`; two kwargs + ungated override branch in `_determine_direction`; `run_hypothesis` reads `smt_conviction_set`, scores it, passes kwargs (+25)
- `session_pipeline.py` — maintain `smt_conviction_set` in `_run_smt_v2_detection`'s shadow block via `update_standing` + status map; persist into `_hyp2` (+28)
- `tests/test_smt_hypothesis.py` — 5 GIL-32 override/integration tests (+202)
- `smt_state.py` — `DEFAULT_HYPOTHESIS["smt_conviction_set"] = []` (additive) (+6)

**Total:** +872 lines (611 new files + 261 into edited files), 0 deletions. All changes UNSTAGED.

---

## Success Criteria Met

- [x] New pure module `smt_conviction.py` with `update_standing` + `conviction_score`, documented constants, totality (never raises)
- [x] Standing set maintained in the detection path, persisted as `smt_conviction_set`; `smt_active_set` untouched
- [x] Ungated rule2b override in `_determine_direction` with reason tagging; rule1/rule2/rule3_4 and triggers untouched
- [x] `DEFAULT_HYPOTHESIS` additive key; default `[]` → conviction `0.0` → byte-identical direction
- [x] `INVALIDATE_PTS` not widened (adverse-run reuses `smt_detect._invalidate_pts`)
- [x] Named unit + integration tests green (54/54); no new full-suite breakage
- [x] Changes UNSTAGED; not committed/pushed
- [ ] A/B 1s regression + occurrence verification (06-09/10/12 flip "down"; 05-28/05-20 guards) — DEFERRED to feature.md Stages C–D (run by the orchestrating agent, out of scope here)

Reviews: code-review verdict PASS (4 LOW findings; #1 wick+body union-status FIXED; #2/#3/#4 deferred as Phase-1 out-of-scope / stylistic). acceptance-criteria-validate verdict ACCEPTED (7/7 PASS).

---

## Recommendations for Future

**Plan Improvements:**
- When mandating a collapse key, also specify the corresponding status-aggregation rule for collapsed records (avoids the wick+body status gap fixed here).

**Process Improvements:**
- Continue requiring a byte-identical default-path test for every new optional kwarg added to the direction engine.

**CLAUDE.md Updates:**
- None required — change follows existing conventions (pure shadow module, exception-isolated detection block, additive state key).

---

## Conclusion

**Overall Assessment:** Phase 1 of GIL-32 is implemented cleanly and conservatively. The conviction layer is a self-contained pure module wired into the existing shadow detection block and the rule2b return path with strict back-compat (default no-op) and a fail-safe lifecycle. All named unit and integration tests pass; the only full-suite failures are pre-existing and unrelated. Code review PASS, acceptance ACCEPTED 7/7.

**Alignment Score:** 10/10 — all five tasks delivered as specified; the two divergences are additive improvements (a code-review-driven correctness fix and extra coverage), neither expands scope.

**Ready for Production:** No — by design. This is a shadow-direction Phase 1 whose live value is gated on the A/B 1s regression and occurrence verification in feature.md Stages C–D. Code is complete, tested, and UNSTAGED pending that evaluation.
