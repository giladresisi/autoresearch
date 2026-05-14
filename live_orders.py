# live_orders.py
# Singleton executor for live order routing (V2 pipeline + ad-hoc human orders).
# Executor is chosen once at import time from LIVE_TRADING env var:
#   LIVE_TRADING=true  → PickMyTradeExecutor (real PMT webhook)
#   LIVE_TRADING=false → SimulatedBrokerExecutor (no-op / paper mode)
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

_LIVE = os.getenv("LIVE_TRADING", "false").lower() == "true"

if _LIVE:
    from execution.pickmytrade import PickMyTradeExecutor
    _account_ids = [s.strip() for s in os.environ.get("TRADING_ACCOUNT_IDS", "").split(",") if s.strip()]
    _executor = PickMyTradeExecutor(
        webhook_url=os.environ["PMT_WEBHOOK_URL"],
        api_key=os.environ["PMT_API_KEY"],
        symbol=os.environ.get("TRADING_SYMBOL", "MNQ1!"),
        account_ids=_account_ids,
        contracts=int(os.environ.get("TRADING_CONTRACTS", "2")),
        entry_slip_ticks=int(os.environ.get("PMT_ENTRY_SLIP_TICKS", "2")),
    )
else:
    from execution.simulated import SimulatedBrokerExecutor
    _executor = SimulatedBrokerExecutor(human_mode=True)


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
# Position state helpers
# ---------------------------------------------------------------------------

def _load_pos() -> dict:
    from smt_state import load_position
    return load_position()


def _save_pos(pos: dict) -> None:
    from smt_state import save_position
    save_position(pos)


def get_position() -> dict:
    """Return current position.json state."""
    return _load_pos()


def has_active_position() -> bool:
    """True if position.json shows an active (filled) trade."""
    return bool(_load_pos().get("active"))


def has_pending_entry() -> bool:
    """True if position.json shows an unfilled stop entry order."""
    return bool(_load_pos().get("stop_entry"))


# ---------------------------------------------------------------------------
# Unified API — each function: log → executor → sync position.json
# ---------------------------------------------------------------------------

def place_stop_entry(direction: str, entry_price: float, stop_price: float) -> None:
    """Place unfilled stop entry. Logs, dispatches STP order, writes stop_entry to position.json."""
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    pmt_signal = {
        "direction": direction,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "stop_fill_bars": 1,
    }
    _executor.place_entry(pmt_signal, None)
    pos = _load_pos()
    pos["stop_entry"] = str(entry_price)
    pos["stop_direction"] = "up" if direction == "long" else "down"
    _save_pos(pos)
    _log({"time": now, "kind": "new-stop-entry", "direction": direction,
          "entry_price": entry_price, "stop_price": stop_price})


def place_market_entry(direction: str, entry_price: float, stop_price: float) -> None:
    """Enter at market with stop. Logs, dispatches MKT+sl, writes active to position.json."""
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    pmt_signal = {
        "direction": direction,
        "entry_price": entry_price,
        "stop_price": stop_price,
    }
    _executor.place_entry(pmt_signal, None)
    pos = _load_pos()
    pos["active"] = {
        "direction": direction,
        "fill_price": entry_price,
        "stop": stop_price,
        "cautious": "no",
        "contracts": 2,
    }
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    _save_pos(pos)
    _log({"time": now, "kind": "market-entry", "direction": direction,
          "entry_price": entry_price, "stop_price": stop_price})


def move_stop_entry(new_entry_price: float, new_stop_price: float, direction: str) -> None:
    """Cancel existing unfilled stop entry and replace. Reads old entry_price from position.json."""
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    pos = _load_pos()
    old_entry = float(pos["stop_entry"]) if pos.get("stop_entry") else new_entry_price
    old_pmt = {
        "direction": direction,
        "entry_price": old_entry,
        "stop_price": new_stop_price,
        "stop_fill_bars": 1,
    }
    new_pmt = {
        "direction": direction,
        "entry_price": new_entry_price,
        "stop_price": new_stop_price,
        "stop_fill_bars": 1,
    }
    _executor.modify_stop_entry(old_pmt, new_pmt, None)
    pos["stop_entry"] = str(new_entry_price)
    _save_pos(pos)
    _log({"time": now, "kind": "move-stop-entry", "direction": direction,
          "old_entry_price": old_entry, "new_entry_price": new_entry_price,
          "new_stop_price": new_stop_price})


def stop_entry_filled(direction: str, stop_price: float) -> None:
    """Stop entry just filled — send protective S/L to PMT, log, update active.stop in position.json."""
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    _executor.update_stop_loss({"direction": direction, "stop_price": stop_price}, None)
    pos = _load_pos()
    if pos.get("active"):
        pos["active"]["stop"] = stop_price
        _save_pos(pos)
    else:
        print("[live_orders] stop_entry_filled: active position absent — position.json not updated", flush=True)
    _log({"time": now, "kind": "stop-entry-filled", "direction": direction, "stop_price": stop_price})


def cancel_stop_entry(reason: str = "user-requested") -> None:
    """Cancel pending stop entry. No-op if stop_entry is empty. Logs, dispatches close, clears position.json."""
    pos = _load_pos()
    if not pos.get("stop_entry"):
        return
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    entry_price = float(pos["stop_entry"])
    _executor.place_close("cancel-stop")
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos["confirmation_bar"] = {}
    _save_pos(pos)
    _log({"time": now, "kind": "cancel-stop-entry", "entry_price": entry_price, "reason": reason})


def close_position(price: float, reason: str = "user-requested") -> None:
    """Market-close active position. Logs, dispatches close, clears active in position.json."""
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    _executor.place_close("close")
    pos = _load_pos()
    pos["active"] = {}
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos["confirmation_bar"] = {}
    _save_pos(pos)
    _log({"time": now, "kind": "market-close", "price": float(price), "reason": reason})


def update_stop_loss(stop_price: float, reason: str = "user-requested") -> None:
    """Update protective stop on active position (trade.py close <price>). Logs, dispatches update_sl, updates active.stop."""
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    pos = _load_pos()
    direction = pos.get("active", {}).get("direction", "")
    _executor.update_stop_loss({"direction": direction, "stop_price": stop_price}, None)
    if pos.get("active"):
        pos["active"]["stop"] = stop_price
        _save_pos(pos)
    _log({"time": now, "kind": "update-stop-loss", "stop_price": stop_price, "reason": reason})
