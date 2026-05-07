# Ad-Hoc Live Trading Orders + Session Plotting

## Context and motivation

The live trading pipeline (V2) currently routes all orders through `SmtV2Dispatcher._emit`
inside `automation/main.py`. That dispatcher holds a `PickMyTradeExecutor` instance and
calls it directly. This creates two gaps:

1. **No ad-hoc order path.** If the user wants to manually close a position or cancel a
   pending limit outside the strategy's normal signal flow, there is no way to do it
   through code — and if done through the PMT UI directly, nothing gets logged.

2. **No aggregated session log.** The orchestrator captures subprocess stdout into
   `sessions/{date}/signals.log`, but that file is opened in write mode and truncated on
   each orchestrator restart. There is no durable, structured record of all events across
   restarts and ad-hoc actions.

This plan introduces:
- **`live_orders.py`** — a singleton executor module that owns all order dispatch,
  provides ad-hoc convenience functions, and is the single place everything routes through.
- **`sessions/{date}/events.jsonl`** — an append-mode structured event log that persists
  across orchestrator restarts and includes both strategy-fired and ad-hoc events.
- **`sessions/{date}/levels.json`** — a snapshot of the session's liquidities (from
  `daily.json`) written at session start, used by the plot script.
- **`plot_session.py`** — an interactive HTML chart script (adapted from
  `data/regression/plot_regression.py`) for ad-hoc session review.
- **`.claude/skills/live-trading/SKILL.md`** — a skill that handles user requests like
  "plot the session so far", "close the position", "cancel the limit".

---

## Executor selection

The executor type is chosen ONCE at `live_orders` import time based on the `LIVE_TRADING`
env var. There is never more than one executor active at a time:

- `LIVE_TRADING=true` → `PickMyTradeExecutor` (real PMT webhook orders)
- `LIVE_TRADING=false` or unset → `SimulatedBrokerExecutor` (no-op / paper mode)

This mirrors the same choice already made in `automation/main.py`. After this refactor,
`SmtV2Dispatcher` no longer holds an executor — it delegates to `live_orders`.

---

## File 1: `live_orders.py` (new)

Location: `automation/live_orders.py`

### Responsibilities

- Own the executor singleton
- Track `_pending_limit` state (moves OUT of `SmtV2Dispatcher`)
- Provide executor functions called by `SmtV2Dispatcher._emit` (no logging — caller logs)
- Provide ad-hoc convenience functions with built-in logging, for skill/user use

### Module structure

```python
# live_orders.py
# Singleton executor for live order routing.
# Chosen at import time from LIVE_TRADING env var: PMT (live) or simulated (paper).
# No logging inside executor functions — SmtV2Dispatcher._emit logs automated signals.
# Ad-hoc functions (manual_*) log themselves since they bypass the dispatcher.

from __future__ import annotations
import json
import os
import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

_LIVE = os.getenv("LIVE_TRADING", "false").lower() == "true"

if _LIVE:
    from execution.pickmytrade import PickMyTradeExecutor
    _executor = PickMyTradeExecutor(
        webhook_url=os.environ["PMT_WEBHOOK_URL"],
        api_key=os.environ["PMT_API_KEY"],
        symbol=os.environ.get("TRADING_SYMBOL", "MNQ1!"),
        account_id=os.environ["TRADING_ACCOUNT_ID"],
        contracts=int(os.environ.get("TRADING_CONTRACTS", "2")),
    )
else:
    from execution.simulated import SimulatedBrokerExecutor
    _executor = SimulatedBrokerExecutor(human_mode=True)

_pending_limit: Optional[dict] = None  # last sent limit signal dict (PMT shape)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(event: dict) -> None:
    """Append one JSON line to sessions/{today}/events.jsonl (append mode)."""
    today = datetime.date.today().isoformat()
    path = Path("sessions") / today / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Executor functions — called by SmtV2Dispatcher._emit, no logging here
# ---------------------------------------------------------------------------

def place_entry(pmt_signal: dict) -> None:
    """Place a new limit or market entry. pmt_signal shape: {direction, entry_price, stop_price, [limit_fill_bars]}."""
    global _pending_limit
    is_limit = pmt_signal.get("limit_fill_bars") is not None
    _executor.place_entry(pmt_signal, None)
    if is_limit:
        _pending_limit = pmt_signal
    else:
        _pending_limit = None


def modify_limit(old_pmt: dict, new_pmt: dict) -> None:
    """Cancel existing limit and replace with new one."""
    global _pending_limit
    _executor.modify_limit_entry(old_pmt, new_pmt, None)
    _pending_limit = new_pmt


def place_stop_after_fill(position: dict) -> None:
    """Send stop placement after a limit fill. position: {direction, stop_price}."""
    global _pending_limit
    _executor.place_stop_after_limit_fill(position, None)
    _pending_limit = None


def close(label: str = "close") -> None:
    """Send a market close order."""
    global _pending_limit
    _executor.place_close(label)
    _pending_limit = None


def cancel_limit(label: str = "cancel-limit") -> None:
    """Cancel a pending limit if one exists."""
    global _pending_limit
    if _pending_limit is not None:
        _executor.place_close(label)
        _pending_limit = None


# ---------------------------------------------------------------------------
# Ad-hoc convenience functions — log AND execute; for skill / manual use
# ---------------------------------------------------------------------------

def manual_close(price: float, reason: str = "user-requested") -> None:
    """Manually close the active position. Logs the event and sends close order."""
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    _log({"time": now, "kind": "market-close", "price": price,
          "source": "manual", "reason": reason})
    close("manual")


def manual_cancel_limit(reason: str = "user-requested") -> None:
    """Manually cancel a pending limit order. No-op if no limit is pending."""
    global _pending_limit
    if _pending_limit is None:
        return
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    price = float(_pending_limit.get("entry_price", 0))
    _log({"time": now, "kind": "cancel-limit-entry", "price": price,
          "source": "manual", "reason": reason})
    cancel_limit("manual")
```

