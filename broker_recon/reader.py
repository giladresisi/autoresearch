# broker_recon/reader.py
# TradovateOrderReader: a persistent, READ-ONLY headed Playwright session over the live
# Tradovate orders blotter (GIL-36).
#
# The reconcile layer (reconcile.py) consumes this reader through its tiny interface
# (`query_orders`), so the pure classification + correction logic is fully testable with a
# fake reader and NO browser. The real reader logs in ONCE via the shared
# broker_recon.tradovate_login flow, keeps the session open, reads the on-screen Orders
# panel from the DOM (no CSV download), auto re-logs-in on a dropped session, and NEVER
# places an order — it is detection-only. Any failure degrades gracefully: query_orders
# returns [] and the trading loop is never blocked or crashed.

from __future__ import annotations

import os
import threading


class TradovateOrderReader:
    """Persistent read-only orders-blotter reader.

    Lifecycle:
        r = TradovateOrderReader()
        r.start()                       # login once (headed chromium)
        rows = r.query_orders("MNQ")    # read the live orders grid -> list[dict]
        r.stop()

    `query_orders` returns a list of dicts:
        {order_id, side, type, price, stop_price, status, avg_fill, time}
    On any failure (login, session drop that can't be recovered, DOM change) it returns []
    and logs — it never raises into the caller.
    """

    def __init__(self, *, username=None, password=None, account_id=None):
        self._username = username or os.environ.get("TRADOVATE_USERNAME", "")
        self._password = password or os.environ.get("TRADOVATE_PASSWORD", "")
        self._account_id = account_id or (
            os.environ.get("TRADING_ACCOUNT_IDS", "").split(",")[0].strip()
        )
        self._symbol = os.environ.get("TRADING_SYMBOL", "MNQ1!")
        self._pw = None          # the sync_playwright context manager handle
        self._browser = None
        self._ctx = None
        self._page = None
        self._disabled = False   # set True on AccountNotFoundError -> reconciler degrades
        self._lock = threading.Lock()

    # -- internal logging (silent-by-default; warnings only) --------------------
    @staticmethod
    def _warn(msg: str) -> None:
        # The reconciler runs alongside live trading; a reader hiccup must be visible but
        # must never crash anything. Stderr/print is acceptable here (operational warning,
        # not a production data path).
        print(f"[broker_recon.reader] {msg}", flush=True)

    @property
    def disabled(self) -> bool:
        return self._disabled

    def start(self) -> bool:
        """Launch headed chromium and log in once. Returns True on success.

        On AccountNotFoundError the reader marks itself disabled (the configured account is
        gone) so the caller degrades gracefully for the session. Any other failure also
        returns False and leaves the reader unusable (query_orders -> []).
        """
        from playwright.sync_api import sync_playwright

        from broker_recon.tradovate_login import (
            AccountNotFoundError,
            login_and_select_account,
        )

        if not self._username or not self._password or not self._account_id:
            self._warn("missing TRADOVATE_USERNAME / TRADOVATE_PASSWORD / "
                       "TRADING_ACCOUNT_IDS — reader disabled")
            self._disabled = True
            return False
        try:
            self._pw = sync_playwright().start()
            # Tradovate renders via WebGL canvas — must run headed on Windows.
            self._browser = self._pw.chromium.launch(headless=False)
            self._ctx = self._browser.new_context()
            self._page = self._ctx.new_page()
            self._page.set_default_timeout(30_000)
            login_and_select_account(
                self._page, self._username, self._password, self._account_id)
            return True
        except AccountNotFoundError as exc:
            self._warn(f"account gone -> reconciler disabled for the session: {exc}")
            self._disabled = True
            self.stop()
            return False
        except Exception as exc:  # pragma: no cover - browser/login flakiness
            self._warn(f"start() failed -> reader unavailable: {exc}")
            self.stop()
            return False

    def _relogin(self) -> bool:
        """Re-run the login flow on the existing page after a dropped session."""
        from broker_recon.tradovate_login import (
            AccountNotFoundError,
            login_and_select_account,
        )
        try:
            login_and_select_account(
                self._page, self._username, self._password, self._account_id)
            return True
        except AccountNotFoundError as exc:
            self._warn(f"account gone on re-login -> disabled: {exc}")
            self._disabled = True
            return False
        except Exception as exc:  # pragma: no cover
            self._warn(f"re-login failed: {exc}")
            return False

    def query_orders(self, symbol: str | None = None, since=None) -> list[dict]:
        """Read the live Orders panel from the DOM -> list of order dicts.

        Never raises: on any failure returns []. Serialized by a lock so concurrent
        reconcile threads don't drive the single page at once.
        """
        if self._disabled or self._page is None:
            return []
        with self._lock:
            try:
                return self._read_orders_grid(symbol or self._symbol, since)
            except Exception as exc:  # pragma: no cover - DOM flakiness / session drop
                self._warn(f"query_orders read failed, attempting re-login: {exc}")
                if self._relogin():
                    try:
                        return self._read_orders_grid(symbol or self._symbol, since)
                    except Exception as exc2:  # pragma: no cover
                        self._warn(f"query_orders read failed after re-login: {exc2}")
                return []

    def _read_orders_grid(self, symbol: str, since) -> list[dict]:  # pragma: no cover
        """Parse the on-screen Orders blotter rows for `symbol` into order dicts.

        NOTE: the live Orders-panel grid DOM (column layout, status text, row selectors)
        must be confirmed against the running platform during the supervised smoke test —
        the CSV-export path in reports/get_tradovate_orders.py reads a different (Reports
        modal) grid, so its selectors are not reusable verbatim. This method is intentionally
        kept thin and behind the reader interface: the reconcile classification/correction
        logic is driven by a fake reader in the unit tests and does not depend on these
        selectors. Returns [] until the selectors are confirmed live (graceful no-op:
        classify then sees no blotter rows and the reconciler skips rather than mis-acts).
        """
        page = self._page
        rows: list[dict] = []
        # The Orders panel renders rows as a grid; each row exposes its cells. Selectors are
        # confirmed during the live smoke test. Read defensively and skip unparsable rows.
        try:
            grid_rows = page.locator("div.order-row, tr.order-row, [data-order-id]")
            n = grid_rows.count()
        except Exception:
            return []
        for i in range(n):
            try:
                row = grid_rows.nth(i)
                txt = (row.text_content() or "")
                if symbol and symbol.split("1!")[0] not in txt:
                    continue
                rows.append(self._parse_row(row))
            except Exception:
                continue
        return rows

    @staticmethod
    def _parse_row(row) -> dict:  # pragma: no cover - DOM-shape dependent
        """Best-effort extraction of one order row into the canonical dict shape.

        Kept defensive: missing cells become empty/0 rather than raising.
        """
        def _cell(attr):
            try:
                return (row.get_attribute(attr) or "").strip()
            except Exception:
                return ""
        return {
            "order_id":   _cell("data-order-id"),
            "side":       _cell("data-side"),
            "type":       _cell("data-type"),
            "price":      _cell("data-price"),
            "stop_price": _cell("data-stop-price"),
            "status":     _cell("data-status"),
            "avg_fill":   _cell("data-avg-fill"),
            "time":       _cell("data-time"),
        }

    def stop(self) -> None:
        """Tear down the browser/session. Idempotent and never raises."""
        for closer, obj in (
            (lambda o: o.close(), self._ctx),
            (lambda o: o.close(), self._browser),
            (lambda o: o.stop(), self._pw),
        ):
            try:
                if obj is not None:
                    closer(obj)
            except Exception:
                pass
        self._ctx = self._browser = self._pw = self._page = None
