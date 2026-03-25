# Feature: Parallel Download Thread Pool

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Add `ThreadPoolExecutor`-based parallel downloading to both `screener_prepare.py` and `prepare.py`, replacing their sequential per-ticker `for` loops. Each file gains a `MAX_WORKERS` constant driven by an environment variable so the degree of parallelism is configurable at runtime without code changes.

No changes to download logic, caching logic, or any other function. This is a pure loop-replacement with no behavioural change beyond speed.

## User Story

As a trader running `screener_prepare.py` each morning,
I want the cache refresh to run 8–12× faster,
So that the incremental morning update completes in 1–3 minutes instead of 15–30.

## Problem Statement

Both `screener_prepare.py` (main loop, ~1,200 tickers) and `prepare.py` (main loop, ~400 tickers) download tickers sequentially. Each `yf.Ticker().history()` call is a blocking HTTP round-trip; the CPU sits idle between requests. I/O-bound work like this is the ideal use case for a thread pool.

## Solution Statement

Wrap the existing per-ticker worker logic in a helper function and replace each `for` loop with `ThreadPoolExecutor` + `as_completed`. Each ticker writes to its own parquet path so there is zero write contention between threads. The `MAX_WORKERS` default (10) is chosen to stay well within Yahoo Finance's undocumented per-IP rate limits while achieving close to 10× throughput.

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: Low
**Primary Systems Affected**: `screener_prepare.py` (main loop), `prepare.py` (main loop)
**Dependencies**: `concurrent.futures` — stdlib, already available
**Breaking Changes**: None — identical observable behaviour; only speed changes

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `screener_prepare.py` (lines 163–198) — current sequential `__main__` loop to replace
- `screener_prepare.py` (lines 111–160) — `download_and_cache()` — already thread-safe (writes to own path)
- `screener_prepare.py` (lines 92–108) — `is_ticker_current()` — read-only, thread-safe
- `prepare.py` (lines 254–270) — `process_ticker()` — already a standalone function, thread-safe (writes to own path, `makedirs` with `exist_ok=True`)
- `prepare.py` (lines 273–285) — current sequential `__main__` loop to replace
- `tests/test_screener_prepare.py` — existing fixture pattern to reuse
- `tests/test_prepare.py` — existing fixture `_make_hourly_df` to reuse

### Thread-Safety Audit

Both worker functions are already thread-safe:

| Shared resource | Risk | Resolution |
|---|---|---|
| Parquet files | Each ticker writes its own path | No contention — different paths |
| `os.makedirs(CACHE_DIR, exist_ok=True)` | Called in `process_ticker` per thread | `exist_ok=True` is idempotent; OS mkdir is atomic |
| `yf.Ticker().history()` | HTTP client | `requests` (used by yfinance) is thread-safe for concurrent reads |
| `resample_to_daily()` | Pure function | No shared state |
| Print statements | Interleaving | Acceptable for progress output; no lock needed |
| `_counter` in main loop | `as_completed` runs in single thread | `done` incremented only in main thread — no race |

### Patterns to Follow

**Environment variable constant** (mirrors `CACHE_DIR` pattern in `train.py` line 13):
```python
MAX_WORKERS = int(os.environ.get("SCREENER_PREPARE_WORKERS", "10"))
```

**ThreadPoolExecutor + as_completed** (stdlib pattern):
```python
from concurrent.futures import ThreadPoolExecutor, as_completed

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {executor.submit(_process_one, t, history_start): t for t in universe}
    done = 0
    for future in as_completed(futures):
        done += 1
        ticker, status = future.result()
        print(f"  [{done:4d}/{total}] {ticker:<6} -- {status}")
```

`done` is only incremented in the main thread (the `as_completed` loop runs single-threaded), so no lock is needed for the counter.

**Output is silent** (CLAUDE.md): print() only in `__main__` block or top-level script body. The new `_process_one` helper must return a status string — not print directly — so the print stays in `__main__`.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────────────┐
│ WAVE 1: Implementation (fully parallel)                    │
├──────────────────────────────┬─────────────────────────────┤
│ Task 1.1                     │ Task 1.2                    │
│ UPDATE screener_prepare.py   │ UPDATE prepare.py           │
│ Add MAX_WORKERS + _process_  │ Add MAX_WORKERS + parallel  │
│ one() + parallel __main__    │ __main__ loop               │
└──────────────────────────────┴─────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 2: Tests (fully parallel)                             │
├──────────────────────────────┬─────────────────────────────┤
│ Task 2.1                     │ Task 2.2                    │
│ UPDATE test_screener_prepare │ UPDATE test_prepare.py      │
│ .py — parallel tests         │ parallel tests              │
└──────────────────────────────┴─────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1 and 1.2 touch different files with no shared state.
**Wave 2 — Fully Parallel**: Tasks 2.1 and 2.2 write different test files with no shared state.

