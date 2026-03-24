# System Upgrade Phases

Ordered implementation path from "baseline optimization complete" to "full live trading system".
Each phase is a deployable milestone — run it, validate it, then move to the next.

---

## Phase 0 — Research Completion (Exit Optimization)

**Goal:** Complete the optimization research cycle started in the global-mar24 worktree before
building any live system infrastructure. These steps produce a strategy with W/L ≥ 1.2 and
establish the enriched trade log that all subsequent diagnostic work depends on.

**Must be done:** after the global-mar24 worktree recommendations are finalized and before Phase 1.

**Source:** `../global-mar24/harness_upgrade.md` — iter21 conclusions and exit optimization plan.

---

### Phase 0.1 — Harness Prerequisites (no strategy logic changes)

These are blocking prerequisites for any exit experiment. They change what is measurable and
what the baseline even is. Captured in `.agents/plans/p0-price-fix-and-instrumentation.md`.

**P0-A: Fix `price_10am` → `price_1030am`**

`prepare.py` currently extracts the 9:30 AM Open (market open — worst possible intraday price
for a "wait for open volatility to settle" strategy). Switch to the Close of the 9:30 AM bar
(~10:30 AM, post-opening-volatility by definition). Rename the column throughout `prepare.py`,
`train.py`, and all test fixtures. Re-run iter21 config (`6ad6edd`) on corrected price first
to establish a new baseline — results will differ from all 27 prior iterations.

**P0-B: Add MFE and MAE to trades.tsv**

Track `max_price_seen` and `min_price_seen` during each position's hold period. At exit, compute
and write to trades.tsv:
- `mfe_atr = (max_price_seen − entry_price) / ATR14` — maximum favorable excursion in ATR units
- `mae_atr = (entry_price − min_price_seen) / ATR14` — maximum adverse excursion in ATR units

ATR units (not raw %) make MFE/MAE directly comparable to stop and trail thresholds.
Without this, it is impossible to distinguish "early false stops" from "true failures."

**P0-C: Add per-exit-type reporting to fold summary**

Add `exit_type` column to all trade records: `stop_hit`, `stall_exit`, `breakeven_stop`,
`trailing_stop`, `time_exit`, `partial`. Log count and avg P&L per exit type in the fold summary
so each run immediately shows which mechanism is responsible for avg win suppression.

**P0-D: Add R-multiple to trades.tsv**

`r_multiple = (exit_price − entry_price) / (entry_price − initial_stop)`. Log at exit.
A healthy momentum strategy shows winners clustering at +1.5–3.0R and losers at -1.0R.
If winners cluster near 0R, the breakeven trigger is the dominant suppressor. If losers
cluster at -0.3 to -0.6R, the stop is being hit too early.

---

### Phase 0.2 — Diagnostic Analysis (no code changes, read the data)

Run Phase 0.1, then answer from the enriched trade log before touching any exit logic:

1. **What fraction of losing trades have MFE > 1.0 ATR?** If > 25%, exits are giving back gains.
   If < 10%, losses are true failures and exit loosening won't help.

2. **What is the avg MFE of winning trades?** If winners peak at +3 ATR but exit at +1.5 ATR,
   there is significant upside being left. If they peak near exit price, stall exit is correct.

3. **Which exit type accounts for most losing trade exits?** If `breakeven_stop` accounts for
   > 20% of all exits at 0R, the breakeven trigger is the primary W/L suppressor. If `stall_exit`
   losers outnumber `stop_hit` losers, stall exit is cutting trades that haven't confirmed failure.

4. **What is the R-multiple distribution shape?** A bimodal distribution (cluster near -1R and
   cluster near +1.5R) is healthy. A trimodal distribution with a mass near 0R indicates the
   breakeven stop is creating an artificial third population of "stranded" trades.

The answers dictate which Phase 0.3 experiment to run first.

---

### Phase 0.3 — Exit Experiments (ordered by expected impact)

Each experiment runs on iter21 base (`6ad6edd`) after P0 fixes re-establish the baseline.
Keep/discard rule: W/L ≥ 1.0 improvement AND min_test_pnl not worse than −17 (allowing $3
regression from the −14.26 floor, since exit changes affect all folds including fold1/2).

**E1: Delay the breakeven stop trigger (1.5 ATR → 2.0 ATR)**

Moves breakeven trigger from entry + 1.5 ATR to entry + 2.0 ATR. Eliminates the "stranded
near 0R" population — trades that moved 1.5–2.0 ATR get stopped flat instead of continuing.
Expected: avg win up, avg loss slightly up, net W/L improvement if MFE data shows 0R
population is material.

**E2: Loosen the stall exit (5d/0.5 ATR → 7d/0.7 ATR)**

The tighter direction (iter5) was catastrophic — this is the untested opposite. Expected:
winners in slow but continuing trends held longer, raising avg win. Risk: some trades that
would have stall-exited at small profit instead reverse and hit stop.

**E3: Trailing stop with wider activation (2.0 ATR) and tighter trail (1.0 ATR)**

