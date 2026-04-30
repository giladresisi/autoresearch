# Code Review — SMT Redesign v2

**Branch**: follow-live
**Reviewer**: Claude Sonnet 4.6
**Date**: 2026-04-27

---

## Stats

- Files Modified: 2 (`backtest_smt.py`, `signal_smt.py`)
- Files Added: 13 (6 modules + 7 test files)
- New lines: ~1900 (code + tests)
- All 23 `test_smt_state` + 27 `test_smt_strategy_v2/trend` + 5 `test_smt_dispatch_order` tests pass

---

## Issues Found

---

### Issue 1

```
severity: high
file: backtest_smt.py
line: 1353
issue: Unreachable dead code references undefined variable after return
detail: The last line of run_backtest_v2 is `_write_trades_tsv(_all_test_trades)` placed
        after the `return {...}` statement. It will never execute. Additionally,
        `_all_test_trades` is not defined anywhere inside run_backtest_v2 — it is a
        module-level name from the `__main__` block (line 1033), so if the line were
        somehow reached it would silently operate on stale global data instead of the
        function's `all_trades` list. This appears to be a copy-paste residue from the
        `__main__` scaffolding and should be deleted.
suggestion: Delete the unreachable line. The function already writes per-day files
            inside the loop when write_events=True, and returns in-memory results.
```

---

### Issue 2

```
severity: high
file: backtest_smt.py
line: 1279-1291
issue: Trade pairing in run_backtest_v2 always defaults direction to "up" because
       the limit-entry-filled signal carries no direction field
detail: strategy.py's `_make_signal("limit-entry-filled", ...)` returns only
        {kind, time, price} — no "direction" key. The trade-pairing loop at line 1287
        does `direction = entry_event.get("direction", "up")`, so every trade,
        long or short, is booked as "up" and `direction_sign = 1` always. For a SHORT
        position, pnl_points = (exit - entry) * 1 instead of (entry - exit) * 1,
        producing an inverted P&L sign.

        Currently, hypothesis.py hardcodes `direction = "up"` (line 298 of
        hypothesis.py), so no short positions are generated in practice — this bug
        has no runtime impact at present. However, it is a latent correctness bug
        that will silently corrupt all short-trade P&L the moment real direction
        logic is wired in. It also makes the regression baseline unreliable for
        future short-capable iterations.
suggestion: Either (a) include `"direction": position["active"]["direction"]` in the
            limit-entry-filled signal from strategy.py, or (b) read direction from
            the position.json active dict at pairing time
            (smt_state.load_position()["active"].get("direction", "up")).
            Option (a) is cleaner and keeps the signal self-contained.
```

---

### Issue 3

```
severity: medium
file: signal_smt.py
line: 1010-1092 (SmtV2Dispatcher class) + 1161-1162 (main)
issue: SmtV2Dispatcher is instantiated in main() but its on_session_start and
       on_1m_bar methods are never wired to IB callbacks — the v2 pipeline is dead
       code in live mode
detail: `_setup_ib_subscriptions` (line 1095) always registers the v1 callbacks
        (on_mnq_1m_bar, on_mes_1m_bar, on_mnq_tick, on_mes_tick) regardless of
        SMT_PIPELINE. SmtV2Dispatcher.on_session_start and on_1m_bar are never called.
        Additionally, main() passes `_mnq_1m_df` as both `mnq_1m_df` AND `hist_mnq_1m`
        (line 1162), so the dispatcher has the same DataFrame object for both current and
        historical bars. The live bar callbacks reassign the global `_mnq_1m_df` to a
        new pd.concat result; the dispatcher's stored reference becomes permanently
        stale after the first bar arrives.

        This is clearly scope-limited by the plan ("Live IB smoke explicitly out of scope
        — deferred to first realtime trial") but the wiring gap means SMT_PIPELINE=v2
        has zero effect in production.
suggestion: Document in a code comment that the IB wiring is a stub pending the first
            realtime trial. Alternatively, add a minimal routing shim inside
            on_mnq_1m_bar / on_mes_1m_bar that calls `_smtv2_dispatcher.on_1m_bar()`
            when `_smtv2_pipeline == "v2"`. Either is acceptable; the current state is
            misleading without a comment.
```

