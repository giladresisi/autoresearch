# IB-Gateway Connection Instability — Findings & Fix Plan

**Created:** 2026-04-17  
**Complexity:** ⚠️ Medium  
**Primary Files:** `signal_smt.py`, `data/sources.py`

---

## Problem Statement

`signal_smt.py` has been experiencing unstable IB-Gateway connections: fails to connect on startup, drops mid-session, and takes a long time to recover. Symptoms observed by the agent that ran the script; exact error messages not captured.

---

## Root Cause Investigation (Phase 1 Complete)

Three distinct root causes were identified by reading the code. They are independent but can compound each other.

### Root Cause 1 — Gap-fill triggers IBKR pacing violation at startup (HIGH likelihood)

**Evidence:** `_gap_fill_1m()` (signal_smt.py L214–256) opens a second IB connection (clientId 16) and calls `IBGatewaySource.fetch()` for both MNQ and MES. `IBGatewaySource.fetch()` paginates backwards in `_IB_CHUNK_DAYS["1m"]`-day windows over a 30-day lookback window. Depending on chunk size this is **10–30+ sequential `reqHistoricalData` calls per instrument = 20–60+ total calls** made in rapid succession.

**IBKR's pacing rule:** max 60 historical data requests per 10 minutes per account (not per clientId). Hitting this limit at startup puts the account in a throttled state right before the main connection (clientId 15) tries to register 4 live subscriptions (`keepUpToDate=True` bars + `reqTickByTickData`).

**When pacing is violated, IB Gateway:**
- Sends error code 162 ("Historical Market Data Service error message: Trading TWS session is connected from a different IP address")  
- Or error 10197 ("No market data during competing live session")
- Or silently drops the historical request and the keepUpToDate subscription never fires
- In severe cases, disconnects the client entirely

**Fix:** Move gap-fill to use Databento Historical (already implemented in the reference patch at `.agents/plans/databento-live-migration.md`) OR replace `IBGatewaySource.fetch()` with `ContFuture` + a single `reqHistoricalData(durationStr="30 D")` call instead of paginated chunks. For the IB-only path: limit the gap-fill lookback at startup to the minimum needed (e.g., last 2–3 days, not 30), since the parquet cache should already be mostly current if the process ran yesterday.

**Simplest fix for now:** Cap the gap-fill lookback at startup to `min(last_cached_bar_age_days + 1, 3)` days instead of always requesting 30 days. This reduces gap-fill requests from 20–60 down to 2–6.

---

### Root Cause 2 — No reconnect loop; any disconnect terminates the process (HIGH likelihood)

**Evidence:** `main()` (signal_smt.py L481–551):
```python
try:
    util.run()
finally:
    if _ib and _ib.isConnected():
        _ib.disconnect()
```
`util.run()` exits on any disconnect (network blip, IB Gateway nightly restart at ~11:45pm ET, pacing kick, etc.). The `finally` block disconnects and the process exits. There is no retry loop.

**IB Gateway restarts nightly** (configurable, default ~11:45pm ET for maintenance). Any script running through that window will lose its connection and never recover without a restart.

**Fix:** Wrap the connection + subscription block in a retry loop with exponential backoff:

```python
import time

MAX_RETRIES = 10
RETRY_DELAY_S = 15  # seconds between retries

for attempt in range(MAX_RETRIES):
    try:
        _ib = IB()
        _ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)
        # ... subscriptions ...
        util.run()
        break  # clean exit
    except Exception as e:
        print(f"[{attempt+1}/{MAX_RETRIES}] Connection error: {e}. Retrying in {RETRY_DELAY_S}s...")
        try:
            if _ib and _ib.isConnected():
                _ib.disconnect()
        except Exception:
            pass
        time.sleep(RETRY_DELAY_S)
```

**Important:** On reconnect, re-register all subscriptions (reqHistoricalData + reqTickByTickData) since IB does not automatically re-deliver them after a disconnect. Module-level state (`_mnq_1m_df`, `_state`, `_position`) should be preserved across reconnects — only the IB connection objects and event subscriptions need to be re-established.

---

### Root Cause 3 — ClientId held by IB Gateway after abrupt exit (MEDIUM likelihood)

**Evidence:** IB Gateway holds a clientId for ~10–30 seconds after a client disconnects (especially on abrupt exit/crash). If `signal_smt.py` is killed (Ctrl+C, crash) and immediately restarted, the new connection attempt with `clientId=15` gets "client id already in use" or silently fails.