Refines iter7 result (1.8 ATR activation / 1.0 ATR trail — directionally correct, not
sufficient). Try: activate at 2.0 ATR, trail at 1.0 ATR. Wider activation prevents clipping
during normal consolidation at +1.5–1.8 ATR. Composes naturally with E1: breakeven at 2.0
ATR hands off to trailing stop at 2.0 ATR, no gap between mechanisms.

**E4: Partial exit at first resistance**

When `nearest_resistance_atr` is reached, exit 50% of position, move stop to breakeven,
trail remainder at 1.0 ATR. Locks in partial gains while allowing continuation. Even a simple
"take half off at +2 ATR" rule is worth testing if MFE data shows winners consistently
reaching > +2 ATR.

**E5: Close-below-SMA20 as a soft exit**

If EOD close drops below SMA20 while position is open, exit next morning. Converts slow-bleed
losers (price grinds down, eventually hits stop well below SMA20) into quicker, smaller losses.
Specifically targets the mae_atr > 0.5 population. Expected: avg loss down, win rate may dip.

**E6: Entry quality gate — minimum resistance clearance 2.0 → 3.0 ATR**

Filter out entries where overhead resistance < 3.0 ATR. Fewer trades (estimate −10 to −20
from 116 baseline) but higher expected avg win on those taken. Complements E4 — more room
means partial exit at +2 ATR is still far from the ceiling.

---

### Phase 0.4 — Combinations

Run only after Phase 0.3 identifies which individual experiments improve W/L.

- **E1 + E3**: Delayed breakeven (2.0 ATR) + trailing stop (2.0 ATR activation). Natural
  compose: breakeven trigger and trail activation at same threshold creates a clean handoff.
- **E2 + E5**: Looser stall exit + SMA20 soft exit. Loosening risks holding through reversals;
  SMA20 soft exit provides the safety valve that catches the same reversals at lower cost.
- **E6 + E4**: Higher resistance clearance (fewer but better trades) + partial exit at
  resistance. Cleanest R:R profile — only enter when room exists, take partial profits at ceiling.

---

### Phase 0.5 — Static Sector Exclusion (lightweight, not a full sector strategy)

One-time rule change, not an optimization loop. Add sector blacklist for entry screening:

**Exclude:** Utilities (XLU), Consumer Staples (XLP), REITs (XLRE). These sectors are
fundamentally mean-reverting or income-driven; momentum breakouts in them during corrections
(ECL, KO entering fold1) represent defensive rotation, not genuine momentum. GICS sector
lookup at prepare-time tags each ticker; `screen_day` adds a single
`if sector in EXCLUDED_SECTORS: return None` check.

Do NOT split by sector or optimize per sector at this stage. The trade count (116 total) makes
per-sector walk-forward statistically meaningless. The blacklist is a static quality gate.

Expected: removes ~5–10 trades, targeted at fold1/2 defensive-stock entries. Does not help
tech entries (APP, HUBS, HOOD in fold1) but removes one class of structural false positive.

---

### Phase 0 — Success Criteria

| Metric | Current (iter21) | Target |
|--------|-----------------|--------|
| W/L ratio | 0.885 | ≥ 1.2 |
| min_test_pnl | -14.26 | ≥ -17 (allow $3 regression) |
| Total trades | 116 | ≥ 100 (allow some reduction for quality) |
| Win rate | 69% | ≥ 65% (allow some reduction) |

### Validation checklist

- [ ] P0-A/B/C/D all implemented and passing full test suite
- [ ] New baseline (iter21 on corrected price) established and documented
- [ ] Phase 0.2 diagnostic questions answered with data from enriched trade log
- [ ] At least one exit experiment (E1–E6) improves W/L to ≥ 1.0 without violating min_test_pnl floor
- [ ] W/L ≥ 1.2 achieved (via experiment or combination) with ≥ 100 trades and min_test_pnl ≥ -17
- [ ] Sector exclusion (if adopted) shows fold1/2 defensive entries removed from trade log
- [ ] `/fetch-strategies` run to register final strategy in `strategies/` before Phase 1

---

## Phase 1 — Pre-Market Signal CLI

**Goal:** Given the baseline strategy, produce pre-market BUY signals (new positions) and RAISE-STOP
signals (existing positions), triggered manually from the terminal, on the local machine.

**No:** OpenClaw, VPS, messaging, sector strategies, mid-day data, scheduled automation.

### Prerequisites

1. Baseline optimization completes in the `multisector-mar23` worktree.
2. Run `/fetch-strategies` to extract and register the baseline strategy.
3. In master `train.py`: update `BACKTEST_START` / `BACKTEST_END` / `TRAIN_END` / `TEST_START` to match
   the baseline run, copy the finalized `screen_day()` and `manage_position()` bodies from the worktree.
   Remove any placeholder strategies from the `strategies/` registry that predate the baseline.

### Deliverables

#### `portfolio.json` schema (user-maintained)

```json
{
  "positions": [
    {
      "ticker":       "AAPL",
      "entry_price":  182.50,
      "entry_date":   "2026-03-10",
      "shares":       10,
      "stop_price":   174.20,
      "notes":        "optional free text"
    }
  ]
}
```

User edits this file manually after each trade (entry and exit). The two scripts read it read-only.
Schema is extended in Phase 3 when OpenClaw takes over portfolio management.

#### `screener.py`

