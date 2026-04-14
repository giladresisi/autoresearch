# signal_smt.py — Design Spec

Realtime SMT divergence signal generator for MNQ1! futures.
Streams 1s bars from IB-Gateway for high-granularity signal detection and exit monitoring,
and 1m bars for persistent parquet storage. Prints entry signals (direction, entry price
after slippage, stop, TP) and exit events (stop/TP hit with P&L).

---

## Context

### Strategy source
The optimized strategy lives in `../first-smt-opt/train_smt.py` (branch `autoresearch/first-smt-opt`).
Before implementing `signal_smt.py`, merge the worktree back to master.
The strategy functions to reuse are:
- `screen_session(mnq_bars, mes_bars, tdo)` — signal detection pipeline (updated signature, see below)
- `manage_position(position, current_bar)` — bar-by-bar exit checker
- `detect_smt_divergence`, `find_entry_bar`, `compute_tdo` — helpers

### Optimized parameters (from worktree iter 26, results.tsv)

| Parameter | Optimized value | Baseline |
|-----------|----------------|----------|
| `SESSION_START` | `"09:00"` | `"09:00"` |
| `SESSION_END` | `"13:30"` | `"10:30"` |
| `MIN_BARS_BEFORE_SIGNAL` | `10` (minutes) | `5` |
| `LONG_STOP_RATIO` | `0.05` | `0.45` |
| `SHORT_STOP_RATIO` | `0.05` | `0.45` |
| `MIN_STOP_POINTS` | `2.5` | `5.0` |
| `TRADE_DIRECTION` | `"both"` | `"both"` |
| `TDO_VALIDITY_CHECK` | `True` | `True` |

Optimized results over 6 × 60 bday folds (5m Databento data, 2024-09-01 → 2026-03-28):
`mean_test_pnl: $8,907 | min_test_pnl: $5,486 | 145 total test trades | avg_rr: 24x`

> ⚠️ **Slippage sensitivity**: `LONG/SHORT_STOP_RATIO = 0.05` means the stop is only 5% of
> |entry − TDO| away from entry. For a 30-point TDO distance, that is 1.5 points = 6 ticks.
> The 2-tick slippage constant consumes 33% of that distance. Keep `ENTRY_SLIPPAGE_TICKS`
> configurable and verify it does not cause immediate stop-outs in practice.

---

## Bar interval — dual subscription

Two parallel IB subscriptions per instrument:

| Subscription | Bar size | `durationStr` | Purpose |
|---|---|---|---|
| 1m keepUpToDate | `'1 min'` | `'1 D'` | Persistent storage in parquet only |
| 1s keepUpToDate | `'1 secs'` | `'1800 S'` | Signal detection and exit monitoring |

**1s buffer**: accumulates 0–59 bars between completed 1m bars. Cleared on each new 1m bar.
Not persisted — starts empty on every startup. No backfill required.

**Combined DataFrame**: before calling `screen_session`, the caller concatenates the 1m parquet
with the current 1s buffer:
```python
combined_mnq = pd.concat([mnq_1m_df, mnq_1s_buffer]) if not mnq_1s_buffer.empty else mnq_1m_df
combined_mes = pd.concat([mes_1m_df, mes_1s_buffer]) if not mes_1s_buffer.empty else mes_1m_df
```
This gives `screen_session` completed 1m bars for session history plus up-to-the-second
resolution for the current incomplete minute. Entry confirmation can fire mid-minute at 1s
precision. `manage_position` is called on each incoming 1s bar, so stop/TP detection is
also second-precise.

### Historical lookback: 30 days

The 1m parquet is capped at 30 days on startup gap-fill. The strategy only uses
current-session bars; 30 days is generous and prevents IB pacing issues after a long offline gap.

---

## screen_session — updated interface

`screen_session` no longer slices internally by SESSION_START/END and no longer computes TDO.
Both are the caller's responsibility. Updated signature:

```python
def screen_session(
    mnq_bars: pd.DataFrame,   # session-sliced bars (SESSION_START–SESSION_END), any interval
    mes_bars: pd.DataFrame,   # same
    tdo: float,               # True Day Open, pre-computed by caller
) -> dict | None:
```

The caller pre-slices to the session window and pre-computes TDO before each call:
```python
session_mask = (
    (combined_mnq.index.date == today)
    & (combined_mnq.index.time >= SESSION_START_time)
    & (combined_mnq.index.time <= SESSION_END_time)
)
mnq_session = combined_mnq[session_mask]
mes_session = combined_mes[session_mask]

tdo = compute_tdo(mnq_1m_df, today)   # full 1m parquet — needed for midnight bar
if tdo is None:
    return  # no TDO yet, skip

signal = screen_session(mnq_session, mes_session, tdo)
```

