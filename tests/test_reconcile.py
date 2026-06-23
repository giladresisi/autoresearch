# tests/test_reconcile.py
# Unit tests for broker_recon.reconcile (GIL-36): the pure classify() against fixture
# blotter rows (no browser — the reader is behind an interface), plus the correction actions
# (SL_REJECTED → corrective stop, ENTRY_REJECTED → flat, stale-skip, per-position lock).

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import live_orders
import paths
import stop_utils
from broker_recon import reconcile


# ---------------------------------------------------------------------------
# Fixtures (mirror test_live_orders.py isolation)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_global_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _reset_state_dir(monkeypatch):
    monkeypatch.delenv("ACT_STATE_DIR", raising=False)
    yield
    paths.set_state_dir(paths._DEFAULT_STATE_DIR)


@pytest.fixture(autouse=True)
def _reset_recon_state():
    reconcile._reset_state_for_tests()
    # Deterministic, fast timing for the worker.
    monkeyattrs = {"SETTLE_S": 0.0, "POLL_INTERVAL_S": 0.0, "POLL_CAP_S": 0.0}
    saved = {k: getattr(reconcile, k) for k in monkeyattrs}
    for k, v in monkeyattrs.items():
        setattr(reconcile, k, v)
    yield
    for k, v in saved.items():
        setattr(reconcile, k, v)
    reconcile._reset_state_for_tests()


_FIXED_DATE = "2026-01-15"


@pytest.fixture()
def _session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    live_orders.set_session_date(_FIXED_DATE)
    yield tmp_path
    live_orders.set_session_date("")


def _read_events(sessions_dir: Path, date: str) -> list[dict]:
    path = sessions_dir / date / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


class _FakeReader:
    """Stand-in for TradovateOrderReader: returns a fixed blotter, never a browser."""
    def __init__(self, orders, disabled=False):
        self._orders = orders
        self.disabled = disabled
        self.calls = 0

    def query_orders(self, symbol=None, since=None):
        self.calls += 1
        return list(self._orders)


# ---------------------------------------------------------------------------
# classify() — pure
# ---------------------------------------------------------------------------

def test_classify_ok_filled_long_with_resting_stop():
    entry = {"direction": "long", "intended_entry": 30388.75, "symbol": "MNQ1!"}
    orders = [
        {"side": "buy", "type": "market", "price": 30366.25, "avg_fill": 30366.25,
         "status": "Filled"},
        {"side": "sell", "type": "stop", "stop_price": 30346.25, "price": 30346.25,
         "status": "Working"},
    ]
    assert reconcile.classify(entry, 30366.25, orders) == reconcile.OK


def test_classify_sl_rejected_exact_1011_case():
    # The exact 2026-06-17 10:11 case: long entry filled @ 30366.25; the sell-stop @ 30368.75
    # is Rejected → no protective stop rests → SL_REJECTED.
    entry = {"direction": "long", "intended_entry": 30388.75,
             "intended_stop": 30368.75, "symbol": "MNQ1!"}
    orders = [
        {"side": "buy", "type": "market", "price": 30366.25, "avg_fill": 30366.25,
         "status": "Filled"},
        {"side": "sell", "type": "stop", "stop_price": 30368.75, "price": 30368.75,
         "status": "Rejected"},
    ]
    assert reconcile.classify(entry, 30366.25, orders) == reconcile.SL_REJECTED
    # And the corrective the layer computes preserves the 20pt intended risk below the fill.
    corrective = stop_utils.valid_stop_for_fill("long", 30366.25, 30368.75, 30388.75)
    assert corrective == pytest.approx(30346.25)


def test_classify_sl_absent_is_sl_rejected():
    # Entry filled but NO stop row at all → SL_REJECTED.
    entry = {"direction": "long", "intended_entry": 30388.75, "symbol": "MNQ1!"}
    orders = [
        {"side": "buy", "type": "market", "avg_fill": 30366.25, "price": 30366.25,
         "status": "Filled"},
    ]
    assert reconcile.classify(entry, 30366.25, orders) == reconcile.SL_REJECTED


def test_classify_entry_rejected_2000_case():
    # The ex2 20:00 case: the entry order itself is Rejected with no fill → ENTRY_REJECTED.
    entry = {"direction": "long", "intended_entry": 30400.0, "symbol": "MNQ1!"}
    orders = [
        {"side": "buy", "type": "market", "price": 30400.0, "avg_fill": 0.0,
         "status": "Rejected"},
    ]
    assert reconcile.classify(entry, 30400.0, orders) == reconcile.ENTRY_REJECTED


def test_classify_short_sl_rejected():
    entry = {"direction": "short", "intended_entry": 30380.0,
             "intended_stop": 30398.0, "symbol": "MNQ1!"}
    orders = [
        {"side": "sell", "type": "market", "avg_fill": 30400.0, "price": 30400.0,
         "status": "Filled"},
        {"side": "buy", "type": "stop", "stop_price": 30398.0, "price": 30398.0,
         "status": "Rejected"},
    ]
    assert reconcile.classify(entry, 30400.0, orders) == reconcile.SL_REJECTED
    corrective = stop_utils.valid_stop_for_fill("short", 30400.0, 30398.0, 30380.0)
    assert corrective == pytest.approx(30418.0)


