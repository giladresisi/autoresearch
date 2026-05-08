# live_orders.py
# Singleton executor for live order routing (V2 pipeline + ad-hoc human orders).
# Executor is chosen once at import time from LIVE_TRADING env var:
#   LIVE_TRADING=true  → PickMyTradeExecutor (real PMT webhook)
#   LIVE_TRADING=false → SimulatedBrokerExecutor (no-op / paper mode)
#
# Two tiers of functions:
#   - Executor functions (place_entry, modify_stop_entry, etc.) — no logging; called by SmtV2Dispatcher._emit
#   - Ad-hoc functions (manual_close, manual_cancel_entry) — log + execute; called by skill / user
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Optional

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

_pending_entry: Optional[dict] = None  # last pmt_signal sent as a stop entry (PMT shape)


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
# Executor functions — called by SmtV2Dispatcher._emit; logging done by caller
# ---------------------------------------------------------------------------

def place_entry(pmt_signal: dict) -> None:
    """Place a new stop or market entry.

    pmt_signal keys: direction ("long"/"short"), entry_price, stop_price,
    optionally stop_fill_bars (present = STP, absent = MKT).
    """
    global _pending_entry
    _executor.place_entry(pmt_signal, None)
    if pmt_signal.get("stop_fill_bars") is not None or pmt_signal.get("limit_fill_bars") is not None:
        _pending_entry = pmt_signal
    else:
        _pending_entry = None


def modify_stop_entry(old_pmt: dict, new_pmt: dict) -> None:
    """Cancel existing stop entry and replace with a new one."""
    global _pending_entry
    _executor.modify_stop_entry(old_pmt, new_pmt, None)
    _pending_entry = new_pmt


def place_stop_after_fill(position: dict) -> None:
    """Send stop placement after a limit fill. position: {direction, stop_price}."""
    global _pending_entry
    _executor.place_stop_after_limit_fill(position, None)
    _pending_entry = None


def close(label: str = "close") -> None:
    """Send a market close order."""
    global _pending_entry
    _executor.place_close(label)
    _pending_entry = None


def cancel_entry(label: str = "cancel-stop") -> None:
    """Cancel a pending stop entry if one exists. No-op if nothing is pending."""
    global _pending_entry
    if _pending_entry is not None:
        _executor.place_close(label)
        _pending_entry = None


# ---------------------------------------------------------------------------
# Position state helpers — read position.json; usable by skill and orchestrator
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
# Ad-hoc convenience functions — log AND execute AND sync position.json
# ---------------------------------------------------------------------------

def manual_close(price: float, reason: str = "user-requested") -> None:
    """Manually close the active position. Logs, sends close order, clears position.json."""
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    _log({"time": now, "kind": "market-close", "price": float(price),
          "source": "manual", "reason": reason})
    close("manual")
    pos = _load_pos()
    pos["active"] = {}
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos["confirmation_bar"] = {}
    _save_pos(pos)


def manual_cancel_entry(reason: str = "user-requested") -> None:
    """Manually cancel a pending stop entry. Checks position.json — no-op if nothing pending."""
    pos = _load_pos()
    stop = pos.get("stop_entry", "")
    if not stop and _pending_entry is None:
        return
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    price = float(stop) if stop else float((_pending_entry or {}).get("entry_price", 0))
    _log({"time": now, "kind": "cancel-stop-entry", "price": price,
          "source": "manual", "reason": reason})
    cancel_entry("manual")
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos["confirmation_bar"] = {}
    _save_pos(pos)
