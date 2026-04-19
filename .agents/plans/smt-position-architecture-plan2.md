# Feature: SMT Strategy — Position Architecture Expansion (Plan 2 of 2)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

**⚠️ DEPENDENCY: Plan 1 (`smt-signal-quality-plan1.md`) MUST be fully executed before this plan.**
Plan 2 reads constants and signal dict fields that Plan 1 introduces:
`STRUCTURAL_STOP_BUFFER_PTS`, `midnight_open`, `smt_defended_level`, `divergence_bar_high/low`, `smt_type` in signal/position dicts.
`detect_smt_divergence` must already return a 5-tuple (Plan 1 task 2.1).

---

## Feature Description

Four architectural additions to the SMT strategy, all defaulting to False/0.0 to preserve the existing baseline:

1. **FVG detection infrastructure** — `detect_fvg()` identifies the most recent Fair Value Gap (3-bar imbalance) in the signal direction on MNQ. Foundation used by Layer B entry and SMT fill detection.
2. **Two-layer position model** — `TWO_LAYER_POSITION`: initial entry uses `LAYER_A_FRACTION` of total contracts (Layer A). When price retraces into the FVG zone identified at signal time, remaining contracts enter (Layer B). Combined stop tightens to the FVG boundary on Layer B entry. Blended average entry price is used for P&L calculation.
3. **SMT-optional gate (displacement entry)** — `SMT_OPTIONAL`: when True, the IDLE detection loop also accepts entries triggered by a displacement candle (large one-bar move ≥ `MIN_DISPLACEMENT_PTS`) even when no wick SMT is detected. Gives access to pure momentum entries aligned with the session bias.
4. **Partial exit at first liquidity draw** — `PARTIAL_EXIT_ENABLED`: `manage_position()` returns a new `"partial_exit"` string when price reaches the partial target level (`PARTIAL_EXIT_FRACTION` of contracts closed). The harness creates a separate trade record for the partial and keeps the remainder running toward TP.

A fifth feature, **SMT fill detection** (`SMT_FILL_ENABLED`), is included as an opt-in constant and `detect_smt_fill()` function but does not modify the state machine — it fires via the same IDLE detection path as displacement entries when enabled.

## User Story

As a quant running the SMT strategy
I want to size into positions in two tranches and exit partial profits at the first draw on liquidity
So that I can reduce drawdown on the initial entry while still capturing the full ICT target on remaining contracts

## Problem Statement

The current strategy enters at full size at a single price level with no ability to pyramid on confirmation, and exits the entire position at TDO/midnight open. ICT's position-building model (small initial entry + add-in on FVG retracement) and partial-exit discipline are not represented. Entries are also locked out when no wick SMT exists despite a valid displacement impulse.

## Solution Statement

Add all four features as opt-in constants. FVG detection is the shared infrastructure (Task 1.2). Two-layer model extends the position dict and modifies `manage_position()` (mutation-based, no new return values for Layer B). Partial exit adds a new `"partial_exit"` return value handled by the harness. SMT-optional adds a secondary detection branch in the IDLE state.

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: High
**Primary Systems Affected**: `strategy_smt.py`, `backtest_smt.py`, `signal_smt.py`
**Dependencies**: Plan 1 (smt-signal-quality-plan1.md) fully executed; no new packages
**Breaking Changes**: No breaking changes — all new constants default to False/0.0

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `strategy_smt.py` (lines 1–135) — STRATEGY TUNING block; Plan 2 constants go here
- `strategy_smt.py` (lines 205–262) — `detect_smt_divergence()` — 5-tuple post-Plan 1; model for new detection functions
- `strategy_smt.py` (lines 316–330) — `compute_tdo()` — style model for `detect_fvg()` / `detect_displacement()`
- `strategy_smt.py` (lines 471–527) — `_build_signal_from_bar()` — gains `fvg_zone` param and `partial_exit_level` field
- `strategy_smt.py` (lines 531–613) — `manage_position()` — gains Layer B entry mutation + partial exit check
- `strategy_smt.py` (lines 409–468) — `screen_session()` — gains SMT-optional branch + FVG zone passing
- `backtest_smt.py` (lines 276–434) — state machine IDLE/WAITING_FOR_ENTRY/IN_TRADE — handle `"partial_exit"`, SMT-optional in IDLE, Layer B contracts
- `backtest_smt.py` (lines 577–595) — `_write_trades_tsv()` — add `fvg_high`, `fvg_low`, `layer_b_entered` TSV fields
- `tests/test_smt_signal_quality.py` (created in Plan 1) — pattern for fixture helpers and test structure

### New Files to Create

- `tests/test_smt_position_arch.py` — all new Plan 2 tests (20 tests)

### Patterns to Follow

