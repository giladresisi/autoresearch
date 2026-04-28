# PROGRESS


## Feature: SMT Redesign — JSON-File Architecture + Module Decomposition

**Status**: Complete
**Plan File**: `.agents/plans/smt-redesign-v2.md`
**Spec**: `docs/superpowers/specs/2026-04-27-smt-redesign-design.md`

Rebuild the SMT control flow as four small modules (`daily.py`, `hypothesis.py`, `strategy.py`, `trend.py`) communicating via four JSON files (`global.json`, `daily.json`, `hypothesis.json`, `position.json`). New files alongside `strategy_smt.py` / `hypothesis_smt.py` (untouched). Reuses primitives (divergence, FVG, TDO, PDH/PDL, EQH/EQL, confirmation-bar) by import. Adds an additive v2 dispatcher to `signal_smt.py` (env-gated) and a v2 entry to `backtest_smt.py`. Specific-day regression via `regression.md` + event-jsonl + trades.tsv diff. TP / breakeven / trail / secondary-target / Layer-B / MSS / CISD / displacement-invalidation are deliberately removed from the new path.

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-redesign-v2.md`
- 7/7 tasks completed across 4 waves; 90 new tests (planned ~67), all passing; full suite 888/897
- Two divergences: test file renamed `test_smt_strategy_v2.py` (collision with pre-existing file); regression.md parser uses calendar-day expansion (matches test spec assertion)
- E2E smoke: `run_backtest_v2('2025-11-14','2025-11-14')` → 4 trades, PnL −$471.00; `regression.py` exits 0
- Alignment score: 9/10


## Feature: SMT Limit Entry Lifecycle & Min-TP Fallback

**Status**: Complete
**Plan File**: `.agents/plans/smt-limit-entry-lifecycle.md`

Reworked the entry-signal lifecycle so the human trader sees LIMIT_PLACED at divergence time (not only on fill). Introduces five typed lifecycle events (`limit_placed`, `limit_moved`, `limit_cancelled`, `limit_expired`, `limit_filled`) emitted by `process_scan_bar` and dispatched by `signal_smt.py`. Drops the pts-distance `ENTRY_LIMIT_CLASSIFICATION_PTS` heuristic; `signal_type` is now derived from whether the scan path went through a limit stage. Raises `MIN_TARGET_PTS=15.0` / `MIN_RR_FOR_TARGET=1.5` with fallback-to-next-ranked draw when the nearest target fails the floor; divergence is preserved when no draw qualifies.

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-limit-entry-lifecycle.md`
- 51/51 tests pass in test_smt_limit_lifecycle.py; full suite 792 passed / 9 skipped / 5 failed (pre-existing)
- Three divergences: 51 tests vs ~46 planned (extra MOVE_LIMIT + cancel+replace tests — good); original_limit_price semantics clarified in test assertion; SYMMETRIC_SMT_ENABLED=False added to _patch_base for test isolation
- Backtest smoke: mean_test_pnl=$5,023.74 (within ±10% of baseline)
- Alignment score: 9/10

---

## Feature: EQH/EQL Detection Extending Secondary-Target Candidate Pool (Gap 1)

**Status**: Complete
**Started**: 2026-04-23
**Plan File**: `.agents/plans/eqh-eql-secondary-target.md`

Adds Equal Highs / Equal Lows detection to the DOL candidate pool used by `_build_draws_and_select`. Computes EQH/EQL levels once per session from prior-day + overnight MNQ bars (swing point clustering with tolerance + staleness invalidation), stores them on `SessionContext`, and feeds them into `_build_draws_and_select` so the existing `secondary_target` mechanism can pick them when they are the closest qualifying liquidity. Purely additive — does NOT change TDO as primary TP, does NOT modify stop/partial/trail mechanics (all locked from prior campaign). Expected lift: +10–20% on total P&L concentrated in the 43 secondary-hit trades. Critical risk: staleness logic must correctly deactivate levels that have been closed through (not just wicked through); adversarial unit tests for staleness must precede integration.

### Reports Generated

**Execution Report:** `.agents/plans/eqh-eql-secondary-target.execution-report.md`
- All 8 tasks across 3 waves complete; 25 tests added (23 unit + 2 regression smoke); 708/708 non-orchestrator suite green, 0 regressions
- Three divergences: extra `_build_draws_and_select` integration tests (good), Windows-forced `uv run -- python -m pytest` invocation and missing `pytest-timeout` (environmental), constant placement shifted past Human execution mode block (good)
- Test results: 25/25 EQH-scoped pass; 5 unrelated orchestrator failures due to Windows Application Control blocking `jiter` DLL
- Alignment score: 9/10

---

## Feature: Uncap Hold Time — Trail Width + Session Extension

**Status**: ✅ Complete
**Plan File**: `.agents/plans/uncap_hold_time.md`

Widens `TRAIL_AFTER_TP_PTS` from 1.0 to 50.0 pts with two safeguards: a never-widen rule (`stop = max(current, trail)` for longs) and a delayed activation gate (`TRAIL_ACTIVATION_R` — trail only fires after price travels R multiples of initial stop past TDO). Extends `SESSION_END` to `"15:15"`. Disables partial exit when trail is active (logically contradictory). Adds `initial_stop_pts` to signal dict. Optimizer grid: 4 × 5 = 20 cells (`TRAIL_AFTER_TP_PTS` × `TRAIL_ACTIVATION_R`).

### Reports Generated

**Execution Report:** `.agents/execution-reports/uncap_hold_time.md`
- All 5 tasks completed; 15 new tests passing (+15 from 149 baseline to 164); 0 regressions
- Three divergences: pre-existing `PARTIAL_EXIT_LEVEL_RATIO` import bug fixed; `test_trail_active_when_no_secondary` updated for new deferred-stop contract; integration tests required deeper monkeypatching for newer feature gates
- Test results: 164/165 full suite; 1 pre-existing failure unchanged
- Alignment score: 9/10

---

## Feature: SMT Limit Entry at Anchor Close

**Status**: ✅ Complete
**Plan File**: `.agents/plans/smt-limit-entry-anchor-close.md`

Replaces market-order entry (bar close) with a limit at `anchor_close ± buffer`. Two modes: same-bar fill (`LIMIT_EXPIRY_SECONDS=None`) and forward-looking limit with new `WAITING_FOR_LIMIT_FILL` state machine state. Bar resolution auto-detected from timestamps — works at 1m (backtest) and 1s (live). Adds `anchor_close_price`, `limit_fill_bars`, `missed_move_pts` diagnostic fields and `limit_expired` exit type to `trades.tsv` so missed big-mover trades are fully visible. Both constants default to `None` (zero behaviour change). Optimizer search space: `LIMIT_ENTRY_BUFFER_PTS ∈ [None, 0.0, 0.25, 0.5, 1.0]`, `LIMIT_EXPIRY_SECONDS ∈ [None, 60.0, 120.0, 300.0]`.

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-limit-entry-anchor-close.md`
- All 6 tasks completed; 19 new tests passing (+19 from 643 baseline to 662); 0 regressions
- One latent gap: forward-limit fill path does not apply secondary_target / DISPLACEMENT_STOP_MODE adjustments (no impact while LIMIT_EXPIRY_SECONDS=None default)
- Test results: 662 full suite; 9 pre-existing failures unchanged
- Alignment score: 9/10

---

## Feature: SMT Solutions A–E — Signal Quality & State Machine Hardening

**Status**: ✅ Complete
**Plan File**: `.agents/plans/smt-solutions-a-e.md`
**Prerequisite**: `smt-structural-and-fixes.md` complete. Executes BEFORE `smt-solution-f-draw-on-liquidity.md`.

Implements five signal quality improvements: divergence scoring (A), hypothesis replacement with score decay (B), inverted stop guard (C), hypothesis invalidation threshold (D), early bar skip (E). All new constants default to off/permissive so baseline output is unchanged at defaults.

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-solutions-a-e.md`
- All 4 waves completed; 20 new tests passing (+20 from 603 baseline to 623); 0 regressions
- One plan divergence: Wave 3 signal_smt.py state machine not verbatim-mirrored (inapplicable — `_process_scanning` is stateless; Solution C inherited via `_build_signal_from_bar`)
- Test results: 77 passed (unit), 27 passed (integration), 623 full suite; 8 pre-existing failures unchanged
- Alignment score: 9/10

---

## Feature: SMT Solution F — ICT-Aligned Draw on Liquidity Target Selection

**Status**: ✅ Complete
**Plan File**: `.agents/plans/smt-solution-f-draw-on-liquidity.md`
**Prerequisite**: `smt-solutions-a-e.md` complete. Isolated cycle — validate independently before merging with A–E optimisation results.

Replaces TDO-only TP with a 6-level draw-on-liquidity hierarchy (FVG → TDO → Midnight Open → Session High/Low → Overnight H/L → PDH/PDL). Signals with no draw satisfying `MIN_RR_FOR_TARGET` (1.5×) and `MIN_TARGET_PTS` (15 pts) are skipped. Adds secondary target and `exit_secondary` exit type. Supersedes `OVERNIGHT_RANGE_AS_TP` and `MIDNIGHT_OPEN_AS_TP` flags.

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-solution-f-draw-on-liquidity.md`
- All 5 waves completed; 21 new tests passing (9 unit + 12 integration); 125 target-suite passed, 0 regressions
- Two plan gaps: TP cascade replaced in two locations (plan cited one); `test_smt_structural_fixes.py` monkeypatching required for new filter constants
- Alignment score: 9/10

**Smoke Backtest (full 6-fold walk-forward):**
- 516 total trades; mean_test_pnl=$2,458.76; min_test_pnl=$916.84 (no losing folds)
- fold6 avg_rr=4.12, win_rate=54.9%, Sharpe=4.50, Calmar=14.77
- `exit_secondary` fired on 7 test trades — two-level exit path confirmed end-to-end

**Post-review fixes (committed with plan):**
- `backtest_smt.py:439` — `_run_ses_high` init changed from `0.0` to `-float("inf")` (symmetric with `_run_ses_low`)
- `strategy_smt.py:1432` — added clarifying comment on standalone `tp_breached` setter
- `tests/test_smt_backtest.py` — fixed `test_signal_with_pdh_draw_placed` assertion (now checks `tp_name`); added `test_exit_secondary_same_bar_primary_and_secondary_crossed`
- `backtest_smt.py` + `signal_smt.py` — added directional guards to `overnight_high/low`, `pdh`, `pdl` draws (consistent with other draw entries)

---

## Feature: SMT Structural Fixes & Signal Quality

**Status**: ✅ Complete
**Plan File**: `.agents/plans/smt-structural-and-fixes.md`

Combines findings.md fixes (F1 live reentry guard, F2a/b midnight open TP default, F2c ratio-based invalidation threshold, F3 pessimistic fill prices) with structural_moves.md signal quality improvements (symmetric SMT, expanded reference levels, HTF visibility filter, displacement body size, always-on confirmation candle). All changes behind opt-in flags. Metric baseline captured after this plan AND smt-humanize are both merged.

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-structural-and-fixes.md`
- All 9 tasks completed (F3, F1, F2a/b, F2c, S1, S4, S5, S2, S3); 3 plan divergences, all improvements
- Three divergences: MIN_DISPLACEMENT_BODY_PTS applied in two locations (plan gap); ALWAYS_REQUIRE_CONFIRMATION checked in backtest WAITING_FOR_ENTRY blocks (plan gap); ratio threshold combined with existing PTS sentinel for backward compatibility
- Test results: 28 new tests passing (tests/test_smt_structural_fixes.py) + 2 pre-existing tests fixed; full suite 605 passed, 5 pre-existing failures unchanged
- Alignment score: 9/10

