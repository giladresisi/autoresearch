# Feature: SMT Strategy — Reference Level Fix + Signal Quality (Plan 1 of 2)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Six targeted improvements to `strategy_smt.py` and `backtest_smt.py` that correct the most significant gaps between the current implementation and ICT theory:

1. **Midnight open as TP target** — the 12:00 AM ET price is the canonical ICT intraday reversion target, not the 9:30 RTH open. Adds `compute_midnight_open()` and `MIDNIGHT_OPEN_AS_TP` flag.
2. **Structural stop placement** — stop beyond the divergence bar's wick extreme, not a ratio of TDO distance. Adds `STRUCTURAL_STOP_MODE` and `STRUCTURAL_STOP_BUFFER_PTS`.
3. **Thesis-invalidation exits** — three close-based early exits in `manage_position()` when the trade reason no longer exists: MSS (divergence extreme breached), CISD (midnight open breached), SMT invalidation (MNQ closes through its defended level).
4. **Overnight range in strategy** — `compute_overnight_range()` + `OVERNIGHT_SWEEP_REQUIRED` gate + `OVERNIGHT_RANGE_AS_TP` alternative target.
5. **Silver bullet window filter** — `SILVER_BULLET_WINDOW_ONLY` restricts new signal acceptance to 09:50–10:10 ET, the highest-probability ICT macro window.
6. **Hidden SMT (body/close-based divergence)** — `HIDDEN_SMT_ENABLED` adds close-based SMT detection as an alternative to wick-based, exposing `smt_type` in all trade records.

All new constants default to `False`/`0.0`. Existing baseline behavior is unchanged unless a flag is explicitly enabled.

**Plan 2** (separate plan, depends on this one) covers: two-layer position model, partial exits, FVG infrastructure, pyramiding, and SMT-optional gate.

## User Story

As a quant running the SMT strategy  
I want the strategy to use ICT-correct reference levels, structural stops, and early exits  
So that I can test whether aligning with ICT theory improves walk-forward P&L before adding architectural complexity

## Problem Statement

Five concrete misalignments with ICT theory exist in the current code: wrong TP reference level (9:30 open vs. midnight open), ratio-based stops that ignore the structural sweep level, no early exits when trade thesis is invalidated, overnight levels computed only in the hypothesis module (not in strategy execution), and no distinction between the high-probability 9:50–10:10 macro window and the noisy 9:30 spike.

## Solution Statement

Add all six features as opt-in constants, expand `detect_smt_divergence` to return a 5-tuple, enrich the signal dict and position dict with structural fields, and add three new exit return values to `manage_position()`. Update all callers in backtest and signal paths.

## Feature Metadata

**Feature Type**: Enhancement  
**Complexity**: Medium  
**Primary Systems Affected**: `strategy_smt.py`, `backtest_smt.py`, `signal_smt.py`  
**Dependencies**: None — no new packages  
**Breaking Changes**: `detect_smt_divergence` return changes from 3-tuple to 5-tuple; all callers must be updated in this plan

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `strategy_smt.py` (lines 1–135) — all existing constants; new ones go in the STRATEGY TUNING block
- `strategy_smt.py` (lines 205–262) — `detect_smt_divergence()` — extend to 5-tuple and add hidden SMT branch
- `strategy_smt.py` (lines 316–330) — `compute_tdo()` — model for `compute_midnight_open()` and `compute_overnight_range()`
- `strategy_smt.py` (lines 471–527) — `_build_signal_from_bar()` — add structural stop logic and new signal dict fields
- `strategy_smt.py` (lines 531–613) — `manage_position()` — add 3 invalidation exit checks before stop check
- `strategy_smt.py` (lines 409–468) — `screen_session()` — update unpack, add overnight/silver-bullet gates, pass new fields
- `backtest_smt.py` (lines 11–19) — import list — add `compute_midnight_open`, `compute_overnight_range`
- `backtest_smt.py` (lines 244–268) — day loop preamble — add `day_midnight_open`, `day_overnight_range` computation
- `backtest_smt.py` (lines 330–435) — state machine WAITING_FOR_ENTRY / REENTRY_ELIGIBLE / IDLE — update unpack, pass new fields
- `backtest_smt.py` (lines 577–595) — `_write_trades_tsv()` — add `smt_type` to fieldnames
- `signal_smt.py` (lines 18, 451–456) — import and `compute_tdo` call site
- `tests/test_smt_strategy.py` (lines 42–100) — existing `detect_smt_divergence` tests; all unpacks must become 5-element
- `tests/test_smt_backtest.py` (lines 453–469) — `exit_market` test pattern to follow for invalidation exit tests

### New Files to Create

- `tests/test_smt_signal_quality.py` — all new tests for Plan 1 features

### Patterns to Follow

