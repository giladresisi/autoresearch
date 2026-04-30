# Feature: Uncap Hold Time — Trail Width + Session Extension

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

The SMT backtest exits positions in 1–5 bars after the TDO is crossed because `TRAIL_AFTER_TP_PTS = 1.0` — a 1-point trail on MNQ exits on any 1-tick adverse move after TP breach. This prevents the strategy from riding large trend days like 2025-11-14 (400+ pt move, strategy exited at +37 pts).

This plan widens the trailing stop, adds two safeguards (never-widen rule and delayed activation), extends the session window, and modifies partial exit in trail mode: the stop-slide still fires (preserving the near-breakeven floor on TDO-touch reversals), but contract reduction is skipped — keeping the full position open to capture trend continuation.

Design spec: `docs/superpowers/specs/2026-04-22-uncap-hold-time-design.md`

## User Story

As a backtest developer,
I want positions to trail the market after crossing the TDO instead of exiting immediately,
So that I can measure whether trend-following hold mode captures large directional days.

## Problem Statement

`TRAIL_AFTER_TP_PTS = 1.0` is effectively exit-at-TDO with a noise buffer. The session window (`SESSION_END = "13:30"`) + signal blackout (`11:00–13:00`) also leaves only 30 minutes of post-blackout session. Together these prevent any meaningful trend hold.

## Solution Statement

Three changes to `strategy_smt.py`:
1. Widen `TRAIL_AFTER_TP_PTS` to 50.0 pts (configurable for optimizer).
2. Add `TRAIL_ACTIVATION_R` — trail only activates after price travels R multiples of the initial stop past TDO (prevents a bare TDO tick-through enabling a 50-pt adverse stop).
3. Add a never-widen rule — trail can only tighten the stop, never regress it.
4. In trail mode: `manage_position` still returns `"partial_exit"` at the partial level (stop-slide runs), but `backtest_smt.py` skips contract reduction — preserving the near-breakeven floor while keeping all contracts for trend capture.
5. Extend `SESSION_END` to `"15:15"`.
6. Add `initial_stop_pts` to signal dict (consumed by activation threshold calculation).

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: Medium
**Primary Systems Affected**: `strategy_smt.py` (constants, `_build_signal_from_bar`, `manage_position`)
**Dependencies**: None (pure strategy logic — no external services)
**Breaking Changes**: No — all constants configurable; `TRAIL_ACTIVATION_R = 0.0` reproduces prior TDO-crossing behaviour; existing tests use `monkeypatch` to set `TRAIL_AFTER_TP_PTS = 0.0`

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `strategy_smt.py` (lines 1–20) — SESSION_END, TRAIL_AFTER_TP_PTS constants to update
- `strategy_smt.py` (lines 103–108) — TRAIL_AFTER_TP_PTS comment block; add TRAIL_ACTIVATION_R below it
- `strategy_smt.py` (lines 1390–1424) — `_build_signal_from_bar` return dict; add `initial_stop_pts` field
- `strategy_smt.py` (lines 1456–1480) — Trail-after-TP block in `manage_position`; three edits here
- `strategy_smt.py` (lines 1577–1587) — Partial exit block in `manage_position`; NO change here (partial still fires in trail mode)
- `backtest_smt.py` (lines 617–672) — `if result == "partial_exit":` block; wrap contract-reduction in `if TRAIL_AFTER_TP_PTS == 0:` while leaving stop-slide unconditional
- `tests/test_smt_strategy.py` (lines 336–393) — `_make_position` / `_make_bar` helpers + existing manage_position tests; follow same pattern
- `tests/test_smt_backtest.py` — integration test pattern; follow existing style

### New Files to Create

None — all changes are edits to existing files.

### Patterns to Follow

