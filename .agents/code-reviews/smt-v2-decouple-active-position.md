# Code Review — SMT-v2 Phase 1: Decouple Active-Position Management

**Plan:** `.agents/plans/smt-v2-decouple-active-position.md`
**Date:** 2026-06-10

## Stats
- Files Modified (production): 4 — `smt_state.py`, `strategy.py`, `live_orders.py`, `trend.py`
- Files Modified (tests): 3 — `tests/test_smt_state.py`, `tests/test_smt_strategy_v2.py`, `tests/test_smt_trend.py`
- Files Added: 1 — `tests/test_smt_decouple_active.py`
- Production lines: +151 / -18 (mostly comments + the resolver block)
- PROGRESS.md change is pre-existing (planning-phase note, not part of this changeset)

## Review Findings

### Correctness of the trend.py re-key (highest-risk surface)
- The frozen-snapshot resolver is computed once (after `active = position.get("active", {})`), BEFORE the Step-2 `none`/global-trend gates, so the gates can test `not active`. Correct ordering.
- Inside `if active:` the live symbols `direction`, `_lv1`, `_lv2`, `_cr1`, `_cr2`, `_ath_secondary`, `_mid_cross_guard`, `_weekly_mid_cross_guard` are shadowed to the frozen variants BEFORE the closures (`_surpassed`/`_close_beyond`/`_reversal`) are defined and before any break check — so every downstream read keys off the frozen snapshot. Verified by grep: no `cautious_initial_raw`/`cautious_secondary_raw` leak remains in Step-3; the only `_mid_cross_guard`/`_weekly_mid_cross_guard` uses inside Step-3 read the shadowed frozen values.
- `cautious_initial`/`cautious_secondary` now derive from `f_initial_raw`/`f_secondary_raw`. The frozen ladder values are either `""` or numeric (per `compute_cautious_prices`); `float(x) if x != "" else None` is safe for both. No type bug.
- The two ATH straddle flags (`_session_ath_straddle`/`_dynamic_ath_straddle`) are only consumed in Step-4 (L749/L768), after the `if active: … return None` block — they never interact with an active position, so leaving the ATH block on live `direction` is correct.
- `none`/global-trend gates now carry `and not active`. The intended behavior change: a `none` or flipped live hypothesis no longer short-circuits Step-3 management nor mutates hypothesis state under an open trade — the frozen snapshot manages it. This matches AC2/AC3 and the plan's locked decisions.

### Back-compat
- Resolver fallback `active.get("mgmt_direction") or _norm(active.get("direction",""))` and ladder `active.get(key, hypothesis_fallback)` handle legacy positions (no frozen fields). Verified by `test_legacy_active_without_frozen_fields_managed_via_fallback`.

### strategy.py
- Mismatch block reduced to a single `return None` (manual + automatic both fall through to no-op). The pending-stop-entry cancel (2.1) is untouched. The freeze is captured POST-recompute at both fill paths, per the locked capture-timing decision.

### live_orders.py
- `_register_downgraded_fill` reloads the hypothesis after `_recompute_cautious_at_fill` (which only re-anchors hypothesis.json, returns None) so the frozen ladder reflects the re-anchored values, then re-saves the pos. `place_market_entry` loads the live hypothesis and freezes (no recompute on the manual path — documented). `stop_entry_filled` correctly does NOT freeze (no `active` creation) — documented.

### smt_state.py
- `freeze_active_mgmt` is pure, None-tolerant (explicit-None → `""`), normalizes long/short→up/down, derives `backing_tier` from the frozen secondary level. `DEFAULT_POSITION` active-dict shape documented.

### Production silence
- No new `print`/stdout in production paths. Pre-existing prints in strategy.py L374/L378 and live_orders.py left untouched, per plan.

## Pre-existing Failures (NOT introduced by this changeset)
Baseline (HEAD 89a5e27, before edits) and post-change both show the identical 24 failures, in files this changeset does not touch: `test_pickmytrade_executor.py` (slippage/modify_stop_entry — flagged known/environmental), `test_smt_humanize.py` (slippage), `test_hypothesis_smt.py` (8 — API/direction), `test_check_session_parquets.py` (3), `test_automation_main.py` (2 — session-end, unicode-corrupted assertion), `test_orchestrator_main.py` (1 — no-api-key). `test_ib_realtime.py::test_gap_fill_not_called_from_start` hangs on a real sleep (environmental) — excluded from runs.

## Verdict
**Code review passed.** No genuine technical issues detected. The re-key is value-identical when frozen==live (byte-equivalence guarded by `test_normal_trade_management_byte_equivalent` + `test_flip_does_not_change_normal_management`). Full suite: 1284 passed / 24 pre-existing fail (zero net-new).
