# Plan: IB Quarterly Contract Fetch via conId

## Overview

**Feature**: Expand SMT backtest history from ~45 days to ~6.5 months by fetching specific quarterly futures contracts (MNQM6/MESM6) via IB conId with explicit `endDateTime` pagination, bypassing the ContFuture API restriction.
**Type**: Enhancement
**Complexity**: ⚠️ Medium
**Status**: 📋 Planned
**Date**: 2026-04-01

---

## Execution Agent Rules

- Make ALL code changes required by the plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`

---

## Feature Description

`prepare_futures.py` currently fetches MNQ/MES data using IB's `ContFuture` contract type, which is capped at ~45 calendar days because IB rejects explicit `endDateTime` for ContFuture (error 10339). Only `endDateTime=''` (most recent N days) is accepted.

This plan adds a `contract_type="future_by_conid"` path to `IBGatewaySource.fetch()` that uses a specific quarterly contract (identified by IB conId) with explicit `endDateTime` pagination — the same pagination pattern already used for stocks. Both MNQM6 (`conId=770561201`) and MESM6 (`conId=770561194`) were verified live on 2026-04-01 to return 5m bars with explicit `endDateTime` going back to September 24, 2025.

`prepare_futures.py` is then updated to use this new path, expanding `BACKTEST_START` from ~45 days ago to `2025-09-24`, giving ~6.5 months of data and ~37 expected trades — enough for 2–3 meaningful walk-forward folds.

## User Story

As a strategy researcher running SMT walk-forward optimization,
I want 6+ months of 5m futures bar history instead of 45 days,
So that the optimizer sees enough trades (37+) to produce statistically meaningful fold results instead of a single fold with 8 trades.

## Problem Statement

With only 8 trades in the training window, any autoresearch optimizer will find parameters that fit those 8 trades exactly and fail on the next 8. Statistical significance requires 30+ trades per fold minimum. The root cause is IB's ContFuture restriction, not a configuration issue.

## Solution Statement

Fetch the active quarterly contract (MNQM6/MESM6, conId known) with paginated `reqHistoricalData` calls using explicit `endDateTime`. This is the same pattern as the existing `stock` path in `IBGatewaySource.fetch()` — only the contract type differs. `BACKTEST_START` is set to `2025-09-24`, the earliest date with reliable bar data in both instruments (confirmed live).

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: ⚠️ Medium
**Primary Systems Affected**: `data/sources.py`, `prepare_futures.py`
**Dependencies**: `ib_insync` (already installed), IB Gateway running at `localhost:4002`
**Breaking Changes**: No — ContFuture path unchanged; new path is opt-in via `contract_type`

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `data/sources.py` (lines 154–277) — `IBGatewaySource.fetch()`: add new `elif contract_type == "future_by_conid"` branch mirroring the `"stock"` pagination pattern (lines 227–246) but using `Contract(conId=...)` and `useRTH=False`
- `data/sources.py` (lines 227–246) — Existing stock pagination pattern to mirror exactly
- `data/sources.py` (lines 196–225) — Existing `"contfuture"` branch: do NOT modify, it must remain unchanged
- `prepare_futures.py` (lines 30–50) — `TICKERS`, `BACKTEST_START`, `BACKTEST_END` constants to replace
- `prepare_futures.py` (lines 60–77) — `process_ticker()`: update to pass conId and new `contract_type`
- `tests/test_data_sources.py` (lines 160–188) — `test_ibgateway_contfuture_uses_contfuture_with_empty_enddatetime`: pattern to mirror for new unit test
- `tests/test_data_sources.py` (lines 229–312) — Integration test pattern and `ib_src` fixture to reuse

### New Files to Create

- None — all changes are in existing files

### Files to Modify

- `data/sources.py` — add `future_by_conid` branch to `IBGatewaySource.fetch()`
- `prepare_futures.py` — update constants and `process_ticker()` to use conId path
- `tests/test_data_sources.py` — add unit tests + integration tests for new branch

### Verified Research (live IB Gateway, 2026-04-01)

**Confirmed conIds:**
| Contract | conId | Exchange | Expiry | Data from |
|----------|-------|----------|--------|-----------|
| MNQM6 (Micro E-mini NASDAQ Jun 2026) | `770561201` | CME | 2026-06-18 | 2025-06-25 |
| MESM6 (Micro E-mini S&P Jun 2026)    | `770561194` | CME | 2026-06-18 | 2025-06-27 |

**Data availability confirmed:**
| Window | Both instruments |
|--------|-----------------|
| Before June 25, 2025 | No data (contract not yet listed) |
| June 25 – Aug 31, 2025 | Thin (newly listed, not front-month) |
| Sep 1 – Sep 23, 2025 | **Gap — 0 bars** (avoid) |
| Sep 24, 2025 – today | Good data ✅ use `BACKTEST_START = "2025-09-24"` |

**Test query that confirmed explicit endDateTime works:**
```python
contract = Contract(conId=770561201, exchange="CME")
bars = ib.reqHistoricalData(
    contract,
    endDateTime="20251001 16:00:00",
    durationStr="5 D",
    barSizeSetting="5 mins",
    whatToShow="TRADES",
    useRTH=False,
    formatDate=2,
)  # returned 862 bars ✅
```

**conId rollover:** MNQM6/MESM6 expire 2026-06-18. After that, update to MNQU6 (`793356225`) / MESU6 (`793356217`).

### Patterns to Follow

**New branch pattern** — mirror the stock pagination block (lines 227–246) with two differences:
1. Replace `Stock(ticker, "SMART", "USD")` with `Contract(conId=int(ticker), exchange="CME")`
2. Change `useRTH=True` to `useRTH=False` (futures trade outside regular hours)

**`ticker` parameter reuse for conId** — the caller passes the conId as the `ticker` string argument (e.g. `source.fetch("770561201", ...)`). This follows the same pattern as `contract_type="contfuture"` which passes the symbol string as `ticker`.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────────────────┐
│ WAVE 1: Source Changes (Parallel — different files)          │
├──────────────────────────────────────────────────────────────┤
│ Task 1.1: ADD future_by_conid   │ Task 1.2: UPDATE           │
│           branch in sources.py  │           prepare_futures  │
│ Agent: data-engineer            │ Agent: data-engineer       │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 2: Tests (After Wave 1)                                 │
├──────────────────────────────────────────────────────────────┤
│ Task 2.1: ADD unit + integration tests in test_data_sources  │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 3: Validation (Sequential)                              │
├──────────────────────────────────────────────────────────────┤
│ Task 3.1: Run full test suite (automated)                    │
│ Task 3.2: MANUAL — delete parquets + run prepare_futures.py  │
│ Task 3.3: MANUAL — run train_smt.py, verify ≥2 folds        │
└──────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Parallel**: Tasks 1.1 and 1.2 touch separate files (`data/sources.py` vs `prepare_futures.py`)
**Wave 2 — Sequential after Wave 1**: Tests reference both changed files
**Wave 3 — Sequential**: Validation gate then manual steps

### Interface Contracts

**Task 1.1 provides** → `IBGatewaySource.fetch(conid_str, start, end, interval, contract_type="future_by_conid")` returns standard OHLCV DataFrame
**Task 1.2 consumes** → calls `source.fetch(CONIDS[ticker], ..., contract_type="future_by_conid")`

---

## IMPLEMENTATION PLAN

### Wave 1: Source Code Changes

#### Task 1.1: ADD `future_by_conid` branch to `IBGatewaySource.fetch()` in `data/sources.py`

**File**: `data/sources.py`
**Lines to modify**: After the `"contfuture"` branch (line ~225), before the `"stock"` branch (line ~227), add new `elif` clause.

**Exact implementation** — add this block immediately after the `if not all_bars: return None` that closes the `contfuture` branch and before the `else:` that opens the stock branch:

```python
            elif contract_type == "future_by_conid":
                # Fetch a specific futures contract by conId with explicit endDateTime
                # pagination. Avoids error 10339 that blocks ContFuture from using
                # explicit endDateTime — only works for non-ContFuture specific contracts.
                # `ticker` carries the conId string; exchange is always CME for MNQ/MES.
                from ib_insync import Contract as _IBContract
                contract = _IBContract(conId=int(ticker), exchange="CME")
                ib.qualifyContracts(contract)
                chunk_end = end_dt
                while chunk_end > start_dt:
                    chunk_start = max(start_dt, chunk_end - pd.Timedelta(days=chunk_days))
                    duration_days = max(1, (chunk_end - chunk_start).days)
                    bars = ib.reqHistoricalData(
                        contract,
                        endDateTime=chunk_end.strftime("%Y%m%d %H:%M:%S"),
                        durationStr=f"{duration_days} D",
                        barSizeSetting=bar_size,
                        whatToShow="TRADES",
                        useRTH=False,
                        formatDate=2,
                    )
                    if bars:
                        all_bars.extend(bars)
                    chunk_end = chunk_start
