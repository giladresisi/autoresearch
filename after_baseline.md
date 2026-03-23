# After the Baseline Strategy

This document describes the full roadmap starting from when the baseline optimization run
completes in the `multisector-mar23` worktree. It captures the architectural decisions made
and explains the reasoning behind each step.

---

## Context

The baseline strategy is being optimized in `../multisector-mar23` using:
- **85 tickers** across 9 GICS sectors (Tech/IT, Communication Services, Consumer
  Discretionary, Financials, Health Care, Energy, Consumer Staples, Industrials, Materials)
- **Timeframe**: September 2024 – March 2026 (varied regimes: post-election rally, Fed rate
  cuts, AI volatility, tariff shock, corrections)
- **Test set**: includes holdout tickers not seen during training — validates cross-ticker
  generalization

The baseline is intentionally broad: a single strategy that must work across all sectors and
all regimes. It is not expected to be the best possible strategy for any specific context, but
it establishes a performance floor that every future specialist strategy must beat.

---

## Phase 0: Lock In the Baseline

*Triggered manually when the optimization run in `multisector-mar23` completes.*

### Step 1 — Fetch and register the baseline strategy
**Manual trigger, automated execution.**

Run `/fetch-strategies` from the master branch. This extracts the best strategy found in the
worktree, auto-populates METADATA (tickers, dates, metrics), generates an LLM description,
and updates REGISTRY.md.

```
/fetch-strategies
```

**Why:** The baseline is the performance floor. Every future sector specialist must beat it on
its own sector's holdout tickers. Without registering it, there is no benchmark to compare
against.

### Step 2 — Update `train.py` on master with the baseline strategy
**Manual.**

Copy the strategy functions (indicators, screener, signal logic) from the registered baseline
strategy file into `train.py` on master, replacing whatever is currently there. Do not change
the date constants, harness, or infrastructure preamble.

**Why:** `train.py` on master is the canonical starting point for all future worktrees.
Seeding it with the baseline means every sector-specific worktree starts from a known-good
strategy rather than from scratch, giving Optuna a warm start and reducing the number of
iterations needed to find improvements.

### Step 3 — Delete old strategies from the registry
**Manual.**

Remove all strategy `.py` files created before this baseline — they were built against earlier
versions of the harness and their metrics are not comparable. Keep only the baseline. Git
history preserves everything if needed.

**Why:** Old strategies create noise in the registry and false reference points. A clean
registry with one entry (the baseline) makes future comparisons unambiguous.

### Step 4 — Review baseline metrics before proceeding
**Manual judgment.**

Look at the Optuna results for the baseline run:
- **Train vs. test P&L gap**: a large gap means the strategy overfit to the training tickers.
  If the test set collapses entirely, the universal strategy is already overfit and sector
  specialization becomes more urgent.
- **Trade count**: too few trades means the screener is too restrictive; too many means poor
  signal quality.
- **Max drawdown**: if drawdown is extreme even on training data, the base strategy has
  structural problems that specialization won't fix.

Do not proceed to Phase 1 if the baseline has severe structural problems — fix them first.

---

## Phase 1: Per-Sector Strategies

### Architecture decisions

**One strategy per GICS sector (all 11).** When running on the full S&P 500 + Russell 1000
universe, all 11 GICS sectors are meaningfully populated:

| Sector | ~Russell 1000 count | Notes |
|---|---|---|
| Information Technology | ~170 | Largest |
| Financials | ~150 | Includes BRK.B |
| Health Care | ~130 | |
| Industrials | ~130 | Includes Aerospace & Defense (RTX, LMT, NOC) |
| Consumer Discretionary | ~120 | Includes AMZN, TSLA |
| Real Estate | ~70 | REITs, rate-sensitive |
| Consumer Staples | ~70 | Includes agricultural processors (ADM, BG) |
| Communication Services | ~60 | META, GOOGL, NFLX |
| Energy | ~50 | |
| Materials | ~50 | Includes fertilizer/ag-input companies (MOS, CTVA) |
| Utilities | ~50 | Smallest, very low beta, rate-sensitive |

