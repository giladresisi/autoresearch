# Feature: SMT V2 Part B — Consumer Relevance Rules (Shadow, pre-Phase-3)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations
- **This plan executes in the `entry-stuff` worktree (`C:\Users\gilad\projects\auto-co-trader\entry-stuff`) ONLY.** Do not touch `../smt-stuff`, `../live`, or any sibling worktree.
- **NEVER use `git stash`** — this worktree shares a stash store with concurrent `live` and `smt-stuff` worktrees; stashing risks their data.
- **SHADOW-ONLY.** Every change here computes relevance into `hypothesis.json` *debug keys* and must NOT alter `direction` or anything the strategy/executor reads. Wiring relevance into entry/exit is **Phase 3** and is explicitly OUT OF SCOPE. The proof of correctness is: **trades/P&L identical to baseline on every regression day.**

Validate documentation and codebase patterns before implementing. Match the existing relevance-filter style (pure functions, total/never-raise, exception-isolated shadow block).

---

## Feature Description

The SMT V2 producer (in `smt-stuff`, committed as `6fcea82`) now emits a terminal **`invalidated`** state on `detect_state` plus a `smt_invalidations.json` trail. Producer-side analysis proved invalidation is an **informative filter** (invalidated signals fulfill 46% within 180m vs 95% for kept; base rate 69%) and that producer *geometry* is not the lever (reclaim-vs-point was a wash, 51% vs 52%). **The lever is consumer-side relevance** — deciding which SMTs the dominant-selection should drop or down-rank.

This feature adds those relevance rules to entry-stuff's existing **shadow** relevance filter (`hypothesis.py` + `smt_detect.py` Contract C + the `session_pipeline._run_smt_v2_detection` shadow block), so their effect can be **measured before Phase 3** wires relevance into entry/exit:

1. **`invalidated` awareness** — Contract C reports `invalidated`; the active-set drops invalidated records.
2. **Rule A — same-level latest-take-out-wins** (ship enabled): a newer opposite-direction SMT on the SAME level supersedes the older.
3. **Rule B — recency-trend cross-tier suppression** (gated, default OFF, measured): a fresher opposite-direction SMT suppresses an older contradicting one across tiers.
4. **Leg-scoped counter-trend suppression** (the structural idea): after a FIXED level is swept-and-reclaimed, suppress dominant counter-trend SMTs that predate the reclaim until price returns to the swept origin.
5. **Consumer trail** — emit `superseded_*`/`suppressed_*` events to a debug key for analysis.

## User Story

As a **strategy researcher**, I want **the consumer relevance filter to drop/supersede stale and counter-trend SMTs (even higher-tier ones) and to record why**, so that **the dominant SMT reflects the currently-relevant signal — verifiable in shadow on labeled cases before any P&L is at stake in Phase 3.**

## Problem Statement

`smt_authority` ranks week > day > fill > session, so a **stale higher-tier** SMT stays `dominant` even after price has invalidated it or a fresher opposite signal has appeared. The motivating case: on 2026-06-03 a `prev1_week_high|short` (week tier) fires at 09:49 at the bottom of a V-reversal and remains baseline-dominant through 10:00 while price rallies 240 pts — confusing any consumer of `dominant`.

## Solution Statement

Layer relevance rules onto the existing shadow pipeline: drop `invalidated` records, supersede same-level direction flips (Rule A), optionally suppress older contradicting signals by recency across tiers (Rule B, gated), and apply a structural leg-scoped suppression keyed to the most-recently swept-and-reclaimed FIXED level. All effects land only in `smt_active_set`/`smt_dominant`/new debug keys. Validate by diffing the shadow `smt_dominant` series **with vs without** the rules across regression days; trades stay identical by construction.

## Feature Metadata

