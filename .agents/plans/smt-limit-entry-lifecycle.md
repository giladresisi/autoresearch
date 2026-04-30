# Feature: SMT Limit Entry Lifecycle & Min-TP Fallback

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Rework the entry-signal lifecycle so the human trader sees the limit order *when it is placed* (on divergence), not only when it fills. Today the algo internally queues a limit at `anchor_close ± LIMIT_ENTRY_BUFFER_PTS` inside `WAITING_FOR_LIMIT_FILL` but does not emit anything to the trader until price trades through the limit — so the trader cannot place a resting order in advance.

This plan introduces `LIMIT_PLACED`, `MOVE_LIMIT`, `CANCEL_LIMIT`, `LIMIT_EXPIRED`, and `LIMIT_FILLED` as first-class events emitted by `process_scan_bar` and relayed by `signal_smt.py`. Every entry signal carries a `signal_type` tag (`ENTRY_MARKET` / `ENTRY_LIMIT`) deterministically derived from whether the scan path went through a limit stage, dropping the pts-distance heuristic that conflates "is this actionable as a market order right now" with "what kind of order did the algo simulate".

Also raises `MIN_TARGET_PTS` / `MIN_RR_FOR_TARGET` above zero with a fallback-to-next-ranked target (preserving the divergence when no target passes, so a later confirmation bar can still fire a valid signal off the same divergence).

## User Story

**As a** human trader following `signal_smt.py` output
**I want to** see `LIMIT_PLACED` the moment a divergence is detected (with full entry / stop / TP), be notified via `MOVE_LIMIT` / `CANCEL_LIMIT` when the algo revises the anchor, and see `LIMIT_FILLED` when price touches the resting limit
**So that** I can place a resting limit order in advance, revise it in sync with the algo, and know exactly when I'm in-position without inferring from silence.

## Problem Statement

1. The `SIGNAL` log line appears only on limit fill or confirmation close, too late to pre-place the order.
2. Divergence → `WAITING_FOR_LIMIT_FILL` lifecycle has no visible events when the algo replaces the anchor (replacement logic at `strategy_smt.py:1598–1651`) or abandons the setup (hypothesis invalidation at `strategy_smt.py:1654–1663`) — the trader's resting limit becomes stale without notice.
3. The `ENTRY_LIMIT`/`ENTRY_MARKET` classifier at `signal_smt.py:708–723` is gated on `HUMAN_EXECUTION_MODE` (redundant now that `signal_smt.py:main()` forces it True) and on `ENTRY_LIMIT_CLASSIFICATION_PTS=5.0`, a pts-distance threshold that is orthogonal to whether the algo actually used a limit stage.
4. `MIN_TARGET_PTS=0.0` / `MIN_RR_FOR_TARGET=0.0` (`strategy_smt.py:192–193`) allow TP crumbs (11–12 pts observed in the 2026-04-23 live session), yielding RR ~2.9x that collapses on the first adverse tick. When the closest draw fails a non-zero threshold the code must fall back to the next valid draw, and only skip (preserving divergence) if none qualify.

## Solution Statement

Refactor `process_scan_bar` to emit typed lifecycle events at every state transition instead of only at `signal` or `expired`. Compute the preliminary entry / stop / TP at divergence time (all inputs are available — anchor_close at `strategy_smt.py:1906–1911`, divergence_bar_high/low for stop at lines 1333–1337, DOL ranking for TP via `select_draw_on_liquidity`). Store the preliminary signal on `state.pending_limit_signal` so replacement and cancel paths can read + re-emit it. Thread the lifecycle events through `signal_smt.py` with dedicated formatters.

Drop `ENTRY_LIMIT_CLASSIFICATION_PTS` as a classifier input; instead tag `signal_type=ENTRY_LIMIT` whenever the signal path went through a limit stage (i.e. `LIMIT_ENTRY_BUFFER_PTS is not None`), else `ENTRY_MARKET`. The tag always rides on the signal dict built by `_build_signal_from_bar`.

For the fallback: extend `select_draw_on_liquidity` to return the full ordered list of valid draws; walk the list until `MIN_TARGET_PTS` / `MIN_RR_FOR_TARGET` pass. If none pass, return None → `_build_signal_from_bar` returns None → `process_scan_bar` returns None without resetting `scan_state` (already the case at `strategy_smt.py:1727–1728`; this plan adds the test that locks it in).

## Feature Metadata

