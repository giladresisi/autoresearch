# tests/test_reconcile_on_close.py
# Unit tests for the reconcile-on-close path (GIL-42): the pure decide_correction()
# state machine, the live-only apply_correction() (adopt / suppress_close / resize / noop),
# the synchronous wiring into live_orders.dispatch() for the three close kinds, the
# market-entry adopt-sentinel gate, the broker-unknown degrade, and the offline-inert guard.
#
# No browser: the broker snapshot is injected by monkeypatching
# broker_recon.broker_state.fetch_broker_state. Live paths force live_orders._LIVE = True.

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import live_orders
import paths
from broker_recon import broker_state, recon_on_close


# ---------------------------------------------------------------------------
# Fixtures (mirror test_reconcile.py isolation)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_global_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _reset_state_dir(monkeypatch):
    monkeypatch.delenv("ACT_STATE_DIR", raising=False)
    yield
    paths.set_state_dir(paths._DEFAULT_STATE_DIR)


_FIXED_DATE = "2026-01-15"


@pytest.fixture()
def _session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    live_orders.set_session_date(_FIXED_DATE)
    yield tmp_path
    live_orders.set_session_date("")


@pytest.fixture()
def _live(monkeypatch):
    """Force live mode for the duration of a test (the close-reconcile is live-only)."""
    monkeypatch.setattr(live_orders, "_LIVE", True)
    yield


def _read_events(sessions_dir: Path, date: str) -> list[dict]:
    path = sessions_dir / date / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ===========================================================================
# decide_correction() — pure
# ===========================================================================

def test_decide_flat_flat_is_noop():
    d = recon_on_close.decide_correction({}, {"net_position": 0, "direction": "flat",
                                              "avg_entry": 0.0, "stop_price": None})
    assert d["action"] == "noop"
    assert d["reason"] == "confirmed-flat"


def test_decide_flat_broker_long_adopts():
    broker = {"net_position": 2, "direction": "long", "avg_entry": 30400.0,
              "stop_price": 30380.0}
    d = recon_on_close.decide_correction({}, broker)
    assert d == {"action": "adopt", "direction": "long", "size": 2,
                 "avg_entry": 30400.0, "stop": 30380.0}


def test_decide_long_broker_flat_suppresses_close():
    d = recon_on_close.decide_correction(
        {"direction": "long", "contracts": 2}, {"net_position": 0, "direction": "flat",
                                                "avg_entry": 0.0, "stop_price": None})
    assert d == {"action": "suppress_close", "reason": "broker-flat"}


def test_decide_size_mismatch_same_dir_resizes():
    broker = {"net_position": 3, "direction": "long", "avg_entry": 30400.0,
              "stop_price": 30380.0}
    d = recon_on_close.decide_correction({"direction": "long", "contracts": 2}, broker)
    assert d == {"action": "resize", "size": 3}


def test_decide_broker_unknown_is_noop():
    d = recon_on_close.decide_correction({"direction": "long", "contracts": 2}, None)
    assert d == {"action": "noop", "reason": "broker-unknown"}


def test_decide_dir_mismatch_adopts():
    broker = {"net_position": 2, "direction": "short", "avg_entry": 30400.0,
              "stop_price": 30420.0}
    d = recon_on_close.decide_correction({"direction": "long", "contracts": 2}, broker)
    assert d == {"action": "adopt", "direction": "short", "size": 2,
                 "avg_entry": 30400.0, "stop": 30420.0}


def test_decide_same_dir_same_size_is_noop():
    broker = {"net_position": 2, "direction": "long", "avg_entry": 30400.0,
              "stop_price": 30380.0}
    d = recon_on_close.decide_correction({"direction": "long", "contracts": 2}, broker)
    assert d["action"] == "noop"


# ===========================================================================
# reduce_orders_to_state() — pure (broker_state)
# ===========================================================================

def test_reduce_long_with_stop():
    rows = [
        {"side": "buy", "type": "market", "qty": 2, "avg_fill": 30400.0, "status": "Filled"},
        {"side": "sell", "type": "stop", "stop_price": 30380.0, "status": "Working"},
    ]
    st = broker_state.reduce_orders_to_state(rows, "MNQ1!")
    assert st["net_position"] == 2
    assert st["direction"] == "long"
    assert st["avg_entry"] == pytest.approx(30400.0)
    assert st["stop_price"] == pytest.approx(30380.0)


