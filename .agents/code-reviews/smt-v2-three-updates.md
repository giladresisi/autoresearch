# Code Review — smt-v2-three-updates

**Stats:**
- Files Modified: 13 (6 production, 7 test)
- Files Added: 0 (production); plan file is the only untracked artifact
- Files Deleted: 0
- New lines: ~280
- Deleted lines: ~155

Production files reviewed in full: `daily.py`, `execution/pickmytrade.py`, `hypothesis.py`,
`session_pipeline.py`, `smt_state.py`, `strategy.py`.

## Verdict

Code review passed. No genuine technical issues detected. Notes below are confirmations
of correctness for the load-bearing points, plus one observation that was verified benign.

### Change 1 — Remove 4hr FVGs (keep 4hr BOS/CHoCH)
- `daily.run_daily_fixed` dropped the `hist_4hr` param and the `fvgs_4hr` detect/extend
  lines. The 1hr FVG path is untouched. All call sites updated (1 production, 16 test).
- `session_pipeline`: removed `_fvg_4hr` / `_fvg_done_4hr` members, the `_fvg_4hr_full`
  seeding block, and the `("4h", …)` tuple in `_extend_fvg_frames`. `_hist_4hr` (the BOS
  frame) is preserved and still passed to `run_hypothesis`.
- VERIFIED: `_hist_4hr` in `hypothesis._determine_direction` feeds only `mnq_4hr` → `b4hr` →
  `bos_score_4hr` (weight 0.65). No FVG consumption of 4hr in hypothesis.py, so its
  `hist_4hr` param correctly stays. `live_orders.py` 4hr resample feeds only the BOS path
  (`run_hypothesis(hist_4hr=…)`), so it correctly stays untouched.
- No dangling references to `_fvg_4hr` / `fvgs_4hr` remain in production code.

### Change 2 — 15:30 ET new-entry cutoff (PMT executor)
- `_NEW_ENTRY_CUTOFF = datetime.time(15, 30)` added near `_ET`.
- Guard added in `place_entry()` immediately after the existing `is_entry_allowed` block,
  using wall-clock `datetime.datetime.now(_ET).time()` (hoisted into `_now_et_time`, reused
  by both gates). Strictly-after semantics (`> _NEW_ENTRY_CUTOFF`) — 15:30:00 allowed,
  15:30:01+ blocked. Returns the identical `status="blocked"` FillRecord shape and sets
  `_entry_is_live=False`. No HTTP submit on block.
- Scope confirmed internal to `execution/pickmytrade.py`. `place_close`, `update_stop_loss`,
  `modify_stop_entry` do not pass through the gate (the SL-modify/close paths build their own
  payloads and call `_post_order` directly). No changes to strategy/dispatcher/session_times.
- `print()` calls match the existing house style in this module (other order paths already
  print). Not a new logging-policy violation introduced by this change.

### Change 3 — Dynamic cautious-target max-distance thresholds
- `cautious_dist_shrinks` added to `DEFAULT_POSITION`.
- `CAUTIOUS_DIST_SHRINK_PCT = 0.15`. `compute_cautious_prices` gains `dist_shrinks: int = 0`;
  computes `_factor = 0.85 ** max(0, dist_shrinks)`, `_sec_max`/`_init_max` floored at
  `CAUTIOUS_MIN_DIST`. All five threshold usages (3 secondary, 2 initial) now use the
  effective maxes; the terminal-fallback branch (which uses neither constant) is unchanged.
- VERIFIED byte-identical at `dist_shrinks=0`: `_factor=1.0` → `_sec_max=150`, `_init_max=110`
  (the exact constants), and the new default-kwarg call equals the explicit-0 call (test
  `test_cautious_dist_shrinks_zero_is_unchanged`).
- Increment only at the two real stop-out sites (`strategy.py:604`, `session_pipeline.py:895`),
  in lockstep with `failed_entries`. The liquidity-sweep DECREMENT site
  (`session_pipeline.py` ~L782, `failed_entries -= 1`) is intentionally NOT mirrored — the
  separate counter is keyed only off increments, per the locked decision.
- Reset at all three `failed_entries` reset points: `strategy.reset_position_for_session`,
  `strategy.reset_position_for_new_hypothesis`, and the `skip_position_reset` branch in
  `hypothesis.build_hypothesis_from_direction`.
- Threaded into both `recompute_cautious_for_fill` call sites (`strategy.py:405,497`,
  reading `position.get("cautious_dist_shrinks", 0)`) and the formation site
  (`hypothesis.py:1180`, reading the loaded position). Read happens BEFORE any increment, so
  the fill-time ladder reflects prior-failure tightening — correct.

## Observations (verified benign, no action)

```
severity: low
file: hypothesis.py
line: 1180
issue: build_hypothesis_from_direction now calls load_position() on every formation,
       including when direction=="none".
detail: An extra position read per 5m formation. compute_cautious_prices returns empties
        for direction "none" regardless, so the value is only consumed for up/down. The
        function already performs disk I/O (save_hypothesis), and load_position is the same
        cheap cached/disk read used elsewhere in this module. No correctness or measurable
        performance impact.
suggestion: None required. Acceptable as written.
```

## Pre-existing failures (NOT introduced by this changeset)

Baseline (captured before any edit, full suite minus the known-hanging IB test
`test_gap_fill_not_called_from_start`): **28 failures**. After this changeset the failure set
is **byte-for-byte identical** (`diff` of sorted FAILED lists = empty). Root causes are
environmental: a live IB connection on the machine (`test_ib_realtime.*`,
`test_automation_main.*`), slippage-config drift and close-then-stop sequencing
(`test_pickmytrade_executor::test_pmt_*_slippage`, `::test_modify_stop_entry_*`), missing API
key (`test_orchestrator_main`, `test_hypothesis_smt`), and parquet-merge env
(`test_check_session_parquets`). None touch the code paths changed here.