**Constants block**: Follow style of adjacent constants — inline comment with optimizer search space.
**manage_position mutations**: All stop mutations use `position["stop_price"] = <value>` — no helper functions.
**Test helpers**: `_make_position(direction, entry_price, stop_price, take_profit)` and `_make_bar(high, low)` already exist in `test_smt_strategy.py` — reuse them.
**monkeypatch pattern**: Tests that need to override constants use `monkeypatch.setattr(train_smt, "CONSTANT_NAME", value)`.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (Sequential — same file, small edits) │
├─────────────────────────────────────────────────────────┤
│ Task 1.1: ADD constants + initial_stop_pts field         │
│ Agent: strategy-editor                                   │
└─────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 2: Core Logic (Parallel after Wave 1)                    │
├───────────────────────────────┬──────────────────────────────┤
│ Task 2.1                      │ Task 2.2                      │
│ MODIFY manage_position        │ MODIFY backtest partial block │
│ 2 changes (strategy_smt.py)   │ 1 change (backtest_smt.py)   │
│ Agent: strategy-editor        │ Agent: backtest-editor        │
└───────────────────────────────┴──────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│ WAVE 3: Tests (Parallel after Wave 2)                    │
├─────────────────────────┬────────────────────────────────┤
│ Task 3.1                │ Task 3.2                       │
│ Unit tests              │ Integration test               │
│ test_smt_strategy.py    │ test_smt_backtest.py           │
│ Agent: test-writer-A    │ Agent: test-writer-B           │
└─────────────────────────┴────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Sequential**: Task 1.1 — two edits in `strategy_smt.py`
**Wave 2 — Parallel after Wave 1**: Task 2.1 (two edits in `manage_position`) and Task 2.2 (one edit in `backtest_smt.py`) — different files, no dependency between them
**Wave 3 — Parallel**: Tasks 3.1 and 3.2 — independent test files

### Interface Contracts

**Contract 1**: Task 1.1 provides `TRAIL_ACTIVATION_R` constant and `initial_stop_pts` signal field → Task 2.1 consumes both
**Contract 2**: Task 1.1 provides `TRAIL_AFTER_TP_PTS` constant → Task 2.2 reads it for the conditional guard
**Contract 3**: Tasks 2.1 + 2.2 provide updated `manage_position` + `backtest` behaviour → Tasks 3.1 and 3.2 verify them

### Synchronization Checkpoints

**After Wave 1**: `python -c "import strategy_smt; print(strategy_smt.TRAIL_ACTIVATION_R)"`
**After Wave 2**: `python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -k "manage_position" -x -q`
**After Wave 3**: `python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q`

---

## IMPLEMENTATION PLAN

### Wave 1: Foundation

#### Task 1.1: ADD constants + `initial_stop_pts` signal field

**Purpose**: Introduce `TRAIL_ACTIVATION_R` and update `TRAIL_AFTER_TP_PTS` / `SESSION_END`. Add `initial_stop_pts` to the signal dict for use by `manage_position`.

**Dependencies**: None

**Edit 1 — Update SESSION_END** (`strategy_smt.py` line 19):
```python
# Before:
SESSION_END   = "13:30"

# After:
SESSION_END   = "15:15"
```

**Edit 2 — Update TRAIL_AFTER_TP_PTS and add TRAIL_ACTIVATION_R** (after line 107):
```python
# Before:
TRAIL_AFTER_TP_PTS = 1.0

# After:
TRAIL_AFTER_TP_PTS = 50.0   # was 1.0; optimizer: [25.0, 50.0, 75.0, 100.0]

# TRAIL_ACTIVATION_R: trail only takes over after price travels this many R-multiples
# of |entry - stop| past TDO. 0.0 = activate immediately at TDO (legacy behaviour).
# Prevents a bare TDO tick-through from enabling a 50-pt adverse stop placement.
# R-multiple is scale-invariant, consistent with BREAKEVEN_TRIGGER_PCT.
# Optimizer search space: [0.0, 0.5, 1.0, 1.5, 2.0]
TRAIL_ACTIVATION_R: float = 1.0
```

**Edit 3 — Add `initial_stop_pts` to `_build_signal_from_bar` return dict** (inside the return dict, after `"displacement_body_pts"` around line 1419):
```python
# Add this line to the returned dict:
"initial_stop_pts":     round(abs(entry_price - round(stop_price, 4)), 4),
```