Scans all tickers in the parquet cache for BUY signals.

```
uv run screener.py
```

Behavior:
1. Load all `*.parquet` files from `CACHE_DIR` (same path `train.py` uses).
2. Fetch pre-market price for each ticker via `yfinance.Ticker(t).fast_info['last_price']`.
   - Fall back to previous close if `last_price` is unavailable (weekend / pre-open gap).
3. Append a synthetic "today" row to each df with `price_10am = current_price` and `volume = 0`
   (today's volume is unavailable pre-market and unreliable at 10am — see volume redesign below).
4. Call `screen_day(df, today, current_price=current_price)` from `train.py` (import directly).
5. Print armed candidates sorted by `prev_vol_ratio` descending.

Output columns: ticker, current_price, entry_threshold (high20 + 0.01), suggested_stop, atr14, rsi14,
prev_vol_ratio, vol_trend_ratio, gap_pct, resistance_atr, days_to_earnings.

**`current_price` interface:** `screen_day()` must accept an optional `current_price=None` parameter.
When `None` (harness path), it reads `df["price_10am"]` as before — no change to backtesting behavior.
When passed a value (live path), it uses that price instead. This is the only coupling to break between
the harness and live use; all other strategy logic is interval-agnostic.

**Pre-market vs. open price:** Pre-market prices are thin and noisy. The screener uses them for early
candidate identification only — "these 12 tickers meet all indicator conditions, AAPL threshold is
$183.50, currently at $181 pre-market." The **confirmed signal** fires at 9:30 AM when the actual
opening price is checked against the threshold. Never make final entry decisions on pre-market prices
alone.

#### Volume criterion redesign

The backtested `vol_ratio` rule (`today_vol / vm30 >= 1.9`) is structurally wrong for live use:
- Pre-market: `today_vol = 0` → every candidate fails
- At 10am (30 min in): ~15–20% of daily volume has accumulated → a genuine 1.9× day shows ~0.3×
- By the time today's volume actually confirms at 1.9×, price has already moved past the entry

**Fix:** remove `today_vol` from `screen_day()` entry logic entirely. Replace Rule 3 with two
prior-data-only checks:

| New rule | Condition | Rationale |
|---|---|---|
| **Rule 3a** — recent trend | `mean(vol[-5:]) / vm30 >= 1.0` | Confirms building institutional interest over the past week |
| **Rule 3b** — yesterday not dead | `vol[-2] / vm30 >= 0.8` | Rejects setups where the prior session itself was unusually quiet |

Both use only data available pre-market. The price breakout above `high20` at 10am is already
strong confirmation of buying pressure; prior volume trend is sufficient to filter low-conviction setups.

`today_vol` is still appended as `volume = 0` in the synthetic row so `screen_day()` compiles,
but the volume rules no longer reference it. `prev_vol_ratio` and `vol_trend_ratio` are added to the
output dict for display.

#### Gap filter (pre-entry guard)

Analysis of losing trades shows ~72% are 1-day holds consistent with gap-down events — the stock
opens against the position before any stop management is possible. The largest single-day losses
(ORCL, SOFI, AAL, BBY…) are almost certainly earnings/news gaps visible in pre-market. These
dominate avg_loss and cannot be rescued by any stop technique.

**Step 1 — gap threshold analysis (one-off script `analyze_gaps.py`):**

Before implementing the filter, establish the threshold empirically from `trades.tsv`:
1. For each trade, load the ticker's parquet and look up `close` on `entry_date − 1 trading day`
2. Compute `gap = (entry_price − prev_close) / prev_close`
3. Plot gap vs pnl, and gap distribution for winners vs losers
4. Find the natural breakpoint — expected: large negative gaps (< −2%) correlate strongly with
   worst losses. Also check what fraction of winners had negative gaps to assess filter cost.

**Step 2 — gap filter in `screener.py`:**

After the EOD candidate list is generated, add a 9:30am gap check before committing to entry:
- Compute `gap = (today_open − prev_close) / prev_close` for each candidate
  (use `yfinance.Ticker(t).fast_info['open']` at market open, or pre-market quoted price as
  an earlier estimate — pre-market prices are thin/noisy but useful for early warning)
- Skip entries where `gap < −X%` (threshold set from the analysis above)

This filter is not backtestable with pre-market data but is fully backtestable using the actual
open, which is already in the historical data. Add `gap_pct` to the screener output for all
candidates regardless of filter outcome so context is visible.

#### `position_monitor.py`

Checks stop-raise conditions for all open positions.

```
uv run position_monitor.py
```

Behavior:
1. Read `portfolio.json`.
2. For each position, load its ticker's parquet from `CACHE_DIR`.
3. Fetch current price via `yfinance.Ticker(t).fast_info['last_price']` and append as today row.
4. Call `manage_position(position_dict, df)` from `train.py` (import directly).
5. Print RAISE-STOP signals where `new_stop > position['stop_price']`.

Output per signal: ticker, current_price, current_stop → new_stop, delta_atr, reason
(breakeven / trail / time-exit / early-stall).

No writes — user updates `portfolio.json` manually after confirming.

### Validation checklist

- [ ] `analyze_gaps.py` runs against `trades.tsv` and produces a gap vs pnl scatter with a visible breakpoint
- [ ] `screener.py` runs on fresh terminal, finds at least one armed candidate on a normal market day
- [ ] Screener output shows `prev_vol_ratio`, `vol_trend_ratio`, and `gap_pct` columns
- [ ] A ticker with flat prior volume (yesterday < 0.8× MA30) is correctly excluded
- [ ] A candidate with gap < threshold is excluded from the armed list (threshold from gap analysis)
- [ ] `position_monitor.py` runs with a sample `portfolio.json`, correctly outputs a RAISE-STOP for a
  position that is 1.5+ ATR in profit
- [ ] `screen_day()` in `train.py` master matches the baseline worktree output (spot-check 3 tickers)
- [ ] No old strategies remain in `strategies/` registry

---

## Phase 2 — Per-Sector Strategies

**Goal:** Replace the single baseline strategy with 11 GICS sector-specific strategies, each
independently optimized.

**Deferred still:** OpenClaw, VPS, messaging, mid-day data, scheduling, volatility adaptation.

### GICS classification notes

A few sectors have non-obvious assignments worth knowing before picking tickers:

- **Agricultural companies** are split across three sectors by business activity, not crop:
  - Grain/food processors (ADM, BG) → **Consumer Staples**
  - Fertilizers/agrochemicals (MOS, CF, NTR, CTVA) → **Materials**
  - Farm equipment (DE) → **Industrials**
- **Defense companies** (RTX, LMT, NOC, GD, TXT) → **Industrials** (Aerospace & Defense
  sub-industry). Classified by manufacturing/systems activity, not by government end customer.
  Their correlation with XLI holds reasonably well outside geopolitical shock events. If the
  Industrials strategy consistently underperforms on defense names specifically, split
  Aerospace & Defense into its own strategy at that point — don't pre-emptively do it.
- **No separate high-volatility group.** High-vol outliers (COIN, MSTR, SMCI, PLTR) stay in
  their respective sectors. The volatility-adaptive parameters in Phase 5 handle their extreme
  ATR automatically — no binary classification needed.

### Ticker selection per sector

For each of the 11 sectors, choose 8–12 tickers using these criteria in order:

1. **S&P 500 membership** — ensures liquidity and data quality in yfinance
2. **Top by market cap in sector** — institutional flows make technical patterns more reliable
3. **High correlation with sector ETF** — β > ~0.5 vs. XLK/XLF/XLE/etc. over 6-month rolling
   window. Low-β tickers (e.g., MSTR vs. XLK) are behavioral outliers; include them anyway
   since Phase 5 volatility-adaptive parameters will handle them, but note they may produce
   noisy optimization results
4. **Behavioral diversity within sector** — aim for sub-industry spread. In Financials: a
   megabank (JPM), an investment bank (GS), a payment network (V), an asset manager (BLK).
   They all correlate with XLF but have different volatility profiles, giving Optuna more signal
   to calibrate the volatility-adaptive parameters

This selection is a one-time manual judgment per sector. It does not need to match the live
trading universe exactly — it just needs to be representative.

### Steps

1. For each of the 11 GICS sectors: create a worktree using `/prepare-optimization` with the
   sector ticker list. All 11 can be prepared in sequence and run simultaneously — they are
   fully independent (no shared state, no ordering dependency). Running in parallel reduces
   wall-clock time from 11× to 1×.
2. Run `/fetch-strategies` after each sector run completes.
3. **Compare each sector specialist against the baseline** on the holdout test set for that
   sector's tickers. Decision rule: if the specialist beats the baseline, adopt it. If it
   doesn't, investigate before discarding (overfitting? not enough iterations? sector too
   heterogeneous?). Do not auto-adopt a worse strategy just because it's sector-specific.
