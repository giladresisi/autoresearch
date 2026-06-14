# Feature: Per-ticker liquidity-level invalidation / depletion retirement (R2)

> **Source of truth: Linear GIL-25** — https://linear.app/gilad-resisi/issue/GIL-25
> Read the issue AND its comments first (esp. the 2026-06-13 re-arm refinement: fixed levels
> re-arm and may fire repeatedly *until invalidated* — invalidation, not the first fire, is
> terminal). This file is a thin runbook; the full spec/design lives in GIL-25.

- **Branch:** `autoresearch/smt-level-invalidation` (off `live` @ f475455 — has R1 unidirectional
  + master SMT-v2 merge + R3 1m-cadence detection + the keep:True FVG fix).
- **Status:** spec-only / handoff. A separate agent implements here. Nothing committed yet.
- **Foundation for:** GIL-26 (R4) — R4's FVG-edge invalidation reuses this machinery. Land R2
  first; R4 plugs FVG edges into it.
- **Shadow change:** SMT detection does NOT drive hypothesis/entry yet → **verify by the
  SMT-div stream, NOT P&L.** An A/B P&L delta is meaningless here; do not use it as the pass
  criterion.

## What to build (summary — full detail in GIL-25)
Track per-ticker level state: mark a level "destroyed/invalidated" for a ticker once that
ticker runs a confirmed HH/LL **well beyond** it (reuse the existing `FULFILL_PTS` /
`INVALIDATE_PTS` tier thresholds in `smt_detect.py`). Fan out to **three** consumers:
(1) SMT detection `smt_detect.py::_detect_level_smts` — skip a level if **either** ticker has
it invalidated; (2) cautious-target selection; (3) hypothesis target lists. Also change the
**fixed-level re-arm rule** (GIL-25 2026-06-13 comment): a fixed level re-arms on a fresh
re-visit (depart-then-return) and may fire again **until invalidated** — replace the current
"single fire ever" (`smt_detect.py` re-arm only `kind_cls=="dynamic"`, ~line 348). Distinguish
from the existing Part-A adverse-run `invalidated` flag (that invalidates a fired SMT *record*;
this invalidates the *level*).

## Code anchors (backtest vs live)
Backtests run `regression.py` → `backtest_smt.run_backtest_v2` (SimulatedBrokerExecutor); live
runs `automation.main`. Detection is shared. Anchors: `smt_detect.py::_detect_level_smts`
(direction/fire/re-arm; the dynamic-only re-arm gate ~348), `FULFILL_PTS_*` / `INVALIDATE_PTS_*`
(reuse for the "well beyond" threshold), `eligible_levels` (recency gate); cautious-target +
hypothesis-target assembly in `session_pipeline.py` (consumers to gate). State: `daily.json`
(`liquidities`, `liquidities_mes`, universe keys), `smts.json` (detect_state).

## Runbook
- **A — Plan.** Turn GIL-25 + this file into `.agents/plans/<slug>.md` (`/plan-feature` if
  large/cross-cutting); stamp EXECUTION_MODE + executor directive.
- **B — Implement.** Per the plan; leave changes UNSTAGED. Skip IB-touching tests
  (`--ignore=tests/test_ib_realtime.py --ignore=tests/test_ib_integration.py`); the 24
  master-inherited wall-clock/V1 failures are pre-existing (flag only NEW failures).
- **C — Regression (SMT-stream, NOT P&L).** Run a 1s **and** 1m regression for 2026-06-12
  (`run_regression(dates=["2026-06-12"], mode="1s"/"1m", skip_lock=True)`). Inspect the
  smt-div stream + `daily.json`.
- **D — Verify (SMT-stream).** At each occurrence below assert the desired SMT-stream effect
  (the spurious SMTs are gone). Write `experiment-verification.md`.
- **E — Notify** the user with the one-line verdict + comment on GIL-25.

## Example Occurrences
| date | time (ET) | source | current behavior | desired behavior | window |
|---|---|---|---|---|---|
| 2026-06-12 | 05:35 | live-session:2026-06-12 | spurious bullish `asia_high` SMT (lead=mnq): MES is far above its asia_high 7421.5 (cleared it earlier) while MNQ oscillates at its asia_high 29647.5 → false divergence | with per-ticker invalidation, MES's asia_high is retired → the pair is skipped → **no `asia_high` SMT fires at 05:35** | ±8m |
| 2026-06-12 | 04:19 | live-session:2026-06-12 | `prev1_day_high` SMTs fire @ 04:19 (wick) / 04:20 (body) though both tickers already crossed below it (depleted pool) | the depleted prev1_day_high is invalidated/retired → **no prev1_day_high SMT fires @ 04:19/04:20** | ±8m |
| 2026-06-12 | 00:05 | live-session:2026-06-12 | `prev2_day_high` SMTs fire @ 00:05/00:06 though price is already above it (depleted) | invalidated/retired → **no prev2_day_high SMT @ 00:05/00:06** | ±8m |

## Acceptance criteria
- [ ] Per-ticker level invalidation state; a level is skipped in `_detect_level_smts` if either
      ticker has it invalidated.
- [ ] Fan-out to cautious-target selection + hypothesis target lists (invalidated levels dropped).
- [ ] Fixed-level re-arm: fires repeatedly on fresh re-visits until invalidated (no longer
      single-fire-ever); invalidation is terminal.
- [ ] 2026-06-12 1s+1m regression: the spurious SMTs above (asia_high 05:35, prev1_day_high
      04:19/04:20, prev2_day_high 00:05/00:06) no longer fire.
- [ ] Unit tests for invalidation + re-arm; SMT/pipeline suite green except known pre-existing
      failures.

## Out of scope
- GIL-26 (R4) FVG-edge levels (separate worktree; depends on this). Wiring SMTs into
  hypothesis/entry direction. Any P&L-affecting change (shadow detection only).
