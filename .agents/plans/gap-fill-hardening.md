# Gap-fill hardening (5 changes) — ⚠️ Medium

Implements the five recommendations from the 2026-07-19 weekend gap-fill session.

**Baseline** (2026-07-19, master @ 1d711e9): full suite 737 passed; `tests/test_smt_decouple_active.py`
16 errors PRE-EXISTING (patches `trend.load_daily` which no longer exists) — not caused by this work.

## C4 (first — others depend on it): holiday-aware calendar `data/trading_calendar.py`
New module: `EARLY_CLOSES_ET = {2026-05-25: 13, 2026-06-19: 13, 2026-07-03: 13}` (observed closes),
`is_market_closed(ts)`, `prev_trading_close(ts)`.
Consumers: `_count_expected_1m_bars` (kill false "79% coverage" WARN), `_gap_is_expected`
(holiday early-close gap = expected), C1 clamp, C3 gap classification.
Tests:
- T4.a `is_market_closed`: open weekday minute F; daily break T; weekend T; Jun 19 14:00 T; ordinary Fri 14:00 F.
- T4.b `prev_trading_close`: open ts passthrough; Sunday noon → Fri 17:00; Wed 17:30 → Wed 17:00;
  Sat after Jul 3 early close → Fri 13:00; Jun 19 15:00 → 13:00.
- T4.c `_count_expected_1m_bars` over Jun 19 12:00→16:00 == 60 (was 240).
- T4.d `_gap_is_expected(Jun 19 12:59:59, 3180 min)` True; same gap on non-holiday Friday (Jun 12) False.

## C1: weekend-aware convergence in `gap_hours_by_file`
Clamp `now` to `prev_trading_close(now)`; add optional `now=` param (default `pd.Timestamp.now`).
Fixes: offline weekend loop never terminating; Sunday-pre-open orchestrator start.
Tests:
- T1.a file last bar Fri 16:59:59, now=Sunday noon → gap < 1h (was ~44h).
- T1.b file last bar Tue 10:00, now=Tue 12:00 → gap == 2h (unchanged behavior when open).

## C2: no cursor-advance on pacing exhaustion in `_gap_fill_1s_ib`
When a chunk's empty result coincides with error 162 and retries are exhausted: end the
instrument's round (`active=False`, `all_filled=False`), cursor kept. Skip-advance only on
genuine no-data (no 162). Prevents permanent interior holes (root cause of May 24 / Jun 29 holes).
Tests:
- T2.a FakeIB always 162+[] → all reqHistoricalData endDateTimes identical (no advance), returns False.

## C5: 1m fill before 1s fill in `IbRealtimeSource.gap_fill`
Order: load → `gap_fill_1m_ib` → reload → `_gap_fill_1s_ib`. 1m data (what signals need) no
longer competes with a 1s-exhausted pacing budget.
Tests:
- T5.a update `test_source_gap_fill_runs_1s_then_1m` → `..._1m_then_1s`, calls == [load, 1m, load, 1s].
- T5.b existing degrade test (1s False → no raise, 1m ran) stays green.

## C3: interior 1s gap repair in offline `gap_fill.gap_fill_until_now`
New in gap_fill.py: `find_interior_1s_gaps(df)` (gaps >120s containing ≥1 open-market minute via
trading_calendar) + `repair_interior_1s_gaps(bar_data_dir)` (fetch via
`parquet_maintenance._fetch_gap_chunked`, merge, atomic write). Called after
`run_gap_fill_with_retries` in `gap_fill_until_now` — offline path only (prod startup budget untouched).
Tests:
- T3.a find: fabricated Tue 10:00→10:30 hole detected; maintenance-break-only file → [].
- T3.b repair merges mocked fetched bars into parquet (atomic write, rows increase).
- T3.c `gap_fill_until_now` invokes repair after the retry loop (monkeypatched).
- T3.d clean files → repair makes no IB connection.

## Validation
1. Each change: RED → GREEN per above.
2. `pytest tests/test_ib_realtime.py tests/test_gap_fill.py` fully green.
3. Full suite: no regressions vs baseline (737 passed, known pre-existing errors only).
