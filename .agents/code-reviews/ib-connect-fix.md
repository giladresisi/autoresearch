# Code Review: IB Gateway Connection Resilience

**Date:** 2026-04-17
**Branch:** master
**Reviewer:** Claude Code

---

## Stats

- Files Modified: 1
- Files Added: 0
- Files Deleted: 0
- New lines: 64
- Deleted lines: 32

---

## Test Results

27 passing, 1 pre-existing failure (`test_process_managing_exit_tp` — known, noted in plan acceptance criteria). No regressions introduced.

---

## Issues Found

---

```
severity: medium
file: signal_smt.py
line: 553–572
issue: Retry loop silently exhausts all 10 attempts with no final error log
detail: When all MAX_RETRIES attempts fail (e.g., IB Gateway is permanently down), the
        loop exits without any message indicating that the process is giving up. The last
        iteration prints "Retrying in 15s ..." but then does not sleep (correct due to
        `if attempt < MAX_RETRIES - 1`), and the loop exits silently. An operator watching
        stdout will not see a "giving up" indicator.
suggestion: After the for loop, check if _ib is None or not connected and print a terminal
        failure message:
        
        if not (_ib and _ib.isConnected()):
            print(f"[FATAL] Failed to connect after {MAX_RETRIES} attempts. Exiting.", flush=True)
```

---

```
severity: medium
file: signal_smt.py
line: 233
issue: Gap-fill lookback decision uses mnq_df.empty but applies a shared start_ts to MES
detail: `gap_days` and `start_ts` are derived from `mnq_df` state only. If MNQ parquet
        is non-empty but MES parquet is empty (e.g., first run after adding MES to an
        existing MNQ-only setup, or partial cache deletion), MES will be gap-filled with
        only the last 3 days instead of 30 days, leaving MES critically under-filled for
        signal detection.
suggestion: Compute the start timestamp per instrument. For MES, check `mes_df.empty`
        independently to decide whether to use MAX_LOOKBACK_DAYS or GAP_FILL_MAX_DAYS,
        and derive a separate `mes_start_ts`. Pass per-instrument start strings to the
        respective `source.fetch()` calls. Alternatively, document the assumption that
        both parquets are always either both empty or both non-empty (and enforce it by
        deleting both on a reset).
```

---

```
severity: low
file: signal_smt.py
line: 490–511
issue: _setup_ib_subscriptions() returns None but callers cannot cancel subscriptions on clean shutdown
detail: The four subscription objects (mnq_1m, mes_1m, mnq_tick, mes_tick) are created
        locally inside the helper and discarded. On a clean exit (break after util.run()
        returns), the subscriptions are implicitly cancelled when the IB object is
        disconnected — that is fine. However, if a future need arises to cancel
        individual subscriptions (e.g., selective teardown), the references are lost.
        Not a runtime bug today, but a mild design debt.
suggestion: Return the four subscription objects as a tuple so callers retain references
        if needed. No behavior change required now; this is a forward-compatibility note.
```

---

## Pre-existing Issues (not introduced by this changeset)

- `import datetime` at line 12 is unused. Pre-existing before this diff.
- `test_process_managing_exit_tp` fails (pre-existing, unrelated to connection resilience).

---

## Summary

The implementation correctly addresses all three root causes identified in the plan. The retry loop logic is sound: `KeyboardInterrupt` is not a subclass of `Exception` so Ctrl+C exits cleanly; the final disconnect block runs after the loop in all code paths (clean exit and retry exhaustion). The gap-fill lookback cap (3 days vs 30) is applied correctly for the common case. Two medium-severity issues found, neither is a runtime crash risk today but both warrant attention before the next edge case manifests in production.