**Return tuple changes**: Update every `direction, sweep, miss = detect_smt_divergence(...)` to `direction, sweep, miss, smt_type, defended = detect_smt_divergence(...)`. Search: `_smt_sweep, _smt_miss = _smt` and `_smt_sweep, _smt_miss`.

**New exit values**: Follow existing pattern — `manage_position` returns a string; harness already handles unknown exit types via the generic close path. Add `exit_invalidation_mss`, `exit_invalidation_cisd`, `exit_invalidation_smt` as new strings.

**Constants placement**: All new constants go in the `# ══ STRATEGY TUNING ══` block in `strategy_smt.py`, one group per feature with a blank line between groups.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation — Parallel                          │
│ Task 1.1: ADD 10 constants to strategy_smt.py          │
│ Task 1.2: ADD compute_midnight_open() +                │
│           compute_overnight_range()                    │
└────────────────────────────────────────────────────────┘
                           ↓
┌────────────────────────────────────────────────────────┐
│ WAVE 2: Core signal changes — Parallel                 │
│ Task 2.1: MODIFY detect_smt_divergence() (5-tuple)     │
│ Task 2.2: MODIFY _build_signal_from_bar() (struct stop)│
└────────────────────────────────────────────────────────┘
                           ↓
┌────────────────────────────────────────────────────────┐
│ WAVE 3: Position + session — Parallel                  │
│ Task 3.1: MODIFY manage_position() (3 exit types)      │
│ Task 3.2: MODIFY screen_session() (new gates + unpack) │
└────────────────────────────────────────────────────────┘
                           ↓
┌────────────────────────────────────────────────────────┐
│ WAVE 4: Harness + live — Sequential                    │
│ Task 4.1: UPDATE backtest_smt.py (all callers + state) │
│ Task 4.2: UPDATE signal_smt.py (import + tdo call)     │
└────────────────────────────────────────────────────────┘
                           ↓
┌────────────────────────────────────────────────────────┐
│ WAVE 5: Tests — Parallel                               │
│ Task 5.1: CREATE tests/test_smt_signal_quality.py      │
│ Task 5.2: UPDATE existing tests (fix 3→5 tuple unpacks)│
└────────────────────────────────────────────────────────┘
```

### Interface Contracts

**Contract 1**: `detect_smt_divergence` now returns `Optional[tuple[str, float, float, str, float]]`:  
`(direction, sweep_pts, miss_pts, smt_type, smt_defended_level)`  
- `smt_type`: `"wick"` (current behavior) or `"body"` (new hidden SMT)  
- `smt_defended_level`: the MNQ session extreme MNQ failed to match (session high for shorts, session low for longs)

**Contract 2**: `_build_signal_from_bar` gains new keyword parameters:  
`divergence_bar_high`, `divergence_bar_low`, `midnight_open`, `smt_defended_level`, `smt_type`  
All have safe defaults so existing callers compile; update backtest/screen_session callers to pass them.

**Contract 3**: `manage_position` new return strings: `"exit_invalidation_mss"`, `"exit_invalidation_cisd"`, `"exit_invalidation_smt"`. The harness closes the position on any of these the same as `exit_stop`.

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation

#### Task 1.1: ADD constants to `strategy_smt.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1, 2.2, 3.1, 3.2]
- **PROVIDES**: 10 new tunable constants, all defaulting to off
- **IMPLEMENT**: In the `# ══ STRATEGY TUNING ══` block, add six grouped constant blocks. Each group gets a header comment explaining the optimizer search space.

```python
# Midnight open as TP target (replaces 9:30 RTH open / TDO).
# ICT canonical intraday reversion target = first 1m bar at/after 00:00 ET.
# Optimizer search space: [True, False]
MIDNIGHT_OPEN_AS_TP: bool = False

# Structural stop placement: stop beyond the divergence bar's wick extreme.
# When False: ratio × |entry - TP| (current behavior).
# STRUCTURAL_STOP_BUFFER_PTS: points beyond the wick to place the stop.
# Optimizer search space: STRUCTURAL_STOP_MODE [True, False];
#   STRUCTURAL_STOP_BUFFER_PTS [1.0, 2.0, 3.0, 5.0]
STRUCTURAL_STOP_MODE: bool = False
STRUCTURAL_STOP_BUFFER_PTS: float = 2.0

# Thesis-invalidation exits (close-based; fires before stop check).
# MSS: close beyond the divergence bar's wick extreme on the entry instrument.
# CISD: close beyond the midnight open (requires MIDNIGHT_OPEN_AS_TP = True).
# SMT: close beyond the MNQ level that defined the divergence (defended level).
# All optimizer search space: [True, False]
INVALIDATION_MSS_EXIT: bool = False
INVALIDATION_CISD_EXIT: bool = False
INVALIDATION_SMT_EXIT: bool = False

# Overnight sweep gate: require overnight H (for shorts) or L (for longs)
# to have been swept before the signal bar fires.
# OVERNIGHT_RANGE_AS_TP: use opposite overnight extreme as TP instead of TDO/midnight.
# Optimizer search space: [True, False]
OVERNIGHT_SWEEP_REQUIRED: bool = False
OVERNIGHT_RANGE_AS_TP: bool = False

# Silver Bullet window: restrict new divergence detection to 09:50–10:10 ET.
# Re-entries allowed outside window if original divergence was inside it.
# Optimizer search space: [True, False]
SILVER_BULLET_WINDOW_ONLY: bool = False
SILVER_BULLET_START = "09:50"
SILVER_BULLET_END   = "10:10"

# Hidden SMT: body/close-based divergence (MES close new session extreme,
# MNQ close does not). Only fires if wick SMT did not fire on the same bar.
# Optimizer search space: [True, False]
HIDDEN_SMT_ENABLED: bool = False
```