```

**Also update** the `IBGatewaySource.fetch()` docstring (or inline comment) to document the new `contract_type` value:
- Add `"future_by_conid"` to the list of accepted `contract_type` values with a one-line description: "specific CME quarterly future identified by conId (ticker arg = conId string)"

**Validate**: `uv run python -c "from data.sources import IBGatewaySource; print('import OK')`

---

#### Task 1.2: UPDATE `prepare_futures.py` to use conId-based fetch

**File**: `prepare_futures.py`

**Changes required:**

1. **Replace `BACKTEST_START` and `BACKTEST_END`** (lines ~37–39):
```python
# Specific quarterly contracts allow explicit endDateTime — no 45-day cap.
# BACKTEST_START must be >= 2025-09-24 (earliest reliable bar in MNQM6/MESM6).
BACKTEST_START = "2025-09-24"
_TODAY         = datetime.date.today()
BACKTEST_END   = _TODAY.isoformat()
```

2. **Add `CONIDS` dict** immediately after `TICKERS`:
```python
# IB conIds for the active quarterly contracts.
# MNQM6/MESM6 expire 2026-06-18 — update to MNQU6/MESU6 (793356225/793356217) after rollover.
CONIDS = {
    "MNQ": "770561201",   # MNQM6 (Jun 2026)
    "MES": "770561194",   # MESM6 (Jun 2026)
}
```

