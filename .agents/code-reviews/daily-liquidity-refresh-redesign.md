# Code Review: daily-liquidity-refresh-redesign

**Plan:** `.agents/plans/daily-liquidity-refresh-redesign.md`

## Stats

- Files Modified: 14
- Files Added: 0
- Files Deleted: 0
- New lines: ~1183
- Deleted lines: ~693

## Test Results

All 78 tests in the changed test files pass. No regressions introduced against the pre-existing
baseline (confirmed by `git stash` baseline run).

Pre-existing failures (not introduced by this changeset):
- `tests/test_automation_main.py::test_v2_session_end_closes_active_position`
- `tests/test_hypothesis_smt.py::test_compute_direction_case_1_3_long`
- `tests/test_pickmytrade_executor.py` (4 tests)
- `tests/test_smt_humanize.py` (2 tests)

---

## Issues Found

---

```
severity: high
file: session_pipeline.py
line: 814-831
issue: asia session H/L not updated for bars in the 18:00–00:00 ET overnight window
detail: In _update_dynamic_liquidities, the active session is set to "asia" when _t >= 18*60.
        _session_bars is then called with now.date() as the 'today' argument.
        But _session_bars for "asia" constructs the search range as:
          start = (today - 1 day) 18:00 ET
          end   = today 00:00 ET
        For a bar at 20:00 ET on Nov 14, now.date()=Nov 14, so the range is
        Nov 13 18:00 -> Nov 14 00:00. The current bar (20:00 Nov 14) is outside
        that range, so _sbars.empty is True and the asia_high/asia_low update is
        silently skipped for the entire 18:00–00:00 window.
        The correct 'today' for bars >= 18:00 ET is now.date() + timedelta(days=1),
        because the asia session "belongs to" the next calendar day
        (it will be completed by the time that day's 09:20 fires).
suggestion: Change the _session_bars call for the asia case to:
              _sbars = _session_bars(_combined_week, "asia",
                                     now.date() + datetime.timedelta(days=1))
            Only apply this +1 offset when _active_sess == "asia".
```

---

```
severity: medium
file: session_pipeline.py
line: 776-784 (the else branch of _set()) and line 847-849 (FVG prune rebuild)
issue: New level entries added by _set() are lost when FVG pruning rebuilds _liq_map
detail: _liq (the list) and _liq_map (the dict) are initialized from the same objects.
        When _set() creates a NEW entry (name not yet in daily.json), it inserts the
        new dict into _liq_map only — _liq is not updated.
        If an FVG is also visited on the same bar, the prune path runs:
          _liq[:] = [l for l in _liq if l["name"] not in _to_remove]
          _liq_map = {l["name"]: l for l in _liq}   # rebuilt from _liq
        Because the new entry was never appended to _liq, the rebuild loses it.
        At save time, _state["liquidities"] = list(_liq_map.values()) therefore
        omits the newly computed day_high/day_low/week_high/week_low entries.
        In practice, on_daily_or_startup seeds these levels before on_1m_bar runs,
        so the 'else' branch only fires on the first bar when daily.json was
        incomplete. But the bug is still reachable if seeding failed or if the 09:20
        gate fires on_daily_or_startup for a bar that also visits an FVG.
suggestion: In the else branch of _set(), also append to _liq:
              new_entry = {"name": name, "kind": kind, "price": price}
              _liq_map[name] = new_entry
              _liq.append(new_entry)    # keep _liq and _liq_map in sync
```

---

```
severity: medium
file: trade.py
line: 247-252
issue: trade.py start no longer prompts before killing a running orchestrator, and
       orphans live broker stop-entry orders when restarting without --force
detail: The old behaviour of "trade.py start" (no flags):
          1. Prompted for confirmation if orchestrator was already running
          2. Called cancel_stop_entry() before resetting position.json to avoid orphaned broker orders
          3. Reset position.json to DEFAULT_POSITION
        The new behaviour skips all three. The prompt removal is intentional (kills unconditionally),
        but the stop-entry cancellation removal creates a live-trading hazard: if a pending
        stop_entry order exists in Tradovate and the user restarts the orchestrator (without --force),
        the new session starts with stop_entry="" in state but the broker order is still live.
        The strategy may then place a second entry order alongside the orphaned one.
suggestion: Either:
          a) Restore the cancel_stop_entry() call before launching the new subprocess even
             in the no-flag path, or
          b) Document clearly that "trade.py start" now assumes no active broker orders and
             that users must manually cancel broker orders before restarting.
```

---