- **VALIDATE**: `uv run python -c "import strategy_smt; print(strategy_smt.MIDNIGHT_OPEN_AS_TP)"`

#### Task 1.2: ADD `compute_midnight_open()` and `compute_overnight_range()` to `strategy_smt.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: []
- **BLOCKS**: [2.2, 3.2, 4.1]
- **PROVIDES**: Two new public functions following `compute_tdo()` style
- **IMPLEMENT**: Insert immediately after `compute_tdo()` (after line ~330).

```python
def compute_midnight_open(mnq_bars: pd.DataFrame, date: datetime.date) -> float | None:
    """Return the Open of the first 1m/5m bar at or after 00:00 ET on date.

    ICT canonical intraday reversion target. Falls back to the first bar on
    that date if no bar exists exactly at midnight (e.g. on 5m resampled data).
    Returns None if no bars exist for the date.
    """
    day_bars = mnq_bars[mnq_bars.index.date == date]
    if day_bars.empty:
        return None
    midnight = pd.Timestamp(f"{date} 00:00:00", tz="America/New_York")
    after_midnight = day_bars[day_bars.index >= midnight]
    if not after_midnight.empty:
        return float(after_midnight.iloc[0]["Open"])
    return float(day_bars.iloc[0]["Open"])


def compute_overnight_range(mnq_bars: pd.DataFrame, date: datetime.date) -> dict:
    """Return overnight high/low: bars on date with time < 09:00 ET.

    Returns {"overnight_high": float, "overnight_low": float} or
    {"overnight_high": None, "overnight_low": None} if no pre-9am bars exist.
    """
    mask = (
        (mnq_bars.index.date == date) &
        (mnq_bars.index.time < pd.Timestamp("2000-01-01 09:00:00").time())
    )
    bars = mnq_bars[mask]
    if bars.empty:
        return {"overnight_high": None, "overnight_low": None}
    return {
        "overnight_high": float(bars["High"].max()),
        "overnight_low":  float(bars["Low"].min()),
    }
```

- **VALIDATE**: `uv run python -c "from strategy_smt import compute_midnight_open, compute_overnight_range; print('ok')"`

**Wave 1 Checkpoint**: `uv run python -m pytest tests/test_smt_strategy.py -q`

---

### WAVE 2: Core signal changes

#### Task 2.1: MODIFY `detect_smt_divergence()` — 5-tuple return + hidden SMT

- **WAVE**: 2
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.2, 4.1, 5.1, 5.2]
- **PROVIDES**: 5-tuple `(direction, sweep_pts, miss_pts, smt_type, smt_defended_level)`
- **IMPLEMENT**:
  1. In both bearish and bullish return statements, add `smt_type = "wick"` and return the 5-tuple. `smt_defended_level` = `mnq_session_high` (for shorts) or `mnq_session_low` (for longs).
  2. After the wick checks, when `HIDDEN_SMT_ENABLED` is True, add body/close-based checks. For bearish hidden: `cur_mes["Close"] > mes_session_close_high AND cur_mnq["Close"] <= mnq_session_close_high` where `mes_session_close_high = mes_bars["Close"].iloc[session_slice].max()`. Mirror for bullish. Return `smt_type = "body"` with the appropriate defended level.
  3. The wick SMT takes priority — only check hidden SMT if wick SMT returned None.

New structure (pseudocode):
```python
# ... existing wick SMT checks unchanged, but return 5-tuple:
return ("short", smt_sweep, mnq_miss, "wick", mnq_session_high)
return ("long",  smt_sweep, mnq_miss, "wick", mnq_session_low)

# After both wick checks return None, if HIDDEN_SMT_ENABLED:
if HIDDEN_SMT_ENABLED:
    mes_close_session_high = mes_bars["Close"].iloc[session_slice].max()
    mnq_close_session_high = mnq_bars["Close"].iloc[session_slice].max()
    mes_close_session_low  = mes_bars["Close"].iloc[session_slice].min()
    mnq_close_session_low  = mnq_bars["Close"].iloc[session_slice].min()
    if cur_mes["Close"] > mes_close_session_high and cur_mnq["Close"] <= mnq_close_session_high:
        # apply MIN_SMT_SWEEP/MISS filters on close distances
        return ("short", ..., "body", mnq_close_session_high)
    if cur_mes["Close"] < mes_close_session_low and cur_mnq["Close"] >= mnq_close_session_low:
        return ("long", ..., "body", mnq_close_session_low)
return None
```