**Fix options (pick one):**
1. **Wait on startup:** Add `time.sleep(5)` before `_ib.connect()` — cheap, effective for manual restarts
2. **Retry with backoff:** The retry loop from Root Cause 2 already handles this — a failed connect just retries 15 seconds later when the clientId is released
3. **Randomize clientId:** Use `clientId=random.randint(100, 999)` — avoids stale-id conflicts entirely, but makes debugging harder

Option 2 (retry loop) handles both RC2 and RC3 simultaneously.

---

## What NOT to Investigate (already ruled out)

- `IBGatewaySource.fetch()` disconnect: has a proper `finally: ib.disconnect()` block — clean
- Event loop conflicts between gap-fill and main: gap-fill runs and completes before `util.run()` is called — no overlap
- Multiple simultaneous clients from other scripts: user runs only `signal_smt.py` actively

---

## Implementation Plan

### Task 1 — Reduce gap-fill lookback to cap pacing load

In `signal_smt.py`, change `_gap_fill_1m()` to request at most `GAP_FILL_MAX_DAYS = 3` days (not 30) unless the parquet is empty (first run). This is the line:

```python
# Before:
lookback_floor = now - pd.Timedelta(days=MAX_LOOKBACK_DAYS)

# After:
GAP_FILL_MAX_DAYS = 3  # cap pacing load; 30-day history is in parquet already
lookback_floor = now - pd.Timedelta(days=GAP_FILL_MAX_DAYS)
```

Add a constant `GAP_FILL_MAX_DAYS = 3` near the other constants at the top of the file.

> **Note:** `MAX_LOOKBACK_DAYS = 30` stays unchanged — it's still used by the state machine logic for signal detection. Only the gap-fill's lookback window changes.

### Task 2 — Add reconnect loop to `main()`

Replace the body of `main()` from after the initial setup (parquet load + gap-fill + position restore) through to the end, wrapping only the IB connection + subscription + `util.run()` block in a retry loop. The setup code (session time parsing, buffer init, parquet load, gap-fill, position restore) runs once before the loop.

### Task 3 — Extract subscriptions into a helper

To support reconnects cleanly, extract the subscription setup into `_setup_ib_subscriptions(ib, mnq_contract, mes_contract)` that returns the four subscription objects and wires up the event handlers. This makes the retry loop clean:

```python
def _setup_ib_subscriptions(ib, mnq_contract, mes_contract):
    mnq_1m = ib.reqHistoricalData(mnq_contract, ..., keepUpToDate=True)
    mes_1m = ib.reqHistoricalData(mes_contract, ..., keepUpToDate=True)
    mnq_tick = ib.reqTickByTickData(mnq_contract, "AllLast", 0, False)
    mes_tick  = ib.reqTickByTickData(mes_contract, "AllLast", 0, False)
    mnq_1m.updateEvent   += on_mnq_1m_bar
    mes_1m.updateEvent   += on_mes_1m_bar
    mnq_tick.updateEvent += on_mnq_tick
    mes_tick.updateEvent += on_mes_tick
```

---

## Validation

```bash
# Import check
python -c "import signal_smt; print('OK')"

# Unit tests — no regressions
python -m pytest tests/test_signal_smt.py -v

# Manual connection test (requires IB Gateway running):
# Run signal_smt.py, kill it with Ctrl+C, immediately restart → should reconnect within 15s
# Run signal_smt.py, kill IB Gateway process, restart IB Gateway → should auto-reconnect
```

---

## Acceptance Criteria

- [ ] `GAP_FILL_MAX_DAYS = 3` constant added; gap-fill lookback capped at 3 days (not 30) unless parquet is empty
- [ ] `main()` wraps IB connection in a retry loop (max 10 attempts, 15s delay) with logging on each retry
- [ ] Module-level state (`_mnq_1m_df`, `_mes_1m_df`, `_state`, `_position`) is preserved across reconnects
- [ ] `_setup_ib_subscriptions()` (or equivalent inline) re-registers all 4 subscriptions on each reconnect attempt
- [ ] All existing tests pass (27 passing, 1 pre-existing failure `test_process_managing_exit_tp` — do not fix)
- [ ] `data/sources.py` and `train_smt.py` unchanged

---

## Files Changed

- `signal_smt.py` — all changes
- No other files

## Out of Scope

- Fixing `test_process_managing_exit_tp` (pre-existing failure, unrelated)
- Changing `IBGatewaySource` in `data/sources.py`
- Adding `qualifyContracts()` for conId auto-resolution (separate task, lower priority)
