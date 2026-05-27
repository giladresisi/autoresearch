# Feature: Daily Liquidity Refresh Redesign

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Splits the monolithic `run_daily()` into two distinct concerns: (1) a once-per-day-or-startup function that recomputes fixed reference liquidities (TDO, TWO, prev-day levels, 1hr + 4hr FVGs, ATH seed), and (2) per-bar dynamic updates inside `on_1m_bar` that keep session/day/week high-low and FVG visited-state current throughout the day. Adds a `--force` flag to `trade.py start` (propagated through the orchestrator to the pipeline) that resets hypothesis direction and position state — the only remaining path for state reset. Removes `--resume` and the interactive kill prompt from `trade.py start`.

## User Story

As a trader running the orchestrator continuously throughout the day,
I want reference liquidities refreshed on a predictable daily schedule regardless of when I restart,
So that my hypothesis and strategy always operate on correct, current price levels without stale freeze-at-09:20 values.

## Problem Statement

`run_daily()` currently fires once at 09:20 ET and freezes all liquidity levels at that moment. This means `ny_morning_high/low` is incomplete (frozen mid-session), `ny_evening` levels are absent, FVGs are never pruned when visited nor updated when new ones form, and day/week highs grow throughout the day but are only refreshed ephemerally inside `hypothesis.py` (not persisted). With a continuously-running orchestrator that may be restarted at any time, the old design has no scheduled daily anchor.

## Solution Statement

- Introduce `SessionPipeline.on_daily_or_startup()` — called on every orchestrator startup AND time-gated at 09:20 ET daily — that computes only fixed-for-the-day reference levels (TDO, TWO, prev2-day, 1hr + 4hr FVGs from hist) and seeds ATH / session_ath.
- Add per-bar dynamic updates inside `on_1m_bar`: session h/l for the active window, day/week h/l/mid, FVG visited-prune and hourly/4hr FVG detection. Emit `liquidity-updated` events when values change.
- Remove all state resets from the daily/startup path. `--force` becomes the single explicit opt-in for resetting hypothesis direction + position, propagated from `trade.py` → orchestrator → subprocess env var → pipeline.
- Backtest path explicitly passes `force_reset=True` (it always needs fresh state per day).

## Feature Metadata

**Feature Type**: Refactor + Enhancement
**Complexity**: High
**Primary Systems Affected**: `daily.py`, `session_pipeline.py`, `hypothesis.py`, `trade.py`, `orchestrator/main.py`, `automation/main.py`, `signal_smt.py`, `backtest_smt.py`
**Dependencies**: None external
**Breaking Changes**: Yes — `run_daily` renamed to `run_daily_fixed`, signature changes; `on_session_start` gains `force_reset` parameter; `--resume` removed from `trade.py`

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `daily.py` (full) — current `run_daily()` implementation; split source
- `session_pipeline.py:77–215` — `on_session_start`: replace `run_daily` call with `on_daily_or_startup`
- `session_pipeline.py:216–690` — `on_1m_bar`: add per-bar liquidity updates + 09:20 gate here
- `hypothesis.py:1115–1127` — ephemeral `compute_live_hl_mid` refresh; remove after daily.json is kept current
- `trade.py:237–303` — `start` command to simplify
- `orchestrator/main.py:380–465` — `run()` function; add `--force` handling + env var propagation
- `signal_smt.py:817–844` — `SmtV2Dispatcher`; wire `force_reset`
- `automation/main.py:947–976` — `SmtV2Dispatcher`; wire `force_reset`
- `backtest_smt.py:1268` — pipeline daily call; must add `force_reset=True`
- `tests/test_smt_daily.py` — update for renamed function + 4hr FVG
- `tests/test_session_pipeline.py` — update for new `on_session_start` semantics
- `tests/test_smt_v2_dispatcher.py` — update for `force_reset` propagation

### New Files to Create

None — all changes are in existing files.

### Patterns to Follow