**Validation**:
```bash
python -c "import strategy_smt; print(strategy_smt.TRAIL_ACTIVATION_R, strategy_smt.TRAIL_AFTER_TP_PTS, strategy_smt.SESSION_END)"
```
Expected: `1.0 50.0 15:15`

---

### Wave 2: Core Logic

#### Task 2.1: MODIFY `manage_position` — two targeted edits

**Purpose**: Implement the wider trail with delayed activation and never-widen rule.

**Dependencies**: Task 1.1 (TRAIL_ACTIVATION_R constant and initial_stop_pts field must exist)

**READ FIRST**: `strategy_smt.py` lines 1456–1598. Understand the existing trail block before editing. The two edits below are surgical changes within the existing structure. Do NOT touch the partial exit block at lines 1577–1587.

---

**Edit 1 — TDO crossing: defer stop, only mark breach**

Find the block where `crossed` is True and TDO is crossed for the first time (inside `else:` branch of `if position.get("tp_breached"):`). It currently sets `position["stop_price"]` on the crossing bar. Remove that line.

```python
# BEFORE (long case — the short case mirrors with min/Low):
if crossed:
    position["tp_breached"] = True
    if direction == "short":
        position["best_after_tp"] = min(tp, current_bar["Low"])
        position["stop_price"]    = position["best_after_tp"] + TRAIL_AFTER_TP_PTS   # ← REMOVE
    else:
        position["best_after_tp"] = max(tp, current_bar["High"])
        position["stop_price"]    = position["best_after_tp"] - TRAIL_AFTER_TP_PTS   # ← REMOVE
    return "hold"

# AFTER:
if crossed:
    position["tp_breached"] = True
    if direction == "short":
        position["best_after_tp"] = min(tp, current_bar["Low"])
    else:
        position["best_after_tp"] = max(tp, current_bar["High"])
    return "hold"
```

**Edit 2 — Post-TDO bars: activation threshold + never-widen rule**

Find the block inside `if position.get("tp_breached"):` that updates `best_after_tp` and sets `stop_price`. Replace the unconditional stop assignment with the threshold-gated, never-widen version.

```python
# BEFORE (long case shown; short mirrors with min/Low/+):
if direction == "short":
    best = min(position.get("best_after_tp", tp), current_bar["Low"])
    position["best_after_tp"] = best
    position["stop_price"]    = best + TRAIL_AFTER_TP_PTS
else:
    best = max(position.get("best_after_tp", tp), current_bar["High"])
    position["best_after_tp"] = best
    position["stop_price"]    = best - TRAIL_AFTER_TP_PTS

# AFTER:
if direction == "short":
    best = min(position.get("best_after_tp", tp), current_bar["Low"])
    position["best_after_tp"] = best
    activation_dist = TRAIL_ACTIVATION_R * position.get("initial_stop_pts", 0.0)
    if tp - best >= activation_dist:
        new_stop = best + TRAIL_AFTER_TP_PTS
        position["stop_price"] = min(position["stop_price"], new_stop)  # never widen
else:
    best = max(position.get("best_after_tp", tp), current_bar["High"])
    position["best_after_tp"] = best
    activation_dist = TRAIL_ACTIVATION_R * position.get("initial_stop_pts", 0.0)
    if best - tp >= activation_dist:
        new_stop = best - TRAIL_AFTER_TP_PTS
        position["stop_price"] = max(position["stop_price"], new_stop)  # never widen
```

The never-widen rule uses `max` for longs (stop only moves up) and `min` for shorts (stop only moves down).

**Validation**:
```bash
python -m pytest tests/test_smt_strategy.py -k "manage_position" -x -q
```
All existing manage_position tests must still pass (they monkeypatch TRAIL_AFTER_TP_PTS to 0.0).

---

#### Task 2.2: MODIFY `backtest_smt.py` — conditional contract reduction in partial exit block

**Purpose**: In trail mode, keep the stop-slide from `PARTIAL_STOP_BUFFER_PTS` active (to protect against TDO-touch reversals) but skip closing contracts — the full position stays open to capture trend continuation.

**Dependencies**: Task 1.1 (TRAIL_AFTER_TP_PTS constant read at runtime)