---

## Feature: Declockify — Single-Source Session Config

**Status**: ✅ Planned
**Started**: 2026-04-23
**Plan File**: `.agents/plans/declockify-session-config.md`

Moves every session-time constant out of `strategy_smt.py` into a new top-level `session_config.py`. Removes session-time gates from `strategy_smt.py` and `signal_smt.py` entirely — the orchestrator becomes the sole clock owner, starting/stopping the signal engine and triggering graceful shutdown (position close + CLOSE_MARKET emission + IB disconnect) at session boundaries via a sentinel-file protocol. Also adds walk-back fallback to `compute_tdo()` and `_price_at_900()` so hypothesis generation and TDO resolution work at any clock time.

---

## Feature: SMT Humanize — Human-Executable Signal Model

**Status**: ✅ Complete
**Plan File**: `.agents/plans/smt-humanize.md`
**Prerequisite**: `smt-structural-and-fixes.md` merged and baseline captured first.

Redesigns signal output (typed ENTRY/MOVE_STOP/CLOSE_MARKET), adds execution delay simulation to 1m backtest, PENDING states, daily limits (8 entries, $100/$150 DD), confidence scoring, and deception detection. All gated by HUMAN_EXECUTION_MODE=False default.

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-humanize.md`
- Detailed implementation summary
- Divergences and resolutions
- Test results and metrics
- Team performance analysis (if applicable)

---

## Feature: SMT Strategy — Position Architecture Expansion (Plan 2 of 2)

**Status**: ✅ Complete
**Plan File**: `.agents/plans/smt-position-architecture-plan2.md`

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-position-architecture-plan2.md`
- Detailed implementation summary
- Two divergences: partial-exit integration test assertion weakened (fixture geometry constraint); Level 4 manual backtest deferred (requires Databento parquets)
- Test results: 20 new tests passing (tests/test_smt_position_arch.py); full suite 551 passed, 2 pre-existing failures
- All functional acceptance criteria met; all features opt-in and default-off

**Baseline entering Plan 2** (post-plan-1 experiments):
- `HIDDEN_SMT_ENABLED = True` (approved Round 1 — +30.6% PnL, now default)
- Effective baseline: 45 trades, pnl=$5,075, wr=57.8%, avg_rr=7.25, appt=$112.78, max_dd=$108
- All other plan-1 flags remain False/default; see `.agents/experiment-log.md` for re-test conditions

### Flag Experiment Results (2026-04-20)

1-fold fast backtest (2026-01-19 → 2026-04-12). Full data and verdicts in `.agents/experiment-log.md`.

| Run | Config | final_pnl | win_rate | avg_rr | max_dd | Verdict |
|-----|--------|-----------|----------|--------|--------|---------|
| Baseline | — | $5,075 | 57.8% | 7.25 | $108 | — |
| A-1 | SMT_OPTIONAL, MIN_DISP=8pt | $3,844 | 52.0% | 5.78 | $155 | REJECT |
| A-2 | SMT_OPTIONAL, MIN_DISP=10pt | $3,520 | 52.3% | 6.08 | $102 | REJECT |
| A-3 | SMT_OPTIONAL, MIN_DISP=15pt | $4,386 | 57.5% | 6.93 | $158 | REJECT |
| **B-1** | **PARTIAL_EXIT, FRAC=0.33** | **$4,798** | **62.2%** | **6.56** | **$95** | **APPROVE** |
| B-2 | PARTIAL_EXIT, FRAC=0.5 | $3,229 | 62.2% | 4.75 | $95 | REJECT |
| C-1–C-4 | TWO_LAYER+FVG (all configs) | $1,258–$2,530 | 57.8% | 6.7–7.1 | $30–$54 | NEUTRAL |
| D-1 | SMT_FILL_ENABLED | $5,075 | 57.8% | 7.25 | $109 | NEUTRAL |

- **APPROVED (1):** `PARTIAL_EXIT_ENABLED=True, PARTIAL_EXIT_FRACTION=0.33` — −12% max_drawdown ($108→$95), −5.5% P&L ($5,075→$4,798). Best risk-adjusted result. Now set as default.
- **REJECTED (3):** `SMT_OPTIONAL` all thresholds — displacement entries steal same-session SMT slots, reducing wick SMT count. Needs structural fixes (Plan 3: `DISPLACEMENT_STOP_MODE` + `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT`) before re-test.
- **NEUTRAL (4):** `TWO_LAYER+FVG` all configs — `layer_b_triggers=0` in this 60-day momentum window. FVG retracements are regime-dependent. Plan 3 adds `fvg_detected` diagnostic field and `FVG_LAYER_B_REQUIRES_HYPOTHESIS` gate for re-test.
- **NEUTRAL (1):** `SMT_FILL_ENABLED` — `fill_entries=0`, P&L identical to baseline ($5,075). No fill divergences formed in this 60-day momentum regime. Re-test in Round 3.

**New effective baseline entering Plan 3:** trades=45, pnl=$4,798, wr=62.2%, avg_rr=6.56, max_dd=$95

---

## Feature: SMT Strategy — Reference Level Fix + Signal Quality (Plan 1 of 2)

**Status**: ✅ Complete + Experiments Done
**Plan File**: `.agents/plans/smt-signal-quality-plan1.md`

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-signal-quality-plan1.md`
- Detailed implementation summary
- Two divergences: fewer existing tests needed updating than planned (index-based unpack was already dominant); Level 4 manual backtest deferred (requires Databento parquets)
- Test results: 22 new tests passing (test_smt_signal_quality.py); full suite 531 passed, 2 pre-existing failures
- All functional acceptance criteria met; all features opt-in and default-off

### Flag Experiment Results (2026-04-20)

1-fold fast backtest (2026-01-19 → 2026-04-12). Full data in `plan1-results.tsv`, full analysis in `.agents/experiment-log.md`.

- **APPROVED (1):** `HIDDEN_SMT_ENABLED=True` — +30.6% PnL (+$1,189), lower drawdown, +9 extra trades. Now set as default.
- **REJECTED (2):** `STRUCTURAL_STOP_MODE` — kills RR (7.47→2.08), structurally incompatible with trailing-stop exits.
- **REJECTED (2):** `INVALIDATION_MSS_EXIT` / `INVALIDATION_SMT_EXIT` — fires too close to entry; need `MIN_FAVORABLE_MOVE_PTS` guard before retesting.
- **NEUTRAL (5):** `MIDNIGHT_OPEN_AS_TP`, `SILVER_BULLET_WINDOW_ONLY`, `OVERNIGHT_SWEEP_REQUIRED`, `OVERNIGHT_RANGE_AS_TP`, `INVALIDATION_CISD_EXIT` — no effect in this 60-day window; retained for future regime testing.

---

## Feature: SMT Hypothesis Alignment Analysis (Plan 3 of 3)

**Status**: ✅ Complete
**Plan File**: `.agents/plans/smt-hypothesis-alignment-plan3.md`

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-hypothesis-alignment-plan3.md`
- All 5 waves completed; 30 new tests passing; no new regressions (4 pre-existing failures unchanged)
- Three plan divergences, all improvements: `_pending_displacement_bar_extreme` state variable added; Layer B gate moved to IDLE state (equivalent result, cleaner interface); `fvg_detected` set post-gate for accurate diagnostics
- Manual tests deferred: live backtest TSV column check + `analyze_hypothesis.py` live run (both require Databento parquets)
- Alignment score: 9/10

### What Plan 3 Builds

Four deliverables enabling evidence-based ICT parameter decisions:

1. **Per-rule hypothesis logging** — expose each ICT rule output (`pd_range_case`, `week_zone`, `day_zone`, `trend_direction`, `hypothesis_score`) as per-trade fields in `trades.tsv`, enabling rule-level decomposition.
2. **`HYPOTHESIS_FILTER` flag** in `backtest_smt.py` — when True, only aligned signals are taken; enables Outcome B walk-forward validation.
3. **`analyze_hypothesis.py`** — CLI script that reads `trades.tsv` and prints aligned vs misaligned performance split with per-group win rate, avg P&L, avg RR, and per-fold consistency check.
4. **Round 2 deferred feature fixes** — opt-in constants that unblock rejected/neutral features:
   - `DISPLACEMENT_STOP_MODE: bool = False` — correct stop for displacement entries (bar extreme, not SMT structural stop)
   - `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT: int = 0` — quality gate on displacement entries via hypothesis score
   - `PARTIAL_EXIT_LEVEL_RATIO: float = 0.5` — configurable partial exit target (0=entry, 1=TP); enables ratio tuning in Round 3
   - `fvg_detected: bool` — per-trade TSV field; diagnoses whether FVG zones form but don't retrace vs. don't form at all
   - `FVG_LAYER_B_REQUIRES_HYPOTHESIS: bool = False` — gates Layer B to hypothesis-confirmed reversal sessions

### Entering Baseline

trades=45, pnl=$4,798, wr=62.2%, avg_rr=6.56, max_dd=$95
(HIDDEN_SMT_ENABLED=True, PARTIAL_EXIT_ENABLED=True, PARTIAL_EXIT_FRACTION=0.33)

### Test Plan

30 automated pytest tests + 2 manual (require Databento parquets). See plan file for full spec.

### Implementation Scope