**FVG 3-bar pattern**: bar1 = pre-impulse, bar2 = impulse, bar3 = post-impulse.
- Bullish FVG (long trade): `bar3["Low"] > bar1["High"]` — gap zone is `[bar1.High, bar3.Low]`.
- Bearish FVG (short trade): `bar3["High"] < bar1["Low"]` — gap zone is `[bar3.High, bar1.Low]`.

**Position dict mutation pattern** (existing): `manage_position` already mutates `position["stop_price"]`, `position["tp_breached"]`, etc. Layer B entry follows the same pattern.

**Partial exit precedent**: `train.py` v4-b already uses `exit_type='partial'`; the backtest harness creates a trade record and halves `position['shares']`. Mirror this in `backtest_smt.py`.

**SMT-optional detection**: runs ONLY when `detect_smt_divergence()` returns None. Never fire both on the same bar.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────────┐
│ WAVE 1: Foundation — Parallel                        │
│ Task 1.1: ADD 10 constants to strategy_smt.py        │
│ Task 1.2: ADD detect_fvg(), detect_displacement(),   │
│           detect_smt_fill() to strategy_smt.py       │
└──────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────┐
│ WAVE 2: Signal-layer changes — Parallel              │
│ Task 2.1: MODIFY _build_signal_from_bar()            │
│ Task 2.2: MODIFY screen_session()                    │
└──────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────┐
│ WAVE 3: Position management                          │
│ Task 3.1: MODIFY manage_position() (sequential)      │
└──────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────┐
│ WAVE 4: Harness + live — Parallel                    │
│ Task 4.1: UPDATE backtest_smt.py                     │
│ Task 4.2: UPDATE signal_smt.py                       │
└──────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────┐
│ WAVE 5: Tests — Parallel                             │
│ Task 5.1: CREATE tests/test_smt_position_arch.py     │
│ Task 5.2: Regression check — no new failures         │
└──────────────────────────────────────────────────────┘
```

### Interface Contracts

**Contract 1**: `detect_fvg(bars, bar_idx, direction, lookback=20)` returns `dict | None`:
`{"fvg_high": float, "fvg_low": float, "fvg_bar": int}` where `fvg_high > fvg_low` always.

**Contract 2**: `detect_displacement(bars, bar_idx, direction)` returns `bool`.
True when body ≥ `MIN_DISPLACEMENT_PTS` and direction matches. Always False when `SMT_OPTIONAL=False`.

**Contract 3**: `detect_smt_fill(mes_bars, mnq_bars, bar_idx, lookback=20)` returns `tuple | None`:
`(direction, fvg_high, fvg_low)`. Always None when `SMT_FILL_ENABLED=False`.

**Contract 4**: `manage_position()` new return value: `"partial_exit"`. Harness handles it like an intermediate close (partial trade record, reduce contracts, stay IN_TRADE).

**Contract 5**: Signal dict (post-Plan 2) gains fields: `fvg_high`, `fvg_low`, `partial_exit_level`, `total_contracts_target`.
Position dict (post-Plan 2) gains: `layer_b_entered`, `layer_b_entry_price`, `layer_b_contracts`, `total_contracts_target`, `partial_done`, `partial_price`.

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation

#### Task 1.1: ADD constants to `strategy_smt.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1, 2.2, 3.1, 4.1, 5.1]
- **PROVIDES**: 10 new constants, all defaulting to off
- **IMPLEMENT**: In the `# ══ STRATEGY TUNING ══` block, add after the HIDDEN_SMT_ENABLED block (from Plan 1):

```python
# Two-layer position model: enter Layer A at LAYER_A_FRACTION of max contracts,
# add Layer B when price retraces into the FVG zone.
# LAYER_A_FRACTION = 0.5 means Layer A gets floor(total * 0.5) contracts; Layer B gets the rest.
# Requires FVG_ENABLED and FVG_LAYER_B_TRIGGER to also be True for Layer B to enter.
# Optimizer search space: TWO_LAYER_POSITION [True, False]; LAYER_A_FRACTION [0.33, 0.5, 0.67]
TWO_LAYER_POSITION: bool = False
LAYER_A_FRACTION: float = 0.5

# FVG (Fair Value Gap) detection: 3-bar imbalance where bar3 and bar1 do not overlap.
# FVG_MIN_SIZE_PTS: minimum gap size in MNQ points to qualify as a valid FVG.
# FVG_LAYER_B_TRIGGER: when True + TWO_LAYER_POSITION, Layer B enters on FVG retracement.
# Optimizer search space: FVG_ENABLED [True, False]; FVG_MIN_SIZE_PTS [1.0, 2.0, 3.0, 5.0]
FVG_ENABLED: bool = False
FVG_MIN_SIZE_PTS: float = 2.0
FVG_LAYER_B_TRIGGER: bool = False

# SMT-optional: accept displacement candles (body ≥ MIN_DISPLACEMENT_PTS) as entries
# even when no wick-based SMT exists. Fires only when detect_smt_divergence returns None.
# Optimizer search space: SMT_OPTIONAL [True, False]; MIN_DISPLACEMENT_PTS [8.0, 10.0, 15.0]
SMT_OPTIONAL: bool = False
MIN_DISPLACEMENT_PTS: float = 10.0

# Partial exit at first draw on liquidity.
# PARTIAL_EXIT_FRACTION: fraction of open contracts to close at the partial level.
# Partial level = midpoint between entry and take_profit.
# Optimizer search space: PARTIAL_EXIT_ENABLED [True, False]; PARTIAL_EXIT_FRACTION [0.33, 0.5]
PARTIAL_EXIT_ENABLED: bool = False
PARTIAL_EXIT_FRACTION: float = 0.5

# SMT fill divergence: MES fills a FVG that MNQ has not (bearish) or vice versa (bullish).
# Fires as an alternative to wick SMT in the IDLE detection loop.
# Optimizer search space: [True, False]
SMT_FILL_ENABLED: bool = False
```

