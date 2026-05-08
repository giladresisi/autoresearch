# Feature: 1s Bar Accumulation and Persistence with Databento + IB Gap-Fill

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Accumulate 1-second OHLCV bars for MNQ and MES from the IB realtime tick feed and persist them to `MNQ_1s.parquet` / `MES_1s.parquet`. During the live session, bars are written to a separate session parquet (`MNQ_1s_session_YYYYMMDD.parquet`) to avoid creating a permanent internal gap in the main parquet. At session end, `merge_session_1s_parquets()` merges the session file back into the main parquet. On orchestrator startup, a pre-session Databento backfill fills as much 1s history as Databento can serve (up to the current moment, no artificial cutoff). When the strategy connects, `_gap_fill_1s_ib()` uses `reqHistoricalData barSizeSetting="1 secs"` to fill the remaining gap between Databento's last available bar and now. A one-time seed script downloads all 1s data from 2026-05-01.

**Two additional items added to this plan:**
- **Task 0 (comment):** Add `# Run as: python -m orchestrator.main` as the first line of `orchestrator/main.py` so humans and agents know not to run it as a plain script (relative imports break).
- **Task 3.5 (pre-session IB):** Move IB connection from session-start (09:20 ET) to orchestrator startup. A lightweight `_start_pre_session_ib()` function creates an `IbRealtimeSource` in a daemon thread immediately after `_pre_session_init()`, so the 3-day historical seed fills the gap at boot time and 1m bars accumulate through pre-market. The thread is stopped 30 seconds before 09:20; the session subprocess creates its own fresh IB connection as before.

**Gap-fill coverage for 1m vs 1s:**
- **1m**: Databento fills up to 2 days ago; IB's `keepUpToDate=True` seed fills the last 3 days when the strategy connects — no gap.
- **1s**: Databento fills up to whatever it can serve (typically a few hours to 1 day lag); `_gap_fill_1s_ib()` fills the remaining hours via IB `reqHistoricalData` with 1800s-chunk pagination — no gap.

## User Story

As a quant developer
I want 1s OHLCV bars persisted and fully gap-filled from Databento and IB on each startup
So that regression tests can replay near-realtime tick resolution rather than relying on 1m bars

## Problem Statement

The current pipeline finalizes 1s bars from MNQ ticks but discards them — only 1m bars are persisted. MES has no 1s accumulator at all. Without persistent 1s data, regression tests can only replay at 1m resolution, which is too coarse to test signal timing.

## Solution Statement

Add a 1s pending buffer to `IbRealtimeSource` for both MNQ and MES. Finalize MES 1s bars using a new `_mes_tick_bar` accumulator mirroring the existing MNQ one. During the live session, flush 1m-boundary batches to `MNQ_1s_session_YYYYMMDD.parquet` / `MES_1s_session_YYYYMMDD.parquet` (not the main parquet) — this avoids creating a permanent 2-minute gap between the gap-fill endpoint and the first real-time tick. Add `merge_session_1s_parquets()` to `data/databento_backfill.py` and call it from the orchestrator after each session (merges session file into main) and at orchestrator startup (crash recovery for any leftover session files). Add `backfill_1s_parquets()` to `data/databento_backfill.py` (fetches up to `now` from Databento, no artificial cutoff) and call it from `_pre_session_init()`. Add `_gap_fill_1s_ib()` to `IbRealtimeSource` to fill the remaining gap via IB historical 1s fetch. A `scripts/seed_1s_parquet.py` script performs the one-time download from 2026-05-01.

## Feature Metadata

**Feature Type**: New Capability
**Complexity**: Medium
**Primary Systems Affected**: `data/ib_realtime.py`, `data/databento_backfill.py`, `orchestrator/main.py`
**Dependencies**: `databento>=0.74.0` (in pyproject.toml — install with `uv sync`), `ib_insync` (existing), existing `DatabentSource`
**Breaking Changes**: No — additive only

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `data/ib_realtime.py` (lines 1–320) — Full file; understand existing `_mnq_tick_bar`, `_update_tick_accumulator`, `_on_mnq_tick`, `_on_mnq_1m_bar`, `_load_parquets`
- `data/databento_backfill.py` (lines 1–57) — Mirror `backfill_parquets()` for `backfill_1s_parquets()`, but with no cutoff (end=now); uses `MNQ.v.0`/`MES.v.0` ticker format
- `data/ib_realtime.py` (lines 65–105) — `_gap_fill()` method: exact pattern to mirror for `_gap_fill_1s_ib()` (separate IB connection, `client_id + 1`, pagination, concat+dedup+write)
- `data/sources.py` (lines 161–232) — `DatabentSource.fetch()`; only `"1m"` and `"5m"` supported; extend to include `"1s"` with `schema="ohlcv-1s"`
- `orchestrator/main.py` (lines 62–88) — `_pre_session_init()` pattern; add 1s backfill call after the 1m backfill call
- `tests/test_databento_backfill.py` (lines 1–119) — Existing test patterns; add `backfill_1s_parquets` tests here
- `tests/test_ib_realtime.py` (lines 1–262) — Existing test patterns; `_make_source` helper, tick builder

### New Files to Create

- `scripts/seed_1s_parquet.py` — One-time Databento seed for MNQ/MES from 2026-05-01
- `tests/test_sources.py` — Unit tests for `DatabentSource` 1s
- `tests/test_seed_1s_parquet.py` — Tests for seed script

### Already Implemented (do NOT re-implement)

- `data/databento_backfill.py` `backfill_parquets()` — 1m Databento backfill ✅
- `data/ib_realtime.py` `IbGatewayDisconnectedError` + `_on_gateway_disconnect` ✅
- `orchestrator/main.py` `_pre_session_init()` calling `backfill_parquets()` ✅
- `tests/test_databento_backfill.py` 8 tests for `backfill_parquets()` ✅

### Patterns to Follow

**Ticker symbols**: `MNQ.v.0` / `MES.v.0` (matching `databento_backfill.py`) with `stype_in="continuous"`
**Naming**: `_mnq_1s_df` / `_mes_1s_df` mirror `_mnq_1m_df` / `_mes_1m_df`; `MNQ_1s.parquet` / `MES_1s.parquet`
**Batch writes**: `sort_index()` then `[~index.duplicated(keep="last")]` before `.to_parquet()` — same as 1m
**`backfill_1s_parquets()`**: No `ib_cutoff_days` cutoff — `end=now.tz_convert("UTC")` so Databento returns as much as it has
**`_gap_fill_1s_ib()`**: Mirror `_gap_fill()` pattern — separate IB connection (`client_id + 1`), paginate in 1800s chunks, end at `now - 2 min`; skip if `_mnq_1s_df` is empty (seed script must run first)

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────┐
│ WAVE 0: Housekeeping (independent)               │
├──────────────────────────────────────────────────┤
│ Task 0: ADD comment to orchestrator/main.py      │
└──────────────────────────────────────────────────┘
                       ↓ (parallel with Wave 1)
┌──────────────────────────────────────────────────┐
│ WAVE 1: DatabentSource 1s extension              │
├──────────────────────────────────────────────────┤
│ Task 1.1: UPDATE data/sources.py                 │
└──────────────────────────────────────────────────┘
                       ↓
┌──────────────────────────────────────────────────┐
│ WAVE 2: IbRealtimeSource accumulation (parallel) │
├──────────────────────────────────────────────────┤
│ Task 2.1: init + _load_parquets                  │
│ Task 2.2: _on_mes_tick (MES tick accumulator)    │
│ Task 2.3: _on_mnq_tick (MNQ 1s buffer)           │
└──────────────────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────────────┐
│ WAVE 3: Flush + gap-fill + backfill + session merge (parallel)  │
├─────────────────────────────────────────────────────────────────┤
│ Task 3.1: _on_mnq_1m_bar + _on_mes_1m_bar → session parquet    │
│ Task 3.2: _gap_fill_1s_ib() + start() wiring                   │
│ Task 3.3: backfill_1s_parquets + orchestrator wiring            │
│ Task 3.4: merge_session_1s_parquets() in databento_backfill.py  │
│ Task 3.5: pre-session IB accumulator in orchestrator/main.py    │
└─────────────────────────────────────────────────────────────────┘
                       ↓
┌──────────────────────────────────────────────────┐
│ WAVE 4: Scripts + Tests (parallel)               │
├──────────────────────────────────────────────────┤
│ Task 4.1: scripts/seed_1s_parquet.py             │
│ Task 4.2: tests/test_sources.py                  │
│ Task 4.3: tests/test_ib_realtime.py additions    │
│ Task 4.4: tests/test_databento_backfill.py adds  │
│ Task 4.5: tests/test_seed_1s_parquet.py          │
└──────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 0 — Independent**: One-line comment; can run in parallel with Wave 1
**Wave 1 — Sequential**: Single file edit
**Wave 2 — Parallel**: Tasks 2.1, 2.2, 2.3 all edit `data/ib_realtime.py`; safe in a single ordered pass by one agent
**Wave 3 — Parallel**: Tasks 3.1, 3.2, 3.3 — 3.1 and 3.2 both edit `data/ib_realtime.py` (one pass), 3.3 and 3.5 both touch `orchestrator/main.py` (ordered pass; 3.5 depends on 3.3 for `bar_data_dir` refactor)
**Wave 4 — Fully Parallel**: 6 independent file creations/edits