4. Add sector lookup and introduce `strategy_runner.py` (see below).
5. Route each ticker to its sector strategy in both scripts via `select_strategy()`.

### Sector routing: `strategy_runner.py`

In Phase 1, `screener.py` and `position_monitor.py` import strategy functions directly from
`train.py` — one strategy, no routing needed. In Phase 2 there are 11 strategy files, and
both scripts need to call the right one per ticker without knowing the file layout.

`strategy_runner.py` is a thin strategy abstraction layer (~100 lines) that bridges the sector
selector and the actual strategy function calls:

- Accepts: ticker, recent OHLCV DataFrame, optional current price
- Calls `select_strategy()` to determine which `strategies/<name>.py` to use
- Imports that strategy dynamically and calls `screen_day()` / `manage_position()`
- Returns structured output (armed candidate dict or new stop price)
- Has no knowledge of Optuna, parquet files, backtesting, or walk-forward folds

`screener.py` and `position_monitor.py` replace their direct `train.py` import with a call to
`strategy_runner.py`. The sector routing, `YFINANCE_TO_GICS` mapping, and `select_strategy()`
all live here.

```python
YFINANCE_TO_GICS = {
    "Technology":             "IT",
    "Financial Services":     "Financials",
    "Consumer Cyclical":      "Consumer Discretionary",
    "Consumer Defensive":     "Consumer Staples",
    "Healthcare":             "Health Care",
    "Communication Services": "Communication Services",
    "Basic Materials":        "Materials",
    "Industrials":            "Industrials",
    "Energy":                 "Energy",
    "Utilities":              "Utilities",
    "Real Estate":            "Real Estate",
}

def get_sector(ticker: str, cache: dict) -> str | None:
    if ticker not in cache:
        info = yf.Ticker(ticker).info
        cache[ticker] = YFINANCE_TO_GICS.get(info.get("sector"), None)
    return cache[ticker]

def select_strategy(ticker: str, sector_cache: dict, strategies: dict):
    sector = get_sector(ticker, sector_cache)
    if sector is None:
        return None  # skip — no sector data (SPACs, stale ADRs, etc.)
    return strategies.get(sector)  # None if no strategy for this sector yet
```

