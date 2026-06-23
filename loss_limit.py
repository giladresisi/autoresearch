# loss_limit.py
# Live-only daily realized-loss kill switch.
#
# Polls the live session's events.jsonl, computes the assumed (script-calculated,
# no broker reconciliation) realized session P&L via rebuild_trades_from_events,
# and engages the manual entry pause (identical to `trade.py pause`) once cumulative
# realized P&L drops to or below the configured threshold.
#
# Backtest-safe by construction: this module is only ever started by automation.main
# (never imported by backtest/regression), and it acts solely through the pause
# sentinel, which smt_state.is_paused() reports as False in in-memory (backtest) mode.
# The strategy code is never touched.
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import paths

# Pause once cumulative realized session P&L (dollars) is at or below this.
DEFAULT_LOSS_LIMIT = -300.0
# How often the background monitor recomputes realized P&L.
DEFAULT_POLL_SECS = 15.0


def _marker_path() -> Path:
    """Global marker recording that the entry-pause was engaged by THIS module (vs a
    manual `trade.py pause`). Content = the session date it tripped for, so a stale
    (prior-session) loss-limit pause is auto-cleared at the next session start while a
    manual pause is left untouched. Lives alongside the pause sentinel."""
    return paths.general_live_dir() / "paused_by_loss_limit"


def realized_session_pnl(session_date: str) -> float | None:
    """Assumed realized P&L (no broker reconciliation) for the session, or None when
    there are no events / no clean round-trips yet.

    Reuses scripts.rebuild_trades_from_events so the number matches the scripts exactly:
    closed round-trips only; open positions are unpaired and contribute nothing.
    """
    events_path = paths.sessions_dir() / session_date / "events.jsonl"
    if not events_path.exists():
        return None
    from scripts.rebuild_trades_from_events import rebuild_trades_from_events
    trades = rebuild_trades_from_events(events_path)
    clean = [t["pnl_dollars"] for t in trades if isinstance(t["pnl_dollars"], (int, float))]
    if not clean:
        return None
    return round(sum(clean), 2)


def clear_stale_pause(session_date: str) -> bool:
    """At session start: lift a loss-limit pause left over from a PRIOR session so the
    cap is per-session. A manual pause (no marker) is left untouched. Returns True iff a
    stale loss-limit pause was cleared."""
    marker = _marker_path()
    if not marker.exists():
        return False
    try:
        tripped_for = marker.read_text(encoding="utf-8").strip()
    except OSError:
        tripped_for = ""
    if tripped_for == session_date:
        return False  # tripped for THIS session (e.g. mid-session restart) — keep paused
    import live_orders
    live_orders.resume()
    try:
        marker.unlink()
    except OSError:
        pass
    return True


def check_and_pause(session_date: str, limit: float = DEFAULT_LOSS_LIMIT) -> bool:
    """Compute realized P&L and engage the pause if at/below `limit` and not already
    paused. Returns True iff this call tripped the pause."""
    import live_orders
    if live_orders.is_paused():
        return False
    pnl = realized_session_pnl(session_date)
    if pnl is None or pnl > limit:
        return False
    if not live_orders.pause():
        return False  # lost a race — someone else paused first
    _marker_path().write_text(session_date, encoding="utf-8")
    print(json.dumps({
        "signal_type": "LOSS_LIMIT_PAUSE",
        "realized_pnl": pnl,
        "limit": limit,
    }), flush=True)
    return True


def run_monitor(session_date: str, limit: float, poll_secs: float,
                stop_event: threading.Event) -> None:
    """Poll until the pause trips (then stop) or `stop_event` is set."""
    while not stop_event.wait(poll_secs):
        try:
            if check_and_pause(session_date, limit):
                return
        except Exception as exc:  # never let the monitor crash the session
            print(f"[loss_limit] monitor error (ignored): {exc}", flush=True)


def start_monitor(session_date: str) -> "threading.Event | None":
    """Start the background loss-limit monitor for `session_date` (daemon thread).

    Returns the stop Event, or None when disabled via LOSS_LIMIT_ENABLED=false.
    Clears any stale prior-session loss-limit pause first.
    """
    if os.environ.get("LOSS_LIMIT_ENABLED", "true").lower() != "true":
        return None
    if clear_stale_pause(session_date):
        print("[loss_limit] cleared stale prior-session loss-limit pause", flush=True)
    limit = float(os.environ.get("LOSS_LIMIT_DOLLARS", DEFAULT_LOSS_LIMIT))
    poll = float(os.environ.get("LOSS_LIMIT_POLL_SECS", DEFAULT_POLL_SECS))
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_monitor, args=(session_date, limit, poll, stop_event),
        daemon=True, name="loss-limit-monitor",
    )
    thread.start()
    print(f"[loss_limit] monitor started (limit=${limit:.0f}, poll={poll:.0f}s)", flush=True)
    return stop_event
