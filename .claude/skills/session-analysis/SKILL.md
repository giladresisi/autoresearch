---
name: session-analysis
description: >
  Run after a trading session ends to cross-reference all session data sources
  (strategy events, PMT alerts, Tradovate fills, 1s regression replay, 1m bar data)
  and produce two structured analysis files in sessions/<date>/: discrepancies.md
  (execution gaps between strategy intent, regression replay, and actual broker fills)
  and optimizations.md (missed opportunities and strategy improvement ideas).
  Downloads broker/PMT reports first if they haven't been fetched yet, then runs
  the 1s regression, and plots both the live session and the 1s regression replay.
  Trigger phrases: "analyze the session", "session analysis", "write discrepancies",
  "write optimizations", "analyze yesterday's trades", "what went wrong today",
  "session review", "post-session analysis", "compare strategy vs tradovate",
  "find discrepancies", "find optimization opportunities".
  Do NOT trigger for: regression runs, backtest runs, plotting charts, "run regression.py",
  "plot it", "plot the regression", or any request that mentions regression.py or backtest.
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

## Step 2.5 — Run 1s regression

Run the 1s backtest replay for the session date. This produces a deterministic
re-simulation of what the strategy *should* have done given the recorded bar data,
which is the third comparison axis in the discrepancy analysis.

```python
from regression import run_regression
results = run_regression(dates=["<date>"], mode="1s", skip_lock=True)
print(results)
```

Output files are written to `data/regression/<date>/`:
- `events_1s.jsonl` — replay event stream
- `trades_1s.tsv`   — replay trade ledger
- `chart_1s.html`   — replay chart (opened automatically)

If the regression fails (e.g. corrupted parquet), note the error and continue —
the analysis can proceed without it, but flag the gap in the report.

---

## Step 2.6 — Plot the live session AND the 1s regression (both, every run)

After the regression completes, generate BOTH charts so the live run and the
deterministic replay can be compared side by side. This double plot is a required
output of every session-analysis run.

1. **Live session chart** — what actually happened in the live run:
   ```bash
   python plot_session.py <date>
   ```
   Writes and opens `sessions/<date>/chart.html`.

2. **1s regression replay chart** — the re-simulation produced in Step 2.5.
   `plot_regression.py` imports root modules (e.g. `session_times`), so the project
   root must be on `PYTHONPATH` (running it by file path alone fails with
   `ModuleNotFoundError`). From the project root:
   ```powershell
   $env:PYTHONPATH = (Get-Location).Path; python data\regression\plot_regression.py <date>
   ```
   Writes and opens the regression chart (`chart_1m.html`) under `data/regression/<date>/`.

If either plot command fails, note the error and continue — the analysis files
(Step 3) do not depend on the charts. Report both chart paths in the final summary.

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
REGRESSION = <BASE>\data\regression\<DATE>

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

9. <REGRESSION>\events_1s.jsonl  (may not exist if regression failed)
   JSONL event stream from the 1s backtest replay — same format as events.jsonl.
   This is the deterministic re-simulation: what the strategy logic *would* have
   done given the recorded 1s bar data, with no latency or async side-effects.

10. <REGRESSION>\trades_1s.tsv  (may not exist if regression failed)
    TSV trade ledger from the 1s backtest replay — same schema as trades.tsv.
    Compare against the live trades.tsv to surface logic divergences.

11. <SESSION>\comments.md  (may not exist)
    Free-form analyst notes written during or after the session. Contains
    explanations for known anomalies (e.g. double-position incidents, manual
    interventions, broker issues) that would otherwise appear as unexplained
    discrepancies in the data. READ THIS BEFORE drawing conclusions — if
    something looks like a bug but is already explained here, do not flag it
    as a new discrepancy. Quote the relevant comment section when dismissing
    an apparent anomaly.

12. Git commits made during the session window
    Run this command to list commits that landed while the session was live
    (18:00 ET the calendar day before DATE through 17:00 ET on DATE):

    ```bash
    git -C <BASE> log --oneline --after="<DATE_MINUS_1>T18:00:00-04:00" \
        --before="<DATE>T17:00:00-04:00" --reverse
    ```
    where DATE_MINUS_1 is DATE minus one calendar day (e.g. 2026-05-28 for
    a DATE of 2026-05-29).

    For each commit hash, also fetch the full message:
    ```bash
    git -C <BASE> show --stat --format="%H %s%n%b" <hash>
    ```

    These commits represent strategy/infrastructure changes that were deployed
    mid-session AFTER the live run already occurred but are baked into the
    regression replay (which always runs on the current codebase).  They are
    the primary reason a regression trade may differ from the live trade.

---
CROSS-REFERENCE METHODOLOGY:

### A. Live vs Tradovate (execution fidelity)

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

### B. Live vs Regression (logic fidelity)

If regression files exist, compare them trade-by-trade and event-by-event against
the live session. The goal is to make all three sources converge: any divergence
between live and regression represents a real-time execution artifact that may
be worth fixing in the strategy or infrastructure.

