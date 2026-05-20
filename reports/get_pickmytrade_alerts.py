#!/usr/bin/env python3
"""Download PickMyTrade Alerts CSV for a session date into sessions/<date>/."""
import argparse
import datetime
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

_ALERTS_URL = "https://app.pickmytrade.trade/#/dashboard/home?tab=alerts"


def run(session_date: datetime.date, *, headed: bool = False, count: int = 100) -> Path:
    load_dotenv()
    pmt_email = os.environ["PMT_EMAIL"]
    pmt_pw    = os.environ["PMT_PASSWORD"]

    sessions_dir = Path(os.getenv("SESSIONS_DIR", "sessions"))
    out_dir = sessions_dir / session_date.isoformat()
    if not out_dir.exists():
        raise FileNotFoundError(
            f"Session directory '{out_dir}' does not exist — "
            f"no trading session was recorded for {session_date.isoformat()}."
        )
    out_path = out_dir / "pickmytrade_alerts.csv"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx  = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.set_default_timeout(30_000)

        try:
            # ── Login ──────────────────────────────────────────────────────────
            page.goto(_ALERTS_URL)
            # Wait for the SPA to mount — use the email input appearing as the
            # signal rather than a URL pattern, since PMT lands on the root URL.
            email_sel = "input[placeholder*='Email' i], input[type='email']"
            needs_login = False
            try:
                page.wait_for_selector(email_sel, timeout=20_000)
                needs_login = True
            except Exception:
                pass  # already logged in — alerts page is showing

            print(f"[pmt] needs_login={needs_login}", file=sys.stderr)

            if needs_login:
                page.locator(email_sel).first.fill(pmt_email)
                page.locator("input[type='password']").first.fill(pmt_pw)
                page.get_by_role("button", name="Login").click()
                # Wait for the app to redirect to the dashboard before navigating
                # further — the goto below would kill the login POST otherwise.
                try:
                    page.wait_for_url("*#/dashboard*", timeout=15_000)
                except Exception:
                    page.wait_for_load_state("domcontentloaded")
                page.goto(_ALERTS_URL)
                page.wait_for_load_state("domcontentloaded")

            # ── Wait for alerts page to render ────────────────────────────────
            export_btn = page.get_by_role("button", name="Export")
            export_btn.wait_for(timeout=20_000)

            # ── Date range via calendar picker ────────────────────────────────
            # Calendar shows [prev month | current month].
            # Double-click session_date to set it as start; single-click today to set end.
            today = datetime.date.today()

            date_btn = page.get_by_role("button").filter(
                has_text=re.compile(r'\w+ \d+, \d{4}')
            ).first
            date_btn.click()
            page.wait_for_timeout(300)
            page.screenshot(path=str(out_dir / "pmt_cal_1_opened.png"))

            start_day = str(session_date.day)
            end_day   = str(today.day)

            # session_date is in the current month → right calendar (.last)
            # session_date is in the previous month → left calendar (.first)
            if session_date.month == today.month:
                start_loc = page.locator("[name='day']").get_by_text(start_day, exact=True).last
            else:
                start_loc = page.locator("[name='day']").get_by_text(start_day, exact=True).first

            start_loc.click()
            page.wait_for_timeout(200)
            start_loc.click()
            page.wait_for_timeout(200)
            page.screenshot(path=str(out_dir / "pmt_cal_after_start.png"))

            # today is always in the right (current month) calendar
            page.locator("[name='day']").get_by_text(end_day, exact=True).last.click()
            page.wait_for_timeout(300)
            page.screenshot(path=str(out_dir / "pmt_cal_after_end.png"))

            # Close the calendar popup
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)

            # ── Export ─────────────────────────────────────────────────────────
            export_btn.click()

            # ── Alerts Count ───────────────────────────────────────────────────
            cnt_input = page.get_by_label("Alerts Count", exact=False)
            if not cnt_input.count():
                cnt_input = page.get_by_placeholder("Alerts Count")
            cnt_input.click(click_count=3)
            cnt_input.fill(str(count))

            # ── Download ───────────────────────────────────────────────────────
            with page.expect_download() as dl:
                page.locator("button:has-text('Download CSV'), button:has-text('Download')").first.click()
            dl.value.save_as(out_path)

        except Exception:
            page.screenshot(path=str(out_dir / "pickmytrade_error.png"))
            raise
        finally:
            browser.close()

    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--headed", action="store_true", help="Show browser window (useful for debugging selectors)")
    ap.add_argument("--date", help="Session date YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--count", type=int, default=100, help="Alerts count to export (default: 100)")
    args = ap.parse_args()
    date = (
        datetime.date.fromisoformat(args.date)
        if args.date
        else datetime.date.today() - datetime.timedelta(days=1)
    )
    path = run(date, headed=args.headed, count=args.count)
    print(f"Saved: {path}")


if __name__ == "__main__":
    main()
