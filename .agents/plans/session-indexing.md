# Feature: Session Indexing System

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Build a four-level session corpus index for the SMT v2 backtest system. Currently sessions produce `events.jsonl` and `trades.tsv` per date in `data/regression/{DATE}/` — correct but not queryable. As the corpus grows past 500+ sessions, answering strategy-update questions requires reading every file manually. The index makes those queries instant pandas operations.

Four levels:
- **Level 1 (Session)**: one Parquet row per date, ~25 scalar fields from events + bars + trades
- **Level 2 (Trade)**: one Parquet row per trade, extends trades.tsv with context fields from events + bars
- **Level 3 (Movement)**: per-session YAML + Parquet; splits each session into behavioral movement periods (up/down/sideways × clean/choppy) with endpoints anchored to liquidity levels
- **Trade-Movement Links**: per-session YAML joining each trade to overlapping movements, with auto-generated notes

The indexer integrates with `regression.py` so new sessions are indexed automatically after each regression run.

## User Story

As a strategy developer working on the SMT v2 pipeline,
I want to query the session corpus with pandas filters,
So that Phase 1 auto-selection for strategy updates takes seconds instead of 45 minutes of manual file reading.

## Problem Statement

Phase 1 of `strategy-update-method.md` requires finding sessions that isolate specific failure patterns — both execution failures (entry timing, stop width) and mechanism failures (wrong hypothesis direction in `hypothesis.py`/`trend.py`/`daily.py`). Without an index this means reading every `events.jsonl` across 500+ sessions. The two failure types can only be distinguished by cross-referencing events with bar data, which is infeasible at scale.

## Solution Statement

A pure-function `indexer.py` reads session files and outputs:
1. `data/sessions_index.parquet` — one row per date (Level 1)
2. `data/trades_index.parquet` — one row per trade (Level 2)
3. `data/movements_index.parquet` — one row per movement period (Level 3 summary)
4. `sessions/{DATE}/movements.yaml` — human-readable movement narrative (Level 3 detail)
5. `sessions/{DATE}/trade_movement_links.yaml` — trade-movement links with notes

`regression.py` automatically calls `build_index()` after each run, keeping the index fresh.

## Feature Metadata

**Feature Type**: New Capability
**Complexity**: High
**Primary Systems Affected**: New `indexer.py`, `regression.py` (hook), `pyproject.toml` (pyyaml dep)
**Dependencies**: `pandas`, `pyarrow` (already present); `pyyaml>=6.0` (new — must be added)
**Breaking Changes**: No — additive only

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `indexing.md` — Full design spec; all fields, movement algorithm, YAML format, storage layout
- `regression.py` (lines 1–157) — Date-loop runner pattern; `run_regression()` is the integration point
- `sessions/2026-04-08/events.jsonl` — Real events.jsonl; understand all event kinds and fields
- `sessions/2026-04-08/levels.json` — Levels JSON structure (`liquidities` list + `all_time_high`)
- `data/regression/2026-04-08/trades.tsv` — Real trades.tsv format (tab-separated, header row)
- `data/regression/2026-04-08/levels.json` — Same structure as `sessions/{DATE}/levels.json`
- `backtest_smt.py` (lines 1154–1240) — `run_backtest_v2()` session window definition
- `session_times.py` — `SESSION_OPEN` constant (NY session start time string)
- `tests/test_smt_regression.py` — Test pattern for file-based I/O with `tmp_path`
- `tests/test_smt_strategy_v2.py` (lines 1–80) — Test isolation pattern with monkeypatch

### New Files to Create

- `indexer.py` — Pure-function indexer (all levels, links, `build_index` runner, CLI)
- `tests/test_indexer.py` — Unit and integration tests

### Files to Modify

- `regression.py` — Add `build_index()` call at end of `run_regression()`
- `pyproject.toml` — Add `"pyyaml>=6.0"` dependency

### Data Source Mapping

| Data | Location |
|---|---|
| 1m bars | `sessions/{DATE}/MNQ_1m.parquet` |
| Events | `sessions/{DATE}/events.jsonl` (fallback: `data/regression/{DATE}/events.jsonl`) |
| Levels | `sessions/{DATE}/levels.json` (fallback: `data/regression/{DATE}/levels.json`) |
| Trades | `data/regression/{DATE}/trades.tsv` (only exists here, not in sessions/) |

### Patterns to Follow

**Date-loop**: `regression.py:run_regression()` — iterate dates, load per-date files, write outputs
**File loading**: `pd.read_parquet()` for bars; `json.loads()` line-by-line for events.jsonl; `pd.read_csv(sep='\t')` for trades
**Test isolation**: `tmp_path` + monkeypatch to redirect path constants (see `test_smt_strategy_v2.py`)
**No side effects inside `index_session()`**: all I/O goes through `build_index()` and YAML writers

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌───────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (fully parallel — different files)      │
├───────────────────────────────────────────────────────────┤
│ Task 1.1: Create indexer.py   │ Task 1.2: Create          │
│ scaffold + helpers + loaders  │ tests/test_indexer.py     │
│ + _anchor_price               │ fixtures only             │
└───────────────────────────────────────────────────────────┘
                         ↓
┌───────────────────────────────────────────────────────────┐
│ WAVE 2: Core Levels 1+2 (fully parallel — different files) │
├───────────────────────────────────────────────────────────┤
│ Task 2.1: Add L1+L2 logic     │ Task 2.2: Add L1+L2 unit  │
│ to indexer.py                 │ tests to test_indexer.py  │
└───────────────────────────────────────────────────────────┘
                         ↓
┌───────────────────────────────────────────────────────────┐
│ WAVE 3: Level 3 + Links (fully parallel — different files) │
├───────────────────────────────────────────────────────────┤
│ Task 3.1: Add movement        │ Task 3.2: Add L3+link      │
│ detection + links + YAML      │ tests to test_indexer.py  │
│ to indexer.py                 │                           │
└───────────────────────────────────────────────────────────┘
                         ↓
┌───────────────────────────────────────────────────────────┐
│ WAVE 4: Runner + Integration (parallel — different files)  │
├───────────────────────────────────────────────────────────┤
│ Task 4.1: build_index + CLI   │ Task 4.2: regression.py   │
│ + Parquet output in indexer.py│ integration hook          │
└───────────────────────────────────────────────────────────┘
                         ↓
