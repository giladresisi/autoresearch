# Code Review: signal-smt-implementation

**Date:** 2026-04-14

## Stats

- Files Modified: 3 (`train_smt.py`, `tests/test_smt_strategy.py`, `tests/test_smt_backtest.py`)
- Files Added: 2 (`signal_smt.py`, `tests/test_signal_smt.py`)
- New lines: ~650
- Deleted lines: ~100

---

## Issues

---

```
severity: high
file: signal_smt.py
line: 372–378
issue: `exit_price` is potentially unbound when manage_position returns an unexpected value
detail: The control flow in `_process_managing` assigns `exit_price` in three separate branches:
  (1) inside the `if result == "hold"` block when session end is reached (line 368),
  (2) in the `if result == "exit_tp"` branch (line 373),
  (3) in the `elif result == "exit_stop"` branch (line 375).
  If `manage_position` ever returns a value not in {"hold", "exit_tp", "exit_stop"} — for
  example if the function is extended in the future to return "exit_time" (a value already
  listed in its own docstring) — `exit_price` is never assigned and line 378
  (`pnl = _compute_pnl(_position, exit_price)`) raises UnboundLocalError at runtime,
  silently corrupting the state machine (position becomes None, state reverts to SCANNING,
  but pnl is never logged).
suggestion: Initialize `exit_price` to a sentinel before the branching, or add an explicit
  else-branch that raises ValueError. A safe pattern:
      exit_price: float  # assigned in all exit branches
      if result == "exit_tp":
          exit_price = _position["take_profit"]
      elif result == "exit_stop":
          exit_price = _position["stop_price"]
      elif result == "exit_session_end":
          pass  # already set in the hold block above
      else:
          raise ValueError(f"Unexpected manage_position result: {result!r}")
```

---

```
severity: medium
file: signal_smt.py
line: 170–176
issue: Gap-fill start timestamp uses only mnq_df age, ignoring mes_df; can silently leave MES data stale
detail: `_gap_fill_1m` computes a single `start_ts` based on `mnq_df.index[-1]` (or the 30-day floor
  if mnq_df is empty). This same `start_ts` is then used for both MNQ and MES fetch calls.
  If mnq_df and mes_df are in different states — e.g., mnq_df has data through yesterday but
  mes_df was never cached — MES will only be fetched from the same start as MNQ, potentially
  missing older MES bars needed for accurate session reconstruction.
  The converse also holds: if mes_df is older than mnq_df, MES is over-fetched unnecessarily.
suggestion: Compute separate start timestamps for MNQ and MES, one per instrument's last cached bar:
      start_mnq = max(mnq_df.index[-1], lookback_floor) if not mnq_df.empty else lookback_floor
      start_mes = max(mes_df.index[-1], lookback_floor) if not mes_df.empty else lookback_floor
  Then fetch each with its own start string.
```

---

```
severity: medium
file: signal_smt.py
line: 287–288
issue: `_session_start_time` and `_session_end_time` are None at module level; callback
  invoked before main() causes AttributeError
detail: `_session_start_time` and `_session_end_time` are initialized to None at module load
  (line 63–64) and only set inside `main()` (lines 397–398). The IB callbacks (`on_mnq_1s_bar`,
  `_process_scanning`) call `bar_time < _session_start_time`, which raises `TypeError: '<'
  not supported between instances of 'datetime.time' and 'NoneType'` if any bar arrives
  before main() has run (e.g., during unit tests that call the callbacks without going through
  main(), or if callbacks are registered before session times are set).
  The test suite avoids this by monkeypatching the module vars, but it represents a latent
  ordering dependency that can cause opaque errors.
suggestion: Initialize to a non-None default:
      _session_start_time = datetime.time(9, 0)   # ET; overridden in main()
      _session_end_time   = datetime.time(13, 30)
  Or add a guard at the top of _process_scanning:
      if _session_start_time is None:
          return
```

---

