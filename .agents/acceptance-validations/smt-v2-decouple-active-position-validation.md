## Acceptance Criteria Validation Report

**Feature / Request:** SMT-v2 Phase 1 — Decouple Active-Position Management from the Live Hypothesis
**Plan File:** `.agents/plans/smt-v2-decouple-active-position.md`
**Criteria Source:** Plan file (ACCEPTANCE CRITERIA AC1–AC11)
**Validated:** 2026-06-10

---

### Results

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| AC1 | Freeze at fill (all four paths) writes the six Contract-A fields, ladder from post-recompute hypothesis | PASS | strategy.py stop-entry (after recompute) + market-entry call `smt_state.freeze_active_mgmt(position["active"], direction, hypothesis)`; live_orders `_register_downgraded_fill` (reloads hyp post-recompute) + `place_market_entry` freeze. Tests: `test_stop_entry_fill_freezes_all_six_fields`, `test_market_entry_fill_freezes_all_six_fields`, `test_downgrade_fill_freezes_fields`, `test_place_market_entry_freezes_fields`, `test_fill_freezes_mgmt_fields`. |
| AC2 | trend.py manages off frozen snapshot; none/global-trend reset skipped while active | PASS | trend.py resolver (L288-317) + Step-3 shadow (L375-388); `if direction=="none" and not active` (L325); global-trend gate `and not active` (L336). Tests: `test_trend_manages_when_hypothesis_none`, `test_global_trend_reset_skipped_when_active`, `test_ath_secondary_uses_frozen_lv2`. |
| AC3 | No force-close on mismatch (automatic) — returns None, position preserved | PASS | strategy.py 3.1 reduced to `return None` (no market-close). Tests: `test_in_position_direction_mismatch_preserves_automatic_position`, `test_in_position_direction_none_preserves_automatic_position`, `test_automatic_position_preserved_on_mismatch`. |
| AC4 | Manual position still untouched on mismatch | PASS | Same `return None` path covers manual. Test: `test_in_position_manual_entry_preserved`. |
| AC5 | Pending-stop-entry cancel preserved (direction-changed / direction-none) | PASS | strategy.py 2.1 (L300-312) untouched. Tests: `test_pending_stop_entry_cancel_on_direction_change`, `test_pending_stop_entry_cancel_on_direction_none`. |
| AC6 | Frozen ladder immutable across later recompute / hypothesis change | PASS | freeze writes once at fill; resolver reads `active.*`. Tests: `test_frozen_ladder_not_overwritten_by_recompute`, `test_frozen_ladder_survives_hypothesis_direction_change`. |
| AC7 | Correct-side management after a flip (break/arm comparators on frozen side) | PASS | `direction = mgmt_direction` shadow drives closures/break checks. Tests: `test_trend_manages_when_hypothesis_flipped_opposite`, `test_trend_break_check_correct_side_after_flip`. |
| AC8 | Normal-case byte-equivalence; flip yields identical signals | PASS | Resolver is value-identical when frozen==live. Tests: `test_normal_trade_management_byte_equivalent`, `test_flip_does_not_change_normal_management` (asserts `result == baseline`). |
| AC9 | Back-compat — legacy active (no frozen fields) managed via fallback, no crash | PASS | Resolver `active.get("mgmt_direction") or _norm(active.get("direction"))` + ladder fallback to live hypothesis. Test: `test_legacy_active_without_frozen_fields_managed_via_fallback`. |
| AC10 | Production silence + scope (only the named files; Step-4 + entry logic unchanged; hypothesis.py doc-only) | PASS | No new print/stdout. `git diff` touches only smt_state.py, strategy.py, live_orders.py, trend.py + named test files. hypothesis.py & session_pipeline.py UNCHANGED (confirmed via git diff --stat; recompute_cautious_for_fill already mutates only hypothesis, no edit needed). Step-4 (L629+) untouched. |
| AC11 | Suite green; every changed branch has a named test | PASS | `uv run python -m pytest tests/ -q` (integration excluded by addopts): 1284 passed, 24 pre-existing fail (unrelated files), 0 net-new failures vs baseline. Coverage table tests all present incl. `test_ath_secondary_uses_frozen_lv2`. |

---

### Summary

**PASS:** 11
**FAIL:** 0
**PARTIAL:** 0
**UNVERIFIABLE:** 0
**Total:** 11

**Overall verdict:** ACCEPTED

---

### Notes
- `test_ib_realtime.py::test_gap_fill_not_called_from_start` hangs on a real `sleep` (environmental); excluded from suite runs. Unrelated to this changeset.
- The 24 pre-existing failures are identical pre- and post-change (slippage, parquet-session, hypothesis-API, automation session-end, orchestrator no-api-key) — none in files this changeset touches.

Overall: ACCEPTED — all acceptance criteria met, ready for review.
