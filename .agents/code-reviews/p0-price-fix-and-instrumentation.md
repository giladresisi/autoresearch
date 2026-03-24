# Code Review: P0 Price Fix and Instrumentation

**Date:** 2026-03-24
**Plan:** `.agents/plans/p0-price-fix-and-instrumentation.md`
**Reviewer:** AI Code Review (claude-sonnet-4-6)

---

## Stats

- Files Modified: 11
- Files Added: 0
- Files Deleted: 0
- New lines: ~456
- Deleted lines: ~182

---

## Summary

The P0 changeset correctly implements four tightly-coupled changes: the `price_10am → price_1030am` rename with semantic fix (P0-A), MFE/MAE tracking per trade (P0-B), exit_type tagging (P0-C), and R-multiple per trade (P0-D). The GOLDEN_HASH is valid (recomputed and verified). The core logic is sound and no security issues exist. Six issues were found, ranging from a medium-severity logic undercount in MFE/MAE on partial closes, to low-severity test gaps and a documentation-level acceptance criterion miss.

---

## Issues

---

```
severity: medium
file: train.py
line: 533
issue: Partial close MFE/MAE is computed before today's price is tracked
detail: The MFE/MAE tracking update (high_since_entry / low_since_entry) happens in step 4
  (mark-to-market loop, lines 601-602), AFTER step 2 where the partial close fires. When the
  partial close triggers on day D, _mfe_mae(pos) is called with high_since_entry that does
  NOT include day D's price. This means for a partial that fires on the first +1 ATR day, the
  partial record's mfe_atr will be 0.0 (no price above entry yet tracked) even though
  exit_price clearly shows a gain. The stop_hit and end_of_backtest records are unaffected
  because they see the fully-updated high/low from all prior mark-to-market passes.
suggestion: Move the high/low tracking update to step 2 (position management loop), updating
  pos["high_since_entry"] and pos["low_since_entry"] from price_1030am immediately after it is
  read at line 520, before the partial close block. Alternatively, pass price_1030am into
  _mfe_mae and take max(pos.get("high_since_entry", entry), price_1030am) inline.
```

---

```
severity: medium
file: train.py
line: 494-496
issue: Inconsistent initial_stop access pattern in stop_hit r_multiple guard
detail: The denominator guard reads pos.get("initial_stop", pos["stop_price"]) on line 496,
  but the numerator on line 494 reads pos["initial_stop"] directly (no .get). If initial_stop
  were somehow absent, line 494 would raise KeyError before the guard on 496 is evaluated.
  This is a correctness hazard even though initial_stop is always set at entry (line 586) for
  new positions — it could bite if the code path is ever reached with a legacy position that
  lacks the key (e.g., a position entered before P0 was deployed, or in a unit test that
  constructs a minimal pos dict).
suggestion: Make both sides of the guard consistent:
    _r = round((pos["stop_price"] - pos["entry_price"]) /
               (pos["entry_price"] - pos.get("initial_stop", pos["stop_price"])), 4) \
         if (pos["entry_price"] - pos.get("initial_stop", pos["stop_price"])) > 0 else ""
  The same inconsistency is NOT present for end_of_backtest (line 618 uses .get on both sides)
  but IS also present for partial (line 534-536 uses .get on the guard line but .get on the
  numerator too — so partial is actually consistent and safe). Only the stop_hit block
  (line 494) has the bare pos["initial_stop"].
```

---

```
severity: low
file: tests/test_v4_b.py
line: 323-328
issue: test_program_md_walk_forward_windows_default_is_7 assertion is too weak
detail: The third branch of the assertion at lines 326-328 succeeds if "WALK_FORWARD_WINDOWS"
  and "7" simply both appear anywhere in program.md — even if WALK_FORWARD_WINDOWS = 77 or
  WALK_FORWARD_WINDOWS = 17. The test was written to be forgiving, but 'WALK_FORWARD_WINDOWS'
  and '7' co-occurring is essentially always true (e.g. the doc might say
  "WALK_FORWARD_WINDOWS = 70" and the test would still pass).
suggestion: Tighten to require the value 7 specifically:
    assert 'WALK_FORWARD_WINDOWS = 7' in text or 'WALK_FORWARD_WINDOWS=7' in text, \
        "Expected WALK_FORWARD_WINDOWS with value 7 in program.md"
  The fallback `or ('WALK_FORWARD_WINDOWS' in text and '7' in text)` clause should be removed.
```

---

```
severity: low
file: tests/test_v4_b.py
line: 379-390
issue: test_mfe_atr_positive_for_winning_trade may produce mfe_atr == 0 due to the partial-close ordering bug
detail: _make_trade_run patches manage_position to return pos['stop_price'], so the position
  runs to end_of_backtest. With prices_after_entry=[102, 105, 108, 110, 110] and manage_position
  frozen, high_since_entry is updated daily in the mark-to-market step — but on the very first
  day (price=102), high_since_entry is initialized to entry_price=100 and then updated to 102
  in the MTM step. This is fine for end_of_backtest records. However, if the test is run
  against a code base where the medium-severity ordering bug (above) is not fixed, and a
  partial fires, the partial's mfe_atr would be 0.0, which could cause this test to mislead.
  The test itself is logically correct as written (no partial fires because price never reaches
  entry+atr14=105 before rising to 110, and partial fires when price >= 100.03+5=105.03 which
  DOES fire on day 2, price=105). Actually this means a partial DOES fire and its mfe_atr is
  computed before the MTM update — so eob[-1]["mfe_atr"] > 0 passes (EOB record is fine) but
  the partial record has mfe_atr=0 without the bug fix. The test only checks EOB records so it
  passes currently, but the presence of a partial record with mfe_atr=0 is a latent
  inconsistency not caught by the test suite.
suggestion: Add an assertion that partial records also have mfe_atr > 0 when price moved up:
    partials = [r for r in records if r.get("exit_type") == "partial"]
    for r in partials:
        assert r["mfe_atr"] > 0, f"Partial record has mfe_atr=0 despite price moving up: {r}"
  This test would fail, surfacing the medium-severity bug above for fixing.
```

