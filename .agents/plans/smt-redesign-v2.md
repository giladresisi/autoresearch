# Feature: SMT Redesign — JSON-File Architecture + Module Decomposition

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import primitives from `strategy_smt.py` rather than re-implementing. Do NOT touch `strategy_smt.py` / `hypothesis_smt.py` / their tests. The only edits to `signal_smt.py` and `backtest_smt.py` are *additive* (a new dispatcher path and a new entry point); existing paths must keep working.

**Spec**: `docs/superpowers/specs/2026-04-27-smt-redesign-design.md` — read it first; this plan implements it.

---

## Feature Description

Rebuild the SMT control flow as four small focused modules (`daily.py` once at 09:20 ET, `hypothesis.py` every 5m, `strategy.py` every completed 5m bar, `trend.py` every 1m) communicating exclusively through four JSON files in `data/` (`global.json`, `daily.json`, `hypothesis.json`, `position.json`). All entry / exit / cautious / trend-invalidation logic moves into the new modules; primitive helpers (SMT divergence detection, FVG detection, TDO / PDH / PDL / EQH-EQL computation, confirmation-bar utilities) are imported from the existing `strategy_smt.py`. The existing rich state machine (TP, breakeven, trail, secondary target, Layer-B FVG re-add, MSS / CISD / opposing-displacement invalidations) is *not* in the new path — it stays resident in `strategy_smt.py` for the existing dispatcher only.

## User Story

As the strategy author
I want the SMT decision logic split across four small JSON-glued modules with deterministic per-date regression coverage
So that I can iterate on each piece independently and verify behaviour preservation on real historical days

## Problem Statement

`strategy_smt.py` (~2555 lines) and `hypothesis_smt.py` (~954 lines) bundle entry detection, exit logic, breakeven/trail/secondary-target, layered re-entry, hypothesis voting, and Claude-API integration into monolithic control flows. Adding the simpler model in `ideas.md` on top of this surface is harder than rebuilding the control flow as small files. The user has already chosen option-1 layout and explicitly opted out of TP/partials/breakeven/trail/secondary/MSS/CISD/displacement.

## Solution Statement

Build the new control flow as fresh files alongside the old. Reuse primitives by import. Gate the v2 path in `signal_smt.py` behind `SMT_PIPELINE=v2`; expose a separate `run_backtest_v2` entry in `backtest_smt.py`. Persistence is via four JSON files in `data/`. Specific-day regression compares both `events.jsonl` (every signal emitted) and `trades.tsv` (paired entry/exit records) against committed baselines.

## Feature Metadata

**Feature Type**: Refactor (control-flow rebuild — new files alongside old)
**Complexity**: High
**Primary Systems Affected**: SMT strategy pipeline (4 new modules), backtest harness (additive entry), realtime daemon (additive dispatcher), regression bench (new)
**Dependencies**: No new external libraries. Internal imports from `strategy_smt.py` (primitives) and existing `data/sources.py` (parquet loading).
**Breaking Changes**: No — old path remains the default. New v2 path is opt-in via env flag and a separate backtest entry function.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `docs/superpowers/specs/2026-04-27-smt-redesign-design.md` — full design; this plan is its execution contract
- `ideas.md` — original design source; spec resolves its ambiguities
- `strategy_smt.py:432-499` — `set_bar_data` / `init_bar_data` / `append_bar_data` global frame (read-only — do not modify)
- `strategy_smt.py:619-644` — `build_synthetic_confirmation_bar` (reuse for 5m bar building)
- `strategy_smt.py:755-878` — `detect_smt_divergence` (reuse in `hypothesis.py`)
- `strategy_smt.py:880-936` — `compute_tdo`, `compute_midnight_open`, `compute_overnight_range` (reuse in `daily.py`)
- `strategy_smt.py:939-956` — `compute_pdh_pdl` (reuse in `daily.py` for context)
- `strategy_smt.py:1070-1106` — `detect_eqh_eql` (available; not used in v1 cut)
- `strategy_smt.py:1107-1145` — `detect_fvg` (reuse in `daily.py` for 1hr FVG scan)
- `strategy_smt.py:1148-1170` — `detect_displacement` (available; not used)
- `strategy_smt.py:1173-1203` — `detect_smt_fill` (reuse in `hypothesis.py`)
- `strategy_smt.py:1228-1244` — `is_confirmation_bar` (reuse in `strategy.py`)
- `signal_smt.py:65, 308, 372-454, 873-982, 1027-1117` — IB callbacks, parquet loaders, position.json IO patterns, main entry — mirror these patterns for the v2 dispatcher
- `backtest_smt.py:148-185, 287-389, 941-1040` — sizer, trade-record builder, walk-forward fold logic, results writers — mirror trade-record schema in v2
- `data/sources.py` — parquet loader API (untouched)
- `strategy_smt.load_futures_data` / `strategy_smt.load_futures_manifest` — parquet loader used by `run_backtest_v2` in Task 3.1; do not re-implement
- `tests/conftest.py` — synthetic OHLCV fixtures (extend, do not replace)

### New Files to Create

- `smt_state.py` — JSON load/save for the four state files; defaults; atomic writes
- `daily.py` — once-per-session computation; writes `daily.json`, updates `global.json`, resets `position.json`
- `hypothesis.py` — 5m loop; writes `hypothesis.json`
- `strategy.py` — per-5m-bar entry / stop / direction-mismatch
- `trend.py` — per-1m-bar cautious + trend invalidation
- `regression.py` — runs specific-day regression, diffs against baselines
- `regression.md` — list of dates / date-ranges (initially: 2–3 sanity dates)
- `data/global.json`, `data/daily.json`, `data/hypothesis.json` — created on first run via `smt_state.py` defaults
- `data/regression/<YYYY-MM-DD>/{events.jsonl,trades.tsv,baseline_events.jsonl,baseline_trades.tsv}` — produced by regression bench
- `tests/test_smt_state.py`
- `tests/test_smt_daily.py`
- `tests/test_smt_hypothesis.py`
- `tests/test_smt_strategy.py`
- `tests/test_smt_trend.py`
- `tests/test_smt_dispatch_order.py`
- `tests/test_smt_regression.py`

### Patterns to Follow

**JSON IO**: atomic write via `tmp.write_text(...)` + `os.replace(tmp, dst)` (mirrors `hypothesis_smt.py:919`). Default-on-missing pattern: `json.loads(p.read_text()) if p.exists() else copy.deepcopy(DEFAULT_X)`.