**Parallel with**: Task 2.1 — different file, no code dependency

**READ FIRST**: `backtest_smt.py` lines 617–672. The `if result == "partial_exit":` block currently does three things in sequence: (1) computes partial contracts + PnL, (2) slides the stop via PARTIAL_STOP_BUFFER_PTS, (3) reduces `position["contracts"]`. This edit keeps step 2 unconditional and gates steps 1 and 3 behind `if TRAIL_AFTER_TP_PTS == 0:`.

**Edit — Restructure partial exit block**:

```python
# BEFORE (simplified structure):
if result == "partial_exit":
    partial_contracts = min(...)
    if partial_contracts < 1:
        continue
    partial_exit_price = position.get("partial_price", ...)
    pnl_per_contract = (...)
    partial_pnl = pnl_per_contract * partial_contracts * MNQ_PNL_PER_POINT
    partial_trade, _ = _build_trade_record(...)
    # ... populate partial_trade fields ...
    # slide stop:
    _old_stop = position["stop_price"]
    if position["direction"] == "long":
        _lock_stop = partial_exit_price - PARTIAL_STOP_BUFFER_PTS
        _new_stop  = max(_lock_stop, _old_stop)
    else:
        _lock_stop = partial_exit_price + PARTIAL_STOP_BUFFER_PTS
        _new_stop  = min(_lock_stop, _old_stop)
    # promote secondary target ...
    trades.append(partial_trade)
    day_pnl += partial_pnl
    position["contracts"] -= partial_contracts
    position["stop_price"]    = _new_stop
    position["take_profit"]   = _new_tp
    if _sec is not None:
        position["secondary_target"] = None
    continue

# AFTER:
if result == "partial_exit":
    partial_exit_price = position.get("partial_price", float(bar["Close"]))

    # Stop-slide always runs regardless of trail mode
    _old_stop = position["stop_price"]
    if position["direction"] == "long":
        _lock_stop = partial_exit_price - PARTIAL_STOP_BUFFER_PTS
        _new_stop  = max(_lock_stop, _old_stop)
    else:
        _lock_stop = partial_exit_price + PARTIAL_STOP_BUFFER_PTS
        _new_stop  = min(_lock_stop, _old_stop)
    position["stop_price"] = _new_stop

    if TRAIL_AFTER_TP_PTS == 0:
        # Contract reduction only when not trailing
        partial_contracts = min(
            max(1, int(position["contracts"] * PARTIAL_EXIT_FRACTION)),
            position["contracts"] - 1,
        )
        if partial_contracts < 1:
            continue
        pnl_per_contract = (
            (partial_exit_price - position["entry_price"]) if position["direction"] == "long"
            else (position["entry_price"] - partial_exit_price)
        )
        partial_pnl = pnl_per_contract * partial_contracts * MNQ_PNL_PER_POINT
        partial_trade, _ = _build_trade_record(
            {**position, "contracts": partial_contracts}, "partial_exit", bar, MNQ_PNL_PER_POINT
        )
        partial_trade["matches_hypothesis"] = position.get("matches_hypothesis")
        for _f in ("hypothesis_direction", "pd_range_case", "pd_range_bias",
                   "week_zone", "day_zone", "trend_direction", "hypothesis_score",
                   "fvg_detected"):
            partial_trade[_f] = position.get(_f)
        partial_trade["exit_type"] = "partial_exit"
        partial_trade["pnl"] = round(partial_pnl, 2)
        partial_trade["exit_price"] = round(partial_exit_price, 4)
        _old_tp = position["take_profit"]
        _sec = position.get("secondary_target")
        _new_tp = _sec if _sec is not None else _old_tp
        partial_trade["partial_adjustments"] = (
            f"stop:{_old_stop:.2f}->{_new_stop:.2f};"
            f"tp:{_old_tp:.2f}->{_new_tp:.2f}"
        )
        trades.append(partial_trade)
        day_pnl += partial_pnl
        position["contracts"] -= partial_contracts
        position["take_profit"] = _new_tp
        if _sec is not None:
            position["secondary_target"] = None
    continue
```

