# Feature: SMT-driven hypothesis direction + event-driven reform cadence (Phase 3 of 3)

**⚠️ EXECUTION RULES — READ FIRST (verbatim):**
EXECUTION RULES: implement all changes; delete debug logs you add; leave ALL changes UNSTAGED — no git add/commit; only code changes.

> **HARD PREREQUISITE — Phases 1 & 2 MUST be merged into this tree before Phase 3 executes.**
> This plan is the **behavioral switch** that consumes the contracts produced by:
> - **Phase 1** — `.agents/plans/smt-v2-decouple-active-position.md`: freezes a live trade's
>   management direction + cautious ladder into `position["active"]` (Contract A) and **removes the
>   direction-mismatch force-close**, so a reform/flip while a position is open is safe (the trade is
>   managed off its frozen snapshot; cautious targets exit it).
> - **Phase 2** — `.agents/plans/smt-v2-relevance-filter-core.md`: provides the active-set /
>   authority / ingest pure functions in `hypothesis.py` (Contract B) and the `smt_detect`
>   fulfillment query (Contract C). `divs` in `hypothesis.json` now stores the **active SMT set**
>   (Phase 2 schema), no longer the old 15m/30m recompute.
>
> If `smt_authority`, `dominant`, `ingest_smts`, `to_record` (hypothesis.py) and `fulfillment_status`
> (smt_detect.py) are not importable, or `position["active"]` does not carry `mgmt_direction` /
> `backing_tier`, **STOP** — Phases 1/2 are not in this tree and Phase 3 cannot be executed. The
> contracts below are CONSUMED, never redefined here.

Validate documentation and codebase patterns before implementing. Match naming of existing utils,
types, and models. Import from the correct files.

---

## Feature Description

The live SMT strategy currently re-forms its directional hypothesis on a **fixed every-5m cadence**
(`session_pipeline.on_1m_bar` → `run_hypothesis` at each 5m boundary), and derives direction from a
**blended internal score** that recomputes 15m/30m SMT divergences each call
(`hypothesis._compute_divs` → `_compute_smt_score`, mixed at weight 0.35 into the structural score in
`_determine_direction`). This is slow to react (a fresh, relevant SMT can wait up to 5 minutes to
influence direction, and only as a fractional score term), and it couples the hypothesis to a
divergence recompute that Phase 2 has already replaced with a persisted, relevance-filtered active
SMT set.

This feature (Phase 3) makes the hypothesis **SMT-driven and event-driven**:

1. **Direction source** — when the active SMT set (Phase 2 `divs`) is **non-empty**, the hypothesis
   direction is the **dominant** SMT's direction (`long→up`, `short→down`). When the set is
   **empty**, fall back to the **existing structural engine** (`_determine_direction`'s PD / BOS-CHoCH
   score + global-trend fallback — **kept intact**). Only the internal SMT path is removed:
   `_compute_divs`, `_compute_smt_score`, and the `strategy_smt.detect_smt_*` internal-detection
   calls inside hypothesis.
2. **Cadence** — retire the fixed every-5m reform. Replace with (a) a **session-start initial
   formation**, (b) **event-driven reform** (a relevant SMT becoming the new dominant flips
   immediately on detection, same-or-higher tier, no confirmation wait; OR the current dominant
   becoming **fulfilled/invalidated** re-derives → flip or `none`), and (c) a **low-frequency
   safety-net formation** so we are never stuck at `direction=none` with no events.
3. **Reforms during an active position are allowed** (Phase 1 made this safe). A reform fires a
   `new-hypothesis` signal but triggers **NO entry and NO exit**. Quick-exit / opposite re-entry are
   explicitly **out of scope**.
4. **Reset detachment** — `failed_entries` and `cautious_dist_shrinks` must **no longer reset** on a
   `none→direction` reform/flip. They reset **only** on (a) session start and (b) a successful
   (non-stopped) trade closure.

## User Story

As the operator of the auto-co-trader live strategy,
I want the directional hypothesis to be driven by the dominant relevant SMT and to re-form the instant
that dominant changes, fulfills, or invalidates (instead of waiting for a 5-minute tick and blending a
recomputed divergence score),
So that direction reacts immediately to the SMT evidence Phase 2 already curates, falls back cleanly to
structure when there is no SMT, and never thrashes the failed-entry / cautious-shrink dynamic
thresholds on routine same-tier flips.

## Problem Statement

Direction is (a) **laggy** — a relevant SMT influences the hypothesis only at the next 5m boundary and
only as a 0.35-weighted score term, never decisively; and (b) **doubly-computed** — `hypothesis`
recomputes 15m/30m SMTs (`_compute_divs`) for a score even though Phase 2's per-1m `smt_detect` already
produces the authoritative, relevance-filtered set persisted as `divs`. The fixed-cadence reform also
**resets `failed_entries` / `cautious_dist_shrinks` on every `none→dir` transition**
(`reset_position_for_new_hypothesis`, `build_hypothesis_from_direction` reset block), so once reforms
become frequent and event-driven (same-tier flips), the dynamic-threshold shrink can never accumulate.

## Solution Statement

- **Direction**: in `run_hypothesis`/`build_hypothesis_from_direction`, when the persisted active set
  is non-empty, set direction from `dominant(active_set)`'s side; else call the **unchanged**
  `_determine_direction` (with its SMT-score path removed). `build_hypothesis_from_direction` keeps
  ALL existing vetoes (ATH guard, no-targets→none, secondary cautious `< CAUTIOUS_MIN_DIST`→none) and
  its cautious computation. At entry, `position["active"].backing_tier` is set to the dominant SMT's
  tier (Contract A).
- **Cadence**: remove the per-5m `run_hypothesis` call + the `run_hypothesis` "sticky" early-exit
  (`if old_direction != "none": return []`). Drive reforms from `on_1m_bar` via **ingest** (Phase 2
  `ingest_smts`) of the per-1m `smt_detect` emissions and the per-1m **fulfillment query**
  (`fulfillment_status`): reform when ingest makes a new dominant, OR when the current dominant
  fulfills/invalidates. Add a session-start formation (already largely present in `on_session_start`)
  and a low-frequency safety-net formation when stuck at `none`.
- **Reset detachment**: remove the `failed_entries` / `cautious_dist_shrinks` reset from the
  `none→dir` reform path (`reset_position_for_new_hypothesis`, `build_hypothesis_from_direction`
  position-reset block); keep them on `reset_position_for_session` (session start) and add them to the
  **successful-closure** path (cautious-target / managed exit, NOT the stop-out path).
- Validate with unit + integration tests (below) and recommend a 1s regression A/B (event-driven vs
  prior fixed-cadence) as a **manual** validation step.

## Feature Metadata

**Feature Type**: Behavioral redesign (direction engine + reform cadence) + Refactor (remove internal
SMT recompute).
**Complexity**: 🔴 Complex.
**Primary Systems Affected**: `hypothesis.py` (direction source, vetoes kept, internal SMT path
removed, position-reset detached), `session_pipeline.py` (`on_1m_bar` cadence: per-1m ingest + reform
+ fulfillment, remove 5m `run_hypothesis`; `on_session_start` initial formation), `strategy.py`
(reset sites — detach from flip, add to successful closure), `trend.py` (successful-closure reset hook
— see Wave 3), unit + integration tests.
**Dependencies**: **Phase 1** (`smt-v2-decouple-active-position.md`) and **Phase 2**
(`smt-v2-relevance-filter-core.md`) — HARD prerequisites; their Contracts A/B/C are consumed. No new
third-party deps. Backtest via `regression.py` → `session_pipeline` (already present in the worktree).
**Breaking Changes**: **Yes.** The direction engine changes behavior: direction is the dominant SMT
when a set exists (no longer a blended score), reform is event-driven (no fixed 5m), and
`failed_entries` / `cautious_dist_shrinks` no longer reset on a flip. `_compute_divs` /
`_compute_smt_score` are removed (internal-only functions). Covered by tests; gated on a 1s regression
A/B before any merge.