┌───────────────────────────────────────────────────────────┐
│ WAVE 5: Integration Validation (sequential)                │
├───────────────────────────────────────────────────────────┤
│ Task 5.1: Full test suite + real-session end-to-end       │
└───────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: 1.1 writes `indexer.py`, 1.2 writes `tests/test_indexer.py`
**Wave 2 — Fully Parallel**: 2.1 extends `indexer.py`, 2.2 extends `tests/test_indexer.py`
**Wave 3 — Fully Parallel**: 3.1 extends `indexer.py`, 3.2 extends `tests/test_indexer.py`
**Wave 4 — Parallel**: 4.1 extends `indexer.py`, 4.2 modifies `regression.py`
**Wave 5 — Sequential**: must run after Wave 4

### Interface Contracts

**Contract 1**: `index_session(date: str) -> dict` returns `{session: dict, trades: list[dict], movements: list[dict], links: list[dict]}`

**Contract 2**: `_anchor_price(price: float, sorted_levels: list[tuple[str, float]]) -> str`
- sorted_levels: `[(name, price), ...]` ascending by price, includes ATH
- Returns: `"12.5 pts above week_low and 8.0 pts below TDO"` / `"5.0 pts above ATH"` / `"at week_low"`

**Contract 3**: Movement period dict shape:
```python
{
    "movement_id": "{date}_M{n}",   "start_time": "HH:MM",    "end_time": "HH:MM",
    "direction": "up|down|sideways", "type": "clean|choppy",
    "start_price": float,            "end_price": float,
    "pts_net": float,                "displacement_ratio": float,
    "start_anchor": str,             "end_anchor": str,
    "trade_ids": list[str],
}
```

**Contract 4**: Link dict shape:
```python
{
    "trade_id": str, "movement_id": str,
    "entry_pct_into_movement": float, "exit_pct_into_movement": float,
    "direction_aligned": bool, "note": str,
}
```

**Mock for parallel test agent**: Wave 2 and 3 test agents can write tests against the Contract specs above without waiting for code — tests will fail initially and pass once the code wave merges.

### Synchronization Checkpoints

**After Wave 2**: `uv run pytest tests/test_indexer.py -k "level1 or level2 or anchor" -v`
**After Wave 3**: `uv run pytest tests/test_indexer.py -v`
**After Wave 4**: `uv run pytest tests/test_indexer.py tests/test_smt_regression.py -v`
**After Wave 5**: Full validation commands (see Level 4 below)

---

## STEP-BY-STEP TASKS

---

### WAVE 1: Foundation

#### Task 1.1: CREATE `indexer.py` (scaffold + helpers)

- **WAVE**: 1
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1, 3.1, 4.1]
- **PROVIDES**: Module structure, imports, data loaders, `_anchor_price`, stub `index_session()`

**IMPLEMENT**:

1. **Add `pyyaml>=6.0` to `pyproject.toml`** dependencies. Run `uv sync`.

2. **Create `indexer.py`** with header docstring, imports (`json`, `pathlib`, `argparse`, `sys`, `pandas`, `yaml`), and constants:
```python
SESSIONS_DIR   = Path("sessions")
REGRESSION_DIR = Path("data") / "regression"
INDEX_DIR      = Path("data")
SESSION_INDEX_PATH   = INDEX_DIR / "sessions_index.parquet"
TRADES_INDEX_PATH    = INDEX_DIR / "trades_index.parquet"
MOVEMENTS_INDEX_PATH = INDEX_DIR / "movements_index.parquet"
NY_TZ = "America/New_York"
DISPLACEMENT_CLEAN_THRESHOLD = 0.65
MOVEMENT_MIN_BARS = 5
```

3. **Implement file loaders**:
   - `_load_events(date: str) -> list[dict]` — read `sessions/{date}/events.jsonl` line-by-line, fallback to `data/regression/{date}/events.jsonl`; return `[]` if neither exists
   - `_load_trades(date: str) -> list[dict]` — `pd.read_csv(REGRESSION_DIR/date/"trades.tsv", sep='\t')`, handle empty file; return list of dicts
   - `_load_bars(date: str) -> pd.DataFrame` — `pd.read_parquet(SESSIONS_DIR/date/"MNQ_1m.parquet")`; filter to NY session window using `session_times.SESSION_OPEN` through `16:00:00` ET
   - `_load_levels(date: str) -> tuple[list[tuple[str,float]], float]` — parse levels.json; return `(sorted_levels, ath)` where sorted_levels is `[(name, price)]` from `kind=="level"` entries plus `("ATH", ath)`, sorted ascending by price

4. **Implement `_anchor_price(price, sorted_levels) -> str`**:
   - Find floor (largest price ≤ query) and ceiling (smallest price > query) from sorted_levels
   - `"at {name}"` if within 0.01 pts of a level
   - `"{x:.2f} pts above {floor} and {y:.2f} pts below {ceiling}"` if both exist and x≠y
   - `"{x:.2f} pts above {floor}"` if no ceiling (above all levels including ATH)
   - `"{y:.2f} pts below {ceiling}"` if no floor (below all known levels — rare)

5. **Add `_parse_date_tokens(tokens)` utility** — copy pattern from `regression.py` for range expansion

6. **Add stub**: `def index_session(date: str) -> dict: raise NotImplementedError`

- **VALIDATE**: `uv run python -c "import indexer; print('ok')"`

---

#### Task 1.2: CREATE `tests/test_indexer.py` (fixtures only)

- **WAVE**: 1
- **AGENT_ROLE**: test-developer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.2, 3.2]
- **PROVIDES**: Reusable synthetic fixtures for all subsequent test waves

**IMPLEMENT**:

Create `tests/test_indexer.py` with imports and shared constants + fixtures:

```python
DATE = "2026-04-08"

SYNTHETIC_EVENTS = [
    # First hypothesis: down
    {"kind": "new-hypothesis", "time": f"{DATE}T09:20:00-04:00", "direction": "down",
     "direction_reason": {"rule": "rule2b", "smt_score": 0.0}, "entry_ranges": [], "targets": []},
    {"kind": "market-entry", "time": f"{DATE}T09:35:00-04:00",
     "direction": "down", "price": 100.0, "stop": 105.0, "slippage": 2.0},
    {"kind": "stopped-out", "time": f"{DATE}T09:40:00-04:00", "direction": "down", "price": 105.0},
    # Cautious reversal
    {"kind": "market-close", "time": f"{DATE}T10:00:00-04:00",
     "direction": "down", "price": 102.0, "reason": "cautious-1m-break",
     "close_reason": "2nd-cautious (TDO)", "slippage": 2.0},
    # Second hypothesis: up (direction flip)
    {"kind": "new-hypothesis", "time": f"{DATE}T10:05:00-04:00", "direction": "up",
     "direction_reason": {"rule": "rule2b", "smt_score": 0.0}, "entry_ranges": [], "targets": []},
    # Re-entry within 30 min of cautious reversal
    {"kind": "market-entry", "time": f"{DATE}T10:10:00-04:00",
     "direction": "up", "price": 98.0, "stop": 93.0, "slippage": 2.0},
    {"kind": "market-close", "time": f"{DATE}T16:00:00-04:00",
     "direction": "up", "price": 110.0, "reason": "end-of-session", "slippage": 2.0},
]

SYNTHETIC_TRADES = [
    {"entry_time": f"{DATE}T09:35:00-04:00", "entry_price": 100.0,
     "direction": "down", "contracts": 2,
     "exit_time": f"{DATE}T09:40:00-04:00", "exit_price": 105.0,
     "exit_reason": "stopped-out", "pnl_points": -5.0, "pnl_dollars": -50.0},
    {"entry_time": f"{DATE}T10:10:00-04:00", "entry_price": 98.0,
     "direction": "up", "contracts": 2,
     "exit_time": f"{DATE}T16:00:00-04:00", "exit_price": 110.0,
     "exit_reason": "end-of-session", "pnl_points": 12.0, "pnl_dollars": 120.0},
]

SYNTHETIC_LEVELS_JSON = {
    "all_time_high": 200.0,
    "liquidities": [
        {"name": "week_low",  "kind": "level", "price": 80.0},
        {"name": "tdo_low",   "kind": "level", "price": 90.0},
        {"name": "TDO",       "kind": "level", "price": 105.0},
        {"name": "week_high", "kind": "level", "price": 120.0},
        {"name": "fvg_test",  "kind": "fvg",   "top": 115.0, "bottom": 110.0},  # excluded from levels
    ],
}
```

Fixtures:
- `_make_bars(date, n=60, start_price=95.0) -> pd.DataFrame` — synthetic MNQ 1m bars with deterministic RNG
- `session_dir(tmp_path)` — creates `tmp_path/sessions/{DATE}/` with events.jsonl, levels.json, MNQ_1m.parquet
- `regression_dir(tmp_path)` — creates `tmp_path/data/regression/{DATE}/trades.tsv`
- `patched_indexer(tmp_path, session_dir, regression_dir, monkeypatch)` — patches all path constants in `indexer` module to tmp_path; returns `indexer` module

- **VALIDATE**: `uv run pytest tests/test_indexer.py --collect-only` — no errors (0 tests collected is fine)

**Wave 1 Checkpoint**: `uv run python -c "import indexer; print('ok')" && uv run pytest tests/test_indexer.py --collect-only`

---

### WAVE 2: Core Levels 1 + 2

#### Task 2.1: ADD Level-1 and Level-2 logic to `indexer.py`

- **WAVE**: 2
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1, 4.1]
- **PROVIDES**: Populated `index_session()` for L1 and L2; `_index_session_level()`, `_index_trades()`

**IMPLEMENT**:

**`_index_session_level(date, events, trades, bars, sorted_levels, ath) -> dict`:**

| Field | Logic |
|---|---|
| `date` | pass-through |
| `n_trades` | `len(trades)` |
| `n_stops` | count `exit_reason == "stopped-out"` |
| `n_market_close` | count `exit_reason == "market-close"` |
| `n_eos` | count `exit_reason == "end-of-session"` |
| `pnl_dollars` | `sum(float(t["pnl_dollars"]) for t in trades)` |
| `win_rate` | fraction with `pnl_points > 0`; `0.0` if no trades |
| `max_consecutive_stops` | iterate trades in order, track run of stopped-out, keep max |
| `direction_flips` | count consecutive new-hypothesis events where direction changes from previous |
| `dominant_direction` | for each new-hypothesis, compute duration to next event; accumulate by direction; `"up"/"down"` if >60% of total time, else `"mixed"` |
| `ath_crossed` | `bars["High"].max() > ath` (False if bars empty) |
| `ath_cross_time` | first bar timestamp where `High > ath`; None if not crossed |
| `n_smt_divs` | count new-hypothesis events where `direction_reason.smt_score > 0` (approximation) |
| `n_hypothesis_updates` | count `kind == "new-hypothesis"` events |
| `had_cautious_reversal` | any market-close event with `reason` containing `"cautious"` |
| `had_post_cautious_reentry` | any market-entry within 30 min after a cautious market-close |
| `n_stop_entries_filled` | count `kind == "stop-entry-filled"` |
| `n_stop_entries_cancelled` | count `kind == "stop-entry-cancelled"` |
| `rule` | `direction_reason.rule` from first new-hypothesis; None if missing |
| `session_range_pts` | `bars["High"].max() - bars["Low"].min()`; None if empty |
| `session_direction` | `"up"` / `"down"` / `"flat"` from first-bar open to last-bar close |
| `bar_atr` | `(bars["High"] - bars["Low"]).mean()`; None if empty |
| `has_unrecorded_trades` | `False` (placeholder — logic TBD in future iteration) |

**`_index_trades(date, events, trades, bars, sorted_levels, ath) -> list[dict]`:**

