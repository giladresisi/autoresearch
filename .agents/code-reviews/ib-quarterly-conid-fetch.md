# Code Review: IB Quarterly Contract Fetch via conId

**Date**: 2026-04-01
**Plan**: `.agents/plans/ib-quarterly-conid-fetch.md`
**Reviewer**: ai-dev-env:code-review skill

## Stats

- Files Modified: 4
- Files Added: 0
- Files Deleted: 0
- New lines: ~130 (sources.py +44, prepare_futures.py +26, test_data_sources.py +125, train_smt.py alignment block +7)
- Deleted lines: ~30

## Test Results

Full test suite: **359 passed, 9 skipped** — all new unit tests pass, integration tests auto-skipped (IB offline).

---

## Issues

---

```
severity: medium
file: data/sources.py
line: 237
issue: qualifyContracts called unconditionally for future_by_conid, but fails silently on expired conIds
detail: ib_insync.qualifyContracts logs a warning and returns [] when IB cannot find the contract
        (Error 200: No security definition has been found). The plan NOTES section explicitly states
        "Using the conId directly bypasses qualification and works for both active and recently expired
        contracts." But the code still calls qualifyContracts before the pagination loop. For an expired
        contract, this generates an IB warning log and adds an unnecessary round-trip. The return value
        is not checked, so the contract object (which already has conId and exchange set) is used as-is
        regardless. While this does NOT break functionality today (MNQM6 is still active), it will
        produce noisy warnings after June 18, 2026 rollover when the operator updates CONIDS to
        MNQU6/MESU6 and any backfill fetches MNQM6 historical data.
suggestion: Remove the qualifyContracts(contract) call from the future_by_conid branch entirely.
            The conId uniquely identifies the contract; IB does not require qualification when
            conId + exchange are provided. This also makes the code consistent with the plan rationale
            documented in the NOTES section.
```

---

```
severity: medium
file: data/sources.py
line: 230-253
issue: future_by_conid branch has no early-exit when qualifyContracts fails (return value ignored)
detail: The existing contfuture branch also ignores the qualifyContracts return value, but this is
        a new branch where the concern is explicitly noted in the plan. More importantly: if qualifyContracts
        raises (which it does NOT in ib_insync — it returns []) this is caught by the outer except block.
        However if IB returns an error for the reqHistoricalData calls (e.g., "no data for conId"),
        bars returns [] or None, the code correctly continues to the next chunk. The loop terminates
        when chunk_end reaches start_dt. On zero-data results, all_bars remains empty and the function
        returns None (line 276). This path is correct.
        The only real gap: if bars returns None (not []), the 'if bars:' check on line 251 catches it.
        ib_insync.reqHistoricalData always returns a list (possibly empty), not None. No crash path.
        This issue is informational — the logic is safe but the ignored return value is a code smell.
suggestion: Accept as-is given ib_insync's contract (reqHistoricalData never returns None).
            If future-proofing is desired, add a log or assertion after qualifyContracts:
            'if not ib.qualifyContracts(contract): logger.warning(...)' — but do not block on it.
```

---

```
severity: low
file: tests/test_data_sources.py
line: 317-337
issue: test_ibgateway_future_by_conid_uses_contract_not_stock may pass even if Contract() is called with wrong args
detail: The test patches ib_insync.Contract with mock_contract_cls and asserts
        mock_contract_cls.assert_called_once_with(conId=770561201, exchange="CME"). This is correct
        and does verify the exact call signature. However, note: the mock_contract_cls() call returns
        mock_contract_cls.return_value (a MagicMock). When ib.qualifyContracts(contract) is called,
        it receives this mock. Since qualifyContracts is also mocked (mock_ib.qualifyContracts),
        this is fine. The test correctly verifies the constructor call.
        Minor gap: there is no assertion that qualifyContracts was called with the mock contract
        instance — if future code skips qualifyContracts entirely (the suggested fix above), this
        test remains valid. No action required.
suggestion: No action needed. Test is correct for what it verifies.
```

---

```
severity: low
file: tests/test_data_sources.py
line: 340-357
issue: endDateTime assertion extracts from kwargs only, may miss positional args
detail: In test_ibgateway_future_by_conid_uses_explicit_enddatetime, line 355:
        'end_dt = call.kwargs.get("endDateTime", call.args[1] if len(call.args) > 1 else "")'
        This handles both keyword and positional endDateTime. Since ib_insync.reqHistoricalData
        uses positional arg order (contract, endDateTime, ...), the fallback to call.args[1]
        is correct for positional calls. In sources.py the call uses keyword args, so call.kwargs
        will always have 'endDateTime'. This test is correct as written.
suggestion: No action needed.
```

---

```
severity: low
file: train_smt.py
line: 133-139
issue: Alignment fix placed in load_futures_data() rather than inside run_backtest() — verify this is intentional
detail: The index intersection (common_idx) is computed once at load time across the full 6.5-month
        DataFrame. This is the correct placement: it ensures every call to run_backtest() receives
        pre-aligned DataFrames and every per-day session_mask in screen_session (which uses
        mnq_bars.index to build a mask applied to mes_bars) will produce same-length slices.
        The alternative — aligning inside run_backtest — would work but be O(n) per fold call.
        No issue, just confirming correctness.
        One edge case to note: if MNQM6 and MESM6 have large timestamp gaps for the same calendar
        day (e.g., one instrument had a data outage), the intersection silently drops those bars
        from both. This is the correct behavior for the SMT strategy (divergence detection requires
        simultaneous bars), but it means the downloaded parquet counts may not reflect what
        the backtest actually sees.
suggestion: Document the silent-drop behavior in the load_futures_data docstring so the operator
            is not confused when bar counts in the parquet differ from trading-day bar counts in
            the backtest logs. (Low priority — operator is aware of the data pipeline.)
```

---

```
severity: low
file: prepare_futures.py
line: 30-33
issue: CONIDS dict values are strings, not ints — conversion to int happens in sources.py
detail: CONIDS stores conId values as strings ("770561201"). The conversion to int happens in
        sources.py line 236: 'Contract(conId=int(ticker), ...)'. This is correct and intentional
        (the DataSource.fetch() protocol takes ticker: str). However, a typo in CONIDS (e.g., a
        letter accidentally introduced) would only fail at runtime during fetch, not at module
        load time. The failure would be a ValueError inside the except block, returning None,
        with a printed error message. Acceptable for this codebase.
suggestion: No change required — existing error handling is adequate.
```

---

## Summary

The implementation is correct and complete. All 5 unit tests and the integration test follow existing patterns precisely. The pagination logic is verified correct (4 calls for the 6.5-month window at 60-day chunks). The alignment fix in `load_futures_data()` is correctly placed and sufficient.

**One actionable finding**: the `qualifyContracts(contract)` call on line 237 of `data/sources.py` is unnecessary for the `future_by_conid` path and will generate IB warning logs after the June 2026 rollover when older conIds are re-fetched. Removing it is a one-line fix that aligns the code with the plan's own rationale. All other findings are informational.
