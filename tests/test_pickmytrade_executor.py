# tests/test_pickmytrade_executor.py
# Unit tests for PickMyTradeExecutor — HTTP calls mocked on the executor's _http instance.
from unittest.mock import MagicMock

import pandas as pd
import pytest

from execution.pickmytrade import PickMyTradeExecutor
from execution.protocol import FillRecord
from strategy_smt import _BarRow


@pytest.fixture(autouse=True)
def _fast_executor_env(monkeypatch):
    """Strip the two per-test wall-clock costs in this module (GIL-28 perf cleanup):

    1. httpx.Client() construction (SSL context / CA-bundle load) costs ~1.7s EVERY
       instantiation, and PickMyTradeExecutor builds one in __init__ per _make_executor()
       call — this dominated the module's runtime (~48s for 50 tests). Every test mocks the
       HTTP layer (sets ex._http.post), so the real client is never used; replace it with a
       MagicMock factory.
    2. The exponential retry backoff (time.sleep(2**attempt)) on the retry path.
    """
    import execution.pickmytrade as _mod
    monkeypatch.setattr(_mod.httpx, "Client", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(_mod.time, "sleep", lambda *a, **kw: None)


def _make_executor(entry_slip_ticks: int = 2, account_ids: list = None) -> PickMyTradeExecutor:
    return PickMyTradeExecutor(
        webhook_url="https://pmt.example.com/signal",
        api_key="test-key",
        symbol="MNQ1!",
        account_ids=account_ids if account_ids is not None else ["ACC123"],
        contracts=1,
        entry_slip_ticks=entry_slip_ticks,
    )


def _bar() -> _BarRow:
    ts = pd.Timestamp("2026-04-30 10:00:00", tz="America/New_York")
    return _BarRow(20000.0, 20005.0, 19995.0, 20000.0, 100.0, ts)


def _signal(direction: str = "long", limit: bool = False) -> dict:
    sig = {
        "direction": direction,
        "entry_price": 20000.0,
        "stop_price": 19980.0,
        "take_profit": 20040.0,
    }
    if limit:
        sig["stop_fill_bars"] = 3
    return sig


def _position(direction: str = "long") -> dict:
    return {
        "direction": direction,
        "assumed_entry": 20000.0,
        "contracts": 1,
        "stop_price": 19980.0,
        "take_profit": 20040.0,
    }


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "OK"
    return resp


def _server_error_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 500
    resp.text = "Internal Server Error"
    return resp


def _drain(ex: PickMyTradeExecutor) -> None:
    """Flush the order thread pool so all async dispatches complete before assertions."""
    ex._order_pool.shutdown(wait=True, cancel_futures=False)


def test_place_entry_long_posts_buy_market_order():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert ex._http.post.call_count == 1
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "buy"
    assert payload["order_type"] == "MKT"
    assert "price" not in payload  # PMT uses latest close; sending price adds unwanted slippage


def test_place_entry_short_posts_sell_market_order():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("short"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "sell"
    assert payload["order_type"] == "MKT"
    assert "price" not in payload


def test_place_entry_stop_posts_stop_order():
    ex = _make_executor()
    sig = _signal("long", limit=True)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(sig, _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["order_type"] == "STP"
    assert payload["gtd_in_second"] == 0
    assert payload["price"] == sig["entry_price"]


# ---------------------------------------------------------------------------
# R1: STP->MKT downgrade only when the trigger is actually reached.
# The returned FillRecord.order_type reflects the downgrade decision (it is set
# before the entry-window check and preserved on every return path), so these
# assertions are independent of the wall-clock entry window.
# ---------------------------------------------------------------------------

def _stop_signal(direction: str, entry_price: float, current_price: float) -> dict:
    """A stop entry (stop_fill_bars set) carrying the current market price."""
    return {
        "direction":     direction,
        "entry_price":   entry_price,
        "stop_price":    entry_price - 20.0 if direction == "long" else entry_price + 20.0,
        "stop_fill_bars": 3,
        "current_price": current_price,
    }


def test_r1_long_below_trigger_stays_stop():
    # LONG stop, market BELOW the trigger (the 00:30 case) -> NOT downgraded, rests as STP.
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_stop_signal("long", entry_price=30498.5, current_price=30495.6), _bar())
    _drain(ex)
    assert rec.order_type == "stop"


def test_r1_long_at_or_above_trigger_downgrades():
    # LONG stop, market AT/ABOVE the trigger -> Tradovate would reject the resting stop -> MKT.
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_stop_signal("long", entry_price=30498.5, current_price=30499.0), _bar())
    _drain(ex)
    assert rec.order_type == "market"


def test_r1_short_above_trigger_stays_stop():
    # SHORT stop, market ABOVE the trigger -> NOT downgraded, rests as STP.
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_stop_signal("short", entry_price=30532.25, current_price=30536.0), _bar())
    _drain(ex)
    assert rec.order_type == "stop"