SESSION_START and SESSION_END are never referenced inside `screen_session` or `manage_position`.
They are caller-side constants used to gate when to start/stop processing and when to force-close.

---

## True Day Open (TDO)

`compute_tdo` returns the **Open price of the 00:00 ET bar** for the given date.

CME equity-index futures (MNQ, MES) trade nearly 24 hours. The 00:00 ET bar marks the start
of the New York calendar day and is the natural "day open" for these instruments — not 09:30,
which is the equities open and has no special meaning for futures.

Falls back to the first available bar on that date if the 00:00 bar is absent.

`compute_tdo` is called with the full 1m parquet (not the session slice) so the midnight bar
is accessible. TDO is stable throughout the session; it is computed once per signal scan,
not once per 1s bar.

---

## Architecture

```
IB-Gateway
    ├── MNQ 1m (keepUpToDate, '1 min',  '1 D')
    │       └── on_mnq_1m_bar → append to MNQ_1m.parquet + mnq_1m_df
    │                         → clear mnq_1s_buffer
    │
    ├── MNQ 1s (keepUpToDate, '1 secs', '1800 S')
    │       └── on_mnq_1s_bar → append to mnq_1s_buffer → _process()
    │
    ├── MES 1m (keepUpToDate, '1 min',  '1 D')
    │       └── on_mes_1m_bar → append to MES_1m.parquet + mes_1m_df
    │                         → clear mes_1s_buffer
    │
    └── MES 1s (keepUpToDate, '1 secs', '1800 S')
            └── on_mes_1s_bar → append to mes_1s_buffer (no signal logic)

_process() — called on each MNQ 1s bar:
    ├── SCANNING:
    │       gate: current bar within [SESSION_START, SESSION_END]
    │       gate: MES 1s buffer aligned (same latest timestamp as MNQ 1s buffer)
    │       combined_mnq = concat(mnq_1m_df, mnq_1s_buffer)
    │       combined_mes = concat(mes_1m_df, mes_1s_buffer)
    │       slice to session window → mnq_session, mes_session
    │       tdo = compute_tdo(mnq_1m_df, today)
    │       signal = screen_session(mnq_session, mes_session, tdo)
    │       re-detection guard: skip if signal["entry_time"] <= last_exit_time
    │       stale startup guard: skip if signal["entry_time"] <= startup_ts
    │       → signal? → apply slippage → save position.json → print → MANAGING
    │
    └── MANAGING:
            result = manage_position(position, current_1s_bar)
            → exit_tp / exit_stop? → compute P&L → print → record last_exit_time
                                   → delete position.json → SCANNING
            → still "hold" at SESSION_END? → force-close at current bar close
                                           → record last_exit_time → SCANNING
```

### Key design decisions

**Session awareness in caller only**: `screen_session` and `manage_position` are stateless
functions that operate on whatever bars they receive. SESSION_START and SESSION_END are
enforced entirely by the event handlers — not passed to or checked inside these functions.

**1s buffer cleared on 1m bar**: When a new 1m bar arrives, the corresponding minute is
complete. The 1s bars from that minute are no longer needed for the combined DataFrame
(they are now represented by the 1m bar). Clearing the buffer avoids double-counting the
prior minute's bars in the next `screen_session` call.

**MES 1s during MANAGING**: `on_mes_1s_bar` always appends to `mes_1s_buffer` regardless
of state. The MES 1s buffer is not used during MANAGING (only MNQ bars feed `manage_position`),
but keeping it current means the next SCANNING call has an up-to-date combined MES DataFrame.

**Re-detection guard**: After returning to SCANNING following an exit, `screen_session` would
re-scan from session start and re-detect the same signal that already fired. The guard
`signal["entry_time"] <= last_exit_time` skips any signal whose entry bar is at or before
the most recent exit — preventing the script from re-entering a trade that already played out.

**No forced close at SESSION_END for stops/TP**: `manage_position` keeps running on 1s bars
past 13:30 until stop or TP is hit. The SESSION_END force-close only triggers if the position
is still open and `manage_position` is returning "hold" at that point.

**Bar alignment gate (SCANNING only)**: `_process` checks that both MNQ and MES 1s buffers
share the same latest timestamp before calling `screen_session`. CME equity-index futures
bars arrive within milliseconds of each other so this gate is almost never the bottleneck.
It prevents a startup race condition where one buffer is ahead of the other.

---

## Startup sequence

