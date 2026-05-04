# tests/test_pickmytrade_executor.py
# Unit tests for PickMyTradeExecutor — HTTP calls mocked on the executor's _http instance.
from unittest.mock import MagicMock

import pandas as pd
import pytest

from execution.pickmytrade import PickMyTradeExecutor
from execution.protocol import FillRecord
from strategy_smt import _BarRow


def _make_executor(entry_slip_ticks: int = 2) -> PickMyTradeExecutor:
    return PickMyTradeExecutor(
        webhook_url="https://pmt.example.com/signal",
        api_key="test-key",
        symbol="MNQ1!",
        account_id="ACC123",
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
        sig["limit_fill_bars"] = 3
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


def test_place_entry_limit_posts_limit_order():
    ex = _make_executor()
    sig = _signal("long", limit=True)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(sig, _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["order_type"] == "LMT"
    assert payload["gtd_in_second"] == 0
    assert payload["price"] == sig["entry_price"]


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
    ex = _make_executor(entry_slip_ticks=2)
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert rec.fill_price == pytest.approx(20000.0 + 2 * 0.25)


def test_pmt_market_entry_short_slippage():
    ex = _make_executor(entry_slip_ticks=2)
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_signal("short"), _bar())
    _drain(ex)
    assert rec.fill_price == pytest.approx(20000.0 - 2 * 0.25)


def test_pmt_limit_entry_no_slippage():
    ex = _make_executor(entry_slip_ticks=2)
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_signal("long", limit=True), _bar())
    _drain(ex)
    assert rec.fill_price == pytest.approx(20000.0)


def test_pmt_zero_slip_ticks_fill_at_signal_price():
    ex = _make_executor(entry_slip_ticks=0)
    ex._http.post = MagicMock(return_value=_ok_response())
    rec = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert rec.fill_price == pytest.approx(20000.0)


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
        account_id="ACC123",
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


def test_limit_entry_excludes_sl():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long", limit=True), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert "sl" not in payload


def test_market_entry_includes_multiple_accounts():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["multiple_accounts"][0]["account_id"] == "ACC123"


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


def test_place_stop_after_limit_fill_long():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    pos = _position("long")
    ex.place_stop_after_limit_fill(pos, _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "buy"
    assert payload["quantity"] == 0
    assert payload["update_sl"] is True
    assert payload["pyramid"] is False
    assert payload["same_direction_ignore"] is True
    assert payload["sl"] == 19980.0


def test_place_stop_after_limit_fill_short():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    pos = _position("short")
    ex.place_stop_after_limit_fill(pos, _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "sell"
    assert payload["sl"] == 19980.0


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


def test_modify_limit_entry_sends_close_then_limit():
    ex = _make_executor()
    ex._http.post = MagicMock(return_value=_ok_response())
    old_sig = _signal("long", limit=True)
    new_sig = {**_signal("long", limit=True), "entry_price": 20010.0}
    ex.modify_limit_entry(old_sig, new_sig, _bar())
    _drain(ex)
    assert ex._http.post.call_count == 2
    first_payload = ex._http.post.call_args_list[0].kwargs["json"]
    second_payload = ex._http.post.call_args_list[1].kwargs["json"]
    assert first_payload["data"] == "close"
    assert second_payload["data"] == "buy"
    assert second_payload["price"] == 20010.0


def test_modify_limit_entry_close_is_synchronous():
    """Close step in modify_limit_entry must run synchronously, not via thread pool."""
    ex = _make_executor()
    call_order = []

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
    ex.modify_limit_entry(old_sig, new_sig, _bar())
    _drain(ex)

    # First call must be direct (synchronous close), second via pool (async re-place)
    assert call_order[0] == ("direct", "close")
    assert call_order[1][0] == "pool"


def test_modify_limit_entry_replaces_even_if_close_fails():
    import httpx as _httpx
    import execution.pickmytrade as _mod
    ex = _make_executor()
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
        ex.modify_limit_entry(old_sig, new_sig, _bar())
        _drain(ex)
    finally:
        _mod.time.sleep = orig_sleep
    assert "close" in post_calls
    assert "buy" in post_calls
