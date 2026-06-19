# Execution Report — SMT conviction-seed (carry) + suppress weekly-mid trend-broken (GIL-39)

Plan: `.agents/plans/smt-conv-seed-wkmid-trendbroken.md`
Execution mode: lightweight (sequential, implemented directly — no /plan-feature, no /execute).
Date: 2026-06-19
Status: DONE. All changes UNSTAGED; nothing committed or pushed.

## Summary

Two INDEPENDENT, default-OFF, flag-gated changes implemented per the plan:

- **Change A — `SMT_CONVICTION_SEED_CARRY`** (in `smt_conviction.py`): a new pure helper
  `seed_from_standing(prev, survivors, now_iso, *, require_consensus=True)` that maps carried
  survivor records (carried SMTs and fills, `_pending_entry_to_record`-shaped) into conviction
  records and merges them onto the prior standing-conviction set. Called at the two cold-start
  sites in `session_pipeline.on_session_start` (force-reset path and no-force path), after
  `_ingest_pending_smts` and before `run_hypothesis`, reading `smt_active_set` as the survivor
  source and writing `smt_conviction_set`. When the flag is OFF the helper returns the prior set
  unchanged (no-op).

- **Change B — `SUPPRESS_WEEKLY_MID_TREND_BROKEN`** (in `trend.py`): a `_suppress_weekly_mid`
  local (mirrors the `_suspend_daily_mid` idiom) ANDed (`and not _suppress_weekly_mid`) into the
  two weekly-mid invalidation guards in `run_trend` (flat-unarmed in-position path and the flat
  Step-4 scan). When the flag is OFF this is always False → both guards are byte-identical to
  master. Daily-mid and every other invalidation are untouched.

## Files changed

- `smt_conviction.py` — flag `SMT_CONVICTION_SEED_CARRY` (added after the tunable constants);
  `_SEED_STRONG_TIERS` frozenset; new `seed_from_standing` helper (appended after
  `conviction_score`).
- `session_pipeline.py` — two seed-call blocks: force-reset cold path (after
  `_ingest_pending_smts` at the force_reset branch) and no-force cold path (after the second
  `_ingest_pending_smts`).
- `trend.py` — flag `SUPPRESS_WEEKLY_MID_TREND_BROKEN`; `_suppress_weekly_mid` local next to
  `_suspend_daily_mid`; `and not _suppress_weekly_mid` added to the weekly-mid guard in the
  flat-unarmed path and the flat Step-4 scan.
- `tests/test_smt_conviction.py` — 6 Change-A tests appended (incl. the required fills-only case).
- `tests/test_smt_trend.py` — `TestSuppressWeeklyMidTrendBroken` (3 Change-B tests, incl. the
  required suppression case + daily-mid-still-fires).

## Divergences from the plan

- The plan names the trend function `update_position_for_trend`; the actual function is
  `run_trend` (a documented anchor drift). Edits applied to `run_trend`. No behavioral impact.
- Plan line anchors (`:482`, `:797`, etc.) had drifted by a few lines; edits applied at the
  correct, verified locations (weekly-mid guards now at `trend.py:493` flat-unarmed and
  `trend.py:808` Step-4 scan).
- One extra Change-A test was added beyond the enumerated set
  (`test_seed_from_standing_single_fill_tier_dropped`) to cover the lone `fill`-tier (not `day`)
  consensus-gate drop. Additive only.

## Tests

Baseline (pre-implementation), affected suites:
`tests/test_smt_conviction.py tests/test_smt_trend.py tests/test_smt_hypothesis.py` = 96 passed.

After implementation:
- `tests/test_smt_conviction.py tests/test_smt_trend.py` = 56 passed.
- `tests/test_smt_conviction.py tests/test_smt_trend.py tests/test_smt_hypothesis.py` = 105 passed
  (96 baseline + 9 new tests).
- Required-by-name: the 6 `seed_*` tests pass; `TestSuppressWeeklyMidTrendBroken` (3 tests) pass.
- Default-OFF assertions pass: `SMT_CONVICTION_SEED_CARRY is False`,
  `SUPPRESS_WEEKLY_MID_TREND_BROKEN is False`, and the OFF-no-op tests.

Full suite (`python -m pytest tests/ -q`): **2 failed, 1317 passed, 10 skipped, 12 deselected,
16 errors**.

Pre-existing baseline (verified by running the failing suites against a pristine `HEAD`
worktree): the **2 failures** (`tests/test_smt_fill_plot.py`) and **16 errors**
(`tests/test_smt_decouple_active.py`, all from `monkeypatch.setattr(trend, "load_daily", ...)` —
`trend` has no `load_daily` attribute at HEAD) are identical on pristine HEAD. They are NOT
caused by this change and do not touch any file in this change. NO new failures were introduced.

## Flags-OFF byte-identical

Confirmed at the unit level:
- `seed_from_standing` returns `list(prev or [])` unchanged when `SMT_CONVICTION_SEED_CARRY`
  is False (`test_seed_from_standing_off_is_noop`). On the OFF cold-start path the call rewrites
  `smt_conviction_set` to its own current value (default `[]`), preserving it.
- The trend.py weekly-mid guards reduce to `... and not False` when
  `SUPPRESS_WEEKLY_MID_TREND_BROKEN` is False — identical to master
  (`test_weekly_mid_trend_broken_default_off`).

The 1s-regression A/B byte-identical proof on a carry day is the regression-runner's Stage-C job
(not run here, per the lightweight executor scope).

## Code review (self-conducted; the dispatched review subagents produced no output and were
treated as unavailable)

No genuine issues found.
- Both flags default OFF; OFF path is value-preserving / byte-identical (verified above).
- `seed_from_standing` is total (try/except → returns prior set on any error; degenerate input
  guarded).
- Consensus gate matches the plan: drop seeded records unless
  `top_tier in {ATH, week, day}` OR `n_concur >= 2`.
- No print/stdout added; reuses existing module helpers (`_tier_of`, `_detect_key`,
  `_logical_key`, `_confirm_strength`, `_minutes_between`) so seeding is consistent with
  `update_standing`.
- Minor (non-issue, kept per the plan's prescribed call-site code): the OFF cold-start path
  performs one extra value-preserving `load_hypothesis`/`save_hypothesis` round-trip. Harmless.

## Acceptance criteria (self-validated against the actual code)

All PASS:
- Change A flag declared, default False; helper signature and record schema match
  (`ref_name, direction, side, tier, type, fire_iso, fire_close, adverse_streak:0,
  fulfilled_iso:None, keys`); OFF no-op; consensus gate; called at BOTH cold-start sites after
  `_ingest_pending_smts` and before `run_hypothesis`, reading `smt_active_set`, writing
  `smt_conviction_set`; required fills-only test present.
- Change B flag declared, default False; `_suppress_weekly_mid` computed next to
  `_suspend_daily_mid`; ANDed into BOTH weekly-mid guards; OFF byte-identical; required
  weekly-mid-suppression test + daily-mid-still-fires test present.

## Risks / follow-ups

- A's warmup-source facet is live-only; only the carry-seed-at-open path is regression-testable
  (noted in the project handoff memory).
- The seed source is `smt_active_set` (carried survivors merged by `_ingest_pending_smts`), which
  may also include warm-up actives — acceptable per the plan (they are equally "standing").
- Whole-day P&L effect of A and B is settled only by the 1s A/B regression (Stage C), not here.