---

## CONTEXT REFERENCES

### Contracts provided by Phases 1 & 2 (CONSUME — do NOT redefine)

- **Contract A (Phase 1, `position["active"]`)**: `mgmt_direction`, `cautious_initial`,
  `cautious_initial_level`, `cautious_secondary`, `cautious_secondary_level`, `backing_tier` — frozen
  at fill; `trend.py` manages off these (the live trade no longer follows `hypothesis["direction"]`).
  **Phase 3 sets `backing_tier` to the dominant SMT's tier at entry.** Phase 1 also **removed** the
  direction-mismatch force-close, so a reform during an active position does not close the trade.
- **Contract B (Phase 2, `hypothesis.py`)**:
  - `smt_authority(record) -> <comparable tier/authority>` — ranks a record.
  - `dominant(active_set) -> record | None` — the highest-authority record in the set.
  - `ingest_smts(new_records, active_set, *, flat, cautious_targets, backing_tier, x_pts) -> active_set`
    — relevance-filtered merge; the function that decides whether a new record enters the active set.
  - `to_record(emission) -> record` — normalizes a per-1m `smt_detect` emission into an active-set
    record.
  - `divs` (hypothesis.json) **is** the persisted active set (Phase 2 schema).
- **Contract C (Phase 2, `smt_detect.py`)**:
  - `fulfillment_status(keys, detect_state) -> {key: 'unfulfilled'|'fulfilled'|'gone'}` — queried
    per-1m against the active set's keys to detect fulfillment/invalidation of the dominant.

> The execution agent MUST read the actual Phase-1/2 signatures in the tree before wiring; the
> argument names above are the brainstorm-locked contract and may differ slightly in spelling. If a
> name differs, follow the real signature and note it in the execution report.

### Relevant codebase files — READ BEFORE IMPLEMENTING (verified line numbers, current HEAD)

- `hypothesis.py:324–425` — `_compute_divs(mnq_1m, mes_1m)`: resamples 15m/30m, calls
  `detect_smt_divergence` / `detect_smt_fill`, returns the old `smt-div` list. **REMOVE** (Wave 1).
- `hypothesis.py:663–685` — `_compute_smt_score(divs, liquidities)`. **REMOVE** (Wave 1).
- `hypothesis.py:688–1020` — `_determine_direction(...)`: the structural engine. **KEEP** as the
  empty-set fallback, but remove its SMT-score dependency: the `divs` param (L694), `smt_sc =
  _compute_smt_score(...)` (L703), the `_co_evaluate_with_smt(...)` call in Rule 1 (L754,
  `reason["smt_alignment"]`), the `combined = 0.65*r3_sc + 0.35*smt_sc` blend (L1006) → set
  `combined = r3_sc` (drop the SMT term), and the `reason["smt_score"]` / `reason["smt_alignment"]`
  fields (L720, L728). Rules 1/2/2b/3-4/5 and the global-trend fallback (L1019–1020) stay.
- `hypothesis.py:1141–1283` — `build_hypothesis_from_direction(...)`: called with the **dominant (or
  fallback) direction**. **KEEP** the cautious computation (L1190–1198), Step 8b vetoes
  (L1201–1209: secondary `< CAUTIOUS_MIN_DIST`→none, ATH guard `current_close >= ath`→none, no
  targets→none), entry_ranges (L1211–1221), and the `new-hypothesis` event (L1261–1283). The `divs`
  parameter / write (L1151, L1235) now carries the **active set** (Phase 2), not the old recompute.
  **The position-reset block (L1252–1259) is where `failed_entries`/`cautious_dist_shrinks` reset on
  `none→dir` — DETACH it (Wave 3).**
- `hypothesis.py:1286–1444` — `run_hypothesis(...)`: the 5m entry. **Remove the sticky early-exit**
  (L1308–1309: `if old_direction != "none": return []` — this is the only thing that prevented a
  re-form when a direction already exists; event-driven reform needs to run regardless). Remove the
  internal `divs` computation (Step 5, L1391–1417) and pass the **persisted active set** instead. The
  `confidence == "high"` override (L1421–1423) stays. Direction selection (L1424–1435) becomes:
  `_active = hypothesis.get("divs", [])`; `_dom = dominant(_active)`; if `_dom` → direction =
  `"up" if _dom side is long else "down"`, `direction_reason = {"rule": "smt_dominant", ...}`; else →
  `_determine_direction(...)` (without `divs`). See Wave 2 for the exact shape.
- `hypothesis.py:1252–1259` — the `old_direction == "none" and direction != "none"` reset block:
  `skip_position_reset` branch resets `failed_entries`/`cautious_dist_shrinks` directly; else calls
  `_strategy.reset_position_for_new_hypothesis()`. **Both reset paths must stop resetting those two
  counters** (Wave 3).
- `session_pipeline.py:1078–1128` — the `is_5m` gate (`_this_5m`/`is_5m` at L1078–1079) and the 5m
  `run_hypothesis` call (L1096–1104) + `new-hypothesis` emission loop (L1110–1122) + direction reload
  (L1123–1128). **Re-wire**: remove the per-5m `run_hypothesis`; reform is now triggered by the
  ingest/fulfillment logic in Wave 4. Keep the `_hyp_dir` reload + `_accepted_level_sweeps`/
  `_swept_levels_since_hyp` clear, but trigger it on the **reform event**, not the 5m boundary.
- `session_pipeline.py:1085–1090` — the per-1m `_run_smt_v2_detection(...)` call that yields `smt-div`
  signals every bar (`_sd`). **This is the ingest feed.** The returned records (currently only emitted
  for plotting) become the input to `ingest_smts` (Wave 4).
- `session_pipeline.py:1610–1744` — `_run_smt_v2_detection(...)`: per-1m detection → buffers →
  reference consumer → `smts.json`. It already returns one signal per new record and mutates
  `self._detect_state` / `self._pending_watch` / `self._smt_buffer`. Wave 4 wires its output into the
  active-set ingest and queries `fulfillment_status(keys, self._detect_state)` for the dominant.
  The `PendingSmtWatch`/`SmtBuffer` init is at `session_pipeline.py:307–309` and the restart restore
  at `579–582`.
- `session_pipeline.py:544–640` — `on_session_start(...)`: already runs `run_hypothesis` at session
  open (force-reset branch L593–597; normal branch L603–609) and reconciles with an active position
  (L611–640). **This IS the session-start initial formation** — keep it; it must produce a directional
  hypothesis from the structural fallback when no SMT exists yet at open. `reset_position_for_session`
  (the session-start counter reset) is called from `daily.py`/force-reset; verify it still fires.
- `strategy.py:598–625` — the stop-out block: increments `failed_entries` (L605) and
  `cautious_dist_shrinks` (L606). **This is the STOP path — counters MUST keep incrementing here and
  must NOT be reset by a flip.**
- `strategy.py:626–655` — the above-ATH **managed close** path (`market-close`, L648). This is a
  **successful (non-stopped) closure** → it is one of the reset points (Wave 3).
- `strategy.py:663–676` — `reset_position_for_session()`: resets `failed_entries` (L674) +
  `cautious_dist_shrinks` (L675). **KEEP — session-start reset.**
- `strategy.py:679–692` — `reset_position_for_new_hypothesis()`: resets `failed_entries` (L686) +
  `cautious_dist_shrinks` (L687) + entry-state fields. **REMOVE the two counter resets here** (this is
  the flip path); keep the entry-state field clears (`conf_bar_entry`, `stop_entry`, etc.).
- `trend.py:67–90` — `_clear_position_and_hypothesis(...)`: the managed-exit cleanup that sets
  `hypothesis["direction"]="none"`. **Successful (non-stopped) managed exits route through here** —
  add the `failed_entries`/`cautious_dist_shrinks` reset to the **successful** closure path (NOT the
  stopped-out path). Read this function to find the success-vs-stop discriminator before wiring
  (Wave 3 — exact site verified at implementation).