For each trade pair (matched by entry_time ± 5 seconds and direction):
  - **Entry price**: flag if |live − regression| > 0.25 pts (1 tick)
  - **Exit time**: flag if |live − regression| > 60 seconds
  - **Exit price**: flag if |live − regression| > 0.25 pts (1 tick)
  - **Exit reason**: flag if different (e.g. "stopped-out" vs "stop-exit")
  - **Contracts**: note differences but do not flag as a bug (may be a config diff)

For trades present in regression but not in live (or vice versa):
  Flag as a logic divergence — the real-time system fired (or skipped) a trade
  that the replay did not, indicating a timing, state, or bar-delivery difference.

For event-level comparison (events.jsonl vs events_1s.jsonl):
  - Match events by kind + approximate time (± 5 seconds)
  - Flag price differences > 1 tick on stop placements, entry signals, or exits
  - Flag any event kind present in one stream but absent in the other
  - Pay attention to "move-stop-entry" events: count how many iterations occurred
    live vs in the replay (a difference here reveals bar-timing sensitivity)

Known acceptable differences between live and regression (do NOT flag these):
  - Sub-second timestamp jitter (< 2s) on the same event
  - "direction" label format ("long" vs "up") — cosmetic, different code paths
  - Volume fields — 1s live bars and Databento 1m bars use different feeds
  - Contract count — regression may use a different default than the live config

### C. Commit context — connecting mid-session commits to divergences

This section reconciles the divergences found in section B against the commits
collected in data source #12.  Run it AFTER completing section B.

**Step C1 — Match divergences to commits**

For each live↔regression divergence found in section B (trade present only in
regression, trade with a different entry/exit, or event that fired in regression
but not live):

  a. Read every mid-session commit message and its changed files.
  b. Ask: could this commit have caused or explained the divergence?
     Indicators: the commit touches a code path relevant to the divergence
     (e.g. a commit that unblocks O5 entries → regression now takes a market
     entry that was blocked during the live run), or the commit message
     explicitly mentions the scenario (e.g. "allow O5 when retries exhausted").
  c. If a match is found, annotate the divergence with:
     `Explained by commit <short-hash>: "<commit subject>"`
     Briefly state why the commit explains the difference (one sentence).
  d. If no commit explains the divergence, leave it unannotated — it is a
     genuine live/regression discrepancy that needs its own investigation.

**Step C2 — Check whether targeted commits actually fixed their stated issue**

For each mid-session commit whose message implies a specific fix or improvement
(e.g. "fix entry blocked after ATH rejection", "allow gap-through when retries
exhausted", "unblock DOWN hypothesis at ATH"):

  a. Identify what the commit was intended to fix or enable.
  b. Find the specific session scenario the commit targets
     (e.g. "no DOWN entry after ATH tag at 10:05") in the regression output.
  c. Verify whether the regression shows the fix taking effect:
     - If YES: note it as RESOLVED in the output (commit fixed it as intended).
     - If NO: flag it as UNRESOLVED — the commit exists but the issue still
       appears in the regression.  Describe concretely what the regression
       showed instead of the expected fixed behavior.
  d. "Implied fixes": even if the commit message is generic (e.g. "refactor
     ATH confirmation logic"), if the analyst can reasonably infer the intent
     from the changed code AND the session had a relevant occurrence, apply the
     same RESOLVED / UNRESOLVED check.

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

**Source**: <which comparison revealed this: Live↔Tradovate, Live↔Regression, or all three>
**Time**: <ISO timestamp>
**Expected**: <what the strategy intended / what the regression produced>
**Actual**: <what actually happened per live logs / Tradovate>
**Root cause**: <why it happened — cite specific lines from signals.log or events.jsonl>
**Commit context**: <"Explained by commit <hash>: <subject>" if a mid-session commit
    caused this divergence, otherwise omit this field entirely>
**Suggested fix**: <actionable code change or guard to add; omit if commit already fixed it>

---

## Mid-Session Commits — Impact Summary

List every commit from data source #12.  For each:

### <short-hash> — <commit subject>

**Intent**: <what the commit was meant to fix or enable — inferred from message + diff>
**Session scenario**: <which specific occurrence in this session it targets>
**Regression result**: RESOLVED — <what the regression now shows, confirming the fix>
              OR
              UNRESOLVED — <what the regression showed instead; describe the gap concretely>
**Divergences explained**: <list D-numbers whose live↔regression difference this commit
    accounts for, or "none">

---
```

Include ALL meaningful discrepancies found. Skip trivial noise (sub-tick rounding,
cosmetic label differences, known acceptable differences listed above).
Classify severity in the title: use "[CRITICAL]" if the bug could cause unlimited
or unintended risk, "[MINOR]" if it's cosmetic or small-impact.
The "Mid-Session Commits" section is required whenever data source #12 returns at
least one commit.  If there are no mid-session commits, omit the section entirely.

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
**Suggested fix**: <concrete change to the strategy logic; if a mid-session commit
    already addresses this, write "Already addressed by commit <hash>" instead>
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
- Which discrepancies were found (D1, D2... with one-line summaries and source tag)
- Which optimization themes were identified (O1, O2... with estimated impact)
- File paths written

If any discrepancy is marked [CRITICAL], call it out explicitly and offer to investigate the root cause in the code.
