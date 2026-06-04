# Code Review — Gate chop-zone market entries (o5-fallback + STP→MKT, R1/R2/R6)

Scope: unstaged diff in `strategy.py`, `execution/pickmytrade.py`, `tests/test_smt_strategy_v2.py`,
`tests/test_pickmytrade_executor.py`. Plan: `.agents/plans/gate-chop-zone-market-entries.md`.

## Stats (reviewed files only)
- Files Modified: 4
- Files Added: 0
- New lines: 579
- Deleted lines: 56

## Verdict: PASS — no blocking issues.

All focus areas verified correct:

- **R1 directional comparisons** (`pickmytrade.py:96-99`, `strategy.py:529-532`): both sides use
  `long: market >= entry` / `short: market <= entry`, with the `±5.0` slack dropped. Identical rule
  on both sides (Contract 1 satisfied). Boundary `market == entry` downgrades/market-fills (`>=`/`<=`),
  matching the plan. `_current > 0` guard preserved live.
- **Headroom math** (`strategy.py:213-229`): `headroom = abs(lvl - entry)`, `risk = abs(entry - stop)`,
  returns `headroom >= max(risk, MIN_HEADROOM_PTS)`; returns `True` when no opposing level ahead.
  `_nearest_opposing_level` correctly filters levels behind and picks nearest by distance.
  `_first_target_ahead` uses `min` for up / `max` for down. All guard `None` and missing keys.
- **o5-only gate guard placement**: both gates are guarded by `_conf_is_o5` and placed BEFORE any
  state mutation — market path at `:481` (before `position["active"]` at `:483` and `save_position`),
  stop path at `:549` (before `conf_bar_entry`/`save_position` at `:551-556`). `_conf_is_o5` is set
  only when `_find_last_bar` returns None and `_o5_fallback` supplies a bar (`:422-425`) — correct.
- **Resting stop fills NOT gated**: the gates live only in the placement/emission block (Section 2.3).
  The already-resting-stop fill path (`_entry_reached` / `fill_check_only`) returns at `:411-412`
  before reaching the gate. Confirmed.
- **`_session_mids` refactor**: byte-for-byte equivalent to the original Section-3 derivation
  (`_liq_map` over `kind=="level"`, `l["price"]`, mid = `None` if either bound missing). Section-3
  `reeval_after_stop` logic at `:607-617` unchanged in behavior. `(liquidities or [])` guard is
  additive and harmless (`_daily.get(...,[])` already returns a list).
- **`_gated` closure**: side-effect-free — only calls `_make_signal`, no position mutation, no save.
- **entry-gated safety**: `live_orders.dispatch_order` has no branch for `entry-gated`; it falls
  through to the log-only tail (`live_orders.py:583-585`) — NO broker order. `session_pipeline`
  emits/appends it but no position-mutating branch matches (`market-entry`/`stop-entry-filled` sets
  at `:792/794/824` exclude it). Verified safe.
- **conf/gated attribution**: `_make_signal(**kwargs)` cleanly merges `conf`/`gated`/`direction`/`stop`;
  no key collisions; fields propagate to `events.jsonl`.

## Test results
- New R1 tests (4) PASS; new headroom/gate/attribution tests in `test_smt_strategy_v2.py` PASS.
- Full `test_smt_strategy_v2.py`: all pass.

## Pre-existing failures (NOT introduced by this changeset)
8 failures in `tests/test_pickmytrade_executor.py` (slippage + modify_stop_entry suites:
`test_pmt_market_entry_long_slippage`, `_short_slippage`, `_zero_slip_ticks_mkt_still_applies_1tick`,
`test_modify_stop_entry_includes_sl`, `_sends_close_then_stop`, `_close_is_synchronous`,
`_replaces_even_if_close_fails`, `test_pmt_stop_entry_after_1100_applies_2tick_slippage`).
Verified pre-existing by `git stash` of the full working tree → same 8 fail on clean HEAD.
Root cause is unrelated to R1 (mocking/slippage assertions), not this feature. The 4 new `test_r1_*`
tests are independent and all pass.

## Minor observations (non-blocking)
- `STP_MKT_PROXIMITY_PTS` is now retained only as a documented contract anchor (no longer used by
  `will_market_fill`). This matches the plan's explicit instruction; intentional, not dead-code by
  oversight.
- Stop-entry o5 gate measures headroom from `entry_price` (possibly pushed by `MIN_APPROACH_PTS`),
  not `bar_mid`. This is correct: the resting-stop entry actually fills at `entry_price`.