def test_reduce_flat_when_buys_match_sells():
    rows = [
        {"side": "buy", "type": "market", "qty": 2, "avg_fill": 30400.0, "status": "Filled"},
        {"side": "sell", "type": "market", "qty": 2, "avg_fill": 30410.0, "status": "Filled"},
    ]
    st = broker_state.reduce_orders_to_state(rows, "MNQ1!")
    assert st["net_position"] == 0
    assert st["direction"] == "flat"


def test_reduce_ignores_non_filled_rows():
    rows = [
        {"side": "buy", "type": "market", "qty": 2, "price": 30400.0, "status": "Rejected"},
    ]
    st = broker_state.reduce_orders_to_state(rows, "MNQ1!")
    assert st["net_position"] == 0
    assert st["direction"] == "flat"


def test_reduce_multi_contract_from_parse_row_shape():
    # Guard against the qty-key mismatch: feed rows shaped EXACTLY like _parse_row's output
    # (string-valued cells incl. a populated "qty") with NO hand-supplied integer qty. A real
    # 2-lot must read as net 2, not 1.
    def parse_row_shape(side, qty, fill, status, typ="market", stop=""):
        return {"order_id": "x", "side": side, "type": typ, "price": str(fill),
                "stop_price": str(stop), "status": status, "avg_fill": str(fill),
                "qty": str(qty), "time": ""}
    rows = [
        parse_row_shape("buy", 2, 30400.0, "Filled"),
        parse_row_shape("sell", 2, 30380.0, "Working", typ="stop", stop=30380.0),
    ]
    st = broker_state.reduce_orders_to_state(rows, "MNQ1!")
    assert st["net_position"] == 2
    assert st["direction"] == "long"
    assert st["stop_price"] == pytest.approx(30380.0)


def test_reduce_empty_qty_defaults_to_one():
    # An empty qty cell (DOM attr absent) falls back to 1 contract, not 0.
    rows = [{"order_id": "x", "side": "buy", "type": "market", "price": "30400.0",
             "stop_price": "", "status": "Filled", "avg_fill": "30400.0", "qty": "",
             "time": ""}]
    st = broker_state.reduce_orders_to_state(rows, "MNQ1!")
    assert st["net_position"] == 1
    assert st["direction"] == "long"


# ===========================================================================
# (a) phantom close, broker long -> adopt + suppress + reentry-gate
# ===========================================================================

def test_phantom_close_broker_long_adopts_and_suppresses_reentry(_session, _live):
    seed = {"active": {}, "failed_entries": 1, "cautious_dist_shrinks": 1,
            "stop_entry": "", "stop_direction": ""}
    store = {"pos": dict(seed)}
    broker = {"net_position": 2, "direction": "long", "avg_entry": 30400.0,
              "stop_price": 30380.0}

    place_entry = MagicMock()
    with patch.object(broker_state, "fetch_broker_state", return_value=broker) as fetch, \
         patch("smt_state.load_position", side_effect=lambda: dict(store["pos"])), \
         patch("smt_state.save_position", side_effect=lambda p: store.update(pos=dict(p))), \
         patch("smt_state.load_hypothesis", return_value={}), \
         patch.object(live_orders, "place_market_entry", place_entry):
        suppressed = live_orders._reconcile_on_close({"kind": "stopped-out"})

        assert suppressed is True
        fetch.assert_called_once()
        active = store["pos"]["active"]
        assert active["direction"] == "long"
        assert active["contracts"] == 2
        assert active["fill_price"] == pytest.approx(30400.0)
        assert active["stop"] == pytest.approx(30380.0)
        # phantom side-effects reverted (stopped-out is the only ++ path)
        assert store["pos"]["failed_entries"] == 0
        assert store["pos"]["cautious_dist_shrinks"] == 0
        # cautious ladder re-armed (freeze_active_mgmt wrote mgmt_direction)
        assert active.get("mgmt_direction") == "up"
        # re-entry sentinel set
        assert store["pos"]["recon_suppress_force_entry"] is True

        events = _read_events(_session / "sessions", _FIXED_DATE)
        adopt = [e for e in events if e["kind"] == "recon-adopt"]
        assert len(adopt) == 1
        assert adopt[0]["size"] == 2

        # A subsequent market-entry dispatch is SKIPPED while the sentinel is set, and the
        # sentinel is cleared.
        live_orders.dispatch({"kind": "market-entry", "direction": "up",
                              "price": 30410.0, "stop": 30390.0,
                              "time": "2026-01-15T10:00:00-05:00"})
        place_entry.assert_not_called()
        assert "recon_suppress_force_entry" not in store["pos"]


