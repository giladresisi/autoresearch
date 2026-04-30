# SMT Redesign — JSON-File Architecture + Module Decomposition

_Date: 2026-04-27 | Source: `ideas.md` | Layout: option 1 (new files alongside, old code untouched)_

## Problem

`strategy_smt.py` (2555 lines) bundles entry detection, exit logic, breakeven/trail/secondary-target/Layer-B/MSS/CISD/displacement-invalidation, and re-entry into a single state machine. The user is removing all of it except the bare entry/stop/direction-mismatch path. `ideas.md` redefines the system as four small modules communicating through four JSON files, plus a separate `trend.py` for cautious-mode and trend invalidation.

Goal: minimum working code change. Build the new control flow as fresh files. Reuse the *primitives* (divergence, FVG, TDO, PDH/PDL, EQH/EQL, confirmation-bar) by import. Leave `strategy_smt.py` / `hypothesis_smt.py` and their tests resident-but-inert. Pruning is a later pass.

## Scope

**In:** four new logic modules (`daily.py`, `hypothesis.py`, `strategy.py`, `trend.py`); a small JSON-IO module (`smt_state.py`); a backtest entry point that walks 1m parquet bars and dispatches to the modules at their cadences; a realtime entry point that wires the modules into IB streams; a regression runner driven by `regression.md`; tests for each module.

**Out (this initial cut):** TP, partial exits, breakeven, trail, secondary targets, Layer-B FVG re-add, MSS / CISD / opposing-displacement invalidation; pruning of `strategy_smt.py` / `hypothesis_smt.py`; production-grade always-on daemon scheduler; real implementations of TBD items in `ideas.md` (`estimated_dir`, `opposite_premove`, `hypothesis.direction`, `cautious_price`) — these stay hardcoded per `ideas.md`.

## Architecture

### File layout (option 1 — alongside)

```
follow-live/
├── daily.py             # NEW — once at 09:20 ET
├── hypothesis.py        # NEW — every 5m
├── strategy.py          # NEW — every completed 5m bar
├── trend.py             # NEW — every 1m
├── smt_state.py         # NEW — JSON read/write for the four files
├── regression.py        # NEW — specific-day regression runner
├── regression.md        # NEW — list of dates / date-ranges
├── strategy_smt.py      # UNCHANGED — primitives still imported from here
├── hypothesis_smt.py    # UNCHANGED — resident, unused by new path
├── signal_smt.py        # ADAPTED — alternate dispatch path added; existing path intact
├── backtest_smt.py      # ADAPTED — alternate entry added; existing harness intact
└── data/
    ├── global.json      # NEW
    ├── daily.json       # NEW
    ├── hypothesis.json  # NEW
    ├── position.json    # SCHEMA CHANGE (was minimal, becomes the ideas.md schema)
    └── regression/<YYYY-MM-DD>/{events.jsonl,trades.tsv,baseline_events.jsonl,baseline_trades.tsv}
```

No name collisions verified against current top-level files. The `strategies/` package is unaffected.

### JSON file schemas

#### `global.json` — multi-day persistent state

```json
{
  "all_time_high": 21450.25,
  "trend": "up"
}
```

`trend` is read by `daily.py` to seed `daily.estimated_dir` (TBD: real estimation later). Updated manually for now.

#### `daily.json` — daily-session-immutable state

```json
{
  "date": "2026-04-27",
  "liquidities": [
    {"name": "TDO",         "kind": "level", "price": 21412.50},
    {"name": "TWO",         "kind": "level", "price": 21380.00},
    {"name": "week_high",   "kind": "level", "price": 21450.25},
    {"name": "week_low",    "kind": "level", "price": 21210.00},
    {"name": "day_high",    "kind": "level", "price": 21425.00},
    {"name": "day_low",     "kind": "level", "price": 21390.50},
    {"name": "asia_high",   "kind": "level", "price": 21420.00},
    {"name": "asia_low",    "kind": "level", "price": 21395.00},
    {"name": "london_high", "kind": "level", "price": 21422.00},
    {"name": "london_low",  "kind": "level", "price": 21398.00},
    {"name": "ny_morning_high", "kind": "level", "price": 21425.00},
    {"name": "ny_morning_low",  "kind": "level", "price": 21405.00},
    {"name": "ny_evening_high", "kind": "level", "price": 21418.00},
    {"name": "ny_evening_low",  "kind": "level", "price": 21390.00},
    {"name": "fvg_2026-04-25_14:00_bull", "kind": "fvg", "top": 21408.00, "bottom": 21401.50}
  ],
  "estimated_dir":     "up",
  "opposite_premove":  "no"
}
```

