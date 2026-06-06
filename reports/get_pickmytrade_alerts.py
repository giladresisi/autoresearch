#!/usr/bin/env python3
"""Download PickMyTrade Alerts CSV for a session date into <global>/sessions/<date>/."""
import argparse
import datetime
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Repo root on sys.path so `import paths` works when run as `python reports/<script>.py`
# (sys.path[0] is the script's own folder, not the cwd).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths

_ALERTS_URL = "https://app.pickmytrade.trade/#/dashboard/home?tab=alerts"


def _sessions_dir() -> Path:
    """Session root: the machine-global live sessions root (paths.sessions_dir()).

    SESSIONS_DIR is honored ONLY as an *absolute*-path override. A relative value like
    "sessions" is the pre-restructure worktree-local default — it is frequently still set
    in .env and never matches where the orchestrator writes (the global sessions root), so
    a relative override is ignored rather than silently resolving to the wrong folder."""
    env = os.getenv("SESSIONS_DIR")
    if env and Path(env).is_absolute():
        return Path(env)
    return paths.sessions_dir()


def run(session_date: datetime.date, *, headed: bool = False, count: int = 100) -> Path:
    load_dotenv()
    pmt_email = os.environ["PMT_EMAIL"]
    pmt_pw    = os.environ["PMT_PASSWORD"]

    out_dir = _sessions_dir() / session_date.isoformat()
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
            # The picker (shadcn Calendar / react-day-picker) shows two adjacent
            # months [N | N+1]; the starting month depends on PMT's *persisted* prior
            # range, and each pane renders adjacent-month spillover days (class
            # `day-outside`). Selecting by raw day text (.first/.last) grabs the wrong
            # month — most visibly on the 1st, where e.g. the July-1 spillover cell
            # sits in the June pane and `.last` picks it. Robust, any-date approach:
            # navigate the wanted month into the LEFT pane (stable nav aria-labels),
            # then click the first in-month (non-spillover) cell with that day number.
            today = datetime.date.today()

            date_btn = page.get_by_role("button").filter(
                has_text=re.compile(r'\w+ \d+, \d{4}')
            ).first
            date_btn.click()
            page.wait_for_timeout(300)

            _month_re = re.compile(
                r'^(January|February|March|April|May|June|July|August|September'
                r'|October|November|December)\s+\d{4}$'
            )

            def _visible_months() -> list:
                return page.get_by_text(_month_re).all_inner_texts()

            def _ensure_left_month(target: datetime.date) -> None:
                """Navigate prev/next until target's month is the LEFT (first) pane."""
                want = target.strftime("%B %Y")
                for _ in range(15):
                    months = _visible_months()
                    if not months or months[0] == want:
                        return
                    cur_left = datetime.datetime.strptime(months[0], "%B %Y").date()
                    btn = ("Go to previous month"
                           if cur_left.replace(day=1) > target.replace(day=1)
                           else "Go to next month")
                    page.get_by_role("button", name=btn).click()
                    page.wait_for_timeout(250)

            def _day_cell(target: datetime.date):
                _ensure_left_month(target)
                # target month is the LEFT pane → its in-month (non-spillover) days
                # come first in DOM, so the first such cell is unambiguous.
                return page.locator("button[name='day']:not(.day-outside)").get_by_text(
                    str(target.day), exact=True
                ).first

            # Dismiss any live-chat overlay that intercepts pointer events on the calendar.
            page.evaluate("""
                const el = document.querySelector('.circleChatBubble, [title="Live chat button"]');
                if (el) el.style.display = 'none';
            """)

            # Double-click start to set it, single-click end (matches PMT range behaviour).
            start_cell = _day_cell(session_date)
            start_cell.dispatch_event("click")
            page.wait_for_timeout(200)
            start_cell.dispatch_event("click")
            page.wait_for_timeout(200)

            _day_cell(today).dispatch_event("click")
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