- `smt_state.py:132–146` — `DEFAULT_HYPOTHESIS` (`divs` field present, L142; `direction`,
  `manual`). `smt_state.py:148–158` — `DEFAULT_POSITION` (`failed_entries` L155,
  `cautious_dist_shrinks` L156). No schema change required by Phase 3 (Phase 1 adds the `active`
  sub-keys; Phase 2 redefines `divs` semantics).
- `tests/test_smt_hypothesis.py`, `tests/test_hypothesis_smt.py` — existing hypothesis unit tests
  (will need updates where they assert the old `_compute_divs`/`_compute_smt_score`/blended-score
  behavior). `tests/test_session_pipeline.py` — pipeline/cadence tests (the 5m `run_hypothesis`
  cadence test must be replaced by event-driven tests). `tests/test_smt_detect.py` — fulfillment
  query coverage lives near Phase 2's tests. `tests/test_smt_strategy_v2.py` / `tests/test_smt_trend.py`
  — reset-site + reform-during-position integration.

### New files to create

- **None required.** All production changes are edits to `hypothesis.py`, `session_pipeline.py`,
  `strategy.py`, and `trend.py`. Tests are ADDED to the existing `tests/` files above (a new
  `tests/test_smt_hypothesis_v2_direction.py` is acceptable if cleaner — see Task 2.2).

### Patterns to follow

**Naming**: module-level tunables are UPPER_SNAKE near the top of each module; private helpers are
`_snake_case` with a docstring. Direction strings are `"up"`/`"down"`/`"none"` in hypothesis,
`"long"`/`"short"` in SMT records and `position["active"]["direction"]` — convert explicitly
(`"up" if side == "long" else "down"`), mirroring `on_session_start`'s `_pos_hyp_dir` map
(`session_pipeline.py:616`).
**Error handling**: pure-compute returns `None`/empty to signal "no result"; a missing/empty active
set → `dominant` returns `None` → structural fallback. Guard all `.get()` lookups. Never crash on a
degenerate active set.
**Logging**: production code is silent on success. Do NOT add new stdout logging in the reform path —
attribution is via the `new-hypothesis` event's `direction_reason` (`rule: "smt_dominant"` vs the
structural rule names). Any `print()` added for debugging MUST be removed before completion.
**Reform = signal only**: a reform writes `hypothesis.json` and emits a `new-hypothesis` event; it
performs **no** entry and **no** exit. Entry is gated by `strategy.run_strategy`; exits are managed by
`trend.py` off the frozen `position["active"]` (Phase 1). Do NOT add any close/entry call to the
reform path.

---

## PARALLEL EXECUTION STRATEGY

### Dependency graph

```
        [ Phase 1 + Phase 2 merged in tree ]  ← HARD PREREQUISITE
                        │
┌───────────────────────────────────────────────────────────────┐
│ WAVE 1: Remove internal SMT path (hypothesis.py)               │
│   1.1 REMOVE _compute_divs + _compute_smt_score                │
│   1.2 De-SMT _determine_direction (drop divs/smt_sc/blend)     │
│   (1.1 BLOCKS 1.2; do as one agent on hypothesis.py)           │
└───────────────────────────────────────────────────────────────┘
                        │
┌───────────────────────────────────────────────────────────────┐
│ WAVE 2: Direction source = dominant SMT (hypothesis.py)        │
│   2.1 run_hypothesis: remove sticky early-exit; use persisted  │
│       active set; dominant→dir else structural fallback        │
│   2.2 build_hypothesis_from_direction: keep vetoes; set        │
│       backing_tier at entry (Contract A)                       │
└───────────────────────────────────────────────────────────────┘
              │                              │
              ▼                              ▼
┌──────────────────────────────┐  ┌──────────────────────────────┐
│ WAVE 3: Reset detachment     │  │ WAVE 4: Event-driven cadence  │
│ (strategy.py / trend.py /    │  │ (session_pipeline.py)         │
│  hypothesis.py reset block)  │  │   4.1 per-1m ingest→reform    │
│   3.1 remove flip reset      │  │   4.2 per-1m fulfillment→flip │
│   3.2 add success-close reset│  │       / none                  │
│   (independent of Wave 4)    │  │   4.3 remove 5m run_hypothesis│
│                              │  │   4.4 safety-net formation    │
└──────────────────────────────┘  └──────────────────────────────┘
              │                              │
              └──────────────┬───────────────┘
                             ▼
┌───────────────────────────────────────────────────────────────┐
│ WAVE 5: Integration tests + 1s regression A/B (manual)         │
└───────────────────────────────────────────────────────────────┘
```

### Parallelization summary

- **Wave 1** — single agent on `hypothesis.py` (1.1 then 1.2; same file, sequential edits).
- **Wave 2** — single agent on `hypothesis.py` (2.1 + 2.2 touch adjacent functions). Depends on Wave 1.
- **Wave 3 & Wave 4 run in PARALLEL** after Wave 2: Wave 3 edits `strategy.py` / `trend.py` / the
  `hypothesis.py` reset block; Wave 4 edits `session_pipeline.py`. Disjoint files (the only
  `hypothesis.py` touch in Wave 3 is the reset block at L1252–1259, far from Wave 2's direction code —
  if one agent owns `hypothesis.py` across Waves 2/3 this is trivially safe; if split, integrate the
  reset-block edit last).
- **Wave 5** — sequential: full suite + integration + manual regression.

### Interface contracts (between waves)

- **Wave 1 → Wave 2**: after Wave 1, `_determine_direction(...)` no longer takes `divs` and no longer
  references `smt_sc`/`_co_evaluate_with_smt`; its signature is the empty-set fallback. Wave 2 calls
  it without `divs`.
- **Wave 2 → Waves 3/4**: `build_hypothesis_from_direction` is the single reform entry point; it
  consumes the dominant-or-fallback direction and the persisted active set as `divs`, keeps all
  vetoes, sets `backing_tier`, and is reset-free for the two counters (Wave 3 enforces). Wave 4 calls
  the reform via this path (directly or via a thin `run_hypothesis` that no longer early-exits).
- **Wave 4 ↔ Phase 2 (Contracts B/C)**: Wave 4 calls `to_record` on each `_run_smt_v2_detection`
  record, `ingest_smts(...)` to update the active set, `dominant(...)` to pick the leader, and
  `fulfillment_status(active_keys, self._detect_state)` per-1m. The **reform trigger** is:
  `new_dominant_key != prev_dominant_key` (a new/same-or-higher-tier dominant) OR
  `fulfillment_status[prev_dominant_key] in {"fulfilled","gone"}`.
- **Wave 3 reset semantics** (the load-bearing contract): `failed_entries` and `cautious_dist_shrinks`
  reset **iff** (session start) OR (successful, non-stopped trade closure). They are **never** reset
  by a `none→dir` reform/flip. They **increment** only on stop-out (`strategy.py:605–606`,
  unchanged).

### Synchronization checkpoints

- **After Wave 1**: `uv run python -c "import hypothesis"` clean; `_compute_divs`/`_compute_smt_score`
  gone (`grep` returns nothing); hypothesis unit tests that don't depend on the removed score still
  pass (some will be updated in Wave 2/5).
- **After Wave 2**: `uv run python -m pytest tests/test_smt_hypothesis.py tests/test_hypothesis_smt.py -q`
  — direction = dominant when set non-empty; structural fallback when empty; vetoes intact.
- **After Waves 3 & 4**: `uv run python -m pytest tests/test_session_pipeline.py tests/test_smt_strategy_v2.py tests/test_smt_trend.py -q`.
- **After Wave 5**: `uv run python -m pytest tests/ -q` (full suite, `-m 'not integration'` via
  pyproject addopts) green; manual 1s regression A/B captured.

---

## STEP-BY-STEP TASKS

