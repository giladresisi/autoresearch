# execution/pickmytrade.py
# PickMyTradeExecutor: sends orders to PickMyTrade HTTP API.
# Fill prices are computed synchronously using assumed tick-based slippage —
# no async fill polling (PMT does not expose a queryable fills endpoint).
import concurrent.futures
import datetime
import time
import uuid
import zoneinfo

import httpx

import session_times
from execution.protocol import FillRecord, BarRow, assumed_fill_price

_ET = zoneinfo.ZoneInfo("America/New_York")

# Wall-clock ET window during which new entries are blocked: the final stretch of a session
# before its SESSION_CLOSE (16:55 ET). New entries are blocked strictly after 15:30 ET and up
# to the 16:55 close, then ALLOWED again once the next session opens at 18:05 ET — so the
# overnight/evening session trades normally. Closes, cancels and stop-loss mods are unaffected.
_NEW_ENTRY_CUTOFF = datetime.time(15, 30)



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
        # True iff the most recent stop/market entry was actually sent to the broker.
        # False when the order was suppressed by the entry-window gate.
        self._entry_is_live: bool = False

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
        # Anchor for the assumed-fill estimate. Default = the requested entry price; the
        # STP->MKT downgrade overrides it with the current market price — a market order
        # fills at the market, not at the (already-passed) trigger. Anchoring at the
        # trigger produced 12-20pt strategy-vs-broker fill gaps in fast moves
        # (2026-06-05 05:17 reconciliation).
        fill_anchor = entry_price
        if is_stop:
            # Downgrade STP → MKT only when the market has reached/passed the trigger (R1).
            # Tradovate rejects a stop order whose trigger is at or past the market price (it
            # would fill immediately as a market order) — that is the only case that must go MKT.
            # A stop whose trigger is still ahead of the market (un-reached) rests legally and
            # must NOT be downgraded, or we enter the breakout before it confirms.
            _current = float(signal.get("current_price", 0.0))
            _bar_high = float(signal.get("bar_high", 0.0))
            _bar_low = float(signal.get("bar_low", 0.0))
            # Downgrade STP -> MKT when the live market has REACHED the trigger. Key off the
            # bar EXTREME toward the trigger (high for longs, low for shorts) in addition to
            # the current close: the close lags within the bar, so a stop the live price has
            # already touched intrabar was being placed as a resting STP and rejected by
            # Tradovate (a stop at/past the market is invalid). 2026-06-11 (four rejected
            # brackets). bar_high/bar_low default to 0.0 (absent) -> falls back to close-only.
            if direction == "long":
                _reach = max(_current, _bar_high)
                _trigger_reached = _reach > 0 and _reach >= entry_price
            else:
                _cands = [_p for _p in (_current, _bar_low) if _p > 0]
                _reach = min(_cands) if _cands else 0.0
                _trigger_reached = _reach > 0 and _reach <= entry_price
            if _trigger_reached:
                # Market fills at the live market, not the wick extreme — anchor the assumed
                # fill to the current price (fall back to the trigger if no current price).
                _mkt = _current if _current > 0 else entry_price
                print(f"[PMT] STP->MKT: trigger {entry_price} reached (px={_current} hi={_bar_high} lo={_bar_low})", flush=True)
                payload = self._build_payload(data, order_type="MKT", sl=stop_price)
                order_type = "market"
                fill_anchor = _mkt
            else:
                payload = self._build_payload(data, order_type="STP", sl=stop_price, price=entry_price)
                order_type = "stop"
        else:
            # No price field: PMT uses the latest close price as the market price
            payload = self._build_payload(data, order_type="MKT", sl=stop_price)
            order_type = "market"
        # Extract bar_time in ET for time-based slippage decision
        if bar is not None and hasattr(bar, "name") and bar.name is not None:
            _bar_ts = bar.name
            if hasattr(_bar_ts, "tz_convert"):
                _bar_ts = _bar_ts.tz_convert(_ET)
            _bar_time = _bar_ts.time()
        else:
            _bar_time = datetime.datetime.now(_ET).time()
        fill_price = assumed_fill_price(
            direction, order_type, fill_anchor, self._entry_slip_ticks, self._tick_size,
            bar_time=_bar_time,
        )
        session_date = str(bar.name.date()) if hasattr(bar, "name") and bar.name is not None else ""

        _now_et_time = datetime.datetime.now(_ET).time()

        # Block outside entry windows — return identical FillRecord shape so callers are unaffected
        if not session_times.is_entry_allowed(_now_et_time):
            print(f"[PMT] entry window blocked — not sending {order_id}", flush=True)
            self._entry_is_live = False
            return FillRecord(
                order_id=order_id, symbol=self._symbol, direction=direction,
                order_type=order_type, requested_price=entry_price,
                fill_price=round(fill_price, 4),
                fill_time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                contracts=self._contracts, status="blocked", session_date=session_date,
            )

        # Block new entries only in the pre-close window 15:30 < now <= 16:55 ET — the tail of
        # the session before its SESSION_CLOSE. Outside that window (including the 18:05+ evening
        # of the next session) new entries are allowed. Wall-clock guard, NOT bar time.
        # Closes/cancels/stop-mods are unaffected.
        if _NEW_ENTRY_CUTOFF < _now_et_time <= session_times.SESSION_CLOSE:
            print("[PMT] new entry blocked 15:30–16:55 ET (pre-close window)", flush=True)
            self._entry_is_live = False
            return FillRecord(
                order_id=order_id, symbol=self._symbol, direction=direction,
                order_type=order_type, requested_price=entry_price,
                fill_price=round(fill_price, 4),
                fill_time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                contracts=self._contracts, status="blocked", session_date=session_date,
            )

        # flatten_first: synchronously close any existing position before entering
        if signal.get("flatten_first"):
            self.place_close("pre-reentry")

        # Fire-and-forget: bar callback returns immediately; HTTP runs in background thread
        self._order_pool.submit(self._post_order, order_id, payload)
        self._entry_is_live = True
        return FillRecord(
            order_id=order_id, symbol=self._symbol, direction=direction,
            order_type=order_type, requested_price=entry_price,
            fill_price=round(fill_price, 4),
            fill_time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            contracts=self._contracts, status="filled", session_date=session_date,
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
        self._entry_is_live = False
        # Synchronous — callers can sequence follow-up requests after this returns
        self._post_order(order_id, payload)

    def place_exit(self, position: dict, exit_type: str, bar: BarRow) -> None:
        self.place_close(label=exit_type)
        return None

    def modify_stop_entry(self, old_signal: dict, new_signal: dict, bar: BarRow,
                          placed_at_broker: bool = False) -> None:
        # `_entry_is_live` is per-process — True only in the process that placed the entry
        # (the orchestrator). A separate CLI process (`trade.py move`) has it False even
        # though a working STP order exists at the broker; relying on it alone skipped the
        # cancel and left a DUPLICATE resting order (2026-06-11 09:19; same class as the
        # 2026-06-04 09:05 cancel incident). `placed_at_broker` is the cross-process truth
        # from position.json (a persisted, non-`unplaced` stop_entry), so the cancel fires
        # regardless of which process calls this.
        if not self._entry_is_live and not placed_at_broker:
            # Entry was never sent to broker — window was closed when it was placed
            if session_times.is_entry_allowed(datetime.datetime.now(_ET).time()):
                # Window just opened — place fresh entry (no existing broker order to cancel)
                self.place_entry(new_signal, bar)
            else:
                print("[PMT] entry window blocked — not modifying unplaced stop entry", flush=True)
            return
        # Step 1: synchronously cancel the unfilled stop entry
        self.place_close(label="modify_cancel")
        # Step 2: fire new STP order via thread pool
        order_id = f"pmt-{uuid.uuid4().hex[:8]}"
        direction = new_signal["direction"]
        data = "buy" if direction == "long" else "sell"
        entry_price = float(new_signal["entry_price"])
        stop_price = float(new_signal.get("stop_price", 0.0))
        payload = self._build_payload(data, order_type="STP", price=entry_price, sl=stop_price)
        self._order_pool.submit(self._post_order, order_id, payload)
        self._entry_is_live = True

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
