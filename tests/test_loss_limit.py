"""Tests for loss_limit.py — the live-only daily realized-loss kill switch.

ACT_GLOBAL_DIR is redirected to a temp dir by the autouse conftest fixture, so the
pause sentinel (general_live_dir()/paused), the loss-limit marker, and the session
events.jsonl all live in temp — no real live state is touched.
"""
from __future__ import annotations

import json

import pytest

import loss_limit
import paths


_SESSION = "2026-06-24"


def _write_events(date: str, events: list[dict]) -> None:
    path = paths.sessions_dir() / date / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _roundtrip(entry_price: float, exit_price: float, direction: str,
               t0: str, t1: str) -> list[dict]:
    return [
        {"kind": "stop-entry-filled", "time": t0, "direction": direction, "price": entry_price},
        {"kind": "market-close", "time": t1, "direction": direction, "price": exit_price},
    ]


@pytest.fixture(autouse=True)
def _one_contract(monkeypatch):
    # 1 contract * $2/pt → pnl_dollars = (exit - entry) * sign * 2, deterministic.
    monkeypatch.setenv("TRADING_CONTRACTS", "1")


# ── realized_session_pnl ──────────────────────────────────────────────────────

def test_realized_session_pnl_none_when_no_events():
    assert loss_limit.realized_session_pnl(_SESSION) is None


def test_realized_session_pnl_sums_clean_roundtrips():
    # long 100→25 = -150 ; long 100→25 = -150 → total -300
    _write_events(_SESSION,
        _roundtrip(100.0, 25.0, "long", "2026-06-24T10:00:00-04:00", "2026-06-24T10:05:00-04:00")
        + _roundtrip(100.0, 25.0, "long", "2026-06-24T11:00:00-04:00", "2026-06-24T11:05:00-04:00"))
    assert loss_limit.realized_session_pnl(_SESSION) == -300.0


def test_realized_session_pnl_excludes_open_position():
    # One closed -150 round-trip + one still-open fill (no exit) → only -150 counts.
    _write_events(_SESSION,
        _roundtrip(100.0, 25.0, "long", "2026-06-24T10:00:00-04:00", "2026-06-24T10:05:00-04:00")
        + [{"kind": "stop-entry-filled", "time": "2026-06-24T12:00:00-04:00",
            "direction": "short", "price": 200.0}])
    assert loss_limit.realized_session_pnl(_SESSION) == -150.0


# ── check_and_pause ───────────────────────────────────────────────────────────

def test_check_and_pause_trips_at_or_below_limit():
    import live_orders
    _write_events(_SESSION,
        _roundtrip(100.0, 25.0, "long", "2026-06-24T10:00:00-04:00", "2026-06-24T10:05:00-04:00")
        + _roundtrip(100.0, 25.0, "long", "2026-06-24T11:00:00-04:00", "2026-06-24T11:05:00-04:00"))
    tripped = loss_limit.check_and_pause(_SESSION, -300.0)
    assert tripped is True
    assert live_orders.is_paused() is True
    assert loss_limit._marker_path().read_text(encoding="utf-8").strip() == _SESSION


def test_check_and_pause_noop_above_limit():
    import live_orders
    _write_events(_SESSION,
        _roundtrip(100.0, 50.0, "long", "2026-06-24T10:00:00-04:00", "2026-06-24T10:05:00-04:00"))  # -100
    tripped = loss_limit.check_and_pause(_SESSION, -300.0)
    assert tripped is False
    assert live_orders.is_paused() is False
    assert not loss_limit._marker_path().exists()


def test_check_and_pause_noop_when_already_paused():
    import live_orders
    live_orders.pause()
    _write_events(_SESSION,
        _roundtrip(100.0, 25.0, "long", "2026-06-24T10:00:00-04:00", "2026-06-24T10:05:00-04:00")
        + _roundtrip(100.0, 25.0, "long", "2026-06-24T11:00:00-04:00", "2026-06-24T11:05:00-04:00"))
    # Already paused (e.g. manual) → do not re-trip / stamp the loss-limit marker.
    assert loss_limit.check_and_pause(_SESSION, -300.0) is False
    assert not loss_limit._marker_path().exists()


# ── clear_stale_pause (per-session auto-clear) ────────────────────────────────

def test_clear_stale_pause_lifts_prior_session_loss_pause():
    import live_orders
    live_orders.pause()
    loss_limit._marker_path().write_text("2026-06-23", encoding="utf-8")  # prior session
    cleared = loss_limit.clear_stale_pause(_SESSION)
    assert cleared is True
    assert live_orders.is_paused() is False
    assert not loss_limit._marker_path().exists()


def test_clear_stale_pause_keeps_current_session_pause():
    import live_orders
    live_orders.pause()
    loss_limit._marker_path().write_text(_SESSION, encoding="utf-8")  # tripped this session
    cleared = loss_limit.clear_stale_pause(_SESSION)
    assert cleared is False
    assert live_orders.is_paused() is True


def test_clear_stale_pause_ignores_manual_pause():
    import live_orders
    live_orders.pause()  # manual pause → no loss-limit marker
    cleared = loss_limit.clear_stale_pause(_SESSION)
    assert cleared is False
    assert live_orders.is_paused() is True