**Naming**: `run_daily_fixed` (no resets, computes levels only), `on_daily_or_startup` (pipeline method)
**Event emission**: existing pattern `self._emit({"kind": "liquidity-updated", "time": now.isoformat(), "name": ..., "price": ...})`
**State guard**: follow `_daily_triggered` pattern for 09:20 gate: track `self._last_daily_date: datetime.date | None` to prevent double-firing
**FVG detection**: `_detect_fvgs(bars_df, mnq_1m_for_visit_check)` already timeframe-agnostic; call for both `self._hist_1hr` and `self._hist_4hr`
**In-memory + flush**: update `daily.json` liquidities in-place via `load_daily / save_daily`; emit `liquidity-updated` event when any entry's price changes

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────────────────┐
│ WAVE 1: Independent foundation changes (Parallel)            │
├──────────────────────────────────────────────────────────────┤
│ Task 1.1: trade.py start simplification                      │
│ Task 1.2: daily.py → run_daily_fixed + 4hr FVG              │
│ Task 1.3: orchestrator/main.py --force + FORCE_RESET env var │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 2: Pipeline core (After Wave 1)                         │
├──────────────────────────────────────────────────────────────┤
│ Task 2.1: session_pipeline — on_daily_or_startup +           │
│           refactor on_session_start (force_reset param)      │
│ Task 2.2: session_pipeline.on_1m_bar — per-bar dynamic       │
│           level updates + 09:20 ET time gate                 │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 3: Callers + cleanup (After Wave 2, parallel within)    │
├──────────────────────────────────────────────────────────────┤
│ Task 3.1: signal_smt.py + automation/main.py — force_reset  │
│ Task 3.2: backtest_smt.py — pass force_reset=True            │
│ Task 3.3: hypothesis.py — remove ephemeral H/L refresh       │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 4: Tests (After Wave 3, parallel within)                │
├──────────────────────────────────────────────────────────────┤
│ Task 4.1: Update test_smt_daily.py                           │
│ Task 4.2: Update test_session_pipeline.py                    │
│ Task 4.3: Update test_smt_v2_dispatcher.py + dispatch_order  │
│ Task 4.4: Update test_smt_hypothesis.py if needed            │
└──────────────────────────────────────────────────────────────┘
```

### Interface Contracts

**Contract 1**: Task 1.2 provides `run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)` → Tasks 2.1 and 4.1 consume.
**Contract 2**: Task 2.1 provides `SessionPipeline.on_daily_or_startup(now, today_mnq)` and `on_session_start(now, today_mnq, force_reset=False)` → Tasks 3.1, 3.2 consume.
**Contract 3**: Task 1.3 provides `FORCE_RESET=true` env var convention → Tasks 3.1 consumes.

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation (Wave 1)

No inter-task dependencies; all three can run in parallel.

### Phase 2: Pipeline Core (Wave 2)

Depends on Wave 1. Tasks 2.1 and 2.2 can run in parallel since they touch different methods of `SessionPipeline`.

### Phase 3: Callers + Cleanup (Wave 3)

Depends on Wave 2. All three tasks can run in parallel.

### Phase 4: Tests (Wave 4)

Depends on Wave 3. All four tasks can run in parallel.

---

## STEP-BY-STEP TASKS

---

### WAVE 1: Foundation

#### Task 1.1: REFACTOR `trade.py` start command

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: []
- **PROVIDES**: Simplified `start` command with no interactive prompts, no `--resume`, silent kill
- **IMPLEMENT**:
  1. Remove `--resume` / `-r` detection from `raw_args` (line 244)
  2. Remove the `if not resume:` block that cancelled stop_entry and reset position.json (lines 260–271). The new default is always preserve-state — no position.json reset on startup.
  3. Remove the interactive prompt block entirely (lines 249–258): always call `_terminate_all()` without asking, regardless of `force` flag. Print a simple "Killing existing orchestrator..." line.
  4. `--force` on `start` now means: pass `FORCE_RESET=true` to the subprocess env via the `subprocess.Popen` call. Add `env={**os.environ, "FORCE_RESET": "true"}` to the `Popen()` call when `force` is True.
  5. Remove `if resume: print("Resume mode: position.json unchanged")` line (line 299).
  6. Update the docstring at the top: remove `--resume` line; update `--force` description to "Reset hypothesis direction and position state (start fresh)".
- **VALIDATE**: `uv run python trade.py --help` shows no `--resume`; `uv run python trade.py start --force` parses correctly (dry-run: ensure subprocess would receive env var, no crash before IB check)
- **PATTERN**: `subprocess.Popen` env merging: `env={**os.environ, "FORCE_RESET": "true"}`

---

#### Task 1.2: REFACTOR `daily.py` — rename + add 4hr FVG + strip resets + strip dynamic levels

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1, 4.1]
- **PROVIDES**: `run_daily_fixed(now, hist_mnq_1m, hist_1hr, hist_4hr, today)` — computes only fixed-for-the-day levels, no resets
- **IMPLEMENT**:
  1. Rename `run_daily()` → `run_daily_fixed()`. Update the signature:
     ```python
     def run_daily_fixed(
         now: datetime.datetime,
         hist_mnq_1m: pd.DataFrame,
         hist_1hr: pd.DataFrame,
         hist_4hr: pd.DataFrame,
         today: datetime.date,
     ) -> None:
     ```
     Remove `mnq_1m` parameter (was today's bars — dynamic levels are now per-bar). Remove `reset_hypothesis` and `reset_position` parameters entirely.
  2. Remove the body sections that computed dynamic levels — specifically:
     - Remove `week_high/low/mid`, `day_high/low/mid` computation (these are seeded in `on_daily_or_startup` and updated per-bar)
     - Remove `session highs/lows` loop (asia/london/ny_morning/ny_evening) — these are updated per-bar
     - Remove `_overnight_range` computation and the `overnight_range` field from `daily_state`
  3. Keep: TDO, TWO, prev2-day high/low/TDO. Compute these from `hist_mnq_1m` (combine with any bars in scope).
  4. Add 4hr FVG detection:
     ```python
     fvgs_4hr = _detect_fvgs(hist_4hr, hist_mnq_1m)
     liquidities.extend(fvgs_4hr)
     ```
     Call `_detect_fvgs(hist_1hr, hist_mnq_1m)` for 1hr (existing) and `_detect_fvgs(hist_4hr, hist_mnq_1m)` for 4hr. FVG names: the existing naming uses `fvg_{ts}_{side}` — these will naturally differ by timestamp.
  5. Remove Steps 6 and 7 entirely (hypothesis reset and position reset).
  6. Keep Step 3 (ATH update to `global.json`). Keep writing `daily.json` (Step: write daily_state).
  7. Update `formed_at` field — keep it; it's useful for debugging.
  8. The `compute_tdo` and `_compute_two` helper functions remain unchanged.
  9. `_detect_fvgs` remains unchanged.
  10. Remove the `load_hypothesis, save_hypothesis` imports from `smt_state`. Remove `strategy as _strategy` import (was only for `reset_position_for_session`).
  11. Remove the `from hypothesis import compute_live_hl_mid` import (was for dynamic levels).
- **VALIDATE**: `uv run python -c "from daily import run_daily_fixed; print('OK')"` — no import errors.

---

#### Task 1.3: ADD `--force` to `orchestrator/main.py` + env var propagation

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: `--force` in orchestrator CLI; `FORCE_RESET=true` passed to subprocess env
- **IMPLEMENT**:
  1. In `orchestrator/main.py`, at the `if __name__ == "__main__":` block (line 494), detect `--force` in `sys.argv`.
  2. Pass `force_reset` bool to `run()` as a new parameter: `run(skip_summary=..., force_reset="--force" in sys.argv)`.
  3. Update `run(summarizer=None, skip_summary=False)` signature to `run(summarizer=None, skip_summary=False, force_reset=False)`.
  4. In `run()`, when building the subprocess command for `automation.main` or `signal_smt.py`, pass the env var. The subprocess is launched via `ProcessManager`. Trace how `ProcessManager.run_session` invokes the subprocess to find where to inject env:
     - `orchestrator/process.py` has `ProcessManager(signal_cmd, relay, orch_ch).run_session(today)` — check `process.py` for subprocess launch and add `extra_env={"FORCE_RESET": "true"}` parameter if `force_reset` is True. Or, simpler: set `os.environ["FORCE_RESET"] = "true"` before the subprocess is launched and `del os.environ["FORCE_RESET"]` (or use `subprocess.Popen(env={**os.environ, "FORCE_RESET": "true"})`) — prefer the env-dict approach for cleanliness.
  5. Read `orchestrator/process.py` to understand how to inject the env var cleanly. Add a parameter `extra_env: dict | None = None` to `ProcessManager.__init__` or `run_session`, merged into the Popen call.
  6. Update the `__main__` block comment at the top of `orchestrator/main.py` to document the `--force` flag.
- **VALIDATE**: `uv run python -m orchestrator.main --force` starts (will fail at IB check but that's fine — the flag should parse without error)

---

### WAVE 2: Pipeline Core

#### Task 2.1: ADD `SessionPipeline.on_daily_or_startup()` + REFACTOR `on_session_start()`

- **WAVE**: 2
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [1.2]
- **BLOCKS**: [3.1, 3.2, 4.2]
- **PROVIDES**: `on_daily_or_startup(now, today_mnq)` method; refactored `on_session_start(now, today_mnq, force_reset=False)`
- **IMPLEMENT**:
  1. Add instance variable `self._last_daily_date: datetime.date | None = None` to `__init__`.
  2. Extract a new method `on_daily_or_startup(self, now: pd.Timestamp, today_mnq: pd.DataFrame) -> None`:
     ```python
     def on_daily_or_startup(self, now: pd.Timestamp, today_mnq: pd.DataFrame) -> None:
         """Compute fixed reference liquidities and seed ATH. Called on startup and at 09:20 ET daily."""
     ```
     Move the following from `on_session_start` into this new method:
     - ATH / session_ath seeding from `self._hist_mnq_1m` (lines 97–110)
     - Hourly/4hr resample computation (lines 119–135)
     - `save_daily(copy.deepcopy(DEFAULT_DAILY))` reset
     - Call to `_daily_mod.run_daily_fixed(now, self._hist_mnq_1m, self._hist_1hr, self._hist_4hr, now.date())`
     - Seed initial day/week/session levels into `daily.json` from hist bars (see step below)
     - `levels.json` write (lines 149–166)
     - Set `self._last_daily_date = now.date()`

     **Seeding day/week/session levels in on_daily_or_startup:**
     After `run_daily_fixed` writes the fixed levels to `daily.json`, append initial dynamic levels:
     ```python
     _state = load_daily()
     _liq = _state.get("liquidities", [])
     _combined = pd.concat([self._hist_mnq_1m, today_mnq]).sort_index()
     _combined = _combined[~_combined.index.duplicated(keep="last")]
     _live = compute_live_hl_mid(_combined, now)  # from hypothesis import
     for _name in ("week_high","week_low","week_mid","day_high","day_low","day_mid"):
         if _name in _live:
             _existing = next((l for l in _liq if l["name"] == _name), None)
             if _existing:
                 _existing["price"] = _live[_name]
             else:
                 _liq.append({"name": _name, "kind": "level", "price": _live[_name]})
     # Seed session highs/lows from completed sessions
     _today = now.date()
     for _sess in ("asia", "london", "ny_morning", "ny_evening"):
         _sbars = _session_bars(_combined, _sess, _today)
         if not _sbars.empty:
             for _suffix, _fn in (("high", "max"), ("low", "min")):
                 _key = f"{_sess}_{_suffix}"
                 _price = float(getattr(_sbars["High" if _suffix=="high" else "Low"], _fn)())
                 _ex = next((l for l in _liq if l["name"] == _key), None)
                 if _ex:
                     _ex["price"] = _price
                 else:
                     _liq.append({"name": _key, "kind": "level", "price": _price})
     _state["liquidities"] = _liq
     save_daily(_state)
     ```
     Note: import `_session_bars` from `daily` (it's already a module-level function there, make it importable or duplicate the logic inline — prefer import).

  3. Refactor `on_session_start(self, now, today_mnq_at_open)` to add `force_reset: bool = False`:
     ```python
     def on_session_start(self, now: pd.Timestamp, today_mnq_at_open: pd.DataFrame, force_reset: bool = False) -> None:
     ```
     Replace the existing body with:
     ```python
     # Always run the daily/startup liquidity computation.
     self.on_daily_or_startup(now, today_mnq_at_open)
     self._daily_triggered = True

     # Reset hypothesis and position only when explicitly forced.
     if force_reset:
         save_hypothesis(copy.deepcopy(DEFAULT_HYPOTHESIS))
         save_position(copy.deepcopy(DEFAULT_POSITION))
         # Run first hypothesis so direction is populated immediately after force-reset.
         _init_hyp_divs = _hyp_mod.run_hypothesis(
             now, today_mnq_at_open, self._hist_mes_1m,
             self._hist_mnq_1m, self._hist_mes_1m,
             hist_1hr=self._hist_1hr, hist_4hr=self._hist_4hr,
         )
         for _d in (_init_hyp_divs or []):
             self._emit(_d)
     # else: preserve existing hypothesis and position state; no forced hypothesis run.
     # Direction reconciliation with active position (needed in both paths):
     _has_active = bool(load_position().get("active"))
     if _has_active and force_reset:
         # force_reset + active: direction conflict will be caught on next bar by trend.py
         pass
     ```
     Remove the old `_has_active` conditional reset logic and the old `run_daily` call.
     Keep the `self._last_hyp_cautious`, `self._hyp_formation_price`, `self._accepted_level_sweeps`, `self._swept_levels_since_hyp` resets at the top.
  4. Add `from daily import _session_bars` (or inline the function). Also add `from hypothesis import compute_live_hl_mid` if not already imported in session_pipeline.

- **VALIDATE**: `uv run python -m pytest tests/test_session_pipeline.py -x -q` (will fail until Task 4.2 updates tests; that's expected — confirm import structure is OK first)

---

#### Task 2.2: ADD per-bar dynamic level updates + 09:20 ET time gate in `on_1m_bar`

- **WAVE**: 2
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [1.2, 2.1]
- **BLOCKS**: [4.2]
- **PROVIDES**: Per-bar updates for session/day/week H/L and FVGs in `daily.json`; `liquidity-updated` events
- **IMPLEMENT**:
  1. Add instance variable `self._last_daily_minute: pd.Timestamp | None = None` to `__init__` (prevents double-fire of 09:20 gate within same minute).
  2. At the start of `on_1m_bar`, after the `if not self._daily_triggered: return []` guard, add the 09:20 ET time gate:
     ```python
     # 09:20 ET daily trigger: re-run on_daily_or_startup regardless of orchestrator restart time.
     _bar_floor = now.floor("1min")
     if (now.hour == 9 and now.minute == 20
             and _bar_floor != self._last_daily_minute):
         self._last_daily_minute = _bar_floor
         self.on_daily_or_startup(now, today_mnq)
     ```
  3. After `trend_sig` processing and before the 5m hypothesis gate, add the per-bar dynamic level update block:
     ```python
     # Per-bar: update session/day/week H/L and prune visited FVGs in daily.json.
     self._update_dynamic_liquidities(now, mnq_bar_row, today_mnq, events)
     ```
  4. Implement `_update_dynamic_liquidities(self, now, mnq_bar_row, today_mnq, events)`:
     - Load `daily.json` once: `_state = _smt_state.load_daily()`
     - Build a dict of current levels keyed by name for O(1) lookup
     - **Session H/L**: determine current session from `now.hour/minute`, compute the session window using `_session_bars` logic. For the active session only, check if bar_high > stored high or bar_low < stored low and update.
     - **Day H/L**: `_today_bars = today_mnq[today_mnq.index <= now]`. Update `day_high` if `bar_high > current`, update `day_low` if `bar_low < current`. Update `day_mid = (day_high + day_low) / 2`.
     - **Week H/L**: `_week_start = monday_18_00_et(now.date())`. Filter hist+today bars from week start to now. Update `week_high`, `week_low`, `week_mid` similarly.
     - **FVG visited prune**: iterate FVG entries in liquidities. If bar_high >= fvg_bottom and bar_low <= fvg_top → mark as visited by removing from list.
     - **New FVG detection** (only at boundaries):
       - At 1hr boundary (`now.minute == 0`): call `_detect_fvgs(self._hist_1hr, today_mnq)` and add any new FVGs not already in the list (match by name).
       - At 4hr boundary (`now.minute == 0 and now.hour % 4 == 0`): call `_detect_fvgs(self._hist_4hr, today_mnq)` similarly.
     - Track which entries changed. For each changed entry, emit a `liquidity-updated` event:
       ```python
       {"kind": "liquidity-updated", "time": now.isoformat(), "name": name, "old_price": old, "price": new}
       ```
     - If anything changed, call `_smt_state.save_daily(_state)` once.
     - Update `self._ext_levels` from the new liquidities list (so trend.py sweep check stays current).

     **Helper for week start (inline or private method)**:
     ```python
     def _week_start_ts(self, today: datetime.date) -> pd.Timestamp:
         days_since_monday = today.isocalendar().weekday - 1
         monday = today - datetime.timedelta(days=days_since_monday)
         return pd.Timestamp(datetime.datetime(monday.year, monday.month, monday.day, 18, 0),
                             tz="America/New_York")
     ```

     **Active session detection**:
     ```python
     _hour, _min = now.hour, now.minute
     _t = _hour * 60 + _min
     if 18*60 <= _t or _t < 0*60:   # asia: prior day 18:00→00:00 (handled via today-1)
         _active_sess = "asia"
     elif 0 <= _t < 6*60:
         _active_sess = "london"
     elif 6*60 <= _t < 12*60:
         _active_sess = "ny_morning"
     elif 12*60 <= _t < 17*60:
         _active_sess = "ny_evening"
     else:
         _active_sess = None  # maintenance window 17:00–18:00
     ```
- **VALIDATE**: `uv run python -m pytest tests/test_session_pipeline.py -x -q` after Task 4.2 updates tests

---

### WAVE 3: Callers + Cleanup

#### Task 3.1: WIRE `force_reset` through `SmtV2Dispatcher` in `signal_smt.py` and `automation/main.py`

- **WAVE**: 3
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [2.1, 1.3]
- **BLOCKS**: [4.3]
- **PROVIDES**: `force_reset` from `FORCE_RESET` env var flows into `pipeline.on_session_start`
- **IMPLEMENT**:
  1. In **`signal_smt.py`** `SmtV2Dispatcher.__init__`:
     ```python
     self._force_reset = os.environ.get("FORCE_RESET", "").lower() == "true"
     ```
  2. In `signal_smt.py` `SmtV2Dispatcher.on_session_start`:
     ```python
     self._pipeline.on_session_start(now, today_at_open, force_reset=self._force_reset)
     ```
  3. In **`automation/main.py`** `SmtV2Dispatcher.__init__`: same — add `self._force_reset = os.environ.get("FORCE_RESET", "").lower() == "true"`.
  4. In `automation/main.py` `SmtV2Dispatcher.on_session_start`: same — pass `force_reset=self._force_reset`.
  5. Add `import os` at module top of `signal_smt.py` if not already present.
- **VALIDATE**: `uv run python -c "import signal_smt; print('OK')"` and `uv run python -c "import automation.main; print('OK')"`

---

#### Task 3.2: UPDATE `backtest_smt.py` — pass `force_reset=True`

- **WAVE**: 3
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: []
- **PROVIDES**: Backtests always get fresh state per day (equivalent to old auto-reset behavior)
- **IMPLEMENT**:
  1. Find where `backtest_smt.py` calls `pipeline.on_session_start(...)` (around line 1268+).
  2. Add `force_reset=True` to that call:
     ```python
     pipeline.on_session_start(now, today_mnq_at_open, force_reset=True)
     ```
  3. This restores the pre-redesign behavior for backtests (fresh direction + position each day).
- **VALIDATE**: `uv run python -m pytest tests/test_smt_backtest.py -x -q --timeout=60`

---

#### Task 3.3: REMOVE ephemeral `compute_live_hl_mid` refresh from `hypothesis.py`

- **WAVE**: 3
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [2.2]
- **BLOCKS**: [4.4]
- **PROVIDES**: `hypothesis.py` reads day/week H/L directly from `daily.json` (now current)
- **IMPLEMENT**:
  1. Remove lines 1122–1127 in `hypothesis.py` (the `_combined_live` / `_live_hl` block that overwrites liquidities in-memory with live values).
  2. The `for _liq in liquidities: if _liq.get("kind") == "level" and _liq["name"] in _live_hl: _liq["price"] = _live_hl[_liq["name"]]` block is removed.
  3. `hypothesis.py` now simply reads `daily.json` and trusts those values are current (since per-bar updates keep them fresh).
  4. Check if `compute_live_hl_mid` is still imported in `hypothesis.py`. If it's only used in the removed block, remove the import.
  5. Check if `compute_live_hl_mid` is still needed elsewhere (it IS still used in `daily.py` for `on_daily_or_startup` seeding — that's now in `session_pipeline.py`). The `daily.py` import of `compute_live_hl_mid` may no longer be needed after Task 1.2 strips dynamic levels.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_hypothesis.py -x -q`

