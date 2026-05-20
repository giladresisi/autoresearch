#!/usr/bin/env python3
"""Download Tradovate Orders and Position History CSVs for a session date."""
import argparse
import datetime
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


def _fmt(d: datetime.date) -> str:
    return d.strftime("%m/%d/%Y")


def run(session_date: datetime.date, *, headed: bool = False) -> list[Path]:
    load_dotenv()
    username   = os.environ["TRADOVATE_USERNAME"]
    password   = os.environ["TRADOVATE_PASSWORD"]
    account_id = os.environ["TRADING_ACCOUNT_IDS"].split(",")[0].strip()

    sessions_dir = Path(os.getenv("SESSIONS_DIR", "sessions"))
    out_dir = sessions_dir / session_date.isoformat()
    if not out_dir.exists():
        raise FileNotFoundError(
            f"Session directory '{out_dir}' does not exist — "
            f"no trading session was recorded for {session_date.isoformat()}."
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx  = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.set_default_timeout(30_000)

        try:
            # ── Login ──────────────────────────────────────────────────────────
            page.goto("https://trader.tradovate.com/")
            page.wait_for_load_state("domcontentloaded")
            cookie_btn = page.locator("button:has-text('Accept Cookies')")
            if cookie_btn.is_visible(timeout=2_000):
                cookie_btn.click()
            page.locator("input[type='text'], input[type='email']").first.wait_for()
            page.locator("input[type='text'], input[type='email']").first.fill(username)
            page.locator("input[type='password']").first.fill(password)
            page.get_by_role("button", name="Login").click()
            page.wait_for_load_state("domcontentloaded")

            # ── "Select a Trading Mode" interstitial ───────────────────────────
            try:
                page.wait_for_url("**/trading-mode**", timeout=8_000)
            except Exception:
                pass
            cookie_btn = page.locator("button:has-text('Accept Cookies')")
            if cookie_btn.is_visible():
                cookie_btn.click()
                page.wait_for_timeout(300)
            live_btn = page.locator("button:has-text('Access Live')")
            sim_btn  = page.locator("button:has-text('Access Simulation')")
            if live_btn.is_visible():
                live_btn.click()
            else:
                sim_btn.click()
            page.wait_for_load_state("domcontentloaded")

            # ── Wait for platform; select the correct account ──────────────────
            reports_btn = page.locator("a.btn.btn-icon:has(.icon-columns)")
            reports_btn.wait_for(timeout=30_000)
            # The account selector is a custom dropdown in the platform header.
            # Click the inner div.account (the toggle/display) to open the list,
            # then click the target a.account link. If the dropdown doesn't open,
            # skip gracefully (the current selection may already be correct).
            acct_toggle = page.locator("div.account-selector-wrapper div.account").first
            acct_toggle.wait_for(timeout=10_000)
            acct_toggle.click()
            page.wait_for_timeout(800)
            acct_link = page.locator("a.account").filter(has_text=account_id).first
            if acct_link.is_visible():
                acct_link.click()
                page.wait_for_timeout(500)

            # ── Open Reports modal ─────────────────────────────────────────────
            reports_btn.click()

            modal = page.locator("[role='dialog'].modal")
            modal.wait_for()

            # ── Orders tab ─────────────────────────────────────────────────────
            modal.get_by_text("Orders", exact=True).click()
            page.wait_for_timeout(500)
            _set_custom_range(modal, page, session_date)

            orders_path = out_dir / "tradovate_orders.csv"
            _download_csv(modal, page, orders_path)

            # ── Position History tab ───────────────────────────────────────────
            modal.get_by_text("Position History", exact=True).click()
            page.wait_for_timeout(500)
            _set_custom_range(modal, page, session_date)

            pos_path = out_dir / "tradovate_position_history.csv"
            _download_csv(modal, page, pos_path)

        except Exception:
            page.screenshot(path=str(out_dir / "tradovate_error.png"))
            raise
        finally:
            browser.close()

    return [orders_path, pos_path]


def _set_custom_range(modal, page, session_date: datetime.date) -> None:
    """Select 'Custom Range' and fill FROM/TO date inputs, then click Go."""
    date_sel = None
    for i in range(modal.locator("select").count()):
        sel  = modal.locator("select").nth(i)
        opts = sel.locator("option").all_inner_texts()
        if any(k in " ".join(opts).lower() for k in ("today", "yesterday", "custom")):
            date_sel = sel
            break
    if date_sel is None:
        date_sel = modal.locator("select").first

    opts = date_sel.locator("option").all_inner_texts()
    # Prefer "Custom Range" (two date fields) over "Custom Date" (one date + time)
    custom_label = (
        next((o for o in opts if "custom range" in o.lower()), None)
        or next((o for o in opts if "range" in o.lower()), None)
        or next((o for o in opts if "custom" in o.lower()), None)
    )
    if custom_label:
        date_sel.select_option(label=custom_label)
    page.wait_for_timeout(300)

    # Custom Range layout: nth(0)=FROM date, nth(1)=FROM time, nth(2)=TO date
    from_input = modal.locator("input").nth(0)
    to_input   = modal.locator("input").nth(2)
    from_input.click(click_count=3)
    from_input.fill(_fmt(session_date))
    from_input.press("Tab")
    to_input.click(click_count=3)
    to_input.fill(_fmt(datetime.date.today()))
    to_input.press("Tab")

    modal.get_by_role("button", name="Go").click()
    page.wait_for_timeout(1_500)


def _download_csv(modal, page, out_path: Path) -> None:
    """Click Download CSV; if no records exist, write an empty file instead."""
    # "TOTAL: 0 records" means the button is a no-op — skip the download
    total_loc = modal.locator("text=/TOTAL:/i")
    if total_loc.count() and "0 record" in (total_loc.first.text_content() or "").lower():
        out_path.write_text("")
        return
    try:
        with page.expect_download(timeout=15_000) as dl:
            modal.get_by_role("button", name="Download CSV").click()
        dl.value.save_as(out_path)
    except Exception:
        out_path.write_text("")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--headed", action="store_true", help="Show browser window")
    ap.add_argument("--date", help="Session date YYYY-MM-DD (default: yesterday)")
    args = ap.parse_args()
    date = (
        datetime.date.fromisoformat(args.date)
        if args.date
        else datetime.date.today() - datetime.timedelta(days=1)
    )
    paths = run(date, headed=args.headed)
    for p in paths:
        print(f"Saved: {p}")


if __name__ == "__main__":
    main()