Utilities and Real Estate are worth dedicated strategies despite their smaller size — their
price behavior is driven by interest rates and dividends, not by the same factors that drive
IT or Financials. A momentum strategy tuned for IT would perform poorly on NEE or PLD.

**No separate high-volatility group.** Earlier reasoning considered grouping high-vol outliers
(COIN, MSTR, SMCI, PLTR) into a separate "high-vol strategy." This was rejected in favor of
volatility-adaptive parameters within each sector strategy (see Phase 2). COIN stays in
Financials; MSTR stays in IT. The strategy for each sector handles their extreme ATR
automatically via continuous parameter adjustment — no binary classification needed.

**GICS sector determined from yfinance at runtime, not hardcoded.** This is required for
live trading on arbitrary tickers beyond the 85-ticker training universe. See the Strategy
Selector section below.

### GICS notes

GICS has no Agriculture sector. Agricultural companies are distributed:
- Grain/food processors (ADM, BG) → **Consumer Staples**
- Fertilizers/agrochemicals (MOS, CF, NTR, CTVA) → **Materials**
- Farm equipment (DE) → **Industrials**

Defense companies (RTX, LMT, NOC, GD, TXT) → **Industrials** (Aerospace & Defense
sub-industry). Counterintuitive, but consistent with GICS classifying by business activity
(manufacturing, systems integration) rather than end customer (government). Their correlation
with XLI holds reasonably well outside of geopolitical shock events.

If the Industrials strategy consistently underperforms on defense names specifically, split
Aerospace & Defense into its own strategy at that point — don't pre-emptively do it.

### Step 5 — Select tickers for each sector study
**Manual.**

For each of the 11 sectors, choose 8–12 tickers using these criteria in order:
1. **S&P 500 membership** — ensures liquidity and data quality in yfinance
2. **Top by market cap in sector** — well-known, high-ADV, institutional flows make
   technical patterns more reliable and repeatable
3. **High correlation with sector ETF** — β > ~0.5 vs. XLK/XLF/XLE/etc. over a 6-month
   rolling window. Low-β tickers (e.g., MSTR vs. XLK) are behavioral outliers in their
   sector; include them anyway since the volatility-adaptive parameters will handle them, but
   be aware that extremely low-β tickers may produce noisy optimization results
4. **Behavioral diversity within sector** — aim for sub-industry spread. In Financials, for
   example: a megabank (JPM), an investment bank (GS), a payment network (V), an asset
   manager (BLK). They all correlate with XLF but have different volatility profiles, which
   gives Optuna more signal to calibrate the volatility-adaptive parameters

This selection is a one-time manual judgment per sector. It does not need to match the live
trading universe exactly — it just needs to be representative.

### Step 6 — Prepare and run 11 sector worktrees in parallel
**Manual trigger, automated execution.**

For each sector, run `/prepare-optimization` from master, specifying the sector name and
ticker list. All 11 can be prepared in sequence and run simultaneously since they are fully
independent.

Each worktree:
- Starts from master's `train.py` (seeded with the baseline strategy from Step 2)
- Runs Optuna on the sector-specific ticker list
- Uses the same date range as the baseline for comparability

Each optimization run is triggered manually in its own Claude Code session:
```
cd ../it-mar23 && claude
> Run the optimization for [sector ticker list]
```

**Why parallel:** Sector studies are fully independent — no shared state, no ordering
dependency. Running them simultaneously reduces wall-clock time from 11× to 1×.

### Step 7 — Fetch all sector strategies and compare against baseline
**Manual trigger, automated execution.**

When all 11 sector runs complete, run `/fetch-strategies` to extract and register all of
them. Then compare each sector specialist against the baseline on the holdout test set for
that sector's tickers.

Decision rule: if a sector specialist beats the baseline on its sector's holdout tickers,
adopt it. If it doesn't, investigate why (overfitting? not enough iterations? sector too
heterogeneous?) before discarding.

---

## Phase 2: Volatility-Adaptive Parameters