**Module signatures**: pure functions; caller passes bars + datetime; modules read/write JSON files directly via `smt_state.py`. No internal parquet loading in the four logic modules.

**Signal records**: dataclass-or-dict, no enums. JSON-serializable. Mirror `signal_smt._dispatch_event` (l.487+) record shape — extend, do not redefine.

**Naming conventions**: snake_case modules and functions; `run_<module>` as entry point; `_helper` for private. Match existing `strategy_smt.py` style.

**Tests**: `pytest`, fixtures from `tests/conftest.py`; per-module test file mirrors module name.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────────────┐
│ WAVE 1: State Foundation (1 task — blocks all)              │
├────────────────────────────────────────────────────────────┤
│ Task 1.1: smt_state.py + DEFAULTS + tests                   │
└────────────────────────────────────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 2: Logic Modules (4 parallel — independent)            │
├──────────────┬──────────────┬──────────────┬─────────────┤
│ 2.1 daily.py │ 2.2 hypoth.. │ 2.3 strategy │ 2.4 trend.py │
│ + tests      │ + tests      │ + tests      │ + tests      │
└──────────────┴──────────────┴──────────────┴─────────────┘
                            ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 3: Harness Wiring (2 parallel — depend on Wave 2)     │
├─────────────────────────────┬───────────────────────────────┤
│ 3.1 backtest_smt v2 entry   │ 3.2 signal_smt v2 dispatcher │
│ + dispatch-order test        │ (env-gated)                  │
└─────────────────────────────┴───────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 4: Regression Bench (sequential — depends on 3.1)     │
├────────────────────────────────────────────────────────────┤
│ 4.1 regression.py + regression.md + initial baselines      │
│      + test_smt_regression.py                              │
└────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Sequential (1 task)**: Task 1.1 — pure foundation; everything else needs `smt_state.py`.
**Wave 2 — Fully Parallel (4 tasks)**: Tasks 2.1, 2.2, 2.3, 2.4 — modules are independent of each other; they only share the `smt_state.py` IO surface from Wave 1.
**Wave 3 — Parallel (2 tasks)**: Tasks 3.1, 3.2 — backtest entry and signal dispatcher consume the same Wave 2 modules but never modify each other.
**Wave 4 — Sequential (1 task)**: Task 4.1 — needs the backtest harness from 3.1 to produce baselines.

7 tasks total · 4 parallelizable in Wave 2 · 2 parallelizable in Wave 3 · ~85% of tasks parallelizable.

### Interface Contracts

**Contract 1 (Wave 1 → Wave 2)**: `smt_state.py` exposes `load_<name>` / `save_<name>` for each of the four files plus `DEFAULT_<NAME>` dicts. Wave 2 modules import these — no other shared imports between Wave 2 modules.

**Contract 2 (Wave 2 → Wave 3)**: Each Wave 2 module exposes one entry point — `run_daily(now, mnq_1m, hist_mnq_1m, hist_hourly_mnq) -> None`, `run_hypothesis(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m) -> None`, `run_strategy(now, mnq_5m_bar, mnq_1m_recent) -> Optional[Signal]`, `run_trend(now, mnq_1m_bar, mnq_1m_recent) -> Optional[Signal]`. Wave 3 callers compose these. Note: `run_hypothesis` always returns `None` — it only writes JSON; it emits no caller-routable signal.

**Contract 3 (Signal record)**: dict with `kind`, `time` (ISO string), `price` (float), optional `reason`, optional `payload`. JSON-serializable. Spec § "Signal record (caller contract)" is authoritative.

**Mock for parallel work**: Wave 2 tasks may stub `smt_state.py` with an in-memory dict if Task 1.1 is mid-flight; final integration uses real disk IO.

### Synchronization Checkpoints

**After Wave 1**: `pytest tests/test_smt_state.py -v`
**After Wave 2**: `pytest tests/test_smt_daily.py tests/test_smt_hypothesis.py tests/test_smt_strategy.py tests/test_smt_trend.py -v`
**After Wave 3**: `pytest tests/test_smt_dispatch_order.py -v` and a smoke `python -c "from backtest_smt import run_backtest_v2; run_backtest_v2('2025-11-14','2025-11-14')"`
**Final**: `pytest tests/test_smt_regression.py -v && pytest -q` (full suite)

---

## IMPLEMENTATION PLAN

### Phase 1: State Foundation
JSON IO, defaults, atomic writes. Single dependency for everything downstream.

### Phase 2: Logic Modules (parallel)
The four pure-compute modules. Each reads via `smt_state.load_*`, writes via `smt_state.save_*`, returns an optional `Signal`.

### Phase 3: Harness Wiring (parallel)
Add `run_backtest_v2` to `backtest_smt.py` (additive — old `run_backtest` untouched). Add `SmtV2Dispatcher` class + env-flag selection to `signal_smt.py` (additive — old paths untouched).

### Phase 4: Regression Bench
`regression.py` walks dates from `regression.md`, runs `run_backtest_v2` per date, writes `events.jsonl` + `trades.tsv` to `data/regression/<date>/`, diffs against `baseline_*` files. Generate initial baselines for 2–3 sanity dates and commit them as part of this task.

---

## STEP-BY-STEP TASKS

### WAVE 1: State Foundation

#### Task 1.1: CREATE `smt_state.py` and `tests/test_smt_state.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-utilities
- **DEPENDS_ON**: []
- **BLOCKS**: 2.1, 2.2, 2.3, 2.4
- **PROVIDES**: JSON IO surface for all four state files; default dicts; atomic write helper
- **IMPLEMENT**:
  - File constants: `DATA_DIR = Path("data")`; `GLOBAL_PATH = DATA_DIR / "global.json"`; same for `daily`, `hypothesis`, `position`.
  - Defaults exactly as in spec § "smt_state.py":
    ```python
    DEFAULT_GLOBAL     = {"all_time_high": 0.0, "trend": "up"}
    DEFAULT_DAILY      = {"date": "", "liquidities": [], "estimated_dir": "up", "opposite_premove": "no"}
    DEFAULT_HYPOTHESIS = {"direction": "none", "weekly_mid": "", "daily_mid": "",
                          "last_liquidity": "", "divs": [], "targets": [],
                          "cautious_price": "", "entry_ranges": []}
    DEFAULT_POSITION   = {"active": {}, "limit_entry": "", "confirmation_bar": {}, "failed_entries": 0}
    ```
  - Functions: `load_global()`, `save_global(d)`, and same for `daily`, `hypothesis`, `position`. Each `load`: (1) returns `copy.deepcopy(DEFAULT_*)` if file missing; (2) returns `copy.deepcopy(DEFAULT_*)` if the file exists but its top-level keys are not a superset of the corresponding `DEFAULT_*` keys (`not DEFAULT_X.keys() <= d.keys()`); (3) otherwise returns the parsed dict. Each `save` writes via `tmp.write_text(json.dumps(d, indent=2, sort_keys=True))` + `os.replace(tmp, dst)`.
  - One private `_atomic_write(path, payload_dict)` helper.
