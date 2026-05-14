# Code Review: live-orders-refactor-and-trade-cli

**Date:** 2026-05-14
**Plan:** `.agents/plans/live-orders-refactor-and-trade-cli.md`
**Reviewer:** Claude Sonnet 4.6

---

## Stats

- Files Modified: 11 (tracked)
- Files Added: 3 (untracked: trade.py, tests/test_bar_state.py, tests/test_trade_cli.py)
- Files Deleted: 0
- New lines: +445
- Deleted lines: -217
- Test suite: 52/52 passed (verified)

---

## Issues Found

---

```
severity: medium
file: strategy.py
line: 204, 208
issue: f-strings without format placeholders — should be plain strings
detail: Both print statements in the fill fallback path use f"..." syntax but contain no {variable} expressions. This is a minor inefficiency and style error. Additionally, CLAUDE.md requires production code to be silent ("No print/stdout logging in production paths"), though the existing codebase uses print() throughout automation/main.py as the primary stdout relay mechanism, making this pattern pre-existing and project-consistent. The f-string issue is standalone regardless.
suggestion: Change:
  print(f"[STRATEGY] fill detected but no bar_state.json — skipping fill", flush=True)
to:
  print("[STRATEGY] fill detected but no bar_state.json — skipping fill", flush=True)
(same for line 208)
```

---

```
severity: medium
file: trade.py
line: 42
issue: f-string without format placeholder
detail: print(f"ERROR: bar_state.json not found — cannot determine stop price") uses an f-prefix but has no interpolated expressions.
suggestion: Remove the f prefix: print("ERROR: bar_state.json not found — cannot determine stop price")
```

---

```
severity: medium
file: smt_state.py
line: 136
issue: save_bar_state creates real disk directories even in in-memory (backtest) mode
detail: save_bar_state calls path.parent.mkdir(parents=True, exist_ok=True) before _atomic_write. In backtest mode (_IN_MEMORY=True), _atomic_write correctly skips disk writes and stores in _STORE. However, the mkdir call runs unconditionally, creating a real sessions/{today}/ directory on disk during every backtest run. The other save_* functions (save_position, save_global, etc.) do not call mkdir — they rely on the DATA_DIR being pre-existing. The real directory is created at the process working directory, not in any temp location.

In practice this creates exactly one directory per backtest run (sessions/{today}/ — all backtest bars share today's date as the path key since bar_state_path uses date.today()). It is harmless but a surprising side effect that pollutes the project directory tree during backtesting.
suggestion: Guard the mkdir with an _IN_MEMORY check:
  def save_bar_state(data: dict, date_str: str | None = None) -> None:
      path = bar_state_path(date_str)
      if not _IN_MEMORY:
          path.parent.mkdir(parents=True, exist_ok=True)
      _atomic_write(path, data)
```

---

```
severity: low
file: live_orders.py
line: 147-155
issue: stop_entry_filled silently skips position.json update when active is absent, with no log
detail: If pos.get("active") is falsy, the executor call (place_stop_after_limit_fill) fires successfully — the broker receives the stop order — but position.json is not updated and no log entry indicates that the position sync was skipped. In normal automated flow this branch is unreachable because strategy.py writes active to position.json before emitting the stop-entry-filled signal. However, in an edge case (orchestrator restart mid-fill, manual invocation via trade.py stop path, or a race condition), the broker stop is placed while position.json remains stale. The event log at line 155 fires regardless, so the event IS recorded; only the position.json sync is silently dropped.
suggestion: Add a warning log in the else branch:
  if pos.get("active"):
      pos["active"]["stop"] = stop_price
      _save_pos(pos)
  else:
      _log({"time": now, "kind": "stop-entry-filled-pos-missing",
            "direction": direction, "stop_price": stop_price,
            "warning": "active not set in position.json — stop dispatched but pos not updated"})
```

---

```
severity: low
file: tests/test_live_orders.py
line: (no test for this path)
issue: Missing negative test for stop_entry_filled when active is absent
detail: test_stop_entry_filled_sends_sl_and_updates_stop (test 4) covers the normal path where active is populated. There is no test verifying behavior when pos["active"] is empty: that executor.place_stop_after_limit_fill is still called, that _save_pos is NOT called, and that the event is still logged. This path is reachable in edge cases.
suggestion: Add:
  def test_stop_entry_filled_noop_when_no_active(_in_tmp, _mock_today):
      empty_pos = {"active": {}, "stop_entry": "", ...}
      mock_executor = MagicMock()
      saved = {}
      with patch.object(live_orders, "_executor", mock_executor), \
           patch("smt_state.load_position", return_value=empty_pos), \
           patch("smt_state.save_position", side_effect=lambda p: saved.update(p)):
          live_orders.stop_entry_filled("long", 19820.0)
      mock_executor.place_stop_after_limit_fill.assert_called_once()
      assert saved == {}  # no save when active is absent
```

---

## Pre-existing Patterns (Not Introduced by This Changeset)

The following items were observed but are pre-existing project-wide conventions, not regressions introduced in this changeset:

- **print() in production code**: automation/main.py has 20+ print() calls as the primary stdout relay mechanism. The new print() calls in strategy.py and automation/main.py._emit follow this established project pattern. CLAUDE.md discourages this, but the project has not adopted a logging framework — only `tests/test_ib_integration.py` imports `logging`.

- **Executor dispatch before position.json clear in close_position**: This is the correct order for trading safety — if the broker call fails, position.json correctly remains active. If the save fails after a successful dispatch, position.json is stale, but this is an inherent two-write atomicity limitation in the existing architecture.

---

## Summary

The refactor is well-executed. The unified 7-function live_orders API is clean and consistent. The bar_state.json mechanism correctly handles both live and in-memory backtest modes (the mkdir side effect in backtest mode is the only real gap). The stop price flow is correct: new-stop-entry carries body_low as a placeholder (SL deferred to fill time), and stop-entry-filled carries the wick-capped SL computed at fill — the design is intentional and matches the plan. All 52 new tests pass and verify the core paths. The four issues above are all medium or low severity; none are critical or blocking.