- `hypothesis_smt.py` — add `compute_hypothesis_context()` + `_count_aligned_rules()` helper
- `backtest_smt.py` — integrate context call, attach rule fields to trade records, add `HYPOTHESIS_FILTER` gate, add displacement stop/score-gate/FVG-gate logic
- `strategy_smt.py` — add 4 new constants; update `manage_position()` for `PARTIAL_EXIT_LEVEL_RATIO`
- `analyze_hypothesis.py` — new script (mirrors `analyze_gaps.py` pattern)
- `tests/test_hypothesis_analysis.py` — new test file (30 tests)

### Round 3 Experiment Results

1-fold fast backtest (2026-01-19 → 2026-04-12). Full analysis in `.agents/experiment-log.md`.

| Run | Config | Trades | PnL | WR | avg_rr | max_dd | Verdict |
|-----|--------|--------|-----|----|--------|--------|---------|
| A-1 | HYPOTHESIS_FILTER=True | 19 | $4,124 | 84.2% | 4.40 | $65 | NEUTRAL — fewer trades, similar WR, loses $1.5K; hypothesis doesn't add signal value |
| B-1 | SMT_OPTIONAL=True + MIN_DISPLACEMENT_PTS=10 + DISPLACEMENT_STOP_MODE=True | 47 | $7,148 | 76.6% | 5.52 | $120 | APPROVE — +$2K, +7 displacement trades, structurally correct stop |
| C-1 | PARTIAL_EXIT_LEVEL_RATIO=0.33 | 53 | $5,939 | 81.1% | 5.34 | $65 | APPROVE — tighter partial exit, same WR, lower drawdown |
| C-2 | PARTIAL_EXIT_LEVEL_RATIO=0.25 | 53 | $5,462 | 81.1% | 4.65 | $65 | REJECT — further tightening degrades RR with no WR benefit |
| D-1 | FVG_LAYER_B_REQUIRES_HYPOTHESIS=True + score≥2 | 35 | $3,843 | 80.0% | 5.05 | $125 | DEFERRED — layer_b_triggers=0; gating has no effect this regime |
| Combined | SMT_OPTIONAL + DISPLACEMENT_STOP_MODE + PARTIAL_EXIT_LEVEL_RATIO=0.33 | 40 | $5,513 | 82.5% | 5.36 | $65 | CONFIRMED baseline |

**New approved defaults (set in `strategy_smt.py`):**
- `SMT_OPTIONAL = True`
- `DISPLACEMENT_STOP_MODE = True`
- `PARTIAL_EXIT_LEVEL_RATIO = 0.33`

**New effective baseline entering optimization run:** trades=40, pnl=$5,513, wr=82.5%, avg_rr=5.36, max_dd=$65

---

## Feature: Session Hypothesis System

**Status**: ✅ Complete
**Plan File**: `.agents/plans/session-hypothesis.md`

### Summary
Session-planning layer for the realtime SMT workflow. At ~9:00am ET a `HypothesisManager`
runs 5 deterministic ICT/SMT rules (PDH/PDL case analysis, multi-day trend, deception sweeps,
session extremes, TDO/TWO premium-discount zones) and calls the Claude API for a narrative
hypothesis. Evidence is evaluated per 1m bar; 3 contradictions trigger a revision call.
Session end calls a Claude summary. A `matches_hypothesis` boolean is added to every signal
dict in both realtime (`signal_smt.py`) and backtest (`backtest_smt.py`), enabling downstream
alignment analysis without affecting signal generation.

### Reports Generated

**Execution Report:** `.agents/execution-reports/session-hypothesis.md`
- Detailed implementation summary
- Two minor divergences: `_assign_case` helper extracted for testability; 3 trade-record exit sites annotated vs 1 documented in plan
- Test results: 22 passed (tests/test_hypothesis_smt.py); full suite 510 passed, 9 skipped, 0 failed
- Baseline was 488 passed; +22 new tests, zero regressions
- Live smoke test (real MNQ parquets) deferred — requires IB session data on disk

---

## Feature: Session Orchestrator Daemon

**Status**: ✅ Complete
**Plan File**: `.agents/plans/session-orchestrator.md`

### Summary
Python daemon (`orchestrator/main.py`) that manages `signal_smt.py` lifecycle:
starts at 09:00 ET on trading days, relays SIGNAL/EXIT lines to stdout + session log,
restarts once on unexpected crash, terminates at 13:35 ET, and calls Claude API for
post-session summary with metrics, narrative, and parameter recommendations.

### Reports Generated

**Execution Report:** `.agents/execution-reports/session-orchestrator.md`
- Detailed implementation summary
- One divergence: psutil import moved to module level (testability improvement)
- Test results: 53 passed (51 unit + 2 integration); full suite 495 passed, 0 failures
- Manual-only gap: `python orchestrator/main.py --check` with real key; live IB Gateway session

---

## Feature: IB-Gateway Connection Instability Fix

**Status**: ✅ Complete
**Plan File**: `.agents/plans/ib-connect-fix.md`

### Reports Generated

**Execution Report:** `.agents/execution-reports/ib-connect-fix.md`
- Detailed implementation summary
- No divergences from plan (10/10 alignment)
- Test results: 27 passed, 1 pre-existing failure, 0 regressions
- Manual-only gap: reconnect validation (requires live IB Gateway)

---

## Feature: SMT Bar-by-Bar State Machine Refactor (Phase 2)

**Status**: ✅ Complete
**Plan File**: `.agents/plans/smt-bar-by-bar-refactor.md`

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-bar-by-bar-refactor.md`
- Detailed implementation summary
- Divergences and resolutions (screen_session shim, _scan_bars_for_exit already absent, sequential execution)
- Test results and metrics: 417 passing, 1 pre-existing failure, ~23 new tests
- Level 6 (smoke test with real data) deferred — requires IB-Gateway

---

## Feature: signal_smt.py — Tick-Based 1s Bar Ingestion

**Status**: ✅ Complete
**Plan File**: `.agents/plans/signal-smt-tick-ingestion.md`

### Reports Generated

**Execution Report:** `.agents/execution-reports/signal-smt-tick-ingestion.md`
- Detailed implementation summary
- Divergences and resolutions (none — 10/10 alignment)
- Test results and metrics (27 passed, +7 tests; 291/293 full suite, 0 regressions)
- Manual-only gap: live per-tick callback firing (requires live IB Gateway)

---

## Feature: signal_smt.py + train_smt.py Refactoring

**Status**: ✅ Complete
**Plan File**: `.agents/plans/signal-smt-implementation.md`

### Reports Generated

**Execution Report:** `.agents/execution-reports/signal-smt-implementation.md`
- Detailed implementation summary
- Divergences and resolutions
- Test results and metrics (70 passed, 0 failures; +22 new tests)
- Manual-only gaps: live IB connection + dual subscriptions; live 1s stop/TP detection

---

## Feature: SMT Direction Control Refactor

**Status**: ✅ Complete
**Plan File**: `.agents/plans/smt-direction-control.md`

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-direction-control.md`
- Detailed implementation summary
- Divergences and resolutions
- Test results and metrics (360 passed, 2 skipped, 0 failures)
- Live backtest: 8 trades (down from 12), shorts $541.75, longs $115.25

---

## Feature: Databento Historical Data Pipeline

**Status**: ✅ Complete
**Plan File**: `.agents/plans/databento-historical-pipeline.md`

### Problem

With IB as the sole data source, the SMT backtest window is capped at ~6 months
(Sep 24, 2025 → today), producing only ~30 test trades across 2 active folds.
Walk-forward optimization on 30 trades overfits badly — a minimum of ~100 test
trades across multiple folds is needed for parameters to be statistically meaningful.

IB chaining (fetching expired quarterly contracts by conId) was investigated and
ruled out: `reqContractDetails` returns Error 200 for all expired contracts, and the
public REST endpoint (`contract.ibkr.info`) returns 404. IB drops expired contracts
from its definition API at rollover. No code changes can work around this.

### Solution

Integrate **Databento** as a historical data source for MNQ and MES.
- Dataset: `GLBX.MDP3` (CME Globex MDP 3.0)
- Symbols: `MNQ.c.0` and `MES.c.0` (continuous front-month — Databento handles roll stitching)
- Schema: `ohlcv-1m` (no native 5m; resample to 5m in code)
- Date range: `2024-01-01` → today (~2 years, ~120+ expected trades)
- Estimated cost: ~$4.93 one-time for Jan 2024 → Mar 2026 (75.6 MB); trivial

### Authentication

`DATABENTO_API_KEY` is already set in `.env`. The user has created a Databento account.

### Data persistence

Databento data must survive cache clears. Store in a **project-level directory**
`data/historical/` (gitignored, kept on disk), separate from the ephemeral
`~/.cache/autoresearch/futures_data/` IB cache.

`prepare_futures.py` lookup priority:
1. `data/historical/{ticker}.parquet` — Databento download (permanent)
2. `~/.cache/autoresearch/futures_data/5m/{ticker}.parquet` — IB cache (ephemeral)
3. Live download via Databento API (if neither exists)

### Implementation requirements

**`data/sources.py`**
- Add `DatabentSource` class implementing the `DataSource` abstract interface
- `fetch(ticker, start, end, interval, ...)` — calls Databento `client.timeseries.get_range()` with dataset `GLBX.MDP3`, schema `ohlcv-1m`, symbols `[ticker]` (e.g. `"MNQ.c.0"`)
- Resample 1m → 5m using `df.resample("5min").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"})` after download
- Return standard OHLCV DataFrame with tz-aware ET DatetimeIndex (same contract as existing sources)
- Handle `databento.BentoError` and connection failures gracefully (return None)
- Load `DATABENTO_API_KEY` from environment; raise `RuntimeError` at init if missing

**`prepare_futures.py`**
- Add `HISTORICAL_DATA_DIR = "data/historical"` constant
- Add `DATABENTO_SYMBOLS = {"MNQ": "MNQ.c.0", "MES": "MES.c.0"}` constant
- Update `process_ticker()` to check `data/historical/{ticker}.parquet` first; if present, skip download
- If not present: download via `DatabentSource`, save to `data/historical/{ticker}.parquet`
- IB fetch (existing path) remains as fallback or can be removed in favour of Databento-only — decision for planner
- Update `BACKTEST_START = "2024-01-01"` to match the full Databento window
- Update manifest to reflect the new start date

**`data/historical/`**
- Create directory with a `.gitkeep` and add `data/historical/*.parquet` to `.gitignore`

**Tests**
- Unit test: `DatabentSource.fetch()` with mocked `databento.client` — verify correct dataset, schema, symbol, and resampling logic
- Unit test: `DatabentSource` returns `None` on `BentoError` without raising
- Unit test: `DatabentSource` raises `RuntimeError` at init when `DATABENTO_API_KEY` is missing
- Regression test: existing `IBGatewaySource` tests still pass (no regressions)
- Integration test (live, auto-skipped if no key): fetches a 5-day window for `MNQ.c.0` and validates OHLCV schema + ET timezone