**Validation**:
```bash
python -m pytest tests/test_smt_backtest.py -x -q
```

---

### Wave 3: Tests (Parallel)

#### Task 3.1: Unit tests — `tests/test_smt_strategy.py`

**Purpose**: Verify all new code paths in `manage_position` and the new `initial_stop_pts` field.

**Dependencies**: Tasks 1.1, 2.1

**Agent role**: test-writer-A

Add a new test section block at the end of `tests/test_smt_strategy.py` under the heading:
```python
# ══ manage_position TRAIL_ACTIVATION_R + never-widen tests ═══════════════════
```

All tests must `import strategy_smt as train_smt` locally (per project convention).

**Tests to implement** (13 tests):

---

**T1 — `test_trail_tdo_crossing_defers_stop_long`**
```
Monkeypatch TRAIL_AFTER_TP_PTS=50.0, TRAIL_ACTIVATION_R=1.0.
Position: long, entry=100, stop=97, tp=110, initial_stop_pts=3.0.
Bar: high=111 (crosses TDO). 
Assert: result == "hold", position["tp_breached"] == True, position["stop_price"] == 97 (unchanged).
```

**T2 — `test_trail_tdo_crossing_defers_stop_short`**
```
Monkeypatch TRAIL_AFTER_TP_PTS=50.0, TRAIL_ACTIVATION_R=1.0.
Position: short, entry=100, stop=103, tp=90, initial_stop_pts=3.0.
Bar: low=89 (crosses TDO).
Assert: result == "hold", position["tp_breached"] == True, position["stop_price"] == 103 (unchanged).
```

**T3 — `test_trail_does_not_activate_below_threshold_long`**
```
Monkeypatch TRAIL_AFTER_TP_PTS=50.0, TRAIL_ACTIVATION_R=1.0.
Position: long, entry=100, stop=97, tp=110, initial_stop_pts=3.0, tp_breached=True,
          best_after_tp=112.0 (2 pts past TDO — below threshold of 3.0).
Bar: high=112.
Assert: result == "hold", position["stop_price"] == 97 (unchanged — threshold not met).
```

**T4 — `test_trail_activates_at_threshold_long`**
```
Monkeypatch TRAIL_AFTER_TP_PTS=50.0, TRAIL_ACTIVATION_R=1.0.
Position: long, entry=100, stop=97, tp=110, initial_stop_pts=3.0, tp_breached=True,
          best_after_tp=113.0 (3 pts past TDO — exactly at threshold).
Bar: high=113.
Assert: position["stop_price"] == max(97, 113 - 50) == 97 (never-widen: trail stop is 63, below current 97).
```

**T5 — `test_trail_activates_far_past_tdo_long`**
```
Monkeypatch TRAIL_AFTER_TP_PTS=50.0, TRAIL_ACTIVATION_R=1.0.
Position: long, entry=100, stop=97, tp=110, initial_stop_pts=3.0, tp_breached=True,
          best_after_tp=170.0 (60 pts past TDO — well above threshold of 3.0).
Bar: high=170.
Assert: position["stop_price"] == max(97, 170 - 50) == max(97, 120) == 120 (trail tightens).
```

**T6 — `test_trail_activates_at_threshold_short`**
```
Monkeypatch TRAIL_AFTER_TP_PTS=50.0, TRAIL_ACTIVATION_R=1.0.
Position: short, entry=100, stop=103, tp=90, initial_stop_pts=3.0, tp_breached=True,
          best_after_tp=87.0 (3 pts past TDO — at threshold).
Bar: low=87.
Assert: position["stop_price"] == min(103, 87 + 50) == min(103, 137) == 103 (never-widen: trail stop is 137, above current 103).
```

**T7 — `test_trail_far_past_tdo_short`**
```
Monkeypatch TRAIL_AFTER_TP_PTS=50.0, TRAIL_ACTIVATION_R=1.0.
Position: short, entry=100, stop=103, tp=90, initial_stop_pts=3.0, tp_breached=True,
          best_after_tp=30.0 (60 pts past TDO — well above threshold).
Bar: low=30.
Assert: position["stop_price"] == min(103, 30 + 50) == 80 (trail tightens to 80).
```