def test_r1_short_at_or_below_trigger_downgrades():
    # SHORT stop, market AT/BELOW the trigger -> MKT.
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_stop_signal("short", entry_price=30532.25, current_price=30532.25), _bar())
    _drain(ex)
    assert rec.order_type == "market"


# ---------------------------------------------------------------------------
# O2/D1: STP->MKT downgrade keys off the bar EXTREME (high for longs, low for shorts),
# not just the lagging close — a stop the live market already touched intrabar must go MKT
# instead of resting as a STP that Tradovate rejects (2026-06-11, four rejected brackets).
# bar_high/bar_low absent (0.0) -> falls back to the close-only behavior above.
# ---------------------------------------------------------------------------

def test_stp_mkt_long_downgrades_on_bar_high_even_if_close_below():
    # 2026-06-11 12:45: entry 28896.25, close 28885.5 (below) but bar high 28898 reached it.
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    sig = _stop_signal("long", entry_price=28896.25, current_price=28885.5)
    sig["bar_high"], sig["bar_low"] = 28898.0, 28872.75
    rec = ex.place_entry(sig, _bar())
    _drain(ex)
    assert rec.order_type == "market"


def test_stp_mkt_short_downgrades_on_bar_low_even_if_close_above():
    # 2026-06-11 13:55: entry 29174.0, close 29184.75 (above) but bar low 29163.75 reached it.
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    sig = _stop_signal("short", entry_price=29174.0, current_price=29184.75)
    sig["bar_high"], sig["bar_low"] = 29192.0, 29163.75
    rec = ex.place_entry(sig, _bar())
    _drain(ex)
    assert rec.order_type == "market"


def test_stp_mkt_long_stays_stop_when_neither_close_nor_bar_high_reach():
    # Genuine resting long stop: trigger above BOTH the close and the bar high -> stays STP.
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    sig = _stop_signal("long", entry_price=28900.0, current_price=28885.0)
    sig["bar_high"], sig["bar_low"] = 28895.0, 28870.0
    rec = ex.place_entry(sig, _bar())
    _drain(ex)
    assert rec.order_type == "stop"


def test_gtd_in_second_present_on_market_order():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["gtd_in_second"] == 0


def test_place_entry_returns_fill_record():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    result = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert isinstance(result, FillRecord)
    assert result.status == "filled"
    assert result.direction == "long"


def test_pmt_market_entry_long_slippage():
    # MKT always 3 ticks regardless of entry_slip_ticks
    # (assumed_fill_price market branch is hardcoded to 3 ticks — protocol.py L29-30,
    # calibrated from live PMT relay observations in commit e7ee2b3 / D3).
    ex = _make_executor(entry_slip_ticks=2)
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert rec.fill_price == pytest.approx(20000.0 + 3 * 0.25)


def test_pmt_market_entry_short_slippage():
    # MKT always 3 ticks regardless of entry_slip_ticks (protocol.py L29-30; D3 calibration).
    ex = _make_executor(entry_slip_ticks=2)
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_signal("short"), _bar())
    _drain(ex)
    assert rec.fill_price == pytest.approx(20000.0 - 3 * 0.25)


def test_pmt_stop_entry_before_1100_applies_4tick_slippage():
    # _bar() timestamp is 10:00 ET → before 11:00 → 4 ticks slippage
    ex = _make_executor(entry_slip_ticks=2)
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_signal("long", limit=True), _bar())
    _drain(ex)
    assert rec.fill_price == pytest.approx(20000.0 + 4 * 0.25)


def test_pmt_zero_slip_ticks_mkt_still_applies_3ticks():
    # MKT orders ignore entry_slip_ticks — always 3 ticks (protocol.py L29-30; D3 calibration).
    ex = _make_executor(entry_slip_ticks=0)
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert rec.fill_price == pytest.approx(20000.0 + 3 * 0.25)