Tasks are grouped by wave. Same wave (where noted) = safe to run in parallel.
**Task keywords**: REMOVE · UPDATE · ADD · REFACTOR · WIRE.

---

### WAVE 1: Remove the internal SMT detection path (hypothesis.py)

#### Task 1.1: REMOVE `_compute_divs` and `_compute_smt_score`

- **WAVE**: 1
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [Phase 1, Phase 2]
- **BLOCKS**: 1.2, 2.1
- **PROVIDES**: hypothesis no longer recomputes 15m/30m SMTs internally.
- **IMPLEMENT**:
  - Delete `_compute_divs` (`hypothesis.py:324–425`) and `_compute_smt_score`
    (`hypothesis.py:663–685`).
  - Remove now-unused imports of `detect_smt_divergence` / `detect_smt_fill` (the
    `strategy_smt.detect_smt_*` internal-detection calls) **iff** no other live function references
    them — `grep` first; the Phase-2 `smt_detect` path is the only SMT producer now. If
    `_closest_level_name` (`~636–660`) is only used by `_compute_smt_score`, remove it too; otherwise
    keep.
- **PATTERN**: `hypothesis.py:324–425`, `663–685`.
- **VALIDATE**: `uv run python -c "import hypothesis"`; `grep -n "_compute_divs\|_compute_smt_score" hypothesis.py` returns nothing.

#### Task 1.2: UPDATE `_determine_direction` — drop the SMT-score dependency (keep the structural engine)

- **WAVE**: 1
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: 2.1
- **PROVIDES**: `_determine_direction` is the pure structural empty-set fallback.
- **IMPLEMENT**:
  - Remove the `divs: list` parameter (`hypothesis.py:694`) and the `smt_sc = _compute_smt_score(...)`
    line (L703).
  - Rule 1 (L752–758): replace `_co_evaluate_with_smt(r1["direction"], r1["base_conf"], smt_sc)` —
    the SMT co-evaluation is gone; return `r1["direction"]` directly. Drop `reason["smt_alignment"]`
    assignment. (If `_co_evaluate_with_smt` becomes unused, remove it.)
  - Rules 3+4 blend (L1004–1006): change `combined = 0.65 * r3_sc + 0.35 * smt_sc` → `combined = r3_sc`
    (structure-only). The `DIRECTION_SCORE_THRESHOLD` check (L1014) and the `rule5_trend` global-trend
    fallback (L1018–1020) are unchanged.
  - Remove `reason["smt_score"]` (L720) and `reason["smt_alignment"]` (L728) from the `reason` dict
    (or keep the keys set to `None` if a downstream consumer reads them — `grep` `direction_reason`
    consumers first; prefer removal if unused).
  - **KEEP** everything else in `_determine_direction`: Rules 1/2/2b, PD score, BOS/CHoCH 1hr+4hr,
    zones, all guards.
- **PATTERN**: `hypothesis.py:688–1020`.
- **VALIDATE**: `uv run python -c "import hypothesis"`; structural-only direction on a no-SMT fixture
  matches the pre-change result when SMT score was 0 (regression guard — see Test T-FALLBACK).

**Wave 1 Checkpoint**: `uv run python -m pytest tests/test_smt_hypothesis.py -q -k "direction or determine"` (update assertions that referenced the removed score; structural rules unchanged).

---

### WAVE 2: Direction source = dominant SMT, else structural fallback (hypothesis.py)

#### Task 2.1: UPDATE `run_hypothesis` — remove sticky early-exit; dominant-driven direction

- **WAVE**: 2
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [1.1, 1.2]
- **BLOCKS**: 3.1, 4.1, 4.3
- **PROVIDES**: a re-runnable formation that derives direction from the dominant SMT or structure.
- **IMPLEMENT**:
  - **Remove the sticky early-exit** at `hypothesis.py:1308–1309`
    (`if old_direction != "none": return []`). Reform must run regardless of the current direction
    (event-driven re-forms while a direction already exists). Keep the fresh-start detection
    (L1311–1330) and the ATH-bar gate (L1340–1341).
  - **Remove the internal `divs` computation** (Step 5, L1391–1417). Instead read the persisted active
    set: `_active_set = hypothesis.get("divs", []) or []` (Phase 2 schema). This is the set used both
    for direction and re-persisted unchanged into the new hypothesis (the active set is owned by the
    pipeline's ingest, not recomputed here).
  - **Direction selection** (replace L1419–1435): keep the `confidence == "high"` override
    (L1421–1423) FIRST. Then:
    ```
    _dom = dominant(_active_set)            # Contract B
    if _dom is not None:
        direction = "up" if _dom["side"] in ("long", "bullish") else "down"
        direction_reason = {"rule": "smt_dominant",
                            "dominant_key": _dom.get("key"),
                            "tier": smt_authority(_dom)}
    else:
        direction, direction_reason = _determine_direction(   # structural fallback (no divs param)
            current_bar=bar, mnq_1m=mnq_1m, hist_mnq_1m=hist_mnq_1m,
            liquidities=liquidities, global_state=global_state, now=now,
            hist_1hr=hist_1hr, hist_4hr=hist_4hr)
    ```
    (Use the real `_dom` field names from Phase 2's `to_record`/record schema — read it; `side`/`key`
    above are the brainstorm contract.)
  - Pass `_active_set` through as the `divs` argument to `build_hypothesis_from_direction`
    (L1437–1444) so the new hypothesis persists the same active set (no recompute).
  - The `mes_1m`/`hist_mes_1m` params become unused by formation (they fed `_compute_divs`); keep them
    in the signature for call-site compatibility OR remove and update callers — prefer **keep** (less
    churn; the pipeline still passes them) and annotate as reserved.
- **PATTERN**: `hypothesis.py:1286–1444`.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_hypothesis.py -q`
- **TESTS TO ADD** (`tests/test_smt_hypothesis.py` or new `tests/test_smt_hypothesis_v2_direction.py`):
  - **T-DOM-UP**: `divs` = non-empty with a dominant `long` SMT → `run_hypothesis` direction `"up"`,
    `direction_reason["rule"]=="smt_dominant"`.
  - **T-DOM-DOWN**: dominant `short` → `"down"`.
  - **T-FALLBACK**: `divs` empty → calls `_determine_direction`; direction equals the structural
    result (assert `direction_reason["rule"]` is one of the structural rule names, not
    `smt_dominant`).
  - **T-DOM-TIE / authority**: two SMTs of different tier → the higher-`smt_authority` one wins
    (delegates to Phase 2 `dominant`; this test pins Phase 3's consumption of it).
  - **T-NO-STICKY**: an existing `direction != "none"` does NOT cause an early `return []` — a reform
    with a changed dominant re-derives direction.

#### Task 2.2: UPDATE `build_hypothesis_from_direction` — keep vetoes; set `backing_tier` at entry

- **WAVE**: 2
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [1.1, 1.2]
- **BLOCKS**: 3.1
- **PROVIDES**: reform writes a hypothesis with vetoes intact and (at entry) the dominant tier.
- **IMPLEMENT**:
  - **No change to the vetoes** (L1201–1209): secondary cautious `< CAUTIOUS_MIN_DIST`→none, ATH
    guard `current_close >= ath`→none, no-targets→none. Keep cautious computation (L1190–1198) and the
    `_dist_shrinks` read (L1193).
  - `divs` parameter now carries the active set; write it unchanged (L1235) — no behavior change to the
    write itself.
  - **`backing_tier`**: `build_hypothesis_from_direction` forms the *hypothesis*, not the trade — the
    trade's `position["active"].backing_tier` is set **at fill** (Contract A, set by the entry path).
    Phase 3's responsibility here is to make the dominant tier **available to the fill path**: persist
    it on the hypothesis (e.g. `new_hypothesis["backing_tier"] = direction_reason.get("tier")` when
    `rule=="smt_dominant"`, else `None`) so `strategy.run_strategy`'s fill handler copies it into
    `position["active"]["backing_tier"]`. Confirm the Phase-1 fill path reads
    `hypothesis.get("backing_tier")` — if Phase 1 already wires `backing_tier` from a different source,
    follow Phase 1's mechanism and only ensure the value is the dominant SMT's tier. (Read the Phase-1
    fill code before choosing; do NOT introduce a second source of truth.)
- **PATTERN**: `hypothesis.py:1141–1283`; tier→fill handoff per Phase 1 Contract A.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_hypothesis.py -q -k "veto or backing or build"`
- **TESTS TO ADD**:
  - **T-VETO-ATH**: dominant `long` but `current_close >= ath` → direction vetoed to `none` (no entry).
  - **T-VETO-NOTARGETS**: dominant direction with no in-direction targets → `none`.
  - **T-VETO-SECDIST**: secondary cautious `< CAUTIOUS_MIN_DIST` (not fresh start) → `none`.
  - **T-BACKING-TIER**: `rule=="smt_dominant"` formation → `hypothesis["backing_tier"]` equals the
    dominant's tier; structural fallback → `backing_tier is None`. (Fill-side propagation asserted in
    Wave 5 integration T-FILL-TIER.)