For each trade (index `i`, 0-based):
1. `trade_id = f"{date}_T{i+1}"`
2. Parse `entry_time`, `exit_time` as `pd.Timestamp` with `America/New_York` tz
3. `entry_type` — find market-entry or stop-entry-filled event within 2-min window around entry_time; default `"market-entry"`
4. `stop_pts` — from matched entry event's `"stop"` field: `abs(event["stop"] - trade["entry_price"])`; None if field missing
5. `hold_minutes = (exit_time - entry_time).total_seconds() / 60`
6. `hypothesis_at_entry` — count new-hypothesis events with time < entry_time
7. `direction_flips_before_entry` — count direction changes in new-hypothesis events before entry_time
8. `mins_since_last_exit` — gap from previous trade exit_time; None for first trade
9. `last_exit_reason` — previous trade's exit_reason; None for first trade
10. `was_after_cautious_reversal` — `last_exit_reason == "market-close"` AND the events log shows that exit had reason containing `"cautious"` (find the market-close event closest to previous trade exit)
11. `mins_after_cautious_reversal` — `mins_since_last_exit` if was_after_cautious_reversal else None
12. For stopped-out trades, `post_stop_recovery_pts` / `post_stop_recovery_min` — find bars after exit_time; within next 30 bars, find the furthest excursion in trade direction past stop level; None if not stopped-out
13. `pre_entry_bar_atr_5` — mean(High-Low) of 5 bars immediately before entry_time in bars df; None if < 5 bars available
14. `entry_in_range` — check entry_price against entry_ranges list of active hypothesis at entry time (None if no ranges)
15. `movement_ids = []` — populated later in Wave 3

**Update `index_session(date: str) -> dict`:**
```python
def index_session(date: str) -> dict:
    events        = _load_events(date)
    trades        = _load_trades(date)
    bars          = _load_bars(date)
    sorted_levels, ath = _load_levels(date)
    session_rec   = _index_session_level(date, events, trades, bars, sorted_levels, ath)
    trade_recs    = _index_trades(date, events, trades, bars, sorted_levels, ath)
    return {"session": session_rec, "trades": trade_recs, "movements": [], "links": []}
```

- **VALIDATE**: `uv run python -c "import indexer; r=indexer.index_session('2026-04-08'); print(r['session']['n_trades'], 'trades')"`

---

#### Task 2.2: ADD Level-1 and Level-2 unit tests

- **WAVE**: 2
- **AGENT_ROLE**: test-developer
- **DEPENDS_ON**: [1.2]
- **BLOCKS**: [3.2]
- **PROVIDES**: Test coverage for all L1 and L2 fields; anchor helper tests

**IMPLEMENT** — add to `tests/test_indexer.py` after fixtures:

**Anchor tests** (call `indexer._anchor_price()` directly with SYNTHETIC_LEVELS_JSON sorted levels):
- `test_anchor_between_two_levels` — price 97.0 → `"7.00 pts above tdo_low and 8.00 pts below TDO"`
- `test_anchor_above_ath` — price 250.0 → `"50.00 pts above ATH"`
- `test_anchor_at_level` — price 80.0 → `"at week_low"`
- `test_anchor_fvg_excluded` — FVG entry in levels.json is not mentioned in any anchor string

**L1 tests** (use `patched_indexer` fixture; call `patched_indexer.index_session(DATE)["session"]`):
- `test_level1_n_trades` → 2
- `test_level1_n_stops` → 1
- `test_level1_pnl_dollars` → 70.0
- `test_level1_win_rate` → 0.5
- `test_level1_max_consecutive_stops` → 1
- `test_level1_direction_flips` → 1
- `test_level1_n_hypothesis_updates` → 2
- `test_level1_had_cautious_reversal` → True
- `test_level1_had_post_cautious_reentry` → True (10:10 entry is 10 min after 10:00 cautious)
- `test_level1_rule` → `"rule2b"`
- `test_level1_ath_not_crossed` → False (ATH=200, bars ~95–115)
- `test_level1_ath_crossed_when_bar_exceeds` — inject one bar with High=201 → ath_crossed=True, ath_cross_time is not None
- `test_level1_empty_session` — no trades, no entries → n_trades=0, win_rate=0.0, max_consecutive_stops=0, dominant_direction in {"up","down","mixed","unknown"}

**L2 tests** (call `patched_indexer.index_session(DATE)["trades"]`):
- `test_level2_trade_ids` → `["{DATE}_T1", "{DATE}_T2"]`
- `test_level2_hold_minutes_t1` → 5.0 (09:35–09:40)
- `test_level2_hypothesis_at_entry_t1` → 1 (one hypothesis before 09:35)
- `test_level2_hypothesis_at_entry_t2` → 2
- `test_level2_direction_flips_before_entry_t1` → 0
- `test_level2_direction_flips_before_entry_t2` → 1
- `test_level2_was_after_cautious_reversal_t1` → False
- `test_level2_was_after_cautious_reversal_t2` → True
- `test_level2_mins_after_cautious_reversal_t2` → 10.0
- `test_level2_first_trade_no_last_exit` → T1: mins_since_last_exit=None, last_exit_reason=None
- `test_level2_post_stop_recovery_none_for_eos` → T2 exit_reason=end-of-session: post_stop_recovery_pts is None

- **VALIDATE**: `uv run pytest tests/test_indexer.py -k "level1 or level2 or anchor" -v`

**Wave 2 Checkpoint**: All L1/L2/anchor tests pass.

---

### WAVE 3: Level 3 (Movement) + Trade-Movement Links

#### Task 3.1: ADD movement detection + links + YAML to `indexer.py`

- **WAVE**: 3
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: [4.1]
- **PROVIDES**: `_detect_movement_periods()`, `_build_trade_movement_links()`, YAML writers, fully populated `index_session()`

**IMPLEMENT**:

**`_detect_movement_periods(date, bars, sorted_levels, ath) -> list[dict]`:**