- **VALIDATE**: `uv run python -c "import strategy_smt; print(strategy_smt.TWO_LAYER_POSITION, strategy_smt.FVG_ENABLED)"`

#### Task 1.2: ADD `detect_fvg()`, `detect_displacement()`, `detect_smt_fill()` to `strategy_smt.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1, 2.2, 3.1, 4.1, 5.1]
- **PROVIDES**: Three new public detection functions
- **IMPLEMENT**: Insert after `compute_overnight_range()` (added by Plan 1). Place in this order: `detect_fvg`, `detect_displacement`, `detect_smt_fill`.

```python
def detect_fvg(
    bars: pd.DataFrame,
    bar_idx: int,
    direction: str,
    lookback: int = 20,
) -> dict | None:
    """Find the most recent Fair Value Gap in the given direction before bar_idx.

    Bullish FVG (long): bar3.Low > bar1.High — gap on the way up.
      Zone = [bar1.High, bar3.Low]; price retraces down into this zone for Layer B.
    Bearish FVG (short): bar3.High < bar1.Low — gap on the way down.
      Zone = [bar3.High, bar1.Low]; price retraces up into this zone for Layer B.

    Returns {"fvg_high": float, "fvg_low": float, "fvg_bar": int} or None.
    Always None when FVG_ENABLED is False.
    """
    if not FVG_ENABLED:
        return None
    start = max(0, bar_idx - lookback)
    # Search backward from bar_idx-2 so bar3 = i+2 < bar_idx
    for i in range(bar_idx - 2, start - 1, -1):
        if i + 2 >= bar_idx:
            continue
        bar1_h = float(bars["High"].iloc[i])
        bar1_l = float(bars["Low"].iloc[i])
        bar3_h = float(bars["High"].iloc[i + 2])
        bar3_l = float(bars["Low"].iloc[i + 2])
        if direction == "long":
            if bar3_l > bar1_h and (bar3_l - bar1_h) >= FVG_MIN_SIZE_PTS:
                return {"fvg_high": bar3_l, "fvg_low": bar1_h, "fvg_bar": i}
        else:  # short
            if bar3_h < bar1_l and (bar1_l - bar3_h) >= FVG_MIN_SIZE_PTS:
                return {"fvg_high": bar1_l, "fvg_low": bar3_h, "fvg_bar": i}
    return None


def detect_displacement(
    bars: pd.DataFrame,
    bar_idx: int,
    direction: str,
) -> bool:
    """True if bar_idx is a displacement candle in the given direction.

    Displacement: body (|Close - Open|) >= MIN_DISPLACEMENT_PTS and candle direction
    matches. Always False when SMT_OPTIONAL is False or MIN_DISPLACEMENT_PTS <= 0.
    """
    if not SMT_OPTIONAL or MIN_DISPLACEMENT_PTS <= 0:
        return False
    bar = bars.iloc[bar_idx]
    body = abs(float(bar["Close"]) - float(bar["Open"]))
    if body < MIN_DISPLACEMENT_PTS:
        return False
    if direction == "long"  and bar["Close"] > bar["Open"]: return True
    if direction == "short" and bar["Close"] < bar["Open"]: return True
    return False


def detect_smt_fill(
    mes_bars: pd.DataFrame,
    mnq_bars: pd.DataFrame,
    bar_idx: int,
    lookback: int = 20,
) -> tuple | None:
    """Detect inter-instrument FVG fill divergence.

    Bearish: current MES bar reaches into a recent bearish FVG that MNQ has not reached.
    Bullish: current MNQ bar reaches into a recent bullish FVG that MES has not reached.

    Returns (direction, fvg_high, fvg_low) or None.
    Always None when SMT_FILL_ENABLED is False.
    """
    if not SMT_FILL_ENABLED:
        return None
    mes_bar = mes_bars.iloc[bar_idx]
    mnq_bar = mnq_bars.iloc[bar_idx]
    # Bearish fill: MES rallies into a bearish FVG; MNQ has not reached the zone
    mes_fvg = detect_fvg(mes_bars, bar_idx, "short", lookback)
    if mes_fvg is not None:
        if float(mes_bar["High"]) >= mes_fvg["fvg_low"] and float(mnq_bar["High"]) < mes_fvg["fvg_low"]:
            return ("short", mes_fvg["fvg_high"], mes_fvg["fvg_low"])
    # Bullish fill: MNQ retraces into a bullish FVG; MES has not reached the zone
    mnq_fvg = detect_fvg(mnq_bars, bar_idx, "long", lookback)
    if mnq_fvg is not None:
        if float(mnq_bar["Low"]) <= mnq_fvg["fvg_high"] and float(mes_bar["Low"]) > mnq_fvg["fvg_high"]:
            return ("long", mnq_fvg["fvg_high"], mnq_fvg["fvg_low"])
    return None
```