- **VALIDATE**: `uv run python -c "import strategy_smt; r = strategy_smt.detect_smt_divergence; print('ok')"`

#### Task 2.2: MODIFY `_build_signal_from_bar()` — structural stop + new signal fields

- **WAVE**: 2
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [1.1, 1.2]
- **BLOCKS**: [3.1, 3.2, 4.1, 5.1]
- **PROVIDES**: Signal dict with `divergence_bar_high`, `divergence_bar_low`, `midnight_open`, `smt_defended_level`, `smt_type`; structural stop when `STRUCTURAL_STOP_MODE` is True
- **IMPLEMENT**: Add new keyword parameters with safe defaults:

```python
def _build_signal_from_bar(
    bar, ts, direction, tdo,
    smt_sweep_pts=0.0, smt_miss_pts=0.0, divergence_bar_idx=-1,
    # New Plan 1 fields:
    divergence_bar_high=None, divergence_bar_low=None,
    midnight_open=None, smt_defended_level=None, smt_type="wick",
):
```

Stop placement logic — replace the ratio block with:
```python
if STRUCTURAL_STOP_MODE and divergence_bar_high is not None and divergence_bar_low is not None:
    if direction == "short":
        stop_price = divergence_bar_high + STRUCTURAL_STOP_BUFFER_PTS
    else:
        stop_price = divergence_bar_low - STRUCTURAL_STOP_BUFFER_PTS
else:
    stop_ratio = SHORT_STOP_RATIO if direction == "short" else LONG_STOP_RATIO
    if direction == "short":
        stop_price = entry_price + stop_ratio * distance_to_tdo
    else:
        stop_price = entry_price - stop_ratio * distance_to_tdo
```

Add to returned dict:
```python
"divergence_bar_high":  divergence_bar_high,
"divergence_bar_low":   divergence_bar_low,
"midnight_open":        midnight_open,
"smt_defended_level":   smt_defended_level,
"smt_type":             smt_type,
```

- **VALIDATE**: `uv run python -c "import strategy_smt; print(strategy_smt._build_signal_from_bar.__doc__ or 'ok')"`

**Wave 2 Checkpoint**: `uv run python -m pytest tests/test_smt_strategy.py -q --tb=short 2>&1 | tail -10`

---

### WAVE 3: Position management + session scanner

#### Task 3.1: MODIFY `manage_position()` — 3 invalidation exits

- **WAVE**: 3
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [1.1, 2.2]
- **BLOCKS**: [4.1, 5.1]
- **PROVIDES**: Three new close-based exit signals before the stop check
- **IMPLEMENT**: Insert immediately before the `stop = position["stop_price"]` line. All checks are close-based (not wick) and gated by their respective constants:

```python
# ── Thesis-invalidation exits (close-based; fire before stop check) ───────
if INVALIDATION_MSS_EXIT:
    div_low  = position.get("divergence_bar_low")
    div_high = position.get("divergence_bar_high")
    if direction == "long" and div_low is not None and current_bar["Close"] < div_low:
        return "exit_invalidation_mss"
    if direction == "short" and div_high is not None and current_bar["Close"] > div_high:
        return "exit_invalidation_mss"

if INVALIDATION_CISD_EXIT:
    mo = position.get("midnight_open")
    if mo is not None:
        if direction == "long"  and current_bar["Close"] < mo:
            return "exit_invalidation_cisd"
        if direction == "short" and current_bar["Close"] > mo:
            return "exit_invalidation_cisd"

if INVALIDATION_SMT_EXIT:
    defended = position.get("smt_defended_level")
    if defended is not None:
        if direction == "long"  and current_bar["Close"] < defended:
            return "exit_invalidation_smt"
        if direction == "short" and current_bar["Close"] > defended:
            return "exit_invalidation_smt"
```

Harness side: in `backtest_smt.py`, the three new exit strings are handled exactly like `exit_stop` — they call `_build_trade_record` with the new string as `exit_result`. Since `_build_trade_record` already falls through to `bar["Close"]` for any non-tp/non-stop result, no changes to `_build_trade_record` are needed.

- **VALIDATE**: `uv run python -c "import strategy_smt; print('ok')"`

#### Task 3.2: MODIFY `screen_session()` — new gates + updated field passing