**Feature Type**: Enhancement (relevance rules) + Observability (consumer trail), SHADOW-only
**Complexity**: Medium–High
**Primary Systems Affected**: `entry-stuff/hypothesis.py`, `entry-stuff/smt_detect.py` (Contract C), `entry-stuff/session_pipeline.py` (shadow block)
**Dependencies**: Producer `st["invalidated"]` flag — see Dependency note below
**Breaking Changes**: No. Shadow-only; trade/strategy path untouched.

### ⚠️ Dependency — producer `invalidated` flag

The `invalidated` handling (Task wave 1) requires `detect_state[...]["invalidated"]` to exist, which is produced by the **smt-stuff Part A** change (`6fcea82`). entry-stuff gets it when it **rebases onto master after Part A merges**. Until then, `smt_status` simply never returns `"invalidated"` (the flag is absent → treated as unfulfilled), so the code is forward-compatible and safe to land first. Rules A / Rule B / leg-scoped do NOT depend on the producer flag and can be validated immediately. State this ordering in the execution notes.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING (entry-stuff)

- `hypothesis.py` (lines ~244–437) — `_tier_rank`, `to_record`, `smt_authority`, `dominant`, `ingest_smts` (3-step pipeline: drop fulfilled/ineligible → position-state gate → dedup/supersede by key newer-wins), `RELEVANCE_X_PTS=25.0`. Rules A/B and the consumer trail attach here.
- `smt_detect.py` (lines ~530–579) — Contract C: `_record_key`, `fulfillment_status` → returns `unfulfilled|fulfilled|gone`. Extend to `smt_status` adding `invalidated`.
- `session_pipeline.py` (the `_run_smt_v2_detection` shadow block, ~lines 1698–1736) — invalidate-before-ingest (`fulfillment_status` → drop fulfilled/gone) → `ingest_smts` → `dominant` → store under `smt_active_set`/`smt_dominant` debug keys, wrapped in try/except (exception-isolated, zero behavior change). The current MNQ close (`mnq_bar_row["Close"]`) and level prices are in scope here for the adverse-move gate / leg tracking.
- `tests/test_smt_relevance.py` — existing Contract B unit tests + shadow inertness/exception-isolation assertions. Mirror its builders (`_emission(**overrides)`) and its "existing suite green == parity" shadow assertions.
- `regression.py` (`--mode {1m,1s} --dates ...`) — the shadow multi-day driver.

### Cross-worktree reference (READ-ONLY, do NOT import or edit)

- `../smt-stuff/_shadow_smt_analysis.py` and `../smt-stuff/_verify_invalidation.py` — the analysis pattern to mirror for the consumer shadow comparison (load run-dir artifacts + 1s parquet, reconstruct the dominant series, diff with/without rules). Reimplement locally in entry-stuff; do not import across worktrees.

### New Files to Create (entry-stuff)

- `_shadow_relevance_analysis.py` — throwaway multi-day shadow comparison of the `smt_dominant` series with vs without each rule (underscore-prefixed per repo convention; leave UNSTAGED).
- `tests/test_smt_relevance_rules.py` — unit tests for the new rules (or extend `tests/test_smt_relevance.py`).

### Patterns to Follow

**Naming/Style**: pure, total functions that never raise (mirror `ingest_smts`/`smt_authority`); flags as module constants (`RULE_B_ENABLED`, `RULE_B_MIN_AGE_MIN`, `RULE_B_ADVERSE_PTS`, `RULE_B_TIER_SLACK`, `RECLAIM_MARGIN_PTS`). **Production-silent** — the consumer trail is structured data in a `hypothesis.json` debug key, NOT prints/plots. The shadow block stays inside its `try/except` so any rule bug can never perturb trades.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────────────────────┐
│ WAVE 1: Pure relevance primitives (parallel) + their unit tests     │
├────────────────────────────────────────────────────────────────────┤
│ 1.1 smt_status (Contract C)   │ 1.2 Rule A in ingest_smts          │
│ 1.3 Rule B (gated) primitive  │ 1.4 leg-scope detect+suppress fn   │
│ 1.5 unit tests (against the interface contracts)                    │
└────────────────────────────────────────────────────────────────────┘
                                  ↓