- **VALIDATE**: `uv run python -c "from strategy_smt import detect_fvg, detect_displacement, detect_smt_fill; print('ok')"`

**Wave 1 Checkpoint**: `uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_signal_quality.py -q`

---

### WAVE 2: Signal-layer changes

#### Task 2.1: MODIFY `_build_signal_from_bar()` — FVG zone + partial level

- **WAVE**: 2
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [1.1, 1.2]
- **BLOCKS**: [3.1, 4.1, 5.1]
- **PROVIDES**: Signal dict with `fvg_high`, `fvg_low`, `partial_exit_level`, `total_contracts_target`
- **IMPLEMENT**: Add `fvg_zone=None` parameter after Plan 1's new parameters. In the returned dict, add:

```python
# New Plan 2 fields:
"fvg_high":              float(fvg_zone["fvg_high"]) if fvg_zone else None,
"fvg_low":               float(fvg_zone["fvg_low"])  if fvg_zone else None,
"partial_exit_level":    round((entry_price + tdo) / 2, 4) if PARTIAL_EXIT_ENABLED else None,
```

`total_contracts_target` is NOT set here — it is set by the harness (backtest/screen_session) when computing contracts.

- **VALIDATE**: `uv run python -c "import strategy_smt; print('ok')"`

#### Task 2.2: MODIFY `screen_session()` — SMT-optional + FVG passing

- **WAVE**: 2
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [1.1, 1.2]
- **BLOCKS**: [4.2, 5.1]
- **PROVIDES**: `screen_session` compatible with Plan 2 features
- **IMPLEMENT**:
  1. The function already accepts `midnight_open=None, overnight_range=None` from Plan 1.
  2. In the main bar loop after `_smt = detect_smt_divergence(...)`:
     ```python
     # SMT-optional: accept displacement if no wick/hidden SMT found
     _smt_fill = None
     _displacement_direction = None
     if _smt is None:
         if SMT_FILL_ENABLED:
             _smt_fill = detect_smt_fill(mes_reset, mnq_reset, bar_idx)
         if _smt_fill is None and SMT_OPTIONAL:
             for _d in ("short", "long"):
                 if detect_displacement(mnq_reset, bar_idx, _d):
                     _displacement_direction = _d
                     break
     # Resolve effective direction
     if _smt is not None:
         direction, _smt_sweep, _smt_miss, _smt_type, _smt_defended = _smt
     elif _smt_fill is not None:
         direction, _fill_fvg_high, _fill_fvg_low = _smt_fill
         _smt_sweep = _smt_miss = 0.0; _smt_type = "fill"; _smt_defended = None
     elif _displacement_direction is not None:
         direction = _displacement_direction
         _smt_sweep = _smt_miss = 0.0; _smt_type = "displacement"; _smt_defended = None
     else:
         continue
     ```
  3. After direction is resolved, compute FVG zone:
     ```python
     _fvg_zone = detect_fvg(mnq_reset, bar_idx, direction)
     ```
  4. Pass `fvg_zone=_fvg_zone` to `_build_signal_from_bar()`.

- **VALIDATE**: `uv run python -c "from strategy_smt import screen_session; print('ok')"`

**Wave 2 Checkpoint**: `uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_signal_quality.py -q --tb=short`

---

### WAVE 3: Position management

#### Task 3.1: MODIFY `manage_position()` — Layer B entry + partial exit