---

## IMPLEMENTATION PLAN

### Phase 0: Housekeeping

#### Task 0: ADD run-as-module comment to `orchestrator/main.py`

**Purpose**: Prevent the `ModuleNotFoundError: No module named 'orchestrator'` that occurs when the file is run as `python orchestrator/main.py`. Running it directly breaks relative imports because Python doesn't add the project root to `sys.path`. The module invocation (`python -m orchestrator.main`) does.

**Change**: Add as the very first line of `orchestrator/main.py` (before the existing comment on line 1):

```python
# Run as: python -m orchestrator.main
```

The file currently starts with:
```
# orchestrator/main.py
# Daemon entry point: ...
```

After the change it starts with:
```
# Run as: python -m orchestrator.main
# orchestrator/main.py
# Daemon entry point: ...
```

**Validation**: `head -1 orchestrator/main.py` → outputs `# Run as: python -m orchestrator.main`

---

### Phase 1: Extend DatabentSource for 1s interval

#### Task 1.1: UPDATE `data/sources.py` — DatabentSource 1s support

**Purpose**: Allow `DatabentSource.fetch(..., interval="1s")` to fetch `ohlcv-1s` schema.

**Steps**:
1. Change the supported interval guard on line 186 from `if interval not in ("1m", "5m"):` to `if interval not in ("1m", "5m", "1s"):`.
2. Before calling `client.timeseries.get_range(...)`, compute `schema = "ohlcv-1s" if interval == "1s" else "ohlcv-1m"` and pass `schema=schema` instead of the hardcoded `schema="ohlcv-1m"`.
3. The existing `if interval == "5m":` resample block correctly skips for `"1s"` — no change needed.
4. UTC→ET conversion and column rename apply identically — no change needed.

**Validation**: `python -c "from data.sources import DatabentSource; print('ok')"`

### Phase 2: IbRealtimeSource 1s accumulation state

All three tasks in this phase edit `data/ib_realtime.py`. Execute them in order as a single-agent pass.

#### Task 2.1: UPDATE init + `_load_parquets`

**Purpose**: Add 1s dataframes, pending buffers, and MES tick accumulator to instance state.

**In `__init__`**, after `self._mnq_tick_bar = None`, add:
```python
self._mes_tick_bar        = None
self._mnq_1s_df           = self._empty_bar_df()   # historical, loaded from MNQ_1s.parquet
self._mes_1s_df           = self._empty_bar_df()   # historical, loaded from MES_1s.parquet
self._mnq_1s_pending: list[dict] = []               # tick bars buffered until next 1m boundary
self._mes_1s_pending: list[dict] = []
self._session_date        = pd.Timestamp.now(tz="America/New_York").strftime("%Y%m%d")
self._mnq_1s_session_df   = self._empty_bar_df()   # session bars (NOT written to main parquet)
self._mes_1s_session_df   = self._empty_bar_df()   # session bars (NOT written to main parquet)
```

**Add properties** after the existing `mes_1m_df` property:
```python
@property
def mnq_1s_df(self) -> pd.DataFrame:
    return self._mnq_1s_df

@property
def mes_1s_df(self) -> pd.DataFrame:
    return self._mes_1s_df
```

**In `_load_parquets()`**, add after the existing 1m loads:
```python
mnq_1s_path = self._bar_data_dir / "MNQ_1s.parquet"
mes_1s_path  = self._bar_data_dir / "MES_1s.parquet"
self._mnq_1s_df = pd.read_parquet(mnq_1s_path) if mnq_1s_path.exists() else self._empty_bar_df()
self._mes_1s_df = pd.read_parquet(mes_1s_path)  if mes_1s_path.exists()  else self._empty_bar_df()
```

**Validation**: `python -c "from data.ib_realtime import IbRealtimeSource; print('ok')"`

#### Task 2.2: UPDATE `_on_mes_tick` — MES 1s accumulator