┌────────────────────────────────────────────────────────────────────┐
│ WAVE 2: Shadow-block wiring + consumer trail (sequential)           │
├────────────────────────────────────────────────────────────────────┤
│ 2.1 wire 1.1–1.4 into the session_pipeline shadow block + trail     │
│ 2.2 shadow inertness/parity tests (trades unchanged)                │
└────────────────────────────────────────────────────────────────────┘
                                  ↓
┌────────────────────────────────────────────────────────────────────┐
│ WAVE 3: Multi-day shadow validation (sequential)                    │
├────────────────────────────────────────────────────────────────────┤
│ 3.1 _shadow_relevance_analysis.py: dominant series with/without     │
│     rules across UP/DOWN/CHOP days; labeled-case verification       │
└────────────────────────────────────────────────────────────────────┘
```

### Parallelization Summary
**Wave 1** — 1.1/1.2/1.3/1.4 are independent pure functions; 1.5 authored against their contracts. **Wave 2** — single integrator (shared shadow block, conflict risk) + parity tests. **Wave 3** — sequential analysis needing Waves 1–2. ~40% of tasks parallelizable.

### Interface Contracts

- **C-STATUS**: `smt_status(keys, detect_state) -> {key: "unfulfilled"|"fulfilled"|"invalidated"|"gone"}`. `invalidated` checked (via `st.get("invalidated") is True`) BEFORE the unfulfilled fallback; `fulfilled` precedence preserved. `fulfillment_status` kept as a thin wrapper (existing callers/tests unaffected).
- **C-RULEA**: in `ingest_smts`, after the existing key-dedup, group the resulting active set by `ref_name`; if a single `ref_name` holds BOTH directions, keep only the most-recent (by `time`); the dropped one yields a `superseded_same_level` event. Same-direction sets are untouched.
- **C-RULEB**: `apply_rule_b(active, *, now_close, enabled, min_age_min, adverse_pts, tier_slack) -> (kept, suppressed_events)`. A record `O` is suppressed iff a newer opposite-direction `N` exists with `N.time - O.time >= min_age_min`, price has moved against `O` by `>= adverse_pts` (using `now_close` vs `O.mnq_lvl_price`/`fire`), and `_tier_rank(N.tier) >= _tier_rank(O.tier) - tier_slack`. No-op when `enabled=False`.
- **C-LEG**: `update_leg(leg_state, *, fixed_levels, now_close, now_time) -> leg_state` tracks the most-recently swept-and-reclaimed FIXED level (breach beyond by margin, then close back through it), recording `{level_name, level_price, origin_price, reclaim_time, recovery_dir}`. `suppress_counter_trend(active, leg_state, now_close) -> (kept, suppressed_events)` drops dominant-eligible SMTs whose `direction` opposes `recovery_dir` AND whose `time < reclaim_time`, until `now_close` returns to `origin_price` (then the leg clears). **Pick the level dynamically — do NOT hardcode `prev_day_low`** (on 2026-06-03 the swept level was `prev1_week_high`).

**Mock for parallel work**: 1.5 builds records via the existing `_emission(**overrides)` builder against the contracts above; no integration needed until Wave 2.

### Synchronization Checkpoints
- After Wave 1: `uv run python -m pytest tests/test_smt_relevance.py tests/test_smt_relevance_rules.py -q`
- After Wave 2: `uv run python -m pytest tests/test_smt_relevance.py tests/test_session_pipeline.py -q` (parity/inertness)
- After Wave 3: `uv run python _shadow_relevance_analysis.py` → labeled cases pass; trades unchanged on all days

---

## IMPLEMENTATION PLAN

### Phase 1: Pure relevance primitives
Implement `smt_status`, Rule A (inside `ingest_smts`), Rule B (gated standalone), and the leg-scope detector/suppressor as pure, total functions in `smt_detect.py` / `hypothesis.py`. No pipeline wiring yet.

### Phase 2: Shadow-block integration + consumer trail
Wire the primitives into `_run_smt_v2_detection`'s shadow block in dependency order: invalidate-before-ingest (now via `smt_status`, dropping `fulfilled|gone|invalidated`) → `ingest_smts` (Rule A inside) → Rule B (gated) → leg-scoped suppression → `dominant`. Accumulate `superseded_*`/`suppressed_*` events into a `hypothesis.json` debug key (`smt_suppressions`) alongside `smt_active_set`/`smt_dominant`. Keep the whole block exception-isolated; assert nothing the strategy reads changes.

### Phase 3: Multi-day shadow validation
`_shadow_relevance_analysis.py` reconstructs the `smt_dominant` series per bar with vs without each rule across the 8 regime-diverse days, prints the dominant-flip timeline, and checks labeled cases.

---

## STEP-BY-STEP TASKS

**Task keywords**: CREATE · UPDATE · ADD · REMOVE · REFACTOR · MIRROR

### WAVE 1: Pure primitives + tests

#### Task 1.1: ADD `smt_status` to `smt_detect.py` (Contract C extension)
- **WAVE**: 1 · **AGENT_ROLE**: detection-api · **DEPENDS_ON**: [] · **PROVIDES**: C-STATUS
- **IMPLEMENT**: `smt_status(keys, detect_state)` returning `unfulfilled|fulfilled|invalidated|gone`; `invalidated` when `st.get("invalidated") is True` and not fulfilled; keep `fulfilled` precedence; `gone` when absent. Re-implement `fulfillment_status` as a wrapper that collapses `invalidated`→`unfulfilled` so existing callers/tests are unaffected.
- **PATTERN**: `smt_detect.py:553` (`fulfillment_status`). **VALIDATE**: `uv run python -m pytest tests/test_smt_relevance.py -q -k status`

#### Task 1.2: ADD Rule A to `hypothesis.py::ingest_smts`
- **WAVE**: 1 · **AGENT_ROLE**: relevance-core · **DEPENDS_ON**: [] · **PROVIDES**: C-RULEA
- **IMPLEMENT**: after the existing newer-wins key-dedup, a same-`ref_name` opposite-direction collapse keeping the most recent; return the dropped keys so the caller can log `superseded_same_level`. Add `invalidated` drop to step (1) (`rec.get("invalidated") is True → continue`) and carry `invalidated` through `to_record`.
- **PATTERN**: `hypothesis.py:399-437`. **VALIDATE**: `pytest -q -k rule_a`

#### Task 1.3: ADD gated Rule B primitive to `hypothesis.py`
- **WAVE**: 1 · **AGENT_ROLE**: relevance-core · **DEPENDS_ON**: [] · **PROVIDES**: C-RULEB
- **IMPLEMENT**: `apply_rule_b(...)` + constants `RULE_B_ENABLED=False`, `RULE_B_MIN_AGE_MIN`, `RULE_B_ADVERSE_PTS`, `RULE_B_TIER_SLACK`. Pure; returns `(kept, suppressed_events)`; no-op when disabled.
- **VALIDATE**: `pytest -q -k rule_b`

#### Task 1.4: ADD leg-scope detector + suppressor to `hypothesis.py`
- **WAVE**: 1 · **AGENT_ROLE**: relevance-core · **DEPENDS_ON**: [] · **PROVIDES**: C-LEG
- **IMPLEMENT**: `update_leg(...)` + `suppress_counter_trend(...)` + `RECLAIM_MARGIN_PTS`. Detect a FIXED level breached-then-reclaimed; suppress counter-`recovery_dir` SMTs older than `reclaim_time` until `now_close` returns to `origin_price`. Dynamic level selection (never hardcode `prev_day_low`). `leg_state` is a small dict persisted across bars (stored under a debug key in Wave 2).
- **VALIDATE**: `pytest -q -k leg`

#### Task 1.5: CREATE `tests/test_smt_relevance_rules.py`
- **WAVE**: 1 · **AGENT_ROLE**: test-engineer · **DEPENDS_ON**: [] (write against contracts)
- **IMPLEMENT** — named cases: `test_smt_status_invalidated`; `test_status_fulfilled_precedence`; `test_ingest_drops_invalidated`; `test_rule_a_newer_opposite_supersedes_same_level`; `test_rule_a_noop_same_direction`; `test_rule_b_suppresses_when_all_gates_met`; `test_rule_b_noop_when_disabled`; `test_rule_b_respects_min_age_and_adverse_and_tier_slack`; `test_leg_detect_sweep_then_reclaim`; `test_leg_suppresses_older_counter_trend_until_origin`; `test_leg_does_not_suppress_post_reclaim_or_aligned`; `test_consumer_trail_event_schema`.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_relevance_rules.py -q`