---

```
severity: low
file: tests/test_v4_b.py
line: 411-417
issue: test_partial_exit_type_unchanged does not assert partials > 0
detail: The test comment says "partial fires when price >= entry + 1 ATR; rising dataset should
  produce at least one" and iterates over partials, but if partials is empty the for loop is a
  no-op and the test passes silently. With entry=100.03, atr=5, trigger=105.03 and
  prices_after_entry=[106, 106, 106], the partial should fire, but if the trigger condition
  changes the test would still pass vacuously.
suggestion: Add `assert len(partials) >= 1, "Expected at least one partial record"` before the
  for loop to prevent silent vacuous pass.
```

---

```
severity: low
file: tests/test_v4_b.py
line: 382
issue: test_mfe_atr_positive_for_winning_trade uses prices that trigger a partial close, reducing coverage clarity
detail: With prices_after_entry=[102, 105, 108, 110, 110], price hits 105 on day 2 which is >=
  entry(100.03) + atr(5) = 105.03. This means a partial close fires and the final end_of_backtest
  record is for only 50% of the original shares. The test asserts mfe_atr > 0 on EOB records and
  this works, but the test's docstring says "prices rise from 100 to 110 → MFE = 2.0" which is
  incorrect (the partial fires at 105, MFE at partial time would be ~0 due to ordering bug, and EOB
  mfe is (110-100)/5=2.0 based on full-position history). The expected value stated in the comment
  doesn't match what the test actually verifies (it only asserts > 0, not == 2.0).
suggestion: Either use prices that do NOT trigger a partial (keep all prices below 105.03) so the
  test scenario matches the comment, e.g. prices_after_entry=[102, 103, 104, 104.9, 104.9], or
  update the docstring to accurately describe what is asserted.
```

---

## Non-Issues / Confirmed Correct

- **GOLDEN_HASH**: Recomputed and verified. Hash `6d6a86dbbd755a62c9c276eea83a4317b3ca9d588686b550344ad6989db2d6a3` matches current train.py immutable zone.
- **P0-A rename completeness**: `grep price_10am tests/` returns only `tests/test_selector.py:37` which is in an out-of-scope file not listed in this changeset. All in-scope test files use `price_1030am` correctly.
- **prepare.py**: Uses `Close` of 9:30 bar and names column `price_1030am`. Warning message updated. Logic is correct.
- **_mfe_mae helper**: Zero-ATR guard is correct. `pos.get("high_since_entry", pos["entry_price"])` fallback is safe and correct.
- **_write_trades_tsv fieldnames**: Expanded correctly to include exit_type, mfe_atr, mae_atr, r_multiple. `restval=""` ensures backward compat.
- **end_of_backtest r_multiple**: Uses `.get("initial_stop", pos["stop_price"])` on both guard and computation — consistent and safe.
- **partial r_multiple**: Uses `.get("initial_stop", pos["stop_price"])` on both sides — consistent and safe.
- **stop_hit pnl computation** (line 489): Uses `pos["stop_price"]` as exit — correct because stop_price is the filled price when stop is triggered.
- **R-multiple sign**: Stop-hit exits at or below entry produce non-positive r_multiple. End-of-backtest exits above entry produce positive r_multiple. Formula is semantically correct.
- **MFE/MAE signs**: MFE = (high - entry)/ATR >= 0 by construction (high_since_entry initialized to entry_price). MAE = (entry - low)/ATR >= 0 by construction. The `test_mae_atr_non_negative` test correctly validates this.
- **_fake_stats helper**: Includes all required keys including the new `avg_win_loss_ratio`. Pattern is sound.
- **strategies/ files**: `strategies/multisector_mar23.py` and `strategies/global_mar24.py` still contain `price_10am` but these are archived baseline strategy files not active in the current backtest path. They are NOT in the scope of this P0 changeset and do not affect correctness.

---

## Pre-existing Issues (Not Introduced by This Changeset)

- `tests/test_selector.py:37` — uses `"price_10am"` in a fixture. This file was not in scope for P0 and was pre-existing.
- 4 test files (test_prepare.py, test_selector.py, test_v3_f.py, test_e2e.py) have collection errors due to missing dependencies noted in prior execution reports.

---

## Acceptance Criteria Audit

Per the plan's acceptance criteria:

- [x] `prepare.py` uses `Close` of 9:30 AM bar, produces `price_1030am` column
- [x] Zero occurrences of `price_10am` in `prepare.py`, `train.py`, `test_prepare.py`, `test_optimization.py`, `test_e2e.py`
- [x] `trades.tsv` schema includes `exit_type`, `mfe_atr`, `mae_atr`, `r_multiple` (verified by `_write_trades_tsv` fieldnames)
- [x] All trade records have non-empty `exit_type` in `{"stop_hit", "end_of_backtest", "partial"}`
- [~] MFE >= 0 and MAE >= 0 for all records — MAE/MFE >= 0 holds by construction, but partial record `mfe_atr` will be 0 even when price moved favorably before partial (ordering bug above)
- [x] R-multiple sign is correct for profitable vs losing exits
- [x] `test_harness_below_marker_matches_golden_hash` passes
- [ ] Full test suite passes — not run in this review session; execution report claims 100% pass rate
- [x] PROGRESS.md updated with cache invalidation action note
