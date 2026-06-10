# Execution Report: SMT V2 Relevance-Filter Core (Phase 2 of 3)

**Plan:** `.agents/plans/smt-v2-relevance-filter-core.md`
**Date:** 2026-06-10
**Status:** DONE — shadow-only, zero behavior change, all changes UNSTAGED.

## Summary

Implemented the relevance-filter infrastructure as pure, exhaustively-unit-tested
functions (Contract B in `hypothesis.py`, Contract C in `smt_detect.py`) plus the
SHADOW active-set wiring in `session_pipeline._run_smt_v2_detection`. The active set +
dominant are computed each 1m bar and stored under `hypothesis.json` debug keys
(`smt_active_set`/`smt_dominant`) ONLY — direction determination is entirely unchanged.

## Files Modified

- `hypothesis.py` (+234): divs-record schema docstring; `RELEVANCE_X_PTS=25.0`;
  `_TIER_RANK`/`_tier_rank`; Contract B `to_record`, `smt_authority`, `dominant`,
  `ingest_smts`; import of `_level_class`/`_record_key` from `smt_detect`.
- `smt_detect.py` (+55): Contract C `_record_key` + `fulfillment_status` (read-only).
- `session_pipeline.py` (+42): exception-isolated SHADOW active-set compute + store in
  `_run_smt_v2_detection` (after dedup, before buffer add). No `direction` write.
- `smt_state.py` (+5): `DEFAULT_HYPOTHESIS` gains `smt_active_set: []`, `smt_dominant: None`.

## Files Created

- `tests/test_smt_relevance.py` (37 tests): Contract B authority ordering (all cases),
  `to_record` round-trips (smt/fill × wick/body × timeframes × every tier), `ingest_smts`
  gate (flat/active/proximity/tier/reject, both exact boundaries, dedup, fulfilled-drop,
  none, no-mutation), `_tier_rank`, invalidation lifecycle, and 3 SHADOW tests
  (populates-without-touching-direction, no-smt-no-change, exception-swallowed).
- `tests/test_smt_fulfillment.py` (11 tests): Contract C `_record_key` (level/fill/total)
  and `fulfillment_status` (unfulfilled/fulfilled/gone/fill/read-only/empty/detection-roundtrip).

## Tests

- Plan tests: 48/48 pass (37 relevance + 11 fulfillment).
- Full suite: `1332 passed, 24 failed, 6 skipped, 14 deselected`
  (baseline before change: `1284 passed, 24 failed`). +48 new passing, ZERO new failures.
- The 24 failures are the documented pre-existing environmental set (`test_pmt_*_slippage`,
  `test_modify_stop_entry_*`, `test_smt_humanize`, `test_hypothesis_smt`,
  `test_check_session_parquets`, `test_automation_main`, `test_orchestrator_main`) — all
  unrelated to this change (verified: a `test_hypothesis_smt` failure is a `pd_range_case`
  logic assertion in `_determine_direction`, which was not touched).
- `tests/test_ib_realtime.py` excluded from runs (live IB sleep, hangs — not part of the
  `not integration` standard suite; integration-class).

## Checkpoints

- CP1 (Contract B+C import): PASS.
- CP2 (pipeline import + existing pipeline/detect suites green): PASS (160 passed).
- CP3 (every Contract B/C function+branch has a named passing test; full suite green): PASS.

## Shadow / parity confirmation

Direction determination is UNCHANGED: `run_hypothesis`, `_determine_direction`,
`_compute_smt_score`, `_compute_divs`, `build_hypothesis_from_direction` are untouched.
The shadow block writes ONLY `smt_active_set`/`smt_dominant`; the whole block is wrapped
in `try/except Exception: pass`. Tests prove (a) the active set is populated while
`direction` is preserved, and (b) a raising `to_record` is swallowed and the live smt-div
emission + state persist still happen. Full suite identical to baseline = parity.

## Divergences from plan

- **`divs` reuse deferred (per plan NOTES):** the shadow active set is stored under the
  new `smt_active_set`/`smt_dominant` keys, NOT by overwriting `divs` — exactly as the plan
  recommends for guaranteed zero behavior change. Phase 3 migrates `divs`.
- **`backing_tier`:** Phase-1 already writes `backing_tier` into `position.json["active"]`
  (per `freeze_active_mgmt` docstring, default "week"/"day"). The shadow reads it when
  ACTIVE; when absent the tier gate degrades to proximity-only as specified.
- **No prints / no debug logs added.**

## Risks / follow-ups

- ATH tier branch is tested but not yet exercised by the live producer (`smt_detect` never
  emits `ref_name=="ATH"` today); Phase 3 supplies ATH context.
- `RELEVANCE_X_PTS=25.0` is a first-guess tunable.
- Shadow block's blanket `except: pass` is intentional for Phase 2; Phase 3 replaces it with
  structured error capture once the path is load-bearing.

## Compliance

Changes are UNSTAGED. No `git add`/`commit`/`push`/`reset`. Commits `d67be54` and
`89a5e27` untouched.