- **WAVE**: 3
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [1.1, 2.1]
- **BLOCKS**: [4.1, 5.1]
- **PROVIDES**: Layer B entry mutation + `"partial_exit"` return value
- **IMPLEMENT**: Insert two new blocks. The full order after Plan 1 is:

  1. Trail-after-TP (existing)
  2. **[NEW] Layer B entry detection** (before breakeven)
  3. Breakeven/trailing stop update (existing, from Plan 1)
  4. Thesis-invalidation exits (Plan 1)
  5. Stop check (existing)
  6. **[NEW] Partial exit check** (before TP check)
  7. TP check (existing)
  8. return "hold"

**Layer B entry block** (insert before breakeven block):
```python
# ── Layer B entry (FVG retracement) ─────────────────────────────────────
if TWO_LAYER_POSITION and FVG_LAYER_B_TRIGGER:
    if not position.get("layer_b_entered") and not position.get("partial_done"):
        fvg_high = position.get("fvg_high")
        fvg_low  = position.get("fvg_low")
        if fvg_high is not None and fvg_low is not None:
            in_fvg = (
                (direction == "long"  and current_bar["Low"]  <= fvg_high and current_bar["Low"]  >= fvg_low) or
                (direction == "short" and current_bar["High"] >= fvg_low  and current_bar["High"] <= fvg_high)
            )
            if in_fvg:
                total_target = position.get("total_contracts_target", position["contracts"])
                layer_b_contracts = total_target - position["contracts"]
                if layer_b_contracts > 0:
                    lb_entry = float(current_bar["Close"])
                    n_a = position["contracts"]
                    position["entry_price"] = (
                        position["entry_price"] * n_a + lb_entry * layer_b_contracts
                    ) / (n_a + layer_b_contracts)
                    position["contracts"]           += layer_b_contracts
                    position["layer_b_entered"]      = True
                    position["layer_b_entry_price"]  = lb_entry
                    position["layer_b_contracts"]    = layer_b_contracts
                    # Tighten stop to FVG boundary on Layer B entry
                    if direction == "long":
                        new_stop = fvg_low - STRUCTURAL_STOP_BUFFER_PTS
                        position["stop_price"] = max(position["stop_price"], new_stop)
                    else:
                        new_stop = fvg_high + STRUCTURAL_STOP_BUFFER_PTS
                        position["stop_price"] = min(position["stop_price"], new_stop)
```

**Partial exit block** (insert before TP check):
```python
# ── Partial exit at first draw on liquidity ─────────────────────────────
if PARTIAL_EXIT_ENABLED and not position.get("partial_done"):
    lvl = position.get("partial_exit_level")
    if lvl is not None:
        if direction == "long"  and current_bar["High"] >= lvl:
            position["partial_done"]  = True
            position["partial_price"] = lvl
            return "partial_exit"
        if direction == "short" and current_bar["Low"]  <= lvl:
            position["partial_done"]  = True
            position["partial_price"] = lvl
            return "partial_exit"
```

- **VALIDATE**: `uv run python -c "import strategy_smt; print('ok')"`

**Wave 3 Checkpoint**: `uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_signal_quality.py -q --tb=short`

---

### WAVE 4: Harness + live path

#### Task 4.1: UPDATE `backtest_smt.py` — partial_exit, Layer B, SMT-optional, TSV

