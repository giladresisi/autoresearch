# execution/pickmytrade.py
# PickMyTradeExecutor: sends orders to PickMyTrade HTTP API.
# Fill prices are computed synchronously using assumed tick-based slippage —
# no async fill polling (PMT does not expose a queryable fills endpoint).
import concurrent.futures
import datetime
import time
import uuid

import httpx

from execution.protocol import FillRecord, BarRow, assumed_fill_price


class PickMyTradeExecutor:
    def __init__(self, *,
                 webhook_url: str,
                 api_key: str,
                 symbol: str,
                 account_id: str,
                 contracts: int,
                 request_timeout_s: float = 10.0,
                 max_retries: int = 3,
                 entry_slip_ticks: int = 2,
                 tick_size: float = 0.25):
        self._webhook_url       = webhook_url
        self._api_key           = api_key
        self._symbol            = symbol
        self._account_id        = account_id
        self._contracts         = contracts
        self._request_timeout_s = request_timeout_s
        self._max_retries       = max_retries
        self._entry_slip_ticks  = entry_slip_ticks
        self._tick_size         = tick_size
        # Persistent HTTP client reuses TCP connections across order calls (keep-alive)
        self._http = httpx.Client()
        # Thread pool for non-blocking order dispatch — bar callback returns immediately
        self._order_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="pmt-order"
        )

    def start(self) -> None:
        if not self._webhook_url or not self._api_key:
            raise RuntimeError(
                "PMT_WEBHOOK_URL and PMT_API_KEY must be set before calling start()"
            )

    def stop(self) -> None:
        # Wait for any in-flight order placements before closing the HTTP client
        self._order_pool.shutdown(wait=True, cancel_futures=False)
        self._http.close()

    def _build_payload(self, data: str, **extra) -> dict:
        payload = {
            "symbol":          self._symbol,
            "data":            data,
            "quantity":        self._contracts,
            "risk_percentage": 0,
            "token":           self._api_key,
            "multiple_accounts": [{
                "token":               self._api_key,
                "account_id":          self._account_id,
                "risk_percentage":     0,
                "quantity_multiplier": 1,
            }],
        }
        payload.update(extra)
        return payload

    def place_entry(self, signal: dict, bar: BarRow) -> FillRecord:
        order_id = f"pmt-{uuid.uuid4().hex[:8]}"
        direction = signal["direction"]
        data = "buy" if direction == "long" else "sell"
        entry_price = float(signal["entry_price"])
        stop_price = float(signal["stop_price"]) if signal.get("stop_price") is not None else 0.0
        is_limit = signal.get("limit_fill_bars") is not None
        if is_limit:
            payload = self._build_payload(data, order_type="LMT", price=entry_price, gtd_in_second=0)
            order_type = "limit"
        else:
            payload = self._build_payload(data, order_type="MKT", price=entry_price, sl=stop_price)
            order_type = "market"
        # Fire-and-forget: bar callback returns immediately; HTTP runs in background thread
        self._order_pool.submit(self._post_order, order_id, payload)
        fill_price = assumed_fill_price(
            direction, order_type, entry_price, self._entry_slip_ticks, self._tick_size
        )
        return FillRecord(
            order_id=order_id,
            symbol=self._symbol,
            direction=direction,
            order_type=order_type,
            requested_price=entry_price,
            fill_price=round(fill_price, 4),
            fill_time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            contracts=self._contracts,
            status="filled",
            session_date=str(bar.name.date()) if hasattr(bar, "name") and bar.name is not None else "",
        )

    def place_stop_after_limit_fill(self, position: dict, bar: BarRow) -> None:
        order_id = f"pmt-{uuid.uuid4().hex[:8]}"
        direction = position["direction"]
        data = "buy" if direction == "long" else "sell"
        payload = self._build_payload(
            data,
            quantity=0,
            sl=float(position["stop_price"]),
            update_sl=True,
            pyramid=False,
            same_direction_ignore=True,
        )
        self._order_pool.submit(self._post_order, order_id, payload)

    def place_close(self, label: str = "close") -> None:
        order_id = f"pmt-{uuid.uuid4().hex[:8]}"
        payload = self._build_payload("close")
        # Synchronous — callers can sequence follow-up requests after this returns
        self._post_order(order_id, payload)

    def place_exit(self, position: dict, exit_type: str, bar: BarRow) -> None:
        self.place_close(label=exit_type)
        return None

    def modify_limit_entry(self, old_signal: dict, new_signal: dict, bar: BarRow) -> None:
        # Step 1: synchronously cancel the unfilled limit
        self.place_close(label="modify_cancel")
        # Step 2: fire new LMT order via thread pool
        order_id = f"pmt-{uuid.uuid4().hex[:8]}"
        direction = new_signal["direction"]
        data = "buy" if direction == "long" else "sell"
        entry_price = float(new_signal["entry_price"])
        payload = self._build_payload(data, order_type="LMT", price=entry_price, gtd_in_second=0)
        self._order_pool.submit(self._post_order, order_id, payload)

    def _post_order(self, order_id: str, payload: dict) -> None:
        headers = {"Content-Type": "application/json"}
        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = self._http.post(
                    self._webhook_url, headers=headers, json=payload,
                    timeout=self._request_timeout_s,
                )
                if resp.status_code in (200, 201):
                    return
                last_exc = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:
                last_exc = exc
            if attempt < self._max_retries - 1:
                time.sleep(2 ** attempt)
        print(f"[FILL-WARN] Order {order_id} placement failed: {last_exc}", flush=True)
