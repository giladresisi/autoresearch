# live_orders.py
# Singleton executor for live order routing (V2 pipeline + ad-hoc human orders).
# Executor is chosen once at import time from LIVE_TRADING env var:
#   LIVE_TRADING=true  → PickMyTradeExecutor (real PMT webhook)
#   LIVE_TRADING=false → SimulatedBrokerExecutor (no-op / paper mode)
from __future__ import annotations

import datetime
import json
import os
import zoneinfo
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

_ET = zoneinfo.ZoneInfo("America/New_York")

# Session date locked at startup by automation.main (ET date, YYYY-MM-DD).
# Never recalculated mid-session so the folder stays stable across ET midnight.
_SESSION_DATE: str = ""


def set_session_date(d: str) -> None:
    global _SESSION_DATE
    _SESSION_DATE = d


def _now_et() -> str:
    return datetime.datetime.now(_ET).isoformat()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(event: dict) -> None:
    """Append one JSON line to sessions/{today}/events.jsonl.

    kind and time are always written first; remaining fields follow alphabetically.
    """
    today = _SESSION_DATE or datetime.datetime.now(_ET).date().isoformat()
    path = Path("sessions") / today / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered: dict = {"kind": event.get("kind", ""), "time": event.get("time", "")}
    ordered.update(sorted((k, v) for k, v in event.items() if k not in ("kind", "time")))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(ordered) + "\n")


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
    now = _now_et()
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
    _log({"kind": "new-stop-entry", "time": now, "direction": direction,
          "entry_price": entry_price, "stop_price": stop_price})


def _current_price() -> float:
    """Estimate current market price from the last bar_state midpoint."""
    from smt_state import load_bar_state
    bar = load_bar_state()
    if bar and "potential_stop_long" in bar and "potential_stop_short" in bar:
        return (float(bar["potential_stop_long"]) + float(bar["potential_stop_short"])) / 2.0
    return 0.0


def place_market_entry(direction: str, entry_price: float, stop_price: float) -> None:
    """Enter at market with stop. Logs, dispatches MKT+sl, writes active to position.json."""
    now = _now_et()
    pmt_signal = {
        "direction": direction,
        "entry_price": entry_price,
        "stop_price": stop_price,
    }
    _executor.place_entry(pmt_signal, None)
    fill_price = entry_price if entry_price != 0.0 else _current_price()
    pos = _load_pos()
    pos["active"] = {
        "direction": direction,
        "fill_price": fill_price,
        "stop": stop_price,
        "cautious": "no",
        "contracts": 2,
        "time": now,
    }
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    _save_pos(pos)
    _log({"kind": "market-entry", "time": now, "direction": direction,
          "entry_price": entry_price, "stop_price": stop_price})


def move_stop_entry(new_entry_price: float, new_stop_price: float, direction: str) -> None:
    """Cancel existing unfilled stop entry and replace. Reads old entry_price from position.json."""
    now = _now_et()
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
    _log({"kind": "move-stop-entry", "time": now, "direction": direction,
          "new_entry_price": new_entry_price, "new_stop_price": new_stop_price,
          "old_entry_price": old_entry})


def stop_entry_filled(direction: str, stop_price: float) -> None:
    """Stop entry just filled — send protective S/L to PMT, log, update active.stop in position.json."""
    now = _now_et()
    _executor.update_stop_loss({"direction": direction, "stop_price": stop_price}, None)
    pos = _load_pos()
    if pos.get("active"):
        pos["active"]["stop"] = stop_price
        _save_pos(pos)
    else:
        print("[live_orders] stop_entry_filled: active position absent — position.json not updated", flush=True)
    _log({"kind": "stop-entry-filled", "time": now, "direction": direction, "stop_price": stop_price})


def cancel_stop_entry(reason: str = "user-requested", force: bool = False) -> None:
    """Cancel pending stop entry. No-op if stop_entry is empty (unless force=True). Logs, dispatches close, clears position.json."""
    pos = _load_pos()
    if not force and not pos.get("stop_entry"):
        return
    now = _now_et()
    entry_price = float(pos["stop_entry"]) if pos.get("stop_entry") else 0.0
    _executor.place_close("cancel-stop")
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos["confirmation_bar"] = {}
    _save_pos(pos)
    _log({"kind": "cancel-stop-entry", "time": now, "entry_price": entry_price, "reason": reason})