```
1. Load data/realtime/MNQ_1m.parquet → mnq_1m_df  (empty DataFrame if first run)
   Load data/realtime/MES_1m.parquet → mes_1m_df

2. Cap lookback to 30 days:
      cutoff_ts = now - 30 days
      last_ts = max(mnq_1m_df.index[-1], cutoff_ts) if mnq_1m_df non-empty else cutoff_ts

3. Fetch missing 1m bars from last_ts → now via IBGatewaySource
      contract_type = "future_by_conid"
      MNQ conId: 770561201 (MNQM6, expires 2026-06-18)
      MES conId: 770561194 (MESM6, expires 2026-06-18)
   Append to mnq_1m_df / mes_1m_df, save parquets.

4. Check data/realtime/position.json
      → exists:  load position dict, set state = MANAGING
                 last_exit_time = epoch, startup_ts = None (no stale guard in MANAGING)
      → missing: set state = SCANNING
                 last_exit_time = epoch
                 startup_ts = now (ET-aware) — stale signal guard anchor

5. Initialize empty 1s buffers:
      mnq_1s_buffer = empty DataFrame (columns: Open, High, Low, Close, Volume)
      mes_1s_buffer = empty DataFrame

6. Connect to IB-Gateway (ib_insync IB(), host=127.0.0.1, port=4002, clientId=10)

7. Subscribe to four keepUpToDate streams:
      mnq_1m = ib.reqHistoricalData(mnq_contract, endDateTime='', durationStr='1 D',
                                    barSizeSetting='1 min', whatToShow='TRADES',
                                    useRTH=False, formatDate=2, keepUpToDate=True)
      mnq_1s = ib.reqHistoricalData(mnq_contract, endDateTime='', durationStr='1800 S',
                                    barSizeSetting='1 secs', whatToShow='TRADES',
                                    useRTH=False, formatDate=2, keepUpToDate=True)
      mes_1m = ib.reqHistoricalData(mes_contract, ..., barSizeSetting='1 min',  ...)
      mes_1s = ib.reqHistoricalData(mes_contract, ..., barSizeSetting='1 secs', ...)

      mnq_1m.updateEvent += on_mnq_1m_bar
      mnq_1s.updateEvent += on_mnq_1s_bar
      mes_1m.updateEvent += on_mes_1m_bar
      mes_1s.updateEvent += on_mes_1s_bar

8. ib.run()  ← blocking event loop
```

### conId maintenance

MNQM6 and MESM6 expire 2026-06-18. After rollover, update to MNQU6/MESU6:
```
MNQU6 conId: 793356225
MESU6 conId: 793356217
```
Make these configurable constants at the top of `signal_smt.py`, not hardcoded inline.

---

## Slippage model

```python
ENTRY_SLIPPAGE_TICKS = 2   # 0.5 MNQ points = $1.00 per contract

# Applied adversely at signal time:
# long:  assumed_entry = signal["entry_price"] + ENTRY_SLIPPAGE_TICKS * 0.25
# short: assumed_entry = signal["entry_price"] - ENTRY_SLIPPAGE_TICKS * 0.25

# stop_price and take_profit from screen_session are kept as-is (absolute prices).
# P&L on exit:
#   exit_tp:   (take_profit - assumed_entry) * MNQ_PNL_PER_POINT   [long]
#   exit_stop: (assumed_entry - stop_price) * MNQ_PNL_PER_POINT * -1  [long]
```

`MNQ_PNL_PER_POINT = 2.0` (fixed contract spec — do not change).

Rationale: models the ~15 seconds it takes the user to see the signal, open the position in TWS,
and set stop + TP. Future versions will allow the user to input the actual fill price.

---

## State machine details

### SCANNING

```
On each on_mnq_1s_bar(hasNewBar=True):
  1. append new bar to mnq_1s_buffer
  2. session gate: if bar.timestamp.time() outside [SESSION_START, SESSION_END]: skip
  3. alignment gate: if mes_1s_buffer empty or mes_1s_buffer.index[-1] != mnq_1s_buffer.index[-1]: skip
  4. combined_mnq = pd.concat([mnq_1m_df, mnq_1s_buffer])
     combined_mes = pd.concat([mes_1m_df, mes_1s_buffer])
  5. slice to session window → mnq_session, mes_session
  6. tdo = compute_tdo(mnq_1m_df, today)
  7. if tdo is None: skip
  8. signal = screen_session(mnq_session, mes_session, tdo)
  9. if signal is None: continue
 10. stale startup guard:  if signal["entry_time"] <= startup_ts: skip
 11. re-detection guard:   if signal["entry_time"] <= last_exit_time: skip
 12. assumed_entry = apply_slippage(signal)
 13. position = {**signal, "assumed_entry": assumed_entry, "contracts": 1, "instrument": "MNQ1!"}
 14. save position.json
 15. print signal line
 16. state = MANAGING

On each on_mnq_1m_bar(hasNewBar=True):
  1. append new bar to mnq_1m_df and MNQ_1m.parquet
  2. clear mnq_1s_buffer

On each on_mes_1s_bar(hasNewBar=True):
  1. append new bar to mes_1s_buffer  ← always, regardless of state

On each on_mes_1m_bar(hasNewBar=True):
  1. append new bar to mes_1m_df and MES_1m.parquet
  2. clear mes_1s_buffer
```