### Key technical details

- Databento Python client: `databento` package (`pip install databento`)
- API call pattern:
  ```python
  import databento as db
  client = db.Historical(key=api_key)
  data = client.timeseries.get_range(
      dataset="GLBX.MDP3",
      symbols=["MNQ.c.0"],
      schema="ohlcv-1m",
      start="2024-01-01",
      end="2026-04-01",
  )
  df = data.to_df()
  ```
- Column mapping from Databento: `open`, `high`, `low`, `close`, `volume` → rename to `Open`, `High`, `Low`, `Close`, `Volume`
- Timezone: Databento returns UTC timestamps — convert to `America/New_York`
- Continuous contract (`MNQ.c.0`) vs specific expiry: use continuous for clean stitched history; the `.c.0` suffix means front-month roll-adjusted

### Expected outcome after implementation

- `uv run prepare_futures.py` downloads ~2 years of 5m MNQ/MES bars from Databento and saves to `data/historical/`
- `uv run python train_smt.py` runs with 6 walk-forward folds, each test fold having 15–25 trades, total ~120 test trades
- Parameter optimization (grid search over stop ratios, session window) becomes statistically meaningful

### Reports Generated

**Execution Report:** `.agents/execution-reports/databento-historical-pipeline.md`
- Detailed implementation summary
- No divergences from plan
- Test results and metrics: 357 passed (+11 unit tests), 0 failed, 14 deselected (integration)
- All acceptance criteria met; ready for live run with `DATABENTO_API_KEY`

---

## Feature: Expand Historical Data via IB Quarterly Contracts

**Status**: ✅ Complete
**Plan File**: `.agents/plans/ib-quarterly-conid-fetch.md`

Research completed 2026-04-01. MNQM6/MESM6 (conIds `770561201`/`770561194`) accept explicit `endDateTime` with good data from Sep 24, 2025 → ~6.5 months, ~37 expected trades. Full implementation spec and test cases in plan file.

### Reports Generated

**Execution Report:** `.agents/execution-reports/ib-quarterly-conid-fetch.md`
- Detailed implementation summary
- Divergences and resolutions (train_smt.py index alignment fix, qualifyContracts retention)
- Test results and metrics: 366 passed, 2 skipped, 6 new tests
- Live validation: MNQ 35,375 bars / MES 34,133 bars, 6 folds, 42 trades


## Feature: SMT Strategy — 5m Optimization Harness + Real-Time Screener Architecture

**Status**: ✅ Planned
**Plan File**: `.agents/plans/smt-5m-optimizer.md`

### Background
The current `train_smt.py` harness runs on 1m bars. IB-Gateway's ContFuture API only provides
~14 days of 1m history per request (`endDateTime=''` restriction — error 10339 for explicit dates),
which yields too few trades for meaningful walk-forward optimization.

### Architectural split (decided 2026-04-01)

**Optimizer** (`train_smt.py` + `prepare_futures.py`):
- Switch from 1m to **5m bars**
- IB ContFuture + `endDateTime=''` supports `durationStr='3 M'` for 5m bars → ~3 months of data per download
- 3 months of 5m data gives ~40–60 kill-zone sessions → enough trades for walk-forward stats
- Signal logic (SMT divergence, kill zone window, TDO take-profit) is resolution-agnostic and adapts cleanly to 5m

**Real-time screener** (future `screen_smt.py`, not yet built):
- Subscribes to live 1m bars from IB-Gateway (via `reqRealTimeBars` or `reqMktData`)
- Runs `screen_session()` and `manage_position()` at 1m resolution for precise intraday execution
- Does NOT use the historical parquet cache — operates on streaming bars only

### Tasks for next agent