- **WAVE**: 3
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [1.1, 1.2, 2.1, 2.2]
- **BLOCKS**: [4.2, 5.1]
- **PROVIDES**: `screen_session` compatible with all Plan 1 features
- **IMPLEMENT**:
  1. Update signature to accept `midnight_open=None, overnight_range=None` so callers can pass them in.
  2. Update `detect_smt_divergence` unpack from 3-tuple to 5-tuple: `direction, _smt_sweep, _smt_miss, _smt_type, _smt_defended = _smt`
  3. After the `TRADE_DIRECTION` filter, add overnight sweep gate:
     ```python
     if OVERNIGHT_SWEEP_REQUIRED and overnight_range is not None:
         oh = overnight_range.get("overnight_high")
         ol = overnight_range.get("overnight_low")
         # For short: overnight high must have been swept (session high > oh) before this bar
         if direction == "short" and oh is not None:
             pre_session_high = mnq_reset["High"].iloc[:bar_idx].max() if bar_idx > 0 else 0
             if pre_session_high <= oh:
                 continue
         if direction == "long" and ol is not None:
             pre_session_low = mnq_reset["Low"].iloc[:bar_idx].min() if bar_idx > 0 else float("inf")
             if pre_session_low >= ol:
                 continue
     ```
  4. After direction filter, add silver bullet gate:
     ```python
     if SILVER_BULLET_WINDOW_ONLY:
         bar_t = mnq_bars.index[bar_idx].strftime("%H:%M")
         if not (SILVER_BULLET_START <= bar_t < SILVER_BULLET_END):
             continue
     ```
  5. Read divergence bar extremes from the current bar (bar_idx):
     ```python
     _div_bar = mnq_reset.iloc[bar_idx]
     _div_bar_high = float(_div_bar["High"])
     _div_bar_low  = float(_div_bar["Low"])
     ```
  6. Compute TP target: when `OVERNIGHT_RANGE_AS_TP` and overnight_range is available, use opposite extreme. Else when `MIDNIGHT_OPEN_AS_TP` and midnight_open is not None, use midnight_open. Else use tdo (existing behavior).
  7. Pass new fields to `_build_signal_from_bar`: `divergence_bar_high=_div_bar_high, divergence_bar_low=_div_bar_low, midnight_open=midnight_open, smt_defended_level=_smt_defended, smt_type=_smt_type`.

- **VALIDATE**: `uv run python -c "from strategy_smt import screen_session; print('ok')"`

**Wave 3 Checkpoint**: `uv run python -m pytest tests/test_smt_strategy.py -q --tb=short 2>&1 | tail -10`

---

### WAVE 4: Harness + live path

#### Task 4.1: UPDATE `backtest_smt.py` — all callers, per-day state, TSV

- **WAVE**: 4
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [3.1, 3.2]
- **BLOCKS**: [5.1, 5.2]
- **PROVIDES**: Fully wired backtest with all Plan 1 features
- **IMPLEMENT**:
  1. **Imports**: add `compute_midnight_open, compute_overnight_range` to the `from strategy_smt import` line.
  2. **Per-day preamble** (after `day_tdo` computation): add
     ```python
     _day_midnight_open = compute_midnight_open(mnq_df, day) if MIDNIGHT_OPEN_AS_TP else None
     _day_overnight = compute_overnight_range(mnq_df, day) if (OVERNIGHT_SWEEP_REQUIRED or OVERNIGHT_RANGE_AS_TP) else {"overnight_high": None, "overnight_low": None}
     ```
  3. **TP selection per day**: compute `_day_tp` once:
     ```python
     if OVERNIGHT_RANGE_AS_TP:
         oh = _day_overnight.get("overnight_high"); ol = _day_overnight.get("overnight_low")
         # direction not yet known here — pass overnight_range and let _build_signal_from_bar choose
         _day_tp = day_tdo  # fallback; actual TP resolved per signal in screen_session/build_signal
     elif MIDNIGHT_OPEN_AS_TP and _day_midnight_open is not None:
         _day_tp = _day_midnight_open
     else:
         _day_tp = day_tdo
     ```
     Then pass `_day_tp` (instead of `day_tdo`) to `_build_signal_from_bar` calls. The OVERNIGHT_RANGE_AS_TP resolution happens inside `screen_session`/`_build_signal_from_bar` since it depends on direction.
  4. **State variables**: add two new pending-state variables reset per day:
     ```python
     _pending_div_bar_high = 0.0
     _pending_div_bar_low  = 0.0
     _pending_smt_defended = 0.0
     _pending_smt_type     = "wick"
     ```
  5. **IDLE state** — update detect unpack and capture divergence bar fields:
     ```python
     direction, _smt_sweep, _smt_miss, _smt_type, _smt_defended = _smt
     _div_bar = mnq_reset.iloc[bar_idx]
     _pending_div_bar_high = float(_div_bar["High"])
     _pending_div_bar_low  = float(_div_bar["Low"])
     _pending_smt_defended = _smt_defended
     _pending_smt_type     = _smt_type
     ```
     Also add overnight sweep gate (same logic as screen_session Task 3.2 step 3).
     Also add silver bullet gate (same logic as screen_session Task 3.2 step 4).
  6. **WAITING_FOR_ENTRY / REENTRY_ELIGIBLE**: pass new fields to `_build_signal_from_bar`:
     ```python
     divergence_bar_high=_pending_div_bar_high,
     divergence_bar_low=_pending_div_bar_low,
     midnight_open=_day_midnight_open,
     smt_defended_level=_pending_smt_defended,
     smt_type=_pending_smt_type,
     ```
     After existing `signal["matches_hypothesis"]` assignments, add:
     ```python
     signal["divergence_bar_high"]  = _pending_div_bar_high
     signal["divergence_bar_low"]   = _pending_div_bar_low
     signal["midnight_open"]        = _day_midnight_open
     signal["smt_defended_level"]   = _pending_smt_defended
     signal["smt_type"]             = _pending_smt_type
     ```
  7. **New exit types in IN_TRADE**: The three `exit_invalidation_*` strings are already handled by `_build_trade_record` (they fall through to the generic `bar["Close"]` exit price path) — no changes needed.
  8. **`_write_trades_tsv`**: add `"smt_type"` after `"matches_hypothesis"` in fieldnames.
  9. **`_build_trade_record`**: no changes needed (unknown exit_result already falls to close price).