**Purpose**: MES currently only maintains a partial 1m accumulator. Add `_mes_tick_bar` (identical mechanism to MNQ's `_mnq_tick_bar`) and buffer finalized bars.

**Replace** `_on_mes_tick` (current line 211–217) with:
```python
def _on_mes_tick(self, ticker) -> None:
    if not ticker.tickByTicks:
        return
    t = ticker.tickByTicks[-1]
    second_ts = self._tick_second_ts(t)
    minute_ts = second_ts.floor("min")
    self._mes_tick_bar, mes_finalized = self._update_tick_accumulator(
        self._mes_tick_bar, t.price, t.size, second_ts
    )
    if mes_finalized is not None:
        self._mes_1s_pending.append(mes_finalized)
    self._mes_partial_1m = self._update_partial_1m(
        self._mes_partial_1m, t.price, t.size, minute_ts
    )
```

**Validation**: `pytest tests/test_ib_realtime.py -x -q`

#### Task 2.3: UPDATE `_on_mnq_tick` — buffer finalized MNQ 1s bar

**Purpose**: When a MNQ 1s bar finalizes, also append it to `_mnq_1s_pending`.

**In `_on_mnq_tick`**, inside the `if finalized is not None and self._mnq_partial_1m is not None:` block (current line 228–230), add after the `self._on_bar(...)` call:
```python
self._mnq_1s_pending.append(finalized)
```

**Validation**: `pytest tests/test_ib_realtime.py -x -q`

### Phase 3: Batch flush + IB gap-fill + pre-session backfill

#### Task 3.1: UPDATE `_on_mnq_1m_bar` + `_on_mes_1m_bar` — flush 1s pending to session parquet

**Purpose**: Write accumulated 1s bars once per minute. Bars go to `MNQ_1s_session_YYYYMMDD.parquet` (NOT the main parquet) to avoid creating a permanent internal gap. The session parquet is merged into the main parquet at session end by `merge_session_1s_parquets()`.

**In `_on_mnq_1m_bar`**, after the `self._mnq_1m_df.to_parquet(...)` call (line 186) and before `self._mnq_tick_bar = None`, add:
```python
if self._mnq_1s_pending:
    rows = [[p["open"], p["high"], p["low"], p["close"], p["volume"]]
            for p in self._mnq_1s_pending]
    ts_list = [p["second_ts"] for p in self._mnq_1s_pending]
    new_1s = pd.DataFrame(
        rows, columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex(ts_list),
    )
    self._mnq_1s_session_df = pd.concat([self._mnq_1s_session_df, new_1s]).sort_index()
    self._mnq_1s_session_df = self._mnq_1s_session_df[
        ~self._mnq_1s_session_df.index.duplicated(keep="last")
    ]
    session_path = self._bar_data_dir / f"MNQ_1s_session_{self._session_date}.parquet"
    self._mnq_1s_session_df.to_parquet(session_path)
    self._mnq_1s_pending.clear()
self._mes_tick_bar = None  # reset alongside _mnq_tick_bar (same minute boundary)
```

**In `_on_mes_1m_bar`**, after `self._mes_1m_df.to_parquet(...)` (line 207), add the symmetric MES flush:
```python
if self._mes_1s_pending:
    rows = [[p["open"], p["high"], p["low"], p["close"], p["volume"]]
            for p in self._mes_1s_pending]
    ts_list = [p["second_ts"] for p in self._mes_1s_pending]
    new_1s = pd.DataFrame(
        rows, columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex(ts_list),
    )
    self._mes_1s_session_df = pd.concat([self._mes_1s_session_df, new_1s]).sort_index()
    self._mes_1s_session_df = self._mes_1s_session_df[
        ~self._mes_1s_session_df.index.duplicated(keep="last")
    ]
    session_path = self._bar_data_dir / f"MES_1s_session_{self._session_date}.parquet"
    self._mes_1s_session_df.to_parquet(session_path)
    self._mes_1s_pending.clear()
```

**Validation**: `pytest tests/test_ib_realtime.py -x -q`

#### Task 3.2: ADD `_gap_fill_1s_ib()` to `IbRealtimeSource` and wire into `start()`

**Purpose**: Fill the gap between Databento's last available bar and the current moment using IB's `reqHistoricalData` with `barSizeSetting="1 secs"`. Mirrors the existing `_gap_fill()` method (now unused but still present) — same separate-connection pattern with `client_id + 1`.

**Module-level constant** (add near top of file with other constants, after imports):
```python
_IB_1S_CHUNK_SECONDS = 1800   # IB max duration per reqHistoricalData call for 1s bars
```

**New method** (add after `_gap_fill`):
```python
def _gap_fill_1s_ib(self) -> None:
    """Fill recent 1s bars from IB: covers what Databento can't serve (last few hours).

    Uses a separate IB connection (client_id + 1) so it doesn't interfere with the
    main session connection. Called in start() after _load_parquets(), before the
    main retry loop. Skips instruments with an empty parquet — run seed script first.
    """
    from ib_insync import IB, Contract as _IBContract, util as _util
    now = pd.Timestamp.now(tz="America/New_York")
    end_dt = now - pd.Timedelta(minutes=2)  # avoid requesting in-progress bars

    pairs = [
        ("MNQ", "_mnq_1s_df", "MNQ_1s.parquet", self._mnq_conid),
        ("MES", "_mes_1s_df", "MES_1s.parquet", self._mes_conid),
    ]
    # Check if any fill is needed before opening an IB connection
    needs_fill = any(
        not getattr(self, df_attr).empty and
        (end_dt - getattr(self, df_attr).index[-1]).total_seconds() > 60
        for _, df_attr, _, _ in pairs
    )
    if not needs_fill:
        return

    ib = IB()
    try:
        ib.connect(self._host, self._port, clientId=self._client_id + 1)
        for instrument, df_attr, parquet_name, conid in pairs:
            df = getattr(self, df_attr)
            if df.empty:
                print(f"[gap_fill_1s_ib] {instrument}: no seed data — skipping", flush=True)
                continue
            start_dt = df.index[-1]
            if (end_dt - start_dt).total_seconds() <= 60:
                continue
            contract = _IBContract(conId=int(conid), exchange="CME")
            all_bars: list = []
            chunk_end = end_dt
            while chunk_end > start_dt:
                chunk_start = max(start_dt, chunk_end - pd.Timedelta(seconds=_IB_1S_CHUNK_SECONDS))
                chunk_s = max(1, int((chunk_end - chunk_start).total_seconds()))
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime=chunk_end.strftime("%Y%m%d %H:%M:%S"),
                    durationStr=f"{chunk_s} S",
                    barSizeSetting="1 secs",
                    whatToShow="TRADES",
                    useRTH=False,
                    formatDate=2,
                )
                if bars:
                    all_bars.extend(bars)
                chunk_end = chunk_start
            if not all_bars:
                print(f"[gap_fill_1s_ib] {instrument}: 0 bars returned", flush=True)
                continue
            new_df = _util.df(all_bars).rename(columns={
                "date": "datetime", "open": "Open", "high": "High",
                "low": "Low", "close": "Close", "volume": "Volume",
            }).set_index("datetime")
            if new_df.index.tzinfo is None:
                new_df.index = new_df.index.tz_localize("America/New_York")
            else:
                new_df.index = new_df.index.tz_convert("America/New_York")
            combined = pd.concat([df, new_df[["Open", "High", "Low", "Close", "Volume"]]]).sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
            setattr(self, df_attr, combined)
            self._bar_data_dir.mkdir(parents=True, exist_ok=True)
            combined.to_parquet(self._bar_data_dir / parquet_name)
            print(f"[gap_fill_1s_ib] {instrument}: +{len(new_df)} 1s bars", flush=True)
    except Exception as exc:
        print(f"[gap_fill_1s_ib] error: {exc}", flush=True)
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass
```

**Wire into `start()`**: After `self._load_parquets()` and before `mnq_contract = Future(...)`, add:
```python
self._gap_fill_1s_ib()
```

**Validation**: `python -c "from data.ib_realtime import IbRealtimeSource, _IB_1S_CHUNK_SECONDS; print(_IB_1S_CHUNK_SECONDS)"`

#### Task 3.3: ADD `backfill_1s_parquets()` + wire orchestrator (backfill + crash recovery + post-session merge)

**Sub-task A — `data/databento_backfill.py`**: Add `backfill_1s_parquets()`. Unlike `backfill_parquets()` (1m), there is **no `ib_cutoff_days` cutoff** — `end=now` so Databento returns as much as it has. `_gap_fill_1s_ib()` covers whatever Databento couldn't reach.

```python
MNQ_1S_SEED_START = pd.Timestamp("2026-05-01", tz="America/New_York")


def backfill_1s_parquets(
    bar_data_dir: Path,
    max_lookback_days: int = 10,
) -> None:
    """Fetch Databento 1s bars from the last parquet bar up to now.

    No cutoff: end=now so Databento returns the latest data it has available.
    IbRealtimeSource._gap_fill_1s_ib() fills any remaining gap at session start.
    max_lookback_days is kept small (10) because 1s data is ~60x larger than 1m.
    Raises RuntimeError if DATABENTO_API_KEY is not set (via DatabentSource.__init__).
    """
    now   = pd.Timestamp.now(tz="America/New_York")
    floor = max(now - pd.Timedelta(days=max_lookback_days), MNQ_1S_SEED_START)
    bar_data_dir.mkdir(parents=True, exist_ok=True)

    source = DatabentSource()

    for ticker, fname in [(MNQ_TICKER, "MNQ_1s.parquet"), (MES_TICKER, "MES_1s.parquet")]:
        path     = bar_data_dir / fname
        existing = pd.read_parquet(path) if path.exists() else _empty_df()
        last_bar = existing.index[-1] if not existing.empty else None
        start_ts = max(last_bar + pd.Timedelta(seconds=1), floor) if last_bar is not None else floor
        df_new = source.fetch(
            ticker,
            start_ts.tz_convert("UTC").isoformat(),
            now.tz_convert("UTC").isoformat(),
            interval="1s",
        )
        if df_new is None or df_new.empty:
            continue
        combined = pd.concat([existing, df_new]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        combined.to_parquet(path)
```

**Sub-task B — `orchestrator/main.py`**: In `_pre_session_init()`, after the `backfill_parquets(bar_data_dir)` call, add:
```python
try:
    from data.databento_backfill import backfill_1s_parquets
    print("[ORCH] Running Databento 1s pre-session backfill ...", flush=True)
    backfill_1s_parquets(bar_data_dir)
    print("[ORCH] Databento 1s pre-session backfill complete", flush=True)
except Exception as exc:
    print(
        f"[ORCH] WARNING: Databento 1s backfill failed: {exc}",
        flush=True,
    )
```

**Sub-task C — `orchestrator/main.py` crash recovery**: In `_pre_session_init()`, BEFORE the `backfill_parquets(bar_data_dir)` call (so leftover session bars are in the main parquet before Databento tries to fill from the last bar), add:
```python
try:
    from data.databento_backfill import merge_session_1s_parquets
    merge_session_1s_parquets(bar_data_dir)
except Exception as exc:
    print(f"[ORCH] WARNING: session 1s merge (crash recovery) failed: {exc}", flush=True)
```

**Sub-task D — `orchestrator/main.py` post-session merge**: In `run()`, after `result = ProcessManager(...).run_session(today)` and before `_close_session_position(...)`, add:
```python
try:
    from data.databento_backfill import merge_session_1s_parquets
    merge_session_1s_parquets(bar_data_dir)
    orch_ch.writeln("[ORCH] 1s session parquets merged into main")
except Exception as exc:
    orch_ch.writeln(f"[ORCH] WARNING: 1s session merge failed: {exc}")
```

Also move `bar_data_dir` computation to the top of `run()` so it's accessible (currently it's only in `_pre_session_init()`):
```python
bar_data_dir = Path(__file__).resolve().parent.parent / "data"
```

**Validation**:
- `python -c "from data.databento_backfill import backfill_1s_parquets, merge_session_1s_parquets; print('ok')"`
- `pytest tests/test_databento_backfill.py -x -q`

#### Task 3.4: ADD `merge_session_1s_parquets()` to `data/databento_backfill.py`

**Purpose**: Merge `MNQ_1s_session_*.parquet` / `MES_1s_session_*.parquet` files into the main `MNQ_1s.parquet` / `MES_1s.parquet`, **including IB gap-fill of the ~2-minute window between the main parquet's last bar and the session parquet's first bar**. Safe no-op when no session files exist.

Called in two places:
1. After each session (in `run()`) — the gap is ~6-8 hours old, IB serves it easily.
2. At orchestrator startup (in `_pre_session_init()`, before `backfill_1s_parquets()`) — crash recovery for sessions that ended while the orchestrator was down; the gap is older but still within IB's historical range.

If no session files exist, the function returns immediately (no IB connection opened). If IB is unavailable, the merge still proceeds without gap fill and logs a warning — the gap will be closed by the next `backfill_1s_parquets()` + `_gap_fill_1s_ib()` run.

IB params are read from environment variables (`IB_HOST`, `IB_PORT`, `MNQ_CONID`, `MES_CONID`) so the orchestrator doesn't need to know about them. Client ID 16 is used — safe because `merge_session_1s_parquets()` runs before the strategy starts or after it ends, so the main strategy (client 15) and `_gap_fill_1s_ib` (client 16, inside `start()`) are never connected at the same time.

