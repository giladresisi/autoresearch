---
name: session-analysis
description: >
  Run after a trading session ends to cross-reference all session data sources
  (strategy events, PMT alerts, Tradovate fills, 1m bar data) and produce two
  structured analysis files in sessions/<date>/: discrepancies.md (execution gaps
  between strategy intent and actual broker fills) and optimizations.md (missed
  opportunities and strategy improvement ideas). Downloads broker/PMT reports
  first if they haven't been fetched yet.
  Trigger phrases: "analyze the session", "session analysis", "write discrepancies",
  "write optimizations", "analyze yesterday's trades", "what went wrong today",
  "session review", "post-session analysis", "compare strategy vs tradovate",
  "find discrepancies", "find optimization opportunities".
---

# Session Analysis

Cross-references all session data sources and writes two structured analysis files.
Intended to be run once after a session ends and reports have been (or will be) downloaded.

---

## Step 1 — Determine session date

If the user provided a date (e.g. "analyze 2026-05-21"), use it. Otherwise use yesterday's date in `YYYY-MM-DD` format.

Check that `sessions/<date>/` exists. If it doesn't, stop:
> "No session directory found for `<date>`. Please verify the date."

---

## Step 2 — Ensure reports are downloaded

Check whether all three broker/PMT CSV files exist:
- `sessions/<date>/tradovate_orders.csv`
- `sessions/<date>/tradovate_position_history.csv`
- `sessions/<date>/pickmytrade_alerts.csv`

If any are missing, run the get-reports skill now (follow its instructions exactly):
```bash
uv run python -m reports.get_tradovate_orders --date <date>
uv run python -m reports.get_pickmytrade_alerts --date <date>
```
If fetching fails, note which files are missing and continue with what's available — a partial analysis is more useful than none.

---

## Step 3 — Spawn analysis subagent

Delegate all the reading, cross-referencing, and writing to a subagent. This keeps the main context clean and lets the subagent focus entirely on the analysis.

Use the general-purpose subagent with this prompt (filling in `<DATE>` and `<BASE>`):

```
You are a trading session analyst for an automated MNQ futures strategy.
Your job is to cross-reference all session data sources and write two analysis files.

BASE = C:\Users\gilad\projects\auto-co-trader\live
DATE = <DATE>
SESSION = <BASE>\sessions\<DATE>

---
DATA SOURCES TO READ (read ALL of them before writing anything):

1. <SESSION>\events.jsonl
   JSONL file — one JSON object per line. Each has "kind", "time", and kind-specific fields.
   Key kinds: new-hypothesis, new-stop-entry, stop-entry-filled, market-entry,
   update-stop-loss, new-stop-exit, move-stop-exit, stop-exit, stopped-out,
   market-close, cancel-limit-entry, smt-div, trend-broken.

2. <SESSION>\trades.tsv
   TSV: entry_time, entry_price, direction, contracts, exit_time, exit_price,
   exit_reason, pnl_points, pnl_dollars.
   This is the STRATEGY's own P&L record — the assumed fills, not actual broker fills.

3. <SESSION>\signals.log
   Raw stdout from automation.main. Contains [PMT] lines showing every order sent
   to PickMyTrade (with price, order ID, 200 OK confirmation). Search for:
   - "[PMT] Order pmt-XXXXXXXX sent OK (200):" lines to find exact prices sent
   - "update_sl=" values (stop-loss updates sent to broker)
   - "cancel" / "close" / "entry" order logs

4. <SESSION>\pickmytrade_alerts.csv
   PMT's own log of webhook alerts received. Has columns including alert_data
   (JSON payload). Cross-reference with signals.log to verify all orders were received.

5. <SESSION>\tradovate_orders.csv
   All orders placed at Tradovate: Order ID, Time Placed, Status, Fill Price,
   Order Type (LMT/STP/MKT), Side (B/S), Stop Price, etc.
   This is the GROUND TRUTH for what actually happened at the broker.

6. <SESSION>\tradovate_position_history.csv
   Paired round-trips: Buy Price, Sell Price, P/L, timestamps.
   Useful for verifying P&L totals and finding orphaned fills.

7. <SESSION>\levels.json
   Price levels computed at session open: TDO, TWO, week_high, week_low, day_high,
   day_low, day_mid, etc. Use these for context when evaluating targets/stops.

8. <BASE>\data\MNQ_1m.parquet
   Read with pandas: pd.read_parquet("<BASE>/data/MNQ_1m.parquet")
   Filter to the session date for bar-by-bar price context (OHLCV).
   Use to verify whether a stop was swept by a wick, whether price continued
   after an exit, and to understand market structure.

---
CROSS-REFERENCE METHODOLOGY:

For each trade in trades.tsv:
  a. Find the matching events.jsonl lines by time and direction
  b. Find the Tradovate order(s) that correspond to the entry and exit fills
     (match by approximate time ±30 seconds and direction)
  c. Compare entry price in trades.tsv vs actual Tradovate fill price
  d. Compare exit price in trades.tsv vs actual Tradovate fill price
  e. Check for stop-loss values sent via signals.log / PMT — do they match
     the stop_price in events.jsonl?
  f. Check the 1m bars surrounding entry and exit for context

For stop-loss sanity: if |sent_stop_price - entry_price| > 500 points,
flag as a likely digit-transposition error. The normal stop range is 30–200 pts.

For slippage tracking:
  - Entry slippage = (Tradovate fill) − (strategy's assumed entry in trades.tsv).
    Positive = worse for longs, better for shorts; negative = better for longs.
  - Exit slippage = similar but reversed sign convention.
  - The model assumes 0.50 pt (2 ticks) of slippage. Flag anything > 1.0 pt.

For "immediate stopouts": if (exit_time − entry_time) < 30 seconds, investigate
whether the initial stop was set at or near the fill price.

For orphaned positions: look for Tradovate fills that don't have a corresponding
entry in trades.tsv. These arise from cancel/fill race conditions.

---
OUTPUT: Write TWO files.

### File 1: <SESSION>\discrepancies.md

Structure:
```
# Session Discrepancies — <DATE>