- **VALIDATE**: `uv run python -c "import backtest_smt; print('ok')"`

#### Task 4.2: UPDATE `signal_smt.py` — midnight open + 5-tuple import

- **WAVE**: 4
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [3.2]
- **BLOCKS**: [5.1]
- **PROVIDES**: Live signal path aligned with Plan 1
- **IMPLEMENT**:
  1. Add `compute_midnight_open, compute_overnight_range` to the import line.
  2. In the SCANNING state where `tdo = compute_tdo(...)` is called, add:
     ```python
     midnight_open_price = compute_midnight_open(_mnq_1m_df, today) if strategy_smt.MIDNIGHT_OPEN_AS_TP else None
     overnight_range = compute_overnight_range(_mnq_1m_df, today) if (strategy_smt.OVERNIGHT_SWEEP_REQUIRED or strategy_smt.OVERNIGHT_RANGE_AS_TP) else None
     ```
  3. Pass `midnight_open=midnight_open_price, overnight_range=overnight_range` to `screen_session()` call.

- **VALIDATE**: `uv run python -c "import signal_smt; print('ok')" 2>&1 | grep -v "IB\|ib_"` — expect `ok` (IB import may warn without connection)

**Wave 4 Checkpoint**: `uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -q --tb=short`

---

### WAVE 5: Tests

#### Task 5.1: CREATE `tests/test_smt_signal_quality.py`

- **WAVE**: 5
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [2.1, 2.2, 3.1, 3.2, 4.1]
- **PROVIDES**: 22 new tests covering all Plan 1 code paths
- **IMPLEMENT**: Create the file with these test groups:

**compute_midnight_open (3 tests)**:
1. `test_midnight_open_returns_first_bar_at_midnight` — bar at 00:00 present → returns its Open
2. `test_midnight_open_returns_first_bar_when_no_midnight` — no 00:00 bar → returns first bar's Open
3. `test_midnight_open_returns_none_on_empty` — empty df → None

**compute_overnight_range (3 tests)**:
4. `test_overnight_range_correct_hi_lo` — bars at 02:00 and 07:00 → correct high/low
5. `test_overnight_range_none_when_no_pre9am_bars` — all bars after 09:00 → both None
6. `test_overnight_range_excludes_0900_bar` — bar at exactly 09:00 is excluded (strict <)

**detect_smt_divergence 5-tuple (2 tests)**:
7. `test_smt_divergence_returns_5_tuple` — valid bearish divergence → tuple len 5, smt_type == "wick"
8. `test_smt_divergence_hidden_body_when_enabled` — wick fails but close diverges, HIDDEN_SMT_ENABLED=True → smt_type == "body"

**_build_signal_from_bar structural stop (3 tests)**:
9. `test_structural_stop_short` — STRUCTURAL_STOP_MODE=True, short; stop = div_bar_high + buffer
10. `test_structural_stop_long` — STRUCTURAL_STOP_MODE=True, long; stop = div_bar_low - buffer
11. `test_ratio_stop_unchanged_when_structural_disabled` — STRUCTURAL_STOP_MODE=False → original ratio behavior