def test_place_exit_long_posts_sell_close():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_exit(_position("long"), "exit_tp", _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "close"


def test_place_exit_short_posts_buy_close():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_exit(_position("short"), "exit_tp", _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "close"


def test_modify_stop_entry_cancels_old_when_placed_at_broker_cross_process():
    """D6: modify_stop_entry cancels the old order even when _entry_is_live is False
    (a separate trade.py CLI process), as long as placed_at_broker=True (the order is
    really working at the broker per position.json). Pre-fix it skipped the cancel →
    a DUPLICATE resting order (2026-06-11 09:19)."""
    ex = _make_executor()
    ex._entry_is_live = False  # fresh CLI process — did not place the entry itself
    ex._http.post = MagicMock(return_value=_ok_response())
    old = _stop_signal("long", 20000.0, 19990.0)
    new = _stop_signal("long", 19980.0, 19990.0)
    ex.modify_stop_entry(old, new, _bar(), placed_at_broker=True)
    _drain(ex)
    posted = [c.kwargs["json"] for c in ex._http.post.call_args_list]
    assert any(p.get("data") == "close" for p in posted), "old broker order must be cancelled"
    assert any(p.get("order_type") == "STP" for p in posted), "new STP must be placed"


def test_modify_stop_entry_no_cancel_when_unplaced_cross_process():
    """D6 guard: with _entry_is_live False AND placed_at_broker False (truly unplaced),
    modify_stop_entry must NOT send a cancel — there is no broker order to cancel."""
    ex = _make_executor()
    ex._entry_is_live = False
    ex._http.post = MagicMock(return_value=_ok_response())
    old = _stop_signal("long", 20000.0, 19990.0)
    new = _stop_signal("long", 19980.0, 19990.0)
    ex.modify_stop_entry(old, new, _bar(), placed_at_broker=False)
    _drain(ex)
    posted = [c.kwargs["json"] for c in ex._http.post.call_args_list]
    assert not any(p.get("data") == "close" for p in posted), "must not cancel a non-existent order"


def test_place_exit_returns_none():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    result = ex.place_exit(_position("long"), "exit_tp", _bar())
    _drain(ex)
    assert result is None


def test_order_retries_on_500():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_server_error_response())
    import execution.pickmytrade as _mod
    orig_sleep = _mod.time.sleep
    _mod.time.sleep = lambda _: None
    try:
        ex.place_entry(_signal("long"), _bar())
        _drain(ex)
    finally:
        _mod.time.sleep = orig_sleep
    assert ex._http.post.call_count == ex._max_retries


def test_order_does_not_raise_on_failure():
    import httpx as _httpx
    import execution.pickmytrade as _mod
    ex = _make_executor()
    ex._http.post = MagicMock(side_effect=_httpx.ConnectError("unreachable"))
    orig_sleep = _mod.time.sleep
    _mod.time.sleep = lambda _: None
    try:
        ex.place_entry(_signal("long"), _bar())
        _drain(ex)  # must not propagate any exception
    finally:
        _mod.time.sleep = orig_sleep


def test_start_raises_if_env_missing():
    ex = PickMyTradeExecutor(
        webhook_url="",
        api_key="",
        symbol="MNQ1!",
        account_ids=["ACC123"],
        contracts=1,
    )
    with pytest.raises(RuntimeError):
        ex.start()


def test_stop_shuts_down_cleanly():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.start()
    ex.stop()
    # After stop, the pool is shut down — no threads alive, no exception raised


def test_market_entry_includes_sl():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["sl"] == 19980.0


def test_stop_entry_sends_real_sl():
    # Long STP: real stop_price from signal (19980.0 from _signal())
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long", limit=True), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["sl"] == pytest.approx(19980.0)

    # Short STP: real stop_price from signal
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("short", limit=True), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["sl"] == pytest.approx(19980.0)


def test_modify_stop_entry_includes_sl():
    ex = _make_executor()
    # An entry must already be live at the broker for the cancel-then-replace path to run.
    # The guard at pickmytrade.py L232 (`if not self._entry_is_live and not placed_at_broker`)
    # short-circuits to a fresh place_entry when there is nothing to cancel; set
    # _entry_is_live=True to model the in-orchestrator move of a working STP order.
    ex._entry_is_live = True
    ex._http.post = MagicMock(return_value=_ok_response())
    old = _signal("long", limit=True)
    new = {**_signal("long", limit=True), "entry_price": 20050.0}
    ex.modify_stop_entry(old, new, _bar())
    _drain(ex)
    # Two HTTP calls: synchronous cancel (close) + async new STP
    assert ex._http.post.call_count == 2
    payload = ex._http.post.call_args.kwargs["json"]  # last call = new STP
    assert payload["order_type"] == "STP"
    assert payload["price"] == pytest.approx(20050.0)
    assert payload["sl"] == pytest.approx(19980.0)


def test_market_entry_includes_multiple_accounts():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["multiple_accounts"][0]["account_id"] == "ACC123"


def test_multiple_account_ids_all_appear_in_payload():
    ex = _make_executor(account_ids=["ACC111", "ACC222", "ACC333"])
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    ids = [a["account_id"] for a in payload["multiple_accounts"]]
    assert ids == ["ACC111", "ACC222", "ACC333"]
    assert all(a["token"] == "test-key" for a in payload["multiple_accounts"])
    assert all(a["risk_percentage"] == 0 for a in payload["multiple_accounts"])
    assert all(a["quantity_multiplier"] == 1 for a in payload["multiple_accounts"])


def test_token_in_payload_toplevel():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["token"] == "test-key"


def test_no_bearer_header():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    headers = ex._http.post.call_args.kwargs.get("headers", {})
    assert "Authorization" not in headers


def test_risk_percentage_zero_in_all_payloads():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["risk_percentage"] == 0
    assert payload["multiple_accounts"][0]["risk_percentage"] == 0


def test_update_stop_loss_long():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    pos = _position("long")
    status, body = ex.update_stop_loss(pos, _bar())
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "buy"
    assert payload["order_type"] == "MKT"
    assert payload["quantity"] == 1   # uses self._contracts, not overridden to 0
    assert payload["update_sl"] is True
    assert payload["sl"] == 19980.0
    assert "pyramid" not in payload
    assert "same_direction_ignore" not in payload
    assert status == 200
    assert body == "OK"


def test_update_stop_loss_short():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    pos = _position("short")
    status, body = ex.update_stop_loss(pos, _bar())
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "sell"
    assert payload["order_type"] == "MKT"
    assert payload["quantity"] == 1
    assert payload["update_sl"] is True
    assert payload["sl"] == 19980.0
    assert status == 200


def test_place_close_sends_data_close():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_close()
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "close"


def test_place_exit_delegates_to_close():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    for exit_type in ("exit_tp", "exit_stop", "exit_market"):
        ex._http.post.reset_mock()
        ex.place_exit(_position("long"), exit_type, _bar())
        _drain(ex)
        payload = ex._http.post.call_args.kwargs["json"]
        assert payload["data"] == "close"


def test_modify_stop_entry_sends_close_then_stop():
    ex = _make_executor()
    ex._entry_is_live = True  # working STP at broker -> cancel-then-replace path (L232)
    ex._http.post = MagicMock(return_value=_ok_response())
    old_sig = _signal("long", limit=True)
    new_sig = {**_signal("long", limit=True), "entry_price": 20010.0}
    ex.modify_stop_entry(old_sig, new_sig, _bar())
    _drain(ex)
    assert ex._http.post.call_count == 2
    first_payload = ex._http.post.call_args_list[0].kwargs["json"]
    second_payload = ex._http.post.call_args_list[1].kwargs["json"]
    assert first_payload["data"] == "close"
    assert second_payload["data"] == "buy"
    assert second_payload["order_type"] == "STP"
    assert second_payload["price"] == 20010.0


def test_modify_stop_entry_close_is_synchronous():
    """Close step in modify_stop_entry must run synchronously, not via thread pool."""
    ex = _make_executor()
    call_order = []

    ex._entry_is_live = True  # working STP at broker -> cancel-then-replace path (L232)

    original_post = ex._post_order
    def tracked_post(order_id, payload):
        call_order.append(("direct", payload["data"]))
        return original_post(order_id, payload)

    original_submit = ex._order_pool.submit
    def tracked_submit(fn, *args, **kwargs):
        call_order.append(("pool", args[1]["data"] if len(args) > 1 else "?"))
        return original_submit(fn, *args, **kwargs)

    ex._post_order = tracked_post
    ex._http.post = MagicMock(return_value=_ok_response())
    ex._order_pool.submit = tracked_submit

    old_sig = _signal("long", limit=True)
    new_sig = {**_signal("long", limit=True), "entry_price": 20010.0}
    ex.modify_stop_entry(old_sig, new_sig, _bar())
    _drain(ex)

    # First call must be direct (synchronous close), second via pool (async re-place)
    assert call_order[0] == ("direct", "close")
    assert call_order[1][0] == "pool"


def test_pmt_stop_entry_after_1100_applies_1tick_slippage():
    # Bar timestamp at 11:30 ET → at or after 11:00 → 1 tick slippage for STP
    # (assumed_fill_price stop branch, post-11:00 ET — protocol.py L31-36; D3 calibration
    # in commit e7ee2b3 lowered the post-11:00 stop slippage from 2 ticks to 1).
    ts_after = pd.Timestamp("2026-04-30 11:30:00", tz="America/New_York")
    bar_after = _BarRow(20000.0, 20005.0, 19995.0, 20000.0, 100.0, ts_after)
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_signal("long", limit=True), bar_after)
    _drain(ex)
    assert rec.fill_price == pytest.approx(20000.0 + 1 * 0.25)


