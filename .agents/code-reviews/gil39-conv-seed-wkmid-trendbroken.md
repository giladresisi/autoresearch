# Code Review — GIL-39 conviction-seed (Change A) + weekly-mid trend-broken suppression (Change B)

Worktree: `C:\Users\gilad\projects\auto-co-trader\smt-conv-seed-wkmid-trendbroken`
Reviewed: UNSTAGED changes vs HEAD. Scope: `smt_conviction.py`, `session_pipeline.py`, `trend.py`, `tests/test_smt_conviction.py`, `tests/test_smt_trend.py`.

**Stats:**
- Files Modified: 5 (production: 3, tests: 2) + `feature.md` (doc, not reviewed)
- Files Added: 0 code (1 plan md, not reviewed)
- Files Deleted: 0
- New lines: 365 (code+tests); Deleted lines: 2 (trend.py guard rewrites)

## Verdict
Code review passed. No correctness, security, or standards issues detected. All four key invariants verified. 56/56 affected unit tests pass.

## Invariant verification

1. **Both flags default OFF; byte-identical baseline.** CONFIRMED.
   - `smt_conviction.py:39` `SMT_CONVICTION_SEED_CARRY: bool = False`; `seed_from_standing` returns `list(prev or [])` at :408-410 before any mapping when OFF.
   - `trend.py:54` `SUPPRESS_WEEKLY_MID_TREND_BROKEN: bool = False`; `_suppress_weekly_mid` (:290) is then always `False`, so both guards (`:493`, `:808`) become `... and not False` = behaviorally identical to master.
   - session_pipeline OFF path: the new block loads the hypothesis and writes `smt_conviction_set` back to a value-equal copy (`seed_from_standing` no-op), leaving persisted state identical to what `_ingest_pending_smts` produced. The extra load/save is inert in the in-memory backtest store.

2. **`seed_from_standing` total / fail-safe.** CONFIRMED. Whole body wrapped in `try/except Exception -> return list(prev or [])` (:407, :503-505). Degenerate input handled: non-dict survivors skipped (:415), missing ref_name/direction skipped (:419), non-numeric `mnq_price` coerced to 0.0 (:425-428), missing `keys` reconstructed (:430-431). Schema of seeded records exactly matches `update_standing`'s new-rec schema (`smt_conviction.py:189-202`), so the seed is a valid `prev` for the per-bar `update_standing` (`session_pipeline.py:2333`).

3. **Consensus gate.** CONFIRMED correct. `_SEED_STRONG_TIERS = {ATH, week, day}` (:380); gate keeps the seeded set iff `top_tier in strong OR n_concur >= 2` (:465), else returns `base` (prev only). `top_tier` from the same `_rank` map used by `conviction_score` (:450 vs :330). `n_concur = max(n_bull, n_bear)` over the dominant sign (:464). A single day-tier carried fill (explicit `tier="day"` wins in `_tier_of`) passes via the strong-tier branch — matches `test_seed_from_standing_fills_only`; a single bare `fill`/`session` record is dropped — matches `test_seed_from_standing_single_fill_tier_dropped` / `_weak_single_session_dropped`.

4. **No print/stdout in production paths.** CONFIRMED (grep clean; lone match is a docstring at `smt_conviction.py:305`).

## Other checks
- **No mutation of caller `prev`/`survivors`.** Merge indexes `base` via `dict(r)` copies (:478); survivors mapped into fresh dicts (:432-443). The off-path `list(prev or [])` is a shallow copy (records shared) but the caller re-assigns it to the same key — no aliasing hazard.
- **Import-inside-function** (`import smt_conviction as _smt_conv` at the two call sites) matches the existing per-bar convention at `session_pipeline.py:2316`; not a new anti-pattern.
- **`_smt_state` alias** is imported at module level (`session_pipeline.py:18`); both new call sites use it correctly.
- **trend.py signal shape** unchanged; Step-4 flat path emits `level_name="weekly_mid"` (`:823`), exercised by the flat-position tests.
- **Tests** cover: flag-off no-op, level consensus, fills-only (required), single-session drop, single-fill drop, prev-merge collapse (Change A); default-on fires / suppressed-when-on / daily-mid-still-fires (Change B). Good coverage incl. the required fills-only case.

## Non-blocking notes (informational, no action required)
- The OFF-path extra `load_hypothesis()/save_hypothesis()` in `session_pipeline.py` (:749-755, :774-780) is a redundant round-trip when the flag is OFF. It is inert (value-identical write) and consistent with the "guard lives inside the helper, call site stays unconditional" design choice in the plan, so it is acceptable. If a future maintainer wants zero extra I/O on the OFF path, the load/save could be skipped when `not SMT_CONVICTION_SEED_CARRY` — purely an optimization, not a correctness concern.