```python
def merge_session_1s_parquets(bar_data_dir: Path) -> None:
    """Merge session 1s parquets into main parquets, filling the gap via IB.

    For each instrument with a session file:
      1. IB-fill the gap: main_parquet[-1] → session_parquet[0]  (~2 min)
      2. Concat: existing + gap bars + session bars
      3. Write main parquet; delete session file

    Safe no-op if no session files exist (no IB connection opened).
    IB connection failure is non-fatal: merge proceeds without gap fill.
    IB params from env: IB_HOST, IB_PORT, MNQ_CONID, MES_CONID.
    """
    import os
    from ib_insync import IB, Contract as _IBContract, util as _util

    bar_data_dir = Path(bar_data_dir)
    _host    = os.environ.get("IB_HOST", "127.0.0.1")
    _port    = int(os.environ.get("IB_PORT", "4002"))
    _mnq_con = os.environ.get("MNQ_CONID", "770561201")
    _mes_con = os.environ.get("MES_CONID", "770561194")
    _client  = 16  # strategy not connected at merge time; no conflict

    pairs = [
        ("MNQ", "MNQ_1s.parquet", "MNQ_1s_session_*.parquet", _mnq_con),
        ("MES", "MES_1s.parquet", "MES_1s_session_*.parquet", _mes_con),
    ]

    merges_needed = [
        (inst, main, sorted(bar_data_dir.glob(glob)), conid)
        for inst, main, glob, conid in pairs
        if list(bar_data_dir.glob(glob))
    ]
    if not merges_needed:
        return

    ib = IB()
    ib_ok = False
    try:
        ib.connect(_host, _port, clientId=_client)
        ib_ok = True
    except Exception as exc:
        print(f"[merge_session_1s] IB unavailable ({exc}) — merging without gap fill", flush=True)

    try:
        for instrument, main_name, session_files, conid in merges_needed:
            main_path = bar_data_dir / main_name
            existing  = pd.read_parquet(main_path) if main_path.exists() else _empty_df()

            for session_path in session_files:
                session_df = pd.read_parquet(session_path)
                if session_df.empty:
                    session_path.unlink()
                    continue

                # IB gap fill: main[-1] → session[0]
                if ib_ok and not existing.empty:
                    gap_start = existing.index[-1]
                    gap_end   = session_df.index[0]
                    gap_s     = max(0, int((gap_end - gap_start).total_seconds()) - 1)
                    if gap_s > 1:
                        try:
                            contract = _IBContract(conId=int(conid), exchange="CME")
                            bars = ib.reqHistoricalData(
                                contract,
                                endDateTime=gap_end.strftime("%Y%m%d %H:%M:%S"),
                                durationStr=f"{gap_s} S",
                                barSizeSetting="1 secs",
                                whatToShow="TRADES",
                                useRTH=False,
                                formatDate=2,
                            )
                            if bars:
                                gap_df = _util.df(bars).rename(columns={
                                    "date": "datetime", "open": "Open", "high": "High",
                                    "low": "Low", "close": "Close", "volume": "Volume",
                                }).set_index("datetime")
                                if gap_df.index.tzinfo is None:
                                    gap_df.index = gap_df.index.tz_localize("America/New_York")
                                else:
                                    gap_df.index = gap_df.index.tz_convert("America/New_York")
                                existing = pd.concat(
                                    [existing, gap_df[["Open", "High", "Low", "Close", "Volume"]]]
                                ).sort_index()
                                existing = existing[~existing.index.duplicated(keep="last")]
                                print(f"[merge_session_1s] {instrument}: +{len(gap_df)} gap bars", flush=True)
                        except Exception as exc:
                            print(f"[merge_session_1s] {instrument}: gap fill failed ({exc})", flush=True)

                existing = pd.concat([existing, session_df]).sort_index()
                existing = existing[~existing.index.duplicated(keep="last")]

            existing.to_parquet(main_path)
            for session_path in session_files:
                if session_path.exists():
                    session_path.unlink()
            print(f"[merge_session_1s] {instrument}: merged {len(session_files)} session file(s)", flush=True)
    finally:
        try:
            if ib_ok and ib.isConnected():
                ib.disconnect()
        except Exception:
            pass
```

**Validation**: `python -c "from data.databento_backfill import merge_session_1s_parquets; print('ok')"`

#### Task 3.5: ADD pre-session IB accumulator to `orchestrator/main.py`

**Purpose**: Currently, `IbRealtimeSource.start()` is called inside `automation.main` which only launches at 09:20 ET. This means the 3-day IB historical seed (which fills the gap from the previous day) fires at session start, not at boot. Moving IB connection to orchestrator startup lets the seed fill the gap immediately, and pre-market 1m bars accumulate from then on. At 09:20, the subprocess only needs to run `run_daily` and start the strategy — no gap-fill delay.

**Design**: The orchestrator starts a lightweight `IbRealtimeSource` in a daemon thread. It uses only bar accumulation (no strategy callbacks), a dedicated `PRE_SESSION_IB_CLIENT_ID` (default 10, distinct from automation.main's 20 and merge_session's 16), and writes bars to parquet exactly as the session source does. The thread is stopped 30 seconds before 09:20 to release the IB client slot before the session subprocess connects.

**DEPENDS ON**: Task 3.3 (which refactors `bar_data_dir` into `run()` top-level scope).

**Step 1**: Add module-level constant and two new functions after `_pre_session_init()` in `orchestrator/main.py`:

```python
import threading as _threading

_PRE_SESSION_IB_STOP_EARLY_SECS = 30  # release client slot before session subprocess connects


def _start_pre_session_ib(
    bar_data_dir: Path,
) -> "tuple[object, _threading.Thread] | tuple[None, None]":
    """Start IbRealtimeSource in a daemon thread for pre-market 1m bar accumulation.

    Returns (None, None) when MNQ_CONID or MES_CONID is absent from the environment
    (signal-only / Databento-only mode — graceful degradation).
    """
    import os as _os2
    mnq_conid = _os2.environ.get("MNQ_CONID")
    mes_conid  = _os2.environ.get("MES_CONID")
    if not mnq_conid or not mes_conid:
        print(
            "[ORCH] MNQ_CONID/MES_CONID not set — skipping pre-session IB accumulator",
            flush=True,
        )
        return None, None
    from data.ib_realtime import IbRealtimeSource
    source = IbRealtimeSource(
        host=_os2.environ.get("IB_HOST", "127.0.0.1"),
        port=int(_os2.environ.get("IB_PORT", "4002")),
        client_id=int(_os2.environ.get("PRE_SESSION_IB_CLIENT_ID", "10")),
        mnq_conid=mnq_conid,
        mes_conid=mes_conid,
        bar_data_dir=bar_data_dir,
        on_bar=lambda bar, mes: None,   # accumulate only; strategy runs in session subprocess
    )
    thread = _threading.Thread(target=source.start, daemon=True, name="pre-session-ib")
    thread.start()
    print("[ORCH] Pre-session IB accumulator started (client_id="
          f"{_os2.environ.get('PRE_SESSION_IB_CLIENT_ID', '10')})", flush=True)
    return source, thread


def _stop_pre_session_ib(source, thread: "_threading.Thread | None") -> None:
    """Stop the pre-session IB accumulator and wait for its thread to exit."""
    if source is None:
        return
    source.stop()
    if thread is not None and thread.is_alive():
        thread.join(timeout=15.0)
    print("[ORCH] Pre-session IB accumulator stopped", flush=True)
```

**Step 2**: Update `run()` to start/stop the accumulator around every sleep. Replace the three `_sleep_until` branches and the post-session sleep with the pre-session-IB-wrapped versions. The complete updated `run()` body (only the `while True` loop changes; preamble and exception handler are unchanged):

