# Code Review: SMT Divergence MNQ Strategy

**Date:** 2026-03-31
**Branch:** master
**Reviewer:** Claude Sonnet 4.6

## Stats

- Files Modified: 3 (data/sources.py, tests/conftest.py, tests/test_data_sources.py)
- Files Added: 4 (prepare_futures.py, train_smt.py, tests/test_smt_strategy.py, tests/test_smt_backtest.py)
- New lines: ~660
- Deleted lines: 3

---

## Pre-existing Failures

**test_data_sources.py** — `ModuleNotFoundError: No module named 'yfinance'` when running this test file. Confirmed pre-existing by `git stash` verification. Not introduced by this changeset.

---

## Issues Found

---

```
severity: high
file: train_smt.py
line: 461
issue: Orphaned end-of-backtest position silently discarded when it is the only trade
detail: The guard `if position is not None and trades:` requires `trades` to be non-empty.
        If a position was opened but never closed during the backtest (e.g. the first and
        only signal fires near the end window), `trades` is still an empty list and the
        position is silently dropped — the final P&L and equity curve both miss it entirely.
        Even when trades is non-empty, the trailing fields are copied from trades[-1]
        (the last *closed* trade), not from the open position, producing incorrect
        entry_date, entry_time, direction, and contracts in the appended record.
suggestion: Remove the `and trades` guard. Build the record directly from `position`,
            not from trades[-1]. Example fix:
            if position is not None:
                last_bar = mnq_df[mnq_df.index.date < end_dt].iloc[-1]
                exit_price = float(last_bar["Close"])
                ...
                trades.append({
                    "entry_date":  str(position["entry_date"]),
                    "entry_time":  ...,
                    ...
                    "exit_type": "end_of_backtest",
                })
```

---

```
severity: high
file: prepare_futures.py
line: 52
issue: Both ThreadPoolExecutor threads use the same IB client_id=2, causing the second connection to be rejected
detail: IBGatewaySource is instantiated inside process_ticker() with a hardcoded
        client_id=2 for both MNQ and MES. Interactive Brokers rejects any new connection
        that presents a clientId already in use by another active session. Because
        ThreadPoolExecutor runs both tickers concurrently, the second thread's ib.connect()
        will fail with "There is no current event loop" or "clientId already in use",
        returning None for that ticker and then calling sys.exit(1).
suggestion: Use distinct client IDs per thread. The simplest fix is to pass the index
            into process_ticker and derive the client_id from it:
            def process_ticker(args):
                i, ticker = args
                source = IBGatewaySource(host=IB_HOST, port=IB_PORT, client_id=2 + i)
            Or avoid the thread pool entirely for only 2 tickers and fetch sequentially.
```

---

```
severity: medium
file: data/sources.py
line: 138
issue: ContFuture exchange hardcoded to "CME"; MES and MNQ are on GLOBEX (CME Globex)
detail: ib_insync's ContFuture takes the exchange string that IB recognises. For CME
        Group micro futures (MNQ, MES, MYM etc.) the correct exchange string is
        "GLOBEX" not "CME". Using "CME" may work depending on IB routing rules but
        "GLOBEX" is the canonical value used in ib_insync documentation for these
        instruments and is what IB TWS/Gateway reports. If IB rejects "CME" for a
        given account type, qualifyContracts() will raise or return an ambiguous match.
suggestion: Change line 138 to:
            contract = ContFuture(ticker, "GLOBEX", "USD")
            and update the corresponding assertion in test_ibgateway_contfuture_uses_correct_contract.
```

---

```
severity: medium
file: data/sources.py
line: 117-124
issue: DataSource Protocol signature no longer matches IBGatewaySource.fetch()
detail: The DataSource Protocol (line 22) declares fetch(ticker, start, end, interval)
        with exactly 4 parameters. IBGatewaySource.fetch() now has a fifth parameter
        contract_type. While isinstance() checks still pass (runtime_checkable only
        verifies method existence, not signature), any code that calls fetch() through
        a DataSource-typed reference cannot pass contract_type — the extra parameter is
        invisible to callers using the abstraction. prepare_futures.py bypasses this by
        calling IBGatewaySource directly, but the mismatch will cause confusion for
        future callers and violates Liskov Substitution.
suggestion: Add contract_type to the DataSource Protocol definition with a default value:
            def fetch(self, ticker, start, end, interval, contract_type="stock") -> pd.DataFrame | None: ...
```