State-machine algorithm:
```
1. If bars empty → return []
2. Compute raw_dir[i] = +1 if Close[i] >= Open[i] else -1
3. State machine:
   - period_start = 0; boundaries = []
   - For i in range(MOVEMENT_MIN_BARS, len(bars)):
       trailing = raw_dir[i-MOVEMENT_MIN_BARS : i]
       dominant_raw = +1 if sum(trailing) > 0 else -1
       if dominant_raw != raw_dir[period_start] and i - period_start >= MOVEMENT_MIN_BARS:
           boundaries.append(i - MOVEMENT_MIN_BARS)
           period_start = i - MOVEMENT_MIN_BARS
   - Boundaries define period slices: (0→b[0]), (b[0]→b[1]), ..., (b[-1]→end)
4. For each period slice (start_idx, end_idx):
   - period_bars = bars.iloc[start_idx:end_idx]
   - start_price = float(period_bars.iloc[0]["Open"])
   - end_price   = float(period_bars.iloc[-1]["Close"])
   - pts_net     = end_price - start_price
   - gross_travel = float((period_bars["High"] - period_bars["Low"]).sum())
   - displacement_ratio = round(abs(pts_net) / gross_travel, 4) if gross_travel > 0 else 1.0
   - direction = "up" if pts_net > 0.5 else "down" if pts_net < -0.5 else "sideways"
   - type_ = "clean" if displacement_ratio >= DISPLACEMENT_CLEAN_THRESHOLD else "choppy"
   - start_time = period_bars.index[0].strftime("%H:%M")
   - end_time   = period_bars.index[-1].strftime("%H:%M")
   - movement_id = f"{date}_M{n}"  # 1-indexed
   - start_anchor = _anchor_price(start_price, sorted_levels)
   - end_anchor   = _anchor_price(end_price, sorted_levels)
   - yield: {"movement_id":..., "start_time":..., "end_time":...,
             "direction":..., "type":type_, "start_price":..., "end_price":...,
             "pts_net":..., "displacement_ratio":...,
             "start_anchor":..., "end_anchor":..., "trade_ids":[]}
```

**`_build_trade_movement_links(date, trade_recs, movements) -> list[dict]`:**

For each (trade, movement) pair:
1. Convert movement start/end times to full timestamps using date + `"America/New_York"`
2. Overlap: trade entry_time < movement end AND trade exit_time > movement start
3. For overlapping pairs:
   - `entry_pct = _pct_into(trade["entry_price"], movement["start_price"], movement["end_price"])`
   - `exit_pct  = _pct_into(trade["exit_price"], ...)` — same formula
   - `_pct_into(p, s, e)` = clamp((p-s)/(e-s)*100, 0, 100) if e≠s else 50.0
   - `direction_aligned = trade["direction"] == movement["direction"]`
   - `note = _generate_note(trade, movement, entry_pct)` — natural language string:
     ```
     "entered {entry_pct:.0f}% into a {type} {direction}ward move {from_anchor},
      {exit_desc}"
     exit_desc (stopped-out): "stopped out after {hold:.0f} min{recovery}"
     exit_desc (other): "held {hold:.0f} min, exited {exit_reason} near {end_anchor}"
     recovery (if post_stop_recovery_pts > 0): "; price continued {pts:.1f}pts further"
     ```
   - Append `movement["trade_ids"].append(trade_id)` and `trade_rec["movement_ids"].append(movement_id)`
4. Return list of link dicts

**`_write_movements_yaml(date, movements) -> None`** — write to `sessions/{date}/movements.yaml`

**`_write_links_yaml(date, links) -> None`** — write to `sessions/{date}/trade_movement_links.yaml`

Both use `yaml.dump(..., default_flow_style=False, allow_unicode=True, sort_keys=False)`

**Update `index_session()`:**
```python
def index_session(date: str) -> dict:
    events        = _load_events(date)
    trades        = _load_trades(date)
    bars          = _load_bars(date)
    sorted_levels, ath = _load_levels(date)
    session_rec   = _index_session_level(date, events, trades, bars, sorted_levels, ath)
    trade_recs    = _index_trades(date, events, trades, bars, sorted_levels, ath)
    movements     = _detect_movement_periods(date, bars, sorted_levels, ath)
    links         = _build_trade_movement_links(date, trade_recs, movements)
    return {"session": session_rec, "trades": trade_recs,
            "movements": movements, "links": links}
```

- **VALIDATE**: `uv run python -c "import indexer; r=indexer.index_session('2026-04-08'); print(len(r['movements']),'movements',len(r['links']),'links')"`

---

#### Task 3.2: ADD Level-3 and link tests

- **WAVE**: 3
- **AGENT_ROLE**: test-developer
- **DEPENDS_ON**: [2.2]
- **BLOCKS**: []
- **PROVIDES**: Full test coverage for movement detection and trade-movement links

**IMPLEMENT** — add to `tests/test_indexer.py`:

Helper `_make_clean_bars(date, direction, n=20)` — bars with consistent direction and high displacement ratio
Helper `_make_choppy_bars(date, n=30)` — zigzag bars with low net displacement

**Movement detection tests** (call `indexer._detect_movement_periods(DATE, bars, sorted_levels, ath)` directly):
- `test_movement_clean_up` — monotonically increasing bars → 1 period, direction="up", type="clean"
- `test_movement_clean_down` — monotonically decreasing bars → 1 period, direction="down", type="clean"
- `test_movement_splits_direction_change` — 25 up + 25 down bars → ≥2 periods; first "up", last "down"
- `test_movement_choppy_type` — zigzag bars → displacement_ratio < 0.65 → type="choppy"
- `test_movement_clean_type` — monotone bars → displacement_ratio >= 0.65 → type="clean"
- `test_movement_min_bars_guard` — only 3 bars → 1 period (no split)
- `test_movement_empty_bars` — empty df → []
- `test_movement_anchors_not_empty` — all periods have non-empty start_anchor and end_anchor
- `test_movement_ids_sequential` — movement_ids are `{DATE}_M1`, `{DATE}_M2`, ...
- `test_movement_no_none_anchors` — no movement has start_anchor=None or end_anchor=None

**Link tests** (call `indexer._build_trade_movement_links()` with synthetic data):
- `test_links_overlap_detected` — trade within movement window → link exists
- `test_links_no_overlap` — trade and movement on non-overlapping windows → no link
- `test_links_direction_aligned_true` — matching directions → direction_aligned=True
- `test_links_direction_aligned_false` — opposing directions → direction_aligned=False
- `test_links_entry_pct_range` — entry_pct_into_movement in [0, 100]
- `test_links_note_non_empty` — note is a non-empty string
- `test_links_bidirectional` — after build: movement["trade_ids"] contains trade_id; trade_rec["movement_ids"] contains movement_id