Cache sector lookups in `~/.cache/autoresearch/sector_cache.json` (TTL: 30 days — sector
assignments change rarely). Do not default to a catch-all strategy when `get_sector()` returns
`None`. A wrong strategy is worse than no trade.

### Validation checklist

- [ ] Screener correctly routes a known Consumer Discretionary ticker to its sector strategy
- [ ] Sector cache file created and reused on second run
- [ ] A ticker with no yfinance sector (SPAC, stale ADR) is skipped, not routed to a fallback
- [ ] All 11 sector strategies registered in `strategies/` registry
- [ ] Each adopted sector specialist beats the baseline on its sector's holdout tickers

---

## Phase 3 — OpenClaw + VPS + Messaging

**Goal:** Move signal delivery from terminal output to Telegram/WhatsApp messages. Manual chat
commands trigger scans. User can update portfolio via chat.

**Deferred still:** Scheduled automation, mid-day data, real-time prices, volatility adaptation.

### OpenClaw capabilities

| Capability | Details |
|---|---|
| **Cron jobs** | Built into the Gateway, persist across restarts, configurable interval (`every: "30m"` etc.), active-hours limiting. Deliver output proactively via `--announce --channel telegram --to <chat-id>`. |
| **Proactive messaging** | Cron + `--announce` sends agent output directly to the user without a user-initiated message. Confirmed working. |
| **Native subagents** | `sessions_spawn` tool, up to 8 concurrent (`maxConcurrent` configurable), orchestrator pattern (`maxSpawnDepth >= 2`). Results announced back via request-reply. Best-effort — in-flight results lost if gateway restarts, acceptable for trading signals. |
| **Background exec** | `exec` tool runs shell commands including Python scripts. Background mode returns session ID; `process poll` retrieves output. Not push-based — agent must explicitly poll. |
| **Skills (SKILL.md)** | Markdown files that instruct the agent to use existing tools. No TypeScript required. Python scripts called via `exec`. |
| **Cron concurrency** | `maxConcurrentRuns: 1` default — overlapping cron fires queue, not skip. |

### VPS requirements (live system only)

| Resource | Requirement | Notes |
|---|---|---|
| RAM | 2–4 GB | OpenClaw (~1 GB) + screener peak (pandas on ~45 bars × 1000 tickers, released after run) |
| Disk | 5–10 GB | Live parquet cache (~1–2 GB) + OpenClaw + OS |
| CPU | 1–2 vCPU | No heavy computation |
| Cost | ~$5–15/month | Hetzner CX22, DigitalOcean Basic, or equivalent |

The optimization harness stays on the local development machine (compute/memory heavy: Optuna,
walk-forward across 85+ tickers, multiple parallel worktrees). The VPS is sized for the live
system only — lightweight screener + price monitor.

### Separate parquet caches

| | Harness cache (local) | Live system cache (VPS) |
|---|---|---|
| Tickers | Optimization universe (85–1000, varies per run) | Full live universe (S&P 500 + Russell 1000) |
| History depth | Full backtest window (months to years) | Last 60–90 days only |
| Updated by | `prepare.py` before each optimization run | EOD cron nightly (one bar appended per ticker) |
| Used by | `train.py` backtesting loop | Screener, mid-day rescan, indicator snapshot |

The VPS cache is built from scratch on first deployment. Each nightly EOD cron appends one bar per
ticker — no data transfer from the harness machine needed.

### NemoClaw sandbox constraints

NemoClaw's sandbox (via Landlock) restricts filesystem writes to `/sandbox` and `/tmp` only.
`~/.cache/autoresearch/` (the default parquet cache path) is blocked. Set these env vars in the
NemoClaw policy YAML at sandbox creation — no code changes needed:

```
AUTORESEARCH_CACHE_DIR=/sandbox/stock_data
SIGNALS_DIR=/sandbox/signals
PORTFOLIO_FILE=/sandbox/portfolio.json
SNAPSHOT_FILE=/sandbox/indicators_snapshot.json
```

`/tmp` is cleared on restart — use `/sandbox` for all data that must survive restarts.

### GitHub deployment flow

Strategy files are Python code (~5–10 KB) — a natural fit for git. A dedicated `strategies` branch
decouples strategy promotion from active harness development.