**Wave 1 Checkpoint**: `uv run python -m pytest tests/test_smt_relevance.py tests/test_smt_relevance_rules.py -q`

### WAVE 2: Shadow-block wiring + trail

#### Task 2.1: UPDATE the `_run_smt_v2_detection` shadow block + consumer trail
- **WAVE**: 2 · **AGENT_ROLE**: pipeline-integrator · **DEPENDS_ON**: [1.1,1.2,1.3,1.4]
- **IMPLEMENT**: order = `smt_status`-based invalidate-before-ingest (drop `fulfilled|gone|invalidated`) → `ingest_smts` (Rule A inside) → `apply_rule_b` (gated, pass `now_close`) → `update_leg`+`suppress_counter_trend` → `dominant`. Persist `leg_state` and accumulate `superseded_*`/`suppressed_*` into a `smt_suppressions` debug key next to `smt_active_set`/`smt_dominant`. Keep the entire block inside its existing `try/except`. Touch NO field the strategy/executor reads.
- **PATTERN**: `session_pipeline.py:1698-1736`. **VALIDATE**: `uv run python -m pytest tests/test_session_pipeline.py -q`

#### Task 2.2: ADD shadow parity/inertness tests
- **WAVE**: 2 · **AGENT_ROLE**: test-engineer · **DEPENDS_ON**: [2.1]
- **IMPLEMENT**: `test_shadow_block_does_not_change_direction_or_position`; `test_suppressions_debug_key_populated_and_isolated`; `test_shadow_exception_isolated` (a raising rule cannot perturb outputs). Mirror the existing shadow-inertness assertions in `tests/test_smt_relevance.py`.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_relevance.py tests/test_session_pipeline.py -q`

**Wave 2 Checkpoint**: parity suite green (existing-suite-green == no behavior change).

### WAVE 3: Multi-day shadow validation (sequential)

#### Task 3.1: CREATE `_shadow_relevance_analysis.py` + run it
- **WAVE**: 3 · **AGENT_ROLE**: analysis · **DEPENDS_ON**: [2.1]
- **IMPLEMENT**: for each of 2026-05-06, 05-12, 05-20, 05-27, 05-29, 06-03, 06-05, 06-10 (UP/DOWN/CHOP), reconstruct the per-bar `smt_dominant` WITH vs WITHOUT each rule (replay the shadow active-set logic over the run's `events_1s.jsonl` + `smt_invalidations.json` + 1s parquet, mirroring `../smt-stuff/_shadow_smt_analysis.py`). Report the dominant-flip timeline and verify labeled cases.
- **LABELED CASES** (must hold): on 2026-06-03, `prev1_week_high|short` must DROP OUT of `dominant` by ~09:51–09:52 once invalidated/superseded/leg-suppressed (it stays baseline-dominant through 10:00 without the rules); the corrective `prev5/prev7_day_high|long` bullishes must NOT be dropped by Rule A/leg (they are not counter-trend in their leg).
- **VALIDATE**: `uv run python regression.py --mode 1s --dates 2026-06-03` then `uv run python _shadow_relevance_analysis.py` — labeled cases pass AND each day's trade line is unchanged vs baseline.

**Final Checkpoint**: labeled dominant-flips confirmed across regimes; trades/P&L identical on all days; Rule B OFF by default.

---

## TESTING STRATEGY

**⚠️ ALL tests automatable — pure Python `pytest` + a regression replay.**

| What | Tool |
|---|---|
| Pure rules (status, Rule A/B, leg) | `pytest` (`tests/test_smt_relevance_rules.py`) |
| Shadow inertness / parity | `pytest` (`tests/test_smt_relevance.py`, `tests/test_session_pipeline.py`) |
| Multi-day dominant-flip + trade-neutrality | `regression.py` + `_shadow_relevance_analysis.py` |

### Unit Tests
**Status**: ✅ Automated · **Tool**: pytest · **Location**: `tests/test_smt_relevance_rules.py` (+ existing `tests/test_smt_relevance.py` stay green) · **Run**: `uv run python -m pytest tests/test_smt_relevance_rules.py tests/test_smt_relevance.py -q`

### Integration / Shadow-parity Tests
**Status**: ✅ Automated · **Tool**: pytest · **Location**: `tests/test_session_pipeline.py` · **Run**: `uv run python -m pytest tests/test_session_pipeline.py -q`

### End-to-End (shadow replay)
**Status**: ✅ Automated · **Tool**: `regression.py` + `_shadow_relevance_analysis.py` (1s; `ACT_NO_BROWSER=1`; PowerShell `$env:ACT_NO_BROWSER=1`). Assert labeled dominant-flips + unchanged trade lines.

### Edge Cases
- `invalidated` flag absent (pre-rebase) → `smt_status` returns `unfulfilled`, no drop → ✅ `test_smt_status_invalidated` (absent-flag branch)
- Rule A same-direction set → untouched → ✅ `test_rule_a_noop_same_direction`
- Rule B disabled → identity → ✅ `test_rule_b_noop_when_disabled`
- Leg cleared once price returns to origin → ✅ `test_leg_suppresses_older_counter_trend_until_origin`
- Raising rule inside shadow block → isolated, trades unaffected → ✅ `test_shadow_exception_isolated`

### Script deliverables check (`_shadow_relevance_analysis.py`)
- ✅ "Running `_shadow_relevance_analysis.py` completes without raising."
- ✅ "All output is ASCII-safe (or stdout reconfigured at startup)."

### Test Automation Summary
| | Count | % |
|---|---|---|
| ✅ Unit (pytest) | 12 | |
| ✅ Shadow-parity (pytest) | 3 | |
| ✅ E2E/shadow replay | 1 | |
| ⚠️ Manual | 0 | |
| **Total** | 16 | 100% |

---

## VALIDATION COMMANDS

### Side-effecting test policy (full-suite runs)
- **Run side-effecting tests during validation?** ☑ No (default)
- **Deselect command:** `uv run python -m pytest tests/ -q -m "not ib and not external and not lifecycle"` (or `--ignore` the IB/orchestrator suites by path). A live orchestrator/IB feed is assumed running — keep those deselected.

### Level 1: Syntax
`uv run python -c "import hypothesis, smt_detect, session_pipeline"`
### Level 2: Unit
`uv run python -m pytest tests/test_smt_relevance_rules.py tests/test_smt_relevance.py -q`
### Level 3: Shadow parity
`uv run python -m pytest tests/test_session_pipeline.py -q`
### Level 4: Multi-day shadow replay
`# PowerShell: $env:ACT_NO_BROWSER=1`; `uv run python regression.py --mode 1s --dates 2026-06-03`; `uv run python _shadow_relevance_analysis.py` → labeled cases pass; trades unchanged.

