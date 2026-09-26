---
name: session-analysis
description: >
  Run after a trading session ends to cross-reference all session data sources
  (strategy events, the trader graft's decision log, PMT alerts, Tradovate fills, the
  deterministic trader REPLAY of the session, 1m bar data, and the session's comments.md)
  and produce three structured analysis files in the session folder
  (`<global>/sessions/<date>/`): discrepancies.md
  (execution gaps between strategy intent, the replay, and actual broker fills),
  optimizations.md (missed opportunities and strategy improvement ideas — including
  feature proposals the operator recorded in comments.md), and
  session-analysis.md (a consolidated digest tying together data-health, P&L,
  discrepancies, and optimizations — the file to open first).
  Downloads broker/PMT reports first if they haven't been fetched yet, then runs
  the trader replay (agent/trader, NOT the legacy 1s regression) seeded with the
  session's recorded L1 thesis, and plots the live session.
  Trigger phrases: "analyze the session", "session analysis", "write discrepancies",
  "write optimizations", "analyze yesterday's trades", "what went wrong today",
  "session review", "post-session analysis", "compare strategy vs tradovate",
  "find discrepancies", "find optimization opportunities".
  Do NOT trigger for: regression runs, backtest runs, replay runs on their own, plotting
  charts, "run regression.py", "plot it", or any request that mentions regression.py,
  replay_session.py or backtest.
---

# Session Analysis

Cross-references all session data sources and writes three structured analysis files
(`discrepancies.md`, `optimizations.md`, and the consolidated `session-analysis.md`).
Intended to be run once after a session ends and reports have been (or will be) downloaded.

**Which engine is live (since 2026-09-18, commit 99bb32b).** The live pipeline runs
`trader_only=True`: the legacy trend/SMT/hypothesis/strategy engine is BLOCKED and every
entry comes from the trader graft (`agent/trader`: L1 Analyzer at 09:20 ET, Planner,
Executor; market-only mechanisms `fvg_1m_post_extreme`, `extreme_reject_close`,
`tmso_reject`; no entries at/after 10:30 ET; open position flattened at 13:00 ET). The
graft's simulated orders are mirrored into the legacy signal vocabulary
(`market-entry` / `market-close` with `"source": "agent"`) and dispatched to PMT through
`live_orders` by `automation/agent_dispatch.py`, which also writes its own per-event record
to `<session>/agent_dispatch.jsonl`, so `events.jsonl`, `signals.log`, `position.json` and the
broker reports keep their legacy shapes. The deterministic counterpart is therefore the
**trader replay** (`scripts/replay_session.py`), not `regression.py`.

---

## Path conventions

- **Sessions** live in the machine-global sessions root: `<global>/sessions/<date>/`,
  where `<global>` is env `ACT_GLOBAL_DIR` (default `~/projects/auto-co-trader/global`).
  Resolve it with `paths.sessions_dir()` (`import paths; paths.sessions_dir() / "<date>"`).
  This ONE folder is where every producer writes and every consumer reads: the live
  orchestrator (`events.jsonl`, `signals.log`, `position.json`, `levels.json`, `daily.json`),
  the trader graft (`thesis_state.json`, `plans.json`, `trader_decisions.jsonl`,
  `facts_journal.jsonl`, `facts_snapshot.json`, `snapshots/`), the `live-comment` skill
  (`comments.md`), the `run-orchestrator` skill (the running-commit line in `comments.md`),
  and the `get-reports` extractors (`tradovate_orders.csv`, `tradovate_position_history.csv`,
  `pickmytrade_alerts.csv`). Never look anywhere else for session inputs.
- **Replay** outputs are worktree-local, per-run, under the per-date regression root:
  `regression/sessions/<date>/<HH-MM-SS>/` (one timestamped folder per run; `HH-MM-SS` is
  the TH/Asia-Bangkok start time — the replay shares `paths.regression_run_dir` with the
  legacy regression). A replay run folder holds `trader_decisions.jsonl`, `plans.json` and
  `thesis_state.json`; it never writes `events.jsonl` / `trades_1s.tsv` (those are legacy
  regression artifacts and will NOT exist). Use the run folder you produced in Step 2.5.

## Step 1 — Determine session date

If the user provided a date (e.g. "analyze 2026-05-21"), use it. Otherwise use yesterday's date in `YYYY-MM-DD` format.

Check that `<global>/sessions/<date>/` exists (`paths.sessions_dir() / "<date>"`). If it doesn't, stop:
> "No session directory found for `<date>`. Please verify the date."

---

## Step 2 — Ensure reports are downloaded

Check whether all three broker/PMT CSV files exist (under `<global>/sessions/<date>/`):
- `<global>/sessions/<date>/tradovate_orders.csv`
- `<global>/sessions/<date>/tradovate_position_history.csv`
- `<global>/sessions/<date>/pickmytrade_alerts.csv`

If any are missing, run the get-reports skill now (follow its instructions exactly):
```bash
uv run python -m reports.get_tradovate_orders --date <date>
uv run python -m reports.get_pickmytrade_alerts --date <date>
```
If fetching fails, note which files are missing and continue with what's available — a partial analysis is more useful than none.

**Account closed / gone — no Tradovate reports.** If `get_tradovate_orders` prints
`TRADOVATE_ACCOUNT_MISSING` (or exits 7), the configured Tradovate account is closed/disabled
(e.g. an Apex evaluation blown on losses), so its Orders / Position History CSVs cannot be
fetched and **do not exist** for this session. When this happens:
- **Notify the user** plainly that the broker account appears closed/disabled, so there is no
  Tradovate ground-truth this session (execution-fidelity / slippage / orphaned-fill checks
  cannot be done). Quote the `available: [...]` accounts from the marker line.
- **Proceed with the analysis anyway** on the remaining sources (events.jsonl, trades_full.tsv,
  signals.log, pickmytrade_alerts.csv, levels.json, comments.md, the trader artifacts, the
  parquet bars, and the replay). The **Live↔Replay** axis (section B) and the strategy ledger
  are the focus; **skip section A (Live↔Tradovate)** entirely.
- In the Step-3 subagent prompt, state explicitly that the Tradovate reports are unavailable
  because the account was closed, that section A must be skipped, and that `discrepancies.md` +
  `session-analysis.md` must record the missing broker ground-truth (with the reason).

---

## Step 2.1 — Rebuild a complete trade ledger from events.jsonl

The live `trades.tsv` is written from the orchestrator relay's **in-memory, per-run** events
and overwritten at each session-end, so it loses round-trips across restarts/terminations
(GIL-13). Rebuild a complete ledger from the persistent, append-only `events.jsonl` before
analysis:

```bash
uv run python -m scripts.rebuild_trades_from_events --date <date>
```

This writes `<global>/sessions/<date>/trades_full.tsv` (the original `trades.tsv` is left
intact). It pairs `market-entry` / `stop-entry-filled` with the FIRST following
`market-close` / `stopped-out` / `stop-exit`. Suspect pairings — bad (`<=0`) price, or an
implausibly long hold (a missing-exit / phantom) — keep the row but blank the P&L and tag
`exit_reason` with `|suspect:...`; a fill with no matching exit (a phantom) is emitted as
`unpaired-open`. The analysis reads **`trades_full.tsv`** as the strategy ledger, and the
gap between it and the live `trades.tsv` is itself a data-quality finding (that IS D1).

**Phantom SECOND exit (trader era).** A manual `trade.py close` flattens the broker and
clears `position.json`, but the graft's simulator keeps its position until its own stop /
target / 13:00 fires — and when it does, the mirror emits ANOTHER `market-close` (reason
`target` / `hard-close-13:00`) or `stopped-out`, PMT is sent a close on a flat account, and a
second exit lands in `events.jsonl`. The rebuild pairs the entry with the FIRST exit (the
real, `user-requested` one); the later exit is unpaired noise. Treat it as a discrepancy of
the mirror/manual-close interaction (not a broker fill) and exclude it from P&L.

---

## Step 2.5 — Run the trader replay (seeded with the session's recorded thesis)

The deterministic re-simulation of the session is the trader replay: the same
Planner/Executor code the live graft ran, on the recorded 1s bars, 09:20 → 13:00 ET,
no legacy engine. **The L1 thesis is fixed input** — inject the thesis the live Analyzer
actually produced, otherwise the replay would make a fresh model call and the two runs
would diverge on the thesis rather than on execution.

1. Extract the live thesis (the `thesis` object inside the session's `thesis_state.json`)
   into an oracle file next to it:

   ```bash
   uv run python -c "import json,sys,paths; s=paths.sessions_dir()/'<date>'; d=json.load(open(s/'thesis_state.json',encoding='utf-8')); json.dump(d['thesis'], open(s/'replay_thesis.json','w',encoding='utf-8'), indent=1); print('armed_at', d.get('armed_at'), 'latency', (d.get('call_meta') or {}).get('latency_sec'))"
   ```

   If `thesis_state.json` is missing, the day was DARK (no 09:20 thesis: late start,
   Analyzer failure, or `stands: false`). Skip the replay, and record in the analysis
   that the trader never armed (that is itself a finding: why?).

2. Run the replay with that oracle. Keep the default arrival gate (40 s ≈ the live call's
   latency; the recorded `latency_sec` above tells you how close). NEVER pass `--seed`
   here — that allows a real model call and would replace the live thesis.

   ```bash
   uv run python scripts/replay_session.py --dates <date> --thesis-file "<global>/sessions/<date>/replay_thesis.json"
   ```

   The CLI prints `[replay] <date> done -> <run_dir>` and a P&L block. Note `<run_dir>`
   (`regression/sessions/<date>/<HH-MM-SS>/`) — it is the `<REPLAY>` folder Step 3 reads.

3. Replay P&L, machine-readable (points; dollars at `TRADING_CONTRACTS`, MNQ $2/pt):

   ```bash
   uv run python scripts/report_replay_pnl.py "<run_dir>" --json
   ```

   A `mark` row is a position still open at the window end, NOT an exit — it is reported
   separately (`marked` vs `realised`); in live the 13:00 hard close would have flattened it.

If the replay fails (e.g. corrupted parquet, refused call), note the error and continue —
the analysis can proceed without it, but flag the gap in the report.

---

## Step 2.6 — Plot the live session

Generate the live session chart (the mirror emits legacy-shaped events, so the live
chart shows the trader's entries/exits exactly as before):

```bash
python plot_session.py <date>
```
Writes and opens `<global>/sessions/<date>/chart.html`.

**No replay chart exists yet.** The replay writes no `events.jsonl`, so the legacy
`regression/plot_regression.py` cannot draw it. Do not attempt it; record
"replay chart: not available (no plotting tool for trader_decisions.jsonl yet)" under
Artifacts. If the plot command fails, note the error and continue — the analysis files
(Step 3) do not depend on the chart.

---

## Step 3 — Spawn analysis subagent

Delegate all the reading, cross-referencing, and writing to a subagent. This keeps the main context clean and lets the subagent focus entirely on the analysis.

Use the general-purpose subagent with this prompt. Fill in `<DATE>`, `<BASE>` (the
worktree root), `<GLOBAL>` (env `ACT_GLOBAL_DIR`, default
`~/projects/auto-co-trader/global` — resolve with `paths.global_root()`), and `<RUN>`
(the replay run folder produced in Step 2.5, e.g. `<BASE>\regression\sessions\<DATE>\<HH-MM-SS>`):

```
You are a trading session analyst for an automated MNQ futures strategy.
Your job is to cross-reference all session data sources and write three analysis files.

BASE = C:\Users\gilad\projects\auto-co-trader\live           # the worktree root
GLOBAL = <GLOBAL>                                            # ACT_GLOBAL_DIR; default ~/projects/auto-co-trader/global
DATE = <DATE>
SESSION = <GLOBAL>\sessions\<DATE>
REPLAY = <RUN>                                               # <BASE>\regression\sessions\<DATE>\<HH-MM-SS> (the replay from Step 2.5)

WHICH ENGINE WAS LIVE: since 2026-09-18 (commit 99bb32b) the legacy engine is blocked
(trader_only) and every entry comes from the trader graft (agent/trader): L1 Analyzer arms on
the 09:20 ET bar (one OpenRouter call), a plan derives on the next bar close, the Executor
enters by MARKET only via fvg_1m_post_extreme (§6), extreme_reject_close (§7) or tmso_reject,
with a fixed stop (embedded at the broker) and ONE T2 target picked at the fill (exit = market
close on a tick touch). No breakeven / trail / cautious / initial target exists. No new entries
at/after 10:30 ET; an open position is flattened at 13:00 ET (reason "hard-close-13:00").
Reaching the target kills the plan (no re-entry that day); a stop-out spends one of 3 attempts.
The 16:55 ET session-end close and pause/resume still apply through live_orders.

---
DATA SOURCES TO READ (read ALL of them before writing anything):

1. <SESSION>\events.jsonl
   JSONL file — one JSON object per line. Each has "kind", "time", and kind-specific fields.
   Agent-era order kinds (dispatched to the broker): market-entry {direction up/down,
   price, stop, source "agent", mechanism}, market-close {price, reason: "stop_out" |
   "take_profit" | "window_end" | "user-requested" | "session-end", source}. EVERY exit is
   a market-close carrying `skip_recon` — there is no `stopped-out` order kind on this
   path; a stop-out is a market-close with reason "stop_out". Session-start seeding still writes legacy informational kinds
   (new-hypothesis at startup, smt-div with source "v2-warmup", liquidity/levels) — they
   place nothing. Any legacy ENTRY kind (new-stop-entry, stop-entry-filled, move-stop-entry,
   new-stop-exit, move-stop-exit, stop-exit) appearing here is a [CRITICAL] discrepancy:
   the legacy engine is supposed to be blocked.

2. <SESSION>\trades_full.tsv   (complete strategy ledger — rebuilt from events.jsonl in Step 2.1)
   TSV: entry_time, entry_price, direction, contracts, exit_time, exit_price,
   exit_reason, pnl_points, pnl_dollars.
   The STRATEGY's own P&L record (assumed fills, not actual broker fills), complete across
   restarts. Rows whose exit_reason contains "suspect:" (bad-price / long-hold) or
   "unpaired-open" are FLAGGED anomalies — exclude their P&L from totals but DO surface them.
   A SECOND exit after a "user-requested" close (see Step 2.1, phantom second exit) is
   mirror noise, not a fill — exclude it and record it as a discrepancy of the
   manual-close/simulator interaction. The original <SESSION>\trades.tsv is the live relay's
   restart-lossy record; a gap between trades_full.tsv and trades.tsv is D-class material.

3. <SESSION>\signals.log
   Raw stdout from automation.main. Contains [PMT] lines showing every order sent
   to PickMyTrade (with price, order ID, 200 OK confirmation) and [TRADER] lines mirroring
   the graft's decision log. Search for:
   - "[PMT] Order pmt-XXXXXXXX sent OK (200):" lines to find exact orders sent
   - "update_sl=" values (stop-loss updates sent to broker)
   - "cancel" / "close" / "entry" order logs
   - "[broker_recon" lines (the post-fill stop-loss reconcile; a "start() failed" line
     means the reconcile did not run — note it, it is not an order-path failure)

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

8. <GLOBAL>\general\live\MNQ_1m.parquet   (LIVE PRODUCTION — what the live run saw)
   Read with pandas: pd.read_parquet("<GLOBAL>/general/live/MNQ_1m.parquet")
   Filter to the session date for bar-by-bar price context (OHLCV).
   Use to verify whether a stop was swept by a wick, whether price continued
   after an exit, and to understand market structure.
   NOTE on path separation: the live production parquets live under
   `<GLOBAL>\general\live\` (resolve with `paths.general_live_dir()`); the BACKTEST read
   source the replay in Step 2.5 runs on is the current contract subfolder under
   `<GLOBAL>\general\main\`. After a successful session-end parquet-check these are in
   sync (it promotes live → main) — for live-run bar context read **general/live**. Never
   the legacy worktree-local `data/`.

9. <SESSION>\thesis_state.json   (LIVE L1 thesis, written by the Analyzer at 09:20 ET)
   Fields: armed_at, thesis {bias, regime, confidence, dol {level, price}, dol_rationale,
   evidence [...], falsified_if [...], falsified_if_rationale, recall}, stands, facts_health,
   call_meta {latency_sec, usage}. Missing ⇒ the day was DARK (say why: late start after
   09:20, Analyzer failure, stands=false).

10. <SESSION>\plans.json   (LIVE plan(s) derived from the thesis)
    {plan_id: {direction, dol, valid_while (falsifiers), armed_classes, attempts_used,
    cooldown_until, created_at, last_trend}}. armed_classes MUST be exactly the market set
    [fvg_1m_post_extreme, extreme_reject_close, tmso_reject, fvg_1h_reject, micro_smt_reject]
    (updated 2026-09-26: `fvg_1h_reject` was already unconditionally armed and this line had
    fallen behind; `micro_smt_reject` (O3) joined it at adoption, both flag-gated ON by
    default — see `l2-mechanisms.md` §7a); anything else is [CRITICAL].

11. <SESSION>\trader_decisions.jsonl   (LIVE Executor decision log — the graft's own truth)
    JSONL. Kinds: fill {mechanism, price, direction, artifact_id}, target_selected {pick
    {id, level, price, dist_ratio, band}, target}, stop_out, take_profit, hard_close, mark,
    cancel_entry_cutoff, plan_dead {reason: target_reached | hard_close | falsified |
    attempts_exhausted | ..., detail}, would_have_falsified (falsifier met, NOT acted on —
    plans no longer die on falsification), would_have_killed (dol_reached, recorded only),
    veto / no-move-zone notes — including `reason: micro_smt_exit_unwired` (O4 would have
    market-closed the position but the live order port has no `flatten`; replay-only, one
    deduped record per position — see `l2-mechanisms.md` §7b). This is the primary record
    of WHY each entry/exit happened.

12. <REPLAY>\trader_decisions.jsonl, <REPLAY>\plans.json, <REPLAY>\thesis_state.json
    (may not exist if the replay failed / dark day)
    The deterministic re-simulation on the recorded 1s bars with the SAME thesis injected
    (Step 2.5). Same kinds as #11. The replay's thesis is the live thesis by construction —
    do not diff them; diff plans and Executor decisions. Replay P&L: the JSON from
    scripts/report_replay_pnl.py (Step 2.5.3) — `realised` vs `marked` (a mark is the
    position open at the 13:00 window end, not an exit).

13. <SESSION>gent_dispatch.jsonl   (the agent's own order record, plan 38)
    One JSON line per simulated order event the port reported: the signal built from it,
    the ack (read back off position.json), `dispatch_ms`, and whether it was suppressed or
    deduped on `(plan_id, seq)`. This is where an order that the brain decided but the
    broker never took shows up. Cross-reference against the [PMT] lines in signals.log:
    an event here with no PMT line, or an ack that is not ok, is a D-class finding. Also
    carries the watchdog's `external_kill` / `session_disarmed` records.

14. <SESSION>\comments.md  (MUST read; may not exist)
    Operator notes written during or after the session, plus the "Running commit" line the
    run-orchestrator skill adds at startup. Contains explanations for known anomalies
    (manual interventions, `trade.py close`, broker issues) that would otherwise appear as
    unexplained discrepancies. READ THIS BEFORE drawing conclusions — if something looks
    like a bug but is already explained here, do not flag it as a new discrepancy; quote
    the relevant comment when dismissing it. ALSO: any feature / behavior the operator
    proposes or "re-approves" in a comment (e.g. an initial-target stage for position
    management, flattening the simulator on manual close) MUST be carried into
    optimizations.md as its own O-theme, quoting the comment as supporting evidence and
    grounding the estimated impact in this session's tape — even when the data alone
    would not have surfaced it.

15. Git commits made during the session window
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

    Compare against the "Running commit" line in comments.md: commits AFTER it were
    deployed after the live run already occurred but are baked into the replay (which
    always runs on the current codebase). They are the primary reason a replay decision
    may differ from the live one.

---
CROSS-REFERENCE METHODOLOGY:

### A. Live vs Tradovate (execution fidelity)

For each trade in trades_full.tsv (clean rows):
  a. Find the matching events.jsonl lines by time and direction, and the matching
     trader_decisions.jsonl fill / exit records (they carry the mechanism and target)
  b. Find the Tradovate order(s) that correspond to the entry and exit fills
     (match by approximate time ±30 seconds and direction)
  c. Compare entry price in trades_full.tsv vs actual Tradovate fill price
  d. Compare exit price in trades_full.tsv vs actual Tradovate fill price
  e. Check the stop-loss sent via signals.log / PMT (the `sl` embedded in the market entry,
     and any `update_sl=`) — does it match the `stop` in events.jsonl / trader_decisions?
  f. Check the 1m bars surrounding entry and exit for context
  g. Every [PMT] close in signals.log must correspond to a real position: a close sent on
     a FLAT account (phantom second exit after a manual close; the 13:00 / 16:55 safety
     closes when nothing was open) is expected mirror behavior — record it once, do not
     count it as a fill.

For stop-loss sanity: if |sent_stop_price - entry_price| > 500 points,
flag as a likely digit-transposition error. The normal trader-era stop is the mechanism's
own (§7: opposite wick capped at 15 pts; §6: per l2-mechanisms.md), so anything > 60 pts
is suspicious in this era.

For slippage tracking:
  - Entry slippage = (Tradovate fill) − (strategy's assumed entry in trades_full.tsv).
    Positive = worse for longs, better for shorts; negative = better for longs.
  - Exit slippage = similar but reversed sign convention. The target exit is a MARKET
    close sent on a tick touch, so a real fill beyond the target level is expected;
    quantify it.
  - The model assumes 0.50 pt (2 ticks) of slippage. Flag anything > 1.0 pt.

For "immediate stopouts": if (exit_time − entry_time) < 30 seconds, investigate
whether the initial stop was set at or near the fill price.

For orphaned positions: look for Tradovate fills that don't have a corresponding
entry in trades_full.tsv. These arise from cancel/fill race conditions or from a close
sent while a position was actually open at the broker but not in position.json.

### B. Live vs Replay (logic fidelity)

If replay files exist, compare the two decision logs (live #11 vs replay #12) event by
event. The goal is convergence: any divergence is a real-time execution artifact
(bar-delivery timing, the Analyzer's arrival time, a manual intervention, or a code
change deployed after the live run).

**B0. Whole-session P&L delta — MANDATORY, do this FIRST (before per-event matching).**
Compute the live clean-ledger P&L (sum of `trades_full.tsv` clean rows, phantom second
exits excluded) and the replay P&L (`realised` from report_replay_pnl; state `marked`
separately), and compare them. The replay is the apples-to-apples measure of what the
CURRENT trader logic does on this exact tape with this exact thesis — so a large gap means
the live P&L is NOT attributable to the strategy and must be explained. **Trigger a
dedicated discrepancy (its own D-number) whenever the absolute gap exceeds the larger of
$300 or 50% of the live clean P&L.** When triggered, DECOMPOSE the gap into these buckets
and quantify each in dollars (cite timestamps/prices from events.jsonl ↔ trader_decisions):
  1. **Manual-discretion actions** — `trade.py close` (`reason="user-requested"`), manual
     entries, pauses. Human alpha (or loss), NOT strategy edge. A manual close also
     changes what the replay's exit would have earned — state both figures.
  2. **Fills present only in LIVE** — entries with no replay counterpart in the same
     window (bar-delivery / partial-bar timing, a late start, a pause/resume artifact).
  3. **Fills present only in REPLAY** — entries the clean replay took that live missed.
  4. **Common fills** — present in both (matched fill time ±5 s, same direction and
     mechanism). The only P&L genuinely attributable to current strategy logic running live.
State plainly which P&L figure to trust as the strategy measure (almost always the replay),
and flag the live total as hybrid/manual if buckets 1–2 dominate.

For each matched fill pair (fill time ± 5 seconds, same direction):
  - **Mechanism**: flag if different
  - **Entry price**: flag if |live − replay| > 0.25 pts (1 tick)
  - **Stop**: flag if different
  - **Target pick** (target_selected: level and price): flag if different
  - **Exit kind/time/price**: flag if the exit kind differs (take_profit vs stop_out vs
    hard_close), or |time| > 60 s, or |price| > 0.25 pts — EXCEPT when the live exit was a
    manual close (then the replay's exit is the counterfactual; report it as such)

Plan-level comparison (plans.json live vs replay): direction, dol, valid_while,
armed_classes, created_at (bar). plan_dead: same reason and within 60 s?
would_have_falsified / would_have_killed: same instants (±60 s)?

Known acceptable differences between live and replay (do NOT flag these):
  - Sub-second timestamp jitter (< 2 s) on the same event
  - The thesis arrival bar: live lands at 09:20:00 + the real call latency (call_meta),
    the replay at 09:20:00 + the arrival gate (40 s default); a one-bar shift in when the
    plan derives is expected and only matters if it changes an entry
  - Volume fields; contract count (replay uses TRADING_CONTRACTS as configured)

### C. Commit context — connecting after-run commits to divergences

This section reconciles the divergences found in section B against the commits
collected in data source #15.  Run it AFTER completing section B.

**Step C1 — Match divergences to commits**

For each live↔replay divergence found in section B:
  a. Read every commit message (and changed files) that landed AFTER the "Running commit"
     recorded in comments.md.
  b. Ask: could this commit have caused or explained the divergence? Indicators: it touches
     agent/trader (executor, mechanisms, target, planner, order_sim, order_port,
     initial_target) or automation/agent_dispatch.py or
     live_orders / the PMT executor, or its message names the scenario.
  c. If a match is found, annotate the divergence with:
     `Explained by commit <short-hash>: "<commit subject>"` and one sentence why.
  d. If no commit explains the divergence, leave it unannotated — it is a genuine
     live/replay discrepancy that needs its own investigation.

**Step C2 — Check whether targeted commits actually fixed their stated issue**

For each commit whose message implies a specific fix or improvement:
  a. Identify what the commit was intended to fix or enable.
  b. Find the specific session scenario the commit targets in the replay output.
  c. Verify whether the replay shows the fix taking effect: RESOLVED / UNRESOLVED, with
     what the replay showed instead when unresolved.
  d. "Implied fixes": if intent is reasonably inferable from the diff AND the session had a
     relevant occurrence, apply the same RESOLVED / UNRESOLVED check.

---
OUTPUT: Write THREE files.

### File 1: <SESSION>\discrepancies.md

Structure:
```
# Session Discrepancies — <DATE>

## Summary
<2-4 sentence overview of what was found>

---

## D<N> — <Short descriptive title>

**Source**: <which comparison revealed this: Live↔Tradovate, Live↔Replay, or all three>
**Time**: <ISO timestamp>
**Expected**: <what the strategy intended / what the replay produced>
**Actual**: <what actually happened per live logs / Tradovate>
**Root cause**: <why it happened — cite specific lines from signals.log, events.jsonl or
    trader_decisions.jsonl>
**Commit context**: <"Explained by commit <hash>: <subject>" if an after-run commit
    caused this divergence, otherwise omit this field entirely>
**Suggested fix**: <actionable code change or guard to add; omit if commit already fixed it>

---

## After-Run Commits — Impact Summary

List every commit from data source #14 that landed after the running commit.  For each:

### <short-hash> — <commit subject>

**Intent**: <what the commit was meant to fix or enable — inferred from message + diff>
**Session scenario**: <which specific occurrence in this session it targets>
**Replay result**: RESOLVED — <what the replay now shows, confirming the fix>
              OR
              UNRESOLVED — <what the replay showed instead; describe the gap concretely>
**Divergences explained**: <list D-numbers whose live↔replay difference this commit
    accounts for, or "none">

---
```

Include ALL meaningful discrepancies found. Skip trivial noise (sub-tick rounding,
cosmetic label differences, known acceptable differences listed above).
Classify severity in the title: use "[CRITICAL]" if the bug could cause unlimited
or unintended risk (a legacy entry kind, a wrong armed_classes set, an entry after 10:30,
a position not flattened at 13:00, a stop not embedded at the broker), "[MINOR]" if it's
cosmetic or small-impact.
The "After-Run Commits" section is required whenever data source #15 returns at least one
commit after the running commit.  If there are none, omit the section entirely.

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
**Context**: <relevant levels, thesis, plan, mechanism, bar data>
**Potential gain**: <quantify in points/dollars if price continued as expected>

---

## Optimization Themes

### O<N> — <Theme name>: <one-line description> — [High/Medium/Low]

**Pattern**: <describe the recurring behavior and cite the supporting findings>
**Suggested fix**: <concrete change to the trader logic (agent/trader) or the live bridge;
    if an after-run commit already addresses this, write "Already addressed by commit <hash>">
**Supporting findings**: <list of F-numbers that back this up, and/or the comments.md note
    quoted verbatim when the theme originates from an operator comment>
**Estimated session impact**: <estimated additional P&L if the fix had been applied>

---
```

Raw findings capture individual trade-level observations. Optimization themes
group them into actionable strategy improvements. High/Medium/Low refers to
estimated impact over many sessions, not just this one. Themes originating from
comments.md (operator-proposed features) are REQUIRED, not optional.

### File 3: <SESSION>\session-analysis.md

A consolidated, human-readable digest that ties the run together — the single file to
open first. Derive it ENTIRELY from sources you already read plus the two files you
just wrote (do not invent numbers). Structure:

```
# Session Analysis — <DATE> (MNQ)

Consolidated digest of the post-session run. Full detail lives in the companion files
`discrepancies.md` and `optimizations.md` in this folder.

---

## 1. Data Health
<If parquet/data-health results for this session were provided to you in context (e.g.
a parquet-check was run in the same flow), summarize them here: per-instrument 1s/1m
severity, rows merged, gaps, promotion status, and an explicit "are the 1s parquets fine
/ all gaps filled?" verdict. If NO data-health info was provided, write exactly:
"Not assessed in this run — validate separately with the parquet-check skill." Do not
fabricate parquet results.>

## 2. Thesis & Plan
| Item | Value |
|---|---|
| L1 thesis | <bias / regime / confidence; DOL level + price; armed_at; call latency> |
| Plan | <plan_id; armed_classes; last_trend; falsifiers> |
| Plan outcome | <plan_dead reason + time, attempts used, or "alive at 13:00"> |
<One line: dark day? (no thesis / no plan) and why.>

## 3. Session P&L Snapshot
| Measure | Value |
|---|---|
| Strategy assumed P&L (clean ledger) | <from trades_full.tsv clean rows, phantom exits excluded> |
| Trader replay P&L | <realised (and marked, separately) from report_replay_pnl> |
| Ledger composition | <N clean · N suspect · N unpaired-open · N phantom-second-exit> |
<One line on whether live and replay ran on identical code (running commit vs HEAD).>
<If the B0 P&L-delta check triggered a discrepancy, add a "P&L-delta" line here: the gap
size and its decomposition (manual $ · live-only $ · replay-only $ · common $), and which
figure to trust as the strategy measure. Reference the D-number.>

## 4. Discrepancies (see `discrepancies.md`)
<Bulleted D-list: each D-number, severity tag, one-line summary. Mark [CRITICAL] ones with 🔴.>

## 5. Optimization Themes (see `optimizations.md`)
<Table or bulleted O-list: each O-number, theme, impact tier, est. session swing. Themes
that came from comments.md are tagged "(operator-proposed)".>

## 6. Artifacts
- Live session chart: <path>
- Replay chart: not available (no plotting tool for trader_decisions.jsonl yet)
- Replay run folder: <REPLAY>
- Companion detail files: `discrepancies.md`, `optimizations.md` (this folder)
```

---
IMPORTANT: Do not write the files until you have read ALL data sources. Read first,
synthesize, then write all three files in one pass. The analysis should be grounded in
specific timestamps, prices, order IDs and decision-log records from the actual data —
not generic advice.
```

---

## Step 4 — Report to user

Once the subagent completes, confirm:
- Thesis / plan summary (bias, DOL, plan outcome) or "dark day" with the reason
- Which discrepancies were found (D1, D2... with one-line summaries and source tag)
- Which optimization themes were identified (O1, O2... with estimated impact), marking
  the operator-proposed ones from comments.md
- File paths written — all THREE: `discrepancies.md`, `optimizations.md`, and the
  consolidated `session-analysis.md`

**Data Health back-fill:** `session-analysis.md` has a "Data Health" section. The
session-analysis skill does NOT itself run the parquet-check. If a parquet-check WAS
run in the same flow (e.g. the user chained `/parquet-check` then `/session-analysis`,
or the run-orchestrator maintenance cycle ran it first), paste its per-instrument result
summary into the subagent prompt (Step 3) so the subagent fills that section; otherwise it
will record "Not assessed in this run". If you have the parquet-check summary but only
realize it after the subagent finished, edit the "Data Health" section of
`session-analysis.md` in place to add it.

If any discrepancy is marked [CRITICAL], call it out explicitly and offer to investigate the root cause in the code.