---

```
severity: medium
file: tests/test_smt_backtest.py
line: 1-302 (entire file)
issue: test_smt_backtest.py contains only imports — no test functions are defined
detail: The file has a docstring and 10 import statements but zero test functions.
        The partial content in tests/_test_smt_backtest_part1.txt shows helper
        _build_short_signal_bars was intended but not included. pytest collects 0 tests
        from this file. run_backtest() has no integration test coverage at all —
        the session_close path, multi-day position carry-over, equity curve accumulation,
        and _compute_metrics are all untested.
suggestion: Complete the backtest integration tests. At minimum:
            - test_run_backtest_single_short_trade: one day produces a short, exits at TP
            - test_run_backtest_session_close: position not exited during session, closed at session_close
            - test_run_backtest_no_signals: empty result dict with zeroed metrics
            - test_run_backtest_empty_dataframe: empty DataFrame input returns gracefully
```

---

```
severity: medium
file: train_smt.py
line: 380
issue: screen_session called with swapped argument order (mnq_session, mes_session) vs its signature (mnq_bars, mes_bars)
detail: screen_session is defined as screen_session(mnq_bars, mes_bars, date).
        In run_backtest at line 380 it is called as screen_session(mnq_session, mes_session, day)
        which is correct. However, screen_session internally calls detect_smt_divergence
        as detect_smt_divergence(mes_session.reset_index(drop=True), mnq_session.reset_index(drop=True), ...)
        meaning the MES bars are passed as the first argument (mes_bars) to detect_smt_divergence —
        also correct. The call chain is consistent. No bug here, but the argument order
        inversion between the public screen_session signature and its internal call to
        detect_smt_divergence is a maintenance trap. The detect_smt_divergence docstring
        says "mes_bars" first, and screen_session passes mes first to it, even though
        screen_session's own signature puts mnq first. This is confusing but not broken.
suggestion: Reorder detect_smt_divergence parameters to (mnq_bars, mes_bars, ...) or add
            a comment in screen_session explaining the inversion to reduce future confusion.
```

---

```
severity: low
file: train_smt.py
line: 128 and 196
issue: print() used in production paths
detail: IBGatewaySource.fetch() at sources.py line 128 prints an error message.
        train_smt.py line 196 (compute_tdo docstring) is fine. However, the module-level
        __main__ block uses print() throughout, which is acceptable for a CLI harness.
        The sources.py print at line 128 violates the project's "Production code is silent"
        standard from CLAUDE.md. (This pre-dates this changeset but the ContFuture path
        inherits the same print.)
suggestion: The print in sources.py line 128 is pre-existing. No action needed for this
            changeset, but flag for cleanup in a follow-up.
```

---

```
severity: low
file: tests/test_smt_strategy.py
line: 368-373 and 377-383
issue: test_screen_session_returns_short_signal and test_screen_session_returns_long_signal use `if signal is not None` instead of asserting signal is not None
detail: Both tests check assertions inside `if signal is not None:` blocks. If
        screen_session returns None (e.g. due to MIN_BARS_BEFORE_SIGNAL not being
        met with the synthetic data), the test passes vacuously without verifying
        anything. This silently hides regressions in screen_session.
suggestion: Replace `if signal is not None:` with `assert signal is not None` before
            the field assertions so the test fails loudly when no signal is detected.
```

---

## Summary

7 issues found: 2 high, 3 medium, 2 low.

The two high-severity bugs are:
1. The orphaned position at end of backtest is silently dropped when it is the only trade, and the end-of-backtest trade record is constructed from the wrong source (last closed trade instead of the open position).
2. Concurrent IB connections in prepare_futures.py share a hardcoded client_id=2, which IB will reject for the second thread.

The ContFuture contract creation in data/sources.py is functionally correct for the common case but uses "CME" instead of the more precise "GLOBEX" exchange string. The DataSource Protocol mismatch is a latent design issue. test_smt_backtest.py is effectively an empty stub with no tests covering run_backtest().