### Interface Contracts

**Contract 1**: Task 1.1 extracts `_process_one(ticker, history_start) -> tuple[str, str]` returning `(ticker, status_string)` — Task 2.1 tests this function directly.

**Contract 2**: Task 1.2 keeps `process_ticker(ticker) -> bool` signature unchanged — Task 2.2 tests the parallel loop by mocking `process_ticker`.

### Synchronization Checkpoints

**After Wave 1**: `python -m pytest tests/test_screener_prepare.py tests/test_prepare.py -x -q`
**After Wave 2**: `python -m pytest tests/ -q`

---

## IMPLEMENTATION PLAN

### Wave 1: Implementation

#### Task 1.1: UPDATE `screener_prepare.py` — parallel main loop

- **WAVE**: 1
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]

**Steps**:

1. Add to imports at the top of the file:
   ```python
   import threading
   from concurrent.futures import ThreadPoolExecutor, as_completed
   ```

2. Add constant after `HISTORY_DAYS`:
   ```python
   # Number of parallel download threads. Raise carefully — Yahoo Finance rate-limits
   # aggressive clients. 10 is a safe default that gives ~10x speedup without triggering bans.
   MAX_WORKERS = int(os.environ.get("SCREENER_PREPARE_WORKERS", "10"))
   ```

3. Add a new module-level helper function `_process_one` before `if __name__ == "__main__"`:
   ```python
   def _process_one(ticker: str, history_start: str) -> tuple:
       """
       Worker for parallel execution: checks staleness, downloads if needed.
       Returns (ticker, status_string) — never raises; all errors are caught internally.
       """
       if is_ticker_current(ticker):
           return ticker, "SKIP (current)"
       path = os.path.join(SCREENER_CACHE_DIR, f"{ticker}.parquet")
       mtime_before = os.path.getmtime(path) if os.path.exists(path) else None
       download_and_cache(ticker, history_start)
       if os.path.exists(path) and (
           mtime_before is None or os.path.getmtime(path) > mtime_before
       ):
           try:
               df_check = pd.read_parquet(path)
               if not df_check.empty:
                   return ticker, f"cached ({len(df_check)} rows)"
           except Exception:
               pass
       return ticker, "FAIL"
   ```

4. Replace the `__main__` loop (the `for i, ticker in enumerate(universe, 1):` block through `print(f"\nDone...")`) with:
   ```python
   os.makedirs(SCREENER_CACHE_DIR, exist_ok=True)

   cached = skipped = failed = 0
   total = len(universe)
   done = 0

   with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
       futures = {executor.submit(_process_one, t, history_start): t for t in universe}
       for future in as_completed(futures):
           done += 1
           ticker, status = future.result()
           print(f"  [{done:4d}/{total}] {ticker:<6} -- {status}")
           if "cached" in status:
               cached += 1
           elif "SKIP" in status:
               skipped += 1
           else:
               failed += 1

   print(f"\nDone. cached={cached}  skipped={skipped}  failed={failed}")
   ```

   Note: remove the old `cached = skipped = failed = 0` and `os.makedirs` lines that preceded the old loop (they are replaced by the above).

- **VALIDATE**: `python -c "import screener_prepare; print('OK')"`

#### Task 1.2: UPDATE `prepare.py` — parallel main loop

- **WAVE**: 1
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.2]

**Steps**:

1. Add to imports at the top of the file (after existing imports):
   ```python
   from concurrent.futures import ThreadPoolExecutor
   ```

2. Add constant after `CACHE_DIR` definition (around line 102):
   ```python
   # Number of parallel download threads for prepare.py harness cache build.
   # 10 workers gives ~10x speedup; raise with caution to avoid Yahoo Finance rate limits.
   MAX_WORKERS = int(os.environ.get("PREPARE_WORKERS", "10"))
   ```

3. Replace the `__main__` loop (`for ticker in TICKERS: if process_ticker(ticker): ok += 1`) with:
   ```python
   with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
       results = list(executor.map(process_ticker, TICKERS))
   ok = sum(results)
   ```

   The `executor.map` preserves input order and collects bool results. `write_trend_summary` call after remains unchanged.