**manage_position invalidation exits (6 tests)**:
12. `test_mss_exit_long_fires_on_close_below_div_low` — close < divergence_bar_low → exit_invalidation_mss
13. `test_mss_exit_does_not_fire_on_wick_only` — low < div_low but close > div_low → hold
14. `test_cisd_exit_long_fires_on_close_below_midnight_open` — close < midnight_open → exit_invalidation_cisd
15. `test_cisd_exit_disabled_when_constant_false` — INVALIDATION_CISD_EXIT=False → no cisd exit
16. `test_smt_invalidation_exit_long` — close < smt_defended_level → exit_invalidation_smt
17. `test_invalidation_fires_before_stop` — bar that would hit both invalidation + stop → invalidation wins (fires first)

**Silver bullet window (2 tests)**:
18. `test_silver_bullet_blocks_signal_outside_window` — divergence at 09:35 → no signal when SILVER_BULLET_WINDOW_ONLY=True
19. `test_silver_bullet_allows_signal_inside_window` — divergence at 09:55 → signal fires

**Overnight sweep gate (2 tests)**:
20. `test_overnight_sweep_required_blocks_when_not_swept` — OVERNIGHT_SWEEP_REQUIRED=True, overnight high not exceeded before signal bar → signal skipped
21. `test_overnight_sweep_required_passes_when_swept` — overnight high exceeded before signal bar → signal fires

**TSV schema (1 test)**:
22. `test_write_trades_tsv_includes_smt_type` — call `_write_trades_tsv` with a trade record containing `smt_type="wick"` → smt_type column present in output file

**Fixture helper** (add at top of file):
```python
def _make_5m_bars(date, n=30, base=20000.0, tz="America/New_York"):
    """Minimal 5m bar fixture for signal quality tests."""
    start = pd.Timestamp(f"{date} 09:00:00", tz=tz)
    idx = pd.date_range(start, periods=n, freq="5min")
    return pd.DataFrame({
        "Open": [base]*n, "High": [base+5]*n, "Low": [base-5]*n,
        "Close": [base]*n, "Volume": [1000.0]*n
    }, index=idx)
```

- **VALIDATE**: `uv run python -m pytest tests/test_smt_signal_quality.py -v`

#### Task 5.2: UPDATE existing tests — fix 3-tuple → 5-tuple unpacks

- **WAVE**: 5
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [2.1]
- **PROVIDES**: Existing test suite passes with the new 5-tuple signature
- **IMPLEMENT**: In `tests/test_smt_strategy.py`, find every line that unpacks `detect_smt_divergence` as a 3-tuple:
  ```python
  direction, sweep, miss = train_smt.detect_smt_divergence(...)
  ```
  Update to:
  ```python
  direction, sweep, miss, smt_type, defended = train_smt.detect_smt_divergence(...)
  ```
  Also update any `result == ("short", ...)` tuple equality assertions to match the 5-element tuple or use `result[0]` for direction-only checks.
  In `tests/test_smt_backtest.py`, check for any places that assert on `exit_type` values — no changes expected, but verify the `exit_invalidation_*` strings are handled generically (they are, via `exit_type_breakdown` dict).

- **VALIDATE**: `uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -q`

**Wave 5 Checkpoint**: `uv run python -m pytest -q --tb=short`

---

## TESTING STRATEGY

| What | Tool | Status |
|---|---|---|
| compute_midnight_open correctness | pytest | ✅ Automated |
| compute_overnight_range correctness | pytest | ✅ Automated |
| detect_smt_divergence 5-tuple + hidden SMT | pytest | ✅ Automated |
| Structural stop (short + long) | pytest | ✅ Automated |
| manage_position MSS exit (close-based, not wick) | pytest | ✅ Automated |
| manage_position CISD exit | pytest | ✅ Automated |
| manage_position SMT invalidation exit | pytest | ✅ Automated |
| Invalidation fires before stop | pytest | ✅ Automated |
| Silver bullet window filter | pytest | ✅ Automated |
| Overnight sweep gate | pytest | ✅ Automated |
| TSV smt_type column | pytest | ✅ Automated |
| Existing tests still pass (no regressions) | pytest | ✅ Automated |
| Live backtest comparison: midnight open vs TDO | Manual | ⚠️ Manual |

### Manual Test: Backtest comparison

**Why manual**: Requires `data/historical/MNQ.parquet` + `data/historical/MES.parquet` (Databento, not in repo).

**Steps**:
1. Run baseline: `uv run python backtest_smt.py` → record `mean_test_pnl`
2. Set `MIDNIGHT_OPEN_AS_TP = True` in `strategy_smt.py`
3. Run again → record new `mean_test_pnl`
4. Compare: if midnight open > baseline, it becomes the new default for Plan 2

**Expected**: Non-trivial difference (positive or negative ≥5%). A flat result suggests the two levels are often similar on the test data.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ pytest (new) | 22 | 92% |
| ✅ pytest (updated existing) | ~8 updated | — |
| ⚠️ Manual | 1 | 8% |
| **Total new** | 23 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Import checks