```
Local machine:
  1. Optimization run completes in a worktree
  2. /fetch-strategies → strategies/<name>.py registered, REGISTRY.md updated
  3. User reviews metrics, decides to promote
  4. git push → GitHub (harness repo, strategies/ directory, strategies branch)

VPS (OpenClaw):
  1. User sends "deploy IT strategy v2" via Telegram
  2. Deployer subagent: git pull strategies/<name>.py from GitHub (strategies branch)
  3. Validate: syntax check, METADATA fields present, sector matches live selector
  4. Copy to live system's strategies/ directory
  5. Update strategy_runner.py sector mapping if needed
  6. Announce confirmation to Telegram
```

VPS only needs a read-only SSH deploy key scoped to the harness repo — no direct network access
to the local machine.

### Scripts and skills

**Python scripts:**

| Script | What it does |
|---|---|
| `screener.py` | Loads cached daily data, runs strategy screener on all tickers, outputs armed candidates |
| `position_monitor.py` | Reads `portfolio.json`, runs stop-raise logic, outputs RAISE-STOP signals |
| `strategy_runner.py` | Sector routing layer — maps ticker → strategy file, delegates indicator/signal calls |
| `deployer.py` | Pulls strategy from harness repo, validates, updates strategies/ and selector |
| `sector_cache.py` | Queries yfinance `.info["sector"]`, persists to JSON cache, returns sector for a ticker |

**OpenClaw SKILL.md files (no TypeScript):**

| Skill | Instructs agent to... |
|---|---|
| `run-screener` | Run `screener.py`, parse output, format armed candidates summary, announce to channel |
| `check-stops` | Run `position_monitor.py`, format RAISE-STOP signals or "no updates", announce |
| `add-position` / `close-position` | Write/update entry to `portfolio.json` via exec |
| `deploy-strategy` | Run `deployer.py` for a named strategy, confirm success to channel |
| `update-strategies` | `git pull` from `strategies` branch on GitHub |

### Extended `portfolio.json` schema (Phase 3+)

When OpenClaw manages portfolio updates via chat, extend the schema with:

```json
{
  "positions": [
    {
      "ticker":       "AAPL",
      "entry_time":   "2026-03-24T10:02:00",
      "entry_price":  181.50,
      "entry_date":   "2026-03-24",
      "shares":       20,
      "stop_price":   176.00,
      "strategy":     "IT",
      "signal_id":    "2026-03-24-AAPL-001",
      "notes":        "optional free text"
    }
  ]
}
```

`signal_id` links the position back to the signal that generated it. `strategy` records which sector
strategy was active at entry — useful when strategies are later replaced.

### Validation checklist

- [ ] "run screener" chat message returns armed candidates in Telegram within 60 seconds
- [ ] "check stops" returns RAISE-STOP signals or "no updates" message
- [ ] "add position AAPL 182.50 10 174.20 2026-03-10" updates `portfolio.json` correctly
- [ ] "update strategies" pulls latest from GitHub `strategies` branch
- [ ] NemoClaw env vars set; screener writes to `/sandbox/stock_data/` not `~/.cache/`

---

## Phase 4 — Scheduled Automation + Mid-Day Monitoring

**Goal:** Pre-market scan runs automatically every trading morning. Mid-day re-screen added.
Real-time price monitoring for armed candidates during market hours.

**Deferred still:** Volatility adaptation.

### Key architecture insight

Strategy indicators (RSI, ATR, SMA, volume trend) are computed from historical daily closes and do
not change intraday. Only the price trigger check ("is AAPL currently below $183.50?") needs a live
price. This decouples the problem into two independent data needs and enables an important
optimization: compute all indicators once at EOD, not at 8:30 AM.

### EOD `indicators_snapshot.json`

Rather than loading 30–45 bars and computing rolling windows per ticker at 8:30 AM across 500–1000
tickers, run full indicator computation once during the EOD cron (4:30 PM) and persist results:

```json
{ "AAPL": { "RSI": 45.2, "ATR": 2.1, "SMA20": 183.5, "vol_trend": 1.4, "entry_threshold": 181.0, ... } }
```

The 8:30 AM check becomes a single comparison per ticker: `current_price < snapshot[ticker]["entry_threshold"]`
— no pandas, no rolling windows at signal time. ~80 KB for 1000 tickers × 10 indicators. The morning
scan becomes nearly instantaneous; the EOD cron is the bottleneck.

### Daily workflow

| Step | When | Data source | Notes |
|---|---|---|---|
| Batch download all tickers' history | ~8:00 AM | yfinance daily bars | Market closed — no delay |
| EOD snapshot already built | ~8:30 AM | `indicators_snapshot.json` (from prior EOD) | Read-only, no computation |
| Screen candidates, pre-market price check | ~8:30 AM | Snapshot + yfinance `fast_info` | Informational/early alert only |
| Confirmed signal at open | 9:30 AM | Actual opening price | Authoritative entry check |
| Real-time price monitoring | 9:30 AM – 4:00 PM | yfinance poll or Alpaca WebSocket | Trigger firing |
| Mid-day re-screen | ~2:00 PM | Synthetic bar from intraday partial data | Noisier than opening window |
| EOD update + snapshot rebuild | ~4:30 PM | yfinance daily close | Appends one bar, rebuilds snapshot |

### Price monitoring architecture

**Do not use a short-interval cron for real-time price monitoring.** With `maxConcurrentRuns: 1`
(default), a 2-minute cron where each run takes > 2 minutes (slow network) causes a queue backlog —
signals arrive late and out of order.