**Wave 2 Checkpoint**: `uv run python -m pytest tests/test_smt_hypothesis.py tests/test_hypothesis_smt.py -q`

---

### WAVE 3: Reset detachment (strategy.py / trend.py / hypothesis.py reset block)

*Runs in parallel with Wave 4 after Wave 2.*

#### Task 3.1: REMOVE the flip-time reset of `failed_entries` / `cautious_dist_shrinks`

- **WAVE**: 3
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [2.1, 2.2]
- **BLOCKS**: 5.x
- **PROVIDES**: a reform/flip no longer zeroes the dynamic-threshold counters.
- **IMPLEMENT**:
  - `strategy.py:679–692` `reset_position_for_new_hypothesis()`: **delete** the two counter resets
    (`failed_entries` L686, `cautious_dist_shrinks` L687). **Keep** the entry-state clears
    (`conf_bar_entry`, `conf_bar_exit`, `stop_entry`, `stop_direction`). Update the docstring to note
    the counters are intentionally preserved across reforms (they reset only on session start /
    successful closure).
  - `hypothesis.py:1252–1259` reset block: in the `skip_position_reset` branch, **delete**
    `position["failed_entries"] = 0` and `position["cautious_dist_shrinks"] = 0` (L1255–1256). The
    non-`skip` branch already delegates to `reset_position_for_new_hypothesis()` (fixed above). If,
    after removing the counter resets, the `skip_position_reset` branch does nothing else, drop the
    branch entirely (verify it has no other side effect — it doesn't, per current code).
  - **Do NOT touch** `strategy.py:605–606` (the stop-out increment).
- **PATTERN**: `strategy.py:679–692`; `hypothesis.py:1252–1259`.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_strategy_v2.py -q -k "reset or failed or shrink"`
- **TESTS TO ADD** (`tests/test_smt_strategy_v2.py`):
  - **T-NORESET-FLIP**: set `failed_entries=2`, `cautious_dist_shrinks=2`; trigger a `none→dir`
    reform via `build_hypothesis_from_direction` (skip and non-skip branches) → both counters
    **unchanged** at 2.
  - **T-RESET-SESSION**: `reset_position_for_session()` → both counters `0` (unchanged behavior).

#### Task 3.2: ADD the successful-closure reset of `failed_entries` / `cautious_dist_shrinks`

- **WAVE**: 3
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [2.1, 2.2]
- **BLOCKS**: 5.x
- **PROVIDES**: counters reset after a non-stopped (successful) trade closure.
- **IMPLEMENT**:
  - Identify the successful (non-stopped) managed-exit path(s). The stop-out path
    (`strategy.py:598–625`) **increments** and must NOT reset. The successful exits are:
    - `strategy.py:626–655` above-ATH `market-close` (a managed reversal exit) — reset both counters
      to 0 in the `position["active"] = {}` cleanup (L642–646) **before** `save_position`.
    - `trend.py` cautious-target / managed closes that route through
      `_clear_position_and_hypothesis(...)` (`trend.py:67–90`). Read that function: if it is shared by
      both the success and the stop paths, add the reset **only on the success branch** (discriminate
      via the close reason / `stopped` flag — confirm the discriminator at the call sites in `trend.py`
      before editing). The cautious-target exits are the primary success path Phase 1 relies on.
  - The reset is `position["failed_entries"] = 0; position["cautious_dist_shrinks"] = 0` alongside the
    existing `position["active"] = {}` cleanup, inside the same `save_position` write.
  - **Atomicity**: the counter reset and the `active`-clear must be in the **same** `save_position`
    call (single write), not two writes.
- **PATTERN**: `strategy.py:642–646`; `trend.py:67–90` + its managed-close call sites.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_trend.py tests/test_smt_strategy_v2.py -q -k "close or cautious or reset or ath"`
- **TESTS TO ADD**:
  - **T-RESET-SUCCESS-CAUTIOUS** (`tests/test_smt_trend.py`): `failed_entries=2`,
    `cautious_dist_shrinks=2`, an active position; a cautious-target managed close fires → both
    counters `0` after close.
  - **T-RESET-SUCCESS-ATH** (`tests/test_smt_strategy_v2.py`): the above-ATH `market-close` path →
    both counters `0`.
  - **T-NORESET-STOP** (`tests/test_smt_strategy_v2.py`): a stop-out → `failed_entries` and
    `cautious_dist_shrinks` **increment** (not reset), confirming the stop path is excluded.

**Wave 3 Checkpoint**: `uv run python -m pytest tests/test_smt_strategy_v2.py tests/test_smt_trend.py -q`

---

### WAVE 4: Event-driven reform cadence (session_pipeline.py)

*Runs in parallel with Wave 3 after Wave 2.*

#### Task 4.1: WIRE per-1m `smt_detect` emissions → active-set ingest → reform on new dominant

- **WAVE**: 4
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [2.1, 2.2]
- **BLOCKS**: 4.3, 5.x
- **PROVIDES**: a relevant SMT becoming the new dominant re-forms immediately (same-or-higher tier,
  no confirmation wait).
- **IMPLEMENT**:
  - In `on_1m_bar`, after `_run_smt_v2_detection(...)` returns its records (`session_pipeline.py:1085`),
    capture the **raw records** (not just the emitted `smt-div` signals). `_run_smt_v2_detection`
    currently returns `sd_events`; either (a) have it also return the raw `records`, or (b) expose the
    records via a member set during the call. Prefer extending the return to
    `(sd_events, new_records)` and updating the single call site — minimal blast radius.
  - Load the current hypothesis + active set: `_hyp = _smt_state.load_hypothesis()`;
    `_active = _hyp.get("divs", []) or []`. Compute `_prev_dom = dominant(_active)`.
  - Convert + ingest (Contract B):
    ```
    _recs = [to_record(e) for e in new_records]
    _flat = not _smt_state.load_position().get("active")
    _ct = (_hyp.get("cautious_price_initial",""), _hyp.get("cautious_price_secondary",""))
    _bt = _smt_state.load_position().get("active", {}).get("backing_tier")
    _active = ingest_smts(_recs, _active, flat=_flat,
                          cautious_targets=_ct, backing_tier=_bt, x_pts=<Phase2 default>)
    ```
    (Use Phase 2's real `ingest_smts` signature + the `x_pts` source it defines.)
  - Persist the ingested set back onto the hypothesis **before** deciding to reform (so the reform
    reads the updated `divs`): write `_hyp["divs"] = _active; _smt_state.save_hypothesis(_hyp)`.
  - **Reform trigger**: `_new_dom = dominant(_active)`; if `_new_dom`'s key differs from `_prev_dom`'s
    key (a new/same-or-higher-tier dominant — `ingest_smts` already enforces relevance + tier
    ordering, so any change in the dominant key IS a qualifying flip), call the reform (Task 4.3's
    `_reform(now)` helper). An **irrelevant** SMT is filtered out by `ingest_smts` and therefore does
    NOT change the dominant → no reform (asserted by T-NOREFORM-IRRELEVANT).
  - **No entry/exit on reform** — the reform only writes the hypothesis + emits `new-hypothesis`.
- **PATTERN**: `session_pipeline.py:1085–1090` (records feed), `1610–1744` (`_run_smt_v2_detection`).
- **VALIDATE**: `uv run python -m pytest tests/test_session_pipeline.py -q -k "ingest or reform or dominant"`
- **TESTS TO ADD** (`tests/test_session_pipeline.py`):
  - **T-REFORM-NEWDOM**: feed a relevant SMT that becomes the new dominant → a `new-hypothesis` event
    is emitted that bar with the dominant's direction; `hypothesis["divs"]` contains the new record.
  - **T-NOREFORM-IRRELEVANT**: feed an SMT that `ingest_smts` filters out (not relevant) → dominant
    unchanged → **no** `new-hypothesis` event.
  - **T-REFORM-SAMETIER-FLIP**: an opposite-direction same-tier SMT becomes dominant → immediate
    flip (`new-hypothesis` with the flipped direction), no confirmation delay.

#### Task 4.2: WIRE per-1m fulfillment query → reform on dominant fulfilled/invalidated

- **WAVE**: 4
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [2.1, 2.2]
- **BLOCKS**: 4.3, 5.x
- **PROVIDES**: when the current dominant fulfills or is invalidated, re-derive direction → flip or
  none.
- **IMPLEMENT**:
  - After ingest (Task 4.1), query Contract C:
    `_status = fulfillment_status([r["key"] for r in _active], self._detect_state)`.
  - Drop fulfilled/gone records from the active set:
    `_active = [r for r in _active if _status.get(r["key"]) not in ("fulfilled","gone")]` and persist.
  - If the **dominant** was among the dropped (i.e. `_prev_dom` is now fulfilled/gone), trigger a
    reform: `dominant(_active)` is re-derived → either a new dominant (flip) or `None`
    (→ structural fallback, which may yield a direction or, via the vetoes, `none`).
  - Where 4.1 and 4.2 both want to reform on the same bar, reform **once** (compute the final
    `_active` after both ingest and fulfillment-prune, then a single dominant-change check → one
    `_reform`).
- **PATTERN**: `session_pipeline.py` `_run_smt_v2_detection` mutates `self._detect_state`; query it
  after.
- **VALIDATE**: `uv run python -m pytest tests/test_session_pipeline.py -q -k "fulfill or invalidate"`
- **TESTS TO ADD** (`tests/test_session_pipeline.py`):
  - **T-REFORM-FULFILLED-FLIP**: dominant A long; a lower-tier B short also in set; `fulfillment_status`
    marks A `fulfilled` → A dropped → dominant becomes B → reform flips to `down`.
  - **T-REFORM-FULFILLED-NONE**: only dominant A in set; A `fulfilled`/`gone` → set empty → reform via
    structural fallback (assert the resulting direction comes from `_determine_direction`, or `none`
    if vetoed).
  - **T-INVALIDATE-GONE**: dominant marked `gone` (invalidated, not reached) → same as fulfilled
    (dropped + reform).

#### Task 4.3: UPDATE the 5m gate — remove the fixed `run_hypothesis`; centralize `_reform`

- **WAVE**: 4
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [4.1, 4.2]
- **BLOCKS**: 5.x
- **PROVIDES**: cadence is event-driven; no more every-5m reform.
- **IMPLEMENT**:
  - **Remove** the per-5m `run_hypothesis` call and its `new-hypothesis` emission loop
    (`session_pipeline.py:1092–1122`). The `is_5m` flag is still computed (L1078–1079) and still used
    by the SMT cadence (`_run_smt_v2_detection`'s 1m/5m reference consumer) and the safety-net
    (Task 4.4) — keep `is_5m`, remove only the `run_hypothesis` block.
  - Add a private `_reform(self, now, mnq_bar_row, today_mnq, today_mes)` helper that:
    1. calls `run_hypothesis(...)` (now sticky-free, dominant-driven — Wave 2) with the same args the
       old 5m call used (L1096–1104);
    2. emits each returned `new-hypothesis` event (reuse the existing `_above_session_ath` /
       `_last_hyp_cautious` dedup at L1106–1118 and the `_hyp_formation_price = _c` set at L1119–1120);
    3. reloads `_hyp_dir` and clears `_accepted_level_sweeps` / `_swept_levels_since_hyp` on a
       direction change (the L1123–1128 logic) — return the new `_hyp_dir` to the caller so the
       same-bar `run_strategy` sees the updated bias.
  - Call `_reform(...)` from the Task-4.1/4.2 trigger (dominant changed OR dominant fulfilled/gone)
    and from the safety-net (Task 4.4). The reform may fire on **any** bar, including during an active
    position — it emits `new-hypothesis` but never an entry/exit (Phase 1 safety).
- **PATTERN**: `session_pipeline.py:1092–1128`.
- **VALIDATE**: `uv run python -m pytest tests/test_session_pipeline.py -q`
- **TESTS TO ADD**:
  - **T-NO-5M-REFORM**: advance several 5m boundaries with **no** SMT change and a stable dominant →
    **no** `new-hypothesis` emitted purely because a 5m boundary passed (the old behavior is gone).
  - **T-REFORM-DURING-POSITION**: with an active position, trigger a dominant flip → a
    `new-hypothesis` is emitted but **no** `market-close` / entry signal fires that bar, and
    `position["active"]` is unchanged (integration with Phase 1 decoupling; see also Wave 5
    T-FLIP-NO-CLOSE).

#### Task 4.4: ADD a low-frequency safety-net formation (never stuck at `none`)

- **WAVE**: 4
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [4.3]
- **BLOCKS**: 5.x
- **PROVIDES**: when there are no SMT events and direction is `none`, a periodic formation still runs.
- **IMPLEMENT**:
  - Add a member `self._last_safety_net: pd.Timestamp | None` (init in `__init__` near the other
    cadence members at `session_pipeline.py:233`, and reset in `on_session_start` near L570–574).
  - In `on_1m_bar`, after the ingest/fulfillment block: if
    `_smt_state.load_hypothesis().get("direction","none") == "none"` AND the active set is empty AND
    `(self._last_safety_net is None or now - self._last_safety_net >= SAFETY_NET_INTERVAL)`, call
    `_reform(...)` and set `self._last_safety_net = now`. Use a module constant
    `SAFETY_NET_INTERVAL = pd.Timedelta(minutes=15)` (tune in Wave 5; the point is "low frequency",
    not the old 5m). Gate on `is_5m` to avoid sub-minute churn if preferred — choose one and document.
  - The safety net only fires when stuck at `none` with no SMT — once any directional hypothesis or
    SMT exists, it does nothing.
- **PATTERN**: `session_pipeline.py:233` (member init), `570–574` (session reset), the cadence floor
  pattern at `_run_smt_v2_detection`.
- **VALIDATE**: `uv run python -m pytest tests/test_session_pipeline.py -q -k "safety"`
- **TESTS TO ADD**:
  - **T-SAFETYNET-FIRES**: direction `none`, empty active set, advance past `SAFETY_NET_INTERVAL` with
    no SMT → a formation runs (structural fallback may set a direction or stay `none` per vetoes), and
    `_last_safety_net` advances.
  - **T-SAFETYNET-SUPPRESSED**: a directional hypothesis already exists → the safety net does NOT fire
    an extra reform.

**Wave 4 Checkpoint**: `uv run python -m pytest tests/test_session_pipeline.py -q`

---

### WAVE 5: Integration + manual regression

#### Task 5.1: Integration tests — reform-during-position, fill-tier propagation, reset lifecycle

- **WAVE**: 5
- **AGENT_ROLE**: backend
- **DEPENDS_ON**: [3.1, 3.2, 4.1, 4.2, 4.3, 4.4]
- **BLOCKS**: 5.2
- **IMPLEMENT** (tests only; production code already complete):
  - **T-FLIP-NO-CLOSE** (`tests/test_session_pipeline.py` or `tests/test_smt_trend.py`): an active
    long position (frozen `mgmt_direction="long"`, Phase 1); feed an SMT that flips the hypothesis to
    `down` → `new-hypothesis` emitted, the position is still managed off its frozen snapshot (no
    `market-close`, no force-close — Phase 1 removed it), `failed_entries`/`cautious_dist_shrinks`
    unchanged by the flip.
  - **T-FILL-TIER** (integration): a `smt_dominant` formation → entry fill → assert
    `position["active"]["backing_tier"]` equals the dominant SMT's tier (Contract A handoff).
  - **T-LIFECYCLE-RESET**: full lifecycle — session start (counters 0) → stop-out (counters
    increment) → reform/flip (counters unchanged) → successful cautious close (counters 0). Asserts
    the full Wave-3 reset semantics end-to-end.
- **VALIDATE**: `uv run python -m pytest tests/ -q`

#### Task 5.2: Manual 1s regression A/B (event-driven vs prior fixed-cadence)

- **WAVE**: 5
- **AGENT_ROLE**: backend / quant
- **DEPENDS_ON**: [5.1]
- **IMPLEMENT** (no production code unless tuning `SAFETY_NET_INTERVAL`):
  - Run the 1s regression on a multi-day window (use the dates already in the repo's regression
    history) on the Phase-3 tree, and compare against a baseline run of the **pre-Phase-3** tree
    (the prior fixed-cadence behavior). Compare trade count, total P&L, win rate, and number of
    reforms / direction-flips per session.
  - **This is a manual validation step, not an automated gate.** Do NOT run integration/IB/
    orchestrator/live tests (see VALIDATION COMMANDS — a live trading process may be running).
  - Capture the before/after table in the execution report. **Do NOT merge** — user approval gate.
- **VALIDATE**: regression completes; before/after table produced; full unit suite still green.

**Final Checkpoint**: `uv run python -m pytest tests/ -q` (full suite green) + manual 1s A/B captured.

---

## TESTING STRATEGY

All Phase-3 logic is pure-Python + deterministic pipeline replay — fully automatable with synthetic
fixtures (no IB/network). The 1s regression A/B is a **manual** validation, not an automated gate.

| What you're testing | Tool | Location |
|---|---|---|
| Direction = dominant SMT / structural fallback / vetoes | pytest | `tests/test_smt_hypothesis.py` (+ `..._v2_direction.py`) |
| Reset detachment (flip vs session vs success) | pytest | `tests/test_smt_strategy_v2.py`, `tests/test_smt_trend.py` |
| Event-driven cadence (ingest / fulfillment / no-5m / safety-net) | pytest | `tests/test_session_pipeline.py` |
| Reform-during-position (no close/entry) + fill-tier | pytest (integration-style, in-process) | `tests/test_session_pipeline.py` / `tests/test_smt_trend.py` |
| End-to-end behavior over real bars | `regression.py` (manual A/B) | — |

### Coverage matrix (every changed function/branch → a named test; ✅ covered, ⚠️ verify-and-fill)

| Changed surface | Test(s) | Status |
|---|---|---|
| `_compute_divs` / `_compute_smt_score` removed | grep-absent + import-clean (Wave 1 VALIDATE) | ✅ |
| `_determine_direction` de-SMT'd (structural-only) | T-FALLBACK (structural result == old SMT=0 result) | ✅ |
| `run_hypothesis` sticky early-exit removed | T-NO-STICKY | ✅ |
| `run_hypothesis` dominant→direction | T-DOM-UP, T-DOM-DOWN, T-DOM-TIE | ✅ |
| `run_hypothesis` empty-set fallback | T-FALLBACK | ✅ |
| `build_hypothesis_from_direction` vetoes kept | T-VETO-ATH, T-VETO-NOTARGETS, T-VETO-SECDIST | ✅ |
| `backing_tier` persisted (smt_dominant) / None (fallback) | T-BACKING-TIER; fill handoff T-FILL-TIER | ✅ |
| Flip does NOT reset counters | T-NORESET-FLIP | ✅ |
| Session start resets counters | T-RESET-SESSION | ✅ |
| Successful cautious/ATH close resets counters | T-RESET-SUCCESS-CAUTIOUS, T-RESET-SUCCESS-ATH | ✅ |
| Stop-out increments (not resets) | T-NORESET-STOP | ✅ |
| Reform on new dominant (relevant) | T-REFORM-NEWDOM, T-REFORM-SAMETIER-FLIP | ✅ |
| NO reform on irrelevant SMT (ingest-filtered) | T-NOREFORM-IRRELEVANT | ✅ |
| Reform on dominant fulfilled → flip / none | T-REFORM-FULFILLED-FLIP, T-REFORM-FULFILLED-NONE | ✅ |
| Reform on dominant invalidated (gone) | T-INVALIDATE-GONE | ✅ |
| No 5m fixed reform | T-NO-5M-REFORM | ✅ |
| Reform during active position = signal only | T-REFORM-DURING-POSITION, T-FLIP-NO-CLOSE | ✅ |
| Safety-net fires when stuck at none | T-SAFETYNET-FIRES | ✅ |
| Safety-net suppressed when directional | T-SAFETYNET-SUPPRESSED | ✅ |
| Full reset lifecycle | T-LIFECYCLE-RESET | ✅ |

> **Coverage pass requirement**: before declaring complete, re-walk every edited function/branch and
> confirm a named test above exercises it. Any ⚠️ row must be filled with a concrete test before the
> Final Checkpoint. Several tests depend on Phase 1/2 behavior (frozen `active`, `dominant`,
> `fulfillment_status`) — those tests assume Phases 1/2 are present in the tree (HARD prerequisite).

### Edge cases (explicit)

- **Empty active set** → `dominant` None → structural fallback (T-FALLBACK).
- **Dominant flip and fulfillment on the same bar** → single reform (Task 4.2 "reform once").
- **Reform during an active position** → no close/entry; counters untouched (T-FLIP-NO-CLOSE).
- **Stuck at none, no SMT** → safety net (T-SAFETYNET-FIRES); not double-firing when directional.
- **`confidence == "high"` override** still takes precedence over the dominant SMT (assert in a
  dedicated test if the override path is reachable in fixtures; otherwise note as covered by the
  existing high-confidence test).

### Production code silent

No `print`/stdout in the reform/ingest/fulfillment paths. Any debug `print` added during execution
MUST be removed before completion (COMPLETION CHECKLIST). Attribution is via the `new-hypothesis`
event's `direction_reason`.

---

## VALIDATION COMMANDS

### Level 1: Syntax & import

```bash
uv run python -c "import hypothesis, session_pipeline, strategy, trend, smt_detect"
```

### Level 2: Unit tests (per wave)

```bash
uv run python -m pytest tests/test_smt_hypothesis.py tests/test_hypothesis_smt.py -q     # Waves 1-2
uv run python -m pytest tests/test_smt_strategy_v2.py tests/test_smt_trend.py -q         # Wave 3
uv run python -m pytest tests/test_session_pipeline.py -q                                # Wave 4
```

### Level 3: Full suite (the side-effecting policy)

```bash
uv run python -m pytest tests/ -q
```

- The pyproject `addopts` applies `-m 'not integration'`, so the full suite excludes integration tests.
- **DO NOT run integration / IB / orchestrator / live tests.** A live trading process may be running on
  this host; integration/IB/orchestrator/live tests can touch shared state, the broker connection, or
  session files. Only the `not integration` unit/in-process suite above is permitted as an automated
  gate.

### Level 4: Manual 1s regression A/B (NOT an automated gate)

```bash
# Phase-3 tree vs a pre-Phase-3 baseline; multi-day window from the repo's regression history.
uv run python regression.py --dates <range> --mode 1s --no-plot
```

Capture before/after (trade count, P&L, win rate, reforms/flips per session). Manual; do not merge on
this alone — user approval gate.

---

## ACCEPTANCE CRITERIA

### Functional — Direction source
- [ ] When the persisted active SMT set (`hypothesis["divs"]`) is **non-empty**, the hypothesis
      direction equals `dominant(active_set)`'s direction (`long→up`, `short→down`), with
      `direction_reason["rule"]=="smt_dominant"`. (T-DOM-UP, T-DOM-DOWN, T-DOM-TIE)
- [ ] When the active set is **empty**, direction falls back to the **existing structural engine**
      `_determine_direction` (PD/BOS-CHoCH + global-trend fallback), which is **kept** and de-SMT'd
      (no `divs`/`smt_sc`/blend). Structural output with no SMT matches the pre-change result for
      SMT-score=0. (T-FALLBACK)
- [ ] `_compute_divs` and `_compute_smt_score` are **removed**; the `strategy_smt.detect_smt_*`
      internal-detection calls inside hypothesis are gone. `divs` now stores the Phase-2 active set.
- [ ] `build_hypothesis_from_direction` **keeps** all vetoes (ATH guard, no-targets→none, secondary
      cautious `< CAUTIOUS_MIN_DIST`→none) and the cautious computation. (T-VETO-*)
- [ ] At entry, `position["active"]["backing_tier"]` is the dominant SMT's tier (Contract A handoff);
      structural-fallback formations carry no SMT tier. (T-BACKING-TIER, T-FILL-TIER)

### Functional — Cadence
- [ ] The fixed every-5m `run_hypothesis` reform is **retired**; no `new-hypothesis` is emitted merely
      because a 5m boundary passed. (T-NO-5M-REFORM)
- [ ] A relevant SMT becoming the new dominant (same-or-higher tier) re-forms **immediately on
      detection**, no confirmation wait. (T-REFORM-NEWDOM, T-REFORM-SAMETIER-FLIP)
- [ ] An **irrelevant** SMT (filtered by `ingest_smts`) does NOT change the dominant and does NOT
      reform. (T-NOREFORM-IRRELEVANT)
- [ ] When the current dominant becomes **fulfilled/invalidated** (`fulfillment_status` → `fulfilled`
      / `gone`), the dominant is re-derived → flip or `none`. (T-REFORM-FULFILLED-FLIP,
      T-REFORM-FULFILLED-NONE, T-INVALIDATE-GONE)
- [ ] A **session-start** initial formation runs; a **low-frequency safety-net** formation fires when
      stuck at `direction=none` with no SMT, and is suppressed when a directional hypothesis exists.
      (session-start via `on_session_start`; T-SAFETYNET-FIRES, T-SAFETYNET-SUPPRESSED)

### Functional — Reform during active position (Phase 1 integration)
- [ ] A reform/flip during an active position emits a `new-hypothesis` signal but triggers **NO entry
      and NO exit**; the live trade remains managed off its frozen `position["active"]` snapshot.
      (T-REFORM-DURING-POSITION, T-FLIP-NO-CLOSE)

### Functional — Reset detachment
- [ ] `failed_entries` and `cautious_dist_shrinks` are **NOT reset** by a `none→dir` reform/flip.
      (T-NORESET-FLIP)
- [ ] They reset **only** on (a) session start and (b) a **successful (non-stopped)** trade closure
      (cautious-target / above-ATH managed close). (T-RESET-SESSION, T-RESET-SUCCESS-CAUTIOUS,
      T-RESET-SUCCESS-ATH)
- [ ] The stop-out path still **increments** both counters (not reset). (T-NORESET-STOP)
- [ ] The counter reset and the `active`-clear on a successful close occur in a **single**
      `save_position` write (atomic).

### Validation
- [ ] Level 1 import-clean; Levels 2 per-wave green; Level 3 full `not integration` suite green.
- [ ] Every changed function/branch has a named test (coverage matrix all ✅; no ⚠️ remaining).
- [ ] Production code silent (no new prints); debug logs added during execution removed.
- [ ] Manual 1s regression A/B captured and shared (before/after); **not merged** (user approval gate).

### Out of scope
- Quick-exit / opposite re-entry on a flip during a position (explicitly deferred).
- Any change to the entry-confirmation logic (`strategy.run_strategy` entry gating), cautious-ladder
  math, or `trend.py` management beyond the success-close reset hook.
- Merging to `live`/`master` (gated on the user-approved regression A/B).

---

## COMPLETION CHECKLIST

- [ ] **Phases 1 & 2 verified present in tree** (Contracts A/B/C importable) BEFORE starting.
- [ ] Wave 1: `_compute_divs` + `_compute_smt_score` removed; `_determine_direction` de-SMT'd, all
      structural rules intact.
- [ ] Wave 2: `run_hypothesis` sticky early-exit removed; dominant-driven direction + structural
      fallback; vetoes kept; `backing_tier` persisted.
- [ ] Wave 3: flip-time counter reset removed (`reset_position_for_new_hypothesis`,
      `build_hypothesis_from_direction` reset block); success-close reset added
      (`strategy.py` ATH close + `trend.py` cautious close); stop-out increment untouched; atomic write.
- [ ] Wave 4: per-1m ingest→reform, per-1m fulfillment→flip/none, 5m fixed reform removed, safety-net
      added; reform = signal only.
- [ ] Wave 5: integration tests (reform-during-position, fill-tier, lifecycle) green; manual 1s A/B
      captured.
- [ ] All wave checkpoints + the full `not integration` suite (`uv run python -m pytest tests/ -q`)
      pass.
- [ ] Coverage matrix fully ✅; production code silent; debug logs removed.
- [ ] **⚠️ CRITICAL: changes UNSTAGED — NOT committed; NOT merged; NO `git add`.**

---

## NOTES

**Why dominant-driven (locked via brainstorm):** Phase 2 already curates a relevance-filtered, tiered
active SMT set. Re-deriving a blended score in `hypothesis` (the old `_compute_divs` /
`_compute_smt_score` / 0.35 blend) duplicated detection and made the strongest available signal only a
fractional score term. Making direction = the dominant SMT (when one exists) lets the curated evidence
decide directly, and keeps the proven structural engine as the no-SMT fallback.

**Why event-driven cadence:** a fixed 5m reform is laggy (a fresh dominant waits up to 5 min) and, once
the active set drives direction, redundant (nothing changes between SMT events except fulfillment).
Reforming on (new dominant) ∪ (dominant fulfilled/invalidated), plus a session-start formation and a
low-frequency safety net, reacts immediately and never gets stuck.

**Why detach the resets:** with frequent same-tier flips, resetting `failed_entries` /
`cautious_dist_shrinks` on every `none→dir` transition would perpetually zero the dynamic-threshold
shrink, defeating it. The counters should track the *trade context* — they reset when the context
genuinely restarts (session start) or completes successfully (a managed close), not when the directional
thesis re-forms.

**Phase-1 safety dependency:** reforms during an active position are only safe because Phase 1 freezes
the trade's management direction + cautious ladder into `position["active"]` and removes the
direction-mismatch force-close. If Phase 1 is NOT in the tree, a reform/flip would (in the old code)
force-close the live trade — DO NOT run Phase 3 without Phase 1.

**Contract spellings:** the argument/field names for `ingest_smts`, `to_record`, record `side`/`key`,
and `fulfillment_status` are the brainstorm-locked contract; the execution agent MUST read the real
Phase-1/2 signatures in the tree and follow them, noting any spelling differences in the execution
report. Do NOT redefine these functions here.

**Tuning:** `SAFETY_NET_INTERVAL` (start 15 min) is a low-frequency floor, not a re-introduction of the
5m cadence; tune in the Wave-5 A/B. `x_pts` for `ingest_smts` comes from Phase 2's default — do not
invent a new value.