# ===========================================================================
# (b) broker flat, strategy long -> suppress close-MKT
# ===========================================================================

def test_broker_flat_strategy_long_suppresses_close_mkt(_session, _live):
    seed = {"active": {"direction": "long", "fill_price": 30400.0, "stop": 30380.0,
                       "contracts": 2}, "stop_entry": "x", "stop_direction": "up",
            "conf_bar_entry": {"a": 1}}
    store = {"pos": dict(seed)}
    broker = {"net_position": 0, "direction": "flat", "avg_entry": 0.0, "stop_price": None}

    place_close = MagicMock()
    with patch.object(broker_state, "fetch_broker_state", return_value=broker), \
         patch("smt_state.load_position", side_effect=lambda: dict(store["pos"])), \
         patch("smt_state.save_position", side_effect=lambda p: store.update(pos=dict(p))), \
         patch.object(live_orders._executor, "place_close", place_close):
        live_orders.dispatch({"kind": "market-close", "price": 30400.0,
                              "reason": "strategy"})

    place_close.assert_not_called()
    assert store["pos"]["active"] == {}
    events = _read_events(_session / "sessions", _FIXED_DATE)
    flat = [e for e in events if e["kind"] == "recon-flat"]
    assert len(flat) == 1


def test_real_cautious_stop_resets_hypothesis_when_suppressed(_session, _live):
    # A REAL cautious stop: strategy long+cautious, broker confirmed flat. The reconcile
    # suppresses the safety-net close (suppress_close), but the cautious-stop hypothesis reset
    # (direction="none", manual=False) MUST still run so the strategy doesn't re-enter on a
    # stale direction. Regression guard for the MEDIUM code-review finding.
    seed = {"active": {"direction": "long", "fill_price": 30400.0, "stop": 30380.0,
                       "contracts": 2, "cautious": "primary"}, "stop_entry": "",
            "stop_direction": "up"}
    hyp = {"direction": "up", "manual": True}
    store = {"pos": dict(seed), "hyp": dict(hyp)}
    broker = {"net_position": 0, "direction": "flat", "avg_entry": 0.0, "stop_price": None}

    with patch.object(broker_state, "fetch_broker_state", return_value=broker), \
         patch("smt_state.load_position", side_effect=lambda: dict(store["pos"])), \
         patch("smt_state.save_position", side_effect=lambda p: store.update(pos=dict(p))), \
         patch("smt_state.load_hypothesis", side_effect=lambda: dict(store["hyp"])), \
         patch("smt_state.save_hypothesis", side_effect=lambda h: store.update(hyp=dict(h))):
        live_orders.dispatch({"kind": "stopped-out", "direction": "up",
                              "time": "2026-01-15T10:00:00-05:00"})

    assert store["pos"]["active"] == {}
    assert store["hyp"]["direction"] == "none"
    assert store["hyp"]["manual"] is False


def test_non_cautious_real_stop_keeps_hypothesis(_session, _live):
    # A non-cautious real stop (broker flat) leaves the hypothesis alive for re-entry — the
    # suppress path must not over-reach and reset a non-cautious stop's direction.
    seed = {"active": {"direction": "long", "fill_price": 30400.0, "stop": 30380.0,
                       "contracts": 2, "cautious": "no"}, "stop_entry": "",
            "stop_direction": "up"}
    store = {"pos": dict(seed), "hyp": {"direction": "up", "manual": True}}
    broker = {"net_position": 0, "direction": "flat", "avg_entry": 0.0, "stop_price": None}

    with patch.object(broker_state, "fetch_broker_state", return_value=broker), \
         patch("smt_state.load_position", side_effect=lambda: dict(store["pos"])), \
         patch("smt_state.save_position", side_effect=lambda p: store.update(pos=dict(p))), \
         patch("smt_state.load_hypothesis", side_effect=lambda: dict(store["hyp"])), \
         patch("smt_state.save_hypothesis", side_effect=lambda h: store.update(hyp=dict(h))):
        live_orders.dispatch({"kind": "stopped-out", "direction": "up",
                              "time": "2026-01-15T10:00:00-05:00"})

    assert store["pos"]["active"] == {}
    assert store["hyp"]["direction"] == "up"  # untouched — non-cautious keeps the hypothesis