def test_classify_short_ok_with_buy_stop_above():
    entry = {"direction": "short", "intended_entry": 30380.0, "symbol": "MNQ1!"}
    orders = [
        {"side": "sell", "type": "market", "avg_fill": 30400.0, "price": 30400.0,
         "status": "Filled"},
        {"side": "buy", "type": "stop", "stop_price": 30418.0, "price": 30418.0,
         "status": "Working"},
    ]
    assert reconcile.classify(entry, 30400.0, orders) == reconcile.OK


def test_classify_empty_orders_is_ok():
    # No blotter rows → nothing actionable → OK (never act on an uncertain state).
    entry = {"direction": "long", "intended_entry": 30400.0, "symbol": "MNQ1!"}
    assert reconcile.classify(entry, 30400.0, []) == reconcile.OK


# ---------------------------------------------------------------------------
# Action: SL_REJECTED → corrective stop via the seam + reconcile-stop-placed event
# ---------------------------------------------------------------------------

def test_action_sl_rejected_places_corrective(_session):
    active_pos = {"active": {"direction": "long", "fill_price": 30366.25, "stop": 30368.75},
                  "stop_entry": "", "stop_direction": ""}
    entry = {"direction": "long", "intended_entry": 30388.75, "intended_stop": 30368.75,
             "symbol": "MNQ1!", "time": "2026-01-15T10:11:00-05:00"}
    orders = [
        {"side": "buy", "type": "market", "avg_fill": 30366.25, "price": 30366.25,
         "status": "Filled"},
        {"side": "sell", "type": "stop", "stop_price": 30368.75, "price": 30368.75,
         "status": "Rejected"},
    ]
    reader = _FakeReader(orders)
    seam = MagicMock()
    saved: dict = {}
    with patch("smt_state.load_position", return_value=dict(active_pos)), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)), \
         patch.object(reconcile, "place_protective_stop", seam):
        reconcile.reconcile_after_entry(entry, 30366.25, reader=reader, _sync=True)

    # Seam called once with the corrective 30346.25.
    seam.assert_called_once()
    assert seam.call_args.args[0] == "long"
    assert seam.call_args.args[1] == pytest.approx(30346.25)

    events = _read_events(_session / "sessions", _FIXED_DATE)
    placed = [e for e in events if e["kind"] == "reconcile-stop-placed"]
    assert len(placed) == 1
    assert placed[0]["price"] == pytest.approx(30346.25)
    assert placed[0]["fill"] == pytest.approx(30366.25)
    assert placed[0]["intended_stop"] == pytest.approx(30368.75)


# ---------------------------------------------------------------------------
# Action: ENTRY_REJECTED → clear to flat + reconcile-flat event (no broker order)
# ---------------------------------------------------------------------------

def test_action_entry_rejected_clears_to_flat(_session):
    active_pos = {"active": {"direction": "long", "fill_price": 30400.0, "stop": 30380.0},
                  "stop_entry": "x", "stop_direction": "up",
                  "conf_bar_entry": {"a": 1}, "conf_bar_exit": {"b": 2}}
    entry = {"direction": "long", "intended_entry": 30400.0, "intended_stop": 30380.0,
             "symbol": "MNQ1!", "time": "2026-01-15T20:00:00-05:00"}
    orders = [
        {"side": "buy", "type": "market", "price": 30400.0, "avg_fill": 0.0,
         "status": "Rejected"},
    ]
    reader = _FakeReader(orders)
    seam = MagicMock()
    saved: dict = {}
    with patch("smt_state.load_position", return_value=dict(active_pos)), \
         patch("smt_state.save_position", side_effect=lambda p: saved.update(p)), \
         patch.object(reconcile, "place_protective_stop", seam):
        reconcile.reconcile_after_entry(entry, 30400.0, reader=reader, _sync=True)

    # No protective-stop placed (ENTRY_REJECTED never touches the seam).
    seam.assert_not_called()
    # position.json cleared to flat.
    assert saved["active"] == {}
    assert saved["stop_entry"] == ""
    assert saved["conf_bar_entry"] == {}

    events = _read_events(_session / "sessions", _FIXED_DATE)
    flat = [e for e in events if e["kind"] == "reconcile-flat"]
    assert len(flat) == 1
    assert flat[0]["entry_price"] == pytest.approx(30400.0)


# ---------------------------------------------------------------------------
# Stale-position skip: position.json no longer shows a matching active → no action
# ---------------------------------------------------------------------------