### MANAGING

```
On each on_mnq_1s_bar(hasNewBar=True):
  1. append new bar to mnq_1s_buffer
  2. result = manage_position(position, new_1s_bar_as_Series)
  3. if result == "hold":
       if bar.timestamp.time() >= SESSION_END_time:
         result = "exit_session_end"
         exit_price = float(new_1s_bar["Close"])
       else:
         continue
  4. if result == "exit_tp":          exit_price = position["take_profit"]
     if result == "exit_stop":        exit_price = position["stop_price"]
     # exit_session_end: exit_price already set above
  5. # P&L formula (long):  (exit_price - assumed_entry) * MNQ_PNL_PER_POINT * contracts
     # P&L formula (short): (assumed_entry - exit_price) * MNQ_PNL_PER_POINT * contracts
  6. print exit line
  7. last_exit_time = bar.timestamp
  8. delete position.json
  9. state = SCANNING

On each on_mnq_1m_bar(hasNewBar=True):
  1. append new bar to mnq_1m_df and MNQ_1m.parquet
  2. clear mnq_1s_buffer

On each on_mes_1s_bar(hasNewBar=True):
  1. append new bar to mes_1s_buffer  ← always, regardless of state

On each on_mes_1m_bar(hasNewBar=True):
  1. append new bar to mes_1m_df and MES_1m.parquet
  2. clear mes_1s_buffer
```

---

## Data layout

```
data/
  historical/
    MNQ.parquet          ← Databento 5m, 2024-01-01→present (used by train_smt.py)
    MES.parquet
  realtime/
    MNQ_1m.parquet       ← IB 1m bars, accumulated across all runs of signal_smt.py (max 30 days)
    MES_1m.parquet
    position.json        ← present only while a position is open; deleted on exit
```

`position.json` schema:
```json
{
  "direction":      "long",
  "entry_price":    19850.25,
  "assumed_entry":  19850.75,
  "entry_time":     "2026-01-06T09:14:32-05:00",
  "take_profit":    19890.00,
  "stop_price":     19848.50,
  "tdo":            19890.00,
  "contracts":      1,
  "instrument":     "MNQ1!"
}
```

---

## Terminal output format

```
[09:14:32] SIGNAL    long  | entry ~19850.75 (+2t slip) | stop 19848.50 | TP 19890.00 | RR ~24x
[09:47:11] EXIT      tp    | filled 19890.00 | P&L +$78.50 | 1 MNQ1! contract

[10:02:15] SIGNAL    short | entry ~19903.00 (-2t slip) | stop 19904.50 | TP 19855.00 | RR ~32x
[10:58:44] EXIT      stop  | filled 19904.50 | P&L -$3.00  | 1 MNQ1! contract
```

Each line is printed to stdout immediately when the event fires. No buffering.

---

## Configuration constants (top of signal_smt.py)

```python
# IB connection
IB_HOST = "127.0.0.1"
IB_PORT = 4002
IB_CLIENT_ID = 10         # distinct from prepare_futures.py (clientId=1)

# Active quarterly contract conIds — update after each quarterly rollover
MNQ_CONID = "770561201"   # MNQM6, expires 2026-06-18; next: MNQU6 = 793356225
MES_CONID = "770561194"   # MESM6, expires 2026-06-18; next: MESU6 = 793356217

# Session window — caller-side only; never passed into screen_session or manage_position
SESSION_START = "09:00"   # ET
SESSION_END   = "13:30"   # ET

# Slippage model
ENTRY_SLIPPAGE_TICKS = 2  # ticks adverse fill; 1 tick = 0.25 MNQ points

# Historical data cap
MAX_LOOKBACK_DAYS = 30    # never fetch more than 30 days of 1m bars on startup gap-fill

# Persistent storage
REALTIME_DATA_DIR = Path("data/realtime")
POSITION_FILE     = REALTIME_DATA_DIR / "position.json"
```

---

## Dependencies

All already in `pyproject.toml`:
- `ib_insync` — IB streaming
- `pandas` — DataFrame operations
- `pyarrow` — parquet read/write

No new dependencies.

---

## Out of scope (future)

- User input of actual fill price / contracts after signal fires
- MES1! as the traded instrument (currently hardcoded to MNQ1!)
- Automatic order placement via IB API
- These are deliberately deferred — the module emits signals only; execution is manual