**Feature Type**: Enhancement (lifecycle rework + behavior change)
**Complexity**: High — single-file state machine at `strategy_smt.py:process_scan_bar` is shared with backtest and must stay backtest-compatible
**Primary Systems Affected**: `strategy_smt.py` (state machine + DOL selector), `signal_smt.py` (emission / formatting), `backtest_smt.py` (regression-only — must ignore new events)
**Dependencies**: None new; reuses existing `anthropic` and `ib_insync` deps
**Breaking Changes**:
- Yes (signal output): the `SIGNAL` line's semantics change — no longer the *placement* event when `LIMIT_ENTRY_BUFFER_PTS is not None`; `LIMIT_PLACED` / `LIMIT_FILLED` now split the lifecycle.
- Yes (constants): `MIN_TARGET_PTS` / `MIN_RR_FOR_TARGET` defaults raised from 0.0 → 15.0 / 1.5.
- No (backtest behavior): new event returns are ignored by `backtest_smt.py` and do not alter trade records.
- No (tests): existing suites that rely on `HUMAN_EXECUTION_MODE=False` default stay green via monkeypatch.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `strategy_smt.py:192–193` — `MIN_RR_FOR_TARGET` / `MIN_TARGET_PTS` constants (item 8 target)
- `strategy_smt.py:332–356` — `LIMIT_ENTRY_BUFFER_PTS`, `LIMIT_EXPIRY_SECONDS`, `LIMIT_RATIO_THRESHOLD` (config surface for the limit lifecycle)
- `strategy_smt.py:366–383` — human-execution config block; append `MOVE_LIMIT_MIN_GAP_BARS` and any new lifecycle flags here
- `strategy_smt.py:443–499` — `ScanState` + `__slots__` — MUST add any new state fields to `__slots__` AND `reset()`
- `strategy_smt.py:1210–1238` — `select_draw_on_liquidity`: current nearest-valid algorithm to extend with full ordered-list return
- `strategy_smt.py:1286–1410` — `_build_signal_from_bar`: stop/entry/TP assembly, applies `TDO_VALIDITY_CHECK` / `MIN_STOP_POINTS` / `MIN_DISPLACEMENT_BODY_PTS` guards. Default `signal_type = "ENTRY_MARKET"` is set at line 1395.
- `strategy_smt.py:1436–1461` — `_build_draws_and_select`: wrapper that assembles draws dict and calls `select_draw_on_liquidity`. Central change point for item 8 fallback walk.
- `strategy_smt.py:1464–1584` — `process_scan_bar` header + `WAITING_FOR_LIMIT_FILL` block (fill + expire paths)
- `strategy_smt.py:1585–1785` — `WAITING_FOR_ENTRY` / `REENTRY_ELIGIBLE` block: replacement logic (lines 1598–1651), hypothesis invalidation (1654–1663), confirmation + build (1692–1784)
- `strategy_smt.py:1786–1940` — `IDLE` block: divergence detection, anchor assignment, state transition to `WAITING_FOR_ENTRY` at line 1923. This is where `LIMIT_PLACED` must fire.
- `signal_smt.py:22–35` — `strategy_smt` import + human-mode documentation comment
- `signal_smt.py:155–199` — `_format_signal_line`, `_format_exit_line`, `_format_stop_moved_line`: pattern to mirror for new event formatters
- `signal_smt.py:676–758` — stateful scanner invocation + signal consumption path (currently only handles `result["type"] == "signal"`)
- `signal_smt.py:706–723` — classifier to refactor (item 5): drop `HUMAN_EXECUTION_MODE` gate + `ENTRY_LIMIT_CLASSIFICATION_PTS` heuristic
- `signal_smt.py:899–918` — `main()` where `strategy_smt.HUMAN_EXECUTION_MODE = True` is set (prior session's change; do not duplicate)
- `backtest_smt.py:173, 268` — `HUMAN_EXECUTION_MODE` read sites (verify new events don't leak into TSV)
- `tests/test_smt_humanize.py` — existing pattern for monkeypatching strategy_smt flags + asserting signal_type
- `tests/test_signal_smt.py` — existing tests for `_format_signal_line` / scanner integration

### New Files to Create

- `tests/test_smt_limit_lifecycle.py` — unit + integration tests for the new event types

No new product modules — all changes are edits to existing files.

### Relevant Documentation — READ BEFORE IMPLEMENTING

- `signal_insights.md` (sections 2, 3, 4, 5, 8) — original design motivation + lifecycle semantics
- `.agents/plans/smt-humanize.md` — prior typed-signal plan; mirror its test organization + formatter naming
- `.agents/plans/smt-limit-entry-anchor-close.md` — prior plan that introduced `WAITING_FOR_LIMIT_FILL`; do NOT duplicate its fill-path logic
- `.agents/plans/smt-solution-f-draw-on-liquidity.md` — prior DOL ranking plan; extends here via the fallback walk

### Patterns to Follow

**Naming Conventions**:
- Event types returned from `process_scan_bar`: lowercase_snake with verb-first noun — existing: `"signal"`, `"expired"`. New: `"limit_placed"`, `"limit_moved"`, `"limit_cancelled"`, `"limit_filled"`, `"limit_expired"` (rename `"expired"` for consistency; provide alias if `backtest_smt.py` parses the string).
- `signal_type` string constants on the signal dict: UPPER_SNAKE — `"ENTRY_MARKET"`, `"ENTRY_LIMIT"`, `"MOVE_STOP"`, `"CLOSE_MARKET"` (existing). Do NOT add new `signal_type` values; the lifecycle events live on the event-type envelope, not the signal dict.
- Log line prefix for `signal_smt.py` formatter output: `[HH:MM:SS] <EVENT_LABEL> ...` — match `SIGNAL`, `EXIT`, `STOP_MOVE` width convention.

**Error Handling**: State-machine transitions must never raise on missing fields. If `state.pending_limit_signal` is `None` when a move/cancel is triggered, silently return without emit (defensive). Log an internal warning via `print()` guarded by `DEBUG` env check if we need visibility.

**Logging Pattern**: All user-visible events go through formatter functions in `signal_smt.py` that return a string, then `print(..., flush=True)`. No stderr. No logging framework.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (3 parallel)                                 │
├─────────────────────────────────────────────────────────────────┤
│ 1.1: Item 8 — Raise MIN_TARGET_PTS + fallback walk              │
│ 1.2: Item 5 — Drop ENTRY_LIMIT_CLASSIFICATION_PTS gate          │
│ 1.3: Add lifecycle config constants + ScanState slots           │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ WAVE 2: State Machine Core (sequential — single-function edit)  │
├─────────────────────────────────────────────────────────────────┤
│ 2.1: Emit limit_placed at IDLE→WAITING_FOR_ENTRY                │
│ 2.2: Emit limit_moved / limit_cancelled on replacement          │
│ 2.3: Split limit_filled out of signal emission                  │
│ 2.4: Emit limit_cancelled on hypothesis invalidation / IDLE     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ WAVE 3: signal_smt.py Integration (2 parallel)                  │
├─────────────────────────────────────────────────────────────────┤
│ 3.1: Formatters + event dispatch for all new types              │
│ 3.2: JSON payload schema for human-mode machine consumer        │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ WAVE 4: Testing & Regression (sequential)                       │
├─────────────────────────────────────────────────────────────────┤
│ 4.1: Unit + integration test suite in test_smt_limit_lifecycle  │
│ 4.2: Full-suite regression + backtest smoke                     │
└─────────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1, 1.2, 1.3 — touch disjoint surfaces (DOL selector / signal_smt classifier / config block + ScanState slots). No file-level write conflicts if executed in order: 1.3 writes strategy_smt.py config block + ScanState class; 1.1 writes `select_draw_on_liquidity` + defaults; 1.2 writes signal_smt.py classifier section.
**Wave 2 — Sequential**: All four subtasks modify `process_scan_bar` in `strategy_smt.py:1464–1940`. Splitting across agents would cause merge conflicts inside one function. Execute as one task with four distinct sub-steps.
**Wave 3 — Parallel**: 3.1 (formatters) and 3.2 (JSON schema) both write to `signal_smt.py` but to disjoint sections — 3.1 adds new `_format_*_line` functions after the existing ones at `signal_smt.py:155–199`; 3.2 writes the JSON payload emission block near `signal_smt.py:745–757`.
**Wave 4 — Sequential**: Tests must be written after implementation is complete.

Parallelizable tasks: 5 of 10 = **50%**.

### Interface Contracts

**Contract 1 (Wave 1 → Wave 2)**: `select_draw_on_liquidity` new signature must return `(primary_name, primary_price, secondary_name, secondary_price, valid_draws_list)` where `valid_draws_list = [(name, price, dist), ...]` sorted by distance. Wave 2 replacement logic uses this to recompute TP on `MOVE_LIMIT` without re-assembling the draws dict.

**Contract 2 (Wave 1.3 → Wave 2)**: `ScanState.__slots__` must include `last_limit_move_bar_idx` (int, init -999) so Wave 2 rate-limit check has state to read. `MOVE_LIMIT_MIN_GAP_BARS` constant must be defined with default 0 (no rate limit) so existing tests pass without opt-in.

**Contract 3 (Wave 2 → Wave 3)**: `process_scan_bar` may return ANY of the following shapes for the "type" key: `"signal"`, `"limit_placed"`, `"limit_moved"`, `"limit_cancelled"`, `"limit_expired"`, `"limit_filled"`. Wave 3 must dispatch on this key; `backtest_smt.py` must continue to handle only `"signal"` + `"limit_expired"` (rename of `"expired"`) and ignore the rest.

**Mock for parallel work**: Wave 3 can implement formatters against hand-written dict fixtures matching the Contract 3 shapes before Wave 2 is complete.

### Synchronization Checkpoints

**After Wave 1**: `uv run pytest tests/test_smt_backtest.py tests/test_smt_humanize.py tests/test_smt_signal_quality.py -q` — confirms non-zero `MIN_TARGET_PTS` does not regress existing suites (must monkeypatch to 0.0 where existing tests depend on crumb targets).

**After Wave 2**: `uv run pytest tests/test_smt_backtest.py -q` — backtest path still returns only `"signal"` / `"limit_expired"` trade outcomes; no new event leakage into trade records.

**After Wave 3**: `uv run python -c "import signal_smt"` + a synthetic-bar drive test in `test_smt_limit_lifecycle.py` that asserts the formatter output for each event type.

**After Wave 4**: `uv run pytest -q` full suite; `uv run python -m orchestrator.main --check`.

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation

Independent low-risk changes — config defaults, classifier de-gating, and DOL fallback walk.

### Phase 2: State Machine Core

The heart of the rework. Modifies `process_scan_bar` to emit typed lifecycle events at every state transition. Single function, four sub-steps, one agent.

### Phase 3: signal_smt.py Integration

Wire new event types through formatters and JSON payload emission.

### Phase 4: Testing & Validation

Dedicated test module + full-suite regression.

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation

#### Task 1.1: UPDATE `strategy_smt.py` — item 8 fallback walk + non-zero defaults

- **WAVE**: 1
- **AGENT_ROLE**: state-machine-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: 2.1, 2.2, 4.1
- **PROVIDES**: Extended `select_draw_on_liquidity` returning ordered `valid` list; new defaults `MIN_TARGET_PTS=15.0`, `MIN_RR_FOR_TARGET=1.5`
- **IMPLEMENT**:
  1. Edit `strategy_smt.py:192–193` — change defaults to `MIN_RR_FOR_TARGET: float = 1.5` and `MIN_TARGET_PTS: float = 15.0`. Keep the optimizer-range comments.
  2. Edit `select_draw_on_liquidity` at `strategy_smt.py:1210–1238`:
     - Change return signature from 4-tuple to 5-tuple: `(primary_name, primary_price, secondary_name, secondary_price, valid_draws)` where `valid_draws` is the full sorted list of `(name, price, dist)` tuples that passed the min-dist filter.
     - Return `(None, None, None, None, [])` on no valid draws.
     - Preserve all existing primary/secondary logic.
  3. Edit `_build_draws_and_select` at `strategy_smt.py:1436–1461` to propagate the new 5-tuple return.
  4. Edit caller at `strategy_smt.py:1722–1728`: unpack the new 5th element but do not use it yet (Wave 2 will). Keep the existing `if _day_tp is None: return None` guard — it already preserves state (does not reset `scan_state` to IDLE), which is the item-8 "preserve divergence" semantic.
  5. Audit ALL existing tests that set `MIN_TARGET_PTS`/`MIN_RR_FOR_TARGET` — if any implicitly depend on the 0.0 default, add explicit monkeypatches to preserve their intent. Grep: `rg 'MIN_TARGET_PTS|MIN_RR_FOR_TARGET' tests/`.
- **PATTERN**: `strategy_smt.py:1210–1238` (existing selector) + `smt-solution-f-draw-on-liquidity.md` plan for historical context
- **VALIDATE**: `uv run pytest tests/test_smt_backtest.py tests/test_smt_signal_quality.py -q` — 0 regressions, existing test coverage unchanged.
- **INTEGRATION_TEST**: `uv run pytest tests/ -q` — full suite still passes (may require additional monkeypatches).

#### Task 1.2: UPDATE `signal_smt.py` — item 5 drop ENTRY_LIMIT_CLASSIFICATION_PTS

- **WAVE**: 1
- **AGENT_ROLE**: signal-output-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: 3.1, 3.2, 4.1
- **PROVIDES**: Every `signal` result carries `signal_type` deterministically set from `LIMIT_ENTRY_BUFFER_PTS` presence (not price distance)
- **IMPLEMENT**:
  1. Edit `signal_smt.py:706–723` (the current classifier block):
     - Remove the `HUMAN_EXECUTION_MODE` gate (redundant — `main()` at `signal_smt.py:911–914` sets it True unconditionally).
     - Remove the `ENTRY_LIMIT_CLASSIFICATION_PTS` pts-distance comparison.
     - Replace with deterministic rule: if `signal.get("limit_fill_bars") is not None` OR `strategy_smt.LIMIT_ENTRY_BUFFER_PTS is not None`, set `signal["signal_type"] = "ENTRY_LIMIT"`; else `"ENTRY_MARKET"`. Decision: prefer the `limit_fill_bars` check (presence-based, populated only when the algo went through a limit stage — see `strategy_smt.py:1563, 1770`).
     - Keep the confidence filter at `signal_smt.py:709–717` intact.
  2. Mark `strategy_smt.ENTRY_LIMIT_CLASSIFICATION_PTS` as deprecated with a comment at its definition (`strategy_smt.py:378`); do NOT delete the constant yet — an unrelated optimizer may still read it.
  3. Update `_format_signal_line` at `signal_smt.py:155–169` to include the `signal_type` tag in the printed line: `... | type {signal['signal_type']} | RR ~...`. Place after the RR field.
- **PATTERN**: `signal_smt.py:155–199` (formatter layout + field-order convention)
- **VALIDATE**: `uv run pytest tests/test_smt_humanize.py -q` — classifier tests updated to new rule.
- **INTEGRATION_TEST**: Drive `signal_smt._format_signal_line` with a synthetic signal dict; assert `"type ENTRY_LIMIT"` appears when `limit_fill_bars` is set and `"type ENTRY_MARKET"` otherwise.

#### Task 1.3: UPDATE `strategy_smt.py` — lifecycle config constants + ScanState slots

- **WAVE**: 1
- **AGENT_ROLE**: config-schema-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: 2.1, 2.2, 2.3, 2.4
- **PROVIDES**: `MOVE_LIMIT_MIN_GAP_BARS` config; `ScanState.last_limit_move_bar_idx` + `last_limit_signal_snapshot` slots; centralized event-type string constants
- **IMPLEMENT**:
  1. Edit `strategy_smt.py:366–383` — add after `MOVE_STOP_MIN_GAP_BARS`:
     ```python
     # Minimum bar gap between consecutive MOVE_LIMIT signals; 0 = no rate limiting.
     # Distinct from MOVE_STOP_MIN_GAP_BARS — trader's tolerance for limit revisions
     # is usually lower than for stop revisions.
     MOVE_LIMIT_MIN_GAP_BARS: int = 0
     ```
  2. Edit `ScanState` at `strategy_smt.py:443–499`:
     - Add to `__slots__`: `"last_limit_move_bar_idx"`, `"last_limit_signal_snapshot"`.
     - In `reset()`: initialize `self.last_limit_move_bar_idx = -999` and `self.last_limit_signal_snapshot = None`.
     - Document in the docstring: `last_limit_signal_snapshot` is the previously-emitted `LIMIT_PLACED` or `MOVE_LIMIT` payload, kept so `MOVE_LIMIT` can include old + new prices, and so `CANCEL_LIMIT` can echo the last known entry_price even after the pending signal is cleared.
  3. Add a module-level constants block near the top of `strategy_smt.py` (before the IDLE/WAITING_FOR_* blocks, e.g. after line 340's `LIMIT_EXPIRY_SECONDS`):
     ```python
     # ── Lifecycle event types returned by process_scan_bar ────────────────────
     EVT_SIGNAL          = "signal"
     EVT_LIMIT_PLACED    = "limit_placed"
     EVT_LIMIT_MOVED     = "limit_moved"
     EVT_LIMIT_CANCELLED = "limit_cancelled"
     EVT_LIMIT_EXPIRED   = "limit_expired"
     EVT_LIMIT_FILLED    = "limit_filled"
     ```
  4. Keep the string literal `"expired"` as a backward-compat alias for `EVT_LIMIT_EXPIRED`. Callers (backtest_smt.py) must handle both for one release. Add `# TODO: deprecate "expired" alias after <date>` comment.
- **PATTERN**: Existing `__slots__` + `reset()` convention at `strategy_smt.py:450–499`; existing config block at `strategy_smt.py:366–383`.
- **VALIDATE**: `uv run python -c "from strategy_smt import ScanState, EVT_LIMIT_PLACED; s = ScanState(); print(s.last_limit_move_bar_idx, s.last_limit_signal_snapshot)"` prints `-999 None`.
- **INTEGRATION_TEST**: `uv run pytest tests/test_smt_backtest.py -q` — no regressions (new slots must not break existing state init).

**Wave 1 Checkpoint**: `uv run pytest tests/test_smt_backtest.py tests/test_smt_humanize.py tests/test_smt_signal_quality.py tests/test_signal_smt.py -q`

---

### WAVE 2: State Machine Core — process_scan_bar lifecycle events

#### Task 2.1: UPDATE `strategy_smt.py:process_scan_bar` — full lifecycle emission

- **WAVE**: 2
- **AGENT_ROLE**: state-machine-specialist
- **DEPENDS_ON**: [1.1, 1.3]
- **BLOCKS**: 3.1, 3.2, 4.1
- **PROVIDES**: `process_scan_bar` emits every lifecycle event; state machine produces `limit_placed` at divergence, `limit_moved` / `limit_cancelled` on replacement, `limit_filled` on fill (forward + same-bar modes), `limit_expired` on timeout.
- **USES_FROM_WAVE_1**: Task 1.1 provides 5-tuple return from `_build_draws_and_select`; Task 1.3 provides ScanState new slots + EVT_* constants.

**Sub-step 2.1.a — Emit `limit_placed` at divergence (item 2)**
- At `strategy_smt.py:1910–1923` (the IDLE → WAITING_FOR_ENTRY transition where `state.anchor_close = ac` is assigned), after the state is fully populated and before `return None`:
  - Only fire when `LIMIT_ENTRY_BUFFER_PTS is not None`. When `None` (MARKET mode), keep current behavior (no event on divergence; `signal` event at confirmation).
  - Build a preliminary signal dict using `_build_signal_from_bar` — but with a synthetic "confirmation" bar equal to the divergence bar itself, so that the entry_price uses `anchor_close ± LIMIT_ENTRY_BUFFER_PTS` branch at lines 1310–1314. **Caveat**: the current `_build_signal_from_bar` assumes it's called at confirmation; we need a lighter builder that computes entry / stop / tp without running the confirmation-side guards (ALWAYS_REQUIRE_CONFIRMATION, displacement re-adjustment).
  - Decision: Do NOT call `_build_signal_from_bar` at divergence. Instead, add a new module-private helper `_build_preliminary_limit_signal(state, context, bar_idx, bar, ...)` that computes `entry_price` (anchor ± buffer), `stop_price` (divergence_bar_high/low + STRUCTURAL_STOP_BUFFER_PTS or ratio fallback), calls `_build_draws_and_select` for TP, and returns a partial signal dict. If TP selection fails, do NOT emit — stay in WAITING_FOR_ENTRY with no event (same divergence-preservation semantics as item 8).
  - Attach `signal_type = "ENTRY_LIMIT"` on the preliminary signal.
  - Store the preliminary dict on `state.pending_limit_signal` AND `state.last_limit_signal_snapshot`.
  - Return `{"type": EVT_LIMIT_PLACED, "signal": preliminary_dict}`.
- Update the `IDLE` block's return to carry through the new shape — caller (backtest_smt.py) must tolerate the new envelope.

**Sub-step 2.1.b — Emit `limit_moved` / `limit_cancelled` on replacement (item 4)**
- At `strategy_smt.py:1598–1651` (the replacement logic within WAITING_FOR_ENTRY), after state fields are updated on a successful replacement:
  - Recompute the preliminary signal using `_build_preliminary_limit_signal` with the new anchor. Do not emit if TP selection fails (preserve divergence).
  - Determine event type:
    - If the new `pending_direction` equals the pre-replacement direction and `state.last_limit_signal_snapshot is not None` → `EVT_LIMIT_MOVED` with old + new entry/stop/tp on the payload.
    - If the new `pending_direction` differs from the pre-replacement direction → emit `EVT_LIMIT_CANCELLED` for the old signal, then `EVT_LIMIT_PLACED` for the new. Since `process_scan_bar` returns a single event per call, enqueue both in a single shape: `{"type": "lifecycle_batch", "events": [cancelled_dict, placed_dict]}`. Caller dispatches each.
  - Rate limit: if `bar_idx - state.last_limit_move_bar_idx < MOVE_LIMIT_MIN_GAP_BARS`, suppress emission but still apply the state-level replacement (do NOT swallow a legitimate state change — just don't emit the event).
  - Update `state.last_limit_move_bar_idx = bar_idx` on emission.
  - Update `state.last_limit_signal_snapshot = new_preliminary_dict` on emission.
- **Decision**: Do we need to emit `MOVE_LIMIT` if the new entry_price equals the old entry_price (anchor shifted but buffered price rounded the same)? NO — suppress emission if `new_entry_price == old_entry_price AND new_stop_price == old_stop_price AND new_tp == old_tp`. Still update state for consistency.

**Sub-step 2.1.c — Emit `limit_filled` on fill (item 3)**
- At `strategy_smt.py:1562–1570` (WAITING_FOR_LIMIT_FILL → fill path):
  - Before returning the current `signal` envelope, first return `EVT_LIMIT_FILLED` with payload: `{direction, filled_price, original_limit_price, time_in_queue_secs, divergence_bar_time}`. Filled_price = `pls["entry_price"]` (same as original unless we want to add slippage — keep as-is for parity). `time_in_queue_secs = state.limit_bars_elapsed * context.bar_seconds`.
  - Decision: emit BOTH `limit_filled` AND `signal` — pack into `{"type": "lifecycle_batch", "events": [filled_dict, signal_dict]}` so caller dispatches both in order.
  - Clear `state.last_limit_signal_snapshot = None`.
- At `strategy_smt.py:1770–1784` (WAITING_FOR_ENTRY confirmation → same-bar `signal` path when `LIMIT_EXPIRY_SECONDS is None`):
  - If `LIMIT_ENTRY_BUFFER_PTS is not None` (limit mode), wrap the return in a `lifecycle_batch`: `[EVT_LIMIT_FILLED, EVT_SIGNAL]`.
  - If `LIMIT_ENTRY_BUFFER_PTS is None` (pure MARKET mode), return the current `signal` envelope unchanged.

**Sub-step 2.1.d — Emit `limit_cancelled` on hypothesis invalidation / IDLE fallthrough (item 4 adjacent)**
- At `strategy_smt.py:1654–1663` (hypothesis invalidation branch within WAITING_FOR_ENTRY):
  - Before setting `scan_state = "IDLE"`, if `state.last_limit_signal_snapshot is not None`, build a cancel payload from the snapshot and return `{"type": EVT_LIMIT_CANCELLED, "signal": snapshot, "reason": "hypothesis_invalidated"}`.
  - Clear `state.last_limit_signal_snapshot = None`.
- At `strategy_smt.py:1572–1581` (WAITING_FOR_LIMIT_FILL → expired):
  - Change return from `"expired"` to `EVT_LIMIT_EXPIRED` and add `"reason": "timeout"` on the payload. Keep `"expired"` as a string literal alias via `EVT_LIMIT_EXPIRED = "limit_expired"` already set in Task 1.3.
- At `strategy_smt.py:1587–1593` (REENTRY_ELIGIBLE → IDLE on MAX_REENTRY_COUNT) and `strategy_smt.py:1740–1744` (HYPOTHESIS_FILTER mismatch → IDLE):
  - Same pattern: if `state.last_limit_signal_snapshot is not None`, emit `EVT_LIMIT_CANCELLED` with appropriate `reason` field before resetting state.

- **IMPLEMENT summary**: all four sub-steps land in a single `process_scan_bar` edit. Every state transition that clears `pending_limit_signal` or changes `scan_state` while a limit is resting must emit `EVT_LIMIT_CANCELLED` first.
- **PATTERN**: existing `{"type": "signal", **signal_out}` envelope at `strategy_smt.py:1570`, `1784`; existing `{"type": "expired", ...}` at `strategy_smt.py:1572–1581`.
- **VALIDATE**: `uv run pytest tests/test_smt_backtest.py -q` — backtest path still returns trade records correctly (asserts the backward-compat alias for `"expired"`).
- **INTEGRATION_TEST**: Write a synthetic-bar test (in Wave 4) that drives `process_scan_bar` bar-by-bar and asserts the full event sequence for a happy-path limit fill.

**Wave 2 Checkpoint**: `uv run pytest tests/test_smt_backtest.py -q` — backtest regression-free; no new event leakage into `trades.tsv`.

---

### WAVE 3: signal_smt.py Integration (2 parallel)

#### Task 3.1: UPDATE `signal_smt.py` — formatters + event dispatch

- **WAVE**: 3
- **AGENT_ROLE**: signal-output-specialist
- **DEPENDS_ON**: [1.2, 2.1]
- **BLOCKS**: 4.1
- **PROVIDES**: Every new event type has a human-readable log line; `signal_smt.py` main scanner callback dispatches on `result["type"]`.
- **IMPLEMENT**:
  1. Add formatters after `signal_smt.py:199` (`_format_stop_moved_line`):
     - `_format_limit_placed_line(ts, signal)`: `[HH:MM:SS] LIMIT_PLACED {direction:<5} | entry {entry_price:.2f} | stop {stop_price:.2f} | TP {take_profit:.2f} | RR ~{rr:.1f}x`
     - `_format_limit_moved_line(ts, old, new)`: `[HH:MM:SS] LIMIT_MOVED  {direction:<5} | entry {old_entry:.2f} -> {new_entry:.2f} | stop {old_stop:.2f} -> {new_stop:.2f} | TP {old_tp:.2f} -> {new_tp:.2f}`
     - `_format_limit_cancelled_line(ts, signal, reason)`: `[HH:MM:SS] LIMIT_CANCELLED {direction:<5} | entry {entry:.2f} | reason {reason}`
     - `_format_limit_expired_line(ts, signal, missed_move)`: `[HH:MM:SS] LIMIT_EXPIRED {direction:<5} | entry {entry:.2f} | missed_move {missed_move:.1f} pts`
     - `_format_limit_filled_line(ts, filled)`: `[HH:MM:SS] LIMIT_FILLED {direction:<5} | filled {filled_price:.2f} | orig {original_limit_price:.2f} | queue_s {time_in_queue_secs:.0f}`
  2. At `signal_smt.py:684–686` (the current `if result is None or result["type"] != "signal": return`), replace with a dispatch:
     ```python
     if result is None:
         return
     evt_type = result["type"]
     if evt_type == "lifecycle_batch":
         for sub in result["events"]:
             _dispatch_event(bar_ts, sub)
         return
     _dispatch_event(bar_ts, result)
     if evt_type != "signal":
         return
     signal = result  # existing downstream code requires this binding
     ```
  3. Add `_dispatch_event(bar_ts, evt)` helper that maps event types to formatters and prints to stdout, then writes the JSON payload via Task 3.2's schema.
  4. Keep the full existing signal-handling pipeline (guards at 689–705, classifier at 706–723 per Task 1.2, position open at 725–758) gated on `evt_type == "signal"` — only a `signal` or a `lifecycle_batch` containing a `signal` triggers a position open.
  5. Reset-on-cancel: when `evt_type == "limit_cancelled"`, the trader's resting order is stale. No position state change (we never opened). Just emit the line + JSON.
- **PATTERN**: `signal_smt.py:742–757` (current `print(_format_signal_line(...))` + JSON payload pattern).
- **VALIDATE**: Drive `_dispatch_event` with synthetic event dicts for each type; assert each formatter emits the expected string (Wave 4).
- **INTEGRATION_TEST**: Covered by Wave 4 end-to-end lifecycle test.

#### Task 3.2: UPDATE `signal_smt.py` — JSON payload schema per event

- **WAVE**: 3
- **AGENT_ROLE**: signal-output-specialist
- **DEPENDS_ON**: [1.2, 2.1]
- **BLOCKS**: 4.1
- **PROVIDES**: Machine-readable JSON payload for each event type, emitted alongside the human-readable line.
- **IMPLEMENT**:
  1. Define the JSON shape per event type (include in `_dispatch_event`):
     ```python
     # LIMIT_PLACED
     {"signal_type": "LIMIT_PLACED", "direction", "entry_price", "stop_price",
      "take_profit", "tp_name", "confidence", "divergence_bar_time"}
     # MOVE_LIMIT
     {"signal_type": "MOVE_LIMIT", "direction", "old_entry_price", "new_entry_price",
      "old_stop_price", "new_stop_price", "old_take_profit", "new_take_profit",
      "reason": "score_replacement" | "anchor_shift"}
     # CANCEL_LIMIT
     {"signal_type": "CANCEL_LIMIT", "direction", "entry_price", "reason"}
     # LIMIT_EXPIRED
     {"signal_type": "LIMIT_EXPIRED", "direction", "entry_price", "missed_move_pts"}
     # LIMIT_FILLED
     {"signal_type": "LIMIT_FILLED", "direction", "filled_price",
      "original_limit_price", "time_in_queue_secs"}
     # ENTRY (existing)
     {"signal_type": "ENTRY_MARKET" | "ENTRY_LIMIT", ...existing fields}
     ```
  2. All floats rounded to 4 decimals (match `signal_smt.py:749–751` convention).
  3. All payloads go through a single `print(json.dumps(payload), flush=True)` in `_dispatch_event`.
  4. Do NOT merge the new payload format with the existing `signal_smt.py:745–757` block — replace it. The new `_dispatch_event` owns all JSON emission.
- **PATTERN**: `signal_smt.py:745–757` (existing JSON payload for `ENTRY_MARKET`/`ENTRY_LIMIT`).
- **VALIDATE**: Wave 4 unit test parses each JSON line and asserts field presence + type.
- **INTEGRATION_TEST**: Covered by Wave 4 lifecycle test.

**Wave 3 Checkpoint**: `uv run python -c "import signal_smt; print('ok')"` — no import-time errors.

---

### WAVE 4: Testing & Regression (sequential)

#### Task 4.1: CREATE `tests/test_smt_limit_lifecycle.py` — unit + integration

- **WAVE**: 4
- **AGENT_ROLE**: test-specialist
- **DEPENDS_ON**: [2.1, 3.1, 3.2]
- **BLOCKS**: 4.2
- **PROVIDES**: Complete test coverage for every new lifecycle path.
- **IMPLEMENT** — tests to write (synthetic bars, monkeypatched config, no IB):

**Item 2 (LIMIT_PLACED at divergence)**:
  - `test_limit_placed_on_divergence_idle_transition` — drives `process_scan_bar` through IDLE → divergence bar; asserts return is `{"type": "limit_placed", "signal": {...}}`.
  - `test_limit_placed_entry_price_is_anchor_plus_buffer_long` — assert `signal["entry_price"] == anchor_close + LIMIT_ENTRY_BUFFER_PTS`.
  - `test_limit_placed_entry_price_is_anchor_minus_buffer_short` — symmetric.
  - `test_limit_placed_payload_includes_entry_stop_tp` — asserts all three fields present + numeric.
  - `test_limit_placed_suppressed_when_tp_selection_fails` — monkeypatch `MIN_TARGET_PTS=99999`; assert return is `None` (no emission) and `state.scan_state == "WAITING_FOR_ENTRY"` (divergence preserved).
  - `test_market_mode_no_limit_placed_on_divergence` — `LIMIT_ENTRY_BUFFER_PTS=None`; no event at divergence; `signal` event at confirmation.

**Item 3 (LIMIT_FILLED)**:
  - `test_limit_filled_emitted_on_forward_fill` — forward mode (`LIMIT_EXPIRY_SECONDS=120`); drive bar sequence through fill; assert `lifecycle_batch` containing `[limit_filled, signal]`.
  - `test_limit_filled_same_bar_when_expiry_none` — `LIMIT_EXPIRY_SECONDS=None`; confirmation bar emits both `limit_filled` + `signal`.
  - `test_limit_filled_payload_time_in_queue_secs` — assert `time_in_queue_secs == limit_bars_elapsed * context.bar_seconds`.
  - `test_limit_filled_original_limit_price_equals_entry_price` — basic sanity.

**Item 4 (MOVE_LIMIT / CANCEL_LIMIT)**:
  - `test_move_limit_same_direction_higher_score_replacement` — drive divergence, then a same-direction higher-score divergence; assert `limit_moved` with old + new entry prices.
  - `test_cancel_and_place_on_opposite_direction_replacement` — opposite-direction replacement; assert `lifecycle_batch` = `[limit_cancelled, limit_placed]`.
  - `test_move_limit_rate_limited_by_min_gap_bars` — `MOVE_LIMIT_MIN_GAP_BARS=5`; drive two replacements within 3 bars; assert second emission is suppressed but state is still updated.
  - `test_move_limit_not_emitted_when_entry_unchanged` — anchor shifts but rounded entry matches; assert no `limit_moved` event.
  - `test_cancel_on_hypothesis_invalidation` — drive a limit_placed, then adverse move past HYPOTHESIS_INVALIDATION_PTS; assert `limit_cancelled` with `reason="hypothesis_invalidated"`.
  - `test_cancel_on_hypothesis_filter_miss` — `HYPOTHESIS_FILTER=True` with a non-matching hypothesis direction; limit_placed fires, then confirmation arrives with mismatch → `limit_cancelled` with `reason="hypothesis_filter_miss"`; state → IDLE.
  - `test_cancel_on_max_reentry_fallthrough` — limit_placed, then enter REENTRY_ELIGIBLE with `reentry_count >= MAX_REENTRY_COUNT` → `limit_cancelled` with `reason="max_reentry"`; state → IDLE.
  - `test_limit_expired_emitted_on_timeout` — forward mode; drive bars past `LIMIT_EXPIRY_SECONDS`; assert `limit_expired` with `reason="timeout"` and `missed_move_pts` populated.

**Item 5 (signal_type tag)**:
  - `test_signal_tagged_entry_market_when_buffer_none` — `LIMIT_ENTRY_BUFFER_PTS=None`; confirmation `signal` has `signal_type == "ENTRY_MARKET"`.
  - `test_signal_tagged_entry_limit_when_forward_fill` — forward mode fill; `signal` has `signal_type == "ENTRY_LIMIT"`.
  - `test_signal_tagged_entry_limit_when_same_bar_fill` — same-bar limit mode; `signal` has `signal_type == "ENTRY_LIMIT"`.
  - `test_format_signal_line_includes_type_tag` — `_format_signal_line` output contains `"type ENTRY_MARKET"` or `"type ENTRY_LIMIT"`.
  - `test_classifier_ignores_pts_distance` — set current_price equal to entry_price; assert `signal_type == "ENTRY_LIMIT"` in limit mode regardless of distance.

**Item 8 (MIN_TARGET_PTS fallback)**:
  - `test_tp_below_min_target_pts_uses_next_valid_draw` — construct draws dict where nearest is below threshold; assert picked TP is next-nearest-valid.
  - `test_no_valid_draw_preserves_divergence_and_state` — all draws below threshold; `process_scan_bar` returns None but `state.scan_state == "WAITING_FOR_ENTRY"` and `state.divergence_bar_idx` unchanged.
  - `test_later_confirmation_with_valid_draw_fires_signal` — first confirmation has no valid draw; next confirmation (with different draws) fires signal successfully.
  - `test_default_min_target_pts_is_15` and `test_default_min_rr_is_1p5` — module-level defaults.
  - `test_select_draw_returns_valid_list_sorted_by_distance` — covers the new 5th tuple element.

**Integration (full lifecycle)**:
  - `test_full_lifecycle_idle_to_limit_filled_to_exit` — happy path: divergence → `limit_placed` → confirmation → `limit_filled` → `signal` → `exit_stop`.
  - `test_full_lifecycle_cancel_on_opposite_anchor` — divergence long → `limit_placed(long)` → opposite-direction divergence → `limit_cancelled(long)` + `limit_placed(short)` → confirmation → `limit_filled(short)` + `signal(short)`.
  - `test_full_lifecycle_expired_without_fill` — divergence → `limit_placed` → no confirmation for 120s → `limit_expired`.
  - `test_backtest_unaffected_by_new_events` — run a known-good backtest scenario through `backtest_smt`; assert trade record count + P&L unchanged.

**signal_smt.py formatter + dispatcher tests**:
  - `test_dispatch_limit_placed_prints_formatted_line` — patch `print`; assert expected string.
  - `test_dispatch_lifecycle_batch_emits_all_sub_events` — two-event batch; assert both are printed in order.
  - `test_dispatch_lifecycle_batch_single_event` — single-element batch; ensure dispatcher handles without error (defensive).
  - `test_dispatch_unknown_event_type_is_safe` — unknown `type` key; dispatcher logs a warning and does not crash.
  - `test_json_payload_limit_placed_schema` — parse emitted JSON; assert required keys + types.
  - `test_json_payload_move_limit_schema` — same.
  - `test_json_payload_limit_filled_schema` — same.
  - `test_json_payload_cancel_limit_schema` — same.
  - `test_json_payload_limit_expired_schema` — same.

- **PATTERN**: `tests/test_smt_humanize.py` (monkeypatch-based, synthetic bars, one file per lifecycle change).
- **VALIDATE**: `uv run pytest tests/test_smt_limit_lifecycle.py -v` — all tests pass.

#### Task 4.2: Full-suite regression + backtest smoke

- **WAVE**: 4
- **AGENT_ROLE**: qa-specialist
- **DEPENDS_ON**: [4.1]
- **PROVIDES**: Confirmed no regressions.
- **IMPLEMENT**:
  1. `uv run pytest -q` — full suite passes (or pre-existing failures unchanged).
  2. `uv run python -m orchestrator.main --check` — setup check still passes.
  3. `uv run python backtest_smt.py` — 6-fold walk-forward; compare mean_test_pnl against baseline in `.agents/experiment-log.md`. A ±10% swing on mean_test_pnl from the non-zero `MIN_TARGET_PTS` default is expected and acceptable (documented behavior change); flag >10% swings for review.
  4. `uv run python -c "from hypothesis_smt import _extract_response_text"` — no import regression from unrelated prior change.
- **VALIDATE**: Full-suite pytest green.

**Final Checkpoint**: `uv run pytest -q && uv run python -m orchestrator.main --check`

---

## TESTING STRATEGY

**⚠️ ALL tests that can be automated MUST be automated.**

Manual testing is only acceptable when automation is physically impossible.

| What you're testing | Tool |
|---|---|
| State machine transitions | `pytest` (unit, `tests/test_smt_limit_lifecycle.py`) |
| `signal_smt.py` event dispatch + formatters | `pytest` (patch `print`) |
| JSON payload schemas | `pytest` (parse emitted JSON) |
| Backtest regression | `pytest tests/test_smt_backtest.py` + `python backtest_smt.py` |
| Full orchestrator startup | `python -m orchestrator.main --check` |

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_limit_lifecycle.py` | **Run**: `uv run pytest tests/test_smt_limit_lifecycle.py -v`

Covers every state-machine transition, every formatter, every JSON schema. Uses synthetic bars and monkeypatched config — no IB connection required.

### Integration Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_limit_lifecycle.py` (dedicated `class TestFullLifecycle`) | **Run**: `uv run pytest tests/test_smt_limit_lifecycle.py::TestFullLifecycle -v`

End-to-end lifecycle drives: happy-path fill; cancel-and-replace on opposite anchor; timeout expiry; no-valid-draw preservation.

### End-to-End Tests

**Status**: ✅ Automated (live IB session replay via the orchestrator is out-of-scope for this plan; the orchestrator-integration test covers wiring)
**Tool**: pytest | **Location**: `tests/test_orchestrator_integration.py` | **Run**: `uv run pytest tests/test_orchestrator_integration.py -v`

Existing orchestrator tests run `signal_smt.py` as a subprocess with a mocked IB. If those still pass after this plan, lifecycle events reach the orchestrator log correctly.

### Third-Party Service Validation

N/A — no external APIs in this plan.

### Manual Tests

Recommended one-off manual smoke (not a gate):
- During the next live session at 09:00 ET, observe the orchestrator log for one full `LIMIT_PLACED → LIMIT_FILLED → EXIT` sequence on a real divergence. Expected: trader sees `LIMIT_PLACED` emitted ≤ 1 second after the divergence bar closes, with enough lead time to place the resting order manually.

### Edge Cases

- **No valid draw on confirmation bar**: `MIN_TARGET_PTS` filters all draws → `_day_tp is None` → return None without resetting state. ✅ `test_no_valid_draw_preserves_divergence_and_state`.
- **Replacement while in WAITING_FOR_LIMIT_FILL**: today the state machine only runs replacement within WAITING_FOR_ENTRY. Do we allow replacement during WAITING_FOR_LIMIT_FILL? Decision: NO — current behavior is already "once the limit is queued, wait for fill or expiry". Document in plan; no new test needed.
- **Rate-limited MOVE_LIMIT still updates state**: ✅ `test_move_limit_rate_limited_by_min_gap_bars`.
- **lifecycle_batch with single event**: ensure the dispatcher handles `lifecycle_batch` with a 1-element list correctly (defensive). ✅ `test_dispatch_lifecycle_batch_single_event` (add to plan).
- **Same-bar limit fill**: `LIMIT_EXPIRY_SECONDS=None`; `limit_filled` and `signal` fire on the same bar. ✅ `test_limit_filled_same_bar_when_expiry_none`.
- **Backtest receives new event types**: `backtest_smt.py` must handle `lifecycle_batch` and ignore limit-lifecycle events. ✅ `test_backtest_unaffected_by_new_events`.

### Script Runnability Criteria (signal_smt.py)

`signal_smt.py` is a runnable entry-point script. Plan includes criteria beyond unit-test scenario coverage to confirm the script itself still runs:

- [ ] `uv run python -c "import signal_smt"` completes without raising (import-time setup integrity).
- [ ] `uv run python -m orchestrator.main --check` exits 0 (setup integrity as exercised by the orchestrator — the real runtime consumer).
- [ ] All new formatter outputs use ASCII-safe characters only (no Unicode arrows / em-dashes) OR `sys.stdout.reconfigure(encoding="utf-8")` is already in place. `signal_smt.py` runs as a subprocess under the orchestrator whose stdout encoding follows the Windows console default (CP-1252) unless overridden — the 2026-04-23 session already hit `UnicodeDecodeError: 'utf-8' codec can't decode byte 0x97` on an em-dash, so new formatters MUST use plain ASCII (use `->` not `→`, `--` not `—`).
- [ ] `signal_smt.py` does NOT spawn `claude` as a subprocess — N/A for the env-isolation criterion.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest — state machine transitions) | 26 | 60% |
| ✅ Backend (pytest — signal_smt dispatcher/formatters) | 12 | 28% |
| ✅ Backend (pytest — regression) | 4 | 9% |
| ✅ Integration (pytest — full lifecycle) | 4 | 9% |
| ✅ Script runnability (uv run commands) | 2 | 5% |
| ✅ Orchestrator integration | existing suite re-run | - |
| ⚠️ Manual | 0 | 0% |
| **Total** | ~46 automated | 100% |

**Goal**: 100% path coverage via automated tests. No manual tests required.

**Execution agent**: CREATE `tests/test_smt_limit_lifecycle.py` with every test listed in Task 4.1. RUN after each wave checkpoint AND as final Task 4.2 gate.

---

## VALIDATION COMMANDS

### Level 0: External Service Validation

N/A — no external APIs introduced.

### Level 1: Syntax & Style

```bash
cd C:/Users/gilad/projects/auto-co-trader/signalling
uv run python -c "import strategy_smt; import signal_smt; print('ok')"
uv run ruff check strategy_smt.py signal_smt.py tests/test_smt_limit_lifecycle.py 2>&1 || true
```

(Ruff may not be wired — if so, skip that line and rely on py_compile via import.)

### Level 2: Unit Tests

```bash
uv run pytest tests/test_smt_limit_lifecycle.py -v
uv run pytest tests/test_smt_humanize.py tests/test_signal_smt.py -q   # regression
```

### Level 3: Integration Tests

```bash
uv run pytest tests/test_smt_limit_lifecycle.py::TestFullLifecycle -v
uv run pytest tests/test_orchestrator_integration.py -q
uv run pytest tests/test_smt_backtest.py -q
```

### Level 4: E2E / Manual Validation

```bash
uv run pytest -q                         # full suite
uv run python -m orchestrator.main --check
uv run python backtest_smt.py            # 6-fold walk-forward smoke
```

Compare `mean_test_pnl` against baseline in `.agents/experiment-log.md`. A ±10% swing from non-zero `MIN_TARGET_PTS` is acceptable; larger swings require investigation.

---

## ACCEPTANCE CRITERIA

### Functional

- [ ] `LIMIT_PLACED` is emitted at IDLE → WAITING_FOR_ENTRY when `LIMIT_ENTRY_BUFFER_PTS is not None`; not emitted in MARKET mode
- [ ] `MOVE_LIMIT` fires on same-direction replacement; suppressed when entry/stop/TP all unchanged
- [ ] Opposite-direction replacement emits `CANCEL_LIMIT` + new `LIMIT_PLACED` (via `lifecycle_batch`)
- [ ] `CANCEL_LIMIT` fires on hypothesis invalidation, HYPOTHESIS_FILTER miss, and MAX_REENTRY fallthrough — each with a distinct `reason`
- [ ] `LIMIT_FILLED` is emitted before `SIGNAL` on both forward-fill and same-bar-fill paths
- [ ] `LIMIT_EXPIRED` fires on timeout with `reason="timeout"` and populated `missed_move_pts`; backward-compat alias for `"expired"` preserved
- [ ] Every `SIGNAL` has `signal_type ∈ {"ENTRY_MARKET", "ENTRY_LIMIT"}`, derived from whether the scan path went through a limit stage (not from pts distance)
- [ ] `ENTRY_LIMIT_CLASSIFICATION_PTS` no longer affects the classifier (kept as a deprecated constant for optimizer compatibility)
- [ ] `MIN_TARGET_PTS = 15.0` and `MIN_RR_FOR_TARGET = 1.5` as new module defaults
- [ ] `select_draw_on_liquidity` returns 5-tuple including full ordered `valid_draws` list
- [ ] When no valid draw passes minima, `_build_signal_from_bar` returns None and `state.scan_state` stays `WAITING_FOR_ENTRY` — next confirmation off the same divergence can still fire
- [ ] `MOVE_LIMIT_MIN_GAP_BARS` config added with default 0
- [ ] `ScanState.__slots__` adds `last_limit_move_bar_idx` and `last_limit_signal_snapshot`; `reset()` initializes both
- [ ] `_format_signal_line` output includes `type {signal_type}` field

### Error Handling

- [ ] Rate-limited `MOVE_LIMIT` (within `MOVE_LIMIT_MIN_GAP_BARS`) suppresses emission but still applies the state change
- [ ] `_dispatch_event` on unknown event type logs a warning and does not crash
- [ ] `lifecycle_batch` with a single element dispatches that element without error

### Integration / E2E

- [ ] `backtest_smt.py` produces identical `trades.tsv` on a known-good scenario (±0 trade count, matched config — new events are no-ops for backtest)
- [ ] `signal_smt.py` main scanner callback dispatches on `result["type"]` with `lifecycle_batch` support
- [ ] All new event types have dedicated formatter + JSON payload with documented schema

### Validation

- [ ] `tests/test_smt_limit_lifecycle.py` passes — verified by: `uv run pytest tests/test_smt_limit_lifecycle.py -v`
- [ ] No regression in `test_smt_humanize.py` / `test_signal_smt.py` / `test_smt_backtest.py` — verified by: `uv run pytest tests/test_smt_humanize.py tests/test_signal_smt.py tests/test_smt_backtest.py -q`
- [ ] Full suite green (or pre-existing failures unchanged) — verified by: `uv run pytest -q`
- [ ] Script imports cleanly — verified by: `uv run python -c "import signal_smt"`
- [ ] Orchestrator setup passes — verified by: `uv run python -m orchestrator.main --check`
- [ ] Backtest smoke: `uv run python backtest_smt.py` completes; mean_test_pnl swing ≤ 10% vs baseline in `.agents/experiment-log.md` (flag larger for review)

### Non-Functional

- [ ] All new formatter output uses ASCII-safe characters only (no Unicode arrows / em-dashes — 2026-04-23 session hit `UnicodeDecodeError` on `—`)
- [ ] No new external dependencies
- [ ] No debug prints left in production paths (per-plan execution rules)

### Out of Scope

- Item 6 (TDO midnight vs 09:00) — already implemented in a prior session
- Item 7 (DOL ranking by strategy priority) — separate session; item 8 fallback walks the current distance-based ordering transparently
- Replacement while in `WAITING_FOR_LIMIT_FILL` — current "queue and wait" behavior preserved
- Deletion of `ENTRY_LIMIT_CLASSIFICATION_PTS` constant — only un-wired from the classifier, retained for optimizer compatibility

---

## COMPLETION CHECKLIST

- [ ] All Wave 1 tasks completed; Wave 1 checkpoint passes
- [ ] Wave 2 (single combined `process_scan_bar` edit) completed; Wave 2 checkpoint passes
- [ ] Wave 3 formatters + JSON schema implemented; Wave 3 checkpoint passes
- [ ] Wave 4 tests written and passing; full suite green
- [ ] All validation levels (1–4) executed
- [ ] All automated tests created and passing
- [ ] No new manual tests required (documented N/A)
- [ ] `signal_smt.py` main loop dispatches lifecycle events correctly — verified via orchestrator integration test
- [ ] `backtest_smt.py` regression smoke passes
- [ ] Acceptance criteria all checked
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Design decisions locked in by this plan

1. **`_build_preliminary_limit_signal` is a new private helper, not an overload of `_build_signal_from_bar`.** The confirmation-side builder runs guards (ALWAYS_REQUIRE_CONFIRMATION, displacement re-adjustment) that don't apply at divergence. A dedicated helper keeps both paths clean. Placement-time stop and TP may be revised at confirmation; when revised, we emit `MOVE_LIMIT`.

2. **`lifecycle_batch` as a compound event envelope.** When a single bar produces two events (cancel + place on opposite-direction replacement, or filled + signal on fill), we wrap them in `{"type": "lifecycle_batch", "events": [...]}` rather than making two separate `process_scan_bar` returns. Keeps the call contract at "one call per bar" intact.

3. **`signal_type` is derived from scan-path presence of a limit stage, not from pts distance.** If the signal has `limit_fill_bars` populated (0 for same-bar, >0 for forward) → `ENTRY_LIMIT`. Else `ENTRY_MARKET`.

4. **Rate limit on `MOVE_LIMIT` suppresses emission but not state change.** The state machine's replacement-driven field updates still apply; only the human-facing event is suppressed. This prevents a choppy market from spamming the trader while keeping internal state consistent.

5. **`MIN_TARGET_PTS = 15.0` by default.** Matches the optimizer-hint comment at the constant. Live session evidence (2026-04-23) showed 11–12 pt TPs with RR ~2.9x collapse on first adverse tick. The 15 pts floor is not optimal — it's a reasonable floor that the optimizer can re-tune. The test suite must not depend on 0.0 default implicitly; add explicit monkeypatches where needed.

6. **Replacement during `WAITING_FOR_LIMIT_FILL` is NOT added in this plan.** Today, once the state machine has queued a limit, it waits for fill-or-expire without re-evaluating divergence. This plan preserves that. If in future we want "anchor moves while limit is resting" behavior, it's a separate plan.

7. **`backtest_smt.py` treats new lifecycle events as no-ops.** The backtest only cares about `signal` (trade open) and `limit_expired` (missed-move diagnostic). New events are dispatched in a `lifecycle_batch` envelope that backtest walks but skips for non-signal types. Trade records are unchanged.

### Trade-offs

- **Extra bar-close emissions.** Every divergence now produces an event even if no trade eventuates. Choppy days may produce many `LIMIT_PLACED` + `LIMIT_CANCELLED` pairs. Mitigation: `MOVE_LIMIT_MIN_GAP_BARS` for revisions; log-level filter in the orchestrator if volume becomes a problem.
- **State surface grows by 2 slots** (`last_limit_move_bar_idx`, `last_limit_signal_snapshot`). Acceptable.
- **Defaults change breaks baseline P&L.** Non-zero `MIN_TARGET_PTS` will shift optimizer baselines. Document in `.agents/experiment-log.md` on merge. Optimizer agents can still search the full range including 0.0.

### Out of scope

- Item 6 (TDO 00:00 vs 09:00): user confirmed already implemented in a prior session.
- Item 7 (DOL ranking by strategy priority): user confirmed will be done in a separate session. When item 7 lands, `select_draw_on_liquidity` will re-rank `valid_draws` by strategy priority instead of distance; item 8's fallback walk will pick up the new ordering transparently.
- Replacement during `WAITING_FOR_LIMIT_FILL`.
- Deprecation removal of `ENTRY_LIMIT_CLASSIFICATION_PTS` — this plan only un-wires it from the classifier.

### Execution agent reminders

- Make ALL code changes required by the plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