def test_pmt_place_entry_passes_bar_time_to_assumed_fill_price():
    # Verifies bar_time is used: STP fill should differ from reference_price
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_signal("long", limit=True), _bar())
    _drain(ex)
    # bar is at 10:00 ET → 4-tick STP slippage → fill != reference price
    assert rec.fill_price != pytest.approx(20000.0)
    assert rec.fill_price == pytest.approx(20000.0 + 4 * 0.25)


def test_modify_stop_entry_replaces_even_if_close_fails():
    import httpx as _httpx
    import execution.pickmytrade as _mod
    ex = _make_executor()
    ex._entry_is_live = True  # working STP at broker -> cancel-then-replace path (L232)
    orig_sleep = _mod.time.sleep
    _mod.time.sleep = lambda _: None
    post_calls = []
    def mock_post(*args, **kwargs):
        payload = kwargs.get("json", {})
        post_calls.append(payload.get("data"))
        if payload.get("data") == "close":
            raise _httpx.ConnectError("network error")
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "OK"
        return resp
    ex._http.post = mock_post
    try:
        old_sig = _signal("long", limit=True)
        new_sig = {**_signal("long", limit=True), "entry_price": 20010.0}
        ex.modify_stop_entry(old_sig, new_sig, _bar())
        _drain(ex)
    finally:
        _mod.time.sleep = orig_sleep
    assert "close" in post_calls
    assert "buy" in post_calls