```
severity: medium
file: tests/test_signal_smt.py
line: 554
issue: `test_30_day_cap_on_gap_fill` patches the wrong symbol and fails
detail: The test uses `mock.patch("data.sources.IBGatewaySource", ...)`. However, `_gap_fill_1m`
  imports `IBGatewaySource` with a local `from data.sources import IBGatewaySource` statement,
  meaning the name inside signal_smt's local scope is already bound. The patch target should
  be `signal_smt._gap_fill_1m`'s local import, which requires patching the name in
  `data.sources` before it is imported, or restructuring the test.
  Additionally, `data.sources` cannot be resolved by mock.patch when `yfinance` is absent
  (the module-level `import yfinance as yf` on line 11 of data/sources.py fails), so the
  test always raises `AttributeError: module 'data' has no attribute 'sources'`.
  This test failure is pre-existing (reproduces on the commit before this changeset).
suggestion: The correct patch target is `data.sources.IBGatewaySource` but requires yfinance
  to be installed (or mocked). Alternatively, restructure _gap_fill_1m to accept a `source`
  argument for testability, then inject the fake_source directly without patching.
```

---

```
severity: low
file: signal_smt.py
line: 420–421
issue: Future contracts instantiated without exchange or currency; may fail IB qualification
detail: Lines 421–422 create contracts with only conId:
      mnq_contract = Future(conId=int(MNQ_CONID))
      mes_contract = Future(conId=int(MES_CONID))
  ib_insync Future objects require the exchange to be set (or `qualifyContracts` to be called)
  before reqHistoricalData. Without `_ib.qualifyContracts(mnq_contract, mes_contract)`, IB
  may reject the subscription with error 200 ("No security definition has been found").
  IBGatewaySource.fetch (data/sources.py, future_by_conid path) does call qualifyContracts —
  but the contracts in main() skip that step.
suggestion: Add `_ib.qualifyContracts(mnq_contract, mes_contract)` immediately after contract
  instantiation and before the reqHistoricalData calls, to mirror the pattern used in
  IBGatewaySource.fetch.
```

---

```
severity: low
file: signal_smt.py
line: 93–105
issue: `_format_signal_line` divides by `stop_dist` without guarding against exact-zero stop distance
detail: Line 97 computes `rr = dist / stop_dist if stop_dist > 0 else 0.0` — the guard is
  present. However, `stop_dist` is computed from `signal["entry_price"] - signal["stop_price"]`
  using `abs()`. If the strategy emits a signal where stop_price == entry_price (degenerate
  but possible if MIN_STOP_POINTS == 0 and distance_to_tdo is zero), the display is benign
  (rr=0.0). The guard is correct; this is a documentation note only — no code change needed.
  Mark as low / informational.
suggestion: No action required; existing guard is sufficient.
```

---

```
severity: low
file: train_smt.py
line: 420
issue: manage_position docstring lists "exit_time" as a possible return value but the function never returns it
detail: The docstring on line 420 says:
      Returns one of: "hold" | "exit_tp" | "exit_stop" | "exit_time"
  The implementation body only returns "exit_stop", "exit_tp", or "hold". "exit_time" is
  never returned. The stale docstring is a readability hazard: it caused a review concern about
  an unbound variable in signal_smt._process_managing, and could mislead future callers.
suggestion: Remove "exit_time" from the Returns docstring. Correct: "hold" | "exit_tp" | "exit_stop"
```

---

## Pre-existing Failures

```
test: tests/test_signal_smt.py::test_30_day_cap_on_gap_fill
status: pre-existing (fails on the commit before this changeset)
root cause: mock.patch("data.sources.IBGatewaySource") resolves the module object via
  pkgutil.resolve_name, which triggers data/sources.py's module-level `import yfinance as yf`.
  Since yfinance is not installed in the test environment, the module attribute cannot be
  resolved and AttributeError is raised. Unrelated to the changes in this execution.
```

---

## Summary

The new `signal_smt.py` is well-structured with a clean state machine and thoughtful guards
(startup guard, re-detection guard, alignment gate). The main correctness concern is the
potentially unbound `exit_price` variable in `_process_managing` (high severity), which is
safe given the current `manage_position` implementation but is a fragile assumption that will
break silently if `manage_position` is extended. The gap-fill asymmetry (MES start timestamp
driven by MNQ state) is a medium-severity data integrity issue. The missing `qualifyContracts`
call is a low-severity operational concern that could cause a startup failure in live trading.
