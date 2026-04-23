# Plan: SMT Humanize — Human-Executable Signal Model

**Status**: ✅ Planned
**Complexity**: ⚠️ Medium
**Started**: 2026-04-21
**Prerequisite**: `smt-structural-and-fixes.md` merged. ✅ Complete — `displacement_body_pts` is already in the signal dict.

## Summary

Restructures signal output to produce typed, human-readable signals (ENTRY, MOVE_STOP, CLOSE_MARKET) suitable for a human trader acting on TradingView + Tradovate alerts. Also adds confidence scoring, opposing-displacement deception detection, and explicit MOVE_STOP instructions.

**Scope reduction (2026-04-22):** The original motivation was ~41 trades/day with 89.5% lasting < 2 minutes. After quality filters (Solutions A-F, `MAX_TDO_DISTANCE_PTS=15`, `SIGNAL_BLACKOUT_START/END`, `TRAIL_AFTER_TP_PTS`, `MAX_REENTRY_COUNT=1`), the system now generates ~1.6 trades/day. The frequency and daily-limits problems no longer exist. The plan now covers only what is genuinely new:

1. Typed signal output — **ENTRY_MARKET**, **ENTRY_LIMIT**, **MOVE_STOP**, **CLOSE_MARKET**.
2. Human-execution slippage — a single `HUMAN_ENTRY_SLIPPAGE_PTS` additive parameter (timeframe-agnostic; no bar-delay state machine changes).
3. Confidence scoring on each ENTRY signal.
4. Opposing-displacement deception detection for early exits.

The existing `INVALIDATION_MSS_EXIT` flag already handles MSS exit — no duplicate `DECEPTION_MSS_EXIT` flag is added. Daily limits and PENDING_ENTRY/PENDING_STOP states are dropped entirely.

## User Story

As a human trader following algorithmic signals, I want signals that are typed (entry / move-stop / close), rated by confidence, and accompanied by explicit stop-management instructions, so that I can act on them in real time without ambiguity.

## Acceptance Criteria

- [ ] Signal output has typed payloads: `ENTRY_MARKET`, `ENTRY_LIMIT`, `MOVE_STOP`, `CLOSE_MARKET`, as specified in Key Design Decisions.
- [ ] With `HUMAN_EXECUTION_MODE=True`, market-order fills apply an additive `HUMAN_ENTRY_SLIPPAGE_PTS` (default 0.0) on top of existing slippage — direction-correct (long: +pts, short: -pts on exit; reversed on entry).
- [ ] Each ENTRY signal carries a `confidence` score in [0, 1] computed from time-of-day, prior-trend, displacement body size, and TDO distance.
- [ ] Signals with `confidence < MIN_CONFIDENCE_THRESHOLD` (default 0.50) are not emitted in human mode (recorded internally).
- [ ] Price ≥ `ENTRY_LIMIT_CLASSIFICATION_PTS` away from signal price classifies the signal as `ENTRY_LIMIT`; otherwise `ENTRY_MARKET`. Note: this is independent of `LIMIT_ENTRY_BUFFER_PTS` (algo fill simulation) — different concept.
- [ ] Opposing displacement after entry (when `DECEPTION_OPPOSING_DISP_EXIT=True`) returns `"exit_invalidation_opposing_disp"` from `manage_position()`.
- [ ] When `HUMAN_EXECUTION_MODE=True` and breakeven/trailing-stop fires, `manage_position()` returns `"move_stop"` carrying the new stop level rather than mutating silently; `signal_smt.py` emits a `MOVE_STOP` signal.
- [ ] All new behaviours are gated by `HUMAN_EXECUTION_MODE=True`; default is False to preserve existing algo behaviour.
- [ ] `DECEPTION_OPPOSING_DISP_EXIT` is opt-in (`False` default) and active regardless of `HUMAN_EXECUTION_MODE`.
- [ ] ≥ 13 new tests cover all new code paths in `tests/test_smt_humanize.py`.

## Execution Agent Rules