# ---------------------------------------------------------------------------
# STP->MKT downgrade: assumed fill anchored at the market, not the trigger
# (2026-06-05: trigger-anchoring produced 12-20pt strategy-vs-broker fill gaps)
# ---------------------------------------------------------------------------

def test_stp_mkt_downgrade_fill_anchored_at_market():
    """When the STP downgrades to MKT (trigger already reached), the assumed fill is
    anchored at the CURRENT market price — the broker fills at the market, which may
    sit well past the trigger in a fast move."""
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    sig = _signal("long", limit=True)          # stop_fill_bars set -> STP path
    sig["current_price"] = 20012.0             # market already past the 20000 trigger
    rec = ex.place_entry(sig, _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["order_type"] == "MKT"      # downgraded
    assert rec.order_type == "market"
    # Market order: 3 adverse ticks on top of the market anchor (not the trigger).
    assert rec.fill_price == pytest.approx(20012.0 + 3 * 0.25)
    assert rec.requested_price == pytest.approx(20000.0)  # trigger preserved


def test_resting_stp_fill_still_anchored_at_trigger():
    """A genuinely resting STP (trigger not reached) keeps the trigger anchor —
    the broker will fill it at the trigger when price gets there."""
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    sig = _signal("long", limit=True)
    sig["current_price"] = 19990.0             # below trigger -> rests as STP
    rec = ex.place_entry(sig, _bar())
    _drain(ex)
    assert rec.order_type == "stop"
    # Stop order at 10:00 ET -> 4-tick pre-11:00 slippage anchored at the trigger.
    assert rec.fill_price == pytest.approx(20000.0 + 4 * 0.25)


# ---------------------------------------------------------------------------
# 15:30 ET new-entry cutoff (wall-clock, internal to the PMT executor)
# ---------------------------------------------------------------------------

import datetime as _dt
import execution.pickmytrade as _pmt_mod


def _freeze_pmt_clock(monkeypatch, hh: int, mm: int) -> None:
    """Freeze the module's wall-clock ET 'now' to a fixed HH:MM (today's date).

    Patches execution.pickmytrade.datetime.datetime so now(_ET) returns the frozen
    time while every other datetime/time/timezone use keeps real behavior.
    """
    _real_datetime = _dt.datetime  # capture before patching (datetime module is global)
    _frozen_et = _real_datetime.now(_pmt_mod._ET).replace(
        hour=hh, minute=mm, second=0, microsecond=0
    )

    class _FrozenDateTime(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is _pmt_mod._ET:
                return _frozen_et
            return _real_datetime.now(tz)

    monkeypatch.setattr(_pmt_mod.datetime, "datetime", _FrozenDateTime)


def test_market_entry_allowed_before_cutoff(monkeypatch):
    """15:29 ET -> market entry submitted (status filled, _post_order called)."""
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    _freeze_pmt_clock(monkeypatch, 15, 29)
    rec = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert rec.status == "filled"
    assert ex._entry_is_live is True
    assert ex._http.post.call_count == 1


def test_market_entry_blocked_after_cutoff(monkeypatch):
    """15:31 ET -> market entry blocked (status blocked, no HTTP submit)."""
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    _freeze_pmt_clock(monkeypatch, 15, 31)
    rec = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert rec.status == "blocked"
    assert ex._entry_is_live is False
    assert ex._http.post.call_count == 0


def test_stop_entry_blocked_after_cutoff(monkeypatch):
    """15:31 ET -> stop entry also blocked."""
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    _freeze_pmt_clock(monkeypatch, 15, 31)
    sig = _signal("long", limit=True)
    sig["current_price"] = 19990.0  # below trigger -> rests as STP
    rec = ex.place_entry(sig, _bar())
    _drain(ex)
    assert rec.status == "blocked"
    assert rec.order_type == "stop"
    assert ex._entry_is_live is False
    assert ex._http.post.call_count == 0


def test_entry_allowed_exactly_at_cutoff(monkeypatch):
    """15:30:00 ET exactly -> still allowed (block is strictly AFTER 15:30)."""
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    _freeze_pmt_clock(monkeypatch, 15, 30)
    rec = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert rec.status == "filled"
    assert ex._entry_is_live is True
    assert ex._http.post.call_count == 1


def test_entry_blocked_just_before_close(monkeypatch):
    """16:54 ET -> still inside the 15:30->16:55 pre-close window, entry blocked."""
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    _freeze_pmt_clock(monkeypatch, 16, 54)
    rec = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert rec.status == "blocked"
    assert ex._entry_is_live is False
    assert ex._http.post.call_count == 0


def test_entry_allowed_after_session_close(monkeypatch):
    """16:56 ET -> past the 16:55 close, the pre-close block no longer applies."""
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    _freeze_pmt_clock(monkeypatch, 16, 56)
    rec = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert rec.status == "filled"
    assert ex._entry_is_live is True
    assert ex._http.post.call_count == 1


def test_evening_entry_allowed_new_session(monkeypatch):
    """20:00 ET (new session, opens 18:05) -> entry allowed and submitted.

    This is the regression for the Sunday/overnight-session fix: the 15:30 cutoff must NOT
    suppress entries in the evening of the next session."""
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    _freeze_pmt_clock(monkeypatch, 20, 0)
    rec = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert rec.status == "filled"
    assert ex._entry_is_live is True
    assert ex._http.post.call_count == 1


def test_overnight_entry_allowed(monkeypatch):
    """02:00 ET (overnight, mid-session) -> entry allowed (well before the 15:30 window)."""
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    _freeze_pmt_clock(monkeypatch, 2, 0)
    rec = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert rec.status == "filled"
    assert ex._http.post.call_count == 1


def test_close_allowed_after_cutoff(monkeypatch):
    """15:31 ET -> place_close still works (not gated by the entry cutoff)."""
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    _freeze_pmt_clock(monkeypatch, 15, 31)
    ex.place_close()
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "close"
    assert ex._http.post.call_count == 1


def test_update_stop_loss_allowed_after_cutoff(monkeypatch):
    """15:31 ET -> update_stop_loss still works (not gated by the entry cutoff)."""
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    _freeze_pmt_clock(monkeypatch, 15, 31)
    status, body = ex.update_stop_loss(_position("long"), _bar())
    assert status == 200
    assert ex._http.post.call_count == 1