3. **Update `process_ticker()`** — change the `source.fetch(...)` call to use the conId and new contract type:
```python
    df = source.fetch(
        CONIDS[ticker],
        HISTORY_START,
        BACKTEST_END,
        INTERVAL,
        contract_type="future_by_conid",
    )
```

4. **Update the module docstring** at the top of the file:
   - Remove the sentence about IB ContFuture `endDateTime=''` restriction and the 90-day cap
   - Replace with: "Uses specific quarterly contracts (MNQM6/MESM6) identified by IB conId with explicit `endDateTime` pagination, giving ~6.5 months of 5m history (vs 45 days for ContFuture)."

**Validate**: `uv run python -c "import prepare_futures; print('BACKTEST_START:', prepare_futures.BACKTEST_START)"` — should print `2025-09-24`

---

### Wave 2: Tests

#### Task 2.1: ADD unit and integration tests for `future_by_conid` in `tests/test_data_sources.py`

**File**: `tests/test_data_sources.py`
**Append after** the existing `test_ibgateway_stock_contract_unchanged` test (line ~227).

**Unit test A — `future_by_conid` uses Contract with conId, not Stock or ContFuture:**
```python
def test_ibgateway_future_by_conid_uses_contract_not_stock():
    """contract_type='future_by_conid' must use Contract(conId=...) not Stock or ContFuture."""
    mock_ib = mock.MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalData.return_value = []
    mock_ib.qualifyContracts.return_value = None

    mock_contract_cls = mock.MagicMock()

    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Stock") as mock_stock_cls, \
         mock.patch("ib_insync.ContFuture") as mock_cf_cls, \
         mock.patch("ib_insync.Contract", mock_contract_cls):
        src = IBGatewaySource()
        src.fetch("770561201", "2025-09-24", "2025-10-01", interval="5m",
                  contract_type="future_by_conid")

    # Contract(conId=770561201, exchange="CME") must be used
    mock_contract_cls.assert_called_once_with(conId=770561201, exchange="CME")
    mock_stock_cls.assert_not_called()
    mock_cf_cls.assert_not_called()
```

**Unit test B — `future_by_conid` calls reqHistoricalData with explicit endDateTime (not `""`):**
```python
def test_ibgateway_future_by_conid_uses_explicit_enddatetime():
    """future_by_conid must NOT use endDateTime='' — it must pass an explicit datetime string."""
    mock_ib = mock.MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalData.return_value = []
    mock_ib.qualifyContracts.return_value = None

    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Contract"):
        src = IBGatewaySource()
        src.fetch("770561201", "2025-09-24", "2025-10-01", interval="5m",
                  contract_type="future_by_conid")

    assert mock_ib.reqHistoricalData.called
    for call in mock_ib.reqHistoricalData.call_args_list:
        end_dt = call.kwargs.get("endDateTime", call.args[1] if len(call.args) > 1 else "")
        assert end_dt != "", "future_by_conid must not use endDateTime='' (ContFuture pattern)"
        assert len(end_dt) >= 8, f"Expected a real datetime string, got {end_dt!r}"
```