### Baseline (run BEFORE implementation)
`uv run python -m pytest tests/ -q -m "not ib and not external and not lifecycle"` — record pass/fail/skip.

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `smt_status` returns `unfulfilled|fulfilled|invalidated|gone`; `invalidated` before unfulfilled; `fulfilled` precedence; absent flag → `unfulfilled`. `fulfillment_status` wrapper keeps existing callers green.
- [ ] `ingest_smts` drops `invalidated` records and carries `invalidated` via `to_record`.
- [ ] Rule A: newer opposite-direction same-`ref_name` SMT supersedes the older; same-direction sets untouched; emits `superseded_same_level`.
- [ ] Rule B: suppresses only when min-age AND adverse-move AND tier-slack gates all hold; identity when `RULE_B_ENABLED=False`; emits `suppressed_by_trend`.
- [ ] Leg-scope: detects a FIXED level swept-and-reclaimed (level chosen dynamically, never hardcoded); suppresses counter-`recovery_dir` SMTs older than `reclaim_time` until price returns to `origin_price`; emits `suppressed_by_leg`.
- [ ] Consumer trail `smt_suppressions` populated alongside `smt_active_set`/`smt_dominant`.

### Shadow-safety (the core gate)
- [ ] The shadow block changes NO field the strategy/executor reads; whole block stays exception-isolated.
- [ ] Trades/P&L identical to baseline on every regression day (2026-05-06/05-12/05-20/05-27/05-29/06-03/06-05/06-10).