`liquidities` order is informational — consumers filter by direction or by `name`. `name` is the unique key for `last_liquidity` and for trend.py's opposite-liquidity check.

#### `hypothesis.json` — current-move state

```json
{
  "direction":      "up",
  "weekly_mid":     "above",
  "daily_mid":      "mid",
  "last_liquidity": "day_low",
  "divs": [
    {"timeframe": "15m", "type": "wick", "side": "bullish", "time": "2026-04-27T10:30:00-04:00"}
  ],
  "targets": [
    {"name": "day_high", "price": 21425.00},
    {"name": "TDO",      "price": 21412.50}
  ],
  "cautious_price": "",
  "entry_ranges": [
    {"source": "12hr",  "low": 21401.50, "high": 21408.00},
    {"source": "1week", "low": 21395.00, "high": 21402.25}
  ]
}
```

Empty `direction` is the literal string `"none"` (per ideas.md). `cautious_price` is `""` (empty string) when not set; trend.py treats empty as "no cautious threshold".

#### `position.json` — open position + pending entry state

```json
{
  "active": {
    "time":       "2026-04-27T10:35:00-04:00",
    "fill_price": 21408.50,
    "direction":  "up",
    "stop":       21401.00,
    "contracts":  2,
    "cautious":   "no"
  },
  "limit_entry": "",
  "confirmation_bar": {
    "time":      "2026-04-27T10:30:00-04:00",
    "high":      21410.00,
    "low":       21401.00,
    "body_high": 21408.00,
    "body_low":  21402.50
  },
  "failed_entries": 0
}
```

`active` is `{}` when no position. `limit_entry` is `""` when none pending; otherwise a price float. `confirmation_bar` is `{}` when none.

### Module specifications

#### `daily.py` — once per day at 09:20 ET

Entry: `run_daily(now: datetime, mnq_1m: pd.DataFrame, hist_mnq_1m: pd.DataFrame, hist_hourly_mnq: pd.DataFrame) -> None`

Purpose: write `daily.json`, update `global.json`, reset session-scoped fields in `position.json`, set `hypothesis.direction = "none"`.

Steps:
1. Read existing `daily.json`'s `liquidities` (used as a baseline; recomputed each call).
2. Compute liquidities from `mnq_1m` + `hist_mnq_1m` + `hist_hourly_mnq`:
   - `TDO` (True Day Open) via `strategy_smt.compute_tdo(mnq_1m, now.date())`
   - `TWO` (True Week Open) — the `Open` of the first 1m bar of the current ISO week from `hist_mnq_1m` (Sunday 18:00 ET futures-week start, or Monday 00:00 ET fallback if Sunday absent). Computed inline; no helper exists.
   - `week_high` / `week_low` from `hist_mnq_1m` filtered to current ISO week
   - `day_high` / `day_low` from `mnq_1m` filtered to today
   - `asia_high/low` (18:00–03:00 ET prior session), `london_high/low` (03:00–08:00), `ny_morning_high/low` (08:00–12:00), `ny_evening_high/low` (12:00–17:00) — windows defined as constants in `daily.py`
   - Recent unvisited FVGs from 1hr bars over last 3 trading days. Resample `mnq_1m` (today + last 3 trading days from `hist_mnq_1m`) to 1h with round-bar-init labels; scan with `strategy_smt.detect_fvg`-style triple-bar test; "unvisited" = no subsequent 1m bar's H/L re-entered the gap range after formation. Each surviving FVG produces one liquidity entry with `kind="fvg"`, `top`, `bottom`, and a `name` formed as `fvg_<formation_ts>_<bull|bear>`.
3. Read `global.json`'s `all_time_high`. If `max(today_high) > all_time_high`, write the new value back to `global.json`.
4. `daily.estimated_dir = global.trend` (TBD).
5. `daily.opposite_premove = "no"` (TBD).
6. `hypothesis.json.direction = "none"`. (Other hypothesis fields untouched here — `hypothesis.py` rewrites them.)
7. Reset `position.json` per-session fields:
   - `active = {}`
   - `limit_entry = ""`
   - `confirmation_bar = {}`
   - `failed_entries = 0`

Finite duration: pure compute over passed-in DataFrames. No I/O beyond JSON files.

#### `hypothesis.py` — every 5m during session

Entry: `run_hypothesis(now: datetime, mnq_1m: pd.DataFrame, mes_1m: pd.DataFrame, hist_mnq_1m: pd.DataFrame, hist_mes_1m: pd.DataFrame) -> None`

