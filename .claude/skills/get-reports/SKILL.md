---
name: get-reports
description: >
  Download a session's reports from Tradovate (Orders + Position History CSVs) and
  PickMyTrade (Alerts CSV) into sessions/<YYYY-MM-DD>/.
  Trigger phrases: "get the reports", "download reports", "fetch reports",
  "get yesterday's reports", "download orders CSV", "get tradovate orders",
  "get pickmytrade alerts", "download session reports".
---

# get-reports

Downloads three CSVs for a trading session:
- `sessions/<date>/tradovate_orders.csv`
- `sessions/<date>/tradovate_position_history.csv`
- `sessions/<date>/pickmytrade_alerts.csv`

## Execution

1. Determine the session date:
   - If the user provided a date, use it (format `YYYY-MM-DD`).
   - Otherwise, compute yesterday's date as `YYYY-MM-DD`.

2. **Check that `sessions/<date>/` exists.** If it does not exist, stop and notify the user:
   > "No session directory found for `<date>`. This means no trading session was recorded for that day. Please verify the date."

3. Run both scripts sequentially — if one fails, continue with the other:

```bash
uv run python -m reports.get_tradovate_orders --date <date>
uv run python -m reports.get_pickmytrade_alerts --date <date>
```

Always pass `--date` explicitly (never rely on the script's default).

4. Report which files were saved and their sizes, or show the error for any that failed.

## Account gone / closed (no Tradovate reports possible)

`get_tradovate_orders` detects when the account in `TRADING_ACCOUNT_IDS` is **absent from the
Tradovate account selector** — i.e. it was closed/disabled (e.g. an Apex evaluation blown on
losses). In that case it prints a single machine-detectable line and exits **7** *without*
downloading another account's data:

```
TRADOVATE_ACCOUNT_MISSING: Tradovate account '<id>' was not found in the account selector (available: [...]). The account was likely closed/disabled — no reports can be fetched for it.
```

When you see `TRADOVATE_ACCOUNT_MISSING` (or exit code 7):
- **Do NOT** treat it as a transient selector bug or retry with `--headed`.
- **Notify the user clearly**: the configured Tradovate account appears to be closed/disabled,
  so its Orders / Position History reports cannot be fetched. Quote the `available: [...]` list
  so they can see which accounts (if any) still exist, and confirm whether `TRADING_ACCOUNT_IDS`
  in `.env` should point at a different account.
- **Continue** with whatever reports DID succeed (PMT alerts are keyed by API key, not the
  Tradovate account, so they fetch independently). A partial report set is fine; downstream
  analysis (session-analysis) proceeds without the broker reports.

## Debugging selectors

If a script fails with a selector/timeout error, re-run with `--headed` to watch the browser:

```bash
uv run python -m reports.get_tradovate_orders --date <date> --headed
uv run python -m reports.get_pickmytrade_alerts --date <date> --headed
```

A screenshot is also saved alongside the CSV on failure (e.g. `tradovate_error.png`).

## Required env vars

| Var | Purpose |
|---|---|
| `TRADOVATE_USERNAME` | Tradovate web login username |
| `TRADOVATE_PASSWORD` | Tradovate web login password |
| `TRADING_ACCOUNT_IDS` | Comma-separated account IDs; first is used for the Tradovate Reports selector |
| `PMT_EMAIL` | PickMyTrade login email |
| `PMT_PASSWORD` | PickMyTrade login password |
| `SESSIONS_DIR` | Sessions root directory (default: `sessions`) |

If any are missing, tell the user which ones to add to `.env`.

## First-time setup

If Playwright browsers aren't installed yet:

```bash
uv run playwright install chromium
```