## Summary
<2-4 sentence overview of what was found>

---

## D<N> — <Short descriptive title>

**Time**: <ISO timestamp>
**Expected**: <what the strategy intended / what should have happened>
**Actual**: <what actually happened per Tradovate/PMT logs>
**Root cause**: <why it happened — cite specific lines from signals.log or events.jsonl>
**Suggested fix**: <actionable code change or guard to add>

---
```

Include ALL meaningful discrepancies found. Skip trivial noise (sub-tick rounding).
Classify severity in the title: use "[CRITICAL]" if the bug could cause unlimited
or unintended risk, "[MINOR]" if it's cosmetic or small-impact.

### File 2: <SESSION>\optimizations.md

Structure:
```
# Strategy Optimization Opportunities — <DATE>

## Summary
<2-4 sentence overview of the session's overall performance and main themes>

---

## Raw Findings

### F<N> — <Timestamp and short description>
**What happened**: <what occurred on the bar/trade>
**Context**: <relevant levels, hypothesis, bar data>
**Potential gain**: <quantify in points/dollars if price continued as expected>

---

## Optimization Themes

### O<N> — <Theme name>: <one-line description> — [High/Medium/Low]

**Pattern**: <describe the recurring behavior and cite the supporting findings>
**Suggested fix**: <concrete change to the strategy logic>
**Supporting findings**: <list of F-numbers that back this up>
**Estimated session impact**: <estimated additional P&L if the fix had been applied>

---
```

Raw findings capture individual trade-level observations. Optimization themes
group them into actionable strategy improvements. High/Medium/Low refers to
estimated impact over many sessions, not just this one.

---
IMPORTANT: Do not write the files until you have read ALL data sources. Read first,
synthesize, then write both files in one pass. The analysis should be grounded in
specific timestamps, prices, and order IDs from the actual data — not generic advice.
```

---

## Step 4 — Report to user

Once the subagent completes, confirm:
- Which discrepancies were found (D1, D2... with one-line summaries)
- Which optimization themes were identified (O1, O2... with estimated impact)
- File paths written

If any discrepancy is marked [CRITICAL], call it out explicitly and offer to investigate the root cause in the code.