*Added after Phase 1 strategies are stable. Requires code changes to `train.py`.*

### What this is

Each sector strategy currently has fixed thresholds (e.g., `RSI_ENTRY = 45`). Phase 2
replaces fixed thresholds with functions of continuously observable market signals:

```
RSI_entry_threshold = base_RSI + k_atr × ATR_percentile + k_adx × ADX_normalized
```

Where:
- `base_RSI`, `k_atr`, `k_adx` — fixed constants learned by Optuna during optimization
- `ATR_percentile`, `ADX_normalized` — recomputed fresh each bar from price data in the cache

This handles the high-vol outlier problem without a separate strategy or classifier: MSTR in
the IT sector gets a wider RSI band automatically because its ATR_percentile is high. AAPL
gets a tighter band. The same strategy serves both.

### Regime signals to add

All computable from OHLCV already in the cache:

| Signal | What it proxies | Computation |
|---|---|---|
| ATR percentile (60-day rolling) | Volatility regime | ATR(60) / Close, ranked within the cached universe |
| ADX (14-day) | Trending vs. choppy | Standard Wilder ADX from OHLCV |
| % tickers above 50d SMA | Market breadth / risk-on vs. off | Cross-ticker pass at each bar; requires all tickers loaded |
| Sector ETF momentum | Sector-level trend | Requires adding ~11 ETF tickers (XLK, XLF, XLE, etc.) to `prepare.py` |

Start with ATR percentile only (single k coefficient per threshold). Add additional signals
one at a time, verifying each one improves out-of-sample performance before keeping it.

### How Optuna handles this

No change to the Optuna study structure. Instead of searching for `RSI_ENTRY = 45` (one
scalar), it searches for `base_RSI = 42, k_atr = 0.8` (two scalars per threshold). The trial
count may need to increase proportionally to the added parameter count.

Run this as a new optimization on top of each Phase 1 sector strategy — don't restart from
the baseline.

---

## Live Trading: Strategy Selector

The strategy selector answers one question: given a ticker, which strategy do I use?

### Sector lookup

```python
import yfinance as yf

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
```

**Cache the lookup** — `.info` is a slow HTTP call. Query once per ticker, persist to a JSON
file, refresh when new tickers are added. Sector assignments change rarely enough that weekly
refresh is sufficient.

**Handle `None` returns** — SPACs, certain ADRs, and stale yfinance data return no sector.
Do not default to a catch-all strategy. Skip the ticker and log it for manual review. A wrong
strategy is worse than no trade.

### Selector logic

```python
def select_strategy(ticker: str, sector_cache: dict, strategies: dict):
    sector = get_sector(ticker, sector_cache)
    if sector is None:
        return None  # skip — no sector data
    strategy = strategies.get(sector)
    if strategy is None:
        return None  # skip — no strategy for this sector yet
    return strategy
```

In Phase 2, the selected strategy internally adjusts its thresholds using the ticker's
current ATR percentile — no additional selector logic required.

---

## Summary of Steps

| Step | What | Manual / Automated | Depends on |
|---|---|---|---|
| 1 | Fetch & register baseline strategy | Manual trigger, automated script | Baseline run complete |
| 2 | Update master `train.py` with baseline | Manual | Step 1 |
| 3 | Delete old strategies from registry | Manual | Step 1 |
| 4 | Review baseline metrics | Manual judgment | Step 1 |
| 5 | Select tickers per sector | Manual | Step 4 (go/no-go) |
| 6 | Prepare & run 11 sector worktrees | Manual trigger per worktree, automated optimization | Step 2, Step 5 |
| 7 | Fetch sector strategies, compare vs. baseline | Manual trigger, automated script | Step 6 complete |
| 8 | Add volatility-adaptive parameters to `train.py` | Code change (manual) | Step 7 stable |
| 9 | Re-run sector studies with Phase 2 architecture | Manual trigger, automated optimization | Step 8 |
| 10 | Build and deploy strategy selector | Code (manual) | Step 7 or Step 9 |