**T8 — `test_never_widen_prevents_stop_regression_long`**
```
Monkeypatch TRAIL_AFTER_TP_PTS=50.0, TRAIL_ACTIVATION_R=0.0.
Position: long, entry=100, stop=96, tp=110, initial_stop_pts=4.0, tp_breached=True,
          best_after_tp=110.5 (barely past TDO, activation met at 0.0).
Bar: high=110.5.
Assert: position["stop_price"] == max(96, 110.5 - 50) == max(96, 60.5) == 96.
          (Trail would place stop at 60.5 — below entry stop of 96 — never-widen keeps it at 96.)
```

**T9 — `test_trail_activation_r_zero_activates_immediately`**
```
Monkeypatch TRAIL_AFTER_TP_PTS=50.0, TRAIL_ACTIVATION_R=0.0.
Position: long, entry=100, stop=97, tp=110, initial_stop_pts=3.0, tp_breached=True,
          best_after_tp=110.0 (exactly at TDO — threshold 0.0 means always active).
Bar: high=110.
Assert: trail logic runs (activation_dist == 0.0, 110 - 110 >= 0.0 is True).
         position["stop_price"] == max(97, 110 - 50) == 97 (never-widen protects against regression).
```

**T10 — `test_trail_exit_stop_fires_after_activation_long`**
```
Monkeypatch TRAIL_AFTER_TP_PTS=10.0, TRAIL_ACTIVATION_R=0.0.
Position: long, entry=100, stop=97, tp=110, initial_stop_pts=3.0, tp_breached=True,
          best_after_tp=130.0 (trail stop = 120, well above original stop).
Bar: low=119 (below trail stop of 120).
Assert: manage_position returns "exit_stop".
```

**T11 — `test_partial_exit_still_returned_when_trail_active`**
```
Monkeypatch TRAIL_AFTER_TP_PTS=50.0, PARTIAL_EXIT_ENABLED=True.
Position: long, entry=100, stop=97, tp=110, initial_stop_pts=3.0,
          partial_exit_level=105 (not yet done).
Bar: high=106 (above partial level, below TP).
Assert: result == "partial_exit" (trail mode does NOT suppress partial in manage_position;
        contract reduction is skipped in backtest_smt.py, not here).
```

**T12 — `test_partial_exit_fires_when_trail_disabled`**
```
Monkeypatch TRAIL_AFTER_TP_PTS=0.0, PARTIAL_EXIT_ENABLED=True, PARTIAL_EXIT_LEVEL_RATIO=0.33.
Position: long, entry=100, stop=97, tp=110, partial_exit_level=105, partial_done=False.
Bar: high=106 (above partial level).
Assert: result == "partial_exit".
```

**T13 — `test_initial_stop_pts_in_signal_dict`**
```
Call _build_signal_from_bar (disable all filters via monkeypatch):
  bar with Close=100, direction="long", tdo=115.
  stop_ratio=0.35 → stop = 100 - 0.35 * 15 = 94.75 → initial_stop_pts = 5.25.
Assert: signal["initial_stop_pts"] == pytest.approx(5.25, rel=1e-3).
```

**Run command**:
```bash
python -m pytest tests/test_smt_strategy.py -x -q
```

---

#### Task 3.2: Integration tests — `tests/test_smt_backtest.py`

**Purpose**: Verify end-to-end trail behaviour and the stop-slide-without-contract-reduction in trail mode.

**Dependencies**: Tasks 1.1, 2.1, 2.2

**Agent role**: test-writer-B

Add two tests at the end of `tests/test_smt_backtest.py`:

**T14 — `test_trail_mode_holds_past_tdo_and_exits_via_stop`**

Build a synthetic session where:
- SMT divergence fires early in the session
- Price crosses TDO, then continues upward for several bars, then reverses through the trail stop
- TRAIL_AFTER_TP_PTS=50.0, TRAIL_ACTIVATION_R=0.0 (immediate activation), PARTIAL_EXIT_ENABLED=False