```python
def run(summarizer: Summarizer | None = None, skip_summary: bool = False) -> None:
    """Main daemon loop. Ctrl+C exits cleanly; signal_smt.py is terminated if active."""
    if not skip_summary and summarizer is None:
        summarizer = Summarizer()
    _pre_session_init()
    bar_data_dir = Path(__file__).resolve().parent.parent / "data"  # (already moved by Task 3.3)
    try:
        while True:
            now   = get_et_now()
            today = now.date()

            if not is_trading_day(today):
                _pre_src, _pre_thr = _start_pre_session_ib(bar_data_dir)
                _sleep_until(next_session_open(now), "next trading session")
                _stop_pre_session_ib(_pre_src, _pre_thr)
                continue

            session_open_dt = datetime.datetime.combine(today, _SESSION_OPEN_V2).replace(tzinfo=_ET)
            grace_end_dt    = datetime.datetime.combine(today, _SESSION_CLOSE_V2).replace(tzinfo=_ET)

            if now < session_open_dt:
                _pre_src, _pre_thr = _start_pre_session_ib(bar_data_dir)
                _stop_ts = session_open_dt - datetime.timedelta(seconds=_PRE_SESSION_IB_STOP_EARLY_SECS)
                if now < _stop_ts:
                    _sleep_until(_stop_ts, "pre-session IB shutdown")
                _stop_pre_session_ib(_pre_src, _pre_thr)
                _sleep_until(session_open_dt, f"session open {_SESSION_OPEN_V2.strftime('%H:%M')} ET")
                continue

            if now >= grace_end_dt:
                _pre_src, _pre_thr = _start_pre_session_ib(bar_data_dir)
                _sleep_until(next_session_open(now), "next trading session")
                _stop_pre_session_ib(_pre_src, _pre_thr)
                continue

            # Run session (no pre-session IB during session — subprocess owns the IB connection)
            signal_ch, orch_ch = _make_session_channels(today)
            relay = SessionRelay(signal_ch)
            if LIVE_TRADING:
                signal_cmd = ["uv", "run", "python", "-m", "automation.main"]
            else:
                signal_cmd = _SIGNAL_SMT
            print(f"[orchestrator] mode={'LIVE_TRADING' if LIVE_TRADING else 'signal'}", flush=True)
            result = ProcessManager(signal_cmd, relay, orch_ch).run_session(today)
            # Post-session: fill the ~2-min gap (gap-fill end → first session tick) and merge
            # session 1s parquet into main. This runs before pre-session IB restarts so the
            # session file is cleaned up before overnight accumulation begins.
            try:
                from data.databento_backfill import merge_session_1s_parquets
                merge_session_1s_parquets(bar_data_dir)
                orch_ch.writeln("[ORCH] 1s session parquets merged into main")
            except Exception as _exc:
                orch_ch.writeln(f"[ORCH] WARNING: 1s session merge failed: {_exc}")
            _close_session_position(orch_ch)
            relay.write_trades_tsv(_SESSIONS_DIR / today.isoformat() / "trades.tsv", today)
            if summarizer is not None:
                summarizer.run(today, _SESSIONS_DIR / today.isoformat() / "signals.log", _SESSIONS_DIR, signal_ch)
            if result == "ib_disconnected":
                orch_ch.writeln(
                    "[ORCH] *** IB Gateway disconnected. Restart IB Gateway, then relaunch "
                    "the orchestrator. All positions have been closed. ***"
                )
                sys.exit(3)
            # Post-session: accumulate overnight bars while sleeping until next session
            _pre_src, _pre_thr = _start_pre_session_ib(bar_data_dir)
            _sleep_until(next_session_open(get_et_now()), "next trading session")
            _stop_pre_session_ib(_pre_src, _pre_thr)
    except KeyboardInterrupt:
        print("\n[ORCH] Shutting down.", flush=True)
        sys.exit(0)
```

**Validation**:
- `python -c "from orchestrator.main import _start_pre_session_ib, _stop_pre_session_ib, _PRE_SESSION_IB_STOP_EARLY_SECS; print(_PRE_SESSION_IB_STOP_EARLY_SECS)"`
- Expected: `30`

---

### Phase 4: Scripts and tests

#### Task 4.1: CREATE `scripts/seed_1s_parquet.py`

**Purpose**: One-time idempotent download of 1s OHLCV from Databento starting 2026-05-01. Resumes from last bar if parquets exist.

```python
#!/usr/bin/env python
"""Seed MNQ_1s.parquet and MES_1s.parquet from Databento starting 2026-05-01.

Usage:
    uv run python scripts/seed_1s_parquet.py [--dry-run]

Writes to data/ (BAR_DATA_DIR env var to override). Safe to re-run — resumes
from the last bar in each existing parquet.
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from data.sources import DatabentSource

SEED_START   = "2026-05-01"
BAR_DATA_DIR = Path(os.environ.get("BAR_DATA_DIR", "data"))
PAIRS = [
    ("MNQ", "MNQ.v.0", "MNQ_1s.parquet"),
    ("MES", "MES.v.0", "MES_1s.parquet"),
]

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = DatabentSource()
    now = pd.Timestamp.now(tz="America/New_York")
    end = now.tz_convert("UTC").isoformat()

    for instrument, ticker, parquet_name in PAIRS:
        parquet_path = BAR_DATA_DIR / parquet_name
        if parquet_path.exists():
            existing = pd.read_parquet(parquet_path)
            start = (existing.index[-1] + pd.Timedelta(seconds=1)).tz_convert("UTC").isoformat() if not existing.empty else SEED_START
        else:
            existing = None
            start = SEED_START
        print(f"[seed] {instrument}: {start[:10]} -> {end[:10]}", flush=True)
        if args.dry_run:
            print(f"[seed] DRY RUN — skipping fetch", flush=True)
            continue
        df = source.fetch(ticker, start, end, interval="1s")
        if df is None or df.empty:
            print(f"[seed] {instrument}: no data returned", flush=True)
            continue
        if existing is not None and not existing.empty:
            combined = pd.concat([existing, df]).sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
            df = combined
        BAR_DATA_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(parquet_path)
        print(f"[seed] {instrument}: {len(df)} bars -> {parquet_path}", flush=True)

if __name__ == "__main__":
    main()
```

**Validation**: `uv run python scripts/seed_1s_parquet.py --dry-run`

#### Task 4.2: CREATE `tests/test_sources.py` — DatabentSource 1s

**Purpose**: 3 unit tests for the new 1s interval support in `DatabentSource`.

Tests:
1. **`test_databent_source_1s_calls_ohlcv_1s_schema`** — patch `databento.Historical`, call `fetch("MNQ.v.0", ..., interval="1s")`, assert `schema="ohlcv-1s"` passed to `client.timeseries.get_range`.
2. **`test_databent_source_1s_returns_et_dataframe`** — mock `get_range` returning a DataFrame with UTC DatetimeIndex, assert result has `America/New_York` timezone and OHLCV columns.
3. **`test_databent_source_invalid_interval_raises`** — call `fetch(..., interval="30m")`, assert `ValueError`.

**Run**: `pytest tests/test_sources.py -v`

#### Task 4.3: ADD 9 tests to `tests/test_ib_realtime.py`

Append after the last existing test (`test_ibgateway_disconnected_error_not_retried`):

4. **`test_mes_tick_bar_finalizes_on_second_boundary`** — two MES ticks at different seconds; assert `_mes_1s_pending` has one entry with correct OHLCV.
5. **`test_mes_tick_bar_same_second_accumulates`** — two MES ticks in same second; assert `_mes_1s_pending` is empty and `_mes_tick_bar["volume"] == 2`.
6. **`test_mnq_on_tick_appends_to_1s_pending`** — cross a second boundary for MNQ (with `_mnq_partial_1m` pre-set); assert `_mnq_1s_pending` has one entry.
7. **`test_on_mnq_1m_bar_flushes_1s_pending_to_session_parquet`** — pre-populate `_mnq_1s_pending` with two bars; call `_on_mnq_1m_bar(mock_bars, True)` with mocked `strategy_smt.set_bar_data`; assert `_mnq_1s_pending` cleared, `_mnq_1s_session_df` has 2 rows, `MNQ_1s_session_*.parquet` written, `MNQ_1s.parquet` NOT written.
8. **`test_on_mes_1m_bar_flushes_1s_pending_to_session_parquet`** — symmetric for MES.
9. **`test_on_mnq_1m_bar_resets_mes_tick_bar`** — set `_mes_tick_bar` to a non-None dict; call `_on_mnq_1m_bar(mock_bars, True)`; assert `_mes_tick_bar is None`.
10. **`test_load_parquets_loads_1s_files`** — write `MNQ_1s.parquet` and `MES_1s.parquet` to `tmp_path`; call `_load_parquets()`; assert `mnq_1s_df` and `mes_1s_df` non-empty.
11. **`test_mnq_1s_df_property_returns_loaded_df`** — load a parquet, assert the property matches.
12. **`test_1s_pending_empty_does_not_write_session_parquet`** — call `_on_mnq_1m_bar(mock_bars, True)` with empty `_mnq_1s_pending`; assert no session parquet written.

**Run**: `pytest tests/test_ib_realtime.py -v`

#### Task 4.4: ADD 7 tests to `tests/test_databento_backfill.py`

Append two new classes after the existing test classes:

**Class `TestBackfill1sParquets`**:

13. **`test_backfill_1s_creates_mnq_1s_parquet`** — mock `DatabentSource`, call `backfill_1s_parquets(bar_dir)`; assert `MNQ_1s.parquet` exists.
14. **`test_backfill_1s_creates_mes_1s_parquet`** — same for `MES_1s.parquet`.
15. **`test_backfill_1s_no_cutoff_calls_with_end_near_now`** — assert `source.fetch` called with `end` timestamp within 60s of `pd.Timestamp.now(tz="UTC")` (no cutoff).
16. **`test_backfill_1s_calls_interval_1s`** — assert `source.fetch` called with `interval="1s"`.

**Class `TestMergeSession1sParquets`**:

17. **`test_merge_session_integrates_into_main`** — write `MNQ_1s.parquet` (2 rows, old) and `MNQ_1s_session_20260508.parquet` (2 rows, newer); patch `ib_insync.IB` with mock `reqHistoricalData` returning `[]`; call `merge_session_1s_parquets(bar_dir)`; assert `MNQ_1s.parquet` has 4 rows and session file deleted.
18. **`test_merge_session_noop_when_no_session_files`** — call `merge_session_1s_parquets(bar_dir)` with no session files; assert no IB connection opened and no exception.
19. **`test_merge_session_deduplicates_overlapping_rows`** — create overlapping main + session rows (same timestamps); patch `ib_insync.IB`; assert result has no duplicates.
20. **`test_merge_session_gap_fill_called_with_correct_duration`** — set main parquet last bar to T, session first bar to T+120s; patch `ib_insync.IB` mock; assert `reqHistoricalData` called with `durationStr="119 S"` and `barSizeSetting="1 secs"`.

