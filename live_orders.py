# live_orders.py
# Singleton executor for live order routing (V2 pipeline + ad-hoc human orders).
# Executor is chosen once at import time from LIVE_TRADING env var:
#   LIVE_TRADING=true  → PickMyTradeExecutor (real PMT webhook)
#   LIVE_TRADING=false → SimulatedBrokerExecutor (no-op / paper mode)
#
# Two tiers of functions:
#   - Executor functions (place_entry, modify_limit, etc.) — no logging; called by SmtV2Dispatcher._emit
#   - Ad-hoc functions (manual_close, manual_cancel_limit) — log + execute; called by skill / user
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

_pending_limit: Optional[dict] = None  # last pmt_signal sent as a limit (PMT shape)


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
    """Place a new limit or market entry.

    pmt_signal keys: direction ("long"/"short"), entry_price, stop_price,
    optionally limit_fill_bars (present = LMT, absent = MKT).
    """
    global _pending_limit
    _executor.place_entry(pmt_signal, None)
    if pmt_signal.get("limit_fill_bars") is not None:
        _pending_limit = pmt_signal
    else:
        _pending_limit = None


def modify_limit(old_pmt: dict, new_pmt: dict) -> None:
    """Cancel existing limit and replace with a new one."""
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
    """Cancel a pending limit if one exists. No-op if nothing is pending."""
    global _pending_limit
    if _pending_limit is not None:
        _executor.place_close(label)
        _pending_limit = None


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


def has_pending_limit() -> bool:
    """True if position.json shows an unfilled limit order."""
    return bool(_load_pos().get("limit_entry"))


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
    pos["limit_entry"] = ""
    pos["limit_direction"] = ""
    pos["confirmation_bar"] = {}
    _save_pos(pos)


def manual_cancel_limit(reason: str = "user-requested") -> None:
    """Manually cancel a pending limit. Checks position.json — no-op if no limit pending."""
    pos = _load_pos()
    limit = pos.get("limit_entry", "")
    if not limit and _pending_limit is None:
        return
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    price = float(limit) if limit else float((_pending_limit or {}).get("entry_price", 0))
    _log({"time": now, "kind": "cancel-limit-entry", "price": price,
          "source": "manual", "reason": reason})
    cancel_limit("manual")
    pos["limit_entry"] = ""
    pos["limit_direction"] = ""
    pos["confirmation_bar"] = {}
    _save_pos(pos)