**Unit test C — `future_by_conid` paginates across chunk boundaries:**
```python
def test_ibgateway_future_by_conid_paginates_multiple_chunks():
    """A request spanning >60 days must call reqHistoricalData more than once."""
    mock_ib = mock.MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalData.return_value = []
    mock_ib.qualifyContracts.return_value = None

    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Contract"):
        src = IBGatewaySource()
        # 6.5-month window; chunk_days for 5m = 60 → at least 4 calls
        src.fetch("770561201", "2025-09-24", "2026-04-01", interval="5m",
                  contract_type="future_by_conid")

    assert mock_ib.reqHistoricalData.call_count >= 4, (
        f"Expected ≥4 pagination calls for 6.5-month window, "
        f"got {mock_ib.reqHistoricalData.call_count}"
    )
```

**Unit test D — `future_by_conid` returns None on connect failure:**
```python
def test_ibgateway_future_by_conid_returns_none_on_exception():
    """Connection failure during future_by_conid fetch returns None without raising."""
    mock_ib = mock.MagicMock()
    mock_ib.connect.side_effect = ConnectionRefusedError("refused")
    mock_ib.isConnected.return_value = False

    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.Contract"):
        src = IBGatewaySource()
        result = src.fetch("770561201", "2025-09-24", "2026-04-01", interval="5m",
                           contract_type="future_by_conid")

    assert result is None
```

**Unit test E — existing `contfuture` path still uses `endDateTime=''` after refactor:**
```python
def test_ibgateway_contfuture_path_unchanged_after_refactor():
    """Regression: contfuture path must still use endDateTime='' after adding future_by_conid."""
    mock_ib = mock.MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalData.return_value = []
    mock_ib.qualifyContracts.return_value = None

    with mock.patch("ib_insync.IB", return_value=mock_ib), \
         mock.patch("ib_insync.ContFuture"):
        src = IBGatewaySource()
        src.fetch("MNQ", "2026-03-01", "2026-03-31", interval="5m",
                  contract_type="contfuture")

    assert mock_ib.reqHistoricalData.call_count == 1
    call_kwargs = mock_ib.reqHistoricalData.call_args.kwargs
    assert call_kwargs.get("endDateTime") == "", "contfuture path must still use endDateTime=''"
```

**Integration test — live fetch from MNQM6 using conId:**

Add to the integration section (after `test_ibgateway_source_fetch_unsupported_interval_returns_none`):

```python
@pytest.mark.integration
def test_ibgateway_future_by_conid_live_fetch(ib_src):
    """Live fetch from MNQM6 via conId returns 5m bars for a 5-day window.

    Uses conId=770561201 (MNQM6, Jun 2026), confirmed 2026-04-01.
    Skipped automatically if IB-Gateway is not reachable (ib_src fixture handles this).
    """
    df = ib_src.fetch(
        "770561201",
        "2025-10-01",
        "2025-10-08",
        interval="5m",
        contract_type="future_by_conid",
    )
    assert df is not None, "Expected 5m bars from MNQM6 for Oct 2025"
    assert set(df.columns) == {"Open", "High", "Low", "Close", "Volume"}
    assert len(df) >= 50, f"Expected ≥50 bars for 5 trading days at 5m, got {len(df)}"
    assert df.index.tzinfo is not None
    assert df.index.is_unique
    assert df.index.is_monotonic_increasing
    # All bars should fall in the requested window
    assert df.index.min() >= pd.Timestamp("2025-10-01").tz_localize("America/New_York")
    assert df.index.max() < pd.Timestamp("2025-10-08").tz_localize("America/New_York")
```

---

### Wave 3: Validation

#### Task 3.1: Run full test suite (automated)

```bash
uv run python -m pytest tests/ -x -q
```

Expected: **360+ passed, 2 skipped** (360 original + 5 new unit tests). Integration test skipped unless IB is running.

If `test_ibgateway_contfuture_path_unchanged_after_refactor` fails, the Wave 1 code change broke the contfuture branch — investigate and fix before proceeding.

---