- **WAVE**: 4
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [3.1]
- **BLOCKS**: [5.1, 5.2]
- **PROVIDES**: Fully wired backtest with all Plan 2 features
- **IMPLEMENT**:

  1. **Imports**: add `detect_fvg, detect_displacement, detect_smt_fill` to the `from strategy_smt import` line.

  2. **IDLE state — SMT-optional + fill detection**: After `_smt = detect_smt_divergence(...)` returns None, add:
     ```python
     _smt_fill = None
     _displacement_dir = None
     if _smt is None:
         if SMT_FILL_ENABLED:
             _smt_fill = detect_smt_fill(mes_reset, mnq_reset, bar_idx)
         if _smt_fill is None and SMT_OPTIONAL:
             for _d in ("short", "long"):
                 if TRADE_DIRECTION in ("both", _d) and detect_displacement(mnq_reset, bar_idx, _d):
                     _displacement_dir = _d
                     break
     if _smt is None and _smt_fill is None and _displacement_dir is None:
         continue
     # Resolve effective direction + aux fields
     if _smt is not None:
         direction, _smt_sweep, _smt_miss, _smt_type, _smt_defended = _smt
     elif _smt_fill is not None:
         direction, _, _ = _smt_fill
         _smt_sweep = _smt_miss = 0.0; _smt_type = "fill"; _smt_defended = None
     else:
         direction = _displacement_dir
         _smt_sweep = _smt_miss = 0.0; _smt_type = "displacement"; _smt_defended = None
     ```
     After direction is resolved, compute FVG:
     ```python
     _pending_fvg = detect_fvg(mnq_reset, bar_idx, direction)
     ```
     Store `_pending_fvg_high = _pending_fvg["fvg_high"] if _pending_fvg else None` etc.

  3. **WAITING_FOR_ENTRY / REENTRY_ELIGIBLE**: pass `fvg_zone=_pending_fvg` to `_build_signal_from_bar()`.
     After building signal and position dict, set:
     ```python
     total_contracts_target = contracts  # default: no two-layer
     if TWO_LAYER_POSITION:
         layer_a_contracts = max(1, int(contracts * LAYER_A_FRACTION))
         total_contracts_target = contracts
         contracts = layer_a_contracts
     position = {
         **signal, "entry_date": day, "contracts": contracts,
         "total_contracts_target": total_contracts_target,
         "layer_b_entered": False, "layer_b_entry_price": None,
         "layer_b_contracts": 0, "partial_done": False, "partial_price": None,
     }
     ```

  4. **IN_TRADE `"partial_exit"` handling**: When `result == "partial_exit"`, create a partial trade record and reduce contracts — keep in IN_TRADE:
     ```python
     if result == "partial_exit":
         partial_contracts = max(1, int(position["contracts"] * PARTIAL_EXIT_FRACTION))
         partial_exit_price = position.get("partial_price", float(bar["Close"]))
         pnl_per_contract = (
             (partial_exit_price - position["entry_price"]) if position["direction"] == "long"
             else (position["entry_price"] - partial_exit_price)
         )
         partial_pnl = pnl_per_contract * partial_contracts * MNQ_PNL_PER_POINT
         partial_trade = _build_trade_record(
             {**position, "contracts": partial_contracts}, "partial_exit", bar, MNQ_PNL_PER_POINT
         )
         partial_trade["matches_hypothesis"] = position.get("matches_hypothesis")
         partial_trade["exit_type"] = "partial_exit"
         trades.append(partial_trade)
         day_pnl += partial_pnl
         position["contracts"] -= partial_contracts
         # Stay IN_TRADE with remaining contracts
         continue  # skip the result != "hold" block below
     ```
     Note: add this block BEFORE the `if result != "hold":` block.

  5. **`_write_trades_tsv`**: add `"fvg_high"`, `"fvg_low"`, `"layer_b_entered"`, `"layer_b_entry_price"`, `"layer_b_contracts"` to fieldnames list (after `"smt_type"` from Plan 1).

  6. **`_build_trade_record`**: no changes — `restval=""` in DictWriter handles missing keys.

- **VALIDATE**: `uv run python -c "import backtest_smt; print('ok')"`

#### Task 4.2: UPDATE `signal_smt.py` — FVG/displacement support

- **WAVE**: 4
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [2.2]
- **BLOCKS**: [5.1]
- **PROVIDES**: Live signal path aligned with Plan 2
- **IMPLEMENT**:
  1. Add `detect_fvg, detect_displacement, detect_smt_fill` to the `from strategy_smt import` line.
  2. In the SCANNING state, after calling `screen_session()`, no changes needed — `screen_session()` already handles SMT-optional and FVG zone internally (Task 2.2). The returned signal will have `fvg_high`, `fvg_low`, `partial_exit_level` fields.

- **VALIDATE**: `uv run python -c "import signal_smt; print('ok')" 2>&1 | head -3`

**Wave 4 Checkpoint**: `uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py tests/test_smt_signal_quality.py -q --tb=short`

---

### WAVE 5: Tests

#### Task 5.1: CREATE `tests/test_smt_position_arch.py`

- **WAVE**: 5
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [2.1, 2.2, 3.1, 4.1]
- **PROVIDES**: 20 new tests covering all Plan 2 code paths
- **IMPLEMENT**: Create the file with these test groups.

Use `_make_5m_bars()` fixture from `test_smt_signal_quality.py` — import it or duplicate the helper locally.

**detect_fvg (3 tests)**:
1. `test_fvg_bullish_detected` — bar1.High=100, bar2 impulse, bar3.Low=103 → returns zone [100, 103] for long
2. `test_fvg_bearish_detected` — bar1.Low=97, bar2 impulse, bar3.High=94 → returns zone [94, 97] for short
3. `test_fvg_returns_none_when_bars_overlap` — bar3.Low <= bar1.High → None for long

**detect_displacement (2 tests)**:
4. `test_displacement_long_detected` — SMT_OPTIONAL=True, large bullish body ≥ MIN_DISPLACEMENT_PTS → True
5. `test_displacement_false_when_optional_disabled` — SMT_OPTIONAL=False → always False

