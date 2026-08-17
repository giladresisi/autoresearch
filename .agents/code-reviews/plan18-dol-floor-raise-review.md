# Code Review — Plan 18 Part A: DOL floor raise (DOL_MIN_DRAW_RATIO 0.5 -> 1.0)

Date: 2026-08-16
Scope: unstaged diffs of `agent/derive_facts.py`, `agent/test_menus.py`,
`agent-docs/strategy/decisions/thesis.md` only (per review request). feature.md
deletion, l2-mechanisms.md, `.agents/plans/*`, and untracked files excluded.

**Stats (in-scope files):**

- Files Modified: 3
- Files Added: 0
- Files Deleted: 0
- New lines: 47 (in-scope portion: thesis.md +8/-4, derive_facts.py +14/-3, test_menus.py +39/-14 per `git diff --stat`)
- Deleted lines: 14

## Verdict

Code review passed. No technical issues detected in the changeset.

## Verification performed

1. **Constant + call sites.** `DOL_MIN_DRAW_RATIO = 1.0` (derive_facts.py:1051) is read
   at exactly one behavioral site — `_dol_menu` floor computation (line 1134,
   `max(DOL_MIN_DRAW_DISTANCE_PTS, DOL_MIN_DRAW_RATIO * ar)`) — and one rendering site
   (line 1424 f-string, which interpolates the constant dynamically, so the rendered
   guidance now correctly says `1.0x avg_1h`). No other module hardcodes the old 0.5
   floor (repo-wide grep; the `0.5x` hits in `contracts/validate_contracts.py` /
   `test_contracts.py` are the unrelated clearance-magnitude tiers). The second
   `_dol_menu` call site (derive_facts.py:1868, render fallback) passes no ATR, so it
   uses the flat 5-pt guard by design — documented, unchanged behavior.
2. **Test geometry vs the new floor** (now_price=100, ar=20 -> floor=max(5, 1.0*20)=20):
   - `test_dol_floor_scales_with_avg_range`: 118 -> 18 pts (0.9x) excluded (`18 < 20`),
     122 -> 22 pts (1.1x) kept. Correct, and correctly brackets the boundary.
   - `test_dol_floor_falls_back_to_flat_guard_without_avg_range`: no ATR -> flat 5-pt
     guard, 7-pt pool stays. Correct — ratio never applies without ATR.
   - `test_dol_band_tags_and_ratio`: pools moved to 130 (30 pts = 1.5x BAND) and 190
     (90 pts = 4.5x FAR) — both above the new floor; dist_ratio assertions exact.
   - `test_dol_projection_offered_when_band_empty`: down_band moved 85 -> 70 (15 pts =
     0.75x would now be floor-excluded; 30 pts = 1.5x survives) — necessary and correct.
   - `test_dol_floor_raise_frees_band_for_projection` (new): 116 -> 16 pts (0.8x)
     excluded -> UP band empty -> projection_up at day_hi 104 + 1.0*20 = 124.0. Gates
     verified: day-stretch max(|100-104|,|100-60|)/20 = 2.0 <= 3.0; weekly_mid unset in
     `_refit_bundle` -> weekly gate skipped. Assertions match the code path exactly.
   - Unchanged refit tests (`stretch_gated`, `counts_toward_exhaustion`,
     `weekly_extension_gate`) all use 90-pt (4.5x) pools — unaffected by the raise.
3. **Test runs:**
   - `agent/test_menus.py`: **24/24 pass** (matches the plan's commit-gate claim).
   - DOL-referencing suites (`contracts/test_contracts.py`,
     `contracts/test_menu_membership.py`, `test_validator.py`,
     `executor/test_trade_director.py`, `decisions/test_records.py`): 217 passed,
     3 failed — **pre-existing** (see below).
   - `bench/test_lifecycle.py`, `bench/test_score.py`,
     `executor/test_mechanism_adapter.py`: 42/42 pass.
4. **Docs/comments.** thesis.md §8 bullet and the derive_facts.py comment block both
   cite §10.3 v2 / commit `272cf7c` with figures matching the plan (73 days, 132 arms,
   60–75% swept 0.5–0.7x, <=11% at >=1.0x, 40% post-rollover in 0.7–1.0x, D2 73% /
   +72.6 pts counterfactual). The `_dol_menu` docstring now references
   `DOL_MIN_DRAW_RATIO` by name instead of a hardcoded 0.5 — removes a future doc-drift
   hazard. No prints, no behavior change beyond the constant.

## Pre-existing Failures (NOT introduced by this changeset)

Verified by re-running with `DOL_MIN_DRAW_RATIO` patched back to 0.5 in-process
(pytest plugin; no git mutation) — all 3 fail identically at the old floor value, and
the only other unstaged tracked changes are docs (feature.md deletion,
l2-mechanisms.md):

- `agent/contracts/test_contracts.py::test_pending_resolution_accepted_when_immature`
- `agent/contracts/test_menu_membership.py::test_menu_matching_predicate_passes_and_tagged_menu_hit`
- `agent/contracts/test_menu_membership.py::test_escape_hatch_valid_predicate_passes_with_tag`

Root cause (brief): all three fixtures declare a directional bias with an empty
evidence ledger and now trip `ARI_THESIS_BIAS` ("do not leave the ledger empty under a
directional bias") — fixture drift from the recent bias-consistency validator changes
(likely commit `40e8875`), unrelated to the DOL floor.

Also pre-existing: `agent/contracts/test_schemas.py` fails collection —
`ModuleNotFoundError: No module named 'jsonschema'` (missing from `.venv`; environment
issue, not code).