# ===========================================================================
# (c) entry-STOP path untouched — _reconcile_on_close NEVER invoked
# ===========================================================================

def test_entry_stop_path_untouched(_session, _live):
    place_stop = MagicMock()
    recon = MagicMock(return_value=False)
    with patch.object(live_orders, "_reconcile_on_close", recon), \
         patch.object(live_orders, "place_stop_entry", place_stop):
        live_orders.dispatch({"kind": "new-stop-entry", "direction": "up",
                              "price": 30400.0, "stop": 30380.0,
                              "time": "2026-01-15T10:00:00-05:00"})
    recon.assert_not_called()
    place_stop.assert_called_once()


# ===========================================================================
# (d) market-entry + downgrade gating on the close-reconcile
# ===========================================================================

def test_market_entry_skipped_on_adopt_sentinel(_session, _live):
    store = {"pos": {"active": {"direction": "long", "contracts": 2},
                     "recon_suppress_force_entry": True}}
    place_entry = MagicMock()
    with patch("smt_state.load_position", side_effect=lambda: dict(store["pos"])), \
         patch("smt_state.save_position", side_effect=lambda p: store.update(pos=dict(p))), \
         patch.object(live_orders, "place_market_entry", place_entry):
        live_orders.dispatch({"kind": "market-entry", "direction": "up",
                              "price": 30400.0, "stop": 30380.0,
                              "time": "2026-01-15T10:00:00-05:00"})
    place_entry.assert_not_called()
    assert "recon_suppress_force_entry" not in store["pos"]


def test_market_entry_proceeds_without_sentinel(_session, _live):
    store = {"pos": {"active": {}}}
    place_entry = MagicMock()
    with patch("smt_state.load_position", side_effect=lambda: dict(store["pos"])), \
         patch("smt_state.save_position", side_effect=lambda p: store.update(pos=dict(p))), \
         patch.object(live_orders, "place_market_entry", place_entry):
        live_orders.dispatch({"kind": "market-entry", "direction": "up",
                              "price": 30400.0, "stop": 30380.0,
                              "time": "2026-01-15T10:00:00-05:00"})
    place_entry.assert_called_once()


def test_downgrade_path_does_not_call_close_reconcile(_session, _live):
    # The STP->MKT downgrade inside place_stop_entry is an ENTRY, not a close — it must NOT
    # invoke the close-reconcile. Drive a downgrade by stubbing the executor to return a
    # market-typed fill record.
    recon = MagicMock(return_value=False)
    rec = MagicMock()
    rec.order_type = "market"
    rec.fill_price = 30400.0
    with patch.object(live_orders, "_reconcile_on_close", recon), \
         patch.object(live_orders, "_spawn_reconcile", MagicMock()), \
         patch.object(live_orders, "_register_downgraded_fill", MagicMock()), \
         patch.object(live_orders._executor, "place_entry", return_value=rec), \
         patch.object(live_orders._executor, "_entry_is_live", True, create=True), \
         patch.object(live_orders._executor, "update_stop_loss", MagicMock()), \
         patch.object(live_orders, "_current_bar_extremes", return_value=(0.0, 0.0)), \
         patch.object(live_orders, "_current_price", return_value=30400.0):
        live_orders.place_stop_entry("long", 30400.0, 30380.0)
    recon.assert_not_called()


# ===========================================================================
# broker-unknown -> noop (close proceeds exactly as today)
# ===========================================================================

def test_broker_unknown_is_noop(_session, _live):
    store = {"pos": {"active": {"direction": "long", "fill_price": 30400.0,
                                "stop": 30380.0, "contracts": 2}}}
    with patch.object(broker_state, "fetch_broker_state", return_value=None), \
         patch("smt_state.load_position", side_effect=lambda: dict(store["pos"])), \
         patch("smt_state.save_position", side_effect=lambda p: store.update(pos=dict(p))):
        suppressed = live_orders._reconcile_on_close({"kind": "market-close"})
    assert suppressed is False
    # active untouched
    assert store["pos"]["active"]["direction"] == "long"


# ===========================================================================
# offline-inert — guards the byte-identical-regression claim
# ===========================================================================

def test_offline_inert(monkeypatch):
    monkeypatch.setattr(live_orders, "_LIVE", False)
    fetch = MagicMock()
    monkeypatch.setattr(broker_state, "fetch_broker_state", fetch)
    assert live_orders._reconcile_on_close({"kind": "market-close"}) is False
    fetch.assert_not_called()
