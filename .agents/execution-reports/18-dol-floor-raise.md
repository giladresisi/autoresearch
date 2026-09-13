# Execution Report: Plan 18 Part A — DOL floor raise (DOL_MIN_DRAW_RATIO 0.5 → 1.0)

**Date:** 2026-08-16
**Plan:** `.agents/plans/18-dol-floor-raise.md`
**Executor:** Sequential (single agent; deterministic sweep + LLM mini-diff validation)
**Outcome:** ✅ Success (unstaged — commit intentionally deferred to the parent session)

---

## Executive Summary

Raised the ATR-scaled DOL eligibility floor `DOL_MIN_DRAW_RATIO` from 0.5 to 1.0 in
`agent/derive_facts.py`, per the L2 track's `l2-mechanisms.md` §10.3 v2 evidence (commit
`272cf7c`: 73 days, 132 named-pool arms, pre/post-rollover split — 0.5–0.7× pools are
opening-rotation fodder, ≥1.0× is safe in both regimes). The change was validated three
ways: refit unit tests (24/24 in `test_menus.py`), a deterministic 61-boundary sweep at
floors 0.5/0.9/1.0/1.1 (13 boundaries changed, exactly ONE bias flip — the plan's
enumerated 07-17 ripple-3 case, judged correct), and a 4-day LLM mini-diff harness rerun
(all four days PASS). The L2 hand-off table in the plan is filled with exact prices for
the L2 track's §11 re-verification.

**Key Metrics:**
- **Tasks Completed:** 6/6 plan checklist blocks (100%)
- **Tests Added:** 1 new test + 4 refit to the new floor
- **Test Pass Rate (scoped):** `agent/test_menus.py` 24/24 (100%, was 23)
- **Files Modified (this changeset):** 3 code/doc + 1 plan + 8 result/backup JSONs
- **Lines Changed (code/doc):** +47/−14 across `derive_facts.py` / `test_menus.py` / `thesis.md`
- **Alignment Score:** 10/10

---

## Implementation Summary

### Code change (the "one constant + its ripples" scope)

- `agent/derive_facts.py` (~L1051): `DOL_MIN_DRAW_RATIO = 0.5` → `1.0`.
  - The constant's comment block expanded to cite `l2-mechanisms.md` §10.3 v2 / commit
    `272cf7c` (73 days, 132 arms, segment split, hold-for-D2 counterfactual: D2 reached
    73% by 16:00, median +72.6 pts vs TP-at-near-D1).
  - `_dol_menu` docstring now references the floor by constant name
    (`max(5pts, DOL_MIN_DRAW_RATIO x avg_1h)`) instead of a hardcoded `0.5x`, so the
    docstring can no longer drift from the value.
  - Deliberately unchanged: the flat `DOL_MIN_DRAW_DISTANCE_PTS = 5.0` latency guard, the
    no-ATR fallback, `DOL_BAND_MAX_RATIO = 3.0`, `DOL_PROJECTION_RATIO = 1.0`.

### Tests (`agent/test_menus.py`)

- Refit `test_dol_floor_scales_with_avg_range` to the plan's cases (a)/(b): a 0.9×-away
  pool excluded, a 1.1×-away pool kept (both legal under the old floor).
- Annotated `test_dol_floor_falls_back_to_flat_guard_without_avg_range` as plan case (c):
  flat 5-pt fallback without ATR is unchanged by the raise.
- NEW `test_dol_floor_raise_frees_band_for_projection` — plan case (d): a direction whose
  only band pool sat at 0.8× empties its band under the new floor, and the stretch-gated
  `projection_up` is offered in its place (exact projection price asserted).
- `test_dol_band_tags_and_ratio` and `test_dol_projection_offered_when_band_empty`
  geometry moved above the new floor (0.75× → 1.5×) so they keep testing band-tagging and
  projection-suppression rather than accidentally testing the floor.

### Documentation

- `agent-docs/strategy/decisions/thesis.md` §8 "ATR-scaled draw floor" bullet: value
  updated to 1.0×, with the §10.3 v2 / `272cf7c` citation and the regime-weighting
  rationale (post-rollover reading of the ambiguous 0.7–1.0× band).

### Plan bookkeeping / L2 hand-off

- `.agents/plans/18-dol-floor-raise.md`: all checklist boxes marked `[x]` with inline
  results; the L2 hand-off table's "model adopted?" column filled for all 4 mini-diff
  days; 9 additional changed-boundary rows appended (all non-09:20, none bias-flipping);
  "Part A commit: **PENDING**" line left untouched for the parent session.