**detect_smt_fill (2 tests)**:
6. `test_smt_fill_bearish_detected` — SMT_FILL_ENABLED=True, MES reaches FVG zone, MNQ does not → ("short", ...)
7. `test_smt_fill_none_when_both_instruments_reach_zone` — both reach → None

**_build_signal_from_bar with fvg_zone (2 tests)**:
8. `test_signal_includes_fvg_fields_when_zone_provided` — fvg_zone not None → signal["fvg_high"] matches
9. `test_signal_fvg_none_when_no_zone` — fvg_zone=None → fvg_high/fvg_low are None

**manage_position Layer B (3 tests)**:
10. `test_layer_b_enters_when_price_retraces_to_fvg_long` — bar retraces into fvg zone → position["layer_b_entered"]=True, contracts increased
11. `test_layer_b_does_not_enter_twice` — layer_b_entered already True → contracts unchanged on next FVG bar
12. `test_layer_b_stop_tightens_to_fvg_boundary_on_entry` — stop updated to fvg_low - STRUCTURAL_STOP_BUFFER_PTS for long

**manage_position partial exit (3 tests)**:
13. `test_partial_exit_fires_when_price_reaches_level_long` — bar High ≥ partial_exit_level → "partial_exit" returned
14. `test_partial_exit_does_not_fire_twice` — partial_done=True → no second partial_exit
15. `test_partial_exit_disabled_when_constant_false` — PARTIAL_EXIT_ENABLED=False → "hold" even at level

**screen_session SMT-optional (2 tests)**:
16. `test_smt_optional_returns_signal_on_displacement` — SMT_OPTIONAL=True, large body, no wick SMT → signal returned
17. `test_smt_optional_disabled_blocks_displacement_entry` — SMT_OPTIONAL=False → no signal without wick SMT

**Backtest integration (2 tests)**:
18. `test_partial_exit_produces_two_trade_records` — backtest run on synthetic bars produces partial + final trade records
19. `test_layer_a_initial_contracts_less_than_total` — TWO_LAYER_POSITION=True, LAYER_A_FRACTION=0.5 → initial contracts = ceil(total/2)

**TSV schema (1 test)**:
20. `test_write_trades_tsv_includes_plan2_fvg_fields` — `_write_trades_tsv` output has `fvg_high`, `fvg_low`, `layer_b_entered` columns

- **VALIDATE**: `uv run python -m pytest tests/test_smt_position_arch.py -v`

#### Task 5.2: Regression check

- **WAVE**: 5
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [4.1, 4.2]
- **PROVIDES**: Confirm no regressions in existing test suite
- **IMPLEMENT**: Run full suite; confirm no new failures vs Plan 1 baseline. Fix any import errors from new constants/function additions if they surface in existing tests.
- **VALIDATE**: `uv run python -m pytest -q --tb=short 2>&1 | tail -5`

**Wave 5 Checkpoint**: `uv run python -m pytest -q --tb=short`

---

## TESTING STRATEGY

| What | Tool | Status |
|---|---|---|
| detect_fvg bullish / bearish / no-gap | pytest | ✅ Automated |
| detect_displacement on/off | pytest | ✅ Automated |
| detect_smt_fill bearish / both-fill | pytest | ✅ Automated |
| _build_signal_from_bar FVG zone fields | pytest | ✅ Automated |
| manage_position Layer B entry + stop tighten | pytest | ✅ Automated |
| manage_position partial exit on/off | pytest | ✅ Automated |
| screen_session SMT-optional on/off | pytest | ✅ Automated |
| Backtest: partial_exit produces 2 trade records | pytest | ✅ Automated |
| Backtest: Layer A contracts fraction | pytest | ✅ Automated |
| TSV fvg_high/fvg_low/layer_b_entered columns | pytest | ✅ Automated |
| No regressions in existing test suite | pytest | ✅ Automated |
| Backtest comparison: two-layer vs. single-layer P&L | Manual | ⚠️ Manual |

### Manual Test: Backtest comparison

**Why manual**: Requires `data/historical/MNQ.parquet` + `data/historical/MES.parquet`.

**Steps**:
1. Run baseline: `uv run python backtest_smt.py` (all Plan 2 flags False) → record `mean_test_pnl`
2. Set `TWO_LAYER_POSITION=True, FVG_ENABLED=True, FVG_LAYER_B_TRIGGER=True` → run again
3. Set `PARTIAL_EXIT_ENABLED=True` (on top of Step 2) → run again
4. Compare P&L and trade count across three runs

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ pytest (new) | 20 | 95% |
| ⚠️ Manual | 1 | 5% |
| **Total new** | 21 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Import checks

```bash
uv run python -c "from strategy_smt import detect_fvg, detect_displacement, detect_smt_fill, TWO_LAYER_POSITION, FVG_ENABLED, SMT_OPTIONAL, PARTIAL_EXIT_ENABLED, SMT_FILL_ENABLED; print('ok')"
uv run python -c "import backtest_smt; print('ok')"
uv run python -c "import signal_smt; print('ok')" 2>&1 | head -3
```