**Use a single long-running background Python process instead:**

```
9:30 AM cron:
  → exec "uv run price_monitor.py --candidates AAPL,MSFT,..." (background=true)
  → Python loops internally: every ~60s checks prices, prints signal JSON to stdout on trigger

Every 5 minutes cron:
  → agent polls background process: process poll <session_id>
  → if signal lines found in output → format and announce to Telegram/WhatsApp

4:05 PM cron:
  → agent kills the background process
```

The 5-minute poll cron is idempotent (just reading output) — occasional overlap is harmless. The
Python process has its own internal timing and is not subject to cron concurrency issues.

### Data source upgrade path

| Tier | Source | Delay | Limit | Cost | When to use |
|---|---|---|---|---|---|
| **MVP** | yfinance `fast_info` poll every 2–3 min | 15 min | None | Free | Phase 4 start; 15-min delay acceptable for daily swing signals |
| **Production** | Alpaca IEX WebSocket | Near real-time | 30 symbols | Free (paper account) | Once armed candidates list is stable; no real money deposit needed |
| **If needed** | Polygon.io | Real-time consolidated | Unlimited | $29/month | Only if Alpaca IEX causes missed signals in practice |

Alpaca IEX represents ~10–15% of trade volume but is representative for liquid large-caps (S&P 500 /
Russell 1000). The 30-symbol limit covers armed candidates comfortably — a well-calibrated screener
typically produces 5–30 candidates. If the list consistently exceeds 30, upgrade to Algo Trader Plus
($99/month) for unlimited symbols.

### Rolling window and data artifacts on disk

Python subprocesses spawned by OpenClaw's `exec` tool are ephemeral — in-memory state is lost
between calls. The rolling window lives in parquet files on disk, refreshed nightly by the EOD cron.

```
EOD cron (4:30 PM, daily):
  1. Download today's completed daily bar for all tickers via yfinance
  2. Append to each ticker's parquet file           ← one new row added
  3. Compute all indicators from .tail(45) per ticker
  4. Write indicators_snapshot.json                 ← rebuilt from scratch nightly
  (No explicit pruning — scripts always read .tail(45), never the full file)

8:30 AM screener:
  1. Read indicators_snapshot.json                  ← no parquet access at all
  2. Fetch one current pre-market price per ticker
  3. Compare: current_price < snapshot[ticker]["entry_threshold"]
  4. Announce armed candidates

Price monitor (market hours):
  1. Entry thresholds already in memory from morning scan
  2. Fetches current price periodically
  3. No rolling window access needed during the day

Mid-day rescan (2:00 PM):
  1. Read parquet .tail(44) per candidate           ← 44 completed bars
  2. Construct synthetic partial bar from today's intraday data (in-process, not saved)
  3. Append synthetic bar in-memory → run indicators → produce updated snapshot for candidates only
  4. Original parquet and full snapshot unchanged until EOD
```

| File | Updated by | Read by |
|---|---|---|
| `~/.cache/.../stock_data/<TICKER>.parquet` | EOD cron (append) | EOD cron, mid-day rescan |
| `indicators_snapshot.json` | EOD cron (full rebuild) | 8:30 AM screener, price monitor |
| `sector_cache.json` | On new ticker (lazy) | `strategy_runner.py` |
| `portfolio.json` | User / OpenClaw agent | `position_monitor.py` |
| `signals_today.json` | Price monitor, screener | `show-signals` skill |

### Signal quality and intraday confirmation

When the price trigger fires, `price_monitor.py` fetches the last 5 × 15-minute bars and computes:

| Signal | What it captures |
|---|---|
| Last 2–3 candle direction (green/red) | Is price recovering after touching trigger, or still falling? |
| Volume on trigger bar vs. 5-bar average | High volume at low = capitulation/reversal; low volume = weak dip |
| Short-term RSI (period 7–9 on 15-min bars) direction | Is momentum turning upward from oversold? |
| Bar close vs. bar open | Did price close above its open on the trigger bar? |

**Annotate signals, don't hard-gate them.** Hard-gating suppresses valid signals on noisy days.
Instead include a momentum assessment so the user can weigh context:

```
⚡ BUY signal — AAPL  [OPENING]
Trigger hit: $179.50  (entry threshold: $180.00)
Momentum: ⬆️ RECOVERING — last 2 bars green, volume above avg
15m RSI(9): 28 → 34, rising from oversold
Signal quality: CONFIRMED
```

```
⚡ BUY signal — AAPL  [MID-DAY]
Trigger hit: $179.50
Momentum: ⬇️ CONTINUING DOWN — last 2 bars red, volume expanding
15m RSI(9): 38 → 29, falling
Signal quality: CAUTION — may not have bottomed yet
```

`[OPENING]` signals (9:30 AM window) carry the most weight — high volume, clear overnight gap
information, clean price action. `[MID-DAY]` signals (2 PM rescan) are noisier; the confirmation
annotation is more important there.

The confirmation layer lives entirely in `price_monitor.py` — not in strategy files. Strategy files
identify the opportunity (daily indicators + entry threshold). The confirmation layer assesses
intraday timing of entry within that window. These are separate jobs and must stay separate so
daily strategy optimisation is unaffected.