---

## File 2: `automation/main.py` — `SmtV2Dispatcher` refactor

### What changes

`SmtV2Dispatcher` currently:
- Takes an `executor` arg in `__init__` and stores it as `self._executor`
- Tracks `_pending_limit` on self
- Calls `self._executor.*` directly in `_emit`

After refactor:
- Takes NO executor arg (removed)
- Has NO `_pending_limit` (moved to `live_orders`)
- `_emit` does two things for every signal:
  1. Calls `live_orders._log(sig | {"source": "strategy"})` to log it
  2. Routes to `live_orders.*` executor functions for the signals that need order dispatch

### Updated `__init__`

```python
def __init__(self) -> None:
    self._pipeline = None
    self._session_date = None
```

### Updated `_emit`

```python
def _emit(self, sig: dict) -> None:
    import live_orders as _lo
    _emit_v2_signal(sig)  # stdout (captured by orchestrator SessionRelay → signals.log)
    _lo._log(dict(sig, source="strategy"))  # structured append to events.jsonl

    kind = sig.get("kind")
    direction_v2 = sig.get("direction", "none")

    if kind in ("new-limit-entry", "move-limit-entry"):
        if direction_v2 == "none":
            return
        direction = "long" if direction_v2 == "up" else "short"
        position = smt_state.load_position()
        conf_bar = position.get("confirmation_bar", {})
        stop = conf_bar.get("body_low") if direction_v2 == "up" else conf_bar.get("body_high")
        if stop is None:
            return
        pmt_signal = {
            "direction": direction,
            "entry_price": float(sig["price"]),
            "stop_price": float(stop),
            "limit_fill_bars": 1,
        }
        if kind == "new-limit-entry":
            _lo.place_entry(pmt_signal)
        else:
            _lo.modify_limit(_lo._pending_limit or pmt_signal, pmt_signal)

    elif kind == "limit-entry-filled":
        direction = "long" if direction_v2 == "up" else "short"
        stop = sig.get("stop")
        if stop is None:
            return
        _lo.place_stop_after_fill({"direction": direction, "stop_price": float(stop)})

    elif kind == "market-entry":
        direction = "long" if direction_v2 == "up" else "short"
        stop = sig.get("stop")
        if stop is None:
            return
        pmt_signal = {
            "direction": direction,
            "entry_price": float(sig["price"]),
            "stop_price": float(stop),
        }
        _lo.place_entry(pmt_signal)

    elif kind == "market-close":
        _lo.close("v2-direction-mismatch")

    elif kind == "cancel-limit-entry":
        _lo.cancel_limit("cancel-limit")

    elif kind == "stopped-out":
        _lo._pending_limit = None  # no executor call; just clear pending state
```

### Updated `main()` call site

Remove `SmtV2Dispatcher(_executor)` — it's now just `SmtV2Dispatcher()`. The `_executor`
local variable and `PickMyTradeExecutor` construction can be removed from `main()` too
(the executor lives in `live_orders` now). However: `_executor.start()` and `_executor.stop()`
still need to happen. Add `live_orders._executor.start()` / `live_orders._executor.stop()`
at the appropriate points in `main()`.

---

## File 3: `session_pipeline.py` — write `levels.json`

In `on_session_start`, after `run_daily()` completes (whether it ran or was skipped due to
Option A), write `sessions/{date}/levels.json` if it doesn't already exist:

```python
import json as _json
from pathlib import Path as _Path
from smt_state import load_daily as _load_daily, load_global as _load_global

_session_dir = _Path("sessions") / str(now.date())
_session_dir.mkdir(parents=True, exist_ok=True)
_levels_path = _session_dir / "levels.json"
if not _levels_path.exists():
    _daily_state = _load_daily()
    _global_state = _load_global()
    _levels_path.write_text(
        _json.dumps({
            "liquidities": _daily_state.get("liquidities", []),
            "all_time_high": _global_state.get("all_time_high"),
        }, indent=2),
        encoding="utf-8",
    )
```

---

## File 4: `plot_session.py` (new)

Location: `automation/plot_session.py` (automation root, runnable from there directly)

### Usage

```
python plot_session.py             # defaults to today's date
python plot_session.py 2026-05-06  # specific date
```

### Key differences from `data/regression/plot_regression.py`

| Aspect | `plot_regression.py` | `plot_session.py` |
|--------|---------------------|-------------------|
| Default date | First line of `regression.md` | `datetime.date.today()` |
| Events path | `data/regression/{DATE}/events.jsonl` | `sessions/{DATE}/events.jsonl` |
| Levels path | `data/regression/{DATE}/levels.json` | `sessions/{DATE}/levels.json` |
| Output | `data/regression/{DATE}/chart.html` | `sessions/{DATE}/chart.html` |
| Empty events | Crashes (assumes events exist) | Shows bars + levels with "No events yet" title |
| Zoom window | Padded around first/last event | Full session window (09:00–17:00 ET) when no events |

### Additional marker style to add (not in regression script)

```python
OTHER_MARKER_STYLE = {
    ...existing styles...,
    "cancel-limit-entry": dict(symbol="x-open", color="#FF9800", size=13),
}
```

### Implementation note

Copy `plot_regression.py` verbatim and apply these diffs:
1. Replace date-loading block (remove `_date_from_regression_md`, add `datetime.date.today()` default)
2. Replace path strings (3 occurrences: events, levels, output)
3. Add graceful empty-events handling:
   ```python
   if not events:
       first_t = pd.Timestamp(f"{DATE} 09:00", tz="America/New_York")
       last_t  = pd.Timestamp(f"{DATE} 17:00", tz="America/New_York")
   else:
       first_t = min(e["ts"] for e in events) - pd.Timedelta(minutes=30)
       last_t  = max(e["ts"] for e in events) + pd.Timedelta(minutes=30)
   ```
4. Add `cancel-limit-entry` to `OTHER_MARKER_STYLE`
5. Update title line: `f"Live Session — MNQ {DATE} | {len(pairs)} trades | ..."`

---

## File 5: `.claude/skills/live-trading/SKILL.md` (new)

This skill handles user requests about live session monitoring and ad-hoc order management.
The skill file is at `.claude/skills/live-trading/SKILL.md` in the automation directory.

### Trigger phrases (include in skill frontmatter description)

- "plot the session" / "plot the session so far" / "show me the chart" / "chart today"
- "close the position" / "close manually" / "get out now" / "exit the trade"
- "cancel the limit" / "remove the limit order" / "cancel pending order"
- "what's in the session log" / "show session events" / "what happened today"

### Skill content outline

