# broker_recon/broker_state.py
# Headless broker-state reader (GIL-42): detection-only snapshot of the live Tradovate
# net position for the close-reconcile.
#
# Unlike the GIL-36 headed reader.py (a persistent placeholder-DOM session kept open for
# after-entry SL verification), this is a one-shot, fail-safe state fetch reused by the
# synchronous close-reconcile in live_orders.dispatch(). It reuses the shared
# broker_recon.tradovate_login flow and the same login model as
# reports/get_tradovate_orders.py, reads today's Orders rows in-process (NO CSV download),
# and reduces them to a net position + avg entry + working protective stop + direction.
#
# Detection-only: it NEVER sends a broker order. It degrades to "unknown" (returns None)
# on ANY failure — missing creds, login/DOM/timeout, account-gone — and NEVER raises into
# the caller, so a reconcile failure always lets the close proceed as the strategy intended.
# All logging is ASCII-only (the live orchestrator's stdout is cp1252 on Windows; a non-ASCII
# char in a printed string crashes the loop — the exact GIL-36 charmap regression).

from __future__ import annotations

import os

from broker_recon import reconcile as _recon

# A short, bounded Playwright timeout so a hung broker page can never stall the trading
# loop indefinitely; on timeout the body raises, is swallowed, and we return None (noop).
_PAGE_TIMEOUT_MS = 15_000

# Terminal "filled" statuses (lower-cased substring match against the blotter status text).
_FILLED_STATUSES = _recon._FILLED_STATUSES


def _log(msg: str) -> None:
    # Operational warning, not a production data path. ASCII-only by construction.
    print(f"[broker_recon.broker_state] {msg}", flush=True)


def fetch_broker_state(symbol: str) -> dict | None:
    """Return a detection-only snapshot of the live broker position for `symbol`, or None.

    Snapshot shape on success::

        {"net_position": int, "avg_entry": float,
         "stop_price": float | None, "direction": "long"|"short"|"flat"}

    where ``net_position = sum(filled buys) - sum(filled sells)`` for `symbol`,
    ``direction`` is derived from the sign, ``avg_entry`` is the size-weighted average of
    the open lot's filling fills, and ``stop_price`` is the working protective stop on the
    correct side (reusing ``reconcile._is_protective_stop_row``).

    Headless-as-feasible: Tradovate forces a headed chromium on Windows (WebGL canvas), so
    this matches the launch used by reports/get_tradovate_orders.run() to stay reliable.

    NEVER sends an order. Returns None (degrade to "unknown") on ANY failure, never raises.
    """
    try:
        return _fetch(symbol)
    except Exception as exc:  # pragma: no cover - browser/login flakiness, swallowed
        _log(f"fetch failed -> broker-unknown (ignored): {exc}")
        return None


def _fetch(symbol: str) -> dict | None:
    """Browser body for fetch_broker_state. Wrapped by the public fail-safe try/except."""
    username = os.environ.get("TRADOVATE_USERNAME", "")
    password = os.environ.get("TRADOVATE_PASSWORD", "")
    account_id = os.environ.get("TRADING_ACCOUNT_IDS", "").split(",")[0].strip()
    if not username or not password or not account_id:
        _log("missing TRADOVATE_USERNAME / TRADOVATE_PASSWORD / TRADING_ACCOUNT_IDS "
             "-> broker-unknown")
        return None

    from playwright.sync_api import sync_playwright  # pragma: no cover

    from broker_recon.tradovate_login import (  # pragma: no cover
        AccountNotFoundError,
        login_and_select_account,
    )

    pw = browser = None
    try:  # pragma: no cover - exercised only in the supervised live smoke step
        pw = sync_playwright().start()
        # Tradovate renders via WebGL canvas -> must run headed on Windows (same launch as
        # reports/get_tradovate_orders.run()).
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(_PAGE_TIMEOUT_MS)
        # Bound navigation too (page.goto in the login flow) — set_default_timeout alone
        # only caps locator/action waits, so a stuck initial navigation could otherwise
        # stall the synchronous in-thread reconcile past _PAGE_TIMEOUT_MS.
        page.set_default_navigation_timeout(_PAGE_TIMEOUT_MS)
        try:
            login_and_select_account(page, username, password, account_id)
        except AccountNotFoundError as exc:
            _log(f"account gone -> broker-unknown for the session: {exc}")
            return None
        rows = _read_orders_rows(page, symbol)
        return reduce_orders_to_state(rows, symbol)
    finally:  # pragma: no cover
        for closer, obj in ((lambda o: o.close(), browser), (lambda o: o.stop(), pw)):
            try:
                if obj is not None:
                    closer(obj)
            except Exception:
                pass


