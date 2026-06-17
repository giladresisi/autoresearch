#!/usr/bin/env python3
"""Download Tradovate Orders and Position History CSVs for a session date."""
import argparse
import datetime
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Repo root on sys.path so `import paths` works when run as `python reports/<script>.py`
# (sys.path[0] is the script's own folder, not the cwd).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths


class AccountNotFoundError(RuntimeError):
    """The configured Tradovate account is absent from the account selector.

    Raised when the account dropdown opens with other accounts but NOT the one in
    `TRADING_ACCOUNT_IDS` — i.e. the account was closed/disabled (e.g. an Apex evaluation
    blown on losses). We fail fast instead of silently downloading another account's data.
    """


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


def _fmt(d: datetime.date) -> str:
    return d.strftime("%m/%d/%Y")


def _convert_timestamps_to_et(path: Path) -> None:
    """Subtract 11 h from Timestamp and Fill Time columns (Tradovate exports UTC+7; ET = UTC-4 in summer)."""
    import csv as _csv
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return
    rows = list(_csv.DictReader(text.splitlines()))
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    offset = datetime.timedelta(hours=11)
    ts_fmt = "%m/%d/%Y %H:%M:%S"
    for row in rows:
        for col in ("Timestamp", "Fill Time"):
            val = (row.get(col) or "").strip()
            if val:
                try:
                    dt = datetime.datetime.strptime(val, ts_fmt) - offset
                    row[col] = dt.strftime(ts_fmt)
                except ValueError:
                    pass
        if "Date" in fieldnames:
            ts_val = (row.get("Timestamp") or "").strip()
            if ts_val:
                try:
                    dt = datetime.datetime.strptime(ts_val, ts_fmt)
                    row["Date"] = f"{dt.month}/{dt.day}/{str(dt.year)[2:]}"
                except ValueError:
                    pass
    import io as _io
    buf = _io.StringIO()
    writer = _csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8")


def run(session_date: datetime.date, *, headed: bool = False) -> list[Path]:
    load_dotenv()
    username   = os.environ["TRADOVATE_USERNAME"]
    password   = os.environ["TRADOVATE_PASSWORD"]
    account_id = os.environ["TRADING_ACCOUNT_IDS"].split(",")[0].strip()

    out_dir = _sessions_dir() / session_date.isoformat()
    if not out_dir.exists():
        raise FileNotFoundError(
            f"Session directory '{out_dir}' does not exist — "
            f"no trading session was recorded for {session_date.isoformat()}."
        )

    with sync_playwright() as p:
        # Tradovate renders via WebGL canvas — must run headed on Windows
        browser = p.chromium.launch(headless=False)
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
            # Use JS click() to bypass any cookie-banner z-index overlay
            page.evaluate("""() => {
                for (const b of document.querySelectorAll('button')) {
                    if (b.textContent.trim() === 'Accept Cookies') { b.click(); break; }
                }
            }""")
            page.wait_for_timeout(500)
            # Try clicking any mode-selection element (may not be a <button>)
            page.evaluate("""() => {
                const labels = [
                    'Access Live', 'Start Live Trading',
                    'Access Simulation', 'Start Simulated Trading',
                ];
                for (const el of document.querySelectorAll('button, a, div, span')) {
                    if (labels.some(l => el.textContent.trim().includes(l))) {
                        el.click(); break;
                    }
                }
            }""")
            page.wait_for_timeout(2_000)
            if "trading-mode" in page.url:
                # Mode element wasn't a navigating element — go directly to the platform
                page.goto("https://trader.tradovate.com/", wait_until="domcontentloaded")
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
            else:
                # Distinguish "account gone" from "dropdown didn't open". List the accounts
                # the selector actually offers; if it opened with accounts but ours is absent,
                # the account was closed/disabled (e.g. an Apex eval blown on losses) — fail
                # fast with a clear, machine-detectable error instead of silently downloading
                # another account's data. If the dropdown didn't open at all (no a.account
                # links), keep the old graceful skip (transient UI; current selection may be ok).
                _avail = page.locator("a.account")
                _names = [
                    (_avail.nth(i).text_content() or "").strip()
                    for i in range(_avail.count())
                ]
                _names = [n for n in _names if n]
                if _names:
                    raise AccountNotFoundError(
                        f"Tradovate account '{account_id}' was not found in the account "
                        f"selector (available: {_names}). The account was likely "
                        f"closed/disabled — no reports can be fetched for it."
                    )

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
            _convert_timestamps_to_et(orders_path)

            # ── Position History tab ───────────────────────────────────────────
            modal.get_by_text("Position History", exact=True).click()
            page.wait_for_timeout(500)
            _set_custom_range(modal, page, session_date)

            pos_path = out_dir / "tradovate_position_history.csv"
            _download_csv(modal, page, pos_path)
            _convert_timestamps_to_et(pos_path)

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
    # Wait for the report grid to finish rendering after the Go click.
    total_loc = modal.locator("text=/TOTAL:/i")
    try:
        total_loc.first.wait_for(timeout=15_000)
    except Exception:
        pass
    # On a tab switch the grid briefly shows a stale/transient "TOTAL: 0 records"
    # before the real rows load. Checking it once (and short-circuiting) is what made
    # Orders and Position History fail in alternation. Instead, fold the 0-records
    # check INTO a retry loop: let the grid settle, and only treat 0-records as final
    # if it persists for the whole loop. Records present → download (with retries for
    # the export race); genuinely empty → empty file.
    for _ in range(4):
        page.wait_for_timeout(1_200)
        txt = (total_loc.first.text_content() or "").lower() if total_loc.count() else ""
        if "0 record" in txt:
            continue  # likely a transient load state — re-check next pass
        try:
            with page.expect_download(timeout=12_000) as dl:
                modal.get_by_role("button", name="Download CSV").click()
            dl.value.save_as(out_path)
            return
        except Exception:
            page.wait_for_timeout(1_500)
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
    try:
        paths = run(date, headed=args.headed)
    except AccountNotFoundError as e:
        # Machine-detectable marker so callers (get-reports / session-analysis skills) can
        # recognize the account-gone case and proceed without these reports.
        print(f"TRADOVATE_ACCOUNT_MISSING: {e}", flush=True)
        sys.exit(7)
    for p in paths:
        print(f"Saved: {p}")


if __name__ == "__main__":
    main()
