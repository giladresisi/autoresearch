# broker_recon/tradovate_login.py
# Shared Tradovate Playwright login / trading-mode / account-select flow.
#
# Refactored out of reports/get_tradovate_orders.py so BOTH the CSV reports script and
# the live read-only orders-blotter reader (broker_recon/reader.py) drive the exact same
# sign-in sequence. The flow is browser-only (it never places orders); the reports script
# keeps its CSV-specific helpers (`_set_custom_range`, `_download_csv`).


class AccountNotFoundError(RuntimeError):
    """The configured Tradovate account is absent from the account selector.

    Raised when the account dropdown opens with other accounts but NOT the one in
    `TRADING_ACCOUNT_IDS` — i.e. the account was closed/disabled (e.g. an Apex evaluation
    blown on losses). We fail fast instead of silently downloading another account's data.
    """


def login_and_select_account(page, username: str, password: str, account_id: str) -> None:
    """Perform the cookie-accept → login → trading-mode interstitial → account-select flow
    on an already-created Playwright ``page``, leaving the platform loaded with the correct
    account selected.

    Raises ``AccountNotFoundError`` when the account selector opens with other accounts but
    not ``account_id`` (account closed/disabled). A dropdown that does not open at all is
    treated as a transient/graceful skip (the current selection may already be correct).
    """
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