def _read_orders_rows(page, symbol: str) -> list[dict]:  # pragma: no cover
    """Parse today's on-screen Orders blotter rows for `symbol` into order dicts.

    Kept thin and behind this function so the unit tests inject a fake by monkeypatching
    `fetch_broker_state` directly (no browser). The live Orders-panel grid DOM (column
    layout, status text, row selectors) is confirmed against the running platform in the
    supervised smoke step, NOT in unit tests. Reads defensively and skips unparsable rows;
    returns [] on any read failure (then `reduce_orders_to_state` sees no rows -> flat,
    and the reconcile degrades to confirmed-flat / trust-strategy).
    """
    rows: list[dict] = []
    try:
        grid_rows = page.locator("div.order-row, tr.order-row, [data-order-id]")
        n = grid_rows.count()
    except Exception:
        return []
    sym_root = symbol.split("1!")[0] if symbol else ""
    for i in range(n):
        try:
            row = grid_rows.nth(i)
            txt = (row.text_content() or "")
            if sym_root and sym_root not in txt:
                continue
            rows.append(_parse_row(row))
        except Exception:
            continue
    return rows


def _parse_row(row) -> dict:  # pragma: no cover - DOM-shape dependent
    """Best-effort extraction of one order row into the canonical reconcile row dict."""
    def _cell(attr):
        try:
            return (row.get_attribute(attr) or "").strip()
        except Exception:
            return ""
    # `qty` MUST use the same key `reduce_orders_to_state` reads, or every live-parsed row
    # silently defaults to 1 and a real 2-lot reads as net 1 (defeating adopt-size/resize).
    # The exact DOM attribute is confirmed in the supervised smoke step; read defensively
    # across the likely names so a 0/empty value falls back to 1 (one contract) in the reducer.
    return {
        "order_id":   _cell("data-order-id"),
        "side":       _cell("data-side"),
        "type":       _cell("data-type"),
        "price":      _cell("data-price"),
        "stop_price": _cell("data-stop-price"),
        "status":     _cell("data-status"),
        "avg_fill":   _cell("data-avg-fill"),
        "qty":        _cell("data-qty") or _cell("data-quantity") or _cell("data-size"),
        "time":       _cell("data-time"),
    }


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def reduce_orders_to_state(rows: list[dict], symbol: str) -> dict:
    """Pure reduction of blotter rows to a net-position snapshot (no I/O, unit-testable).

    net_position = sum(filled-buy qty) - sum(filled-sell qty). direction from the sign.
    avg_entry = size-weighted average of the fills on the NET side (the side that owns the
    open lot). stop_price = the working protective stop on the correct side of avg_entry.
    """
    rows = rows or []
    buy_qty = buy_notional = 0.0
    sell_qty = sell_notional = 0.0
    for r in rows:
        if not _recon._status_is(r.get("status", ""), _FILLED_STATUSES):
            continue
        side = (r.get("side") or "").strip().lower()
        qty = _to_float(r.get("qty") or r.get("size") or r.get("contracts") or 1)
        if qty <= 0.0:
            qty = 1.0
        fill = _to_float(r.get("avg_fill") or r.get("price"))
        if side == "buy":
            buy_qty += qty
            buy_notional += qty * fill
        elif side == "sell":
            sell_qty += qty
            sell_notional += qty * fill

    net = int(round(buy_qty - sell_qty))
    if net > 0:
        direction = "long"
        avg_entry = (buy_notional / buy_qty) if buy_qty else 0.0
    elif net < 0:
        direction = "short"
        avg_entry = (sell_notional / sell_qty) if sell_qty else 0.0
    else:
        return {"net_position": 0, "avg_entry": 0.0, "stop_price": None,
                "direction": "flat"}

    stop_price = None
    for r in rows:
        if _recon._is_protective_stop_row(r, direction, avg_entry):
            try:
                stop_price = float(r.get("stop_price") or r.get("price"))
            except (TypeError, ValueError):
                stop_price = None
            break

    return {"net_position": net, "avg_entry": avg_entry,
            "stop_price": stop_price, "direction": direction}