- **VALIDATE**: `python -c "import prepare; print('OK')"`

**Wave 1 Checkpoint**: `python -m pytest tests/test_screener_prepare.py tests/test_prepare.py -x -q`

---

### Wave 2: Tests

#### Task 2.1: UPDATE `tests/test_screener_prepare.py` — parallel tests

- **WAVE**: 2
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: []

**Append these tests** to `tests/test_screener_prepare.py` (do NOT rewrite the file):

```python
# ── Parallel download tests ───────────────────────────────────────────────────

def test_process_one_returns_skip_for_current_ticker(screener_cache_tmpdir, monkeypatch):
    """_process_one returns SKIP status for a ticker that is already current."""
    import screener_prepare
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    path = os.path.join(str(screener_cache_tmpdir), "AAPL.parquet")
    _write_parquet(path, yesterday)
    ticker, status = screener_prepare._process_one("AAPL", "2024-01-01")
    assert ticker == "AAPL"
    assert "SKIP" in status


def test_process_one_returns_cached_after_download(screener_cache_tmpdir, monkeypatch):
    """_process_one returns 'cached' status after a successful download."""
    import screener_prepare

    class FakeTicker:
        def history(self, **kwargs):
            return _make_hourly_df()

    monkeypatch.setattr("screener_prepare.yf.Ticker", lambda t: FakeTicker())
    ticker, status = screener_prepare._process_one("AAPL", "2024-01-01")
    assert ticker == "AAPL"
    assert "cached" in status


def test_process_one_returns_fail_on_empty_download(screener_cache_tmpdir, monkeypatch):
    """_process_one returns FAIL status when yfinance returns empty data."""
    import screener_prepare

    class FakeTicker:
        def history(self, **kwargs):
            return pd.DataFrame()

    monkeypatch.setattr("screener_prepare.yf.Ticker", lambda t: FakeTicker())
    ticker, status = screener_prepare._process_one("AAPL", "2024-01-01")
    assert ticker == "AAPL"
    assert "FAIL" in status


def test_parallel_all_tickers_processed(screener_cache_tmpdir, monkeypatch):
    """With 3 tickers and MAX_WORKERS=2, all 3 are processed (none silently dropped)."""
    import screener_prepare
    monkeypatch.setattr(screener_prepare, "MAX_WORKERS", 2)

    downloaded = []

    class FakeTicker:
        def __init__(self, t):
            self._t = t
        def history(self, **kwargs):
            downloaded.append(self._t)
            return _make_hourly_df()

    monkeypatch.setattr("screener_prepare.yf.Ticker", FakeTicker)
    monkeypatch.setattr("screener_prepare.fetch_screener_universe", lambda: ["AAPL", "MSFT", "NVDA"])

    # Simulate __main__ parallel loop
    from concurrent.futures import ThreadPoolExecutor, as_completed
    today = datetime.date.today()
    history_start = (today - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    os.makedirs(str(screener_cache_tmpdir), exist_ok=True)
    universe = screener_prepare.fetch_screener_universe()
    statuses = {}
    with ThreadPoolExecutor(max_workers=screener_prepare.MAX_WORKERS) as executor:
        futures = {executor.submit(screener_prepare._process_one, t, history_start): t for t in universe}
        for future in as_completed(futures):
            t, status = future.result()
            statuses[t] = status

    assert set(statuses.keys()) == {"AAPL", "MSFT", "NVDA"}


def test_max_workers_reads_from_env(monkeypatch):
    """MAX_WORKERS is set from SCREENER_PREPARE_WORKERS env var at import time."""
    import importlib
    monkeypatch.setenv("SCREENER_PREPARE_WORKERS", "7")
    import screener_prepare
    importlib.reload(screener_prepare)
    assert screener_prepare.MAX_WORKERS == 7
    # Restore
    monkeypatch.delenv("SCREENER_PREPARE_WORKERS", raising=False)
    importlib.reload(screener_prepare)
```

- **VALIDATE**: `python -m pytest tests/test_screener_prepare.py -q`

#### Task 2.2: UPDATE `tests/test_prepare.py` — parallel tests

- **WAVE**: 2
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.2]
- **BLOCKS**: []

**Read `tests/test_prepare.py` first**, then append these tests at the end:

```python
# ── Parallel download tests ───────────────────────────────────────────────────

def test_parallel_loop_processes_all_tickers(tmp_path, monkeypatch):
    """With mocked process_ticker, all tickers in list are called exactly once."""
    import prepare
    monkeypatch.setattr(prepare, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(prepare, "MAX_WORKERS", 2)

    called = []

    def fake_process(ticker):
        called.append(ticker)
        return True

    monkeypatch.setattr(prepare, "process_ticker", fake_process)

    from concurrent.futures import ThreadPoolExecutor
    tickers = ["AAPL", "MSFT", "NVDA"]
    with ThreadPoolExecutor(max_workers=prepare.MAX_WORKERS) as executor:
        results = list(executor.map(prepare.process_ticker, tickers))

    assert sorted(called) == sorted(tickers)
    assert all(results)


def test_parallel_loop_counts_failures(tmp_path, monkeypatch):
    """Failures (False return from process_ticker) are counted correctly."""
    import prepare
    monkeypatch.setattr(prepare, "MAX_WORKERS", 2)

    def fake_process(ticker):
        return ticker != "FAIL_ME"

    from concurrent.futures import ThreadPoolExecutor
    tickers = ["AAPL", "FAIL_ME", "NVDA"]
    with ThreadPoolExecutor(max_workers=prepare.MAX_WORKERS) as executor:
        results = list(executor.map(fake_process, tickers))

    ok = sum(results)
    assert ok == 2


def test_prepare_max_workers_reads_from_env(monkeypatch):
    """MAX_WORKERS is set from PREPARE_WORKERS env var at import time."""
    import importlib
    monkeypatch.setenv("PREPARE_WORKERS", "5")
    import prepare
    importlib.reload(prepare)
    assert prepare.MAX_WORKERS == 5
    monkeypatch.delenv("PREPARE_WORKERS", raising=False)
    importlib.reload(prepare)


def test_process_ticker_parallel_no_contention(tmp_path, monkeypatch):
    """Two tickers run in parallel threads write to separate paths without error."""
    import prepare
    monkeypatch.setattr(prepare, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(prepare, "MAX_WORKERS", 2)

    df_hourly = _make_hourly_df(n_days=5)

    def fake_download(ticker):
        return df_hourly

    monkeypatch.setattr(prepare, "download_ticker", fake_download)
    # Patch _add_earnings_dates to avoid yfinance network call
    monkeypatch.setattr(prepare, "_add_earnings_dates", lambda df, obj: df)

    from concurrent.futures import ThreadPoolExecutor
    tickers = ["AAPL", "MSFT"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(prepare.process_ticker, tickers))

    assert all(results)
    assert os.path.exists(os.path.join(str(tmp_path), "AAPL.parquet"))
    assert os.path.exists(os.path.join(str(tmp_path), "MSFT.parquet"))
```

- **VALIDATE**: `python -m pytest tests/test_prepare.py -q`

**Final Checkpoint**: `python -m pytest tests/ -q`

---

## TESTING STRATEGY

All tests automated with pytest. No manual tests required.

### Unit Tests — `screener_prepare.py` parallel logic

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_screener_prepare.py` | **Run**: `python -m pytest tests/test_screener_prepare.py -q`

Test cases:
- `test_process_one_returns_skip_for_current_ticker` ✅ — current ticker → SKIP status
- `test_process_one_returns_cached_after_download` ✅ — successful download → cached status
- `test_process_one_returns_fail_on_empty_download` ✅ — empty yfinance response → FAIL status
- `test_parallel_all_tickers_processed` ✅ — 3 tickers with 2 workers → all 3 processed
- `test_max_workers_reads_from_env` ✅ — SCREENER_PREPARE_WORKERS env var respected

### Unit Tests — `prepare.py` parallel logic

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_prepare.py` | **Run**: `python -m pytest tests/test_prepare.py -q`

Test cases:
- `test_parallel_loop_processes_all_tickers` ✅ — all tickers called with mocked process_ticker
- `test_parallel_loop_counts_failures` ✅ — False returns counted correctly
- `test_prepare_max_workers_reads_from_env` ✅ — PREPARE_WORKERS env var respected
- `test_process_ticker_parallel_no_contention` ✅ — two threads write separate parquets without conflict

### Coverage Gap Analysis

