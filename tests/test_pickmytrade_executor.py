# tests/test_pickmytrade_executor.py
# Unit tests for PickMyTradeExecutor — HTTP calls mocked on the executor's _http instance.
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from execution.pickmytrade import PickMyTradeExecutor
from execution.protocol import FillRecord
from strategy_smt import _BarRow


def _make_executor(tmp_path: Path, fill_mode: str = "poll") -> PickMyTradeExecutor:
    return PickMyTradeExecutor(
        webhook_url="https://pmt.example.com/signal",
        api_key="test-key",
        symbol="MNQ1!",
        account_id="ACC123",
        contracts=1,
        fill_mode=fill_mode,
        fill_poll_interval_s=999,
        fills_path=tmp_path / "fills.jsonl",
        fills_url="https://pmt.example.com/fills",
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


def test_place_entry_long_posts_buy_market_order(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert ex._http.post.call_count == 1
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "buy"
    assert payload["order_type"] == "MKT"


def test_place_entry_short_posts_sell_market_order(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("short"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "sell"
    assert payload["order_type"] == "MKT"


def test_place_entry_limit_posts_limit_order(tmp_path):
    ex = _make_executor(tmp_path)
    sig = _signal("long", limit=True)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(sig, _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["order_type"] == "LMT"
    assert payload["gtd_in_second"] == 0
    assert payload["price"] == sig["entry_price"]


def test_place_entry_returns_none(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    result = ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    assert result is None


def test_place_exit_long_posts_sell_close(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_exit(_position("long"), "exit_tp", _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "close"


def test_place_exit_short_posts_buy_close(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_exit(_position("short"), "exit_tp", _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "close"


def test_place_exit_returns_none(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    result = ex.place_exit(_position("long"), "exit_tp", _bar())
    _drain(ex)
    assert result is None


def test_order_retries_on_500(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_server_error_response())
    # Patch sleep on the module so retries don't wait
    import execution.pickmytrade as _mod
    orig_sleep = _mod.time.sleep
    _mod.time.sleep = lambda _: None
    try:
        ex.place_entry(_signal("long"), _bar())
        _drain(ex)
    finally:
        _mod.time.sleep = orig_sleep
    assert ex._http.post.call_count == ex._max_retries


def test_order_does_not_raise_on_failure(tmp_path):
    import httpx as _httpx
    import execution.pickmytrade as _mod
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(side_effect=_httpx.ConnectError("unreachable"))
    orig_sleep = _mod.time.sleep
    _mod.time.sleep = lambda _: None
    try:
        ex.place_entry(_signal("long"), _bar())
        _drain(ex)  # must not propagate any exception
    finally:
        _mod.time.sleep = orig_sleep


def test_fill_polling_writes_filled_record_to_jsonl(tmp_path):
    ex = _make_executor(tmp_path)
    order_id = "pmt-test123"
    ctx = {
        "direction": "long",
        "requested_price": 20000.0,
        "order_type": "market",
        "session_date": "2026-04-30",
    }
    with ex._lock:
        ex._pending[order_id] = ctx

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "status": "filled",
        "fill_price": 20005.0,
        "fill_time": "2026-04-30T14:00:00+00:00",
    }
    ex._http.get = MagicMock(return_value=fake_resp)

    rec = ex._query_fill(order_id, ctx)
    assert rec is not None
    assert rec.fill_price == 20005.0
    ex._record_fill(rec)

    fills_path = tmp_path / "fills.jsonl"
    assert fills_path.exists()
    lines = fills_path.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["fill_price"] == 20005.0


def test_fill_jsonl_format(tmp_path):
    ex = _make_executor(tmp_path)
    rec = FillRecord(
        order_id="pmt-fmt001",
        symbol="MNQ1!",
        direction="long",
        order_type="market",
        requested_price=20000.0,
        fill_price=20005.5,
        fill_time="2026-04-30T14:00:00+00:00",
        contracts=1,
        status="filled",
        session_date="2026-04-30",
    )
    ex._record_fill(rec)

    lines = (tmp_path / "fills.jsonl").read_text().strip().splitlines()
    parsed = json.loads(lines[0])

    for field in ("order_id", "symbol", "direction", "fill_price", "status", "session_date"):
        assert field in parsed
    assert parsed["order_id"] == "pmt-fmt001"
    assert parsed["fill_price"] == 20005.5
    assert parsed["status"] == "filled"


def test_pending_order_cleared_after_fill(tmp_path):
    ex = _make_executor(tmp_path)
    order_id = "pmt-clear001"
    ctx = {
        "direction": "long",
        "requested_price": 20000.0,
        "order_type": "market",
        "session_date": "2026-04-30",
    }
    with ex._lock:
        ex._pending[order_id] = ctx

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"status": "filled", "fill_price": 20005.0}
    ex._http.get = MagicMock(return_value=fake_resp)

    rec = ex._query_fill(order_id, ctx)
    assert rec is not None
    ex._record_fill(rec)
    with ex._lock:
        ex._pending.pop(order_id, None)
    assert order_id not in ex._pending


def test_start_raises_if_env_missing(tmp_path):
    ex = PickMyTradeExecutor(
        webhook_url="",
        api_key="",
        symbol="MNQ1!",
        account_id="ACC123",
        contracts=1,
        fill_mode="poll",
        fill_poll_interval_s=999,
        fills_path=tmp_path / "fills.jsonl",
        fills_url="",
    )
    with pytest.raises(RuntimeError):
        ex.start()


def test_stop_joins_fill_thread(tmp_path):
    ex = _make_executor(tmp_path)
    ex._fill_poll_interval_s = 0.05
    ex.start()
    assert ex._fill_thread is not None
    assert ex._fill_thread.is_alive()
    t0 = time.time()
    ex.stop()
    elapsed = time.time() - t0
    assert elapsed < 6.0
    assert not ex._fill_thread.is_alive()


def test_market_entry_includes_sl(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["sl"] == 19980.0


def test_limit_entry_excludes_sl(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long", limit=True), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert "sl" not in payload


def test_market_entry_includes_multiple_accounts(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["multiple_accounts"][0]["account_id"] == "ACC123"


def test_token_in_payload_toplevel(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["token"] == "test-key"


def test_no_bearer_header(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    headers = ex._http.post.call_args.kwargs.get("headers", {})
    assert "Authorization" not in headers


def test_risk_percentage_zero_in_all_payloads(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["risk_percentage"] == 0
    assert payload["multiple_accounts"][0]["risk_percentage"] == 0


def test_place_stop_after_limit_fill_long(tmp_path):
    ex = _make_executor(tmp_path)
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


def test_place_stop_after_limit_fill_short(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    pos = _position("short")
    ex.place_stop_after_limit_fill(pos, _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "sell"
    assert payload["sl"] == 19980.0


def test_place_close_sends_data_close(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_close()
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["data"] == "close"


def test_place_exit_delegates_to_close(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    for exit_type in ("exit_tp", "exit_stop", "exit_market"):
        ex._http.post.reset_mock()
        ex.place_exit(_position("long"), exit_type, _bar())
        _drain(ex)
        payload = ex._http.post.call_args.kwargs["json"]
        assert payload["data"] == "close"


def test_modify_limit_entry_sends_close_then_limit(tmp_path):
    ex = _make_executor(tmp_path)
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


def test_modify_limit_entry_close_is_synchronous(tmp_path):
    """Close step in modify_limit_entry must run synchronously, not via thread pool."""
    import execution.pickmytrade as _mod
    ex = _make_executor(tmp_path)
    call_order = []

    original_post = ex._post_order
    def tracked_post(order_id, payload, ctx):
        call_order.append(("direct", payload["data"]))
        return original_post(order_id, payload, ctx)

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


def test_modify_limit_entry_replaces_even_if_close_fails(tmp_path):
    import httpx as _httpx
    import execution.pickmytrade as _mod
    ex = _make_executor(tmp_path)
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
    # Both close and re-place should have been attempted
    assert "close" in post_calls
    assert "buy" in post_calls