- Make ALL code changes required by the plan.
- Delete debug logs added during execution (keep pre-existing ones).
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`.

---

## Codebase Context

| File | Role |
|---|---|
| `strategy_smt.py` | Config block: add human-mode constants. `manage_position()`: add opposing-displacement check, MOVE_STOP return. `_build_signal_from_bar()`: add `confidence` and `signal_type` fields. New `compute_confidence()` function. |
| `backtest_smt.py` | Fill path: apply `HUMAN_ENTRY_SLIPPAGE_PTS` where market orders fill. `_build_trade_record()`: add `confidence`, `signal_type`, `human_mode`, `deception_exit` fields. Handle `"move_stop"` return from `manage_position()` as non-exit. |
| `signal_smt.py` | `_process_scanning()`: confidence filter, signal-type classification, typed payload emission. `_process_managing()`: intercept `"move_stop"` return, emit MOVE_STOP signal. |
| `tests/test_smt_humanize.py` | New file — all Wave 6 tests. |

---

## Key Design Decisions

### Human mode flag

```python
HUMAN_EXECUTION_MODE: bool = False
```

When False: all existing behaviour unchanged (algo mode).
When True: slippage additive, confidence filter active, typed signals emitted, MOVE_STOP returned.

### Human-execution slippage

```python
HUMAN_ENTRY_SLIPPAGE_PTS: float = 0.0
```

Applied **additively** on top of `MARKET_ORDER_SLIPPAGE_PTS` when `HUMAN_EXECUTION_MODE=True`. Timeframe-agnostic: works identically on 1m, 5m, and 1s bars. Defaults to 0.0 so there is zero behaviour change at defaults; increase to model human reaction delay (e.g., 2–3 pts on MNQ).

For fills:
- Entry (long): `entry_price += HUMAN_ENTRY_SLIPPAGE_PTS`
- Entry (short): `entry_price -= HUMAN_ENTRY_SLIPPAGE_PTS`
- Exit market order: same direction convention as existing `MARKET_ORDER_SLIPPAGE_PTS`

### Signal payload structure

ENTRY signal:
```python
{
    "signal_type":    "ENTRY_MARKET" | "ENTRY_LIMIT",
    "direction":      "long" | "short",
    "entry_price":    float,
    "initial_stop":   float,
    "tp":             float,
    "confidence":     float,          # [0, 1]
    "valid_for_bars": int,            # ENTRY_LIMIT only
    "reason":         str,
}
```

MOVE_STOP signal:
```python
{
    "signal_type": "MOVE_STOP",
    "new_stop":    float,
    "reason":      "breakeven" | "trail_update",
    "urgency":     "low" | "high",
}
```

CLOSE_MARKET signal:
```python
{
    "signal_type": "CLOSE_MARKET",
    "reason":      "exhaustion" | "deception" | "session_close",
    "urgency":     "high",
}
```

---

## Implementation Tasks

### Wave 1 — Config and scaffold

#### Task 1.1 — Human mode config + signal type scaffold [`strategy_smt.py` + `signal_smt.py`]

**WAVE**: 1 | **AGENT_ROLE**: executor | **DEPENDS_ON**: none

In `strategy_smt.py` config block, add:
```python
# Human execution mode
HUMAN_EXECUTION_MODE: bool = False
HUMAN_ENTRY_SLIPPAGE_PTS: float = 0.0   # additive pts on market fills in human mode
MIN_CONFIDENCE_THRESHOLD: float = 0.50
ENTRY_LIMIT_CLASSIFICATION_PTS: float = 5.0  # pts away from signal price → ENTRY_LIMIT
                                               # distinct from LIMIT_ENTRY_BUFFER_PTS (algo fill)
MOVE_STOP_MIN_GAP_BARS: int = 0              # minimum bars between consecutive MOVE_STOP signals
DECEPTION_OPPOSING_DISP_EXIT: bool = False   # opt-in; active regardless of HUMAN_EXECUTION_MODE
```

No `signal_smt.py` daily state is needed (daily limits dropped — system generates ~1.6 trades/day organically).

---

### Wave 2 — Deception detection

#### Task 2.1 — Opposing displacement exit [`strategy_smt.py:manage_position()`]

**WAVE**: 2 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Task 1.1

Note: MSS exit (`INVALIDATION_MSS_EXIT`) already exists in `manage_position()` — do NOT add a duplicate `DECEPTION_MSS_EXIT` flag. Only the opposing displacement exit is new.

When `DECEPTION_OPPOSING_DISP_EXIT=True`, add to `manage_position()` before the stop/TP checks:

```python
if DECEPTION_OPPOSING_DISP_EXIT:
    opposing = "long" if direction == "short" else "short"
    if detect_displacement(current_bar, opposing):
        return "exit_invalidation_opposing_disp"