### Level 2: Unit tests (new)

```bash
uv run python -m pytest tests/test_smt_position_arch.py -v
```

### Level 3: Regression suite

```bash
uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py tests/test_smt_signal_quality.py tests/test_hypothesis_smt.py -q
uv run python -m pytest -q --tb=short
```

### Level 4: Manual backtest comparison

```bash
# All Plan 2 flags False (baseline)
uv run python backtest_smt.py 2>&1 | grep "mean_test_pnl\|total_test_trades"
# Then set TWO_LAYER_POSITION=True, FVG_ENABLED=True, FVG_LAYER_B_TRIGGER=True and repeat
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `detect_fvg(bars, bar_idx, "long")` returns `{"fvg_high", "fvg_low", "fvg_bar"}` when a bullish FVG ≥ FVG_MIN_SIZE_PTS exists before bar_idx; returns None when bars overlap or FVG_ENABLED=False
- [ ] `detect_fvg(bars, bar_idx, "short")` returns the bearish gap zone (bar3.High < bar1.Low) or None
- [ ] `detect_displacement()` returns True only when SMT_OPTIONAL=True and bar body ≥ MIN_DISPLACEMENT_PTS in the given direction
- [ ] `detect_smt_fill()` returns (direction, fvg_high, fvg_low) when MES fills a zone that MNQ has not; None when both fill or SMT_FILL_ENABLED=False
- [ ] Signal dict includes `fvg_high`, `fvg_low` (from fvg_zone param), `partial_exit_level` (midpoint of entry and TP when PARTIAL_EXIT_ENABLED=True)
- [ ] With `TWO_LAYER_POSITION=True`, initial position enters with `floor(total_contracts × LAYER_A_FRACTION)` contracts; `total_contracts_target` carries the full contract count
- [ ] With `FVG_LAYER_B_TRIGGER=True`, Layer B enters when price retraces into the FVG zone; position contracts increase to `total_contracts_target`; stop tightens to FVG boundary ± STRUCTURAL_STOP_BUFFER_PTS
- [ ] Layer B enters at most once per trade (guarded by `layer_b_entered` flag)
- [ ] `manage_position()` returns `"partial_exit"` when `PARTIAL_EXIT_ENABLED=True` and price reaches `partial_exit_level`; `partial_done` set to True afterward
- [ ] Partial exit fires at most once per trade (guarded by `partial_done` flag)
- [ ] With `SMT_OPTIONAL=True`, displacement entries fire when `detect_smt_divergence` returns None AND `detect_displacement` returns True; never fire on the same bar as a wick SMT
- [ ] `trades.tsv` contains `fvg_high`, `fvg_low`, `layer_b_entered`, `layer_b_entry_price`, `layer_b_contracts` columns
- [ ] All new constants default to `False`/`0.0`; all baseline behavior unchanged when all Plan 2 flags are False

### Error Handling
- [ ] `detect_fvg` with `bar_idx < 3` (insufficient lookback) returns None without error
- [ ] `manage_position` with `position["fvg_high"] = None` and `FVG_LAYER_B_TRIGGER=True` skips Layer B logic without error

### Regression
- [ ] All pre-existing tests pass: `uv run python -m pytest -q` shows no new failures vs Plan 1 baseline

### Out of Scope
- Hypothesis alignment analysis infrastructure — separate plan
- SMT fill integration into the full state machine (only `detect_smt_fill` function; IDLE detection uses it but no special tracking)
- Live IB Gateway testing — requires live session

---

## COMPLETION CHECKLIST

- [ ] Task 1.1: 10 constants added to strategy_smt.py STRATEGY TUNING block
- [ ] Task 1.2: detect_fvg(), detect_displacement(), detect_smt_fill() added after compute_overnight_range()
- [ ] Task 2.1: _build_signal_from_bar() gains fvg_zone param and fvg_high/fvg_low/partial_exit_level fields
- [ ] Task 2.2: screen_session() SMT-optional + fill detection + FVG zone passing
- [ ] Task 3.1: manage_position() Layer B entry mutation + partial_exit return value
- [ ] Task 4.1: backtest_smt.py — partial_exit handling, Layer B tracking, SMT-optional in IDLE, TSV fields
- [ ] Task 4.2: signal_smt.py — new function imports
- [ ] Task 5.1: tests/test_smt_position_arch.py created (20 tests passing)
- [ ] Task 5.2: existing tests pass (no regressions)
- [ ] Level 1–3 validation commands all pass
- [ ] **⚠️ Debug logs added during execution REMOVED**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**