---

### WAVE 4: Tests

#### Task 4.1: UPDATE `tests/test_smt_daily.py`

- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.2]
- **BLOCKS**: []
- **IMPLEMENT**:
  1. Rename all `run_daily(...)` calls → `run_daily_fixed(...)`.
  2. Update imports: `from daily import run_daily_fixed`.
  3. Remove `reset_hypothesis` and `reset_position` keyword arguments from all call sites.
  4. Update signature-based tests: remove parameter tests for `reset_hypothesis`/`reset_position`.
  5. Add new test for 4hr FVG detection: create synthetic `hist_4hr` DataFrame with a 3-bar FVG pattern; verify `run_daily_fixed` writes an FVG entry with correct `top/bottom` and `kind="fvg"`.
  6. Tests that verified direction reset (e.g. `test_run_daily_should_only_set_direction_none`) — update or remove. `run_daily_fixed` no longer resets direction.
  7. Tests using `mnq_1m` positional parameter: update to only pass `hist_mnq_1m, hist_1hr, hist_4hr, today`.
  8. Verify session high/low tests are removed or updated — `run_daily_fixed` no longer writes session levels.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_daily.py -v`

---

#### Task 4.2: UPDATE `tests/test_session_pipeline.py`

- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [2.1, 2.2]
- **BLOCKS**: []
- **IMPLEMENT**:
  1. `test_on_session_start_calls_run_daily_with_filtered_bars`: update — `run_daily` is now `run_daily_fixed`; the call no longer passes `mnq_1m` (today bars); update mock expectations.
  2. Tests that verify hypothesis reset on session start (without active position): these must now only pass when `force_reset=True`. Add explicit `force_reset=True` to those test calls, or add new test variant.
  3. Tests that verify position reset on session start: same — add `force_reset=True`.
  4. `test_on_1m_bar_calls_trend_every_bar`: should still pass.
  5. `test_on_1m_bar_calls_hypothesis_only_on_5m`: should still pass.
  6. Add new test: `test_on_daily_or_startup_seeds_session_ath`: verify `global.json["session_ath"]` is set from hist max after `on_daily_or_startup`.
  7. Add new test: `test_0920_gate_calls_on_daily_or_startup`: call `on_1m_bar` with `now` at 09:20 ET and verify `on_daily_or_startup` fires (mock it, assert call count == 1); call again at 09:21 — assert no additional fire.
  8. Add new test: `test_per_bar_updates_day_high`: set up pipeline, call `on_session_start` (force_reset=True), then call `on_1m_bar` with a bar that exceeds stored `day_high`. Verify `daily.json["liquidities"]` entry for `day_high` is updated.
  9. Add new test: `test_per_bar_fvg_visited_prune`: insert a FVG into daily.json; call `on_1m_bar` with a bar that enters the FVG zone; verify FVG is removed from liquidities.
  10. Add new test: `test_force_reset_true_resets_hypothesis`: verify `hypothesis.json["direction"] == "none"` after `on_session_start(force_reset=True)`.
  11. Add new test: `test_force_reset_false_preserves_hypothesis`: set hypothesis direction to "up", call `on_session_start(force_reset=False)`, verify direction still "up".
- **VALIDATE**: `uv run python -m pytest tests/test_session_pipeline.py -v`

---

#### Task 4.3: UPDATE `tests/test_smt_v2_dispatcher.py` and `test_smt_dispatch_order.py`

- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [3.1]
- **BLOCKS**: []
- **IMPLEMENT**:
  1. `test_smt_v2_dispatcher.py`: All tests mock `run_daily` — update mocks to `run_daily_fixed`.
  2. Tests that assert `all_time_high` is set from hist (e.g. line 102) — should still pass since ATH seeding is in `on_daily_or_startup` which `on_session_start` calls.
  3. Add test: `test_force_reset_env_var_passed_to_pipeline`: monkeypatch `os.environ["FORCE_RESET"] = "true"`, create dispatcher, call `on_session_start`; verify `pipeline.on_session_start` was called with `force_reset=True`.
  4. `test_smt_dispatch_order.py`: update `fake_run_daily` mocks to `fake_run_daily_fixed`. Check that fake signatures match new parameter list.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_v2_dispatcher.py tests/test_smt_dispatch_order.py -v`