Assert:
- The trade's `exit_type` is `"exit_stop"` (not `"exit_tp"`)
- `bars_held` (or equivalent duration) is > 1 bar after TDO crossing
- No `partial_exit` trade record is generated

**T15 — `test_trail_mode_partial_slides_stop_no_contract_reduction`**

Build a synthetic session where:
- SMT divergence fires; position is long, entry=100, stop=94, TDO=115
- TRAIL_AFTER_TP_PTS=50.0, PARTIAL_EXIT_ENABLED=True, PARTIAL_EXIT_LEVEL_RATIO=0.33
- Price reaches partial level (~105) then continues to TDO, then reverses to just above the stop-slide level

Assert:
- No `partial_exit` trade record is generated (contract reduction skipped in trail mode)
- The surviving trade exits at a price above the initial stop (94) — confirming the stop slid up to near the partial level
- `position["contracts"]` on the final trade record equals the original entry contracts (no reduction)

Use the same synthetic bar construction pattern as existing integration tests.

**Run command**:
```bash
python -m pytest tests/test_smt_backtest.py -x -q
```

---

## TESTING STRATEGY

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_strategy.py` | **Run**: `python -m pytest tests/test_smt_strategy.py -x -q`

13 tests covering:
- TDO crossing: stop deferred (T1, T2)
- Activation threshold not met: stop unchanged (T3)
- Activation threshold met, never-widen fires (T4, T5, T6, T7, T8)
- TRAIL_ACTIVATION_R=0.0 back-compat (T9)
- Trail exit_stop fires after activation (T10)
- Partial still fires in trail mode / fires correctly when trail disabled (T11, T12)
- initial_stop_pts field present (T13)

### Integration Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_backtest.py` | **Run**: `python -m pytest tests/test_smt_backtest.py -x -q`

2 tests: T14 (end-to-end trail-hold past TDO, exit via trail stop), T15 (partial fires stop-slide without contract reduction in trail mode).

### Regression Check

Run the full test suite after Wave 3 to confirm no regressions:
```bash
python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -q
```

Baseline: 150 collected tests pass before this change. Expect 165 after (150 + 15 new).

### Manual Tests

None — all scenarios are automatable with synthetic DataFrames.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest) | 13 | 87% |
| ✅ Integration (pytest) | 2 | 13% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 15 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Import + Constants

```bash
python -c "import strategy_smt; print('SESSION_END:', strategy_smt.SESSION_END); print('TRAIL_AFTER_TP_PTS:', strategy_smt.TRAIL_AFTER_TP_PTS); print('TRAIL_ACTIVATION_R:', strategy_smt.TRAIL_ACTIVATION_R)"
```
Expected: `SESSION_END: 15:15`, `TRAIL_AFTER_TP_PTS: 50.0`, `TRAIL_ACTIVATION_R: 1.0`

### Level 2: Unit Tests

```bash
python -m pytest tests/test_smt_strategy.py -x -q
```

### Level 3: Integration Tests

```bash
python -m pytest tests/test_smt_backtest.py -x -q
```

### Level 4: Full Suite

```bash
python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -q
```
Expected: 165 passed (150 baseline + 15 new), 0 failures.

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `TRAIL_ACTIVATION_R: float = 1.0` constant present in `strategy_smt.py` with optimizer search space comment `[0.0, 0.5, 1.0, 1.5, 2.0]`
- [ ] `TRAIL_AFTER_TP_PTS = 50.0` in `strategy_smt.py` with updated optimizer comment `[25.0, 50.0, 75.0, 100.0]`
- [ ] `SESSION_END = "15:15"` in `strategy_smt.py`
- [ ] `_build_signal_from_bar` returns a dict containing `initial_stop_pts = abs(entry_price - stop_price)`
- [ ] On the bar where TDO is first crossed, `stop_price` is NOT modified when `TRAIL_AFTER_TP_PTS > 0` — breach is recorded but stop deferred to activation check
- [ ] Trail activates only after `best_after_tp - tp >= TRAIL_ACTIVATION_R * initial_stop_pts` (long) / `tp - best_after_tp >= ...` (short)
- [ ] Trail stop applies never-widen: `max(current_stop, best - trail)` for longs, `min(current_stop, best + trail)` for shorts
- [ ] `manage_position` still returns `"partial_exit"` when the partial level is hit, regardless of trail mode — no guard added to the partial exit block
- [ ] In `backtest_smt.py`, when `result == "partial_exit"` and `TRAIL_AFTER_TP_PTS > 0`: stop-slide runs (position stop updated via `PARTIAL_STOP_BUFFER_PTS`) but `position["contracts"]` is NOT reduced and no partial trade record is appended
- [ ] In `backtest_smt.py`, when `result == "partial_exit"` and `TRAIL_AFTER_TP_PTS == 0`: original behaviour preserved (contracts reduced, partial trade record appended)
- [ ] When `TRAIL_ACTIVATION_R = 0.0`, trail activates immediately at TDO crossing (back-compat)