---

### Issue 4

```
severity: medium
file: daily.py
line: 86-90 (_compute_two function)
issue: TWO computation returns wrong value when today is Sunday (futures-week open day)
detail: The function computes:
          today_weekday = today_ts.isocalendar().weekday  # 1=Mon...7=Sun
          days_since_monday = today_weekday - 1           # 6 when today is Sunday
          monday_ts = today_ts - Timedelta(days=6)        # Mon of prior ISO week
          sunday_ts = monday_ts - Timedelta(days=1)       # Sun = 7 days before today
        When today IS Sunday (weekday=7), sunday_ts ends up being the prior Sunday (7
        days back), not today. The Sunday 18:00 ET lookup targets the wrong date, so
        TWO falls through to Monday-00:00 fallback or the first available ISO-week bar —
        neither of which is the correct futures-week open when trading starts on Sunday.

        This only fires when run_backtest_v2 is called with a Sunday start date, which
        is uncommon since `pd.bdate_range` skips weekends. However, if someone passes
        a Sunday start date explicitly, or extends to include Sunday bars, the calculation
        is silently wrong.
suggestion: Add a guard before computing sunday_ts:
              if today_weekday == 7:  # today IS Sunday — it's the week open
                  sunday_ts = today_ts
              else:
                  sunday_ts = monday_ts - pd.Timedelta(days=1)
```

---

### Issue 5

```
severity: medium
file: regression.py
line: 27
issue: pandas imported inside a per-line loop body instead of at module top
detail: `import pandas as pd` is inside the `if ":" in line:` branch inside the
        `for raw_line in f:` loop. Python caches module imports so this is not a
        correctness bug, but it is non-idiomatic, hides the dependency from static
        analysis tools, and triggers an unnecessary sys.modules lookup on every
        date-range line. The pattern is inconsistent with the rest of the codebase.
suggestion: Move `import pandas as pd` to the top of regression.py alongside the
            other standard-library imports.
```

---

### Issue 6

```
severity: low
file: strategy.py
line: 95-97 (direction-mismatch path)
issue: direction-mismatch market-close leaves confirmation_bar populated in position.json
detail: Section 3.1 clears `position["active"]` and `position["limit_entry"]` but
        does not clear `position["confirmation_bar"]`. After a direction-mismatch
        close, hypothesis.direction becomes "none" (set by trend.py earlier on the
        same bar). On the next hypothesis run that transitions none→up/down, the
        position confirmation_bar is reset by hypothesis.py (step 10). However, in
        the window between the market-close and the next hypothesis run, position.json
        on disk has an inconsistent state: active={}, limit_entry="", but
        confirmation_bar is non-empty.

        In practice this is harmless: the fill check in section 2.4 only triggers
        when limit_entry != "", so the stale confirmation_bar cannot produce a
        spurious fill. But the on-disk state is semantically inconsistent and could
        confuse future consumers of position.json.
suggestion: Add `position["confirmation_bar"] = {}` in the direction-mismatch cleanup
            at strategy.py line 95-97, mirroring what trend.py's
            _clear_position_and_hypothesis does.
```

---

### Issue 7

```
severity: low
file: hypothesis.py
line: 104-118 (_find_last_liquidity function)
issue: O(n * m) backward scan where n = len(mnq_1m bars) and m = len(meaningful levels)
detail: The function iterates all bars in reverse and for each bar checks all levels.
        With a full session (e.g. 400 bars) and 4 meaningful levels, this is 1600
        comparisons per hypothesis call. This fires every 5m during the session
        (~84 calls per session), totalling ~134K comparisons. For the current fixture
        size this is negligible, but it is quadratic in session length.

        The early-exit `if best_idx == len(bars_array) - 1: break` is correct and
        will short-circuit when the most recent bar already touched a level, but
        worst case (no level touched) is still O(n*m).
suggestion: Acceptable for the current scale. If session bar count grows, a vectorised
            approach (e.g. checking each level against mnq_1m["High"].max() or using
            cummax/cummin) would reduce this to O(n+m).
```