def close_position(price: float, reason: str = "user-requested") -> None:
    """Market-close active position. Logs, dispatches close, clears active in position.json."""
    now = _now_et()
    _executor.place_close("close")
    pos = _load_pos()
    pos["active"] = {}
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos["confirmation_bar"] = {}
    _save_pos(pos)
    _log({"kind": "market-close", "time": now, "price": float(price), "reason": reason})


def update_stop_loss(stop_price: float, reason: str = "user-requested", direction: str | None = None) -> None:
    """Update protective stop on active position. Logs, dispatches update_sl, updates active.stop.

    direction: override the direction read from position.json (used with --force when active is empty).
    """
    now = _now_et()
    pos = _load_pos()
    resolved = direction if direction is not None else pos.get("active", {}).get("direction", "")
    _executor.update_stop_loss({"direction": resolved, "stop_price": stop_price}, None)
    if pos.get("active"):
        pos["active"]["stop"] = stop_price
        _save_pos(pos)
    _log({"kind": "update-stop-loss", "time": now, "reason": reason, "stop_price": stop_price})


# ---------------------------------------------------------------------------
# Pipeline dispatch — single entry point for all automatic signals
# ---------------------------------------------------------------------------

def dispatch(sig: dict) -> None:
    """Route a pipeline signal to log + executor. Called by SmtV2Dispatcher._emit().

    Direction mapping: pipeline uses "up"/"down", live_orders uses "long"/"short".
    For manual triggers (trade.py), call the specific functions directly instead.
    """
    kind = sig.get("kind")
    direction_v2 = sig.get("direction", "none")
    direction = "long" if direction_v2 == "up" else ("short" if direction_v2 == "down" else None)

    if kind == "new-stop-entry":
        stop = sig.get("stop")
        if direction and stop is not None:
            place_stop_entry(direction, float(sig["price"]), float(stop))
        return

    if kind == "move-stop-entry":
        stop = sig.get("stop")
        if direction and stop is not None:
            move_stop_entry(float(sig["price"]), float(stop), direction)
        return

    if kind == "stop-entry-filled":
        stop = sig.get("stop")
        if direction and stop is not None:
            stop_entry_filled(direction, float(stop))
        return

    if kind == "market-entry":
        stop = sig.get("stop")
        if direction and stop is not None:
            place_market_entry(direction, float(sig["price"]), float(stop))
        return

    if kind == "market-close":
        close_position(float(sig.get("price", 0.0)), sig.get("reason", "strategy"))
        return

    if kind == "stop-exit":
        # Cautious stop-exit: IB stop already fired in normal flow; this is a safety-net
        # market-close in case the stop order didn't execute (e.g. connectivity gap).
        # trend.py has already cleared position+hypothesis in position.json before emitting.
        _executor.place_close("close")
        _log(sig)
        return

    if kind == "new-stop-exit":
        # Move the protective stop to the cautious break price so IB handles the exit
        # automatically when price reverses through it.
        cbp = sig.get("cautious_break_price")
        if cbp is None:
            cbp = _load_pos().get("active", {}).get("cautious_break_price")
        if cbp is not None:
            update_stop_loss(float(cbp), reason="new-stop-exit")
        _log(sig)
        return

    if kind == "move-stop-exit":
        # Trailing stop update: slide the IB stop to the new tighter break price.
        cbp = sig.get("cautious_break_price")
        if cbp is not None:
            update_stop_loss(float(cbp), reason="move-stop-exit")
        _log(sig)
        return

    if kind == "stop-entry-cancelled":
        # position.json already cleared by the pipeline before emitting this signal;
        # bypass the stop_entry guard and send the broker cancel directly.
        _executor.place_close("cancel-stop")
        _log(sig)
        return

    if kind == "stopped-out":
        # IB's protective stop already executed — clear position state and log.
        pos = _load_pos()
        if pos.get("active", {}).get("cautious", "no") not in ("no", ""):
            # Cautious stop fired: also reset hypothesis so the strategy doesn't re-enter
            # on a stale direction. (Non-cautious stops keep the hypothesis alive for re-entry.)
            from smt_state import load_hypothesis, save_hypothesis
            hyp = load_hypothesis()
            hyp["direction"] = "none"
            save_hypothesis(hyp)
        pos["active"] = {}
        pos["stop_entry"] = ""
        pos["stop_direction"] = ""
        pos["confirmation_bar"] = {}
        _save_pos(pos)
        _log(sig)
        return

    # All other signal kinds (new-hypothesis, trend-broken, level-swept,
    # smt-div, ath-crossed, dynamic-ath-crossed, …): log only.
    _log(sig)