```
severity: low
file: daily.py
line: 218
issue: _daily = load_daily() loaded but never used — unnecessary file I/O
detail: Line 218: `_daily = load_daily()  # noqa: not used after this`
        The variable is assigned and immediately discarded. This is a leftover from
        the old run_daily signature and adds a pointless disk read every 09:20 ET.
suggestion: Remove the line. The call comment "read existing daily.json (recomputed anyway,
            kept for ref)" no longer applies since the code immediately writes a fresh state.
```

---

```
severity: low
file: daily.py
line: 269
issue: global_state["trend"] uses direct indexing instead of .get() — potential KeyError
detail: Line 269: `estimated_dir = global_state["trend"]`
        All other global_state reads in this file and in the adjacent code use .get()
        with a fallback. If a user has an old global.json that predates the "trend" key,
        run_daily_fixed will raise KeyError instead of falling back gracefully.
suggestion: Change to:  `estimated_dir = global_state.get("trend", "up")`
```

---

## Focused Review Notes (per request)

**1. Per-bar liquidity update logic (_update_dynamic_liquidities):**
The day H/L and week H/L calculations are correct in isolation. The week-start computation
in `_week_start_ts` correctly handles Mon–Sun via ISO weekday arithmetic. The FVG visited
prune condition (`_bar_high >= _fbot and _bar_low <= _ftop`) is correct — it fires when the
bar body straddles any part of the FVG zone. The main concern is the _liq/_liq_map sync bug
(issue #2 above) and the asia session date mismatch (issue #1).

**2. 09:20 ET time gate in on_1m_bar:**
The gate condition at lines 269–273 is correct. It uses both `_bar_floor != _last_daily_minute`
(prevents re-firing on the same bar in edge cases) and `now.date() != _last_daily_date`
(ensures it only fires once per calendar day). The guard is set before the call, not after,
which is correct. No off-by-one issues found.

**3. force_reset propagation chain:**
The chain is complete and correct:
  - `trade.py start --force` → `env FORCE_RESET=true` → subprocess Popen
  - `orchestrator/main.py --force` → `force_reset=True` → `ProcessManager(extra_env={"FORCE_RESET":"true"})`
  - `orchestrator/process.py` → `{**os.environ, **_extra_env}` → subprocess Popen
  - `signal_smt.py`/`automation/main.py` SmtV2Dispatcher.__init__ reads `os.environ.get("FORCE_RESET")`
  - `pipeline.on_session_start(..., force_reset=self._force_reset)`
  
  The FORCE_RESET env var is ephemeral (set only for the subprocess spawned that session).
  On orchestrator restart-on-crash, the ProcessManager re-spawns with the same `_extra_env`,
  so force_reset persists correctly through the session. One note: the env is merged via
  `{**os.environ, **self._extra_env}` rather than modifying os.environ in place — this is
  the correct approach.

**4. Empty DataFrame edge cases in run_daily_fixed:**
- `hist_mnq_1m.empty` → TDO/TWO return None → levels not appended (safe)
- `hist_mnq_1m.empty` → ATH update: `_hist_ath = 0.0 if empty`, so ATH only preserves existing (safe)
- `hist_1hr` with < 3 rows → `_detect_fvgs` returns `[]` immediately (safe)
- `_last_n_trading_dates` loop: `hist_mnq_1m.iloc[_ps:_pe]` on empty DF returns empty DF, skipped safely
- `global_state["trend"]` could KeyError if global.json is missing "trend" (see issue #5)

**5. Test quality:**
The new tests (18–23 in test_session_pipeline.py, TestNoResets, TestFourHourFvgDetection,
TestWritesFixedLiquidityNames) are well-structured. Each test:
- Properly isolates state via `_isolate_state` fixture
- Mocks run_daily_fixed and run_hypothesis consistently
- Tests both the positive and negative assertions (e.g. force_reset=True resets, force_reset=False preserves)
- Covers FVG pruning, day_high updates, and the 09:20 gate trigger

One gap: no test exercises the asia session date mismatch identified in issue #1
(a bar at 20:00 ET should update asia_high/asia_low but currently does not).
The test_per_bar_updates_day_high test uses a 09:21 bar, so the overnight session path
is untested.

---

## Pre-existing Failures

The following failures existed before this changeset and are not caused by these changes:
- `tests/test_automation_main.py::test_v2_session_end_closes_active_position` — mock call count mismatch
- `tests/test_hypothesis_smt.py::test_compute_direction_case_1_3_long` — pd_range_case assertion
- `tests/test_pickmytrade_executor.py` (4 tests) — slippage calculation assertions
- `tests/test_smt_humanize.py` (2 tests) — slippage mode assertions