**YAML output tests** (use `patched_indexer`; call full `index_session()` then `_write_movements_yaml()` and `_write_links_yaml()`):
- `test_movements_yaml_exists` — file created at `sessions/{DATE}/movements.yaml`
- `test_movements_yaml_valid` — `yaml.safe_load()` returns a list of dicts
- `test_movements_yaml_has_anchors` — each item has `start_anchor` and `end_anchor` that are strings
- `test_links_yaml_exists` — file created
- `test_links_yaml_valid` — parseable; list of dicts with `trade_id`, `movement_id`, `note`

- **VALIDATE**: `uv run pytest tests/test_indexer.py -v`

**Wave 3 Checkpoint**: All tests pass including movement and link tests.

---

### WAVE 4: Runner + Integration Hook

#### Task 4.1: ADD `build_index()` + CLI to `indexer.py`

- **WAVE**: 4
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: [3.1]
- **BLOCKS**: [5.1]
- **PROVIDES**: `build_index()`, `_append_parquet()`, `main()` CLI

**IMPLEMENT**:

**`_append_parquet(path, new_rows, dedup_col) -> None`** — generic helper:
- If path exists: concat existing df with new_rows df, drop duplicates on dedup_col, sort by dedup_col, save
- If not: create from new_rows, save
- Use `pd.to_parquet(..., index=False)`

**`build_index(dates: list[str] | None = None) -> None`:**
```python
def build_index(dates=None):
    if dates is None:
        all_dates = sorted(p.parent.name for p in REGRESSION_DIR.glob("*/events.jsonl"))
        if SESSION_INDEX_PATH.exists():
            existing = set(pd.read_parquet(SESSION_INDEX_PATH)["date"].tolist())
            dates = [d for d in all_dates if d not in existing]
        else:
            dates = all_dates
    if not dates:
        return
    session_rows, trade_rows, movement_rows, link_rows = [], [], [], []
    for date in dates:
        try:
            r = index_session(date)
        except Exception as exc:
            print(f"indexer: skip {date}: {exc}", file=sys.stderr)
            continue
        session_rows.append(r["session"])
        trade_rows.extend(r["trades"])
        movement_rows.extend(r["movements"])
        link_rows.extend(r["links"])
        if r["movements"]:
            _write_movements_yaml(date, r["movements"])
        if r["links"]:
            _write_links_yaml(date, r["links"])
    if not session_rows:
        return
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    _append_parquet(SESSION_INDEX_PATH,   session_rows,  dedup_col="date")
    _append_parquet(TRADES_INDEX_PATH,    trade_rows,    dedup_col="trade_id")
    _append_parquet(MOVEMENTS_INDEX_PATH, movement_rows, dedup_col="movement_id")
```

**`main() -> int`:**
```python
def main() -> int:
    parser = argparse.ArgumentParser(description="SMT v2 session indexer")
    parser.add_argument("--dates", nargs="+", metavar="DATE_OR_RANGE")
    parser.add_argument("--all", action="store_true",
                        help="Reindex all dates (ignore existing index)")
    args = parser.parse_args()
    dates = None
    if args.dates:
        dates = _parse_date_tokens(args.dates)
    elif args.all:
        dates = sorted(p.parent.name for p in REGRESSION_DIR.glob("*/events.jsonl"))
    build_index(dates)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

- **VALIDATE**: `uv run python indexer.py --dates 2026-04-08 && echo "Exit 0 OK"`

---

#### Task 4.2: ADD `regression.py` integration hook

- **WAVE**: 4
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: [5.1]
- **PROVIDES**: Automatic indexing after each regression run

**IMPLEMENT** — in `regression.py:run_regression()`, just before `return results`:

```python
    # Index newly computed dates (lazy import; silent on failure so regression still returns)
    try:
        from indexer import build_index as _build_index
        _build_index(dates)
    except Exception as exc:
        import sys as _sys
        print(f"indexer hook: {exc}", file=_sys.stderr)

    return results
```

The import must be inside the function body (lazy) to avoid circular import issues and to keep `regression.py` working if `indexer.py` hasn't been created yet.

- **VALIDATE**: `uv run pytest tests/test_smt_regression.py -v` — no regressions

**Wave 4 Checkpoint**: `uv run pytest tests/test_indexer.py tests/test_smt_regression.py -v`

---

### WAVE 5: Integration Validation

#### Task 5.1: Integration tests + fix any failures

- **WAVE**: 5
- **AGENT_ROLE**: integration-specialist
- **DEPENDS_ON**: [4.1, 4.2]
- **PROVIDES**: Verified working system against real session data

**IMPLEMENT** — add to `tests/test_indexer.py`:

```python
@pytest.mark.integration
def test_build_index_real_session(tmp_path, monkeypatch):
    """Full pipeline against real 2026-04-08 data (skipped if data absent)."""
    import importlib
    import indexer as idxr

    real_sess = Path("sessions") / "2026-04-08"
    real_reg  = Path("data") / "regression" / "2026-04-08"
    if not real_sess.exists() or not real_reg.exists():
        pytest.skip("Real session data not available")

    out_dir = tmp_path / "data"
    out_dir.mkdir()
    for attr, val in [
        ("INDEX_DIR",            out_dir),
        ("SESSION_INDEX_PATH",   out_dir / "sessions_index.parquet"),
        ("TRADES_INDEX_PATH",    out_dir / "trades_index.parquet"),
        ("MOVEMENTS_INDEX_PATH", out_dir / "movements_index.parquet"),
    ]:
        monkeypatch.setattr(idxr, attr, val)

    idxr.build_index(["2026-04-08"])

    sess_df  = pd.read_parquet(out_dir / "sessions_index.parquet")
    trade_df = pd.read_parquet(out_dir / "trades_index.parquet")
    mv_df    = pd.read_parquet(out_dir / "movements_index.parquet")

    assert len(sess_df) == 1
    assert sess_df.iloc[0]["date"] == "2026-04-08"
    assert len(trade_df) == sess_df.iloc[0]["n_trades"]
    assert len(mv_df) >= 1
    assert set(mv_df["direction"].unique()).issubset({"up", "down", "sideways"})
    assert set(mv_df["type"].unique()).issubset({"clean", "choppy"})

    mvs_path   = Path("sessions") / "2026-04-08" / "movements.yaml"
    links_path = Path("sessions") / "2026-04-08" / "trade_movement_links.yaml"
    assert mvs_path.exists()
    assert links_path.exists()
    mvs = yaml.safe_load(mvs_path.read_text(encoding="utf-8"))
    assert isinstance(mvs, list) and len(mvs) >= 1
    assert all("start_anchor" in m and "end_anchor" in m for m in mvs)