**Task A — Update optimizer to 5m bars**
1. `prepare_futures.py`: change `INTERVAL = "1m"` → `"5m"`, update `BACKTEST_START` to today − 90 days
2. `data/sources.py`: verify `_IB_CONTFUTURE_MAX_DAYS` is not applied to 5m (it shouldn't be — 5m uses the standard `chunk_days` path and `durationStr='90 D'` should not timeout). If needed, add a `_IB_CONTFUTURE_MAX_DAYS_5M` constant or make the cap interval-aware.
3. `train_smt.py` editable section: no signal logic changes needed; `SESSION_START`/`SESSION_END` constants already work at any bar resolution. Verify the harness slices bars correctly for 5m (index alignment, bar count thresholds like `MIN_BARS_BEFORE_SIGNAL`).
4. `tests/conftest.py`: update futures fixture to write 5m parquets and update the manifest `fetch_interval`.
5. `tests/test_smt_backtest.py`: update synthetic bar fixtures from 1m to 5m.
6. Run `prepare_futures.py` with IB-Gateway active, confirm 3 months of data downloads, run `train_smt.py` and confirm walk-forward produces multiple folds with sufficient trades.

**Task B — Plan real-time SMT screener** (separate feature, after Task A)
- Design `screen_smt.py` mirroring `screen.py` for the equity strategy
- Uses live IB streaming bars at 1m resolution (not historical parquet)
- Calls `screen_session()` at session open, then `manage_position()` each new 1m bar
- Outputs: active signal (direction, entry, stop, TP) + open position status

### Key constraints
- `_IB_CONTFUTURE_MAX_DAYS = 14` cap in `data/sources.py` is a 1m-specific limit; 5m requests use `_IB_CHUNK_DAYS["5m"] = 60` and are not affected by this constant (the cap is only applied in the `contfuture` branch — verify this during implementation)
- The `_compute_fold_params` short-window guard (< 130 bdays → 1 fold) will no longer trigger once 3 months of 5m data are available — the full 6-fold walk-forward will run normally
- `MIN_BARS_BEFORE_SIGNAL = 5` was calibrated for 1m bars; for 5m bars each bar is 5× wider so the value may need recalibration (5 × 5m = 25 minutes before signal — reasonable for the kill zone)

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-5m-optimizer.md`
- Detailed implementation summary
- Divergences and resolutions
- Test results and metrics
- Wave 3 (live IB-Gateway) deferred — pending user action

---

## Feature: SMT Divergence Strategy on MNQ1!

**Status**: ✅ Complete
**Started / Completed**: 2026-03-31
**Plan File**: `.agents/plans/smt-divergence-mnq-strategy.md`

### What was completed
- Task 1.1: `data/sources.py` — ContFuture support; IB 1m data uses ContFuture + `endDateTime=''`
  - Root cause discovery: IB error 10339 (explicit endDateTime rejected for ContFuture 1m)
  - Solution: `endDateTime=''` capped at `_IB_CONTFUTURE_MAX_DAYS=7` to prevent timeouts
- Task 1.2: `prepare_futures.py` — downloads MNQ/MES 1m bars; dynamic dates (7-day window)
  - Live test ✅ — 6900 bars each for MNQ and MES downloaded successfully
- Task 2.1: `train_smt.py` strategy functions + constants
  - Auto-loads BACKTEST_START/BACKTEST_END from futures_manifest.json at module load
  - `_compute_fold_params` auto-detects short windows (< 130 bdays → 1 fold, minimal test days)
- Task 2.2: `tests/test_smt_strategy.py` — 24 unit tests, all passing
- Task 3.1: `train_smt.py` harness — run_backtest, _compute_metrics, fold loop
- Task 4.1: `tests/test_smt_backtest.py` — 10 integration tests, all passing
- Task 4.2: `tests/conftest.py` futures bootstrap + `program_smt.md`
- Task DS: 4 ContFuture / futures tests in `tests/test_data_sources.py`
- Live end-to-end ✅ — `uv run train_smt.py` ran full backtest on live IB data

### IB Data Limitation
IB rejects explicit `endDateTime` for CME equity-index futures 1m bars (error 10339 for ContFuture; silent cancellation for specific quarterly contracts). Only `endDateTime=''` (most recent data) is accepted, limiting 1m futures history to ~7 calendar days per download. The `_compute_fold_params` harness auto-detects this and reduces to 1 fold with minimal test days. For longer-window backtesting, supply pre-downloaded parquet files from an external data provider.

### Test Status
- 346 passed, 2 skipped (2026-03-31)
- New tests: 24 unit (test_smt_strategy.py) + 10 integration (test_smt_backtest.py) + 4 data source (test_data_sources.py)
- All pre-existing failures fixed: manifest.json + interval subdir for conftest fixture

---

## Feature: Data Layer Abstraction (Multi-Source / Multi-Interval)

**Status**: ✅ Complete
**Started**: 2026-03-29
**Plan File**: .agents/plans/data-layer-abstraction.md

### Reports Generated

**Execution Report:** `.agents/execution-reports/data-layer-abstraction.md`
- Detailed implementation summary
- Divergences and resolutions (test_v3_f.py unplanned fix, manifest tests in test_prepare.py)
- Test results and metrics: 291/291 passing, 14 new tests, 0 regressions

## Optimization Run Series: Dollar-Vol Entry Quality

**Status**: Run A Setup Complete
**Plan Files**:
- Run A: `.agents/plans/run-a-eval-foundation.md`
- Run B: `.agents/plans/run-b-exit-timing.md` (conditional on Run A gate)
- Run C: `.agents/plans/run-c-hardening.md` (conditional on Run B gate or Run A if B skipped)

### Summary
Three-run optimization series addressing the price-volume-updates post-mortem finding
that 89% of trades in the 1–5d bucket are collectively losing −$120.97 while 40 trades
held 6+ days generate all positive P&L. Config: 6 folds × 60 days, 14-day holdout,
110-trade discard floor. Run A: entry quality via dollar volume filter + fold reconfig.
Run B: exit timing protection (conditional). Run C: combined criteria hardening.

### Reports Generated

**Execution Report:** `.agents/execution-reports/run-a-eval-foundation.md`
- Detailed implementation summary
- Divergences and resolutions (pre-existing ATR fix, fixture volume updates)
- Test results and metrics: 290/290 passing, 0 regressions
- Baseline validation: exit 0, 6 folds, fold6_train_total_trades=324 >= 110

---

## Feature: Recovery Mode Signal Path

**Status**: ✅ Complete
**Completed**: 2026-03-25
**Plan File**: .agents/plans/recovery-mode-signal.md

### Core Changes
- `train.py` `screen_day()` — added SMA200 computation, two-path check (bull/recovery), `signal_path` variable, relaxed `slope_floor` (0.990 recovery vs 0.995 bull), relaxed RSI range (40–65 recovery vs 50–75 bull), `signal_path` in return dict
- `screener.py` — added `signal_path` to per-ticker row dict, `PATH` column in output tables, `death_cross` added to `_RULES`, `_rejection_reason()` returns `"death_cross"` when SMA50 <= SMA200
- `screener_prepare.py` — `HISTORY_DAYS` increased from 180 to 300 for SMA200 support
- `tests/test_screener.py` — `make_recovery_signal_df` fixture + 7 recovery unit tests
- `tests/test_screener_script.py` — `_write_recovery_parquet` helper + `test_screener_finds_candidate_in_bearish_period` integration test
- `tests/test_e2e.py` — RSI string anchors updated for 2 agent-loop mutation tests (unplanned fix required by RSI tuple refactor)

### Test Status
- Automated: ✅ 289 passed, 1 skipped, 1 pre-existing failure (`test_select_strategy_real_claude_code`)
- New tests: 8 (7 unit in test_screener.py, 1 integration in test_screener_script.py)
- All 8 plan-specified test scenarios: ✅ pass

### Reports Generated

**Execution Report:** `.agents/execution-reports/recovery-mode-signal.md`
- Detailed implementation summary
- Divergences and resolutions (fixture geometry adjustments, unplanned e2e fix)
- Test results and metrics: 289/290 passing, 8 new tests, 0 new regressions

---

## Feature: train.py Performance Optimization

**Status**: ✅ Complete
**Plan File**: .agents/plans/train-py-performance-optimization.md

### Core Changes
- `train.py` `find_pivot_lows()` — vectorized with numpy sliding_window_view (eliminates Python/iloc loop)
- `train.py` `zone_touch_count()` — vectorized with numpy boolean array ops
- `train.py` `nearest_resistance_atr()` — vectorized with numpy sliding_window_view
- `train.py` `manage_position()` — tail-sliced calc_atr14 input (df.iloc[-30:] instead of full df)
- `train.py` `detect_regime()` — df.loc[:today] (binary search) + tail-slice mean (iloc[-50:])
- `tests/test_optimization.py` GOLDEN_HASH — updated to match new detect_regime code
- `screener.py` — rejection diagnostics: `_rejection_reason()` helper + `--diagnose` flag support
- `program.md` — experiment loop step 2 updated with python-performance-optimization skill instruction

### Reports Generated

**Execution Report:** `.agents/execution-reports/train-py-performance-optimization.md`
- Detailed implementation summary
- Divergences and resolutions (no new tests for _rejection_reason; pre-existing screener_prepare.py diff)
- Test results and metrics: 50/50 passing, 0 new regressions

---

## Feature: Parallel Download Thread Pool

**Status**: ✅ Complete
**Completed**: 2026-03-25
**Plan File**: .agents/plans/parallel-download-threadpool.md

### Core Changes
- `screener_prepare.py` — added `MAX_WORKERS` (env: `SCREENER_PREPARE_WORKERS`, default 10), module-level `_process_one(ticker, history_start) -> tuple` helper, replaced sequential `for` loop with `ThreadPoolExecutor` + `as_completed`.
- `prepare.py` — added `MAX_WORKERS` (env: `PREPARE_WORKERS`, default 10), replaced sequential `for ticker in TICKERS` loop with `executor.map(process_ticker, TICKERS)`.

### Test Status
- Automated: ✅ 282 passed, 1 pre-existing failure (`test_selector.py::test_select_strategy_real_claude_code`), 0 new failures
- New tests: 9 (5 in test_screener_prepare.py, 4 in test_prepare.py)

### Reports Generated

**Execution Report:** `.agents/execution-reports/parallel-download-threadpool.md`
- Detailed implementation summary
- No divergences from plan
- Test results and metrics
- 9/9 new tests pass, 0 new regressions

---

## Feature: Phase 1 — Pre-Market Signal CLI

**Status**: ✅ Complete
**Completed**: 2026-03-25
**Plan File**: .agents/plans/phase-1-pre-market-signal-cli.md

### Core Changes
- `train.py` `screen_day()` — added `current_price: float | None = None` param; `price_1030am` uses injected price when provided; `rsi14` and `res_atr` added to return dict.
- `screener_prepare.py` — builds/refreshes `SCREENER_CACHE_DIR`; `fetch_screener_universe()` (S&P 500 + Russell 1000 + fallback), `is_ticker_current()`, `download_and_cache()` (incremental: fetches only from last cached date forward, merges and deduplicates).
- `screener.py` — pre-market BUY signal scanner; staleness check, gap filter (`GAP_THRESHOLD = -0.03`), armed table sorted by `prev_vol_ratio` desc.
- `position_monitor.py` — RAISE-STOP scanner; reads `portfolio.json`, calls `manage_position()`, prints signals where new_stop > current stop.
- `analyze_gaps.py` — gap vs PnL analysis from `trades.tsv`; exports `load_trades()`, `compute_gaps()`, `print_analysis()`.
- `portfolio.json` — user-maintained template with one example position.

### Test Status
- Automated: ✅ 273 passed, 1 skipped (pre-existing), 0 failed (full suite)
- Baseline before changes: 238 passed, 1 skipped
- New tests: 35 (5 in test_screener.py, 9 in test_screener_prepare.py, 9 in test_screener_script.py, 7 in test_position_monitor.py, 5 in test_analyze_gaps.py)
- Level 4 (live pre-market run): not executed — requires network + live pre-market hours

### Reports Generated

**Execution Report:** `.agents/execution-reports/phase-1-pre-market-signal-cli.md`
- Detailed implementation summary
- Divergences and resolutions (test count discrepancy, inline-test workaround, Level 4 deferral)
- Test results and metrics
- 32/32 new tests pass, 0 new regressions

---

## Feature: Volume Criterion Redesign

**Status**: ✅ Complete
**Completed**: 2026-03-25
**Plan**: system_upgrade_phases.md — "Volume criterion redesign" subsection

### Core Validation
`screen_day()` volume filter replaced with two prior-data-only rules that work correctly pre-market: Rule 3a (`vol_trend_ratio >= 1.0`: 5-day avg >= MA30) and Rule 3b (`prev_vol_ratio >= 0.8`: yesterday >= 0.8× MA30). `today_vol` removed from entry logic entirely. `prev_vol_ratio` and `vol_trend_ratio` added to return dict replacing `vol_ratio`. Tests updated to use `vol_trend_ratio < 1.0` as the mutation target. `strategy_selector.py` fixed to handle `train_pnl = None` in strategy METADATA.

### Test Status
- Automated: ✅ 238 passed, 1 skipped, 0 failed (full suite)
- Pre-existing skip: `test_most_recent_train_commit_modified_only_editable_section` — git artefact from ecbc2d2 (Phase 0.1 harness rename)

### Notes
- Code review (`v5-b-volume-criterion-redesign.md`): 1 High / 2 Medium / 2 Low findings, all pre-existing test structure issues unrelated to this change
- `BACKTEST_START` corrected to `"2024-09-01"` (from `"2025-12-20"`) to match global-mar24 session dates

---

## Feature: Dedicated Small Test Parquet Fixture

**Status**: ✅ Complete
**Completed**: 2026-03-25
**Plan File**: .agents/plans/dedicated-test-parquet-fixture.md

### Core Changes
- `tests/conftest.py` — session-scoped `test_parquet_fixtures` fixture downloads AAPL/MSFT/NVDA via yfinance, caches to `~/.cache/autoresearch/test_fixtures/`. Dates: history `2024-04-01` (yfinance 1h 730-day limit), backtest `2024-09-01..2025-11-01`.
- `train.py` mutable — `_compute_fold_params()` added; auto-detects short windows (< 130 bdays → 1 fold).
- `train.py` `__main__` — fold loop uses `_effective_n_folds` / `_effective_fold_test_days` from `_compute_fold_params`.
- `tests/test_fold_auto_detect.py` — 13 unit tests for fold logic and ticker split constraints.
- `tests/test_e2e.py` — all 9 integration tests wired to `test_parquet_fixtures`; subprocess tests inject `AUTORESEARCH_CACHE_DIR`.
- `tests/test_optimization.py` — 3 `@_live` tests (previously skipped without 389-ticker cache) converted to `@pytest.mark.integration` using the small fixture.
- GOLDEN_HASH updated to `efea3141a0df8870e77df15f987fdf61f89745225fcb7d6f54cff9c790779732`.

### Test Status
- 13/13 `test_fold_auto_detect.py` — ✅ passed
- 9/9 `test_e2e.py -m integration` — ✅ passed
- 3/3 `test_optimization.py` integration tests — ✅ passed
- Full suite: 222 passed, 1 pre-existing failure (`test_most_recent_train_commit_modified_only_editable_section` — git history artefact from commit ecbc2d2, predates this feature), 0 new failures

---

## Feature: P0 — Price Fix and Trade Instrumentation

**Status**: ✅ Complete
**Completed**: 2026-03-24
**Plan File**: .agents/plans/p0-price-fix-and-instrumentation.md

### Core Changes
P0-A: `prepare.py` now uses `Close` of the 9:30 AM bar (not `Open`) and produces column `price_1030am`. All `price_10am` references renamed to `price_1030am` throughout `train.py`, strategy files, and all test files.
P0-B: MFE/MAE (`mfe_atr`, `mae_atr`) added to all trade records, tracked via `high_since_entry`/`low_since_entry` in position dict. Normalized by ATR at entry.
P0-C: `exit_type` field now present in all trade records: `stop_hit`, `end_of_backtest`, `partial`.
P0-D: `r_multiple` added to all trade records: `(exit − entry) / (entry − initial_stop)`.
`_write_trades_tsv` fieldnames expanded to include `exit_type`, `mfe_atr`, `mae_atr`, `r_multiple`. GOLDEN_HASH updated.

### Cache and Ticker Expansion
- TICKERS expanded from 85 → 389 (copied from `global-mar24` worktree): all 10 GICS sectors + high-vol ETFs
- Fresh parquet cache downloaded for all 389 tickers (381/389 succeeded; 8 failed: `DFS`, `FI`, `SQ`, `SKX`, `K`, `PARA`, `HES`, `MRO` — yfinance delisted/symbol conflicts)
- Cache uses correct `price_1030am` (9:30 bar Close) throughout

### Test Status
- Automated: ✅ 210 passed, 0 failed, 0 skipped (all 12 previously-cache-gated tests now run)
- 7 new P0 unit tests pass; 4 `test_e2e.py` tests updated for walk-forward output format and vol_ratio threshold change
- Pre-existing selector tests excluded (collection error, out of scope)

### Reports Generated

**Execution Report:** `.agents/execution-reports/p0-price-fix-and-instrumentation.md`
- Detailed implementation summary
- Divergences and resolutions (T9 deferral, _make_trade_run() design choice)
- Test results and metrics
- 7/7 new tests pass, 0 new regressions

---

## Feature: V4-B Harness Metric Improvements and Position Management Refinements

**Status**: ✅ Complete
**Completed**: 2026-03-24
**Plan File**: .agents/plans/v4-b-harness-metrics.md

### Core Validation
R16: `WALK_FORWARD_WINDOWS=7`, `FOLD_TEST_DAYS=40` in mutable zone. R13: trailing stop tightened 1.5× → 1.2× ATR in `manage_position()`. R15: early stall exit added — if `cal_days_held <= 5` and `price_10am < entry + 0.5×ATR`, stop raised to `max(current_stop, price_10am)`. R11: `avg_win_loss_ratio` computed in `run_backtest()`, emitted in `print_results()`, `discard-fragile` rule added to `program.md`. R2: `min_test_pnl` fold guard excludes folds with < 3 test trades; `min_test_pnl_folds_included` printed. R14: partial close at +1.0R appends `exit_type='partial'` record, halves `position['shares']`, fires exactly once. R4: `results.tsv` header expanded to 13 columns. GOLDEN_HASH updated for R2 + R11 + R14 immutable zone changes.

### Test Status
- Automated: ✅ 139 passed, 0 failures, 0 new regressions
- Baseline: 114 passed, 3 pre-existing failures (test_program_md.py)
- New tests: 20 (14 in test_v4_b.py + 6 in test_backtester.py)
- Pre-existing failures fixed: 3

### Notes
- `test_run_backtest_win_loss_ratio_positive_formula` tests the formula directly (not via full backtest run) — simpler and equivalent for pure arithmetic
- All R2 tests use `train.WALK_FORWARD_WINDOWS` dynamically; no hardcoded fold counts
- R7 (sector concentration guard) not implemented — marked optional/low priority in PRD

### Reports Generated

**Execution Report:** `.agents/execution-reports/v4-b-harness-metrics.md`
- Detailed implementation summary
- Divergences and resolutions (extra tests, formula-direct test, pre-existing fixes)
- Test results and metrics
- 20/20 new tests pass, 0 new regressions

---

---

## Feature: V4-A Strategy Quality and Loop Control

**Status**: ✅ Complete
**Completed**: 2026-03-24
**Plan File**: .agents/plans/v4-a-strategy-quality.md

### Core Validation
R9: `screen_day()` now returns `None` for fallback-stop entries (no structural pivot support); `stop_type` is always `'pivot'` for returning signals. R8: earnings-proximity guard added — entries within 14 calendar days of next earnings are rejected; backward-compatible with old parquet files lacking `next_earnings_date`. R10: `manage_position()` forces exit at `max(current_stop, price_10am)` for positions held >30 business days with unrealised PnL < 30% of RISK_PER_TRADE. `prepare.py` extended with `_add_earnings_dates()` helper. `program.md` updated: FOLD_TEST_DAYS default 20→40 (R1), consistency floor auto-calibrated to `−RISK×MAX_SIMULTANEOUS_POSITIONS×10` (R3), deadlock detection pivot paragraph (R5), position-management priority in iterations 6–10 (R6).

### Test Status
- Automated: ✅ 64 passed (+18 new V4-A unit tests), 0 failures, 0 new regressions
- Pre-implementation baseline: 46 passed (test_screener.py + test_backtester.py)
- Pre-existing collection error in test_selector.py: not in scope, unchanged

### Notes
- Existing parquet files lack `next_earnings_date` column — delete cache and re-run `prepare.py` before next optimization session for R8 to take effect
- Volume threshold co-changed from ≥1.0× to ≥1.9× MA30 alongside R9; fixtures updated accordingly
- GOLDEN_HASH not modified — immutable zone untouched

### Reports Generated

**Execution Report:** `.agents/execution-reports/v4-a-strategy-quality.md`
- Detailed implementation summary
- Divergences and resolutions (volume threshold co-change, ticker_obj re-instantiation)
- Test results and metrics
- 18/18 new tests pass, 0 new regressions

---

## Feature: V3-G Harness Integrity and Objective Quality

**Status**: ✅ Complete
**Completed**: 2026-03-23
**Plan File**: .agents/plans/v3-g-harness-integrity.md

### Core Validation
SESSION SETUP / STRATEGY TUNING comment headers added to `train.py` mutable section; `RISK_PER_TRADE` comment updated with "DO NOT raise to inflate P&L." `program.md` updated with five targeted edits: SESSION SETUP scope instruction, `TICKER_HOLDOUT_FRAC = 0.1` recommended default, dual keep/discard condition (`min_test_pnl` + `train_pnl_consistency` floor), zero-trade plateau early-stop rule (3-iteration direction reversal; 10-iteration `plateau` status + revert), and `discard-inconsistent` status added to column 10 definition. No GOLDEN_HASH update required — immutable zone untouched.

### Test Status
- Automated: ✅ 49 passed (+10 new V3-G unit tests), 1 pre-existing skip (git state), 0 new regressions
- Manual: none required

### Notes
- All 10 V3-G tests are text/import-level assertions — no live parquet cache required
- Baseline before V3-G: 39 passed, 1 skipped
- Box-drawing characters (U+2550 `══`) in headers verified to survive save/re-read without corruption
- `RISK_PER_TRADE` value unchanged at 50.0; `MAX_SIMULTANEOUS_POSITIONS` value unchanged at 5

### Reports Generated

**Execution Report:** `.agents/execution-reports/v3-g-harness-integrity.md`
- Detailed implementation summary
- Divergences and resolutions (none)
- Test results and metrics
- 10/10 new tests pass, 0 new regressions

---

## Feature: V3-F Test-Universe Ticker Holdout and Per-Session Cache Path

**Status**: ✅ Complete
**Completed**: 2026-03-22
**Plan File**: .agents/plans/v3-f-test-universe-holdout-and-cache-path.md

### Core Validation
`TEST_EXTRA_TICKERS: list = []` added to mutable section after `TICKER_HOLDOUT_FRAC`. `CACHE_DIR` replaced with `os.environ.get("AUTORESEARCH_CACHE_DIR", <default>)` in both `train.py` and `prepare.py`. Immutable `__main__` block extended with `_extra_ticker_dfs` / `_test_ticker_dfs` construction; fold test call updated to use `_test_ticker_dfs`. `program.md` updated with session setup docs for both new mechanisms.

### Test Status
- Automated: ✅ 152 passed (+10 new V3-F unit tests), 1 pre-existing skip (git state), 15 pre-existing failures unchanged
- Manual: live-cache scenarios non-blocking per plan

### Notes
- GOLDEN_HASH updated to `912907497f6da52e3f4907a43a0f176a4b71784194f9ebfab5faae133fd20ea9`
- `TEST_EXTRA_TICKERS = []` and env-var fallback preserve all existing behavior — no migration required
- Extra tickers absent from cache are silently skipped via `if t in ticker_dfs` guard

### Reports Generated

**Execution Report:** `.agents/execution-reports/v3-f-test-universe-holdout-and-cache-path.md`
- Detailed implementation summary
- Divergences and resolutions (none)
- Test results and metrics
- 10/10 new tests pass, 0 new regressions

---

## Feature: V3-E Configurable Walk-Forward Window Size and Rolling Training Windows

**Status**: ✅ Complete
**Completed**: 2026-03-22
**Plan File**: .agents/plans/v3-e-configurable-walk-forward.md

### Core Validation
`FOLD_TEST_DAYS = 20` and `FOLD_TRAIN_DAYS = 0` added to the mutable section immediately after `WALK_FORWARD_WINDOWS`. Walk-forward loop in `__main__` updated to use `FOLD_TEST_DAYS` for all fold window calculations; `FOLD_TRAIN_DAYS > 0` rolling-window if/else branch added with `max(...)` clamping against `date.fromisoformat(BACKTEST_START)`. `program.md` step 4b expanded with agent setup guidance. Setting `FOLD_TEST_DAYS=10, FOLD_TRAIN_DAYS=0` reproduces V3-D fold boundaries exactly.

### Test Status
- Automated: ✅ 105 passed, 3 pre-existing failures (unchanged), 1 pre-existing skip
- Manual: Scenarios A/B/C require live parquet cache — accepted non-blocking gaps per plan

### Notes
- GOLDEN_HASH updated from `9fba956b62e48a93d40a8ab6f386c6674bb96bd7efcfef793db198d4a078749e` to `8e52c979a05340df9bef49dbfda0c7086621e6dd2ac2e7c3a9bf12772c04e0a7`
- `FOLD_TRAIN_DAYS = 0` (expanding) preserves all existing backtest behavior
- `program.md` recommends `WALK_FORWARD_WINDOWS = 9` (expanding) or `13` (rolling) for the 19-month window

**Reports:** `.agents/execution-reports/v3-e-configurable-walk-forward.md` | `.agents/code-reviews/v3-e-configurable-walk-forward.md`

---

## Feature: V3-D Diagnostics and Advanced (R6, R10, R11)

**Status**: ✅ Complete
**Completed**: 2026-03-22
**Plan File**: .agents/plans/v3-d-diagnostics.md

### Core Validation
R11 `detect_regime()` added to the immutable zone — cross-sectional SMA50 majority vote classifies each trading day as `'bull'`/`'bear'`/`'unknown'`; regime stored in every trade record and surfaced in `regime_stats` return dict key and `trades.tsv` `regime` column. R10 `_bootstrap_ci()` added; `_write_final_outputs()` prints `bootstrap_pnl_p05:` / `bootstrap_pnl_p95:` when `trade_records` are passed (final run only). R6 deterministic ticker holdout (sorted tail split) added to `__main__`; walk-forward folds use training tickers only; `ticker_holdout_pnl:` / `ticker_holdout_trades:` printed after `min_test_pnl:` when holdout is non-empty. `TICKER_HOLDOUT_FRAC = 0.0` default leaves all existing behavior unchanged.

### Test Status
- Automated: ✅ 39 passed (+15 new V3-D unit tests), 1 pre-existing skip (git state)
- Manual: none required

### Notes
- GOLDEN_HASH updated to `9fba956b62e48a93d40a8ab6f386c6674bb96bd7efcfef793db198d4a078749e`
- `regime_stats: {}` added to early-exit guard return in `run_backtest()` so callers always see the key
- Bootstrap uses `np.random.default_rng(42)` for fully deterministic output; `ci=0.90` so `p05`/`p95` key names match 5th/95th percentile math
- `trades.tsv` DictWriter uses `restval=""` for backward compatibility on records missing `regime`
- `detect_regime()` cached per trading day (hoisted before screening loop) — all entries on the same day share the same regime value
- Code review fixes applied: bootstrap CI label mismatch (ci 0.95→0.90), program.md stop_type values corrected ('pivot'/'fallback'/'unknown'), look-ahead docstring note added

**Reports:** `.agents/execution-reports/v3-d-diagnostics.md` | `.agents/code-reviews/v3-d-diagnostics.md`

---

## Feature: V3-C Portfolio Robustness Controls (R8, R9-price-only)

**Status**: ✅ Complete
**Completed**: 2026-03-22
**Plan**: .agents/plans/v3-c-portfolio-robustness.md

### Core Validation
R8 position cap (`MAX_SIMULTANEOUS_POSITIONS`) and correlation penalty (`CORRELATION_PENALTY_WEIGHT`) added to `run_backtest()`. R9 perturbation loop runs up to 4 jitter seeds (±0.5% price × ±0.3 ATR stop) and exposes `pnl_min` in return dict, `print_results()`, and `trades.tsv` annotation. End-to-end validated via live subprocess test (`test_live_train_py_subprocess_outputs_pnl_min`) that runs `train.py` with real cached data and asserts all fold `pnl_min` lines are present, parseable, and ≤ `total_pnl`.

### Test Status
- Automated: ✅ 24 passed (+9 new V3-C unit tests, +1 live subprocess test), 1 pre-existing skip (git state)
- Manual: none required

### Notes
- All three new constants default to off; existing backtest behavior fully preserved
- Correlation penalty gated on `total_pnl > 0` to prevent sign inversion on losing portfolios
- GOLDEN_HASH updated to `8f2174487376cd0ac3e40a2dc8628ec374cc3753dbfb566cec2c6a16d5857bad`
- `program.md` updated with `discard-fragile` status, loop instructions, and pnl_min grep commands

**Execution Report:** `.agents/execution-reports/v3-c-portfolio-robustness.md`

---

## Feature: V3-B Walk-Forward Evaluation Framework (R2, R4-full, R7)

**Status**: ✅ Complete
**Completed**: 2026-03-22
**Plan**: .agents/plans/v3-b-walk-forward.md

### Core Validation
Walk-forward CV loop (N=3 folds, 10-business-day test windows) implemented in `__main__`. R7 diagnostics (max_drawdown, calmar, pnl_consistency) added to `run_backtest()` and `print_results()`. Silent holdout `[TRAIN_END, BACKTEST_END]` prints `HIDDEN` during optimization loop. `program.md` updated for new output format and `min_test_pnl` keep/discard criterion. Live-cache tests confirmed all R7 metrics finite on real data (NaN guard added for missing `price_10am` rows).

### Test Status
- Automated: ✅ 14 passed (+9 new V3-B unit tests, +2 live-cache tests), 1 pre-existing skip (git state)
- Manual: none required

### Notes
- `WALK_FORWARD_WINDOWS = 3` and `SILENT_END = "2026-02-20"` added to mutable section
- `_exec_main_block()` helper introduced in test file to work around `runpy.run_path` namespace isolation
- NaN guard added to `portfolio_value` computation — all 17 cached tickers have one `price_10am = NaN` row (2026-02-02 data gap); without guard, `max_drawdown` returned NaN
- GOLDEN_HASH updated to `9ed46928eb57190df2e2413c326a73713526fde6f68b068f04ddbd222495baf9`
- Keep/discard criterion changed from `train_total_pnl` to `min_test_pnl`

**Execution Report:** `.agents/execution-reports/v3-b-walk-forward.md`

---

## Feature: V3-A Signal Correctness (R1, R3, R5, R4-partial)

**Status**: ✅ Complete
**Completed**: 2026-03-22
**Plan**: .agents/plans/v3-a-signal-correctness.md

### Core Validation
All four correctness fixes validated via 7 new automated tests plus the GOLDEN_HASH integrity test. `screen_day()` confirmed to pass `df.iloc[:-1]` (not the full df) to `calc_atr14` via a patched recorder; minimum history boundary tested at exactly 60/61 rows; sizing verified via wide-vs-tight stop comparison; `trade_records` schema validated against 8 required fields; `trades.tsv` header confirmed written even on empty input.

### Test Status
- Automated: ✅ 73 passed (+7 new V3-A tests), 9 pre-existing failures (test_registry.py ImportError, unrelated), 1 skipped
- Manual: none required

### Notes
- `RISK_PER_TRADE = 50.0` is in the mutable section; size formula is now `RISK_PER_TRADE / (entry_price - stop)`
- `_write_trades_tsv()` is in the immutable zone (below DO NOT EDIT); `__main__` writes train trades only (not test)
- GOLDEN_HASH updated to `8c797ebed7a436656539ab4d664c2c147372505769a140c29e3c4ad2b483f3c7`
- Code review flagged: `test_run_backtest_risk_proportional_sizing` verifies the math identity, not the actual sizing applied by `run_backtest()` — minor coverage gap, non-blocking

**Detailed Report**: `.agents/execution-reports/v3-a-signal-correctness.md`

---

## Feature: Strategy Registry and LLM Selector (Enhancements 6a + 6b)

**Status**: ✅ Complete
**Completed**: 2026-03-21
**Plan**: .agents/plans/strategy-registry-and-selector.md

### Core Validation
`strategies/` package created with REGISTRY, `base_indicators.py`, and `energy_momentum_v1.py` (extracted from `e9886df`). `strategy_selector.py` calls `claude -p` CLI (no API key — runs inside Claude Code); `_call_claude()` strips `CLAUDECODE` env var before spawning subprocess. Integration test `test_select_strategy_real_claude_code` makes a real CLI call for XOM and validates response shape and strategy validity.

### Test Status
- Automated: ✅ 120 passed, 1 skipped (unit + integration + e2e, full suite via `.venv`)
- Manual: none required

### Notes
- Selector uses `claude -p <prompt>` subprocess — requires Claude Code on PATH; not portable outside Claude Code
- `BOUNDARY` in `extract_strategy.py` is a substring match (`"DO NOT EDIT BELOW THIS LINE"`) to handle Unicode dash decorations in the actual comment
- `ANTHROPIC_API_KEY` removed from `.env` and `pyproject.toml`

---

## Feature: Optimization Harness Overhaul (Enhancements 1–5)

**Status**: ✅ Complete
**Completed**: 2026-03-21
**Plan**: .agents/plans/optimization-harness-overhaul.md

### Core Validation
Train/test split, P&L-based keep/discard, final test CSV output, sector trend summary, and extended results.tsv all implemented and verified via automated tests.

### Test Status
- Automated: ✅ 58/61 passing (3 pre-existing failures unrelated to this feature)
- Manual: none required

---

## Run: mar20 — Energy/Materials Universe (2026-03-20)

Branch: `autoresearch/mar20` | Best commit: `e9886df` | 30 iterations

| commit | sharpe | total_pnl | total_trades | win_rate | status | description |
|--------|--------|-----------|--------------|----------|--------|-------------|
| 48191e7 | 0.000 | $0 | 0 | — | keep | baseline (strict pullback screener, no trades) |
| 2ceed17 | 2.809 | — | 1 | — | keep | NaN guard + CCI -30 + pullback 5% |
| 68ac7ae | 5.176 | — | 18 | — | keep | momentum breakout: SMA50 + 20d high + vol 1.2× |
| b95992a | 5.266 | — | 19 | — | keep | relax volume to 1.0× MA30 |
| 2a3c800 | 5.382 | — | 19 | — | keep | add price_10am > prev day high |
| **e9886df** | **5.791** | **$952.88** | **18** | **77.8%** | **keep** | **add RSI(14) 50–75 filter** |

Avg PnL/trade: **$52.94** | Backtest: 2025-12-20 → 2026-03-20 | Universe: CTVA, LIN, XOM, DBA, SM, IYE, EOG, APA, EQT, CTRA, APD, DVN, BKR, COP, VLO, HEI, HAL

### Multi-Sector Optimization Results (2026-03-20)

| Sector / Run | Branch | Window | Best Sharpe | Trades | Total PnL | Tag |
|---|---|---|---|---|---|---|
| Energy (in-sample) | `autoresearch/mar20` | Dec 20 → Mar 20, 2026 | 5.791 | 18 | — | `energy-momentum-v1` |
| Energy (OOS validation) | `autoresearch/energy-oos-sep25` | Sep 20 → Dec 20, 2025 | -0.010 | 6 | -$85 | — |
| Energy (OOS optimized) | `autoresearch/energy-oos-opt-sep25` | Sep 20 → Dec 20, 2025 | 8.208 | 73 | ~$956 | `energy-oos-v1` |
| Semis | `autoresearch/semis-mar20` | Dec 20 → Mar 20, 2026 | 4.754 | 34 | $602 | `semis-momentum-v1` |
| Utilities | `autoresearch/utilities-mar20` | Dec 20 → Mar 20, 2026 | 4.363 | 29 | $47 | `utilities-breakout-v1` |
| Financials | `autoresearch/financials-mar20` | Dec 20 → Mar 20, 2026 | 6.575 | 45 | **-$60** | `financials-v1` |

**Key finding — Sharpe metric flaw:** Financials produced Sharpe 6.58 with -$60 PnL. `daily_values` is raw mark-to-market sum (not capital returns); on days with no positions it is $0, so any change that holds positions longer reduces stop-management variance → artificially high Sharpe. This is why Sharpe was replaced with `train_total_pnl` as the optimization criterion (Enhancement 2).

**OOS validation result: FAIL (Sharpe -0.01)** — the in-sample energy strategy overfit to Dec–Mar 2026 regime.

**Detailed analysis**: `.agents/progress-archive/mar20-analysis-and-next-steps.md`

---

## Feature: Phase 5 — End-to-End Integration Test + Post-Phase Enhancements

**Status**: ✅ Complete
**Completed**: 2026-03-20
**Plan**: .agents/plans/phase-5-end-to-end.md

### Core Validation
Full pipeline validated: `prepare.py` → parquet cache → `run_backtest()` → output block. 9 integration tests cover schema, Sharpe consistency, output format, and multi-iteration loop simulation.

### Test Status
- Automated: ✅ 86/86 passing (9 new integration + 77 pre-existing)
- Manual: none required

### Notes
- `price_10am` always-NaN bug fixed: yfinance 1h bars label the 9:30 AM bar at 9:30 ET, not 10:00 AM
- Windows cp1252 encoding fix applied to `prepare.py` (`→` → `->` in print statements)
- `manage_position()` breakeven trigger raised from 1×ATR to 1.5×ATR (matches `screen_day()` entry guard)
- `run_backtest()` loop order fixed: manage existing positions before screening for new entries

**Detailed Report**: `.agents/execution-reports/phase-5-end-to-end.md`

---

## Feature: Phase 4 — Agent Instructions (`program.md`)

**Status**: ✅ Complete
**Completed**: 2026-03-19
**Plan**: .agents/plans/phase-4-agent-instructions.md

### Core Validation
Full rewrite of `program.md` from nanochat/GPU instructions to stock Sharpe optimization agent loop. 23 structural tests verify setup steps, output format, TSV schema, loop instructions, and cannot-modify constraints.

### Test Status
- Automated: ✅ 74/74 passing (23 new + 51 pre-existing)
- Manual: none required

---

## Feature: Phase 3 — Strategy + Backtester (`train.py`)

**Status**: ✅ Complete
**Completed**: 2026-03-18
**Plan**: .agents/plans/phase-3-strategy-backtester.md

### Core Validation
`manage_position()`, `run_backtest()`, `print_results()`, and `__main__` implemented and tested. Stop detection uses `prev_day` low (no look-ahead); Sharpe `std == 0` guard returns 0.0; `grep "^sharpe:"` captures exactly one parseable float.

### Test Status
- Automated: ✅ 51/51 passing (15 new + 36 pre-existing)
- Manual: none required

---

## Feature: Phase 2 — Data Layer (`prepare.py`)

**Status**: ✅ Complete
**Completed**: 2026-03-18
**Plan**: .agents/plans/phase-2-data-layer.md

### Core Validation
yfinance OHLCV downloader implemented; `price_10am` extracted from 9:30 AM bar; parquet cache writes confirmed. `HISTORY_START` set to 1 year before backtest start (yfinance 730-day rolling limit on 1h data).

### Test Status
- Automated: ✅ 16/16 passing (14 mock + 1 integration + 1 subprocess)
- Manual: none required

---

## Feature: Feature 2 — Screener (`screen_day`)

**Status**: ✅ Complete
**Completed**: 2026-03-18
**Plan**: .agents/plans/feature-2-screener.md

### Core Validation
11-rule momentum breakout screener implemented. All indicator helpers present and importable. Acceptance criteria validated 21/22 (1 unverifiable pending parquet cache).

### Test Status
- Automated: ✅ 20/20 passing (19 original + 1 added in code review)
- Manual: none required

---

## System Ready

All phases complete. Pipeline operational:

1. `uv run prepare.py` — downloads and caches OHLCV data for the configured tickers
2. `uv run train.py` — runs the backtest, prints a fixed-format results block
3. Agent loop (via `program.md`) — autonomously mutates `train.py`, commits, backtests, keeps or reverts

To start an experiment session: open a Claude Code conversation in this repo and describe your desired run parameters (tickers, timeframe, iterations).

---

## Feature: Strategy Refactor — Stateful Scanner, Perf, and Cleanup

**Status**: ✅ Complete (Phases 1–5; Phase 0 and RC1–RC6 deferred)
**Plan File**: `.agents/plans/strategy_refactor.md`

### Reports Generated

**Execution Report:** `.agents/execution-reports/strategy_refactor.md`
- Phases 1–5 complete; `process_scan_bar` is the single source of truth for all scanning logic
- 709 passed, 9 skipped, 0 regressions; backtest parity verified
- Phase 0 dead-code cleanup and RC1/RC3 live bar-resolution fixes deferred
- Alignment score: 7/10

---

## Feature: SMT Strategy Refactor (Bar Globals + exit_market + File Split)

**Status**: ✅ Complete
**Plan File**: `.agents/plans/smt-strategy-refactor.md`

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-strategy-refactor.md`
- Detailed implementation summary
- Divergences and resolutions (MAX_TDO_DISTANCE_PTS patches, MAX_REENTRY_COUNT patches, dual-module patching, test_signal_smt.py patch-target fix)
- Test results: 434 passed, 10 skipped, 0 failures
- Manual validation (backtest_smt.py output vs pre-refactor baseline) deferred — requires Databento parquets

---

## Feature: SMT Quality-Focused Parameter Extensions

**Status**: ✅ Complete
**Plan File**: `.agents/plans/smt-quality-params.md`

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-quality-params.md`
- Detailed implementation summary
- Divergences and resolutions (test count discrepancy, test fixture adaptation, TDO geometry fix)
- Test results and metrics: 79/79 passing (+11 new tests, 3 existing tests fixed)
- All 9 tasks completed; all acceptance criteria met

---

## Next: SMT Parameter Optimization Run

**Status**: ✅ Planned
**Date scoped**: 2026-04-02
**Plan File**: .agents/plans/smt-optimization-run-setup.md

### Baseline (post-Plans-1-2-3 defaults, full 6-fold run 2024-09-01 → 2026-03-20)

- **Data**: `data/historical/MNQ.parquet` + `data/historical/MES.parquet`
  - Source: Databento GLBX.MDP3, MNQ.v.0 / MES.v.0 (volume-roll continuous), `stype_in="continuous"`
- **Walk-forward**: 6 folds × 60 business-day test windows

| Fold | Test Trades | Win Rate | Test PnL | avg_rr |
|------|------------|----------|----------|--------|
| 1    | 92         | 65.2%    | $2,677   | 2.05   |
| 2    | 111        | 84.7%    | $7,679   | 2.03   |
| 3    | 100        | 75.0%    | $4,832   | 2.45   |
| 4    | 119        | 69.8%    | $4,263   | 2.14   |
| 5    | 77         | 76.6%    | $3,398   | 1.37   |
| 6    | 70         | 90.0%    | $5,731   | 2.92   |

- **mean_test_pnl**: $4,763 | **min_test_pnl**: $2,677 | **Total test trades**: 569
- **Avg WR**: 76.9% | **Avg avg_rr**: 2.17

> **Pre-Plans-1-3 historical reference**: 105 trades, 52% WR, $830/fold.

### Parameters at baseline

```python
SESSION_START          = "09:00"
SESSION_END            = "13:30"
MAX_TDO_DISTANCE_PTS   = 15.0
MAX_REENTRY_COUNT      = 1
SIGNAL_BLACKOUT_START  = "11:00"
SIGNAL_BLACKOUT_END    = "13:00"
SHORT_STOP_RATIO       = 0.35
LONG_STOP_RATIO        = 0.35
TRAIL_AFTER_TP_PTS     = 1.0
HIDDEN_SMT_ENABLED     = True       # Plan 1 approved
PARTIAL_EXIT_ENABLED   = True       # Plan 2 approved
PARTIAL_EXIT_FRACTION  = 0.33
SMT_OPTIONAL           = True       # Plan 3 approved
DISPLACEMENT_STOP_MODE = True       # Plan 3 approved
PARTIAL_EXIT_LEVEL_RATIO = 0.33     # Plan 3 approved
```

### Proposed tweaks for next run (priority order)

#### 1. Asymmetric stop ratios — HIGHEST PRIORITY
`LONG_STOP_RATIO` and `SHORT_STOP_RATIO` are both `0.45` (RR ≈ 2.22 for both sides).
Longs and shorts perform differently across folds (e.g., fold 1 test: long $334 / short $472;
fold 6: long $472 / short $225). Tuning each independently is the lever most likely to improve
the worst-fold score.
- Suggested search space: `LONG_STOP_RATIO` ∈ [0.30, 0.55], `SHORT_STOP_RATIO` ∈ [0.30, 0.55]
- Optimise for: `mean_test_pnl` (primary), `min_test_pnl` > 0 (guard)

#### 2. Session window — HIGH PRIORITY
Kill zone is 9:00–10:30. Pre-cash (9:00–9:30) and RTH open (9:30–10:30) behave differently.
Pre-9:30 divergences target a TDO that hasn't printed yet, making those signals more speculative.
- Candidates: `("09:30", "10:30")`, `("09:00", "10:00")`, `("09:30", "11:00")`
- Watch: narrowing window reduces trade count — need ≥80 test trades total to stay meaningful

#### 3. Minimum divergence magnitude — MEDIUM PRIORITY
`detect_smt_divergence` fires on any MES breach, even 0.25 points past the session
high/low. A weak liquidity sweep is less meaningful than a decisive one.
- Add a `MIN_DIVERGENCE_POINTS` constant (e.g., 2–8 MNQ points) — currently absent.
- Expected effect: fewer signals, lower stop-out rate, higher avg_rr.

#### 4. MIN_BARS_BEFORE_SIGNAL — MEDIUM PRIORITY
Currently 5 bars = 25 min warm-up. ~50% stop-out rate suggests signals may still fire
too early before structure is established.
- Suggested search space: 2–8 bars (10–40 min at 5m)
- Trade-off: more bars = fewer signals but stronger context.

#### 5. Entry confirmation tightening — LOWER PRIORITY
`find_entry_bar` accepts any bearish/bullish bar whose wick pierces a prior close.
Requiring the confirmation bar to also close in the top/bottom X% of its range
(e.g., close in bottom 30% for shorts) would ensure conviction rather than a wick touch.
- Add `ENTRY_CLOSE_STRENGTH` constant ∈ [0.0, 0.5] (0.0 = current behaviour, disabled)

#### 6. TDO definition variant — LOWER PRIORITY
`compute_tdo` uses the 9:30 RT open. For pre-9:30 signals, TDO is unknown at signal
time. Alternatives:
- Previous session close
- 4am globex open (first available bar)
- Flag pre-9:30 signals separately and compare their stats vs post-9:30

### Optimisation objective

Primary: **maximise `mean_test_pnl`** (average fold P&L) — maximises total return across regimes.
Secondary: **`min_test_pnl` > 0** (all qualified folds profitable), **Sharpe ≥ 2.0 in every fold**, total test trades ≥ 80.

### Agent instructions for next run

1. Read this section and `train_smt.py` (above the boundary at line 436).
2. Implement tweaks in priority order; each tweak is an independent constant change.
3. Run `uv run python train_smt.py` after each change and compare `mean_test_pnl` (primary) and `min_test_pnl` (guard).
4. Keep changes that improve `mean_test_pnl` without dropping total test trades below 80 or `min_test_pnl` below 0.
5. Do not modify anything below `# DO NOT EDIT BELOW THIS LINE` (line 436).
6. Update this section with results after each accepted change.

### Reports Generated

**Execution Report:** `.agents/execution-reports/smt-optimization-run-setup.md`
- Detailed implementation summary
- Divergences and resolutions
- Test results and metrics