**Run**: `pytest tests/test_databento_backfill.py -v`

#### Task 4.5: CREATE `tests/test_seed_1s_parquet.py`

17. **`test_seed_dry_run_prints_without_writing`** — patch `DatabentSource`, run `main(["--dry-run"])`; assert no parquet written.
18. **`test_seed_creates_parquets_for_both_instruments`** — patch `DatabentSource.fetch` returning a small DataFrame; patch `BAR_DATA_DIR`; run `main([])`; assert `MNQ_1s.parquet` and `MES_1s.parquet` exist.
19. **`test_seed_resumes_from_last_bar`** — write parquet with last bar at timestamp T; patch `DatabentSource.fetch`; run `main([])`; assert `fetch` called with `start` reflecting T+1s.
20. **`test_seed_script_is_runnable`** — `subprocess.run(["uv", "run", "python", "scripts/seed_1s_parquet.py", "--dry-run"])` with cwd at automation root; assert returncode 0.

**Run**: `pytest tests/test_seed_1s_parquet.py -v`

---

## STEP-BY-STEP TASKS

### WAVE 0

#### Task 0: ADD run-as-module comment to `orchestrator/main.py`
- **WAVE**: 0
- **AGENT_ROLE**: any
- **DEPENDS_ON**: []
- **BLOCKS**: []
- **PROVIDES**: First-line comment `# Run as: python -m orchestrator.main`
- **IMPLEMENT**: See Phase 0 Task 0 — prepend one comment line
- **VALIDATE**: `head -1 orchestrator/main.py` → `# Run as: python -m orchestrator.main`

**Wave 0 Checkpoint**: `head -1 orchestrator/main.py`

---

### WAVE 1

#### Task 1.1: UPDATE `data/sources.py`
- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.2, 4.2]
- **PROVIDES**: `DatabentSource.fetch(..., interval="1s")` returning ohlcv-1s DataFrame
- **IMPLEMENT**: Change interval guard; compute `schema` variable; pass to `get_range`
- **VALIDATE**: `python -c "from data.sources import DatabentSource; print('ok')"`

**Wave 1 Checkpoint**: `python -c "from data.sources import DatabentSource; print('ok')"`

---

### WAVE 2

#### Task 2.1: UPDATE `data/ib_realtime.py` — init + _load_parquets
- **WAVE**: 2
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.2, 2.3, 3.1]
- **PROVIDES**: `_mes_tick_bar`, `_mnq_1s_df`, `_mes_1s_df`, pending lists, 1s parquet loading
- **IMPLEMENT**: See Phase 2 Task 2.1
- **VALIDATE**: `python -c "from data.ib_realtime import IbRealtimeSource; print('ok')"`

#### Task 2.2: UPDATE `_on_mes_tick`
- **WAVE**: 2
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: [2.1]
- **PROVIDES**: MES 1s finalization via `_mes_tick_bar`; `_mes_1s_pending` population
- **IMPLEMENT**: See Phase 2 Task 2.2
- **VALIDATE**: `pytest tests/test_ib_realtime.py -x -q`

#### Task 2.3: UPDATE `_on_mnq_tick`
- **WAVE**: 2
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: [2.1]
- **PROVIDES**: MNQ 1s bars buffered in `_mnq_1s_pending`
- **IMPLEMENT**: See Phase 2 Task 2.3
- **VALIDATE**: `pytest tests/test_ib_realtime.py -x -q`

**Wave 2 Checkpoint**: `pytest tests/test_ib_realtime.py -x -q`

---

### WAVE 3

#### Task 3.1: UPDATE `_on_mnq_1m_bar` + `_on_mes_1m_bar`
- **WAVE**: 3
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: [2.1, 2.2, 2.3]
- **PROVIDES**: 1s parquets written on each completed 1m bar; `_mes_tick_bar` reset in `_on_mnq_1m_bar`
- **IMPLEMENT**: See Phase 3 Task 3.1
- **VALIDATE**: `pytest tests/test_ib_realtime.py -x -q`

#### Task 3.2: ADD `_gap_fill_1s_ib()` + `start()` wiring
- **WAVE**: 3
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: [2.1]
- **PROVIDES**: IB 1s historical fill covering whatever Databento couldn't serve; wired into `start()`
- **IMPLEMENT**: See Phase 3 Task 3.2 (constant `_IB_1S_CHUNK_SECONDS`, `_gap_fill_1s_ib()` method, `start()` call)
- **VALIDATE**: `python -c "from data.ib_realtime import IbRealtimeSource, _IB_1S_CHUNK_SECONDS; print(_IB_1S_CHUNK_SECONDS)"`

#### Task 3.3: ADD `backfill_1s_parquets()` + orchestrator wiring (backfill + crash recovery + post-session merge)
- **WAVE**: 3
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: [1.1, 3.4]
- **PROVIDES**: Pre-session Databento 1s backfill; crash-recovery merge at startup; post-session merge in `run()`
- **IMPLEMENT**: See Phase 3 Task 3.3 (Sub-tasks A, B, C, D)
- **VALIDATE**: `python -c "from data.databento_backfill import backfill_1s_parquets, merge_session_1s_parquets; print('ok')"`

#### Task 3.4: ADD `merge_session_1s_parquets()` to `data/databento_backfill.py`
- **WAVE**: 3
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.3]
- **PROVIDES**: `merge_session_1s_parquets(bar_data_dir)` — merges session parquets into main, safe no-op when none exist
- **IMPLEMENT**: See Phase 3 Task 3.4
- **VALIDATE**: `python -c "from data.databento_backfill import merge_session_1s_parquets; print('ok')"`

#### Task 3.5: ADD pre-session IB accumulator to `orchestrator/main.py`
- **WAVE**: 3
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: [3.3]
- **BLOCKS**: [4.6]
- **PROVIDES**: `_PRE_SESSION_IB_STOP_EARLY_SECS`, `_start_pre_session_ib()`, `_stop_pre_session_ib()`; `run()` updated to start/stop IB around all pre/post-session sleeps
- **IMPLEMENT**: See Phase 3 Task 3.5 — module constant, two functions, updated `run()` body. **NOTE**: The `run()` body in Task 3.5 is the authoritative final version and already includes the `merge_session_1s_parquets()` call from Task 3.3 Sub-task D. Apply Task 3.3 first (which introduces `bar_data_dir` in `run()` and wires backfill/merge into `_pre_session_init()`), then apply Task 3.5's full `run()` replacement last.
- **VALIDATE**: `python -c "from orchestrator.main import _start_pre_session_ib, _stop_pre_session_ib, _PRE_SESSION_IB_STOP_EARLY_SECS; print(_PRE_SESSION_IB_STOP_EARLY_SECS)"`

**Wave 3 Checkpoint**: `pytest tests/test_ib_realtime.py tests/test_databento_backfill.py tests/test_orchestrator_main.py -x -q`

---

### WAVE 4

#### Task 4.1: CREATE `scripts/seed_1s_parquet.py`
- **WAVE**: 4
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: [1.1]
- **IMPLEMENT**: See Phase 4 Task 4.1
- **VALIDATE**: `uv run python scripts/seed_1s_parquet.py --dry-run`

#### Task 4.2: CREATE `tests/test_sources.py`
- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.1]
- **IMPLEMENT**: 3 tests for DatabentSource 1s
- **VALIDATE**: `pytest tests/test_sources.py -v`

#### Task 4.3: ADD tests to `tests/test_ib_realtime.py`
- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [3.1, 3.2]
- **IMPLEMENT**: 9 accumulation tests (tests 4–12 above) + 3 `_gap_fill_1s_ib` tests (tests 21–23 below)
- **VALIDATE**: `pytest tests/test_ib_realtime.py -v`

#### Task 4.4: ADD tests to `tests/test_databento_backfill.py`
- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [3.3, 3.4]
- **IMPLEMENT**: 8 new tests — 4 for `backfill_1s_parquets` (class `TestBackfill1sParquets`) + 4 for `merge_session_1s_parquets` including gap-fill (class `TestMergeSession1sParquets`) — see Phase 4 Task 4.4 for the full test list
- **VALIDATE**: `pytest tests/test_databento_backfill.py -v`

#### Task 4.5: CREATE `tests/test_seed_1s_parquet.py`
- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [4.1]
- **IMPLEMENT**: 4 tests for seed script
- **VALIDATE**: `pytest tests/test_seed_1s_parquet.py -v`

#### Task 4.6: ADD 4 tests to `tests/test_orchestrator_main.py` — pre-session IB accumulator
- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [3.5]
- **IMPLEMENT**: Append after the last existing test in `tests/test_orchestrator_main.py`:

**Test details:**

**`test_start_pre_session_ib_creates_daemon_thread`** — patch `IbRealtimeSource` in `orchestrator.main`; set `MNQ_CONID=770561201` and `MES_CONID=770561194` in env; call `_start_pre_session_ib(tmp_path)`; assert returned source is the mock instance; assert a daemon `threading.Thread` was started; assert `source.start` was called inside the thread (use `threading.Event` or check `thread.is_alive()` after short sleep).

```python
def test_start_pre_session_ib_creates_daemon_thread(tmp_path, monkeypatch):
    import threading, time
    from orchestrator.main import _start_pre_session_ib

    monkeypatch.setenv("MNQ_CONID", "770561201")
    monkeypatch.setenv("MES_CONID", "770561194")
    started = threading.Event()

    class FakeSource:
        def start(self):
            started.set()
            time.sleep(0.05)   # simulate blocking
        def stop(self): pass

    fake = FakeSource()
    with patch("orchestrator.main.IbRealtimeSource", return_value=fake):
        # import after patch because IbRealtimeSource is imported lazily inside the function
        from orchestrator import main as _m
        src, thr = _m._start_pre_session_ib(tmp_path)

    assert src is fake
    assert thr is not None and thr.daemon
    assert started.wait(timeout=1.0), "source.start() never called in thread"
```

**`test_start_pre_session_ib_returns_none_when_conid_not_set`** — do NOT set `MNQ_CONID`/`MES_CONID`; call `_start_pre_session_ib(tmp_path)`; assert both return values are `None`; assert `IbRealtimeSource` was never instantiated.

```python
def test_start_pre_session_ib_returns_none_when_conid_not_set(tmp_path, monkeypatch):
    monkeypatch.delenv("MNQ_CONID", raising=False)
    monkeypatch.delenv("MES_CONID", raising=False)
    from orchestrator.main import _start_pre_session_ib
    src, thr = _start_pre_session_ib(tmp_path)
    assert src is None and thr is None
```

**`test_stop_pre_session_ib_calls_stop_and_join`** — create mock source and mock thread; call `_stop_pre_session_ib(source, thread)`; assert `source.stop()` called; assert `thread.join(timeout=15.0)` called.

```python
def test_stop_pre_session_ib_calls_stop_and_join(tmp_path):
    from unittest.mock import MagicMock
    from orchestrator.main import _stop_pre_session_ib

    source = MagicMock()
    thread = MagicMock()
    thread.is_alive.return_value = True
    _stop_pre_session_ib(source, thread)
    source.stop.assert_called_once()
    thread.join.assert_called_once_with(timeout=15.0)
```

**`test_stop_pre_session_ib_noop_when_source_none`** — call `_stop_pre_session_ib(None, None)`; assert no exception raised.

```python
def test_stop_pre_session_ib_noop_when_source_none():
    from orchestrator.main import _stop_pre_session_ib
    _stop_pre_session_ib(None, None)   # must not raise
```

- **VALIDATE**: `pytest tests/test_orchestrator_main.py -v -k "pre_session_ib"`

**Wave 4 Checkpoint**: `pytest tests/test_sources.py tests/test_ib_realtime.py tests/test_databento_backfill.py tests/test_seed_1s_parquet.py tests/test_orchestrator_main.py -v`

---

## TESTING STRATEGY

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Run**: `pytest tests/ -x -q`

All Databento and IB calls mocked. No live connections required.

| Test file | Tests | What |
|---|---|---|
| `tests/test_sources.py` | 3 | DatabentSource 1s schema routing |
| `tests/test_ib_realtime.py` | +12 | MES accumulator, pending buffers, session flush, `_gap_fill_1s_ib` |
| `tests/test_databento_backfill.py` | +8 | `backfill_1s_parquets()` (no cutoff, interval="1s") + `merge_session_1s_parquets()` (incl. gap fill) |
| `tests/test_seed_1s_parquet.py` | 4 | Seed script behaviour |
| `tests/test_orchestrator_main.py` | +4 | `_start_pre_session_ib` (creates thread, returns None when conids absent), `_stop_pre_session_ib` (calls stop+join, no-ops on None) |
| **Total new** | **31** | |

**`_start_pre_session_ib` / `_stop_pre_session_ib` tests** (Task 4.6 — add to `tests/test_orchestrator_main.py`):

21. **`test_start_pre_session_ib_creates_daemon_thread`** — see Task 4.6 above for full code.
22. **`test_start_pre_session_ib_returns_none_when_conid_not_set`** — see Task 4.6 above.
23. **`test_stop_pre_session_ib_calls_stop_and_join`** — see Task 4.6 above.
24. **`test_stop_pre_session_ib_noop_when_source_none`** — see Task 4.6 above.

**`_gap_fill_1s_ib` tests** (add to `tests/test_ib_realtime.py` alongside accumulation tests):

21. **`test_gap_fill_1s_ib_skips_when_empty_parquet`** — `_mnq_1s_df` is empty; patch `ib_insync.IB`; assert `IB.connect` never called (empty parquet = skip, no IB connection opened).
22. **`test_gap_fill_1s_ib_skips_when_already_current`** — `_mnq_1s_df` last bar within 60s of now; patch `ib_insync.IB`; assert no connection opened.
23. **`test_gap_fill_1s_ib_paginates_in_1800s_chunks`** — set `_mnq_1s_df` last bar to 1 hour ago; mock `IB.connect`, `IB.reqHistoricalData` returning `[]`; assert `reqHistoricalData` called with `barSizeSetting="1 secs"` and `durationStr` containing `" S"` suffix ≤ 1800.

### Manual Tests

**Status**: ⚠️ Manual — requires live Databento API key and live IB Gateway

#### Manual Test 1: Databento seed + backfill

**Why Manual**: Requires `DATABENTO_API_KEY` and live internet
**Steps**:
1. `uv run python scripts/seed_1s_parquet.py`
2. Check `data/MNQ_1s.parquet` — should have bars from 2026-05-01
3. Run orchestrator; check that `_pre_session_init()` logs "Databento 1s pre-session backfill complete"

#### Manual Test 2: IB 1s gap-fill (end-to-end)

**Why Manual**: Requires live IB Gateway on port 4002
**Steps**:
1. Ensure `MNQ_1s.parquet` exists (seed first) and last bar is > 2 minutes ago
2. Start the strategy process; observe `[gap_fill_1s_ib] MNQ: +N 1s bars` log lines
3. Verify `data/MNQ_1s.parquet` last bar is within 2 minutes of now after startup

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest) | 31 | 94% |
| ⚠️ Manual (live API/IB) | 2 | 6% |
| **Total** | 33 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Syntax check

```bash
head -1 orchestrator/main.py   # must output: # Run as: python -m orchestrator.main
python -c "from orchestrator.main import _start_pre_session_ib, _stop_pre_session_ib, _PRE_SESSION_IB_STOP_EARLY_SECS; print(_PRE_SESSION_IB_STOP_EARLY_SECS)"
python -c "from data.sources import DatabentSource; print('sources ok')"
python -c "from data.ib_realtime import IbRealtimeSource; print('ib_realtime ok')"
python -c "from data.databento_backfill import backfill_parquets, backfill_1s_parquets; print('backfill ok')"
uv run python scripts/seed_1s_parquet.py --dry-run
```

### Level 2: Unit tests

```bash
pytest tests/test_sources.py -v
pytest tests/test_ib_realtime.py -v
pytest tests/test_databento_backfill.py -v
pytest tests/test_seed_1s_parquet.py -v
pytest tests/test_orchestrator_main.py -v -k "pre_session_ib"
```

### Level 3: Full suite (regression check)

```bash
pytest tests/ -x -q
```

---

## ACCEPTANCE CRITERIA