#### Task 3.2: MANUAL — Delete cached parquets and re-download

**Requires**: IB Gateway running on `localhost:4002`

```bash
rm ~/.cache/autoresearch/futures_data/5m/MNQ.parquet
rm ~/.cache/autoresearch/futures_data/5m/MES.parquet
uv run prepare_futures.py
```

**Expected output** (example):
```
Downloading futures data to ~/.cache/autoresearch/futures_data
  MNQ: saved 14000+ bars to ...5m/MNQ.parquet
  MES: saved 14000+ bars to ...5m/MES.parquet
Manifest written to ...futures_manifest.json
Done.
```

**Verify parquet coverage:**
```python
import pandas as pd
mnq = pd.read_parquet("~/.cache/autoresearch/futures_data/5m/MNQ.parquet")
print("MNQ first bar:", mnq.index.min())
print("MNQ last bar:", mnq.index.max())
print("MNQ total bars:", len(mnq))
```

Expected: `first bar ≥ 2025-09-24`, `total bars ≥ 10000`

**If download fails**: Check IB Gateway is connected to live market data, conIds are still valid. Run `uv run python -c "from ib_insync import IB, Contract; ib=IB(); ib.connect('127.0.0.1',4002,99); c=Contract(conId=770561201,exchange='CME'); ib.qualifyContracts(c); print(c); ib.disconnect()"` to verify conId.

---

#### Task 3.3: MANUAL — Run train_smt.py, verify ≥2 folds and ≥20 trades

```bash
uv run python train_smt.py
```

**Expected output** (with ~6.5 months of data):
- Multiple `fold1_*`, `fold2_*`, ... lines (≥ 2 folds)
- `fold1_train_total_trades` > 5 across at least one fold
- `min_test_pnl_folds_included` ≥ 2
- No Python exceptions

**If only 1 fold runs**: The short-window guard in `_compute_fold_params` may still be triggering. Check total business days: `len(pd.bdate_range(BACKTEST_START, TRAIN_END))` should be ≥ 130 for full 6-fold mode.

**If 0 total trades**: Verify the parquets contain NY open kill-zone bars (09:00–10:30 ET), and that `BACKTEST_START` in the manifest matches `2025-09-24`. Delete parquets and re-run `prepare_futures.py` if the manifest is stale.

---

## TESTING STRATEGY

| What | Tool | File | Status |
|------|------|------|--------|
| `future_by_conid` uses Contract(conId=...) | pytest | `tests/test_data_sources.py` | ✅ Automated |
| `future_by_conid` uses explicit endDateTime | pytest | `tests/test_data_sources.py` | ✅ Automated |
| `future_by_conid` paginates across chunks | pytest | `tests/test_data_sources.py` | ✅ Automated |
| `future_by_conid` returns None on exception | pytest | `tests/test_data_sources.py` | ✅ Automated |
| `contfuture` path regression (endDateTime='') | pytest | `tests/test_data_sources.py` | ✅ Automated |
| Live 5m fetch from MNQM6 via conId | pytest integration | `tests/test_data_sources.py` | ✅ Automated (skipped if IB offline) |
| Full parquet download covers Sep 24, 2025 | shell + pandas | Manual step 3.2 | ⚠️ Manual — requires live IB connection; automated unit tests mock IB |
| train_smt.py produces ≥2 folds with data | shell | Manual step 3.3 | ⚠️ Manual — requires the downloaded parquets from step 3.2 |

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest, mocked IB) | 5 | 71% |
| ✅ Integration (pytest, live IB, auto-skipped) | 1 | 14% |
| ⚠️ Manual (live IB + data validation) | 2 | 15% |
| **Total** | 8 | 100% |

**Manual justification**: Steps 3.2 and 3.3 require a live IB Gateway connection and real market data to write to disk — the end-to-end data pipeline cannot be meaningfully mocked at this level (the value is verifying IB returns the expected ~14,000 bars for the 6.5-month window). All code-logic paths in `IBGatewaySource.fetch()` are covered by mocked unit tests.

---

## VALIDATION COMMANDS

### Level 1: Syntax check

```bash
uv run python -c "from data.sources import IBGatewaySource; import prepare_futures; print('OK')"
```

### Level 2: Unit tests

```bash
uv run python -m pytest tests/test_data_sources.py -x -q
```

Expected: all tests pass (unit pass; integration auto-skipped if IB offline)

### Level 3: Full test suite regression