def test_stale_position_skips(_session):
    flat_pos = {"active": {}, "stop_entry": "", "stop_direction": ""}
    entry = {"direction": "long", "intended_entry": 30388.75, "intended_stop": 30368.75,
             "symbol": "MNQ1!", "time": "2026-01-15T10:11:00-05:00"}
    orders = [
        {"side": "buy", "type": "market", "avg_fill": 30366.25, "price": 30366.25,
         "status": "Filled"},
        {"side": "sell", "type": "stop", "stop_price": 30368.75, "status": "Rejected"},
    ]
    reader = _FakeReader(orders)
    seam = MagicMock()
    with patch("smt_state.load_position", return_value=dict(flat_pos)), \
         patch("smt_state.save_position", side_effect=lambda p: None), \
         patch.object(reconcile, "place_protective_stop", seam):
        reconcile.reconcile_after_entry(entry, 30366.25, reader=reader, _sync=True)

    seam.assert_not_called()
    assert _read_events(_session / "sessions", _FIXED_DATE) == []


# ---------------------------------------------------------------------------
# Per-position lock: concurrent reconciles for the same position correct only once
# ---------------------------------------------------------------------------

def test_per_position_lock_corrects_once(_session):
    active_pos = {"active": {"direction": "long", "fill_price": 30366.25, "stop": 30368.75},
                  "stop_entry": "", "stop_direction": ""}
    entry = {"direction": "long", "intended_entry": 30388.75, "intended_stop": 30368.75,
             "symbol": "MNQ1!", "time": "2026-01-15T10:11:00-05:00"}
    orders = [
        {"side": "buy", "type": "market", "avg_fill": 30366.25, "price": 30366.25,
         "status": "Filled"},
        {"side": "sell", "type": "stop", "stop_price": 30368.75, "status": "Rejected"},
    ]
    seam = MagicMock()

    with patch("smt_state.load_position", return_value=dict(active_pos)), \
         patch("smt_state.save_position", side_effect=lambda p: None), \
         patch.object(reconcile, "place_protective_stop", seam):
        # Run the second call as a real background thread while the first holds the lock,
        # then drive both via the worker directly (sync) to assert idempotency through the
        # handled-set: a second sync run after the first must NOT re-fire the seam.
        reconcile.reconcile_after_entry(entry, 30366.25, reader=_FakeReader(orders), _sync=True)
        reconcile.reconcile_after_entry(entry, 30366.25, reader=_FakeReader(orders), _sync=True)

    assert seam.call_count == 1


def test_per_position_lock_blocks_concurrent(_session):
    # A genuinely concurrent second worker (while the first holds the per-position lock)
    # must short-circuit without classifying/correcting.
    active_pos = {"active": {"direction": "long", "fill_price": 30366.25, "stop": 30368.75},
                  "stop_entry": "", "stop_direction": ""}
    entry = {"direction": "long", "intended_entry": 30388.75, "intended_stop": 30368.75,
             "symbol": "MNQ1!", "time": "2026-01-15T10:11:00-05:00"}
    key = reconcile._position_key(entry)
    held = threading.Lock()
    with reconcile._locks_guard:
        reconcile._position_locks[key] = held
    held.acquire()  # simulate the first worker still running
    try:
        reader = _FakeReader([])
        with patch("smt_state.load_position", return_value=dict(active_pos)):
            reconcile.reconcile_after_entry(entry, 30366.25, reader=reader, _sync=True)
        # The blocked worker returned immediately without ever reading the blotter.
        assert reader.calls == 0
    finally:
        held.release()


# ---------------------------------------------------------------------------
# Disabled reader degrades gracefully (no action, no crash)
# ---------------------------------------------------------------------------

def test_disabled_reader_no_action(_session):
    entry = {"direction": "long", "intended_entry": 30388.75, "intended_stop": 30368.75,
             "symbol": "MNQ1!", "time": "2026-01-15T10:11:00-05:00"}
    seam = MagicMock()
    with patch.object(reconcile, "place_protective_stop", seam):
        reconcile.reconcile_after_entry(
            entry, 30366.25, reader=_FakeReader([], disabled=True), _sync=True)
    seam.assert_not_called()
    assert _read_events(_session / "sessions", _FIXED_DATE) == []


# ---------------------------------------------------------------------------
# Arrow-char guard (GIL-42): a literal '->' arrow (U+2192) inside a broker_recon f-string
# crashed the live orchestrator's cp1252 stdout, killing the reconciler all session. The
# crash sites were printed/logged strings, so the fix sweeps the arrow from every
# broker_recon source. Lock it: no U+2192 may reappear (a copy into a print would re-crash).
# ---------------------------------------------------------------------------

def test_broker_recon_sources_no_arrow_char():
    import broker_recon
    pkg_dir = Path(broker_recon.__file__).parent
    offenders = [py.name for py in sorted(pkg_dir.glob("*.py"))
                 if "→" in py.read_text(encoding="utf-8")]
    assert not offenders, f"U+2192 ('->') remains in broker_recon sources: {offenders}"


def test_broker_recon_modules_import_cleanly():
    # All three new/edited modules import without error (no charmap crash on load).
    import importlib
    for name in ("broker_recon.reader", "broker_recon.reconcile",
                 "broker_recon.tradovate_login", "broker_recon.broker_state",
                 "broker_recon.recon_on_close"):
        importlib.import_module(name)