- [ ] `orchestrator/main.py` first line is exactly `# Run as: python -m orchestrator.main`
- [ ] `_start_pre_session_ib(bar_data_dir)` creates an `IbRealtimeSource` with `client_id` from `PRE_SESSION_IB_CLIENT_ID` env var (default `"10"`) and `on_bar=lambda bar, mes: None`
- [ ] `_start_pre_session_ib` returns `(None, None)` and prints a skip message when `MNQ_CONID` or `MES_CONID` is absent from the environment (graceful degradation for signal-only / Databento-only mode)
- [ ] The IbRealtimeSource thread started by `_start_pre_session_ib` is a daemon thread (does not block process exit)
- [ ] `run()` calls `_start_pre_session_ib` before every pre-session `_sleep_until` (non-trading day, before session open, after grace end) and `_stop_pre_session_ib` after waking
- [ ] `run()` stops the pre-session IB thread at least `_PRE_SESSION_IB_STOP_EARLY_SECS` (30) seconds before session open — giving the subprocess time to connect on a fresh client slot
- [ ] `run()` starts a post-session accumulator after `run_session()` returns and before the overnight `_sleep_until(next_session_open(...))`; stops it when waking for the next session
- [ ] `DatabentSource.fetch(..., interval="1s")` fetches `ohlcv-1s` schema from Databento GLBX.MDP3
- [ ] `IbRealtimeSource` accumulates MES 1s bars via `_mes_tick_bar` (mirrors existing MNQ logic)
- [ ] `IbRealtimeSource` buffers finalized MNQ and MES 1s bars in `_mnq_1s_pending` / `_mes_1s_pending`
- [ ] 1m boundary flush writes pending bars to `MNQ_1s_session_YYYYMMDD.parquet` / `MES_1s_session_YYYYMMDD.parquet` — NOT to `MNQ_1s.parquet` / `MES_1s.parquet`
- [ ] `MNQ_1s.parquet` / `MES_1s.parquet` (main parquets) are never modified during a live session
- [ ] `_mes_tick_bar` reset in `_on_mnq_1m_bar` alongside `_mnq_tick_bar` (same boundary)
- [ ] `merge_session_1s_parquets()` fills the ~2-minute gap (main[-1] → session[0]) via IB before merging, then concats session bars into main and deletes session files
- [ ] `merge_session_1s_parquets()` is a safe no-op when no session files exist (no IB connection opened)
- [ ] `merge_session_1s_parquets()` IB failure is non-fatal: merge still proceeds, gap left for next startup's `backfill_1s_parquets()`
- [ ] Orchestrator calls `merge_session_1s_parquets()` after `run_session()` returns (post-session gap fill + merge)
- [ ] Orchestrator calls `merge_session_1s_parquets()` in `_pre_session_init()` BEFORE Databento backfill (crash recovery — any leftover session file is gap-filled and merged before Databento fill point is computed)
- [ ] After `merge_session_1s_parquets()` (session end), `MNQ_1s.parquet` has no gap at the session boundary
- [ ] `backfill_1s_parquets()` fetches `interval="1s"` from Databento up to `now` (no cutoff) and writes `*_1s.parquet`
- [ ] `orchestrator/main.py` `_pre_session_init()` calls `backfill_1s_parquets()` gracefully
- [ ] `_gap_fill_1s_ib()` fills remaining gap from last 1s bar to `now - 2 min` using IB 1800s-chunk pagination
- [ ] `start()` calls `_gap_fill_1s_ib()` before the main IB retry loop
- [ ] `_gap_fill_1s_ib()` is a no-op when parquet is empty (no IB connection opened)
- [ ] `scripts/seed_1s_parquet.py --dry-run` exits 0 without writing files
- [ ] All 31 new pytest tests pass
- [ ] No regressions: `pytest tests/ -x -q` passes

---

## COMPLETION CHECKLIST

- [ ] `orchestrator/main.py`: first line is `# Run as: python -m orchestrator.main` (Task 0)
- [ ] `orchestrator/main.py`: `_PRE_SESSION_IB_STOP_EARLY_SECS = 30`, `_start_pre_session_ib()`, `_stop_pre_session_ib()` added (Task 3.5)
- [ ] `orchestrator/main.py`: `run()` updated — pre-session IB started/stopped around every pre-session and post-session sleep (Task 3.5)
- [ ] `tests/test_orchestrator_main.py`: 4 new tests for pre-session IB (Task 4.6)
- [ ] `data/sources.py` updated: DatabentSource 1s
- [ ] `data/ib_realtime.py` updated: init (`_session_date`, `_mnq_1s_session_df`, `_mes_1s_session_df`, `_mes_tick_bar`, pending lists), `_load_parquets`, `_on_mes_tick`, `_on_mnq_tick`
- [ ] `data/ib_realtime.py` updated: `_on_mnq_1m_bar` + `_on_mes_1m_bar` flush to session parquet (NOT main parquet)
- [ ] `data/databento_backfill.py` updated: `backfill_1s_parquets()` added (no cutoff, end=now)
- [ ] `data/databento_backfill.py` updated: `merge_session_1s_parquets()` added
- [ ] `orchestrator/main.py` updated: `_pre_session_init()` calls `merge_session_1s_parquets()` (crash recovery) then `backfill_1s_parquets()`
- [ ] `orchestrator/main.py` updated: `run()` calls `merge_session_1s_parquets()` after `run_session()`
- [ ] `data/ib_realtime.py` updated: `_IB_1S_CHUNK_SECONDS` constant + `_gap_fill_1s_ib()` method + `start()` wiring
- [ ] `scripts/seed_1s_parquet.py` created and runnable
- [ ] `tests/test_sources.py` created with 3 tests
- [ ] `tests/test_ib_realtime.py` extended with 9 new tests (session parquet, accumulator, merge)
- [ ] `tests/test_databento_backfill.py` extended with 8 new tests (backfill 1s + merge with gap fill)
- [ ] `tests/test_seed_1s_parquet.py` created with 4 tests
- [ ] All validation levels 1–3 pass
- [ ] **⚠️ Debug logs added during execution REMOVED**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**Session file design — why it exists**: `_gap_fill_1s_ib()` ends at `now - 2 min`. Real-time ticks start at ~`now` (when the IB subscription fires). If we wrote session bars directly to `MNQ_1s.parquet`, there would be a permanent 2-minute internal gap (09:18 → 09:20). This gap cannot be repaired by subsequent gap-fills because next morning's `_gap_fill_1s_ib()` sees the last bar as ~16:00 (end of session), not 09:18 — so it thinks there's nothing to fill. The session file pattern avoids this: session bars accumulate in `MNQ_1s_session_YYYYMMDD.parquet` during the session; at session end `merge_session_1s_parquets()` fills the ~2-minute gap via IB, then merges session bars into the main parquet — leaving no gap.

**Data flow summary (full day cycle)**:
1. **Orchestrator startup**: crash-recovery `merge_session_1s_parquets()` (fills gap + merges any leftover session file — no-op if none) → `backfill_1s_parquets()` (Databento, up to now) → `_gap_fill_1s_ib()` in `start()` (IB, Databento's lag to now-2min) → main parquet current to ~09:18
2. **Session**: real-time bars → `MNQ_1s_session_YYYYMMDD.parquet` (09:20 onwards) — main parquet unchanged during session
3. **Session end**: `merge_session_1s_parquets()` fills 09:18→09:20 gap via IB, then concats session (09:20–16:00) → main parquet complete through 16:00 with no gaps
4. **Next morning startup**: crash-recovery merge (no-op if post-session merge already ran) → `backfill_1s_parquets()` fills from 16:00 to now → `_gap_fill_1s_ib()` closes any remaining lag to now-2min

**Complete gap-fill coverage**:
- **1m**: Databento fills up to 2 days ago; IB `keepUpToDate=True` seed fills the last 3 days on connect — no gap.
- **1s**: `merge_session_1s_parquets()` closes the session boundary gap via IB immediately. `backfill_1s_parquets()` (Databento) + `_gap_fill_1s_ib()` (IB) bring the main parquet current to now-2min at startup. Together: no gaps.

**IB 1s cost at startup**: Number of IB requests = `gap_seconds / 1800`. If Databento serves up to 2h ago, IB needs 4 requests (<10s). If Databento has T+1 day latency, IB needs ~48 requests (~2–3 minutes). This is one-time startup cost per session.

**Why `_gap_fill_1s_ib` skips empty parquets**: If the parquet is empty, we don't know a safe start timestamp, and fetching unbounded history would be expensive. The seed script must be run first (`scripts/seed_1s_parquet.py`) to establish the initial baseline from 2026-05-01.

**`_gap_fill_1s_ib` vs removed `_gap_fill()`**: The old `_gap_fill()` (for 1m) was removed because it was dead weight — the parquet already had overnight data from the prior IB seed, so the only "gap" was the current active session, which IB refuses to serve. For 1s, there is no prior IB seed, so the gap is real and IB can serve it (it's completed historical time, not an active session).

**`_mes_tick_bar` reset timing**: Reset `_mes_tick_bar = None` inside `_on_mnq_1m_bar` (not `_on_mes_1m_bar`) so both MNQ and MES accumulators clear at the same 1m boundary. This prevents the last partial MES second from bleeding into the next minute's 1s pending buffer.

**1m boundary flush is best-effort**: If the process crashes between the 1m parquet write and the 1s flush, at most one minute of 1s bars is lost. `backfill_1s_parquets()` restores them at next startup once Databento has the data.

**`_start_ts` in backfill_1s_parquets**: Uses `last_bar + 1s` (not `last_bar` like the 1m version) because 1s granularity means we need to advance by exactly one bar to avoid re-fetching the last known bar.

**Ticker symbol**: `MNQ.v.0` / `MES.v.0` (volume-weighted front month, same as `databento_backfill.py`) not `.c.0` (price-adjusted). Both work with `stype_in="continuous"`.

**`max_lookback_days=10` for 1s**: Much shorter than the 1m `max_lookback_days=30` because 1s data volume is ~60× larger. 10 days ≈ 10 × 6.5h × 3600s ≈ 234 000 bars per instrument — manageable but not trivial.