```bash
uv run python -c "from strategy_smt import compute_midnight_open, compute_overnight_range, MIDNIGHT_OPEN_AS_TP, STRUCTURAL_STOP_MODE, INVALIDATION_MSS_EXIT, SILVER_BULLET_WINDOW_ONLY, HIDDEN_SMT_ENABLED; print('ok')"
uv run python -c "import backtest_smt; print('ok')"
uv run python -c "import signal_smt; print('ok')" 2>&1 | head -3
```

### Level 2: Unit tests (new)

```bash
uv run python -m pytest tests/test_smt_signal_quality.py -v
```

### Level 3: Regression suite

```bash
uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py tests/test_hypothesis_smt.py -q
uv run python -m pytest -q --tb=short
```

### Level 4: Manual backtest comparison

```bash
# Baseline (MIDNIGHT_OPEN_AS_TP = False)
uv run python backtest_smt.py 2>&1 | grep "mean_test_pnl\|min_test_pnl\|total_test_trades"
# Then set MIDNIGHT_OPEN_AS_TP = True and repeat
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `compute_midnight_open(mnq_df, date)` returns the Open of the first bar at or after 00:00 ET on date; returns None on empty input
- [ ] `compute_overnight_range(mnq_df, date)` returns high/low of all bars before 09:00 ET; returns `{...: None}` when no such bars exist
- [ ] `detect_smt_divergence()` always returns None or a 5-element tuple `(direction, sweep_pts, miss_pts, smt_type, smt_defended_level)`
- [ ] With `HIDDEN_SMT_ENABLED=True`, body/close-based divergence fires on bars where wick SMT does not; `smt_type == "body"`
- [ ] With `STRUCTURAL_STOP_MODE=True`, stop is placed at `divergence_bar_high + STRUCTURAL_STOP_BUFFER_PTS` (short) or `divergence_bar_low - STRUCTURAL_STOP_BUFFER_PTS` (long)
- [ ] `manage_position()` returns `"exit_invalidation_mss"` on a CLOSE (not wick) beyond the divergence bar extreme when `INVALIDATION_MSS_EXIT=True`
- [ ] `manage_position()` returns `"exit_invalidation_cisd"` on a CLOSE beyond midnight open when `INVALIDATION_CISD_EXIT=True`
- [ ] `manage_position()` returns `"exit_invalidation_smt"` on a CLOSE beyond the defended level when `INVALIDATION_SMT_EXIT=True`
- [ ] All three invalidation exits fire before the stop check
- [ ] With `SILVER_BULLET_WINDOW_ONLY=True`, no new divergences are accepted outside 09:50–10:10 ET
- [ ] With `OVERNIGHT_SWEEP_REQUIRED=True`, signals are skipped unless the overnight high (short) or low (long) was swept before the signal bar
- [ ] `trades.tsv` contains a `smt_type` column with values `"wick"` or `"body"`
- [ ] All new constants default to `False`/`0.0`; existing baseline behavior is unchanged when all are False

### Error Handling
- [ ] `compute_midnight_open` returns None (no exception) on empty DataFrame
- [ ] `_build_signal_from_bar` with `divergence_bar_high=None` and `STRUCTURAL_STOP_MODE=True` falls back to ratio-based stop (None check in the structural stop branch)

### Regression
- [ ] All pre-existing tests pass: `uv run python -m pytest -q` shows no new failures vs baseline (519 collected)
- [ ] Existing `detect_smt_divergence` tests updated to 5-tuple unpack and passing

### Out of Scope
- Two-layer position model, partial exits, pyramiding — Plan 2
- FVG infrastructure and FVG entry confirmation — Plan 2
- SMT-optional (pure displacement entries) — Plan 2
- SMT fill signal — Plan 2
- TWO validity check, PD range case filter, IPDA cycles — hypothesis analysis plan

---

## COMPLETION CHECKLIST

- [ ] Task 1.1: 10 constants added to strategy_smt.py STRATEGY TUNING block
- [ ] Task 1.2: compute_midnight_open() and compute_overnight_range() added
- [ ] Task 2.1: detect_smt_divergence() returns 5-tuple; hidden SMT branch added
- [ ] Task 2.2: _build_signal_from_bar() structural stop + 5 new signal dict fields
- [ ] Task 3.1: manage_position() 3 invalidation exit checks added
- [ ] Task 3.2: screen_session() updated (unpack, gates, new field passing)
- [ ] Task 4.1: backtest_smt.py fully updated (imports, per-day state, unpack, TSV)
- [ ] Task 4.2: signal_smt.py updated (import + tdo/midnight call)
- [ ] Task 5.1: tests/test_smt_signal_quality.py created (22 tests passing)
- [ ] Task 5.2: existing tests updated (5-tuple unpacks)
- [ ] Level 1–3 validation commands all pass
- [ ] **⚠️ Debug logs added during execution REMOVED**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**