# ---------------------------------------------------------------------------
# Manual commands
# ---------------------------------------------------------------------------

def trend_broken() -> dict:
    """Reset hypothesis direction to 'none', cancel any pending stop entry, log trend-broken.

    Calls cancel_stop_entry BEFORE clearing position state so the broker cancel
    actually fires (cancel_stop_entry is a no-op if stop_entry is already empty).
    The next 5-minute bar will form a fresh hypothesis via the normal pipeline.

    Usage:
        import live_orders; live_orders.trend_broken()
        python trade.py trend-broken
    """
    from smt_state import load_hypothesis, save_hypothesis

    hypothesis = load_hypothesis()
    broken_dir = hypothesis.get("direction", "none")

    # Cancel broker order first, while stop_entry is still set in position.json.
    cancel_stop_entry(reason="trend-broken-manual")

    hypothesis["direction"] = "none"
    save_hypothesis(hypothesis)

    # Clear confirmation_bar in case cancel_stop_entry didn't (no pending stop).
    pos = _load_pos()
    pos["confirmation_bar"] = {}
    _save_pos(pos)

    event = {
        "kind":             "trend-broken",
        "time":             _now_et(),
        "broken_direction": broken_dir,
        "source":           "manual",
    }
    _log(event)
    print(
        f"[live_orders] trend-broken fired (was {broken_dir!r}) — "
        "next 5m bar will form a fresh hypothesis.",
        flush=True,
    )
    return event


def hypothesis() -> list:
    """Force a fresh hypothesis evaluation regardless of current direction.

    Resets hypothesis direction to 'none', loads the latest bar parquets from disk,
    calls run_hypothesis, and logs all resulting signals to events.jsonl.

    Usage:
        import live_orders; live_orders.hypothesis()
        python trade.py hypothesis
    """
    import pandas as pd
    from pathlib import Path as _Path
    import hypothesis as _hyp_mod
    from smt_state import load_hypothesis, save_hypothesis

    now      = datetime.datetime.now(_ET)
    now_ts   = pd.Timestamp(now)
    today    = now.date()
    now_str  = now.isoformat()

    hyp     = load_hypothesis()
    old_dir = hyp.get("direction", "none")
    hyp["direction"] = "none"
    save_hypothesis(hyp)

    mnq_path = _Path("data/MNQ_1m.parquet")
    mes_path = _Path("data/MES_1m.parquet")
    if not mnq_path.exists() or not mes_path.exists():
        print("[live_orders] hypothesis: bar parquets not found — is the orchestrator running?", flush=True)
        return []

    hist_mnq_1m = pd.read_parquet(mnq_path)
    hist_mes_1m = pd.read_parquet(mes_path)
    today_mnq   = hist_mnq_1m[hist_mnq_1m.index.date == today]
    today_mes   = hist_mes_1m[hist_mes_1m.index.date == today]

    _agg     = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    _14d_ago = now_ts - pd.Timedelta(days=14)
    hist_1hr = (
        hist_mnq_1m[hist_mnq_1m.index >= _14d_ago]
        .resample("1h", label="left").agg(_agg).dropna(subset=["Open"])
    )
    hist_4hr = (
        hist_mnq_1m
        .resample("4h", label="left").agg(_agg).dropna(subset=["Open"])
    )

    signals = _hyp_mod.run_hypothesis(
        now, today_mnq, today_mes,
        hist_mnq_1m, hist_mes_1m,
        hist_1hr=hist_1hr, hist_4hr=hist_4hr,
        skip_position_reset=True,
    )

    if signals:
        for sig in signals:
            _log(dict(sig, source="manual", time=now_str))
            print(
                f"[live_orders] hypothesis {sig.get('kind')} "
                f"direction={sig.get('direction', '?')} "
                f"cautious_initial={sig.get('cautious_price_initial', '?')}",
                flush=True,
            )
    else:
        print(f"[live_orders] hypothesis: no hypothesis formed (was {old_dir!r})", flush=True)

    return signals or []