def test_build_index_incremental(patched_indexer):
    """Running build_index twice for the same date yields no duplicate rows."""
    patched_indexer.build_index([DATE])
    patched_indexer.build_index([DATE])
    sess_df = pd.read_parquet(patched_indexer.SESSION_INDEX_PATH)
    assert len(sess_df) == 1


def test_cli_runs_exit_0(patched_indexer, monkeypatch):
    """CLI main() exits 0 with --dates arg."""
    import sys
    monkeypatch.setattr(sys, "argv", ["indexer.py", "--dates", DATE])
    assert patched_indexer.main() == 0
```

Run sequence:
```bash
uv run pytest tests/test_indexer.py -v
uv run pytest tests/test_smt_regression.py -v
uv run pytest -m "not integration" -v
uv run pytest tests/test_indexer.py -m integration -v
```

Fix any failures found.

**Wave 5 Checkpoint**: All tests pass. See Level 4 validation below.

---

## TESTING STRATEGY

All tests automated with pytest. No manual tests.

### Test Inventory

| Test | Type | Covers |
|---|---|---|
| `test_anchor_between_two_levels` | Unit ✅ | Two-bound anchor string |
| `test_anchor_above_ath` | Unit ✅ | Single-bound extreme |
| `test_anchor_at_level` | Unit ✅ | Exact level match |
| `test_anchor_fvg_excluded` | Unit ✅ | FVG not in anchor |
| `test_level1_n_trades` | Unit ✅ | L1: n_trades |
| `test_level1_n_stops` | Unit ✅ | L1: n_stops |
| `test_level1_pnl_dollars` | Unit ✅ | L1: pnl aggregation |
| `test_level1_win_rate` | Unit ✅ | L1: win_rate |
| `test_level1_max_consecutive_stops` | Unit ✅ | L1: streak detection |
| `test_level1_direction_flips` | Unit ✅ | L1: flip counting |
| `test_level1_n_hypothesis_updates` | Unit ✅ | L1: event count |
| `test_level1_had_cautious_reversal` | Unit ✅ | L1: cautious detection |
| `test_level1_had_post_cautious_reentry` | Unit ✅ | L1: re-entry cluster |
| `test_level1_rule` | Unit ✅ | L1: first hypothesis rule |
| `test_level1_ath_not_crossed` | Unit ✅ | L1: ATH guard |
| `test_level1_ath_crossed_when_bar_exceeds` | Unit ✅ | L1: ATH crossing |
| `test_level1_empty_session` | Unit ✅ | L1: edge case |
| `test_level2_trade_ids` | Unit ✅ | L2: ID scheme |
| `test_level2_hold_minutes_t1` | Unit ✅ | L2: hold time |
| `test_level2_hypothesis_at_entry_t1/t2` | Unit ✅ | L2: active hypothesis |
| `test_level2_direction_flips_before_entry` | Unit ✅ | L2: pre-entry flips |
| `test_level2_was_after_cautious_reversal` | Unit ✅ | L2: pattern flag |
| `test_level2_mins_after_cautious_reversal` | Unit ✅ | L2: timing gap |
| `test_level2_first_trade_no_last_exit` | Unit ✅ | L2: first trade edge |
| `test_level2_post_stop_recovery_none_for_eos` | Unit ✅ | L2: non-stop exit |
| `test_movement_clean_up/down` | Unit ✅ | L3: direction |
| `test_movement_splits_direction_change` | Unit ✅ | L3: boundary detection |
| `test_movement_choppy/clean_type` | Unit ✅ | L3: displacement_ratio |
| `test_movement_min_bars_guard` | Unit ✅ | L3: minimum period |
| `test_movement_empty_bars` | Unit ✅ | L3: edge case |
| `test_movement_anchors_not_empty` | Unit ✅ | L3: anchor population |
| `test_movement_ids_sequential` | Unit ✅ | L3: ID scheme |
| `test_movement_no_none_anchors` | Unit ✅ | L3: no None anchors |
| `test_links_overlap_detected` | Unit ✅ | Links: detection |
| `test_links_no_overlap` | Unit ✅ | Links: edge case |
| `test_links_direction_aligned_true/false` | Unit ✅ | Links: alignment |
| `test_links_entry_pct_range` | Unit ✅ | Links: pct clamped |
| `test_links_note_non_empty` | Unit ✅ | Links: note generation |
| `test_links_bidirectional` | Unit ✅ | Links: bi-directional refs |
| `test_movements_yaml_exists/valid/has_anchors` | Unit ✅ | YAML output |
| `test_links_yaml_exists/valid` | Unit ✅ | YAML output |
| `test_build_index_incremental` | Unit ✅ | Runner: dedup |
| `test_cli_runs_exit_0` | Unit ✅ | CLI: runnability |
| `test_build_index_real_session` | Integration ✅ | Full pipeline (skipped if no data) |
| `test_smt_regression.py` (existing suite) | Non-regression ✅ | No regressions in regression.py |

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest) | ~45 | 93% |
| ✅ Integration (pytest, real data, skip-safe) | 1 | 2% |
| ✅ Non-regression (existing tests) | ~15 | 3% |
| ✅ CLI smoke | 1 | 2% |
| ⚠️ Manual | 0 | 0% |
| **Total** | ~62 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Import Smoke
```bash
uv run python -c "import indexer; print('ok')"
```

### Level 2: Unit Tests
```bash
uv run pytest tests/test_indexer.py -v
```

### Level 3: Non-Regression
```bash
uv run pytest tests/test_smt_regression.py -v
uv run pytest -m "not integration" -v
```

### Level 4: End-to-End Validation
```bash
# CLI produces outputs
uv run python indexer.py --dates 2026-04-08
# Verify Parquets and YAML
python -c "
import pandas as pd, yaml, pathlib
s = pd.read_parquet('data/sessions_index.parquet')
t = pd.read_parquet('data/trades_index.parquet')
m = pd.read_parquet('data/movements_index.parquet')
print('Sessions:', len(s), '| Trades:', len(t), '| Movements:', len(m))
print('Directions:', m['direction'].unique().tolist())
print('Types:', m['type'].unique().tolist())
mvs = yaml.safe_load(pathlib.Path('sessions/2026-04-08/movements.yaml').read_text())
lnk = yaml.safe_load(pathlib.Path('sessions/2026-04-08/trade_movement_links.yaml').read_text())
print('YAML movements:', len(mvs or []), '| YAML links:', len(lnk or []))
assert all('start_anchor' in m and 'end_anchor' in m for m in (mvs or []))
print('OK')
"
# Script runnability criterion
uv run python indexer.py --dates 2026-04-08 && echo 'CLI exit 0 PASS'
# ASCII-safe output criterion (no UnicodeEncodeError on default locale)
uv run python indexer.py --dates 2026-04-08 2>&1 | python -c \"import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())\"
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `index_session(date)` returns `{session, trades, movements, links}` — all keys present, none None
- [ ] Session record contains all L1 fields: `date`, `n_trades`, `n_stops`, `pnl_dollars`, `win_rate`, `dominant_direction`, `session_direction`, `ath_crossed`, `had_cautious_reversal`, `had_post_cautious_reentry`, `rule`, `bar_atr`, `session_range_pts`
- [ ] `indexer.py` exists; `index_session()` is a pure function (no I/O) — all file writing goes through `build_index()` and YAML writers
- [ ] Trade records: one per trades.tsv row; `trade_id` = `{date}_T{n}`; L2 fields include `hold_minutes`, `was_after_cautious_reversal`, `mins_after_cautious_reversal`, `post_stop_recovery_pts`, `movement_ids`
- [ ] Movement periods: `direction` ∈ `{up, down, sideways}`; `type` ∈ `{clean, choppy}`; `displacement_ratio` ≥ 0.65 iff type is "clean"
- [ ] Every movement's `start_anchor` and `end_anchor` are non-empty strings — never None, never empty
- [ ] Every link record has `trade_id`, `movement_id`, `direction_aligned` (bool), `note` (non-empty string); bi-directional refs populated in both movement and trade records