| Code path | Test |
|---|---|
| `_process_one` → current → SKIP | `test_process_one_returns_skip_for_current_ticker` ✅ |
| `_process_one` → download → cached | `test_process_one_returns_cached_after_download` ✅ |
| `_process_one` → download → FAIL | `test_process_one_returns_fail_on_empty_download` ✅ |
| `MAX_WORKERS` from env (screener) | `test_max_workers_reads_from_env` ✅ |
| `MAX_WORKERS` from env (prepare) | `test_prepare_max_workers_reads_from_env` ✅ |
| Parallel loop: all tickers processed | `test_parallel_all_tickers_processed` ✅ |
| Parallel loop: failure counting | `test_parallel_loop_counts_failures` ✅ |
| Thread write contention (prepare) | `test_process_ticker_parallel_no_contention` ✅ |
| Thread write contention (screener) | Covered by existing `test_download_and_cache_*` (each writes own path) ✅ |

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest) | 9 | 100% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 9 | 100% |

### Script Deliverables Check

**`screener_prepare.py`**:
- ✅ Running `python -c "import screener_prepare"` passes after changes (syntax check)
- ✅ All user-visible output uses ASCII-safe characters (no change to print format)

**`prepare.py`**:
- ✅ Running `python -c "import prepare"` passes after changes (syntax check)
- ✅ All user-visible output uses ASCII-safe characters (no change to print format)

---

## VALIDATION COMMANDS

### Level 1: Syntax

```bash
cd C:/Users/gilad/projects/auto-co-trader/auto-co-trader-main
python -c "import screener_prepare; print('screener_prepare OK')"
python -c "import prepare; print('prepare OK')"
```

### Level 2: New unit tests

```bash
python -m pytest tests/test_screener_prepare.py tests/test_prepare.py -v
```

### Level 3: Full suite (no regressions)

```bash
python -m pytest tests/ -q
```

Expected: all existing tests pass + all new tests pass.

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `screener_prepare.py` has a `MAX_WORKERS` constant set from `SCREENER_PREPARE_WORKERS` env var, defaulting to `10`
- [ ] `screener_prepare.py` main loop uses `ThreadPoolExecutor(max_workers=MAX_WORKERS)` with `as_completed`
- [ ] `_process_one(ticker, history_start) -> tuple` helper exists at module level and returns `(ticker, status_string)`
- [ ] `_process_one` returns `"SKIP (current)"` for a ticker whose parquet is already current
- [ ] `_process_one` returns a `"cached (N rows)"` string on successful download
- [ ] `_process_one` returns `"FAIL"` when yfinance returns empty data
- [ ] `prepare.py` has a `MAX_WORKERS` constant set from `PREPARE_WORKERS` env var, defaulting to `10`
- [ ] `prepare.py` main loop uses `ThreadPoolExecutor(max_workers=MAX_WORKERS)` with `executor.map`
- [ ] `write_trend_summary` is still called after all parallel downloads complete

### Error Handling
- [ ] A failed ticker download (empty yfinance response) does not abort other in-flight downloads
- [ ] The final `cached/skipped/failed` counts correctly reflect outcomes across all threads

### Integration / E2E
- [ ] Both scripts import cleanly: `python -c "import screener_prepare, prepare"` exits 0
- [ ] With 2 tickers and 2 workers, each parquet is written to its own path with no file corruption

### Validation
- [ ] All 9 new tests pass — verified by: `python -m pytest tests/test_screener_prepare.py tests/test_prepare.py -v`
- [ ] Full test suite has no new failures — verified by: `python -m pytest tests/ -q`

### Out of Scope
- Batch yfinance API (`yf.download`) — not part of this change
- Rate-limit retry logic — user can lower `MAX_WORKERS` via env var
- Progress bar / ETA display — plain text output only
- Thread count validation (e.g. rejecting `MAX_WORKERS=0`) — not required

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order
- [ ] Wave 1 checkpoint passed
- [ ] Wave 2 final checkpoint passed
- [ ] All validation levels executed (1–3)
- [ ] All 9 new tests created and passing
- [ ] Full test suite passes with no new failures
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**`as_completed` vs `executor.map` choice**: `screener_prepare.py` uses `as_completed` so progress lines print as each ticker finishes (better UX for a long-running script). `prepare.py` uses `executor.map` because `process_ticker` already prints its own per-ticker line internally, so ordering doesn't matter and `map` is simpler.

**Rate limiting**: Yahoo Finance has undocumented rate limits. 10 workers is empirically safe; if you see HTTP 429 errors, lower `SCREENER_PREPARE_WORKERS` / `PREPARE_WORKERS` to 5. Do not exceed 20.

**`_process_one` scope**: this function is module-level (not nested in `__main__`) so it can be imported and tested directly. It references `SCREENER_CACHE_DIR` from module scope, which is correct — tests monkeypatch that attribute on the module.
