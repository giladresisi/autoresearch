# execution/pickmytrade.py
# PickMyTradeExecutor: sends orders to PickMyTrade HTTP API.
# Fills are received asynchronously via poll or webhook, then written to fills.jsonl.
import concurrent.futures
import datetime
import http.server
import json
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx

from execution.protocol import FillRecord, BarRow


class PickMyTradeExecutor:
    def __init__(self, *,
                 webhook_url: str,
                 api_key: str,
                 symbol: str,
                 account_id: str,
                 contracts: int,
                 fill_mode: str = "poll",          # "poll" | "webhook"
                 fill_poll_interval_s: int = 30,
                 fill_webhook_port: int = 8765,
                 fills_path: Path | None = None,
                 request_timeout_s: float = 10.0,
                 max_retries: int = 3,
                 fills_url: str = ""):             # PMT fills query endpoint
        self._webhook_url        = webhook_url
        self._api_key            = api_key
        self._symbol             = symbol
        self._account_id         = account_id
        self._contracts          = contracts
        self._fill_mode          = fill_mode
        self._fill_poll_interval_s = fill_poll_interval_s
        self._fill_webhook_port  = fill_webhook_port
        self._fills_path         = fills_path or Path("fills.jsonl")
        self._request_timeout_s  = request_timeout_s
        self._max_retries        = max_retries
        self._fills_url          = fills_url

        self._pending: dict[str, dict] = {}   # order_id -> context dict
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._fill_thread: threading.Thread | None = None
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
        if self._fill_thread is not None and self._fill_thread.is_alive():
            raise RuntimeError("start() called on already-running executor; call stop() first")
        self._stop_event.clear()
        if self._fill_mode == "webhook":
            self._fill_thread = threading.Thread(
                target=self._fill_webhook_server, daemon=True
            )
        else:
            self._fill_thread = threading.Thread(
                target=self._fill_poll_loop, daemon=True
            )
        self._fill_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        # Wait for any in-flight order placements before closing the HTTP client
        self._order_pool.shutdown(wait=True, cancel_futures=False)
        self._http.close()
        if self._fill_thread is not None:
            self._fill_thread.join(timeout=5)

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

    def place_entry(self, signal: dict, bar: BarRow) -> None:
        order_id = f"pmt-{uuid.uuid4().hex[:8]}"
        direction = signal["direction"]
        data = "buy" if direction == "long" else "sell"
        entry_price = float(signal["entry_price"])
        stop_price = float(signal["stop_price"]) if signal.get("stop_price") is not None else 0.0
        is_limit = signal.get("limit_fill_bars") is not None
        if is_limit:
            payload = self._build_payload(data, order_type="LMT", price=entry_price, gtd_in_second=0)
        else:
            payload = self._build_payload(data, order_type="MKT", price=entry_price, sl=stop_price)
        ctx = {
            "direction": direction,
            "requested_price": entry_price,
            "order_type": "limit" if is_limit else "market",
            "session_date": str(bar.name.date()) if hasattr(bar, "name") and bar.name is not None else "",
        }
        # Fire-and-forget: bar callback returns immediately; HTTP runs in background thread
        self._order_pool.submit(self._post_order, order_id, payload, ctx)
        return None

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
        ctx = {
            "direction": direction,
            "session_date": str(bar.name.date()) if hasattr(bar, "name") and bar.name is not None else "",
        }
        self._order_pool.submit(self._post_order, order_id, payload, ctx)

    def place_close(self, label: str = "close") -> None:
        order_id = f"pmt-{uuid.uuid4().hex[:8]}"
        payload = self._build_payload("close")
        ctx = {"label": label}
        # Synchronous — callers can sequence follow-up requests after this returns.
        # Remove from _pending immediately: PMT does not return a queryable fill for
        # close orders, so the poll loop would spin on this order_id forever otherwise.
        self._post_order(order_id, payload, ctx)
        with self._lock:
            self._pending.pop(order_id, None)

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
        ctx = {
            "direction": direction,
            "requested_price": entry_price,
            "order_type": "limit",
            "session_date": str(bar.name.date()) if hasattr(bar, "name") and bar.name is not None else "",
        }
        self._order_pool.submit(self._post_order, order_id, payload, ctx)

    def _post_order(self, order_id: str, payload: dict, ctx: dict) -> None:
        headers = {
            "Content-Type": "application/json",
        }
        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = self._http.post(
                    self._webhook_url, headers=headers, json=payload,
                    timeout=self._request_timeout_s,
                )
                if resp.status_code in (200, 201):
                    with self._lock:
                        self._pending[order_id] = ctx
                    return
                last_exc = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:
                last_exc = exc
            if attempt < self._max_retries - 1:
                time.sleep(2 ** attempt)
        print(f"[FILL-WARN] Order {order_id} placement failed: {last_exc}", flush=True)

    def _fill_poll_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._fill_poll_interval_s)
            if self._stop_event.is_set():
                break
            with self._lock:
                pending = list(self._pending.items())
            for order_id, ctx in pending:
                rec = self._query_fill(order_id, ctx)
                if rec is not None:
                    self._record_fill(rec)
                    with self._lock:
                        self._pending.pop(order_id, None)

    def _query_fill(self, order_id: str, ctx: dict) -> FillRecord | None:
        if not self._fills_url:
            return None
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            resp = self._http.get(
                self._fills_url,
                headers=headers,
                params={"order_id": order_id},
                timeout=self._request_timeout_s,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("status") != "filled":
                return None
            return FillRecord(
                order_id=order_id,
                symbol=self._symbol,
                direction=ctx["direction"],
                order_type=ctx["order_type"],
                requested_price=ctx["requested_price"],
                fill_price=float(data["fill_price"]),
                fill_time=data.get("fill_time", datetime.datetime.now(datetime.timezone.utc).isoformat()),
                contracts=self._contracts,
                status="filled",
                session_date=ctx.get("session_date", ""),
            )
        except Exception as exc:
            print(f"[FILL-WARN] _query_fill error for {order_id}: {exc}", flush=True)
            return None

    def _fill_webhook_server(self) -> None:
        executor_ref = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    order_id = data.get("order_id", "")
                    with executor_ref._lock:
                        ctx = executor_ref._pending.get(order_id, {})
                    rec = FillRecord(
                        order_id=order_id,
                        symbol=executor_ref._symbol,
                        direction=ctx.get("direction", ""),
                        order_type=ctx.get("order_type", "market"),
                        requested_price=ctx.get("requested_price", 0.0),
                        fill_price=float(data.get("fill_price", 0.0)),
                        fill_time=data.get("fill_time", datetime.datetime.now(datetime.timezone.utc).isoformat()),
                        contracts=executor_ref._contracts,
                        status="filled",
                        session_date=ctx.get("session_date", ""),
                    )
                    executor_ref._record_fill(rec)
                    with executor_ref._lock:
                        executor_ref._pending.pop(order_id, None)
                    self.send_response(200)
                except Exception as exc:
                    print(f"[FILL-WARN] Webhook handler error: {exc}", flush=True)
                    self.send_response(400)
                self.end_headers()

            def log_message(self, fmt, *args):
                pass

        # Bind to localhost only — prevents spoofed fill callbacks from the network
        server = http.server.HTTPServer(("127.0.0.1", self._fill_webhook_port), _Handler)
        server.timeout = 1.0
        while not self._stop_event.is_set():
            server.handle_request()
        server.server_close()

    def _record_fill(self, rec: FillRecord) -> None:
        # Lock guards against concurrent fills racing on the read-modify-write of fills.jsonl
        with self._lock:
            line = json.dumps({
                "order_id":        rec.order_id,
                "symbol":          rec.symbol,
                "direction":       rec.direction,
                "order_type":      rec.order_type,
                "requested_price": rec.requested_price,
                "fill_price":      rec.fill_price,
                "fill_time":       rec.fill_time,
                "contracts":       rec.contracts,
                "status":          rec.status,
                "session_date":    rec.session_date,
            })
            self._fills_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self._fills_path.parent, delete=False, suffix=".tmp"
            ) as tmp:
                if self._fills_path.exists():
                    tmp.write(self._fills_path.read_text())
                tmp.write(line + "\n")
                tmp_path = Path(tmp.name)
            tmp_path.replace(self._fills_path)
        print(f"[FILL] order={rec.order_id} price={rec.fill_price} status={rec.status}", flush=True)