Steps:
1. Read `hypothesis.json`. If `direction != "none"`, return.
2. Read `global.json.all_time_high`. Build current 5m bar (round-down to last 5m boundary). If `bar.low > all_time_high` AND `bar.high > all_time_high`, return. (Both wicks above ATH → no entry.)
3. Compute `weekly_mid` and `daily_mid`:
   - week extreme range = `(week_high - week_low)` from `daily.json.liquidities`
   - week mid = `(week_high + week_low) / 2`
   - same for day
   - if `|current_close - mid| <= 10`: `"mid"`; else `"above"` / `"below"`
4. `last_liquidity` = the most recently-touched name from `daily.json.liquidities` looking backward at `mnq_1m`. Restricted to the meaningful set: `{week_high, week_low, day_high, day_low}`.
5. `divs` = list of 15m/30m SMT divergences detected via `strategy_smt.detect_smt_divergence` and `strategy_smt.detect_smt_fill` between MNQ and MES (resampled to 15m and 30m respectively). Three types: `"wick"`, `"body"` (hidden, close-based — already supported by `detect_smt_divergence` returning `smt_type`), `"fill"` (FVG-fill divergence — `detect_smt_fill`).
6. `direction` (TBD): hardcoded `"up"` for now.
7. `targets` = filter `daily.json.liquidities` to entries strictly in the direction of `direction` from current price. For `kind=level`: include if `price > current` (when direction=up) or `price < current` (down). For `kind=fvg`: include if `bottom > current` (up) or `top < current` (down).
8. `cautious_price = ""` (TBD).
9. `entry_ranges`: for each of `{12hr-ago, 1-week-ago-same-time}`, find the 15m or 30m bar at that anchor and report `{low, high}` of its discount/premium wick — discount wick for longs (the lower wick), premium wick for shorts (the upper wick). Direction comes from current `direction`; if `"up"`, return discount; if `"down"`, return premium.
10. **If on entry the file's `direction` was `"none"` and the new computed `direction != "none"`, also reset `position.failed_entries = 0` and clear `position.confirmation_bar = {}`.**

Finite duration: pure compute, then JSON writes.

#### `strategy.py` — every completed 5m bar

Entry: `run_strategy(now: datetime, mnq_5m_bar: dict, mnq_1m_recent: pd.DataFrame) -> Optional[Signal]`

`mnq_5m_bar` is the just-completed 5m bar built by accumulating five 1m bars on round 5m boundaries (10:00, 10:05, …). Caller (backtest harness or realtime daemon) builds it.

Steps:
1. Read `hypothesis.json.direction` and `position.json`.
2. If `position.active == {}`:
   1. If `direction == "none"` OR `position.failed_entries > 2`: return `None`.
   2. Define `body_opposite_to(direction)` = `True` if `(direction == "up" and close < open)` or `(direction == "down" and close > open)`. I.e. an `up` hypothesis's "opposite" 5m bar is a bearish (red) candle; a `down` hypothesis's "opposite" is bullish (green).
   3. If `body_opposite_to(direction)`:
      - Write `position.confirmation_bar` = `{time, high, low, body_high, body_low}` (overwrite).
      - Compute `body_end_price` = `body_low` (for shorts, dir=down) or `body_high` (for longs, dir=up).
      - If `position.limit_entry == ""`: emit `new-limit-entry` signal, set `position.limit_entry = body_end_price`.
      - Else: emit `move-limit-entry` signal, set `position.limit_entry = body_end_price`.
      - Return signal (caller routes to broker / records).
   4. Else (no new opposite 5m bar): check fill — if `position.limit_entry != ""` AND the just-closed 5m bar's H/L crossed `limit_entry`:
      - Open position. Set `position.active = {time: bar.time, fill_price: limit_entry, direction, stop: confirmation_bar.high (for short) | .low (for long), contracts: 2, cautious: "no"}`.
      - Clear `position.limit_entry = ""`.
      - Clear `position.confirmation_bar = {}`.
      - Emit `limit-entry-filled` signal. Return.
3. Else (`position.active` set):
   1. If `direction == "none"` OR `direction != position.active.direction`: emit `market-close` (reason: `direction-mismatch`); clear `active = {}`, `limit_entry = ""`. Return.
   2. Else if `mnq_5m_bar` H/L crossed `position.active.stop`: emit `stopped-out`; clear `active`, `limit_entry`; `position.failed_entries += 1`. Return.

