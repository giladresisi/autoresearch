# Code Review: tests/test_smt_limit_lifecycle.py + strategy_smt.py:390-392

**Date**: 2026-04-24
**Reviewer**: Claude Code (automated)
**Scope**: New test file for SMT limit-entry lifecycle feature; one-line deprecation comment on ENTRY_LIMIT_CLASSIFICATION_PTS

---

## Stats

- Files Modified: 1 (strategy_smt.py — deprecation comment only)
- Files Added: 1 (tests/test_smt_limit_lifecycle.py)
- New lines: ~1016 (test file) + 3 (deprecation comment)
- Deleted lines: 0

---

## Issues Found

---

```
severity: medium
file: tests/test_smt_limit_lifecycle.py
line: 716-731
issue: test_later_confirmation_fires_signal_after_valid_draw_exists does not match the plan's specified scenario
detail: The plan (Task 4.1, Item 8) specifies: "first confirmation has no valid draw; next confirmation (with different draws) fires signal successfully." The test instead drives bar 5 directly as a normal confirmation without first simulating a failed-draw confirmation bar. It does not exercise the divergence-preservation path between two confirmation attempts, which is the behavior the test name implies it covers. The test passes because bar 5 always fires a signal here — it is testing a trivially different path from test_no_valid_draw_preserves_divergence_and_state.
suggestion: Extend the test to match the plan: (1) monkeypatch _build_draws_and_select to return (None, None, None, None, []) for bar 5 only, assert state.scan_state remains WAITING_FOR_ENTRY, then (2) remove the monkeypatch and call bar 6 (or a second pass of bar 5) as a real confirmation that produces a signal. This proves the divergence-preserved state is still usable on a subsequent bar.
```

---

```
severity: medium
file: tests/test_smt_limit_lifecycle.py
line: 814-823
issue: test_dispatch_lifecycle_batch_emits_sub_events_in_order does not exercise the lifecycle_batch dispatch path in signal_smt.py
detail: The test calls _dispatch_event twice sequentially with two individual events, then checks output order. This tests nothing about the lifecycle_batch code path at signal_smt.py:825-833 (which loops over result["events"] and calls _dispatch_event for each). If the batch dispatch loop were accidentally reversed, or if the batch type were not handled at all, this test would still pass. The test name ("lifecycle_batch") creates a false confidence that this path is covered.
suggestion: Change the test to drive signal_smt._process_scanning or the equivalent batch-dispatch caller with a synthetic result of type "lifecycle_batch", or minimally pass {"type": "lifecycle_batch", "events": [evt1, evt2]} directly to whatever dispatcher function handles it at the top level. Alternatively rename the test to reflect what it actually tests: test_dispatch_event_ordering_for_two_individual_events.
```

---

```
severity: medium
file: tests/test_smt_limit_lifecycle.py
line: 994-1015
issue: test_backtest_lifecycle_batch_unwrapping is a static assertion, not a backtest regression
detail: The plan (Task 4.1, final integration tests) specified test_backtest_unaffected_by_new_events as "run a known-good backtest scenario through backtest_smt; assert trade record count + P&L unchanged." The test that shipped instead constructs a hard-coded dict and manually simulates the unwrapping logic inline — it does not import or call backtest_smt at all. This does not verify that backtest_smt.py actually handles lifecycle_batch without leaking lifecycle events into trade records, which was the plan's intent.
suggestion: Import backtest_smt and drive its scan loop with a synthetic bar sequence that produces a lifecycle_batch (limit-mode with a forward fill). Assert that only the "signal" event inside the batch contributes to a trade record, and that the trade count and fields match the non-lifecycle-batch equivalent. If a full session drive is too expensive, patch _session_bars and run a minimal 6-bar synthetic sequence.
```

---

```
severity: low
file: tests/test_smt_limit_lifecycle.py
line: 551-573
issue: test_limit_expired_emitted_on_timeout asserts key presence but not missed_move value
detail: limit_missed_move will be 0.0 in this scenario (short trade, bar 6 has lows above entry_price, so pls["entry_price"] - bar["Low"] is negative and max(0.0, negative) = 0.0). The assertion only checks "limit_missed_move" in result — it would pass even if the value were None or -inf. A zero missed_move on expiry is technically valid but signals a test where the market never moved toward entry at all, which is an unusual expiry scenario that hides a latent arithmetic issue if the sign convention changes.
suggestion: Either (a) set up bar 6 so price moves partially toward entry (e.g. low just above entry) so missed_move is positive and assert the numeric value, or (b) add assert result["limit_missed_move"] >= 0.0 to at least rule out negative values.
```

---