### Cron jobs

| Job | Schedule (ET, weekdays) | Action |
|---|---|---|
| Pre-market scan | `0 8 * * 1-5` | Run `run-screener` skill, announce to Telegram |
| Start price monitor | `30 9 * * 1-5` | Run `price_monitor.py` in background with today's candidates |
| Poll price monitor | `*/5 9-16 * * 1-5` | Poll background process, announce any pending signals |
| Mid-day rescan | `0 14 * * 1-5` | Run mid-day rescan, announce updated candidates |
| Stop price monitor | `5 16 * * 1-5` | Kill `price_monitor.py` background process |
| EOD update | `30 16 * * 1-5` | Download final daily closes, append to parquet, rebuild snapshot |

### Validation checklist

- [ ] 8:30 AM cron fires on a trading day and delivers pre-market scan to Telegram
- [ ] EOD cron appends one bar per ticker and rebuilds `indicators_snapshot.json`
- [ ] Mid-day scan produces non-duplicate results (doesn't re-signal already-held tickers)
- [ ] Alpaca WebSocket connects for ≤30 tickers and emits price update within 5 seconds of trade
- [ ] `[CONFIRMED]` vs `[CAUTION]` annotation visible in signals, `[OPENING]` vs `[MID-DAY]` label present
- [ ] `price_monitor.py` exits cleanly at 4:05 PM via kill cron

---

## Phase 5 — Volatility-Adaptive Parameters

**Goal:** Replace fixed RSI/volume/ATR thresholds with k-coefficient regime-adaptive logic per sector.
Handles high-vol outliers (COIN, MSTR, SMCI) within their sector strategies — no separate classifier.

### How it works

Each sector strategy currently has fixed thresholds (e.g., `RSI_ENTRY = 45`). Phase 5 replaces
fixed thresholds with functions of continuously observable market signals:

```
RSI_entry_threshold = base_RSI + k_atr × ATR_percentile + k_adx × ADX_normalized
```

Where:
- `base_RSI`, `k_atr`, `k_adx` — fixed constants learned by Optuna during optimization
- `ATR_percentile`, `ADX_normalized` — recomputed fresh each bar from price data in the cache

MSTR in the IT sector gets a wider RSI band automatically because its ATR_percentile is high.
AAPL gets a tighter band. The same strategy code serves both.

### Regime signals

All computable from OHLCV already in the cache:

| Signal | What it proxies | Computation |
|---|---|---|
| ATR percentile (60-day rolling) | Volatility regime | ATR(60) / Close, ranked within cached universe |
| ADX (14-day) | Trending vs. choppy | Standard Wilder ADX from OHLCV |
| % tickers above 50d SMA | Market breadth / risk-on vs. off | Cross-ticker pass at each bar |
| Sector ETF momentum | Sector-level trend | Requires adding ~11 ETF tickers (XLK, XLF, XLE, etc.) to `prepare.py` |

Start with ATR percentile only (single `k` coefficient per threshold). Add additional signals
one at a time, verifying each improves out-of-sample performance before keeping it.

### How Optuna handles this

No change to the Optuna study structure. Instead of searching for `RSI_ENTRY = 45` (one scalar),
it searches for `base_RSI = 42, k_atr = 0.8` (two scalars per threshold). Trial count may need
to increase proportionally to the added parameter count. Run on top of each Phase 2 sector
strategy — do not restart from the baseline.

### Steps

1. Extend `train.py` strategy interface: replace scalar threshold constants with `(base, k)` pairs.
2. For each sector: run a new Optuna study calibrating `base_RSI`, `vol_multiplier_base`, and `k_atr`
   against ATR-percentile. Use the Phase 2 sector strategy as the warm start.
3. Update sector strategies to use `threshold = base + k × regime_signal`.
4. Update `screener.py` and `price_monitor.py` to compute regime signals at scan time and pass
   them into `screen_day()`.
5. Optionally expose k-value and ATR-percentile in signal output so context is visible.

### Validation checklist

- [ ] `screen_day()` accepts regime inputs and returns different thresholds for high-ATR vs low-ATR day
- [ ] Sector strategy Optuna studies complete with k-coefficient in [0.0, 1.0]
- [ ] Live scan computes ATR percentile from last 60 bars and passes it through correctly
- [ ] MSTR and AAPL produce visibly different RSI thresholds on the same scan day

---

## Summary

| Phase | Unlocks | Key files |
|---|---|---|
| 1 | Pre-market BUY + RAISE-STOP signals, terminal | `screener.py`, `position_monitor.py`, `portfolio.json`, `train.py` master update |
| 2 | Sector-specific signals | 11 sector worktrees, `strategy_runner.py`, sector routing |
| 3 | Chat-triggered signals, Telegram delivery | OpenClaw SKILL.md skills, VPS, GitHub deploy pipeline |
| 4 | Automatic daily scans, real-time triggers | Cron jobs, `price_monitor.py`, `indicators_snapshot.json`, Alpaca WebSocket |
| 5 | Regime-adaptive thresholds | Extended `screen_day()`, sector Optuna k-calibration |