Per-bar fill precedence within step 2: a single bar that *both* generates a new opposite confirmation AND crosses an existing `limit_entry` is interpreted by ideas.md 2.2.1 as "new confirmation overrides" — emit `move-limit-entry`, do not fill. (Justification: the new confirmation bar invalidates the prior entry premise.)

#### `trend.py` — every 1m bar

Entry: `run_trend(now: datetime, mnq_1m_bar: dict, mnq_1m_recent: pd.DataFrame) -> Optional[Signal]`

Steps:
1. Read `hypothesis.json.direction`, `position.json`, `daily.json.liquidities`.
2. If `direction == "none"`: return `None`.
3. If `position.active != {}`:
   1. If `position.active.cautious == "no"` AND `cautious_price != ""` AND the 1m bar surpassed `cautious_price`:
      - "Surpassed" for `direction == "up"`: `bar.high >= cautious_price`. For `direction == "down"`: `bar.low <= cautious_price`.
      - If `bar.close` is beyond `cautious_price` (up: `close > cautious_price`; down: `close < cautious_price`): set `position.active.cautious = "yes"`. Return informational signal `cautious-armed`.
      - Else (touched but did not close beyond): emit `market-close` (reason: `cautious-rejected`); clear `active`, `limit_entry`; set `hypothesis.direction = "none"`; clear `confirmation_bar`. Return.
   2. Else if `position.active.cautious == "yes"`:
      - If price crossed back to pre-cautious side — for `direction == "up"`: `bar.low <= cautious_price`; for `direction == "down"`: `bar.high >= cautious_price`: emit `market-close` (reason: `cautious-reversal`); clear; reset hypothesis. Return.
      - Else if 1m bar surpassed the *last opposite 1m bar*. Construction:
        - "Last opposite 1m bar" = most recent 1m bar in `mnq_1m_recent` (excluding the current bar) with body opposite to `direction` — for `direction == "up"`, the most recent bar with `close < open`; for `direction == "down"`, the most recent bar with `close > open`.
        - "Surpassed" — for `direction == "up"`: current `bar.low <= last_opposite.low` (broke below the recent bearish 1m's low); for `direction == "down"`: current `bar.high >= last_opposite.high`.
        - On surpass: emit `market-close` (reason: `cautious-1m-break`); clear; reset hypothesis. Return.
4. Else (`position.active == {}`):
   - If 1m bar surpassed any liquidity in `daily.json.liquidities` whose price is in the direction *opposite* to `hypothesis.direction` from current price:
     - Set `hypothesis.direction = "none"`.
     - Clear `position.confirmation_bar = {}`, `position.limit_entry = ""`.
     - Return informational signal `trend-broken`.

`cautious_price`'s "post-cautious side" interpretation: for `direction == "up"` the trade expects price to keep rising; `cautious_price` is *above* the entry; "beyond" means `close > cautious_price`. For `direction == "down"`: `cautious_price` is below entry; "beyond" = `close < cautious_price`. Symmetric for "pre-cautious".

### Per-bar dispatch order

Per `ideas.md` Q1 resolution → trend.py runs *before* strategy.py.

| Bar boundary | Modules invoked, in order |
|---|---|
| 09:20 ET (session start) | `daily.py` |
| 1m boundary, not 5m | `trend.py` |
| 5m boundary | `hypothesis.py` → `trend.py` → `strategy.py` |

Rationale:
- `hypothesis.py` first so `direction` is current before consumers read it.
- `trend.py` before `strategy.py` so a same-bar trend-invalidation closes the position before strategy.py would have processed an entry. A bar that simultaneously invalidates trend *and* would have filled a limit closes instead of fills; the next bar sees `active = {}`, `direction = "none"`, and strategy.py exits early.

### Signal record (caller contract)

All four modules return `Optional[Signal]` where `Signal` is:

```python
{
  "kind":      "new-limit-entry" | "move-limit-entry" | "limit-entry-filled"
             | "market-close"    | "stopped-out"       | "cautious-armed"
             | "trend-broken"    | "cautious-rejected" | "cautious-reversal"
             | "cautious-1m-break",
  "time":      "2026-04-27T10:35:00-04:00",
  "price":     21408.50,
  "reason":    "direction-mismatch" | ...,    # market-close subtype only
  "payload":   { ...module-specific... }      # e.g. confirmation_bar dict, exit P&L hints
}
```

Caller (backtest harness / realtime daemon) handles persistence: events.jsonl row per signal, trades.tsv row built from open + close pair, IB order routing for realtime.

### `smt_state.py` — JSON-IO contract

Pure utility module. No business logic.

```python
def load_global() -> dict
def save_global(d: dict) -> None
def load_daily() -> dict
def save_daily(d: dict) -> None
def load_hypothesis() -> dict
def save_hypothesis(d: dict) -> None
def load_position() -> dict
def save_position(d: dict) -> None

DEFAULT_GLOBAL     = {"all_time_high": 0.0, "trend": "up"}
DEFAULT_DAILY      = {"date": "", "liquidities": [], "estimated_dir": "up", "opposite_premove": "no"}
DEFAULT_HYPOTHESIS = {"direction": "none", "weekly_mid": "", "daily_mid": "",
                      "last_liquidity": "", "divs": [], "targets": [],
                      "cautious_price": "", "entry_ranges": []}
DEFAULT_POSITION   = {"active": {}, "limit_entry": "", "confirmation_bar": {}, "failed_entries": 0}
```

All four files live in `data/` (alongside parquets). Files autocreated with defaults if missing. Atomic writes via `tmp + os.replace`.

### Backtest harness adaptation

New entry point in `backtest_smt.py`:

```python
def run_backtest_v2(start_date: str, end_date: str, *, write_events: bool = True) -> dict
```

Behaviour:
- Loads MNQ 1m + MES 1m parquets via existing `load_futures_data()`.
- For each trading day in range:
  1. At 09:20 ET, call `daily.py.run_daily(...)`. (`daily.py` itself resets per-session position fields.)
  2. Iterate 1m bars from 09:20 to 16:00 ET:
     - On every 1m boundary that is **not** also a 5m boundary: call `trend.py.run_trend(...)`. Append signal to `events.jsonl`.
     - On every 5m boundary (e.g. 09:25, 09:30, …): build the just-completed 5m bar by accumulating the prior five 1m bars (10:00–10:04 → 5m bar timestamped 10:00, dispatched at 10:05). Call in order: `hypothesis.py` → `trend.py` (with the current 1m bar) → `strategy.py` (with the 5m bar). Append all returned signals to `events.jsonl`.
  3. At end of day, build trade records from paired entry / exit signals in `events.jsonl`. Append rows to `trades.tsv`.
- Returns metrics dict (trade count, P&L, win rate by direction).

The existing `run_backtest()` is untouched.

### Realtime harness adaptation

In `signal_smt.py`, add a parallel dispatcher class `SmtV2Dispatcher` (or inline functions) wired to existing IB subscriptions:
- 1m bar callback → call `trend.py`.
- Every five 1m bars on round boundary → build 5m bar, call `hypothesis.py` → `trend.py` → `strategy.py`.
- 09:20 ET wall-clock trigger → `daily.py`.

Signals are dispatched to the existing JSON-line stdout printer + position.json writer (already present at lines 873/951/982/1065 — the writer is reused, but the dict shape it persists is the new schema).

Existing `_process_scanning` / `_process_managing` paths remain in the file but the new dispatcher is selected via an environment variable or CLI flag (e.g. `SMT_PIPELINE=v2`). Default stays on current path until v2 is validated.

### Reusable primitives — imported, not copied

From `strategy_smt.py`:
- `detect_smt_divergence` (l.755)
- `detect_smt_fill` (l.1173)
- `detect_fvg` (l.1107)
- `compute_tdo` (l.880)
- `compute_midnight_open` (l.903)
- `compute_overnight_range` (l.920)
- `compute_pdh_pdl` (l.939)
- `detect_eqh_eql` (l.1070)
- `is_confirmation_bar` (l.1228)
- `build_synthetic_confirmation_bar` (l.619)
- `load_futures_data` / `load_futures_manifest`

These are pure helpers. No coupling to the retired state machine.

### Retired but resident

Untouched in this change:
- `strategy_smt.process_scan_bar`, `strategy_smt.manage_position`, `strategy_smt.screen_session`, `ScanState`, `SessionContext`
- `hypothesis_smt.HypothesisManager` and the rules pipeline
- Existing `signal_smt._process_scanning` / `_process_managing`
- Existing `backtest_smt.run_backtest`
- Their tests

The new path is selectable; the old path keeps running. Pruning is a follow-up.

### Regression testing

`regression.md` format — one entry per line, `#` for comments:

```
# Sanity dates — known good behaviour
2025-11-14
2026-01-08

# Range — inclusive
2026-02-15:2026-02-19

# Single date with note
2026-03-12   # SMT body-divergence morning
```

Parser: ignore blank lines and `#`-prefixed; split on `:` for ranges; trim inline `#` comments.

`regression.py` entry:

```python
def run_regression(regression_md_path: str = "regression.md", *, update_baseline: bool = False) -> dict
```

Per date:
1. Run `run_backtest_v2(date, date, write_events=True)`.
2. Write `data/regression/<date>/events.jsonl` and `trades.tsv`.
3. If `update_baseline`: copy outputs to `baseline_events.jsonl` / `baseline_trades.tsv` and skip diff.
4. Else: diff against existing baselines.
   - **events diff (the actual regression signal):** line-by-line strict diff, after canonical JSON sort_keys serialisation. Any difference → fail.
   - **trades diff (human-readable artefact):** TSV diff with same column ordering. Any difference → fail.
5. Return dict: `{date: {events_match: bool, trades_match: bool, n_trades: int, pnl: float}}`.

Exit code: 0 if all dates pass both diffs; 1 otherwise.

Initial baselines are produced by running `run_regression(update_baseline=True)` once and committing the outputs.

## Tests

New tests, all under `tests/`:
- `test_smt_state.py` — JSON load/save round-trip, defaults applied when files missing, atomic write on simulated crash.
- `test_smt_daily.py` — given a fixture day's 1m bars, assert daily.json contents (TDO, week H/L, session H/L, FVG list); assert global.json all_time_high update; assert position.json reset.
- `test_smt_hypothesis.py` — fixture cases for each rule (early-exit on direction set, early-exit on bar above ATH, weekly_mid above/mid/below classification with 10pt boundary, last_liquidity selection, divs from synthetic divergence, targets filtered by direction, entry_ranges from 12hr / 1wk anchors).
- `test_smt_strategy.py` — fixture cases: no-position direction-none early-exit; failed_entries>2 early-exit; new opposite 5m bar emits new-limit-entry; second opposite 5m bar emits move-limit-entry; subsequent bar crossing limit_entry emits limit-entry-filled and writes active; in-position direction-flip emits market-close; in-position stop crossed emits stopped-out and increments failed_entries.
- `test_smt_trend.py` — fixture cases: cautious arming (close beyond), cautious-rejected (close pre-cautious), cautious-yes reversal, cautious-yes 1m-break, no-position trend-broken on opposite-liquidity surpass.
- `test_smt_dispatch_order.py` — synthetic bar where both 5m fill condition and trend invalidation are true: assert only `market-close` from trend.py, no `limit-entry-filled` from strategy.py.
- `test_smt_regression.py` — runs `run_regression` on a small fixture date, asserts pass against committed baseline; corrupts events.jsonl, asserts fail.

Existing tests are unmodified and must continue to pass.

## Acceptance criteria

1. The four JSON files exist with the documented schemas; `smt_state.py` round-trips them.
2. `daily.py` invoked at 09:20 produces a `daily.json` whose `liquidities` list contains all required entries for a fixture day's parquet slice.
3. `hypothesis.py` honours all early-exit conditions and produces a hypothesis.json matching the schema.
4. `strategy.py` emits the four signal kinds (`new-limit-entry`, `move-limit-entry`, `limit-entry-filled`, `market-close`, `stopped-out`) under their respective conditions and updates position.json correctly.
5. `trend.py` emits cautious progression and the no-position `trend-broken` reset under their respective conditions.
6. `failed_entries` resets on hypothesis transition from `none` → `up`/`down`.
7. Per-bar dispatch order matches the table; the two-modules-fire-same-bar test passes.
8. `run_backtest_v2` runs end-to-end on at least one fixture day with no exceptions and produces deterministic events.jsonl + trades.tsv.
9. `run_regression` over a small `regression.md` returns matching diffs against committed baselines.
10. Realtime path: invoking `signal_smt.py` with `SMT_PIPELINE=v2` connects to IB, subscribes, and the new dispatcher receives bars (smoke test only — no fills required).
11. Existing test suite (`pytest`) still green.

## Open items deliberately deferred

- Real `estimated_dir` estimation in `daily.py` (currently reads `global.trend`).
- Real `direction` estimation in `hypothesis.py` (currently `"up"`).
- Real `cautious_price` estimation in `hypothesis.py` (currently `""`).
- Real `opposite_premove` logic (currently `"no"`).
- Production scheduler for the realtime daemon (initial cut piggybacks on `signal_smt.py`'s IB callbacks).
- Retiring `strategy_smt.py` / `hypothesis_smt.py` and pruning their tests.