---

#### Task 4.4: UPDATE `tests/test_smt_hypothesis.py` if hypothesis.py changes break tests

- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [3.3]
- **BLOCKS**: []
- **IMPLEMENT**:
  1. Run `test_smt_hypothesis.py` after Task 3.3 and identify failures.
  2. If tests set up `daily.json` with `day_high/day_low` and relied on `hypothesis.py` overwriting them from live bars: now `hypothesis.py` reads them as-is from `daily.json`. Tests need to ensure `daily.json` is pre-populated with correct values (they likely already do via `_make_default_daily()`).
  3. Remove any test that specifically asserted the `compute_live_hl_mid` refresh behavior inside `run_hypothesis`.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_hypothesis.py -v`

---

## TESTING STRATEGY

| What | Tool | Location | Run command |
|---|---|---|---|
| daily.py unit | pytest | `tests/test_smt_daily.py` | `uv run python -m pytest tests/test_smt_daily.py -v` |
| session_pipeline unit | pytest | `tests/test_session_pipeline.py` | `uv run python -m pytest tests/test_session_pipeline.py -v` |
| dispatcher wiring | pytest | `tests/test_smt_v2_dispatcher.py` | `uv run python -m pytest tests/test_smt_v2_dispatcher.py -v` |
| hypothesis unit | pytest | `tests/test_smt_hypothesis.py` | `uv run python -m pytest tests/test_smt_hypothesis.py -v` |
| backtest regression | pytest | `tests/test_smt_backtest.py` | `uv run python -m pytest tests/test_smt_backtest.py -v --timeout=60` |
| full suite | pytest | `tests/` | `uv run python -m pytest tests/ -q --timeout=60` |

### Unit Tests

**Task 4.1 — `test_smt_daily.py`**:
- ✅ `test_run_daily_fixed_writes_tdo` — TDO appears in liquidities
- ✅ `test_run_daily_fixed_writes_two` — TWO appears in liquidities
- ✅ `test_run_daily_fixed_writes_prev2_day_levels` — prev1/prev2 entries written
- ✅ `test_run_daily_fixed_1hr_fvg_detected` — 1hr FVG written with correct top/bottom
- ✅ `test_run_daily_fixed_4hr_fvg_detected` — 4hr FVG written (NEW)
- ✅ `test_run_daily_fixed_does_not_reset_hypothesis` — direction unchanged after call
- ✅ `test_run_daily_fixed_does_not_reset_position` — position unchanged after call
- ✅ `test_all_time_high_updates_when_today_higher` — ATH in global.json updated
- ✅ `test_all_time_high_unchanged_when_today_lower` — ATH not decreased

**Task 4.2 — `test_session_pipeline.py`** (new tests):
- ✅ `test_on_daily_or_startup_seeds_session_ath`
- ✅ `test_0920_gate_calls_on_daily_or_startup`
- ✅ `test_per_bar_updates_day_high`
- ✅ `test_per_bar_fvg_visited_prune`
- ✅ `test_force_reset_true_resets_hypothesis`
- ✅ `test_force_reset_false_preserves_hypothesis`

**Task 4.3 — `test_smt_v2_dispatcher.py`** (new test):
- ✅ `test_force_reset_env_var_passed_to_pipeline`

### Edge Cases

- **09:20 gate fires once**: bar at 09:20:00 and 09:20:30 both in same minute — gate fires once only ✅ `test_0920_gate_fires_once_per_minute`
- **force_reset=True with active position**: position and hypothesis reset, direction conflict caught on next bar ✅ `test_force_reset_with_active_position`
- **FVG at 1hr boundary**: bar at `now.minute == 0` detects new FVGs ✅ `test_new_1hr_fvg_detected_at_hour_boundary`
- **Backtest forward_reset**: ensure backtest tests still pass with `force_reset=True` explicitly

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest) | 20 | 100% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 20 | 100% |

Manual tests: None required. Trade.py `--force` flow requires live IB but the env var propagation is testable via subprocess env inspection.

---

## VALIDATION COMMANDS

### Level 1: Imports OK

```bash
uv run python -c "from daily import run_daily_fixed; print('daily OK')"
uv run python -c "from session_pipeline import SessionPipeline; print('pipeline OK')"
uv run python -c "import signal_smt; print('signal_smt OK')"
uv run python -c "import automation.main; print('automation OK')"
```

### Level 2: Unit Tests

```bash
uv run python -m pytest tests/test_smt_daily.py -v
uv run python -m pytest tests/test_session_pipeline.py -v
uv run python -m pytest tests/test_smt_v2_dispatcher.py tests/test_smt_dispatch_order.py -v
uv run python -m pytest tests/test_smt_hypothesis.py -v
```

### Level 3: Backtest Regression

```bash
uv run python -m pytest tests/test_smt_backtest.py -v --timeout=60
```

### Level 4: Full Suite

```bash
uv run python -m pytest tests/ -q --timeout=60
```

Expected: zero new failures vs pre-change baseline (run baseline first and record count).

---

## ACCEPTANCE CRITERIA

- [ ] `run_daily` renamed to `run_daily_fixed`; no `reset_hypothesis`/`reset_position` params; no dynamic level computation (day/week/session H/L removed from function)
- [ ] `run_daily_fixed` detects and writes both 1hr and 4hr FVGs from hist data
- [ ] `SessionPipeline.on_daily_or_startup()` exists; seeds ATH/session_ath, resamples, calls `run_daily_fixed`, seeds initial day/week/session levels
- [ ] `on_session_start(force_reset=False)` calls `on_daily_or_startup`; no state reset unless `force_reset=True`; first hypothesis only when `force_reset=True`
- [ ] `on_1m_bar` fires `on_daily_or_startup` at the first 09:20 ET bar of each calendar day (not repeated within same minute)
- [ ] `on_1m_bar` updates session/day/week H/L and FVGs in `daily.json` on every bar
- [ ] `liquidity-updated` events emitted when any tracked level changes value
- [ ] `trade.py start`: no interactive prompt; no `--resume`; always kills silently; `--force` sets `FORCE_RESET=true` in subprocess env
- [ ] `FORCE_RESET=true` propagates from orchestrator → subprocess env → `SmtV2Dispatcher` → `pipeline.on_session_start(force_reset=True)` in both `signal_smt.py` and `automation/main.py`
- [ ] `backtest_smt.py` calls `pipeline.on_session_start(force_reset=True)` (backtests always get fresh state)
- [ ] `hypothesis.py` no longer calls `compute_live_hl_mid` to refresh day/week levels — reads from `daily.json` directly
- [ ] All existing tests pass (zero regressions); new tests pass

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order
- [ ] Each task validation passed
- [ ] All validation levels executed (1–4)
- [ ] All automated tests created and passing
- [ ] Full test suite passes (no regressions)
- [ ] No linting/type errors (imports clean, no undefined names)
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**`_session_bars` visibility**: This function is defined in `daily.py` but needed by `session_pipeline.py` for per-bar session H/L updates. Options: (a) import it from `daily` — requires making it importable (it already is at module level), (b) duplicate the time-window logic inline in `session_pipeline.py`. Prefer (a).

**`compute_live_hl_mid` in `session_pipeline.py`**: After Task 2.1, `session_pipeline.py` imports and calls `compute_live_hl_mid` from `hypothesis.py` for the initial seeding in `on_daily_or_startup`. This creates a mild circular-import risk (hypothesis imports smt_state, session_pipeline imports hypothesis). Since this was already the case before this change (hypothesis was already called from session_pipeline), it's not new. Verify import order is correct.

**`overnight_range` field removal**: `strategy.py:250` reads `_daily.get("overnight_range", 0)` for the chop detection guard. After Task 1.2 removes this field from `daily.json`, it will default to `0` (the `.get` default), effectively disabling the chop guard. This is an acceptable interim state — the chop range can be added back as a per-bar computed value later. Document in code: `# overnight_range removed; chop guard disabled pending per-bar range tracking`.

**`SmtV2Dispatcher` duplication**: Both `signal_smt.py` and `automation/main.py` have their own `SmtV2Dispatcher`. Task 3.1 must update both. They are intentionally kept separate (per the Session Pipeline Unification execution report), so update both independently.

**Backtest `force_reset`**: The backtest path in `backtest_smt.py` creates a new `SessionPipeline` for each trading day and calls `on_session_start`. Passing `force_reset=True` restores the old reset behavior for backtests. Verify the backtest also re-seeds state JSON files (position.json, hypothesis.json, daily.json) before each day's pipeline — it currently does this via the old `run_daily` path; with the new design, the `force_reset=True` path in `on_session_start` handles it.