```
severity: low
file: tests/test_smt_limit_lifecycle.py
line: 90-122 (_patch_base)
issue: _patch_base does not disable HIDDEN_SMT_ENABLED or EQH_ENABLED
detail: Both are True by default. EQH_ENABLED=True causes detect_eqh_eql to run and inject EQH/EQL levels into the draws dict via context.eqh_levels / context.eql_levels. In the current tests this is harmless because MIN_TARGET_PTS=0.0 in _patch_base means any EQH/EQL draw passes, and SessionContext is constructed without eqh_levels/eql_levels (so they default to None/[]). However, if a future test constructs a richer context, or if the EQH/EQL detection path is modified, these flags could produce unexpected draw candidates. HIDDEN_SMT_ENABLED=True enables close-based SMT detection in expanded-reference-level checks — also harmless here since EXPANDED_REFERENCE_LEVELS=False, but adds non-obvious interaction surface.
suggestion: Add ("HIDDEN_SMT_ENABLED", False) and ("EQH_ENABLED", False) to the _patch_base list to make isolation explicit and future-proof.
```

---

```
severity: low
file: tests/test_smt_limit_lifecycle.py
line: 443-473
issue: test_move_limit_rate_limited_suppresses_event_but_updates_state has a conditional assertion
detail: Line 470-471 reads: "if result_bar6 is not None: assert result_bar6['type'] != EVT_LIMIT_MOVED". The test accepts result_bar6 being None as an equally valid outcome. While None is correct when rate-limited (no event emitted), the conditional means the test would also silently pass if the patched detect_smt_divergence never triggered the replacement logic at all, producing None for a different reason. The test does not assert that a replacement was actually attempted and suppressed.
suggestion: Assert result_bar6 is None unconditionally (since rate-limiting suppresses the return), and separately assert state.last_limit_signal_snapshot != old_snap (to prove the state update happened despite the suppression). This eliminates the conditional and makes both behaviours explicit.
```

---

```
severity: low
file: tests/test_smt_limit_lifecycle.py
line: 459-463
issue: _patched_detect in test_move_limit_rate_limited retains wrong cache signature
detail: The real detect_smt_divergence signature at the call site (strategy_smt.py:1905) is detect_smt_divergence(mes_reset, mnq_reset, bar_idx, 0, _cached=smt_cache). The patched lambda uses (mes_df, mnq_df, bar_idx, offset, _cached=None) which matches, but it also calls real_detect with _cached=_cached. Since smt_cache was not captured, the real_detect call for bar_idx != 6 will use _cached=None instead of the actual smt_cache, potentially causing detect_smt_divergence to return stale or incorrect results for surrounding bars. In practice this is harmless for this short session, but it is a latent correctness issue.
suggestion: Capture smt_cache from the outer scope, or replace the real_detect fallthrough with a simple "return None" for all bar_idx != 6 (since no other bars should produce a divergence in this test).
```

---

## Missing Plan-Specified Tests

The following tests were enumerated in Task 4.1 of the plan but are absent from the file:

| Plan test name | Status |
|---|---|
| test_limit_placed_entry_price_is_anchor_plus_buffer_long | Missing — only the short variant was implemented |
| test_move_limit_not_emitted_when_entry_unchanged | Missing — no coverage of price-equality suppression logic |
| test_full_lifecycle_cancel_on_opposite_anchor (plan's TestFullLifecycle variant) | Partially covered by test_cancel_and_place_on_opposite_direction_replacement (unit) but no full-lifecycle integration version |
| test_signal_tagged_entry_market_when_buffer_none (plan name) | Covered, but as test_signal_tagged_with_none_fill_bars_in_market_mode (tests the indicator field, not the signal_type string directly) |

The missing `test_move_limit_not_emitted_when_entry_unchanged` is the most consequential: it is the only test that would catch a regression if the price-equality suppression at strategy_smt.py:1971-1975 were inadvertently removed.

---

## Positive Findings

- `_patch_base` correctly disables SYMMETRIC_SMT_ENABLED, SMT_OPTIONAL, SMT_FILL_ENABLED, HYPOTHESIS_FILTER, and all other strategy flags that could create false-positive divergences in unrelated test bars.
- The EVT_* constant tests are correctly matched against the implementation values.
- The ScanState slot tests verify both initial state and post-reset state, which is the right two-case coverage.
- `test_limit_placed_suppressed_when_tp_selection_fails` correctly verifies state.scan_state == "WAITING_FOR_ENTRY" (divergence preserved), which is the critical behavior from plan item 8.
- The formatter ASCII-safety assertions (`line.encode("ascii")`) directly address the 2026-04-23 UnicodeDecodeError from the live session — good defensive testing.
- JSON schema tests assert key presence and type, not just non-crash, which gives meaningful regression coverage.
- The full-lifecycle class (TestFullLifecycle) correctly chains bar-by-bar calls rather than testing individual functions in isolation.

---

## Note on strategy_smt.py:390-392 (ENTRY_LIMIT_CLASSIFICATION_PTS deprecation comment)

The change is:
```python
# DEPRECATED: no longer used by the classifier (signal_smt.py now derives signal_type
# from limit_fill_bars). Retained for optimizer compatibility only.
ENTRY_LIMIT_CLASSIFICATION_PTS: float = 5.0
```

This is correct and sufficient. The constant is still referenced in optimizer sweep configs that read module attributes; removing it would break those. The comment accurately states the reason for retention. No issues.