```markdown
---
name: live-trading
description: >
  Use when the user asks to plot the current live session, close a position manually,
  cancel a pending limit, or inspect today's session events. Trigger phrases include:
  "plot the session", "show the chart", "close the position", "cancel the limit",
  "what happened today", "show session events".
---

# live-trading

## Plot the session

Run from the automation directory:
```bash
python plot_session.py
```
Or for a specific date:
```bash
python plot_session.py 2026-05-06
```

This opens `sessions/{date}/chart.html` in the browser. The chart shows:
- MNQ 1m candlesticks for the visible window
- All price levels from the daily computation (TDO, TWO, week/day H/L/mid, session highs/lows, FVGs)
- All strategy events: limit placements, entries, stops, closes, SMT divergences, hypothesis formations
- All manual/ad-hoc events (source: "manual")
- P&L annotations on exit markers

The events file is `sessions/{date}/events.jsonl` — it persists across orchestrator
restarts and includes both automated strategy events and ad-hoc manual events.

## Close a position manually

```python
import live_orders
live_orders.manual_close(price=<current_price>, reason="user-requested")
```

Replace `<current_price>` with the approximate current MNQ price. This:
1. Logs the close event to `sessions/{today}/events.jsonl` with `source: "manual"`
2. Sends a market close order to PMT (or simulated executor in paper mode)
3. Clears `_pending_limit` state

## Cancel a pending limit manually

```python
import live_orders
live_orders.manual_cancel_limit(reason="user-requested")
```

This is a no-op if no limit is currently pending. Otherwise it:
1. Logs the cancel event to `sessions/{today}/events.jsonl`
2. Sends a close/cancel order to PMT

## Inspect today's events

The session events live at `sessions/{today}/events.jsonl`.
Each line is a JSON object with at minimum: `time`, `kind`, `price`, `source`.

To read them:
```bash
python -c "
import json
from pathlib import Path
import datetime
today = datetime.date.today().isoformat()
path = Path(f'sessions/{today}/events.jsonl')
if path.exists():
    for line in path.read_text().splitlines():
        e = json.loads(line)
        print(f\"{e['time'][11:16]} {e['kind']:25s} {e.get('price',''):>10} {e.get('source','')}\")
else:
    print('No events yet today.')
"
```

## Session file structure

```
sessions/{date}/
├── signals.log    # Raw stdout from automation process (orchestrator-captured)
├── events.jsonl   # Structured events, append-mode, persists across restarts
├── levels.json    # Liquidities snapshot from daily.json (written at session start)
└── chart.html     # Generated by plot_session.py
```
```

---

## Event schema

Every line in `events.jsonl` is a JSON object. Required fields:

| Field | Type | Description |
|-------|------|-------------|
| `time` | ISO-8601 string | Bar/wall-clock time (tz-aware) |
| `kind` | string | Signal kind (see below) |
| `price` | float | Reference price for the event |
| `source` | string | `"strategy"` (automated) or `"manual"` (ad-hoc) |

Optional fields (present on some kinds):

| Field | Present on |
|-------|-----------|
| `direction` | `new-limit-entry`, `move-limit-entry`, `limit-entry-filled`, `market-entry`, `market-close` |
| `stop` | `limit-entry-filled`, `market-entry` |
| `reason` | `cancel-limit-entry`, `market-close`, `stopped-out` |
| `close_reason` | `market-close` |
| `side` | `smt-div` |
| `type` | `smt-div` |
| `timeframe` | `smt-div` |
| `mnq_div_price` | `smt-div` |

Known `kind` values:
- `new-limit-entry` — limit order placed
- `move-limit-entry` — limit order moved (cancel + replace)
- `limit-entry-filled` — limit order filled
- `market-entry` — market entry placed
- `market-close` — position closed at market
- `cancel-limit-entry` — pending limit cancelled (no fill)
- `stopped-out` — stop hit
- `smt-div` — SMT divergence detected
- `new-hypothesis` — new directional hypothesis formed
- `trend-broken` — trend invalidated

---

## Implementation checklist

An implementing agent should complete these tasks in order:

1. **Create `live_orders.py`** as specified above. Verify it imports cleanly with
   `python -c "import live_orders"` (both `LIVE_TRADING=true` and `=false`).

2. **Refactor `SmtV2Dispatcher` in `automation/main.py`**:
   - Remove `executor` parameter from `__init__`
   - Remove `self._executor` and `self._pending_limit`
   - Replace `_emit` body as specified above
   - In `main()`: replace `SmtV2Dispatcher(_executor)` with `SmtV2Dispatcher()`
   - In `main()`: replace `_executor.start()` / `_executor.stop()` with
     `live_orders._executor.start()` / `live_orders._executor.stop()`
   - Remove local `_executor` construction in `main()` (now lives in `live_orders`)

3. **Update `session_pipeline.py`** to write `sessions/{date}/levels.json` in
   `on_session_start` as specified above.

4. **Create `plot_session.py`** by adapting `data/regression/plot_regression.py`
   as specified above.

5. **Create `.claude/skills/live-trading/SKILL.md`** with the content outlined above.

6. **Smoke test** (no live trading needed):
   - Set `LIVE_TRADING=false` in `.env`
   - Run `python -c "import live_orders; live_orders.manual_close(19800.0)"`
   - Verify `sessions/{today}/events.jsonl` was created with one line
   - Run `python plot_session.py` — should open a chart with just bars and levels
     (or "No events yet" if no events yet)

7. **Run existing test suite** to confirm no regressions:
   ```bash
   python -m pytest tests/ -q
   ```

---

## What does NOT change

- `strategy.py` — unchanged
- `session_pipeline.py` signal routing — unchanged (still calls `self._emit`)
- `daily.py` — unchanged
- `hypothesis.py`, `trend.py` — unchanged
- `smt_state.py` DEFAULT_POSITION — unchanged (already has `limit_direction`)
- `signals.log` capture via orchestrator `SessionRelay` — unchanged (still happens, now
  redundant with `events.jsonl` for automated signals but kept for raw debugging)
- Regression plotting (`plot_regression.py`) — unchanged