```

`detect_displacement()` already exists in `strategy_smt.py` — import/call directly. No new position fields are required.

---

### Wave 3 — Confidence scoring

#### Task 3.1 — Confidence scorer [`strategy_smt.py`]

**WAVE**: 3 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Wave 2 complete

`displacement_body_pts` is already in the signal dict (satisfied prerequisite). Add new function:

```python
def compute_confidence(signal: dict, prior_session_profitable: bool, session_start_ts: pd.Timestamp) -> float:
    # Time of day: ramp 0→1 from 9:30→13:00 ET
    mins_since_930 = max(0, (signal["entry_time"] - session_start_ts).total_seconds() / 60)
    time_score = min(1.0, mins_since_930 / 210.0)   # 210 min = 9:30 to 13:00

    # Prior confirmed trend
    trend_score = 1.0 if prior_session_profitable else 0.0

    # Displacement body size (normalise to 20 pts)
    body_pts = signal.get("displacement_body_pts") or 0.0
    body_score = min(1.0, body_pts / 20.0) if body_pts > 0 else 0.0

    # TDO distance sweet spot: peak at 30–100 pts
    tdo_dist = abs(signal["entry_price"] - signal["tdo"])
    if tdo_dist < 30:
        dist_score = tdo_dist / 30.0
    elif tdo_dist <= 100:
        dist_score = 1.0
    elif tdo_dist <= 200:
        dist_score = 1.0 - (tdo_dist - 100) / 100.0
    else:
        dist_score = 0.0

    return round(0.4 * time_score + 0.3 * trend_score + 0.2 * body_score + 0.1 * dist_score, 4)
