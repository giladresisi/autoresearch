# Code Review: Data Layer Abstraction

**Date:** 2026-03-29
**Plan:** `.agents/plans/data-layer-abstraction.md`
**Reviewer:** automated

---

## Stats

- Files Modified: 5 (prepare.py, train.py, pyproject.toml, tests/test_prepare.py, tests/test_v3_f.py)
- Files Added: 3 (data/__init__.py, data/sources.py, tests/test_data_sources.py)
- Files Deleted: 0
- New lines: +205 (tracked) + ~279 (new files)
- Deleted lines: -66

**Test run:** 291 passed, 15 deselected (integration), 0 failed — `uv run python -m pytest tests/ -m "not integration"`

---

## Issues Found

---

```
severity: high
file: train.py
line: 31
issue: _load_manifest() called at module import time with no fallback — raises FileNotFoundError on any environment without a pre-existing manifest.json
detail: `_manifest = _load_manifest()` executes unconditionally when the module is imported. In a CI environment, a fresh worktree, or any machine that has not yet run prepare.py, importing train.py raises FileNotFoundError and makes the entire module unusable — including running the test suite. The tests currently pass only because the developer machine has a cached manifest at the default CACHE_DIR. Any test that reimports train with a custom CACHE_DIR (test_v3_f.py lines 106–126) must manually create the manifest first, which is fragile boilerplate that will need to be repeated in every future test that monkeypatches CACHE_DIR.
suggestion: Wrap the module-level call in a try/except and fall back to safe defaults: `try: _manifest = _load_manifest() except FileNotFoundError: _manifest = {}`. BACKTEST_START/BACKTEST_END can then remain as their hard-coded defaults when the manifest is absent (matching pre-refactor behaviour) and emit a warning. Alternatively, lazy-load the manifest inside load_ticker_data() / load_all_ticker_data() so import never fails.
```

---

```
severity: medium
file: prepare.py
line: 285
issue: process_ticker() calls yf.Ticker(ticker) directly regardless of which DataSource is active — leaking a yfinance dependency even when PREPARE_SOURCE="ib"
detail: After fetching data via `source.fetch(...)` (which may be IBGatewaySource), the function immediately calls `yf.Ticker(ticker)` to add earnings dates. When `source` is IBGatewaySource, this creates a second, unrelated yfinance network request for every ticker, partly defeating the purpose of the IB source abstraction. More importantly, it means the IB path still requires a working Yahoo Finance connection and the `yfinance` package at runtime.
suggestion: Either (a) move earnings-date enrichment into a separate optional step only triggered when `PREPARE_SOURCE == "yfinance"` / when the source is a YFinanceSource instance, or (b) expose a `get_earnings_dates(ticker)` method on the DataSource protocol so each source can provide its own implementation. At minimum, guard the call: `if isinstance(source, YFinanceSource): df_daily = _add_earnings_dates(df_daily, yf.Ticker(ticker))`.
```

---

```
severity: medium
file: data/sources.py
line: 141
issue: IBGatewaySource.fetch() uses useRTH=True, which returns only regular trading hours bars — inconsistent with the yfinance path (prepost=False) and missing extended hours data needed for accurate open/close derivation
detail: useRTH=True restricts IB historical bars to regular trading hours only. The yfinance path uses prepost=False, which is the equivalent restriction, so the intent is consistent. However, the comment at line 142 says `formatDate=2  # returns datetime objects` — with useRTH=True, IB returns only RTH bars and the 9:30 bar will exist, so resample_to_daily() will find a price_1030am. This is actually correct for the strategy's needs. However, the docstring on DataSource says the function returns "OHLCV bars" without specifying RTH vs full-session — if a caller ever switches to useRTH=False expecting the same schema, price_1030am derivation will silently differ. The inconsistency is a latent confusion risk.
suggestion: Add a brief comment at the useRTH=True line explaining the rationale: "RTH only — required so resample_to_daily() can find the 9:30 AM bar for price_1030am". Update the DataSource protocol docstring to state "Regular trading hours bars only" as a contract requirement.
```

---

```
severity: medium
file: data/sources.py
line: 169–170
issue: tz_localize("America/New_York") called on start_dt and end_dt which were created from plain strings — will raise TypeError if start/end strings are already tz-aware or if pandas infers a tz from them
detail: `pd.Timestamp(start)` from a plain "YYYY-MM-DD" string produces a tz-naive Timestamp. Then line 169 calls `start_dt.tz_localize("America/New_York")`, which is correct. However, if a caller ever passes a tz-aware ISO string (e.g. "2025-01-01T00:00:00+00:00"), `start_dt` will already be tz-aware and `.tz_localize()` will raise `TypeError: Already tz-aware, use tz_convert to convert`. The trim step (lines 169–170) is applied after the index is already converted to America/New_York (line 164), so a simpler and robust approach is to just use `pd.Timestamp(start).tz_localize("UTC").tz_convert("America/New_York")` or compare with tz-naive dates.
suggestion: Defend against tz-aware inputs: `start_tz = pd.Timestamp(start).tz_localize(None).tz_localize("America/New_York")` (force naive first, then localize), or narrow the contract in the docstring to "start/end must be plain YYYY-MM-DD strings" and add a ValueError guard.
```

---

```
severity: low
file: tests/test_prepare.py
line: 325–336
issue: test_parallel_loop_processes_all_tickers patches prepare.process_ticker but the executor.map call invokes the patched fake_process(ticker) — fake_process only accepts one argument but the real process_ticker now has signature (ticker, source, interval). The test is internally consistent (patched version is what gets called) but tests the old 1-arg signature, not the new 3-arg production signature.
detail: After the refactor, the production main block calls `executor.map(lambda t: process_ticker(t, source, PREPARE_INTERVAL), TICKERS)`. The test instead calls `executor.map(prepare.process_ticker, tickers)` — which maps a single ticker argument. Because the test monkeypatches `prepare.process_ticker` with `fake_process(ticker)`, the test passes, but it is testing the pre-refactor calling convention. If a future test author looks at this test to understand how process_ticker is called, they'll infer the old single-argument signature.
suggestion: Update the test to call the executor with a lambda matching the actual production pattern: `executor.map(lambda t: prepare.process_ticker(t, mock_source, PREPARE_INTERVAL), tickers)`, using a mock source. This makes the test mirror the actual invocation and validate the new 3-arg signature.
```

---

```
severity: low
file: data/sources.py
line: 54
issue: Column rename uses str.capitalize() which only uppercases the first character — silently drops multi-word columns like "Stock Splits" or "Dividends" that yfinance sometimes returns
detail: `df.rename(columns={c: c.capitalize() for c in df.columns})` — `capitalize()` lowercases all characters after the first, e.g. "OPEN" → "Open" (correct), "stock splits" → "Stock splits" (not in the target set). The subsequent `df[["Open", "High", "Low", "Close", "Volume"]]` slice would raise KeyError if any of the five expected columns are absent after the rename. The current yfinance output format consistently uses title-case so this works in practice, but it's fragile.
suggestion: Use a fixed rename mapping for the five expected OHLCV columns rather than a dynamic capitalize: `rename_map = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}; df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})`. This is explicit and immune to column naming variations.
```

---

## Summary

The abstraction is well-structured — the protocol pattern, pagination logic, and manifest writing are all clean. The two medium-severity issues (yf.Ticker leak in IBGatewaySource path; tz_localize on potentially tz-aware timestamps) represent correctness gaps that will surface when the IB source is first used in production. The high-severity module-level manifest load is the most likely to cause friction for collaborators and CI environments. The remaining two are low-risk improvements.