### Validation
- [ ] Labeled case: 2026-06-03 `prev1_week_high|short` drops out of `dominant` by ~09:51–09:52 with the rules (vs baseline-dominant through 10:00); corrective day-high bullishes NOT dropped — verified by `_shadow_relevance_analysis.py`.
- [ ] Unit + shadow-parity suites pass; no NEW failures vs recorded baseline.
- [ ] Rule B OFF by default.
- [ ] Changes left UNSTAGED.

### Out of Scope
- Wiring relevance into entry/exit (that is **Phase 3**).
- Any change to `../smt-stuff` or producer code.
- Producer invalidation geometry (settled: point-threshold; reclaim was a wash).

---

## COMPLETION CHECKLIST
- [ ] Baseline recorded before implementation
- [ ] Waves 1–3 complete in order; each task validation passed
- [ ] All 16 automated tests created and passing
- [ ] Labeled dominant-flip confirmed; trades unchanged on all days
- [ ] Rule B default OFF; leg level chosen dynamically (no hardcoded `prev_day_low`)
- [ ] Debug logs added during execution REMOVED
- [ ] **entry-stuff ONLY; no `git stash`; changes UNSTAGED — NOT committed; `../smt-stuff` untouched**

---

## NOTES

- **Why shadow-before-Phase-3 works**: the relevance active set + dominant already compute into `hypothesis.json` debug keys without driving `direction`. Adding rules there lets us measure the *dominant* decision (drop the right SMTs, keep the right ones) on labeled cases with **zero** P&L risk. Shadow cannot give the P&L delta (that needs Phase 3 wiring) — it gives relevance *correctness*.
- **Producer insight that justifies dropping invalidated dominants**: invalidated signals fulfill 46% within 180m vs 95% for kept (base rate 69%) — invalidation is an informative filter, so dropping invalidated dominants is well-founded.
- **Geometry is settled**: reclaim-vs-point producer geometry was a wash (51% vs 52%); do not revisit it. The lever is here, in consumer relevance.
- **June-3 level is dynamic**: the swept-and-reclaimed level was `prev1_week_high` (price 30807→30496→30738), NOT `prev_day_low` (30317, never touched). The leg rule must pick the level from the data.
- **Authority context**: `smt_authority` ranks week>day>fill>session, so a stale week-tier bearish stays dominant — exactly what these rules drop/supersede.
- **Order matters in the shadow block**: invalidate-drop → Rule A (dedup/supersede) → Rule B (gated) → leg-suppress → dominant. Earlier stages shrink the set the later stages rank.