```

Call `compute_confidence()` in `_build_signal_from_bar()` and add `"confidence": ...` to the returned signal dict. For `prior_session_profitable`, pass `False` as default in the backtest (can be wired to actual prior-session PnL in a later iteration).

---

#### Task 3.2 — Confidence filter + ENTRY_LIMIT classification [`signal_smt.py:_process_scanning()`]

**WAVE**: 3 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Task 3.1

In `_build_signal_from_bar()`, add to the returned dict:
```python
"signal_type": "ENTRY_MARKET",   # classified in signal_smt.py
```

In `signal_smt.py` `_process_scanning()`, after the signal dict is built:
1. If `HUMAN_EXECUTION_MODE` and `signal["confidence"] < MIN_CONFIDENCE_THRESHOLD`: log internally, return without emitting.
2. Classify signal type:
   - If `|current_price - signal["entry_price"]| >= ENTRY_LIMIT_CLASSIFICATION_PTS` → `signal["signal_type"] = "ENTRY_LIMIT"`
   - Otherwise → `signal["signal_type"] = "ENTRY_MARKET"` (already the default)
3. Emit signal payload in the structured format from Key Design Decisions.

---

### Wave 4 — MOVE_STOP emission

#### Task 4.1 — MOVE_STOP signal emission [`signal_smt.py` + `strategy_smt.py`]

**WAVE**: 4 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Task 3.2

In `manage_position()`, when breakeven trigger fires or trailing stop updates, currently the position is mutated silently. When `HUMAN_EXECUTION_MODE=True`:
- Instead of (or after) mutating `position["stop_price"]`, return `"move_stop"` with the new stop level attached:
  ```python
  position["_pending_move_stop"] = new_stop   # set before returning
  return "move_stop"
  ```
- The backtest state machine must handle `"move_stop"` as a non-exit event: apply the new stop, continue the bar loop.

In `signal_smt.py` `_process_managing()`, intercept `"move_stop"` return and emit a `MOVE_STOP` signal:
```python
{
    "signal_type": "MOVE_STOP",
    "new_stop":    position["_pending_move_stop"],
    "reason":      "breakeven" if position.get("breakeven_active") else "trail_update",
    "urgency":     "high" if reason == "breakeven" else "low",
}
```

Apply `MOVE_STOP_MIN_GAP_BARS`: track `_last_move_stop_bar`; suppress emission if `current_bar_idx - _last_move_stop_bar < MOVE_STOP_MIN_GAP_BARS`.

**Caller audit required:** All call sites of `manage_position()` must handle `"move_stop"` without treating it as an exit. Check `backtest_smt.py` state machine and `signal_smt.py`.

---

### Wave 5 — Trade record output updates

#### Task 5.1 — Trade record human-mode fields [`backtest_smt.py:_build_trade_record()`]

**WAVE**: 5 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Wave 4 complete

Add to trade dict and TSV header:
```python
"confidence":     signal.get("confidence"),
"signal_type":    signal.get("signal_type"),
"human_mode":     HUMAN_EXECUTION_MODE,
"deception_exit": True if "invalidation" in exit_result else False,
```

Also wire `HUMAN_ENTRY_SLIPPAGE_PTS` into the fill path: in the entry fill block (`WAITING_FOR_ENTRY` → `IN_TRADE` transition), when `HUMAN_EXECUTION_MODE=True`, adjust `position["entry_price"]` by `±HUMAN_ENTRY_SLIPPAGE_PTS` (direction-correct).

---

### Wave 6 — Tests

**WAVE**: 6 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Wave 5 complete

File: `tests/test_smt_humanize.py` (new file)

#### Human-execution slippage

| # | Test | Tool |
|---|---|---|
| S-1 | With HUMAN_EXECUTION_MODE=True and HUMAN_ENTRY_SLIPPAGE_PTS=2.0, long entry price is bar["Close"]+2.0 | pytest |
| S-2 | With HUMAN_EXECUTION_MODE=False, HUMAN_ENTRY_SLIPPAGE_PTS is not applied | pytest |

#### Confidence scoring

| # | Test | Tool |
|---|---|---|
| C-1 | compute_confidence returns value in [0, 1] | pytest |
| C-2 | Time-of-day score is 0.0 at 9:30 and 1.0 at 13:00 | pytest |
| C-3 | Signal with confidence < 0.50 is not emitted in human mode | pytest |
| C-4 | Signal with confidence >= 0.50 is emitted | pytest |
| C-5 | TDO distance of 75 pts scores 1.0; distance of 250 pts scores 0 | pytest |

#### Deception detection

| # | Test | Tool |
|---|---|---|
| DC-1 | Opposing displacement candle after entry triggers exit_invalidation_opposing_disp | pytest |
| DC-2 | DECEPTION_OPPOSING_DISP_EXIT=False: no opposing displacement exit fires | pytest |

#### MOVE_STOP

| # | Test | Tool |
|---|---|---|
| M-1 | Breakeven trigger returns "move_stop" (not silent mutation) in human mode | pytest |
| M-2 | MOVE_STOP signal emitted with correct new_stop and reason=breakeven | pytest |

#### Signal type classification

| # | Test | Tool |
|---|---|---|
| SC-1 | Price within ENTRY_LIMIT_CLASSIFICATION_PTS of signal price → ENTRY_MARKET | pytest |
| SC-2 | Price >= ENTRY_LIMIT_CLASSIFICATION_PTS from signal price → ENTRY_LIMIT | pytest |

#### Regression

| # | Test | Tool |
|---|---|---|
| R-1 | With HUMAN_EXECUTION_MODE=False, all existing tests pass unchanged | `pytest tests/` |

---

## Ordering Constraint

~~This plan must be executed after `smt-structural-and-fixes.md` is merged.~~ **✅ Satisfied** — structural plan is merged, `displacement_body_pts` is in the signal dict, `divergence_bar_high`/`divergence_bar_low` are in the position dict.

## Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| `manage_position()` MOVE_STOP return variant may break callers expecting only "hold" / "exit_*" | Medium | Audit all call sites before implementation; handle "move_stop" as a non-exit in backtest state machine; add regression test R-1 |
| Confidence score weights are hand-tuned and may not generalise | Low | Weights are v0 optimizable parameters; document as such; score in [0,1] with neutral defaults |
| `ENTRY_LIMIT_CLASSIFICATION_PTS` may be confused with `LIMIT_ENTRY_BUFFER_PTS` (algo fill) | Low | Names are intentionally different; comment in config explains distinction |

## Test Automation Summary

- **Total new test cases**: 13
- **Automated**: 13 (100%) — all via pytest with synthetic bar/session fixtures
- **Manual**: 0
- **Run command**: `pytest tests/test_smt_humanize.py -v`
- **Full regression**: `pytest tests/ -x`