---

### Issue 8

```
severity: low
file: tests/test_smt_trend.py
line: 94-100 (redirect_paths fixture)
issue: Extra monkeypatch.setattr calls for trend module functions are redundant
detail: The fixture patches both smt_state path constants AND the names that trend.py
        imported directly (trend.load_hypothesis, trend.save_hypothesis, etc.). The
        smt_state path patches are sufficient because smt_state's load/save functions
        read the path constants at call time (not at import time). The extra setattr
        calls re-bind the same function objects — they are not wrong, just redundant.
        This could cause confusion for future maintainers who might think the extra
        patches are necessary.
suggestion: Remove the redundant monkeypatch.setattr calls for trend.load_hypothesis,
            trend.save_hypothesis, trend.load_position, trend.save_position,
            trend.load_daily — the smt_state path redirects are sufficient.
```

---

## Key Concerns Addressed

1. **Atomic write correctness (Windows)**: `smt_state._atomic_write` uses
   `path.with_suffix(".tmp")` producing `data/global.tmp` — same directory, same
   drive as the destination. `os.replace` on Windows requires same-volume source and
   dest; this is satisfied. The crash test passes. No issue.

2. **Signal record shape consistency**: All modules use `{"kind", "time", "price"}`
   as the base (strategy.py via `_make_signal`, trend.py inline dicts). The
   `reason` optional field is added correctly via kwargs. The `json.dumps` shape
   test passes. Minor: `limit-entry-filled` is missing a `direction` field
   (Issue 2 above).

3. **State mutation correctness**: All market-close paths in trend.py go through
   `_clear_position_and_hypothesis` which clears `active`, `limit_entry`,
   `confirmation_bar`, and sets `hypothesis.direction="none"`. strategy.py
   direction-mismatch leaves `confirmation_bar` populated (Issue 6 — low severity,
   functionally harmless). stopped-out leaves `confirmation_bar` (also harmless —
   overwritten on next opposite bar).

4. **Same-bar dispatch order (trend blocks strategy)**: Verified by
   `test_trend_invalidation_blocks_same_bar_fill` which passes. The 5m dispatch is
   hypothesis → trend → strategy, so a trend.py direction="none" write is visible
   to strategy.py on the same bar. Confirmed passing.

5. **No scope creep**: No TP, breakeven, trail, secondary target, or MSS/CISD
   invalidation code found in any new file. Confirmed clean.

6. **Test isolation**: All 7 test files use `autouse=True` fixtures with
   `monkeypatch.setattr` on all 4 smt_state path constants (`DATA_DIR`,
   `GLOBAL_PATH`, `DAILY_PATH`, `HYPOTHESIS_PATH`, `POSITION_PATH`). No test
   touches `data/*.json` on disk. Confirmed isolated.

7. **Existing run_backtest and old dispatch paths unchanged**: `run_backtest`
   signature is unchanged (`test_old_run_backtest_unchanged` passes). The old
   `_process_scanning`/`_process_managing` callbacks in signal_smt.py are untouched.
   The new code is purely additive at the bottom of both files.

---

## Summary

The implementation is functionally correct for the current use-case (direction hardcoded
to "up", backtest only). All new tests pass. The most actionable fixes before committing
baseline regression data are:

- **Delete the unreachable `_write_trades_tsv` line** (Issue 1 — prevents undefined
  behaviour if the hardcoded direction changes and causes shorts).
- **Add `direction` to the `limit-entry-filled` signal** (Issue 2 — latent correctness
  bug that will corrupt short P&L when real direction logic is added).
- **Document or wire the v2 IB callback routing** (Issue 3 — currently misleading).

Issues 4–8 are low-to-medium severity quality improvements that can be addressed in
follow-up iterations.