### Error Handling
- [ ] `build_index()` skips a date that raises an exception, prints to stderr, and continues — does not abort the run
- [ ] Sessions with no trades produce a valid L1 record with `n_trades=0`, `win_rate=0.0`, `max_consecutive_stops=0`
- [ ] Sessions with empty bar data produce an empty movements list without raising

### Integration / E2E
- [ ] `uv run python indexer.py --dates 2026-04-08` exits 0 and produces all five output files (`sessions_index.parquet`, `trades_index.parquet`, `movements_index.parquet`, `movements.yaml`, `trade_movement_links.yaml`)
- [ ] `build_index()` is incremental: re-running on an already-indexed date produces no duplicate Parquet rows
- [ ] `regression.py` automatically calls `build_index()` for the dates just run; existing regression tests pass with no change in behaviour
- [ ] `pyyaml>=6.0` added to `pyproject.toml`; `uv sync` succeeds

### Validation
- [ ] All unit tests in `tests/test_indexer.py` pass — verified by: `uv run pytest tests/test_indexer.py -v`
- [ ] No regressions in full suite — verified by: `uv run pytest -m "not integration" -v`
- [ ] Parquet files are readable with correct column names and no object-dtype nullable columns — verified by: `pd.read_parquet("data/sessions_index.parquet").dtypes`
- [ ] YAML outputs are parseable — verified by: `python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('sessions/2026-04-08/movements.yaml').read_text())"`

### Out of Scope
- Live/incremental movement indexing as bars arrive during a session
- `has_unrecorded_trades` field logic (placeholder — field exists but always False)
- Centralized `trade_movement_links_index.parquet` (links are per-session YAML only)
- UI or browser-based index explorer
- Indexing live-only sessions with no `data/regression/{DATE}/` directory

---

## COMPLETION CHECKLIST

- [ ] `pyyaml>=6.0` added to `pyproject.toml`; `uv sync` run
- [ ] `indexer.py` created (Wave 1–4 tasks complete)
- [ ] `tests/test_indexer.py` created with all test waves
- [ ] `regression.py` hook added (lazy import, silent on failure)
- [ ] Level 1 import smoke passes
- [ ] Level 2 unit tests all pass
- [ ] Level 3 non-regression: no new failures
- [ ] Level 4 end-to-end: Parquets created, YAML valid, CLI exits 0
- [ ] Parquet columns use nullable dtypes for optional fields (not object dtype)
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**Movement threshold calibration**: `DISPLACEMENT_CLEAN_THRESHOLD = 0.65` and `MOVEMENT_MIN_BARS = 5` are starting values. After the first full-corpus index run, spot-check 5–10 sessions visually against the chart to validate boundaries match intuition. Tune constants if splits are too noisy (raise `MOVEMENT_MIN_BARS`) or too coarse (lower it). The threshold is a module constant so it can be changed without touching logic.

**SMT divergence approximation**: No explicit `smt-divergence` event kind was found in sample events.jsonl. `n_smt_divs` is approximated from new-hypothesis events where `direction_reason.smt_score > 0`. If the strategy adds an explicit event kind in the future, update this field.

**sessions/ vs data/regression/ sources**: Bars live only in `sessions/{DATE}/MNQ_1m.parquet`. Events and levels exist in both directories; prefer `sessions/` with fallback to `data/regression/`. Trades exist only in `data/regression/{DATE}/trades.tsv`. YAML outputs are written to `sessions/{DATE}/` to keep human-readable context with the chart and levels files.

**Parquet dtypes**: Use pandas nullable dtypes (`pd.StringDtype()`, `pd.Float64Dtype()`, `pd.BooleanDtype()`) for nullable fields such as `ath_cross_time`, `post_stop_recovery_pts`, `mins_after_cautious_reversal`. This avoids object-dtype columns that degrade Parquet performance and query ergonomics.

**Failure isolation in build_index()**: The try/except per date ensures a single malformed session does not abort the entire index build. Errors are logged to stderr with the date so they can be investigated without interrupting the run.
