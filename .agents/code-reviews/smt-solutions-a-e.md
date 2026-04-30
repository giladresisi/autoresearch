# Code Review: smt-solutions-a-e

**Date**: 2026-04-21  
**Plan**: `.agents/plans/smt-solutions-a-e.md`  
**Branch**: master

## Stats

- Files Modified: 4 (strategy_smt.py, backtest_smt.py, signal_smt.py, tests/test_smt_strategy.py, tests/test_smt_backtest.py)
- Files Added: 0
- New lines: ~576
- All 104 tests pass

---

## Issues Found

---

```
severity: medium
file: backtest_smt.py
line: 640-658
issue: _pending_fvg_zone and _pending_fvg_detected not updated when replacement fires
detail: The replacement block updates 14 pending-state variables when a new divergence
  displaces the current pending hypothesis. However, _pending_fvg_zone and
  _pending_fvg_detected are not refreshed. If a short divergence is replaced by a long
  divergence (opposite direction, Rule 4), the FVG zone from the original short direction
  is carried forward into the new long hypothesis. When the entry eventually fires it passes
  this stale fvg_zone to _build_signal_from_bar, which stores it on the position dict. Layer B
  management (TWO_LAYER_POSITION + FVG_LAYER_B_TRIGGER) would then target the wrong FVG zone.
  Currently dormant because FVG_ENABLED and TWO_LAYER_POSITION both default to False, but the
  bug will activate whenever those features are turned on alongside Solutions A/B.
suggestion: After the `_new_ac is not None` guard inside the replacement block, add:
  _pending_fvg_zone = detect_fvg(mnq_reset, bar_idx, _nd_dir)
  _pending_fvg_detected = _pending_fvg_zone is not None
  This mirrors what the IDLE block does when a divergence is first accepted (lines 971-972).
```

---

```
severity: low
file: backtest_smt.py
line: 872-873
issue: Comment says "session extremes undefined with <4 data points" but guard allows bar_idx=3 (only 3 prior bars)
detail: The comment at line 872 states "skip first 3 bars — session extremes undefined with <4 data
  points." At bar_idx=3 (the first allowed bar), _smt_cache contains data from bars 0, 1, and 2
  — exactly 3 prior bars, not 4. The guard is therefore off-by-one relative to its own
  documentation. The code behavior (skipping bars 0, 1, 2 and allowing detection starting at
  bar_idx=3) is a valid strategy choice and produces the desired effect. The bug is in the
  comment, not the logic.
suggestion: Correct the comment to: "skip first 3 bars — at bar_idx<3 the session extreme cache
  has fewer than 3 prior bars, making max/min comparisons noise-sensitive."
```

---

```
severity: low
file: tests/test_smt_backtest.py
line: 488-596
issue: Replacement rule tests (Rules 1-4, test_replacement_full_state_reset) do not exercise
  the actual replacement branch in run_backtest — they replicate the logic inline
detail: Tests 11-15 (Rule 1-4 and full_state_reset) verify the correctness of the if/elif
  decision tree by re-implementing it in test code, not by calling run_backtest with synthetic
  bar data that would trigger replacement. test_replacement_full_state_reset mutates a plain
  dict to prove that Python assignment works. As a result, a regression in the actual
  replacement branch (e.g., forgetting to update _pending_smt_type) would not be caught by
  any existing test. The _pending_fvg_zone gap found above is one such regression that the
  current tests cannot detect.
suggestion: Add at least one integration test that constructs a two-day synthetic session
  where (a) a weak displacement divergence is accepted on bar N, then (b) a stronger wick
  divergence fires on bar N+5, and assert the resulting trade's smt_type == "wick" and
  divergence_bar == N+5. This would prove the replacement branch executes in run_backtest
  and correctly overwrites all pending state.
```

---

```
severity: low
file: tests/test_smt_strategy.py
line: 1224-1308
issue: Inverted stop guard tests use manual try/finally to restore module globals instead of monkeypatch
detail: The three test_inverted_stop_guard_* tests set module-level constants directly
  (sm.STRUCTURAL_STOP_MODE = True) and restore them in a finally block. This is functionally
  safe in Python (finally always executes), but diverges from the project pattern of using
  monkeypatch for all other tests in this file. If a future refactor wraps these in a class or
  changes the test lifecycle, the manual restore pattern is more fragile than monkeypatch, which
  integrates with pytest's fixture teardown.
suggestion: Convert to monkeypatch:
  monkeypatch.setattr(sm, "STRUCTURAL_STOP_MODE", True)
  monkeypatch.setattr(sm, "STRUCTURAL_STOP_BUFFER_PTS", 0.0)
  monkeypatch.setattr(sm, "TDO_VALIDITY_CHECK", False)
  monkeypatch.setattr(sm, "MIN_STOP_POINTS", 0)
  This removes the try/finally block and keeps teardown in pytest's control.
```

---

## Confirmed Correct — Items Verified During Review

**ADVERSE_MOVE_FULL_DECAY_PTS=0 guard**: `_effective_div_score` uses `max(ADVERSE_MOVE_FULL_DECAY_PTS, 1.0)` as the divisor. Setting the constant to 0.0 does not cause a ZeroDivisionError; it caps the divisor at 1.0 and produces `move_decay = ADVERSE_MOVE_MIN_DECAY`. Safe.

**bar_idx < 3 guard (Solution E)**: Logic is correct. Bar_idx 0, 1, 2 are skipped; first detection attempt is at bar_idx=3. The session extreme cache at that point contains 3 prior bars, which is sufficient for meaningful comparisons. The comment has a wording issue (documented above) but the code behavior is correct.

**_pending_div_score initialization**: All four new pending-state variables (`_pending_div_score`, `_pending_div_provisional`, `_pending_discovery_bar_idx`, `_pending_discovery_price`) are initialized in the day-boundary reset block (lines 347-350). The WAITING_FOR_ENTRY replacement block is only reachable after transitioning from IDLE (which sets `_pending_div_score` at lines 1013-1027 immediately before changing state). First-access is always safe.

**Replacement rules 1-4 logic**: The if/elif chain is internally consistent. Displacement cannot displace wick/body (Rule 1 catches it). Wick/body always displaces provisional displacement (Rule 2). Same-direction upgrade requires strictly higher score (Rule 3). Opposite-direction requires score > effective * REPLACE_THRESHOLD=1.5 (Rule 4). No overlapping cases; no unreachable branches.

**Solution C (inverted stop guard) placement**: The guard fires after stop calculation and before MIN_STOP_POINTS, which is the correct order. Both STRUCTURAL_STOP_MODE and ratio-based paths are covered. For ratio-based stops with TDO_VALIDITY_CHECK=False and zero TDO distance, stop equals entry and is correctly rejected.

**HYPOTHESIS_INVALIDATION_PTS reset path**: After invalidation, `continue` skips the confirmation bar check. State is correctly set to IDLE with pending_direction=None and anchor_close=None before the continue. No fallthrough possible.

**MIN_DIV_SCORE gate consistency**: Applied at IDLE acceptance (line 1020) and at WAITING_FOR_ENTRY replacement (line 620). Both gates use the same threshold. Consistent behavior.

**All 104 tests pass**: No pre-existing failures. All new tests are passing.