- **PATTERN**: Mirror `hypothesis_smt.py:919` for the atomic write pattern.
- **VALIDATE**: `pytest tests/test_smt_state.py -v`
- **TESTS** (`tests/test_smt_state.py`):
  - `test_load_returns_default_when_missing` — for each of the four files, delete it, call `load_*`, assert equals `DEFAULT_*` and is a deep copy (mutating return doesn't affect default).
  - `test_load_returns_default_when_schema_mismatch` — for each file, write a JSON file with only `{"foo": 1}` (missing all required keys), call `load_*`, assert returns `copy.deepcopy(DEFAULT_*)` and the bad file is left untouched on disk.
  - `test_save_then_load_roundtrip` — for each file, save a non-default dict, reload, assert equality.
  - `test_save_is_atomic` — patch `os.replace` to raise; assert original file (or absence) is preserved.
  - `test_save_uses_sort_keys_for_determinism` — save the same logical dict in different key orders, assert byte-identical files (matters for regression diff stability).

---

### WAVE 2: Logic Modules (parallel)

#### Task 2.1: CREATE `daily.py` and `tests/test_smt_daily.py`

- **WAVE**: 2
- **AGENT_ROLE**: strategy-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: 3.1, 3.2
- **PROVIDES**: `run_daily(now, mnq_1m, hist_mnq_1m, hist_hourly_mnq) -> None`
- **USES_FROM_WAVE_1**: `smt_state.load_global/save_global/load_daily/save_daily/load_position/save_position/load_hypothesis/save_hypothesis`
- **IMPLEMENT**: Per spec § "daily.py — once per day at 09:20 ET". Steps 1–7 verbatim. Imports from `strategy_smt`: `compute_tdo`, `compute_midnight_open`, `compute_overnight_range`, `detect_fvg`. TWO computed inline as the `Open` of the first 1m bar of the current ISO week (Sunday 18:00 ET futures-week start; fallback Monday 00:00 ET). Session windows (Asia/London/NY-morning/NY-evening) are module-level constants. Unvisited-FVG check: after detecting a 1hr FVG via 3-bar test, scan all subsequent 1m bars and exclude if any later H/L crossed the gap range.
- **PATTERN**: Reuse `strategy_smt.compute_tdo` (l.880) directly. Resample 1hr bars with `pd.DataFrame.resample('1H', label='left', origin='start_day').agg({...})` keyed to ET tz.
- **VALIDATE**: `pytest tests/test_smt_daily.py -v`
- **TESTS** (`tests/test_smt_daily.py`):
  - `test_writes_all_required_liquidity_names` — pass a fixture day; assert `daily.json.liquidities` contains each of: `TDO`, `TWO`, `week_high`, `week_low`, `day_high`, `day_low`, `asia_high`, `asia_low`, `london_high`, `london_low`, `ny_morning_high`, `ny_morning_low`, `ny_evening_high`, `ny_evening_low`, plus at least one `fvg_*` entry.
  - `test_two_is_first_1m_bar_open_of_week` — fixture spans a week; assert `TWO.price == hist_mnq_1m[<sunday-18:00-or-monday-00:00>].Open`.
  - `test_all_time_high_updates_when_today_higher` — preset `global.json.all_time_high = 100`; pass fixture with `day_high = 200`; assert `global.json.all_time_high == 200`.
  - `test_all_time_high_unchanged_when_today_lower` — preset `100`; fixture `day_high = 50`; assert still `100`.
  - `test_estimated_dir_copied_from_global_trend` — preset `global.trend = "down"`; assert `daily.estimated_dir == "down"`.
  - `test_opposite_premove_hardcoded_no` — assert `daily.opposite_premove == "no"`.
  - `test_position_per_session_fields_reset` — preset `position.json` with `active={...}, limit_entry=21000.0, confirmation_bar={...}, failed_entries=2`; assert all four reset to defaults after `run_daily`.
  - `test_hypothesis_direction_set_to_none` — preset `hypothesis.direction = "up"`; assert `"none"` after.
  - `test_unvisited_fvg_filter_excludes_filled_gaps` — fixture with two 1hr FVGs, one re-entered later; assert only one in `liquidities`.

#### Task 2.2: CREATE `hypothesis.py` and `tests/test_smt_hypothesis.py`

- **WAVE**: 2
- **AGENT_ROLE**: strategy-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: 3.1, 3.2
- **PROVIDES**: `run_hypothesis(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m) -> Optional[Signal]`
- **USES_FROM_WAVE_1**: `smt_state.load_*` / `save_*`
- **IMPLEMENT**: Per spec § "hypothesis.py — every 5m during session". Steps 1–10 verbatim. Imports from `strategy_smt`: `detect_smt_divergence`, `detect_smt_fill`. The `failed_entries` reset (step 10) MUST happen on `none → up/down` transition AND clear `position.confirmation_bar = {}` (also per spec). Returns `None` (this module emits no caller-routable signals — it only updates JSON).
- **VALIDATE**: `pytest tests/test_smt_hypothesis.py -v`
- **TESTS** (`tests/test_smt_hypothesis.py`):
  - `test_early_exit_when_direction_already_set` — preset `hypothesis.direction = "up"`; call; assert no fields besides `direction` changed.
  - `test_early_exit_when_5m_bar_fully_above_ath` — fixture: `bar.low > all_time_high`, `bar.high > all_time_high`; assert no hypothesis fields written.
  - `test_no_early_exit_when_only_one_extreme_above_ath` — only `bar.high > all_time_high`, `bar.low <= all_time_high`; assert proceeds.
  - `test_weekly_mid_above` — current_close > week_mid + 10pts → `"above"`.
  - `test_weekly_mid_below` — current_close < week_mid − 10pts → `"below"`.
  - `test_weekly_mid_mid_within_tolerance` — `|current_close − mid| <= 10` → `"mid"`.
  - `test_daily_mid_same_three_branches` — same triplet for `daily_mid`.
  - `test_last_liquidity_picks_most_recent_meaningful` — fixture price-touched `day_low` then `day_high`; assert `last_liquidity == "day_high"`.
  - `test_divs_includes_wick_body_and_fill_types` — fixture with all three; assert each `type` in returned `divs`.
  - `test_direction_hardcoded_up` — assert `direction == "up"` (TBD-acknowledged).
  - `test_targets_filtered_by_direction_for_levels` — `direction=up`, current_price=100; assert all `level` targets have `price > 100`.
  - `test_targets_filtered_by_direction_for_fvg` — FVG `bottom > current` included for up; `top < current` included for down.
  - `test_cautious_price_empty_string` — assert `cautious_price == ""`.
  - `test_entry_ranges_uses_12hr_and_1week_anchors` — assert two entries with `source` values `"12hr"` and `"1week"`, each with `low <= high`.
  - `test_failed_entries_reset_on_direction_transition_from_none` — preset `direction="none"`, `failed_entries=2`; assert post-call `failed_entries == 0` and `confirmation_bar == {}`.
  - `test_failed_entries_not_reset_when_direction_stays_set` — preset `direction="up"` (early-exit branch); assert `failed_entries` unchanged.

#### Task 2.3: CREATE `strategy.py` and `tests/test_smt_strategy.py`

- **WAVE**: 2
- **AGENT_ROLE**: strategy-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: 3.1, 3.2
- **PROVIDES**: `run_strategy(now, mnq_5m_bar, mnq_1m_recent) -> Optional[Signal]`
- **USES_FROM_WAVE_1**: `smt_state.load_*` / `save_*`
- **IMPLEMENT**: Per spec § "strategy.py — every completed 5m bar". Section 2 (no-position) and Section 3 (position) branches verbatim. `body_opposite_to(direction)` helper as defined. Same-bar fill-vs-new-confirmation precedence: a new opposite confirmation overrides any pending fill (emit `move-limit-entry`, do not fill — matches ideas.md 2.2.1's "override" semantics). Stop side: `confirmation_bar.high` for short, `.low` for long. `contracts = 2` literal. Signal `kind` strings exactly as in spec. Caller routes signal; this module updates `position.json`.
- **PATTERN**: Mirror `signal_smt._dispatch_event` (l.487+) for signal-record shape, but emit dicts not class instances.
- **VALIDATE**: `pytest tests/test_smt_strategy.py -v`
- **TESTS** (`tests/test_smt_strategy.py`):
  - `test_early_exit_when_direction_none` — preset `hypothesis.direction="none"`; assert returns `None`, no JSON mutations.
  - `test_early_exit_when_failed_entries_above_two` — preset `failed_entries=3`; assert returns `None`.
  - `test_failed_entries_exactly_two_still_allowed` — `failed_entries=2`; spec is `> 2`; assert NOT exited early.
  - `test_new_opposite_5m_emits_new_limit_entry` — preset empty position, `direction=up`, opposite (bearish) bar; assert `kind="new-limit-entry"`, `position.limit_entry == bar.body_high`, `position.confirmation_bar` populated.
  - `test_second_opposite_5m_emits_move_limit_entry` — preset existing `limit_entry`, `confirmation_bar`; new opposite bar; assert `kind="move-limit-entry"`, `limit_entry` updated to new body extreme.
  - `test_non_opposite_5m_no_signal_no_mutation` — preset empty position, `direction=up`, bullish bar (same direction); assert returns `None`, no mutations.
  - `test_5m_bar_crossing_limit_emits_filled_and_writes_active` — preset `limit_entry=100.0`, `direction=up`, bar `low <= 100 <= high`; assert `kind="limit-entry-filled"`, `position.active` populated with all spec fields, `limit_entry=""`, `confirmation_bar={}`.
  - `test_stop_side_short` — fill a SHORT; assert `position.active.stop == confirmation_bar.high`.
  - `test_stop_side_long` — fill a LONG; assert `position.active.stop == confirmation_bar.low`.
  - `test_in_position_direction_mismatch_emits_market_close` — `position.active.direction="up"`, `hypothesis.direction="down"`; assert `kind="market-close"`, `reason="direction-mismatch"`, `active={}`, `limit_entry=""`.
  - `test_in_position_direction_none_emits_market_close` — same as above but `hypothesis.direction="none"`.
  - `test_in_position_stop_crossed_emits_stopped_out_and_increments_failed` — `direction=up`, `stop=100`, bar `low <= 100`; preset `failed_entries=0`; assert `kind="stopped-out"`, `failed_entries==1`.
  - `test_same_bar_new_confirmation_and_fill_emits_only_move` — preset `limit_entry=100`, then new opposite bar that *also* has H/L spanning 100; assert `kind="move-limit-entry"` only, NOT `limit-entry-filled` (override semantics).
  - `test_signal_record_shape` — assert any returned signal has keys `kind`, `time`, `price`, and is JSON-serializable via `json.dumps`.

#### Task 2.4: CREATE `trend.py` and `tests/test_smt_trend.py`

- **WAVE**: 2
- **AGENT_ROLE**: strategy-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: 3.1, 3.2
- **PROVIDES**: `run_trend(now, mnq_1m_bar, mnq_1m_recent) -> Optional[Signal]`
- **USES_FROM_WAVE_1**: `smt_state.load_*` / `save_*`
- **IMPLEMENT**: Per spec § "trend.py — every 1m bar". All four cases: cautious-arming, cautious-rejected, cautious-yes-reversal, cautious-yes-1m-break, no-position trend-broken. Direction-aware "surpassed" / "beyond" / "pre-cautious-side" definitions verbatim. "Last opposite 1m bar" excludes the current bar and is the most recent bar with body opposite to `direction`. On every market-close path: clear `active`, clear `limit_entry`, set `hypothesis.direction="none"`, clear `confirmation_bar`. On `trend-broken`: same except no `active` to clear.
- **VALIDATE**: `pytest tests/test_smt_trend.py -v`
- **TESTS** (`tests/test_smt_trend.py`):
  - `test_early_exit_when_direction_none` — preset `direction="none"`; assert returns `None`.
  - `test_with_position_no_cautious_no_signal_when_below_threshold` — `direction="up"`, active set, `cautious="no"`, `cautious_price=110`, bar.high=105; assert no signal.
  - `test_cautious_arming_long_close_beyond` — `direction="up"`, `cautious_price=110`, bar.high=112, bar.close=111; assert `kind="cautious-armed"`, `active.cautious=="yes"`.
  - `test_cautious_rejected_long_close_below` — `direction="up"`, `cautious_price=110`, bar.high=112, bar.close=109; assert `kind="market-close"`, `reason="cautious-rejected"`, `hypothesis.direction=="none"`.
  - `test_cautious_arming_short_close_beyond` — `direction="down"`, `cautious_price=90`, bar.low=88, bar.close=89; assert `cautious-armed`.
  - `test_cautious_yes_reversal_long` — `cautious="yes"`, `direction="up"`, `cautious_price=110`, bar.low=109; assert `kind="market-close"`, `reason="cautious-reversal"`.
  - `test_cautious_yes_reversal_short` — symmetric.
  - `test_cautious_yes_1m_break_long` — `cautious="yes"`, `direction="up"`, `mnq_1m_recent` ends with bearish bar with low=100, current bar.low=99; assert `kind="market-close"`, `reason="cautious-1m-break"`.
  - `test_cautious_yes_1m_break_short` — symmetric (bullish opposite bar, current bar.high crosses).
  - `test_no_position_no_opposite_liquidity_no_signal` — empty active, `direction="up"`, current bar surpasses no opposite (down-direction) liquidity; assert no signal.
  - `test_no_position_opposite_liquidity_break_emits_trend_broken` — empty active, `direction="up"`, fixture has `day_low=50` in liquidities, current bar.low<=50; assert `kind="trend-broken"`, `hypothesis.direction=="none"`, `confirmation_bar=={}`, `limit_entry==""`.
  - `test_cautious_price_empty_string_skips_arming` — `cautious_price=""`; assert no `cautious-armed` even if bar would have crossed.
  - `test_signal_record_shape` — same as 2.3.

**Wave 2 Checkpoint**: `pytest tests/test_smt_daily.py tests/test_smt_hypothesis.py tests/test_smt_strategy.py tests/test_smt_trend.py -v`

---

### WAVE 3: Harness Wiring (parallel)

#### Task 3.1: ADD `run_backtest_v2` to `backtest_smt.py` and CREATE `tests/test_smt_dispatch_order.py`

- **WAVE**: 3
- **AGENT_ROLE**: integration-specialist
- **DEPENDS_ON**: [2.1, 2.2, 2.3, 2.4]
- **BLOCKS**: 4.1
- **PROVIDES**: `run_backtest_v2(start_date, end_date, *, write_events=True) -> dict`; per-bar dispatch order verified
- **USES_FROM_WAVE_2**: All four `run_*` entry points
- **IMPLEMENT**:
  - Add new function `run_backtest_v2(start_date: str, end_date: str, *, write_events: bool = True) -> dict` at the bottom of `backtest_smt.py`. Do NOT modify `run_backtest`.
  - Load 1m parquets for MNQ + MES via existing `load_futures_data()` (covering `start_date - 14 days` to `end_date` for the historical windows daily/hypothesis need).
  - For each business day in `[start_date, end_date]`:
    1. At 09:20 ET, call `daily.run_daily(now, mnq_1m_today_so_far, hist_mnq_1m, hist_hourly_mnq)`. (Build `hist_hourly_mnq` by resampling.)
    2. Iterate 1m bars from 09:20 to 16:00 ET:
       - At every 1m boundary that is NOT a 5m boundary: call `trend.run_trend(now, current_1m_bar, mnq_1m_recent)`. Append any returned signal to `events.jsonl`.
       - At every 5m boundary (i.e. `minute % 5 == 0` after 09:20): build the just-completed 5m bar from the prior five 1m bars (use `strategy_smt.build_synthetic_confirmation_bar` — l.619 — or a similar inline aggregator). Call IN ORDER: `hypothesis.run_hypothesis(...)` → `trend.run_trend(now, mnq_1m_bars[-1], mnq_1m_recent)` (pass the 5th/last 1m bar of the completing 5m bar as `mnq_1m_bar`) → `strategy.run_strategy(now, mnq_5m_bar, mnq_1m_recent)`. Append all returned signals. Note: `run_hypothesis` returns `None` — do not append its return value.
  - End-of-day: pair entry signals (`limit-entry-filled`) with the next exit signal (`market-close` / `stopped-out`); emit one trade record per pair to `trades.tsv`. Trade-record schema mirrors `_build_trade_record` (l.185) — at minimum: `entry_time, entry_price, direction, contracts, exit_time, exit_price, exit_reason, pnl_points, pnl_dollars`.
  - Output dir: `data/regression/<date>/events.jsonl` and `trades.tsv` if `write_events=True`; otherwise return in-memory.
  - Return dict: `{"trades": [...], "events": [...], "metrics": {n_trades, total_pnl, win_rate}}`.
- **PATTERN**: `_build_trade_record` (l.185) — mirror columns. `_write_trades_tsv` (l.978) — mirror append-with-header pattern.
- **VALIDATE**: `pytest tests/test_smt_dispatch_order.py -v` and `python -c "from backtest_smt import run_backtest_v2; r = run_backtest_v2('2025-11-14','2025-11-14', write_events=False); print(r['metrics'])"` (smoke).
- **TESTS** (`tests/test_smt_dispatch_order.py`):
  - `test_5m_dispatch_order_is_hypothesis_then_trend_then_strategy` — instrument each module's entry point with a mock that records call order; run one 5m bar; assert exact order.
  - `test_1m_only_dispatches_trend` — instrument; run a 1m bar that's not a 5m boundary; assert only `trend.run_trend` called.
  - `test_trend_invalidation_blocks_same_bar_fill` — set up state where strategy.py *would* fill a limit on this 5m bar AND trend.py *would* trend-break; assert no `limit-entry-filled` event in `events.jsonl`, only `trend-broken`.
  - `test_run_backtest_v2_smoke_one_day` — call `run_backtest_v2('2025-11-14','2025-11-14', write_events=False)`; assert returns dict with `trades`, `events`, `metrics`; no exception.
  - `test_old_run_backtest_unchanged` — import `run_backtest`; assert callable and signature unchanged (regression guard against accidental modification).

#### Task 3.2: ADD `SmtV2Dispatcher` to `signal_smt.py` (env-gated)

- **WAVE**: 3
- **AGENT_ROLE**: integration-specialist
- **DEPENDS_ON**: [2.1, 2.2, 2.3, 2.4]
- **BLOCKS**: []
- **PROVIDES**: Realtime v2 path selectable via `SMT_PIPELINE=v2`
- **USES_FROM_WAVE_2**: All four `run_*` entry points
- **IMPLEMENT**:
  - Add new class `SmtV2Dispatcher` in `signal_smt.py` (placement: near the bottom of the file, before `main()`).
  - Methods: `on_1m_bar(self, now, mnq_1m_bar, mes_1m_bar) -> None` and `on_session_start(self, now, ...) -> None`. Internally tracks 5m boundary and accumulates the in-progress 5m bar.
  - `on_session_start` triggers `daily.run_daily`. `on_1m_bar` invokes `trend.run_trend` every minute and, on 5m boundaries, invokes `hypothesis.run_hypothesis` → `trend.run_trend` → `strategy.run_strategy` in order.
  - In `main()`, read `os.environ.get("SMT_PIPELINE", "v1")`. If `"v2"`, instantiate `SmtV2Dispatcher` and route IB callbacks to its methods (replacing the existing `_process` hook). If unset or `"v1"`, behaviour unchanged.
  - All signals emitted by Wave 2 modules are also printed as JSON-lines to stdout via the existing `_dispatch_event` printer (l.487) — reuse, do not redefine.
  - Position.json writes go through `smt_state.save_position`, NOT the existing inline writes at l.873/951.
- **PATTERN**: Mirror `on_mnq_1m_bar` / `on_mes_1m_bar` (l.372, 407) for IB callback wiring; mirror `_dispatch_event` (l.487) for stdout JSON-lines.
- **VALIDATE**: `python -c "import signal_smt; assert hasattr(signal_smt, 'SmtV2Dispatcher')"` and `SMT_PIPELINE=v2 python -c "import signal_smt; print('v2 path importable')"`. No live IB smoke required (offline).
- **TESTS**: Covered indirectly via Task 3.1's dispatch-order tests. No new pytest file — class is thin glue.

**Wave 3 Checkpoint**: `pytest tests/test_smt_dispatch_order.py -v && python -c "from backtest_smt import run_backtest_v2"`

---

### WAVE 4: Regression Bench

#### Task 4.1: CREATE `regression.py`, `regression.md`, baselines, and `tests/test_smt_regression.py`

- **WAVE**: 4
- **AGENT_ROLE**: integration-specialist
- **DEPENDS_ON**: [3.1]
- **BLOCKS**: []
- **PROVIDES**: `run_regression(regression_md_path, *, update_baseline=False) -> dict`; initial baselines for committed sanity dates
- **USES_FROM_WAVE_3**: `run_backtest_v2`
- **IMPLEMENT**:
  - `regression.py`:
    - Parser: read `regression.md`; ignore blank lines and `#`-prefixed; trim inline `# ...` comments; for each remaining line, split on `:` → either single date or inclusive range. Expand ranges to date list.
    - For each date: call `run_backtest_v2(date, date, write_events=True)`. Outputs land at `data/regression/<date>/events.jsonl` and `trades.tsv`.
    - If `update_baseline=True`: copy `events.jsonl` → `baseline_events.jsonl`, `trades.tsv` → `baseline_trades.tsv`, mark this date as `"updated"` in result; do NOT diff.
    - Else: line-by-line strict diff:
      - `events_match`: byte-equal after `json.dumps(line, sort_keys=True)` canonicalisation.
      - `trades_match`: byte-equal TSV contents.
    - Return dict `{date: {events_match: bool, trades_match: bool, n_trades: int, pnl: float}}`.
    - CLI entry: `if __name__ == "__main__": ... sys.exit(0 if all_pass else 1)`.
  - `regression.md`: 2–3 dates initially, e.g.:
    ```
    # Sanity dates — known good behaviour
    2025-11-14
    ```
    Pick one date that exists in current parquet coverage. Verify before committing.
  - Generate initial baselines by running `python regression.py --update-baseline` once and committing `data/regression/<date>/baseline_*` files.
- **VALIDATE**: `pytest tests/test_smt_regression.py -v && python regression.py` (the CLI run must exit 0).
- **TESTS** (`tests/test_smt_regression.py`):
  - `test_parser_strips_comments_and_ranges` — feed `regression.md` content `"2026-01-08\n2026-02-15:2026-02-17  # range\n# skip\n\n2026-03-12"`; assert `["2026-01-08", "2026-02-15", "2026-02-16", "2026-02-17", "2026-03-12"]`.
  - `test_run_regression_pass_when_baselines_match` — fixture date with existing baselines; assert all `events_match` and `trades_match` True.
  - `test_run_regression_fail_when_events_differ` — corrupt one line in `baseline_events.jsonl`; assert `events_match=False`; CLI exits 1.
  - `test_run_regression_fail_when_trades_differ` — corrupt one row in `baseline_trades.tsv`; assert `trades_match=False`.
  - `test_update_baseline_overwrites_and_skips_diff` — call with `update_baseline=True`; assert baseline files match current, no diff performed.
  - `test_cli_exit_code_zero_on_pass` / `test_cli_exit_code_one_on_fail` — invoke `python regression.py` via subprocess.

**Final Checkpoint**: `pytest -q` (full suite green) and `python regression.py` (exit 0).

---

## TESTING STRATEGY

**⚠️ ALL tests automated.** No manual tests required for this feature — everything is offline pure-compute or file IO. No browser, no third-party services, no hardware.

| What you're testing | Tool |
|---|---|
| JSON IO & defaults | `pytest` (`tests/test_smt_state.py`) |
| daily.py compute correctness | `pytest` (`tests/test_smt_daily.py`) |
| hypothesis.py rules and transitions | `pytest` (`tests/test_smt_hypothesis.py`) |
| strategy.py state machine | `pytest` (`tests/test_smt_strategy.py`) |
| trend.py state machine | `pytest` (`tests/test_smt_trend.py`) |
| Dispatch order + harness smoke | `pytest` (`tests/test_smt_dispatch_order.py`) |
| Regression diff | `pytest` (`tests/test_smt_regression.py`) |
| Existing suite | `pytest -q` (must still pass) |

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_*.py` (six new files) | **Run**: `pytest tests/test_smt_state.py tests/test_smt_daily.py tests/test_smt_hypothesis.py tests/test_smt_strategy.py tests/test_smt_trend.py -v`

Each test enumerated inside its task above (Wave 1 has 5 tests, Wave 2 has 9 + 16 + 13 + 13 = 51 tests, Wave 3 has 5 tests, Wave 4 has 6 tests). Total: ~67 new tests.

### Integration Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_dispatch_order.py`, `tests/test_smt_regression.py` | **Run**: `pytest tests/test_smt_dispatch_order.py tests/test_smt_regression.py -v`

Covers cross-module dispatch order + end-to-end harness smoke + regression-bench round-trip.

### End-to-End Tests

**Status**: ✅ Automated via `run_backtest_v2` smoke test in `test_smt_dispatch_order.py::test_run_backtest_v2_smoke_one_day` and via `regression.py` CLI run.
**Tool**: pytest + subprocess | **Location**: `tests/test_smt_dispatch_order.py`, `tests/test_smt_regression.py` | **Run**: `pytest tests/test_smt_dispatch_order.py::test_run_backtest_v2_smoke_one_day tests/test_smt_regression.py -v && python regression.py`

### Edge Cases

- **Same-bar fill-and-trend-broken**: covered in `test_trend_invalidation_blocks_same_bar_fill` (Task 3.1).
- **Same-bar new-confirmation-and-fill**: covered in `test_same_bar_new_confirmation_and_fill_emits_only_move` (Task 2.3).
- **failed_entries=2 boundary**: covered in `test_failed_entries_exactly_two_still_allowed` (Task 2.3).
- **5m bar fully above ATH**: covered in `test_early_exit_when_5m_bar_fully_above_ath` (Task 2.2).
- **Cautious_price empty string**: covered in `test_cautious_price_empty_string_skips_arming` (Task 2.4).
- **JSON file missing on first run**: covered in `test_load_returns_default_when_missing` (Task 1.1).
- **Atomic write crash**: covered in `test_save_is_atomic` (Task 1.1).
- **TSV / JSONL determinism for regression diff stability**: covered in `test_save_uses_sort_keys_for_determinism` (Task 1.1).

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest) | ~67 | 100% |
| ✅ Frontend/E2E (Playwright) | 0 | 0% |
| ✅ Third-party validation | 0 | 0% |
| ⚠️ Manual | 0 | 0% |
| **Total** | ~66 | 100% |

**Coverage gap analysis**:
- All new module entry points covered by per-module tests.
- All branches of strategy.py and trend.py state machines enumerated as named test cases.
- Dispatch order verified by mock-instrumented test.
- Old `run_backtest` regression-guarded by `test_old_run_backtest_unchanged`.
- `signal_smt.SmtV2Dispatcher`: thin glue; verified by import-level checks plus the dispatch-order test that exercises the same module composition under the backtest harness. Live IB smoke explicitly out of scope (no offline IB simulator available; documented in spec § "Acceptance criteria" #10 as a manual smoke step deferred to first realtime trial).

**Execution agent**: CREATE all automated test files alongside their target modules. RUN the wave checkpoint after each wave completes. Do not skip Task 4.1's CLI subprocess test — it validates the regression entry point that the user will run in production.

---

## VALIDATION COMMANDS

### Level 0: External Service Validation

Not applicable — no external services.

### Level 1: Syntax & Style

```bash
python -m py_compile smt_state.py daily.py hypothesis.py strategy.py trend.py regression.py
```

### Level 2: Unit Tests

```bash
pytest tests/test_smt_state.py tests/test_smt_daily.py tests/test_smt_hypothesis.py tests/test_smt_strategy.py tests/test_smt_trend.py -v
```

### Level 3: Integration Tests

```bash
pytest tests/test_smt_dispatch_order.py tests/test_smt_regression.py -v
```

### Level 4: End-to-End / Smoke

```bash
python -c "from backtest_smt import run_backtest_v2; r = run_backtest_v2('2025-11-14','2025-11-14', write_events=False); print(r['metrics'])"
python regression.py
pytest -q   # full suite — existing tests must still pass
```

---

## ACCEPTANCE CRITERIA

### Functional

- [ ] `smt_state.py` exposes `load_<name>` / `save_<name>` for `global`, `daily`, `hypothesis`, `position` with documented default dicts and atomic writes.
- [ ] `daily.py` writes `daily.json.liquidities` containing all 14 named levels (TDO, TWO, week_high/low, day_high/low, asia/london/ny_morning/ny_evening high/low) plus at least one `fvg_*` entry on a fixture day; updates `global.all_time_high` only when today's high exceeds the prior value; sets `daily.estimated_dir = global.trend`; sets `daily.opposite_premove = "no"`; resets `position.json` per-session fields (`active`, `limit_entry`, `confirmation_bar`, `failed_entries`); sets `hypothesis.direction = "none"`.
- [ ] `hypothesis.py` early-exits when `direction != "none"` AND when both extremes of the current 5m bar are above `global.all_time_high`; produces all schema fields (`weekly_mid`, `daily_mid`, `last_liquidity`, `divs`, `direction`, `targets`, `cautious_price`, `entry_ranges`); resets `position.failed_entries = 0` and clears `position.confirmation_bar = {}` on `none → up/down` transition.
- [ ] `strategy.py` emits exactly five signal kinds — `new-limit-entry`, `move-limit-entry`, `limit-entry-filled`, `market-close`, `stopped-out` — each under its specified condition; respects same-bar override (new opposite confirmation overrides a would-be fill on the same bar); increments `failed_entries` on `stopped-out`; gate is strictly `> 2` so `failed_entries == 2` still allows entry attempts.
- [ ] `trend.py` emits all five trend signals — `cautious-armed`, `cautious-rejected`, `cautious-reversal`, `cautious-1m-break`, `trend-broken` — each under its specified condition; every market-close path sets `hypothesis.direction = "none"`, clears `position.confirmation_bar`, clears `position.limit_entry`.
- [ ] Per-bar dispatch order: on a 5m boundary, modules invoke as `hypothesis → trend → strategy`; on a non-5m 1m boundary, only `trend` invokes; trend invalidation on a same bar blocks any would-be `limit-entry-filled` from `strategy`.

### Error Handling

- [ ] All four state files return their defaults when missing on disk (no exception); returned dict is a deep copy so mutating it does not poison subsequent loads.
- [ ] Atomic write contract: a simulated crash inside `os.replace` leaves the destination file in its prior state (or absent) — no half-written corruption.
- [ ] `cautious_price = ""` (empty string) is treated as "no cautious threshold" — `trend.py` does not arm cautious mode regardless of price action.

### Integration / E2E

- [ ] `run_backtest_v2(start_date, end_date)` runs end-to-end on at least one committed sanity date with no exception and produces deterministic `data/regression/<date>/events.jsonl` and `trades.tsv`.
- [ ] `events.jsonl` is byte-stable across runs (deterministic dispatch order + `json.dumps(sort_keys=True)`); `trades.tsv` is byte-stable across runs.
- [ ] `regression.py` CLI exits 0 when all dates' events.jsonl + trades.tsv match their committed baselines; exits 1 if any date's events or trades diff.
- [ ] `signal_smt.py` default behaviour unchanged when `SMT_PIPELINE` is unset; with `SMT_PIPELINE=v2`, `SmtV2Dispatcher` is instantiated and routes IB callbacks through the four new modules.
- [ ] Existing `backtest_smt.run_backtest` function exists with unchanged signature after this change; existing test suite (`pytest -q`) remains green.

### Validation

- [ ] All ~67 new tests pass — verified by: `pytest tests/test_smt_state.py tests/test_smt_daily.py tests/test_smt_hypothesis.py tests/test_smt_strategy.py tests/test_smt_trend.py tests/test_smt_dispatch_order.py tests/test_smt_regression.py -v`
- [ ] Full suite green — verified by: `pytest -q`
- [ ] Compiles clean — verified by: `python -m py_compile smt_state.py daily.py hypothesis.py strategy.py trend.py regression.py`
- [ ] Backtest v2 smoke — verified by: `python -c "from backtest_smt import run_backtest_v2; r = run_backtest_v2('2025-11-14','2025-11-14', write_events=False); print(r['metrics'])"`
- [ ] Regression CLI — verified by: `python regression.py` (exit 0)

### Out of Scope

- TP, partial exits, breakeven, trail, secondary target — deliberately removed from the new path
- Layer-B FVG retracement re-add — not in v2
- MSS / CISD / opposing-displacement invalidation exits — not in v2
- Real estimators for `daily.estimated_dir`, `daily.opposite_premove`, `hypothesis.direction`, `hypothesis.cautious_price` — stay hardcoded per ideas.md
- Pruning of `strategy_smt.py` / `hypothesis_smt.py` and their tests — separate future plan
- Live IB realtime smoke test (no offline IB simulator); deferred to first realtime trial
- Production-grade always-on daemon scheduler — initial cut piggybacks on existing IB callbacks
- New external dependencies — none added in this change

---

## COMPLETION CHECKLIST

- [ ] External service verification passed (N/A)
- [ ] All 7 tasks completed in wave order
- [ ] Each task validation command passed
- [ ] All validation levels executed (1–4; Level 0 N/A)
- [ ] All ~66 automated tests created and passing
- [ ] No manual tests (none required)
- [ ] Full test suite passes (`pytest -q`)
- [ ] `python -m py_compile` clean for all 6 new modules
- [ ] All acceptance criteria checked
- [ ] Code reviewed for adherence to spec (no scope creep — no TP/breakeven/trail/etc reintroduced)
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**Why minimum diff over reuse-existing-control-flow**: gluing the new requirements onto `strategy_smt.py`'s state machine would require disabling ~half its branches and reasoning about dead paths every time a future change lands. The user's "many additions later" framing makes a clean foundation more valuable than a smaller initial diff. New code is ~600 lines new + ~66 tests; old code remains resident untouched and can be pruned in a follow-up once v2 is validated.

**Why option-1 layout (alongside, not in subfolder)**: matches the existing repo convention where top-level `*.py` files are entry points and feature modules. Subfolder would force import-path changes in `signal_smt.py` and `backtest_smt.py`, increasing diff size without semantic benefit at this stage.

**TBD items deliberately preserved**: `daily.estimated_dir = global.trend`, `daily.opposite_premove = "no"`, `hypothesis.direction = "up"`, `hypothesis.cautious_price = ""`. Each is exactly as `ideas.md` mandates for the initial cut. The user has explicitly said they will iterate on these later; do NOT introduce real estimators in this plan.

**Old code retirement deferred**: `strategy_smt.process_scan_bar`, `strategy_smt.manage_position`, `hypothesis_smt.HypothesisManager`, `signal_smt._process_scanning`/`_process_managing`, `backtest_smt.run_backtest` all stay resident. Their tests stay running. Pruning is a separate future plan.

**`position.json` schema migration**: an existing `data/position.json` from the old v1 path has a different schema (minimal fields). `load_position()` must validate the schema on every load: if the file exists but its top-level keys are not a superset of `DEFAULT_POSITION.keys()`, discard it and return `copy.deepcopy(DEFAULT_POSITION)` instead. Same defensive guard applies to all four `load_*` functions. This silently self-heals on first v2 run with no manual deletion or external migration step required.

**Regression-baseline first run**: the executing agent generates baselines as part of Task 4.1 by running `python regression.py --update-baseline` once after `run_backtest_v2` is functional. The committed baselines lock in v2's behaviour at acceptance time. Future plan changes will need to update these baselines deliberately.

**Determinism contract**: `events.jsonl` lines are produced in dispatch order and serialized with `json.dumps(..., sort_keys=True)`. Trade rows in `trades.tsv` are written in entry-time order. Both must be byte-stable across runs for the regression diff to be meaningful.

**Risk: same-bar dispatch ordering corner cases.** The trend-blocks-fill semantics are explicitly tested but a rare configuration may surface an unanticipated interaction. Mitigation: the regression bench's per-date events.jsonl diff catches behaviour drift; if a corner case lands, capture it as a new sanity date in `regression.md`.

**Risk: parquet date coverage.** Sanity date in `regression.md` must exist in the current `data/MNQ_1m.parquet` and `data/MES_1m.parquet`. The executing agent must verify this before committing the baseline.

**Risk: wall-clock 09:20 ET in backtest.** In backtest, "09:20 ET" is bar-time, not real time. Build the daily-call trigger off the first 1m bar of the session whose timestamp is `>= 09:20:00 ET`.

**No new dependencies added.** Plan compiles against existing `pyproject.toml`.