- `manual-l1-thesis/`: 4 rerun result JSONs
  (`20260717/0721/0731/0805_0920000400_openrouter.json`) + `.pre_floor_raise.json`
  backups of the 4 baselines.

---

## Divergences from Plan

### Divergence #1: Sweep covered 61 stored boundaries, not the estimated 66

**Classification:** ⚠️ ENVIRONMENTAL

**Planned:** "the full 66-boundary enumeration" (hand-off Method/scope note).
**Actual:** 61 distinct stored boundaries harvested from `manual-l1-thesis/**/*.json`.
**Reason:** The 66 figure was an estimate written before the boundary harvest; 61 is what
actually exists on disk.
**Root Cause:** Plan estimate predating data collection.
**Impact:** Neutral — the sweep is exhaustive over every stored boundary; nothing was
skipped.
**Justified:** Yes.

### Divergence #2 (finding, not a plan deviation): 08-05 model falsifier inverted

**Classification:** ⚠️ ENVIRONMENTAL (model-reasoning artifact, flagged onward)

**Planned:** Mini-diff pass criteria: model adopts deeper D1, zero DOL-related rejections.
**Actual:** 08-05 passes all criteria (UP + `prev3_week_high @ 30062.5` on all attempts),
but the model's `falsified_if` is inverted — `price_beyond 29990.75 above` on an UP
thesis — so the walk-forward reads falsified at 09:33, 12 minutes BEFORE the genuine
09:45 DOL touch.
**Reason:** Model reasoning artifact, unrelated to the floor change (the same predicate
class existed pre-change).
**Impact:** Neutral to Part A; material to the L2 replay — flagged in the hand-off
table's CAVEAT so L2 doesn't misread the walk-forward as a floor regression.
**Justified:** Yes (documented in the hand-off row per the plan's flag-to-L2 gate).

No other divergences: floor value (1.0×), test cases (a)–(d), sweep design, knife-edge
floors, draw-realism check, 07-17 hard-case adjudication, and the hand-off format all
executed exactly as specified.

---

## Test Results

**Tests Added/Refit:**
- NEW `test_dol_floor_raise_frees_band_for_projection` (plan case d)
- Refit: `test_dol_floor_scales_with_avg_range` (cases a+b),
  `test_dol_floor_falls_back_to_flat_guard_without_avg_range` (case c annotation),
  `test_dol_band_tags_and_ratio`, `test_dol_projection_offered_when_band_empty`
  (geometry lifted above the new floor)

**Test Execution (all via `.venv/Scripts/python.exe`):**

| Suite | Result | vs baseline |
|---|---|---|
| `agent/test_menus.py` | **24 passed** | was 23 (+1 new) |
| agent top-level suites | **166 passed** | all green |
| `agent/contracts` (−test_schemas) | 158 passed, 3 failed | 3 failures PRE-EXISTING (`test_pending_resolution_accepted_when_immature` + 2 `test_menu_membership` ARI empty-ledger); `test_schemas` collection error = `jsonschema` not installed in `.venv` (known env gap) |
| `agent/decisions` | 36 passed, 1 failed | failure PRE-EXISTING (`test_history_supplies_prior_day_levels`) |
| `agent/executor` | **56 passed** | all green |
| `agent/bench` (`--timeout=300`) | **55 passed** | identical to pre-change baseline |
| `tests/` (full repo) | 1379 passed, 2 failed, 16 errors | all failures/errors PRE-EXISTING `trend.py`-related (`test_smt_decouple_active` AttributeError `trend.load_daily`; `test_smt_fill_plot`) — unrelated to this `agent/`-only change |

**Pass Rate:** 0 new failures anywhere; scoped suite 24/24 (100%).

---

## What was tested

- A pool 0.9× avg_range away is EXCLUDED from the DOL menu at the new 1.0× floor (it was
  legal at 0.5×).
- A pool 1.1× avg_range away is KEPT — the floor cuts exactly at 1.0×.
- With no ATR available, the flat 5-pt fallback guard is byte-identical to pre-change
  behavior (the ratio never applies without `avg_range_1h`).
- A direction whose ONLY band pool sat at 0.8× now has an empty band, and the
  stretch-gated `projection_up` synthetic draw is offered in its place at the exact
  projected price (day_hi + 1.0×ATR, tick-snapped).
- BAND/FAR tagging and `dist_ratio` values are computed correctly for pools that survive
  the new floor (1.5× BAND, 4.5× FAR).
- Projection is still SUPPRESSED when a direction retains a genuine band pool above the
  floor (no spurious projections from the raise).

---

## Validation Results

| Level | Check | Status | Notes |
|---|---|---|---|
| 1 | Unit tests `agent/test_menus.py` | ✅ | 24/24; plan cases (a)–(d) all covered |
| 2 | Full agent + repo suites | ✅ | 0 new failures; pre-existing failures enumerated above |
| 3 | Deterministic boundary sweep 0.5 vs 1.0 (61 boundaries; facts built once per boundary, menus rebuilt per floor) | ✅ | 13 boundaries changed; exactly 1 bias flip (07-17 09:20 DOWN → no-liquidity NEUTRAL — the plan's enumerated ripple-3 case, judged CORRECT); the 4 pre-computed hand-off rows reproduced exactly. Output: scratchpad `dol_floor_sweep_out.txt` |
| 3a | Knife-edge at 0.9 / 1.1 | ✅ | ZERO bias differences vs 1.0 at either floor; only changed-D1-set membership differs (07-31's 0.9214× pool survives at 0.9; five boundaries deepen an extra direction at 1.1) — nothing load-bearing sits on the exact value |
| 3b | Draw-realism on changed boundaries | ✅ | New D1 reached same-day on 6/12 changed boundaries with a surviving D1 (~50%, below §10.3's 67–100% — expected, since our set includes anti-direction menus and NEUTRAL days, per the plan's own caveat) |
| 4 | LLM mini-diff (4 harness reruns @ 09:20, openrouter `anthropic/claude-haiku-4.5`) | ✅ | 07-17 NEUTRAL dol=None clean first attempt 0 retries; 07-21 UP `prev3_day_high@29796.5` retries 2 (=baseline); 07-31 UP `prev4_day_high@28763.75` retries 1 (baseline 2); 08-05 UP `prev3_week_high@30062.5` all attempts, retries 2 (baseline 1, none DOL-related). ZERO DOL-related rejection codes; no fallbacks |
| 5 | L2 hand-off gate | ✅ | Hand-off section filled with exact prices/ratios, "model adopted?" outcomes, same-day-touch times, and the 08-05 inverted-falsifier caveat |
| 6 | Review pipeline | ✅ | code-review: PASSED, zero changeset issues (`.agents/code-reviews/plan18-dol-floor-raise-review.md`); acceptance-criteria validation: ACCEPTED 7/7 |

---

## Challenges & Resolutions

**Challenge 1:** The 07-17 hard case (ripple-3) had two possible outcomes — projection or
NEUTRAL downgrade — and the plan required explicit adjudication.
- **Issue:** Its DOWN menu had no D2 behind the excluded `london(cur)_low` (0.51×), so
  the raise empties the band entirely.
- **Root Cause:** `projection_down` is doubly gated on this day (day-stretch 5.21× > 3.0
  AND weekly 5.98× below mid > 4.0), so no synthetic draw can backfill.
- **Resolution:** Sweep produces no-liquidity NEUTRAL; the mini-diff confirms the model
  reaches NEUTRAL cleanly on the FIRST attempt with dol=None and 0 retries (the pre-raise
  baseline model had itself tried NEUTRAL and been ARI-forced to DOWN). Judged CORRECT —
  everything left below was opening-rotation fodder; restores thesis.md §10's own 07-17
  no-liquidity precedent. Flagged to L2 (their +91.25 §8 takeover day now has no L1 arm).
- **Time Lost:** None — the plan pre-enumerated the case.
- **Prevention:** n/a; this is the process working as designed.

**Challenge 2:** 08-05 walk-forward showed a falsification BEFORE the DOL touch.
- **Issue:** Looked like a floor-raise regression at first read.
- **Root Cause:** The model's `falsified_if` is inverted (`price_beyond 29990.75 above`
  on an UP thesis) — a model-reasoning artifact independent of the floor.
- **Resolution:** Verified the genuine DOL touch occurs 09:45:08 (+25 min); documented as
  a CAVEAT in the hand-off row so the L2 track doesn't misattribute it.
- **Time Lost:** Minimal.
- **Prevention:** Candidate for a future polarity sanity check on `falsified_if`
  direction vs thesis bias (noted for the L2/L1 tracks; NOT implemented here — out of
  Part A's one-constant scope).

---

## Files Modified

**Code + docs (3 files, +47/−14):**
- `agent/derive_facts.py` — `DOL_MIN_DRAW_RATIO` 0.5 → 1.0 + expanded evidence comment
  block + `_dol_menu` docstring references the constant by name (+12/−2, ~L1029–L1128)
- `agent/test_menus.py` — cases (a)–(d) refit/added; two neighbor tests' geometry lifted
  above the new floor (+31/−8, L306–L370)
- `agent-docs/strategy/decisions/thesis.md` — §8 draw-floor bullet: 1.0× + §10.3 v2 /
  `272cf7c` citation (+6/−2, ~L697)

**Plan (1 file):**
- `.agents/plans/18-dol-floor-raise.md` — checklist `[x]` with inline results; hand-off
  "model adopted?" column filled; 9 non-09:20 changed-boundary rows appended;
  "Part A commit: PENDING" untouched

**Validation artifacts (untracked):**
- `manual-l1-thesis/2026071{7}/…` etc. — 4 rerun result JSONs
  (`20260717/20260721/20260731/20260805_0920000400_openrouter.json`) + 4
  `.pre_floor_raise.json` baseline backups
- Scratchpad: `dol_floor_sweep_out.txt` (61-boundary × 4-floor sweep output)

**Out-of-scope working-tree changes present but NOT part of this changeset** (from the
parallel L2 track / earlier sessions; code review confirmed zero changeset issues):
`l2-mechanisms.md` (L2 session's §10.3 v2 working copy), `feature.md` deletion,
`.agents/plans/19-*/20-*.md`.

**Total (this changeset):** 47 insertions(+), 14 deletions(−) across code/docs, plus plan
bookkeeping and result JSONs.

---

## Success Criteria Met

- [x] Unit tests: cases (a) 0.9× excluded, (b) 1.1× kept, (c) no-ATR flat fallback
      unchanged, (d) 0.8×-only band → projection offered — 24/24 `test_menus.py`
- [x] Boundary sweep 0.5 vs 1.0 over all 61 stored boundaries — 13 changed, exactly ONE
      bias flip (07-17, the enumerated ripple-3 case, judged correct)
- [x] Draw-realism on changed boundaries — 6/12 new D1s reached same-day (caveat noted)
- [x] Knife-edge 0.9/1.1 — zero bias differences; no conclusion flips
- [x] 07-17 hard case enumerated and explicitly judged (no-liquidity NEUTRAL, CORRECT)
- [x] LLM mini-diff on all 4 changed 09:20 days — all PASS, zero DOL-related rejections,
      no fallbacks
- [x] L2 hand-off section filled (exact prices, outcomes, caveats, 9 extra sweep rows)
- [x] Doc rides along: thesis.md §8 + constant comment block
- [x] code-review PASSED (zero changeset issues); acceptance-criteria ACCEPTED 7/7
- [ ] Part A commit — INTENTIONALLY deferred: "Part A commit: PENDING" stands; the parent
      session commits (per instruction, nothing staged/committed/pushed here)

---

## Recommendations for Future

**Plan Improvements:**
- Write boundary-count estimates as "all stored boundaries (~N)" rather than a hard
  number when the harvest hasn't run yet (the 66 vs 61 non-divergence cost a
  clarification cycle).

**Process Improvements:**
- The pre-computation-then-implement pattern (hand-off rows computed with an in-process
  floor override before touching the repo constant) made the sweep a true regression
  check — the 4 pre-computed rows reproducing exactly is strong evidence. Worth reusing
  for future one-constant changes.

**Follow-ups (for the L1/L2 tracks, not CLAUDE.md):**
- 08-05's inverted `falsified_if` suggests a cheap validator polarity check
  (`price_beyond X above` on an UP thesis whose DOL is above → suspicious); consider for
  a future L1 validator iteration.
- Two FAR-only-direction days (07-21 own-direction, 07-16 09:00) are flagged in the
  hand-off table as watch-items for the day-stretch projection gate.

---

## Conclusion

**Overall Assessment:** A textbook minimal-scope change: one constant, its comment block,
one doc bullet, and tests refit to prove exactly the plan's four cases — validated by an
exhaustive deterministic sweep with a knife-edge robustness check and a targeted 4-day
LLM mini-diff, and handed off to the L2 track with exact prices. The single behavioral
surprise candidate (07-17 losing its DOWN thesis entirely) was pre-enumerated by the
plan, produced, and judged correct.

**Alignment Score:** 10/10 — every checklist block executed as written; the only
divergence is a pre-harvest estimate (66→61) with no substantive effect.

**Ready for Production:** Yes, pending the parent session's commit — all gates passed;
changes are deliberately left unstaged and "Part A commit: PENDING" remains in the plan
until the parent commits.
