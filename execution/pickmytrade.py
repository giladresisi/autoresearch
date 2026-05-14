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

# Placeholder SL prices injected into STP entry orders so Tradovate creates a
# stop-order anchor at fill time (required for update_stop_loss to work later).
# Values are chosen to be unreachable so the placeholder never triggers.
_STP_PLACEHOLDER_SL_LONG  = 0.0      # below any real futures price
_STP_PLACEHOLDER_SL_SHORT = 50000.0  # above any real MNQ price


class PickMyTradeExecutor:
    def __init__(self, *,
                 webhook_url: str,
                 api_key: str,
                 symbol: str,
                 account_ids: list,
                 contracts: int,
                 request_timeout_s: float = 10.0,
                 max_retries: int = 3,
                 entry_slip_ticks: int = 2,
                 tick_size: float = 0.25):
        self._webhook_url       = webhook_url
        self._api_key           = api_key
        self._symbol            = symbol
        self._account_ids       = account_ids
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
            "gtd_in_second":   0,
            "token":           self._api_key,
            "multiple_accounts": [
                {
                    "token":               self._api_key,
                    "account_id":          account_id,
                    "risk_percentage":     0,
                    "quantity_multiplier": 1,
                }
                for account_id in self._account_ids
            ],
        }
        payload.update(extra)
        return payload

    def place_entry(self, signal: dict, bar: BarRow) -> FillRecord:
        order_id = f"pmt-{uuid.uuid4().hex[:8]}"
        direction = signal["direction"]
        data = "buy" if direction == "long" else "sell"
        entry_price = float(signal["entry_price"])
        stop_price = float(signal["stop_price"]) if signal.get("stop_price") is not None else 0.0
        is_stop = signal.get("stop_fill_bars") is not None or signal.get("limit_fill_bars") is not None
        if is_stop:
            # PMT/Tradovate requires a non-zero sl in the STP entry payload to create a
            # stop-order anchor at fill time. Without it, update_stop_loss called after
            # fill has nothing to update. We use a placeholder far from any real price
            # so it never triggers accidentally before the real SL is attached via
            # update_stop_loss once the order fills.
            placeholder_sl = _STP_PLACEHOLDER_SL_LONG if direction == "long" else _STP_PLACEHOLDER_SL_SHORT
            payload = self._build_payload(data, order_type="STP", sl=placeholder_sl, price=entry_price)
            order_type = "stop"
        else:
            # No price field: PMT uses the latest close price as the market price
            payload = self._build_payload(data, order_type="MKT", sl=stop_price)
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

    def update_stop_loss(self, position: dict, bar: BarRow) -> tuple:
        """Replace the placeholder SL on a FILLED open position with the real SL price.

        Only works on open (filled) positions. Calling this on an unfilled STP order
        has no effect — PMT will acknowledge the request but Tradovate ignores it.
        STP orders convert to MKT orders when their trigger price is touched, so by
        the time this is called the position is always a market-filled position.
        Returns (status_code, response_body).
        """
        order_id = f"pmt-{uuid.uuid4().hex[:8]}"
        direction = position["direction"]
        data = "buy" if direction == "long" else "sell"
        payload = self._build_payload(
            data,
            order_type="MKT",  # position is always MKT-filled by the time this is called
            sl=float(position["stop_price"]),
            update_sl=True,
        )
        return self._post_order(order_id, payload)

    def place_close(self, label: str = "close") -> None:
        order_id = f"pmt-{uuid.uuid4().hex[:8]}"
        payload = self._build_payload("close")
        # Synchronous — callers can sequence follow-up requests after this returns
        self._post_order(order_id, payload)

    def place_exit(self, position: dict, exit_type: str, bar: BarRow) -> None:
        self.place_close(label=exit_type)
        return None

    def modify_stop_entry(self, old_signal: dict, new_signal: dict, bar: BarRow) -> None:
        # Step 1: synchronously cancel the unfilled stop entry
        self.place_close(label="modify_cancel")
        # Step 2: fire new STP order via thread pool
        order_id = f"pmt-{uuid.uuid4().hex[:8]}"
        direction = new_signal["direction"]
        data = "buy" if direction == "long" else "sell"
        entry_price = float(new_signal["entry_price"])
        payload = self._build_payload(data, order_type="STP", price=entry_price)
        self._order_pool.submit(self._post_order, order_id, payload)

    def _post_order(self, order_id: str, payload: dict) -> tuple:
        headers = {"Content-Type": "application/json"}
        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = self._http.post(
                    self._webhook_url, headers=headers, json=payload,
                    timeout=self._request_timeout_s,
                )
                if resp.status_code in (200, 201):
                    if payload.get("update_sl") or payload.get("update_tp"):
                        action = f"update_sl={payload.get('sl')} update_tp={payload.get('tp')}"
                    else:
                        action = f"{payload.get('data')} {payload.get('order_type', 'MKT')} @ {payload.get('price', 'mkt')}"
                    print(f"[PMT] Order {order_id} sent OK ({resp.status_code}): {action}", flush=True)
                    return resp.status_code, resp.text
                last_exc = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:
                last_exc = exc
            if attempt < self._max_retries - 1:
                time.sleep(2 ** attempt)
        print(f"[FILL-WARN] Order {order_id} placement failed: {last_exc}", flush=True)
        return -1, str(last_exc)
