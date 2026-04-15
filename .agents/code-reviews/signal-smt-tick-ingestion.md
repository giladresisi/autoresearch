# Code Review: signal-smt-tick-ingestion

**Date:** 2026-04-15
**Plan:** `.agents/plans/signal-smt-tick-ingestion.md`
**Reviewer:** Claude Sonnet 4.6
**Scope:** `signal_smt.py` (new file), `tests/test_signal_smt.py` (new file)

---

## Stats

- Files Modified: 0
- Files Added: 2
- Files Deleted: 0
- New lines: ~619 (signal_smt.py ~525 + 6 new tests + 1 updated test in test_signal_smt.py)
- Deleted lines: 0

---

## Pre-existing Failures

`test_30_day_cap_on_gap_fill` — fails with `AttributeError: module 'data' has no attribute 'sources'` due to incorrect `mock.patch` target path (`data.sources` vs. `data.sources` submodule not yet imported at patch time). Confirmed pre-existing by `git stash` + test run. **Not introduced by this changeset.**

---

## Issues Found

---

```
severity: medium
file: C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main\signal_smt.py
line: 261-301 (on_mnq_1m_bar and on_mes_1m_bar)
issue: Tick accumulators not reset when 1m bar clears the 1s buffer
detail: on_mnq_1m_bar resets _mnq_1s_buf to an empty DataFrame but does not reset
_mnq_tick_bar to None. The same omission exists in on_mes_1m_bar for _mes_tick_bar.

Scenario: a tick arrives at second 09:04:59 and is held in _mnq_tick_bar. At
09:05:00.0, the 1m bar callback fires and clears _mnq_1s_buf. At 09:05:00.1, the
first tick of the new second arrives; _update_tick_accumulator sees a second-boundary
crossing, finalizes the 09:04:59 accumulator, and appends that bar to the freshly-
cleared _mnq_1s_buf. The result is that the fresh buffer's first entry is a 1s bar
from the previous minute — a minute whose data is already fully captured in the 1m
parquet. When _process_scanning builds:

    combined_mnq = pd.concat([_mnq_1m_df, _mnq_1s_buf])

the 09:04:59 second is represented twice: once inside the 09:04 1m bar and once as a
standalone 1s entry. This corrupts the bar sequence passed to screen_session and can
cause detect_smt_divergence to receive bars with a repeated timestamp at minute
boundaries.

suggestion: In on_mnq_1m_bar, add `global _mnq_tick_bar` and set `_mnq_tick_bar = None`
immediately after `_mnq_1s_buf = _empty_bar_df()`. Apply the same fix to on_mes_1m_bar
for _mes_tick_bar.
```

---

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main\signal_smt.py
line: 315, 332
issue: No guard against naive tick.time from IB — crash path is unhandled
detail: Both on_mes_tick and on_mnq_tick call:
    pd.Timestamp(t.time).tz_convert("America/New_York")
If t.time is a tz-naive datetime (which IB can deliver under some configurations or
during market open/close edge cases), pd.Timestamp(naive).tz_convert() raises a
TypeError. This exception propagates to ib_insync's event loop and terminates the
tick subscription silently — the handler stops firing without any visible error in the
main loop.

The plan notes tick.time "comes as UTC datetimes" and this is the normal case.
However, IB has been observed to send naive datetimes on reconnect or for the first
tick after subscription. A single crash kills the data pipeline for the session.

suggestion: Replace the bare tz_convert call with a safe wrapper:
    ts = pd.Timestamp(t.time)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    second_ts = ts.tz_convert("America/New_York").floor("s")
This mirrors the defensive pattern already used in _bar_timestamp (line 200-202).
```

---

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main\tests\test_signal_smt.py
line: 539-560 (test_tick_accumulator_ohlcv_correct)
issue: Test monkeypatches signal_smt._process but does not reset _mnq_tick_bar after the test
detail: The test sets _mnq_tick_bar=None via monkeypatch (correctly restored on teardown),
but the timestamp suffix construction `f"{ts_base}.{int(price % 1000):03d}000"` is
fragile: for price=19998.0, int(19998.0 % 1000) = 998, producing
"2025-01-02 14:06:00.998000". For prices where modulo produces 0 (e.g., 20000.0),
the suffix is ".000000" — still in the same second, still correct. However this
numeric construction is obscure. If a future test adds a price >= 1000 (e.g., SPX),
the format string would produce a sub-second offset > 999ms
(e.g., int(1200.0 % 1000) = 200, still fine), but it is not at all obvious to a reader
why price values are being used to generate timestamps. The three ticks all share the
same base timestamp second, so the sub-second precision is irrelevant — using fixed
offsets (.100000, .200000, .300000) would be clearer.
suggestion: Replace with explicit fixed sub-second offsets:
    for i, price in enumerate((20001.0, 20005.0, 19998.0)):
        ts = f"2025-01-02 14:06:00.{(i+1)*100:06d}"
        signal_smt.on_mnq_tick(_make_mock_ticker(ts, price=price))
This is a readability concern, not a functional bug — the current test passes correctly.
```

---

## Summary

All 27 tests introduced or modified by this changeset pass. The `_SyntheticBar` class, `_update_tick_accumulator`, `_acc_to_df_row`, `on_mnq_tick`, and `on_mes_tick` implementations are logically correct and match the plan spec. Timezone handling in `_acc_to_df_row` is consistent with the `_empty_bar_df` schema. The `_bar_timestamp` path for `_SyntheticBar` works correctly with ET-localized `pd.Timestamp` inputs.

The one medium-severity issue (tick accumulator not reset on 1m boundary) is a real data-integrity bug that manifests at every minute transition during live trading: the last second of each minute gets appended to the next minute's 1s buffer, producing a duplicate entry in the combined DataFrame fed to `screen_session`. This should be fixed before deploying to live.

The low-severity naive-timestamp guard is a robustness concern for edge cases on reconnect. The test readability issue is minor and does not affect correctness.
