# Feature: SMT Adverse-Run Invalidation + Relevance-Filter Supersession

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations
- **NEVER use `git stash`** — this repo shares a stash store with the concurrent `live` and `entry-stuff` worktrees.
- **DO NOT touch `../entry-stuff/`** — Part B (consumer) is SPEC-ONLY in this plan. It is documented for the user to fold into the entry-stuff Phase-3 work in that worktree. The executor implements **Part A (smt-stuff producer) only.**

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

SMTs are reversal signals anchored at `fire_mnq_close`. Today a fired SMT has exactly two terminal states tracked in `detect_state`: **fired** and **fulfilled** (MNQ close follows through `FULFILL_PTS[tier]` in the SMT's favorable direction). There is no symmetric "the thesis is dead because price ran the *wrong* way" state. The motivating case: on 2026-06-03 a `prev1_week_high` SMT fires **bearish** at 09:49:25 / 09:50:00 right as a strong uptrend begins; price runs *up* through the week high, but nothing flags the bearish thesis as dead, so a tier-ranked (week=4) stale bearish record can stay `dominant` in the downstream relevance filter and confuse consumers.

This feature adds the missing terminal state in two layers:

- **Part A — Producer (smt-stuff, EXECUTABLE):** Add **adverse-run invalidation** — the exact mirror of fulfillment. When a fired, not-yet-fulfilled SMT's MNQ close runs *against* its direction past `fire_mnq_close` by a tier-scaled `INVALIDATE_PTS`, set `st["invalidated"]=True` and append a structured event to a new `smt_invalidations` debug trail (NOT plotted). The trail lets an agent verify, after a regression run, exactly which SMTs were invalidated, when, and why.
- **Part B — Consumer (entry-stuff, SPEC-ONLY):** The relevance filter (`hypothesis.py` / `smt_detect.py` Contract C / `session_pipeline.py` shadow block) is updated to (1) drop `invalidated` records, (2) apply **Rule A — same-level latest-take-out-wins** (a newer opposite-direction SMT on the *same* level supersedes the older), (3) apply **Rule B — recency-trend cross-tier suppression** (a fresher opposite-direction SMT suppresses an older contradicting one even across tiers; gated + measured), and (4) emit `superseded_*` / `suppressed_*` events into the same trail.

## User Story

As a **strategy researcher iterating on SMT V2**,
I want **a fired SMT to gain a terminal "invalidated" state when price runs through it the wrong way, recorded in an agent-readable trail, plus consumer rules that drop/supersede stale contradicting SMTs**,
So that **a stale higher-tier counter-trend SMT (e.g. the 09:49 prev1_week_high bearish) no longer stays `dominant`, and I can measure — post-run, without plots — whether invalidation/supersession fires on the right signals.**

## Problem Statement

1. A fired SMT has no "ran the wrong way → dead" terminal state — only fulfilled. Counter-trend higher-tier SMTs linger.
2. There is no structured, agent-consumable record of invalidation decisions, so the effect of any such mechanism cannot be measured after a regression run.
3. The relevance filter ranks by tier (week > day > fill > session), so a stale week-tier bearish out-ranks a fresh day-tier bullish and becomes `dominant`. There is no rule that lets a newer, contradicting signal supersede an older one.

## Solution Statement

Producer: add `INVALIDATE_PTS_{MNQ,MES}` tier tables + an `_invalidate_pts()` helper; in `_detect_level_smts` block (a), compute invalidation as the mirror of fulfillment; store `st["invalidated"]` (+ `invalidated_time`, `invalidated_mnq_close`) and append an event to `state["__invalidations__"]` (a reserved key, no `|`, skipped by the post-pass exactly like `__prevref_<type>`). `session_pipeline._run_smt_v2_detection` mirrors the list to `smt_invalidations.json` under `paths.state_dir()` (debug-only; never plotted). **Invalidation adds a NEW flag and a NEW reserved key only — it does NOT alter `records`, fire, fulfill, or re-arm — so the 1s 2026-06-03 trade invariant (21 / $534.50) is preserved by construction** and verified by regression.

Consumer (spec-only): extend Contract C to `smt_status(...) → unfulfilled|fulfilled|invalidated|gone`; drop `invalidated` in invalidate-before-ingest and in `ingest_smts` step (1); add Rule A (same-`ref_name`, opposite-direction, newer-wins) and Rule B (gated cross-tier recency suppression behind `RULE_B_ENABLED`); emit consumer events into the shared trail.

## Feature Metadata

**Feature Type**: Enhancement (new terminal state + observability) + Spec (consumer rules)
**Complexity**: Medium
**Primary Systems Affected**: `smt-stuff/smt_detect.py` (producer), `smt-stuff/session_pipeline.py` (trail wiring); SPEC-ONLY: `entry-stuff/hypothesis.py`, `entry-stuff/smt_detect.py`, `entry-stuff/session_pipeline.py`
**Dependencies**: None new (pandas already present)
**Breaking Changes**: No. New `detect_state` keys are additive; nothing in the strategy/trade path reads them.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING (smt-stuff)

- `smt_detect.py` (lines 32–36, 86–88) — Why: `FULFILL_PTS_MNQ/MES` tier tables + `_fulfill_pts(tier, inst)`; mirror these for `INVALIDATE_PTS` / `_invalidate_pts`.
- `smt_detect.py` (lines 293–323) — Why: init `st` dict + block (a) fulfillment. Add `"invalidated": False` to the init dict; add the invalidation branch right after the fulfillment branch (mutually exclusive — fulfillment checked first).
- `smt_detect.py` (lines 325–333) — Why: dynamic re-arm block. When a dynamic level re-arms, also reset `st["invalidated"]=False`.
- `smt_detect.py` (lines 335–360) — Why: fire block. Add `st["invalidated"]=False` and `st["fire_time"]=iso` at fire (fire_time is needed in the trail event; currently only `fire_price`/`fire_leader`/`fire_level_price`/`fire_mnq_close` are stored).
- `smt_detect.py` (lines 365–388) — Why: post-pass over `state.items()` + `state[_prevref_key]=mnq_close`. Confirms the reserved-key pattern: the guard `if "|" not in skey or st.get("armed")` short-circuits on `"|" not in skey` BEFORE calling `.get`, so `__invalidations__` (a list) and `__prevref_<type>` (a float) are safely skipped. The new `__invalidations__` key MUST also contain no `|`.
- `session_pipeline.py` (lines 533–551) — Why: `levels.json` snapshot write pattern (`paths.state_dir()`, `write_text(json.dumps(...))`). Mirror this exact pattern for `smt_invalidations.json`. NOTE: `levels.json` is written once at daily-compute time — the invalidation trail is per-bar, so it is written in `_run_smt_v2_detection`, NOT here.
- `session_pipeline.py` (lines 1678–1757) — Why: `_run_smt_v2_detection` — where `detect_regular_smts` / `detect_fill_smts` / `detect_hidden_smts` run, `records` is built, and `save_smts({"detect_state":..., "watch":...})` persists. Add the `smt_invalidations.json` mirror write here, reading `self._detect_state.get("__invalidations__", [])`.
- `tests/test_smt_detect.py` — Why: existing direction/fulfillment unit tests (Phase 3 added `test_fixed_high_bullish_when_swept_from_above`, etc.). Mirror their fixture/helper style for invalidation tests.
- `tests/test_session_pipeline.py` — Why: `_smt_v2_pipeline` setup + `_freeze_liquidities` helper; mirror for the trail-wiring test.

### Relevant Codebase Files — READ FOR CONTEXT (entry-stuff, SPEC-ONLY — do NOT edit)

- `entry-stuff/hypothesis.py` (lines 244–437) — `_tier_rank`, `to_record`, `smt_authority`, `dominant`, `ingest_smts`, `RELEVANCE_X_PTS=25.0`. Rules A/B attach here.
- `entry-stuff/smt_detect.py` (lines 530–579) — Contract C: `_record_key`, `fulfillment_status` → extend to `smt_status`.
- `entry-stuff/session_pipeline.py` (lines 1698–1736) — shadow block: invalidate-before-ingest → ingest → dominant, stored under `smt_active_set`/`smt_dominant` debug keys.

### New Files to Create

- None required for Part A (changes are in-place). `smt_invalidations.json` is a runtime artifact, not a source file.
- `tests/test_smt_invalidation.py` (smt-stuff) — dedicated unit tests for adverse-run invalidation + trail accumulation (or extend `tests/test_smt_detect.py` — see Task 1.2).

### Patterns to Follow

**Naming Conventions**: tier tables `INVALIDATE_PTS_MNQ = {"week":..,"day":..,"session":..}` mirroring `FULFILL_PTS_MNQ`; helper `_invalidate_pts(tier, inst)` mirroring `_fulfill_pts`. Reserved state key `__invalidations__` mirroring `_prevref_key = "__prevref_" + rec_type`.
**Error Handling**: pure detector never raises; the `smt_invalidations.json` write in `session_pipeline` follows the existing best-effort I/O style (the surrounding `_run_smt_v2_detection` already does plain writes; do not add new broad excepts unless the file pattern around it does).
**Logging Pattern**: Production code is silent (global CLAUDE.md). The trail is **structured data** in `detect_state["__invalidations__"]` + a JSON artifact — NOT print/stdout. No plot marks (explicit user constraint: "not in the plots, not yet").

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌───────────────────────────────────────────────────────────────┐
│ WAVE 1: Producer core + tests (Parallel)                      │
├───────────────────────────────────────────────────────────────┤
│ Task 1.1: Invalidation logic in        │ Task 1.2: Unit tests  │
│           _detect_level_smts            │  (against the         │
│           (smt_detect.py)               │   interface contract) │
│ Agent: detection-engineer               │ Agent: test-engineer  │
└───────────────────────────────────────────────────────────────┘
                              ↓
┌───────────────────────────────────────────────────────────────┐
│ WAVE 2: Trail wiring + pipeline test (After Wave 1)           │
├───────────────────────────────────────────────────────────────┤
│ Task 2.1: smt_invalidations.json mirror │ Task 2.2: pipeline    │
│           in _run_smt_v2_detection      │   trail test          │
│ Agent: pipeline-engineer (2.1)          │ Agent: test-engineer  │
└───────────────────────────────────────────────────────────────┘
                              ↓
┌───────────────────────────────────────────────────────────────┐
│ WAVE 3: Regression validation + trail analysis (Sequential)   │
├───────────────────────────────────────────────────────────────┤
│ Task 3.1: 1s 2026-06-03 regression (trades 21/$534.50) +      │
│           confirm 09:49 prev1_week_high bearish in the trail   │
│ Agent: integration-specialist                                  │
└───────────────────────────────────────────────────────────────┘

(Part B — entry-stuff consumer — is SPEC ONLY; not a wave. See "PART B SPEC".)
```

### Parallelization Summary

**Wave 1 — Parallel**: Task 1.1 (impl) + Task 1.2 (tests) against a fixed interface contract.
**Wave 2 — Parallel after Wave 1**: Task 2.1 (trail mirror) + Task 2.2 (pipeline test).
**Wave 3 — Sequential**: Task 3.1 (regression + trail analysis) — needs Waves 1–2.

3 of 5 tasks (1.2, 2.2, and the spec authoring) are parallelizable with their implementation peers → ≥40% parallel.

### Interface Contracts

**Contract INV-1 (Task 1.1 provides → Tasks 1.2, 2.1, 2.2, Part B consume):**
- `st["invalidated"]: bool` added to every level-SMT state dict (init `False`).
- Invalidation condition, anchored at `fc = st["fire_mnq_close"]`, tier from `_level_class(name)[1]`, `inv = _invalidate_pts(tier, "mnq")`:
  - `direction == "short"` → invalidated when `mnq_close >= fc + inv`
  - `direction == "long"`  → invalidated when `mnq_close <= fc - inv`
  - Only when `fired and not fulfilled and not invalidated`; **fulfillment is evaluated first in the same bar** — if it fulfilled this bar it cannot also invalidate.
- On the transition to invalidated: set `st["invalidated"]=True`, `st["invalidated_time"]=iso`, `st["invalidated_mnq_close"]=mnq_close`, and append ONE event dict to `state["__invalidations__"]` (created as `[]` on first use):
  ```
  {"time": iso, "key": skey, "ref_name": name, "tier": tier, "kind": kind_cls,
   "direction": direction, "type": rec_type, "fire_time": st.get("fire_time"),
   "fire_mnq_close": fc, "trigger_mnq_close": mnq_close, "threshold_pts": inv,
   "reason": "adverse_run"}
  ```
- `st["fire_time"]=iso` set at fire; `st["invalidated"]` reset to `False` at fire and at every dynamic re-arm (block (b) and the post-pass).
- `INVALIDATE_PTS_MNQ = {"week":40.0,"day":20.0,"session":10.0}` and `INVALIDATE_PTS_MES = {"week":6.0,"day":3.0,"session":1.5}` (default: half of `FULFILL_PTS` — abandon a wrong reversal faster than confirming a right one; tunable).

**Contract INV-2 (Task 2.1 provides → Task 2.2, Part B consume):**
- After SMT detection each bar, `_run_smt_v2_detection` overwrites `paths.state_dir() / "smt_invalidations.json"` with `json.dumps(self._detect_state.get("__invalidations__", []), indent=2)` whenever the list is non-empty. Debug-only artifact; never read by the strategy/trade path.

**Mock for parallel work**: Task 1.2 (tests) and Task 2.2 build fixtures directly against Contract INV-1/INV-2 schemas above, so they do not need the implementation finished to be written.

### Synchronization Checkpoints

**After Wave 1**: `uv run python -m pytest tests/test_smt_detect.py tests/test_smt_invalidation.py -q`
**After Wave 2**: `uv run python -m pytest tests/test_session_pipeline.py -q`
**After Wave 3**: `uv run python regression.py --mode 1s --dates 2026-06-03` → `trades=PASS n_trades=21 pnl=534.50`

---

## IMPLEMENTATION PLAN

### Phase 1: Producer invalidation (core)

No external services. Implement the mirror of fulfillment in the pure detector, keeping `records` and all fire/fulfill/re-arm behavior byte-for-byte unchanged.

### Phase 2: Trail wiring (observability)

Mirror `detect_state["__invalidations__"]` to a debug JSON artifact in the run/session state dir.

### Phase 3: Validation

Run the 1s 2026-06-03 regression; assert the trade invariant; read the trail to confirm the motivating 09:49 case is captured.

---

## STEP-BY-STEP TASKS

**Task keywords**: CREATE · UPDATE · ADD · REMOVE · REFACTOR · MIRROR

---

### WAVE 1: Producer core + tests

#### Task 1.1: ADD adverse-run invalidation to `smt_detect.py::_detect_level_smts`

- **WAVE**: 1
- **AGENT_ROLE**: detection-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: 1.2 (integration), 2.1, 3.1
- **PROVIDES**: Contract INV-1
- **IMPLEMENT**:
  1. Near `FULFILL_PTS_*` (lines 35–36) ADD `INVALIDATE_PTS_MNQ`/`INVALIDATE_PTS_MES` (values per Contract INV-1).
  2. Near `_fulfill_pts` (line 86) ADD `_invalidate_pts(tier, inst)` mirroring it.
  3. In the `st` init dict (lines 295–304) ADD `"invalidated": False`.
  4. In block (a) (lines 314–323): after the existing fulfillment branch, ADD the invalidation branch guarded by `not st.get("fulfilled")` (so a bar that fulfills cannot also invalidate). On transition, set the three `st` fields and append the event dict to `state.setdefault("__invalidations__", [])`. `tier`/`kind_cls` are already computed locally in this function (per the Phase-3 direction logic); reuse them — do NOT recompute divergently.
  5. In the fire block (lines 354–360) ADD `st["invalidated"]=False` and `st["fire_time"]=iso`.
  6. In dynamic re-arm block (b) (lines 331–333) ADD `st["invalidated"]=False`; in the post-pass re-arm (lines 383–385) ADD `st["invalidated"]=False`.
  7. CONFIRM the post-pass guard (line 374) still skips `__invalidations__` (no `|` → short-circuit before `.get`). Do not change the guard.
- **PATTERN**: `smt_detect.py:307-323` (fulfillment mirror); `smt_detect.py:388` (reserved-key persistence pattern).
- **CONSTRAINT**: Do NOT touch `records.append(...)`, the fire `cond` edge, fulfillment math, or dynamic re-arm *triggers*. Invalidation is informational only this iteration (NOT a re-arm trigger) — note it as a future option in NOTES.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_detect.py -q` (existing 42 detection tests still pass — proves no behavioral drift).

#### Task 1.2: CREATE `tests/test_smt_invalidation.py` (unit tests)

- **WAVE**: 1
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [] (write against Contract INV-1; integrate after 1.1)
- **BLOCKS**: 1.x checkpoint
- **PROVIDES**: Unit coverage for every invalidation branch + trail event schema.
- **IMPLEMENT** — name these cases explicitly:
  1. `test_short_invalidated_when_close_runs_up_past_threshold` — fixed bearish SMT fires; feed MNQ closes rising to `fc + INVALIDATE_PTS["week"]` → `st["invalidated"] is True`; exactly one `__invalidations__` event with `reason=="adverse_run"`, correct `key/direction/fire_mnq_close/trigger_mnq_close/threshold_pts`.
  2. `test_long_invalidated_when_close_runs_down_past_threshold` — mirror for a bullish SMT.
  3. `test_not_invalidated_just_below_threshold` — close at `fc + inv - epsilon` → not invalidated, empty trail.
  4. `test_fulfillment_takes_precedence_same_bar` — a bar that satisfies BOTH fulfill and (numerically) invalidate marks `fulfilled`, NOT `invalidated`, no trail event.
  5. `test_invalidation_event_emitted_once` — once invalidated, further adverse bars do NOT append duplicate events (guard `not st.get("invalidated")`).
  6. `test_dynamic_rearm_resets_invalidated` — a dynamic level invalidated, then re-armed (fulfilled or opposite SMT), has `invalidated` reset to `False`.
  7. `test_fixed_level_invalidation_does_not_change_records` — assert the returned `records` list is identical with/without the adverse run (invalidation never adds/removes a record).
  8. `test_reserved_key_skipped_by_postpass` — after a batch with both an invalidation and a re-arm, `__invalidations__` is untouched by the post-pass and contains no `|`.
- **PATTERN**: mirror fixtures/helpers in `tests/test_smt_detect.py` (Phase-3 direction tests).
- **VALIDATE**: `uv run python -m pytest tests/test_smt_invalidation.py -q`

**Wave 1 Checkpoint**: `uv run python -m pytest tests/test_smt_detect.py tests/test_smt_invalidation.py -q`

---

### WAVE 2: Trail wiring + pipeline test

#### Task 2.1: ADD `smt_invalidations.json` mirror in `session_pipeline.py::_run_smt_v2_detection`

- **WAVE**: 2
- **AGENT_ROLE**: pipeline-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: 2.2, 3.1
- **PROVIDES**: Contract INV-2
- **USES_FROM_WAVE_1**: Task 1.1 provides `detect_state["__invalidations__"]`.
- **IMPLEMENT**: After `save_smts({...})` (line 1752) and before `return sd_events`, if `self._detect_state.get("__invalidations__")` is non-empty, write `paths.state_dir() / "smt_invalidations.json"` with `json.dumps(self._detect_state["__invalidations__"], indent=2)` (mirror the `levels.json` write at 544–551: `import json`, `write_text(..., encoding="utf-8")`). Overwrite each call (full snapshot — the list only grows within a run). Do NOT add it to `sd_events`, the golden-events stream, or `levels.json` (no plot path).
- **PATTERN**: `session_pipeline.py:537,544-551`.
- **VALIDATE**: `uv run python -m pytest tests/test_session_pipeline.py -q`

#### Task 2.2: ADD pipeline trail test to `tests/test_session_pipeline.py`

- **WAVE**: 2
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [2.1] (fixture can be authored against Contract INV-2 earlier)
- **PROVIDES**: Integration coverage that the trail reaches disk and is plot-free.
- **IMPLEMENT** — name these cases:
  1. `test_smt_invalidations_written_to_state_dir` — drive `_smt_v2_pipeline` with a fixed bearish SMT then adverse-up bars; assert `smt_invalidations.json` exists in `paths.state_dir()` and parses to a list with one `reason=="adverse_run"` event.
  2. `test_invalidation_trail_not_in_sd_events` — assert no `smt-div`/event emitted by the same bars carries an invalidation record (the trail is debug-only).
  3. `test_no_trail_file_when_no_invalidations` — a clean run writes no events (file absent or empty list — match whichever the impl chooses; assert the chosen contract).
- **PATTERN**: `_smt_v2_pipeline` + `_freeze_liquidities` in `tests/test_session_pipeline.py` (note: `_freeze_liquidities` already clears `liquidities_universe*`).
- **VALIDATE**: `uv run python -m pytest tests/test_session_pipeline.py -q`

**Wave 2 Checkpoint**: `uv run python -m pytest tests/test_session_pipeline.py tests/test_smt_invalidation.py -q`

---

### WAVE 3: Regression validation + trail analysis (Sequential)

#### Task 3.1: VALIDATE 1s 2026-06-03 + confirm motivating case in trail

- **WAVE**: 3
- **AGENT_ROLE**: integration-specialist
- **DEPENDS_ON**: [1.1, 2.1]
- **PROVIDES**: Proof the trade invariant holds and the 09:49 case is captured.
- **IMPLEMENT**:
  1. Run `uv run python regression.py --mode 1s --dates 2026-06-03` (set `ACT_NO_BROWSER=1`; use the **PowerShell tool** — `$env:ACT_NO_BROWSER=1` — not the Bash tool).
  2. ASSERT output `trades=PASS n_trades=21 pnl=534.50`. `events=FAIL` is EXPECTED (golden-events mismatch from signal changes) and is NOT a failure here.
  3. Locate the run's `smt_invalidations.json` under the per-run state dir; confirm it contains the `prev1_week_high` bearish entries near 09:49:25 / 09:50:00 with `reason=="adverse_run"`, and record their `invalidated_time` (when price ran up past `fc + 40`).
  4. Sanity-scan the trail for obvious false positives (e.g. a known-good SMT invalidated immediately) and report counts by tier.
- **VALIDATE**: regression trade line + a readable summary of the trail (count, the two prev1_week_high events).
- **IF_FAILS**: If `n_trades != 21` or `pnl != 534.50`, the invalidation change leaked into `records`/fire/fulfill/re-arm — diff against the constraint in Task 1.1 and remove the leak before re-running.

**Final Checkpoint**: `uv run python regression.py --mode 1s --dates 2026-06-03` → `trades=PASS n_trades=21 pnl=534.50`, trail confirms the 09:49 case.

---

## PART B SPEC (entry-stuff consumer) — DO NOT EXECUTE

Documented for the user to fold into the entry-stuff Phase-3 relevance-filter work. The executor of THIS plan does not edit `../entry-stuff/`. Listed here so the producer interface (Contracts INV-1/INV-2) is designed to serve it.

**Dependency note:** Part B requires the producer's `st["invalidated"]` flag to be present in the `detect_state` produced *by the entry-stuff detector*. entry-stuff has its OWN `smt_detect.py`. So before Part B can consume invalidation, either (i) Part A merges to `master` and entry-stuff rebases, or (ii) the Part-A producer change is ported into `entry-stuff/smt_detect.py`. Recommended: (i) rebase-after-merge to keep one source of truth.

**B-1 — Extend Contract C (`entry-stuff/smt_detect.py:553`)**: add `smt_status(keys, detect_state) → "unfulfilled"|"fulfilled"|"invalidated"|"gone"` — check `st.get("invalidated") is True` BEFORE the unfulfilled fallback; keep `fulfillment_status` as a thin wrapper (or migrate callers).

**B-2 — `to_record` (`hypothesis.py:249`)**: carry `invalidated` from the emission onto the record (default `False`), alongside the existing `fulfilled`.

**B-3 — Drop invalidated (`hypothesis.py::ingest_smts` step (1), line 404)**: also `continue` when `rec.get("invalidated") is True`. In the shadow block (`entry-stuff/session_pipeline.py:1716-1723`) widen the invalidate-before-ingest filter to drop records whose `smt_status` is `fulfilled`/`gone`/**`invalidated`**.

**B-4 — Rule A (same-level latest-take-out-wins)** in `ingest_smts`, after the existing key-dedup: group the resulting active set by `ref_name`; if a single `ref_name` has records of BOTH directions, keep only the most-recent (by `time`) and drop the older opposite-direction one. Emit a `superseded_same_level` event (schema below) naming the dropped key and the winning key. This is the safe, principled core (a level flipping direction = its prior take-out was reclaimed). Ship enabled.

**B-5 — Rule B (recency-trend cross-tier suppression; GATED + MEASURED)** behind `RULE_B_ENABLED` (default off until measured). Before `dominant(...)`: a record `O` is suppressed if there exists a newer record `N` with opposite direction such that ALL gates hold: (a) `N.time > O.time` by ≥ `RULE_B_MIN_AGE_MIN` minutes; (b) price has moved against `O` by ≥ `RULE_B_ADVERSE_PTS` (use the current MNQ close already available in the shadow block); (c) `_tier_rank(N.tier) >= _tier_rank(O.tier) - RULE_B_TIER_SLACK`. This deliberately lets a fresher, possibly-lower-tier signal override the tier ranking — so it is gated and emitted to the trail for measurement, NOT silent. Emit `suppressed_by_trend` events. **Note for the user:** a future "day general-trend hypothesis" layer (WIP in another worktree) should subsume Rule B — once that exists, prefer trend-context suppression over this heuristic.

**B-6 — Consumer trail**: accumulate `superseded_same_level` / `suppressed_by_trend` events (schema: `{time, dropped_key, winner_key, ref_name, dropped_direction, reason, rule}`) into `hyp["smt_invalidations"]` (or a sibling `smt_suppressions` debug key) saved next to `smt_active_set`/`smt_dominant`, so the SAME post-run analysis reads producer + consumer decisions together.

**B-7 — Tests (entry-stuff `tests/test_smt_relevance.py`)**: `test_status_invalidated`; `test_ingest_drops_invalidated`; `test_rule_a_same_level_newer_opposite_wins`; `test_rule_a_no_effect_same_direction`; `test_rule_b_suppresses_older_when_gates_met`; `test_rule_b_noop_when_flag_off`; `test_rule_b_respects_min_age_and_adverse_gates`; `test_consumer_trail_records_supersession`.

---

## REFERENCE IMPLEMENTATION SKETCH (Task 1.1 / 2.1)

Illustrative — match surrounding style; the executor adapts to the exact local variables present in `_detect_level_smts` (`name`, `direction`, `rec_type`, `iso`, `mnq_close`, `tier`, `kind_cls`, `skey`).

**Constants (near lines 35–36):**
```python
# Adverse-run invalidation — mirror of FULFILL_PTS. A fired, not-yet-fulfilled SMT is
# "invalidated" when MNQ close runs AGAINST its direction past the fire close by this much.
# Default = half of FULFILL_PTS: abandon a wrong reversal faster than confirming a right one.
INVALIDATE_PTS_MNQ = {"week": 40.0, "day": 20.0, "session": 10.0}
INVALIDATE_PTS_MES = {"week": 6.0, "day": 3.0, "session": 1.5}
```

**Helper (near line 86):**
```python
def _invalidate_pts(tier: str, inst: str) -> float:
    table = INVALIDATE_PTS_MES if inst == "mes" else INVALIDATE_PTS_MNQ
    return table.get(tier, table["session"])
```

**Block (a) — append AFTER the existing fulfillment branch (lines 314–323):**
```python
        if st.get("fired") and not st.get("fulfilled") and not st.get("invalidated"):
            fc = st.get("fire_mnq_close")
            if fc is not None:
                # (existing fulfillment branch sets st["fulfilled"] here) ...
                if not st.get("fulfilled"):
                    inv = _invalidate_pts(tier, "mnq")
                    adverse = (
                        (direction == "short" and mnq_close >= float(fc) + inv)
                        or (direction == "long" and mnq_close <= float(fc) - inv)
                    )
                    if adverse:
                        st["invalidated"] = True
                        st["invalidated_time"] = iso
                        st["invalidated_mnq_close"] = mnq_close
                        state.setdefault("__invalidations__", []).append({
                            "time": iso, "key": skey, "ref_name": name, "tier": tier,
                            "kind": kind_cls, "direction": direction, "type": rec_type,
                            "fire_time": st.get("fire_time"), "fire_mnq_close": float(fc),
                            "trigger_mnq_close": mnq_close, "threshold_pts": inv,
                            "reason": "adverse_run",
                        })
```

**Trail mirror — `_run_smt_v2_detection`, after `save_smts(...)` (Task 2.1):**
```python
        _inv = self._detect_state.get("__invalidations__")
        if _inv:
            import json as _json
            (paths.state_dir() / "smt_invalidations.json").write_text(
                _json.dumps(_inv, indent=2), encoding="utf-8")
```

---

## TESTING STRATEGY

**⚠️ ALL tests that can be automated MUST be automated.** All tests here are pure-Python `pytest` — fully automatable.

| What you're testing | Tool |
|---|---|
| Producer invalidation branches, trail schema | `pytest` (`tests/test_smt_invalidation.py`) |
| Trail reaches disk, plot-free | `pytest` (`tests/test_session_pipeline.py`) |
| Trade invariant + motivating case | `regression.py` (1s 2026-06-03) |
| Consumer Rules A/B, status, trail (SPEC) | `pytest` (`entry-stuff/tests/test_smt_relevance.py`) — authored in entry-stuff, NOT here |

### Unit Tests
**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_invalidation.py` (+ existing `tests/test_smt_detect.py` stay green) | **Run**: `uv run python -m pytest tests/test_smt_invalidation.py tests/test_smt_detect.py -q`

### Integration Tests
**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_session_pipeline.py` | **Run**: `uv run python -m pytest tests/test_session_pipeline.py -q`

### End-to-End Tests
**Status**: ✅ Automated (regression replay) | **Tool**: `regression.py` | **Run**: `uv run python regression.py --mode 1s --dates 2026-06-03` (PowerShell tool; `$env:ACT_NO_BROWSER=1`). Assert `trades=PASS n_trades=21 pnl=534.50`; inspect `smt_invalidations.json`.

### Edge Cases
- **Threshold boundary**: close exactly at `fc ± inv` vs. just inside → ✅ `tests/test_smt_invalidation.py::test_not_invalidated_just_below_threshold`
- **Same-bar fulfill+invalidate**: fulfillment wins → ✅ `test_fulfillment_takes_precedence_same_bar`
- **Idempotent event**: no duplicate trail rows → ✅ `test_invalidation_event_emitted_once`
- **Dynamic re-arm reset**: ✅ `test_dynamic_rearm_resets_invalidated`
- **Reserved-key safety**: post-pass skips `__invalidations__` → ✅ `test_reserved_key_skipped_by_postpass`
- **No-records invariant**: `records` unchanged by invalidation → ✅ `test_fixed_level_invalidation_does_not_change_records` + the 1s regression trade line

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend unit (pytest, smt-stuff) | 8 | |
| ✅ Integration (pytest, smt-stuff) | 3 | |
| ✅ E2E (regression invariant) | 1 | |
| ✅ Consumer (pytest, entry-stuff — SPEC, authored there) | 8 | |
| ⚠️ Manual | 0 | |
| **Total (executable here: 12)** | 20 | 100% |

**Goal**: 100% path coverage for Part A. No manual tests. Consumer tests (8) are part of the entry-stuff spec, not executed by this plan.

---

## VALIDATION COMMANDS

### Side-effecting test policy (full-suite runs)

- **Run side-effecting tests during validation?** ☑ No (default)
- **Deselect command (default-skip):** `uv run python -m pytest tests/ -q --ignore=tests/test_ib_realtime.py --ignore=tests/test_ib_integration.py --ignore=tests/test_orchestrator_main.py --ignore=tests/test_orchestrator_integration.py --ignore=tests/test_orchestrator_process.py --ignore=tests/test_orchestrator_kill_scope.py --ignore=tests/smoke_pmt_connection.py`
  - Rationale: `test_ib_*` open live IB connections (may hang / disrupt the live worktree); the `test_orchestrator_*` suite starts/scans/kills OS processes machine-wide and could kill a *live* orchestrator in another worktree. A live orchestrator IS typically running on this machine — keep these deselected.
- **If Yes — exact paths/markers + safe command:** N/A (not opting in; a live orchestrator/IB feed is assumed running).

### Level 1: Syntax & Style
```
uv run python -c "import smt_detect, session_pipeline"
```

### Level 2: Unit Tests
```
uv run python -m pytest tests/test_smt_invalidation.py tests/test_smt_detect.py -q
```

### Level 3: Integration Tests
```
uv run python -m pytest tests/test_session_pipeline.py tests/test_smt_daily.py -q
```

### Level 4: E2E / Regression invariant
```
# PowerShell tool: $env:ACT_NO_BROWSER=1; then:
uv run python regression.py --mode 1s --dates 2026-06-03
# REQUIRE: trades=PASS n_trades=21 pnl=534.50   (events=FAIL is expected)
# Then read smt_invalidations.json in the run's state dir; confirm the 09:49 prev1_week_high bearish events.
```

### Baseline (run BEFORE implementation — record pass/fail/skip)
```
uv run python -m pytest tests/ -q --ignore=tests/test_ib_realtime.py --ignore=tests/test_ib_integration.py --ignore=tests/test_orchestrator_main.py --ignore=tests/test_orchestrator_integration.py --ignore=tests/test_orchestrator_process.py --ignore=tests/test_orchestrator_kill_scope.py --ignore=tests/smoke_pmt_connection.py
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `INVALIDATE_PTS_MNQ/MES` tables + `_invalidate_pts(tier, inst)` exist, mirroring `FULFILL_PTS`/`_fulfill_pts` (defaults = half of FULFILL).
- [ ] `_detect_level_smts` sets `st["invalidated"]=True` when `fired and not fulfilled and not invalidated` and MNQ close runs adverse past `fire_mnq_close` by `_invalidate_pts(tier,"mnq")` — short: `close >= fc+inv`; long: `close <= fc-inv`.
- [ ] Fulfillment is evaluated first in the same bar; a bar that fulfills can never also invalidate.
- [ ] Each invalidation transition appends exactly one `reason=="adverse_run"` event (full schema: time, key, ref_name, tier, kind, direction, type, fire_time, fire_mnq_close, trigger_mnq_close, threshold_pts) to `detect_state["__invalidations__"]`; no duplicate on later adverse bars.
- [ ] `st["invalidated"]` resets to `False` at fire and at every dynamic re-arm (block b + post-pass); `st["fire_time"]=iso` recorded at fire.

### Error Handling / Edge Cases
- [ ] `__invalidations__` (and `__prevref_*`) are safely skipped by the post-pass — no `|` in the key, guard short-circuits before `.get` on the list (confirmed by test).
- [ ] Close exactly at / just inside `fc ± inv` does not invalidate (boundary).

### Integration / E2E
- [ ] `_run_smt_v2_detection` writes `smt_invalidations.json` to `paths.state_dir()` when events exist; the trail is NOT in `sd_events`, golden events, or any plot path.
- [ ] `smt_invalidations.json` from the 2026-06-03 run contains the `prev1_week_high` bearish 09:49:25 / 09:50:00 events with their `invalidated_time`.

### Non-Functional (Observability + Invariant)
- [ ] Trail is structured data only — no print/stdout, no plot marks.
- [ ] `records` and all fire/fulfill/re-arm behavior unchanged: `tests/test_smt_detect.py` stays green AND 1s 2026-06-03 = `21 / $534.50`.

### Validation
- [ ] 12 executable tests pass — verified by: `uv run python -m pytest tests/test_smt_invalidation.py tests/test_smt_detect.py tests/test_session_pipeline.py -q`
- [ ] Trade invariant — verified by: `uv run python regression.py --mode 1s --dates 2026-06-03` → `trades=PASS n_trades=21 pnl=534.50`
- [ ] No NEW failures vs. recorded baseline (side-effecting tests deselected per policy).
- [ ] Changes left UNSTAGED — not committed.

### Out of Scope
- Editing `../entry-stuff/` (Part B is spec-only).
- Plotting invalidation marks (explicit user constraint — not yet).
- Making invalidation a dynamic re-arm trigger (future option; would change trades).
- Implementing the future "day general-trend hypothesis" layer that should eventually subsume Rule B.

---

## COMPLETION CHECKLIST

- [ ] Baseline test run recorded before implementation
- [ ] All Wave 1–3 tasks completed in order
- [ ] Each task validation passed
- [ ] Validation Levels 1–4 executed
- [ ] All 12 executable automated tests created and passing
- [ ] 1s 2026-06-03 regression = 21 / $534.50; trail confirms 09:49 case
- [ ] No new linting/import errors
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ entry-stuff worktree NOT touched**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## COVERAGE REVIEW PASS

**New code paths (Part A) → covering test:**
| Path | Branch | Covering test | Status |
|---|---|---|---|
| `_invalidate_pts` | tier lookup + session fallback | exercised by all invalidation tests | ✅ |
| block (a) | short adverse → invalidated | `test_short_invalidated_when_close_runs_up_past_threshold` | ✅ |
| block (a) | long adverse → invalidated | `test_long_invalidated_when_close_runs_down_past_threshold` | ✅ |
| block (a) | below threshold → no-op | `test_not_invalidated_just_below_threshold` | ✅ |
| block (a) | fulfill precedence same bar | `test_fulfillment_takes_precedence_same_bar` | ✅ |
| block (a) | idempotent event (no dup) | `test_invalidation_event_emitted_once` | ✅ |
| trail append | event schema/fields | asserted in cases 1–2 | ✅ |
| fire block | `invalidated=False` + `fire_time` set | `test_dynamic_rearm_resets_invalidated` (pre-state) + case 1 (fire_time in event) | ✅ |
| re-arm (b) + post-pass | `invalidated` reset | `test_dynamic_rearm_resets_invalidated` | ✅ |
| post-pass guard | `__invalidations__` skipped | `test_reserved_key_skipped_by_postpass` | ✅ |
| `records` invariance | no record added/removed | `test_fixed_level_invalidation_does_not_change_records` + regression | ✅ |
| `_run_smt_v2_detection` | trail written to disk | `test_smt_invalidations_written_to_state_dir` | ✅ |
| `_run_smt_v2_detection` | trail NOT in events | `test_invalidation_trail_not_in_sd_events` | ✅ |
| `_run_smt_v2_detection` | no events → no/empty file | `test_no_trail_file_when_no_invalidations` | ✅ |

**Project-impact re-validation:**
- `tests/test_smt_detect.py` (42 detection tests) — re-run to prove no behavioral drift (records/fire/fulfill/re-arm unchanged). ✅
- `tests/test_smt_daily.py`, `tests/test_signal_smt.py`, `tests/test_smt_strategy_v2.py` — touch the detector indirectly; included in Level 2/3 to catch any unexpected interaction. ✅
- 1s 2026-06-03 regression — the authoritative no-regression gate (trades 21 / $534.50). ✅

**Gaps**: None for Part A. Part B (entry-stuff) carries its own 8 tests in its spec (B-7), authored in that worktree.

**Script deliverables check**: `regression.py` is a pre-existing runnable script, not introduced/modified here; the trail artifact is read by an agent, not a new script. No new runnable script deliverable → script-runnability criteria N/A.

---

## NOTES

- **Why invalidation is reactive, not preventive**: it can only trip after price runs `INVALIDATE_PTS` against the SMT — so between fire and threshold-cross the SMT is still live. The consumer Rules A/B (Part B) close that gap earlier using *other SMT evidence* instead of waiting for price. The two mechanisms are complementary: adverse-run handles "price ran through"; Rules A/B handle "a fresher contradicting SMT exists".
- **Why not suppress at the producer**: at fire time the divergence is a real fact and the reversal thesis (e.g. rejection at a week high) is legitimate — it simply failed this time. Suppressing valid theses at the source is wrong; a price-confirmed terminal state is the correct tool.
- **Tier-ranking is the root of the confusion**: `smt_authority` ranks week(4) > day(3); a stale week bearish out-ranks a fresh day bullish and becomes `dominant`. Invalidation + Rule A/B remove the stale record from contention.
- **`INVALIDATE_PTS` defaults are half of `FULFILL_PTS`** — abandon a wrong reversal faster than confirming a right one. Tunable once the trail shows real-world behavior.
- **Trade-invariant safety is structural**: invalidation only ADDS a `detect_state` flag + a reserved-key list and resets the flag alongside existing resets. Nothing in `_pending_watch` / the strategy / executor reads `invalidated`. The regression is the backstop check.
- **Two-worktree reality**: the producer (Part A) lands in smt-stuff; the consumer (Part B) lives in entry-stuff with its own `smt_detect.py`. Keep one source of truth by merging Part A to master and rebasing entry-stuff, rather than porting the producer twice.