### Edge Cases
- [ ] When `initial_stop_pts` is absent from the position dict, `manage_position` defaults to `0.0` (immediate activation — safe fallback)
- [ ] Never-widen rule prevents stop regression even when `TRAIL_AFTER_TP_PTS` is large relative to TDO distance

### Validation
- [ ] All 13 new unit tests pass — `python -m pytest tests/test_smt_strategy.py -x -q`
- [ ] Integration test T14 passes: wide-trail position exits via `exit_stop` (not `exit_tp`) after continuing past TDO — `python -m pytest tests/test_smt_backtest.py -x -q`
- [ ] Integration test T15 passes: in trail mode, partial level reached causes stop-slide but no contract reduction and no partial trade record — `python -m pytest tests/test_smt_backtest.py -x -q`
- [ ] All 150 pre-existing tests still pass with no regressions — `python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -q`

### Out of Scope
- Entry logic, SMT divergence detection, confirmation bar rules
- Weekly H/L as hard secondary-target exit
- `SIGNAL_BLACKOUT_END` changes
- `MAX_HOLD_BARS` changes
- Optimizer run execution (this plan only implements the constants and logic)
- Multi-session/overnight holds

---

## COMPLETION CHECKLIST

- [ ] Task 1.1 complete and validated (constants + initial_stop_pts field)
- [ ] Task 2.1 complete and validated (manage_position three edits)
- [ ] Task 3.1 complete (13 unit tests passing)
- [ ] Task 3.2 complete (integration tests T14 and T15 passing)
- [ ] All validation levels executed (1–4)
- [ ] Full suite passes (165 tests, 0 failures)
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**Why never-widen matters**: Without this rule, TRAIL_AFTER_TP_PTS=50 on a position where TDO is barely crossed would move the stop from ~3.5 pts below entry to 50 pts below TDO — turning a winning exit into a potential 50+ pt loss. The `max` (long) / `min` (short) operator ensures the trail can only tighten.

**TRAIL_ACTIVATION_R=0.0 back-compat**: When set to 0.0, `activation_dist = 0.0` and `best - tp >= 0.0` is always True once `tp_breached`. This reproduces the immediate-activation behaviour (useful as an optimizer baseline cell).

**Partial exit in trail mode**: `manage_position` always returns `"partial_exit"` when the partial level is hit — the guard is in `backtest_smt.py`, not in the strategy. When `TRAIL_AFTER_TP_PTS > 0`, the backtest skips contract reduction but still runs the stop-slide via `PARTIAL_STOP_BUFFER_PTS`. This preserves a near-breakeven floor (outcome 2: TDO-touch reversals exit around the partial level, not at the initial stop) while keeping all contracts open for trend capture (outcome 3). When the optimizer sets `TRAIL_AFTER_TP_PTS = 0.0`, the full original partial exit behaviour restores automatically.

**SIGNAL_BLACKOUT_END unchanged**: Stays at "13:00". With SESSION_END at 15:15, the post-blackout window is 13:00–15:15 (2h15m). The 11:00–13:00 blackout was calibrated for a 13:30 close — its validity at 15:15 should be evaluated from backtest output, not pre-emptively removed.

**MAX_HOLD_BARS unchanged**: 120 bars = 2 hours at 1-min resolution. The user confirmed this is sufficient for now.