```bash
uv run python -m pytest tests/ -x -q
```

Expected: 360+ passed, 2 skipped

### Level 4: Manual data pipeline

```bash
# Requires IB Gateway at localhost:4002
rm ~/.cache/autoresearch/futures_data/5m/MNQ.parquet
rm ~/.cache/autoresearch/futures_data/5m/MES.parquet
uv run prepare_futures.py
uv run python train_smt.py
```

Expected: ≥10,000 bars downloaded, ≥2 walk-forward folds, ≥20 total trades.

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `IBGatewaySource.fetch(..., contract_type="future_by_conid")` uses `Contract(conId=int(ticker), exchange="CME")` and paginates with explicit `endDateTime` (not `""`)
- [ ] The `"contfuture"` path is unchanged — still uses `ContFuture` + `endDateTime=''` (regression)
- [ ] `prepare_futures.py` defines `CONIDS = {"MNQ": "770561201", "MES": "770561194"}` and `BACKTEST_START = "2025-09-24"`
- [ ] `process_ticker()` passes `CONIDS[ticker]` as the ticker arg and `contract_type="future_by_conid"`

### Error Handling
- [ ] `IBGatewaySource.fetch(..., contract_type="future_by_conid")` returns `None` on connection failure without raising

### Validation — Automated
- [ ] 5 new unit tests pass — verified by: `uv run python -m pytest tests/test_data_sources.py -x -q`
- [ ] Integration test passes when IB live; auto-skipped when offline
- [ ] Full test suite: 360+ passed, 2 skipped, 0 failures — verified by: `uv run python -m pytest tests/ -x -q`

### Validation — Manual
- [ ] `uv run prepare_futures.py` downloads MNQ/MES with first bar ≥ 2025-09-24 and ≥ 10,000 bars (requires IB Gateway)
- [ ] `uv run python train_smt.py` completes without error, ≥ 2 folds, ≥ 20 total trades (requires downloaded data)

### Out of Scope
- `train_smt.py` strategy logic, `tests/conftest.py`, `tests/test_smt_backtest.py` — no changes
- conId rollover to MNQU6/MESU6
- Data before Sep 24, 2025

---

## COMPLETION CHECKLIST

- [ ] Task 1.1: `future_by_conid` branch added to `IBGatewaySource.fetch()` in `data/sources.py`
- [ ] Task 1.2: `prepare_futures.py` updated — `CONIDS`, `BACKTEST_START = "2025-09-24"`, `process_ticker()` updated
- [ ] Task 2.1: 5 unit tests + 1 integration test added to `tests/test_data_sources.py`
- [ ] Task 3.1: Full test suite passes
- [ ] Task 3.2: Parquets deleted and re-downloaded via `prepare_futures.py` (manual, requires IB)
- [ ] Task 3.3: `train_smt.py` confirms ≥2 folds (manual, requires downloaded data)
- [ ] No debug logs added during execution
- [ ] All changes UNSTAGED — no `git add`, no `git commit`

---

## NOTES

**Why conId instead of symbol+expiry**: IB's contract qualification API (`qualifyContracts`) only returns currently active (non-expired) futures contracts. Quarterly expiries before the current date cannot be re-qualified — they show as "Error 200: No security definition found." Using the conId directly bypasses qualification and works for both active and recently expired contracts. This was verified live.

**Why Sep 24, 2025 as BACKTEST_START**: Both MNQM6 and MESM6 return 0 bars for endDateTime requests in the Sep 1–23, 2025 window (likely thin volume as the contract was not yet near the front-month). Sep 24 is the first date with reliable bar counts in both instruments (confirmed live: 862 MNQ bars, 712 MES bars for the 5-day window ending Oct 1, 2025).

**conId rollover plan**: When MNQM6/MESM6 expire on 2026-06-18, update `CONIDS` in `prepare_futures.py` to:
```python
CONIDS = {
    "MNQ": "793356225",   # MNQU6 (Sep 2026)
    "MES": "793356217",   # MESU6 (Sep 2026)
}
```
Also update `BACKTEST_START` if the new contract's data window doesn't reach back to `2025-09-24`.

**HISTORY_START vs BACKTEST_START**: In `prepare_futures.py`, `HISTORY_START = BACKTEST_START` — there is no warmup period needed since the SMT strategy uses no multi-day indicators (no SMA, no ATR). Both can be `"2025-09-24"`.
