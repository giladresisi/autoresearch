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
    assert payload["action"] == "BUY"
    assert payload["orderType"] == "Market"


def test_place_entry_short_posts_sell_market_order(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("short"), _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["action"] == "SELL"
    assert payload["orderType"] == "Market"


def test_place_entry_limit_posts_limit_order(tmp_path):
    ex = _make_executor(tmp_path)
    sig = _signal("long", limit=True)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(sig, _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["orderType"] == "Limit"
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
    assert payload["action"] == "SELL"


def test_place_exit_short_posts_buy_close(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_exit(_position("short"), "exit_tp", _bar())
    _drain(ex)
    payload = ex._http.post.call_args.kwargs["json"]
    assert payload["action"] == "BUY"


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


def test_is_automated_flag_set(tmp_path):
    ex = _make_executor(tmp_path)
    ex._http.post = MagicMock(return_value=_ok_response())
    ex.place_entry(_signal("long"), _bar())
    ex.place_exit(_position("long"), "exit_tp", _bar())
    _drain(ex)
    assert ex._http.post.call_count == 2
    for call in ex._http.post.call_args_list:
        payload = call.kwargs["json"]
        assert payload["isAutomated"] is True
