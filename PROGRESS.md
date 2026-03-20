# PROGRESS

## Run: mar20 — Energy/Materials Universe (2026-03-20)

### Results Summary

Branch: `autoresearch/mar20` | Best commit: `e9886df` | 30 iterations

| commit | sharpe | total_pnl | total_trades | win_rate | status | description |
|--------|--------|-----------|--------------|----------|--------|-------------|
| 48191e7 | 0.000 | $0 | 0 | — | keep | baseline (strict pullback screener, no trades) |
| 2ceed17 | 2.809 | — | 1 | — | keep | NaN guard + CCI -30 + pullback 5% |
| 68ac7ae | 5.176 | — | 18 | — | keep | momentum breakout: SMA50 + 20d high + vol 1.2× |
| b95992a | 5.266 | — | 19 | — | keep | relax volume to 1.0× MA30 |
| 2a3c800 | 5.382 | — | 19 | — | keep | add price_10am > prev day high |
| **e9886df** | **5.791** | **$952.88** | **18** | **77.8%** | **keep** | **add RSI(14) 50–75 filter** |

Avg PnL/trade: **$52.94** | Backtest: 2025-12-20 → 2026-03-20 | Universe: CTVA, LIN, XOM, DBA, SM, IYE, EOG, APA, EQT, CTRA, APD, DVN, BKR, COP, VLO, HEI, HAL

---

### Post-Run Analysis (2026-03-20)

#### 1. How did the momentum breakout idea emerge?

The pullback/oversold screener (CCI < -50 + 8% ATH pullback + 3 up-close days + SMA150 trend filter) produced **zero trades** on the energy/materials universe. After systematically relaxing every individual threshold — CCI from -50 to -30, pullback from 8% to 5% to 3%, SMA150 to within 3% below, volume from 0.85× to 0.7×, removing the ATH gate, removing the upper wick rule, trying ATR fallback stops — the trade count stayed at 0 or 1. Each change in isolation was insufficient.

The diagnosis was structural: the original screener was designed for a specific market condition (deep pullback within an uptrend, catching bounces from oversold CCI), and energy stocks in the Dec 2025–Mar 2026 period simply didn't present that pattern at sufficient frequency. The rules as a group were coherent but incompatible with the data.

The switch to a **momentum breakout** was a standard pattern recognition response: if "buying oversold bounces" doesn't fit the regime, try "buying trend continuations." Momentum breakout (price at a new N-day high, above trend MA, with volume) is a textbook complement to the pullback strategy — it targets *the same uptrending stock class* but at a different phase in the cycle (expansion rather than retracement). The idea came from the observation that these rules coexist in classical technical trading: CAN SLIM-style breakouts, Donchian channel breakouts, and IBD "Stage 2" breakout entries all share this structure.

No external source was consulted. The reasoning was: (a) energy stocks in this period were likely in trending moves rather than deep pullbacks, (b) the 17-ticker universe spans commodities, energy ETFs, and related industrials — a group that often trends together — (c) breakout strategies on a trending group in a 3-month window tend to catch inter-month momentum rather than within-week reversions.

#### 2. Is this strategy specific to these tickers and this timeframe, or is it a general strategy?

**Probably not general.** Several reasons for caution:

*What makes it work here specifically:*
- The energy/materials sector is highly correlated. When XOM, COP, CVX, EOG, DVN break out together, the signal is strong and the follow-through is real. A breakout screener on a universe of correlated assets in a trending macro regime produces multiple confirming signals at similar times, which creates the smooth positive mark-to-market curve that a high Sharpe ratio requires.
- The 3-month window (Dec 2025–Mar 2026) was short enough that macroeconomic conditions were broadly consistent throughout. There was no major reversal that would have turned breakout trades into loss-generators mid-run.
- The `price_10am > SMA50` filter keeps entries in the direction of the 50-day trend, which was likely positive for at least a subset of these tickers during this period.

*Why it likely degrades in other regimes:*
- **Bear markets or range-bound markets:** breakout strategies suffer badly when price breaks to a new high and then immediately reverses (false breakout). In those regimes, a mean-reversion or pullback screener would outperform.
- **Diversified universes (S&P 500, all sectors):** with lower inter-ticker correlation, you get more noise. False breakouts are more frequent, and the Sharpe degrades toward average.
- **Longer timeframes:** a 3-month window with one trending macro regime is the ideal laboratory for a breakout strategy. A 3-year backtest would capture multiple regime changes, and the strategy's Sharpe would normalize toward something more modest.
- **The RSI 50–75 filter is regime-sensitive:** it worked here because energy was in a momentum phase. In a distribution phase, RSI will be >75 when breakouts occur (and those will fail), or <50 (and the breakout won't trigger). The filter is not causally protective — it's calibrated to this specific regime.

The strategy is better understood as a **regime-conditional approach**: it performs well when: (a) the sector is trending upward, (b) the macro backdrop is broadly positive, and (c) the universe is correlated. That's a real and repeatable condition, but it requires knowing which regime you're in before deploying it.

#### 3. Next ideas to examine with more iterations

*On the screener:*
- **Sector-relative breakout**: require price to break out relative to the ETF benchmark (e.g. XOM > IYE's 20d high), filtering for alpha rather than beta. This separates genuine individual-stock moves from broad sector rallies.
- **Earnings/event filter**: exclude the 5 days around an earnings date — breakouts on earnings are often noise relative to the underlying trend signal.
- **Consecutive days below SMA before breakout**: require the stock spent at least 5 of the last 20 days below SMA50 before today's breakout. This would select "fresh starts" rather than extended momentum runs that are already overextended.
- **Volume spike on prior day**: require the day *before* the breakout day to have above-average volume (institutional accumulation signal), not just the breakout day itself.
- **ATR-normalized breakout distance**: require `(price_10am - high20) / atr > 0.5` — the breakout must clear the prior high by a meaningful ATR fraction, filtering out micro-breakouts.

*On position management:*
- **Partial exit at +2 ATR**: sell half the position at the first profit target, let the rest run. This improves the distribution of daily P&L changes and could raise Sharpe by reducing variance.
- **Time-stop**: close any position held > 20 trading days without hitting profit target. Aging trades that don't move tend to eventually become losses.
- **Volatility-scaled position size**: instead of fixed $500 per trade, size as $500 / ATR — higher ATR = smaller position. This normalizes dollar risk per trade and tends to improve Sharpe in cross-asset backtests.

*On context window constraints:*
The 30-iteration run consumed a large share of context but remained effective throughout. The primary risk of compacting and continuing is **loss of the "why" behind earlier discarded experiments** — if the context is compacted and later experiments revisit a discarded idea, the agent may repeat the same failed path. The results.tsv log partially mitigates this, but the log doesn't capture the reasoning, only the outcome.

My assessment: compacting and continuing with an additional 30 iterations is **feasible but slightly degraded** compared to a fresh start. The most useful approach would be to start a new session with this PROGRESS.md and results.tsv as context, explicitly noting which directions have been exhausted, then exploring the next tier of ideas (sector-relative signals, partial exits, ATR-scaled sizing) with full context available.

#### 4. Does this optimization method produce strategies worth trusting for live trading?

**With significant caveats.** The method — systematic single-parameter variation, keep-or-discard by Sharpe on a fixed backtest window — is a legitimate form of exploratory strategy research. But it has structural risks that the current setup doesn't fully address:

*Why you should be skeptical:*
- **Overfitting to one 3-month window**: the final strategy has ~8 parameters (SMA period, breakout window, volume ratio, RSI bounds, ATR stop multiplier, etc.) tuned against a single 3-month period on 17 correlated tickers. The effective number of out-of-sample observations is very small. A strategy that achieves Sharpe 5.8 in-sample on a short, coherent window frequently delivers Sharpe 0.5–1.5 out-of-sample.
- **The correlation problem**: 17 tickers from one sector in one macro regime are not 17 independent observations. The 18 trades are not 18 independent data points — many fired during the same macro move. The measured Sharpe is inflated relative to what a truly diversified 18-trade sample would imply.
- **No walk-forward or out-of-sample validation**: the right next step is to test the final strategy on a *different* 3-month window (e.g. Mar–Jun 2026, or Sep–Dec 2025) without re-optimizing, and measure whether the Sharpe degrades gracefully or collapses.
- **The momentum breakout discovery was regime-driven**: the original pullback strategy was the "intended" strategy, replaced because it didn't fit this period. That replacement was correct tactically, but it means the selected strategy is post-hoc selected for this specific regime — exactly the mechanism that produces overfitting.

*Why there's still signal worth pursuing:*
- The **RSI 50–75 filter** and **price > prev day high** condition are not arbitrary — they have genuine market microstructure rationale (filtering overbought entries, requiring intraday continuation). These types of rules have appeared consistently across decades of systematic strategy research.
- A **Sharpe of 5.8 in-sample, even if it degrades to 1.5 out-of-sample**, is a meaningful finding if the degradation pattern is understood. Many institutional strategies are run at Sharpe 0.8–1.5 live with much lower in-sample numbers.
- The **method itself** (autonomous iterative parameter search with keep/discard) is sound and scales. With more compute, a larger universe, multiple non-overlapping backtest windows, and proper walk-forward testing, it would produce more robust results. This run is a proof-of-concept that the loop works and can find meaningful improvements.

**Bottom line:** treat the current result as a *hypothesis generator*, not a deployment-ready signal. The next step that would meaningfully raise confidence is out-of-sample validation on at least two non-overlapping time windows using the final strategy without re-optimization.

---

### What to Do Next (2026-03-20)

#### Step 1: Validate the energy strategy before trusting it

Before doing anything with the optimized strategy — adding more strategies, deploying it, or using it as a starting point — test whether it actually works outside the window it was trained on. The mechanism:

1. Keep `prepare.py` and `train.py` at their current state (commit `e9886df`).
2. Change only `BACKTEST_START` / `BACKTEST_END` to a **different 3-month window** — ideally the one immediately *after* the training window: `2026-03-20 → 2026-06-20` (or whichever data is available). Do **not** re-optimize `train.py` at all.
3. Re-download data with `uv run prepare.py` (delete the cache first so fresh data comes in).
4. Run `uv run train.py` once and note the Sharpe.

**Interpreting the result:**
- Sharpe ≥ 2.0: the strategy has durable signal. The core logic (SMA50 trend filter, 20-day breakout, volume, RSI 50–75, intraday confirmation) is capturing something real about energy stock momentum. You can proceed with more confidence.
- Sharpe 0.5–2.0: the strategy has partial signal. It works but it's noisier than the in-sample result suggests. Useful as one input among several, not as a standalone system.
- Sharpe < 0.5 or negative: the strategy is largely overfit to the Dec 2025–Mar 2026 window. The optimization found a pattern that was specific to that period rather than a persistent one. Treat it as a learning exercise, not a deployable signal.

This test costs one data download and one backtest run. It is the single highest-value action available right now.

#### Step 2: What to do with the optimized strategy regardless of validation outcome

Even if out-of-sample validation is strong, this strategy should be treated as **regime-conditional**, not always-on. Use it when:
- The energy sector is in a clear uptrend (price of IYE or XLE above their own SMA50 and rising).
- Macro conditions are not in acute risk-off mode (energy correlates strongly with risk appetite).
- The broader market (SPY) is above its 50-day MA — breakout strategies fail badly in downtrends.

When *not* to use it:
- Sector is in a confirmed downtrend (energy stocks broadly below SMA50).
- High macro volatility / Fed shock / geopolitical disruption affecting commodities.
- Earnings season for large tickers in the universe — breakouts on earnings are structurally different from trend-continuation signals.

The practical approach: check the three conditions above before each potential trade. If all three are green, use the strategy. Otherwise, sit out. This turns the optimized strategy into a *conditional* signal rather than a mechanical one, which is how almost all professional systematic strategies are actually deployed.

#### Step 3: Adding a second strategy for a different sector

Yes — this is the right direction. The goal is a portfolio of regime-conditional strategies, each tuned to its sector's dominant price dynamics, deployed only when its matching regime is active. Here is how to do it cleanly:

**Structure:**
- Each sector gets its own branch: `autoresearch/energy-mar20`, `autoresearch/tech-apr20`, etc.
- Branches are **isolated** — never merge them. Each branch contains `train.py` tuned for its sector.
- `prepare.py` is re-configured (TICKERS + dates) per run, then reset.
- The active strategy per sector lives on its own branch. To use it, you check out that branch and run `train.py` with fresh data for the current period.

**Keeping the current strategy:**
Tag the energy strategy now: `git tag energy-momentum-v1 e9886df`. This creates a permanent, named reference to the best energy strategy independent of whatever happens to any branch. You can always `git checkout energy-momentum-v1` to recover it.

#### Step 4: What to start the next sector loop from — original or energy-optimized?

**Start from the energy-optimized strategy (`e9886df`), but understand why.**

The energy-optimized strategy replaced the original pullback screener with a momentum breakout. That replacement was not an energy-specific quirk — it was a response to the fundamental failure of the pullback screener on any trending universe. The momentum breakout structure (SMA trend filter + N-day high breakout + volume + RSI) is generic and well-grounded: it works across most trending equity markets and is not energy-specific.

The energy-specific tuning lives in the thresholds: SMA50 period (50), breakout window (20 days), volume ratio (1.0×), RSI bounds (50–75), ATR stop multiplier (2.0). These numbers will likely shift when you run the loop on a different sector — that is exactly what the optimization loop is for. Starting from `e9886df` means:

- You skip the "zero trades / pullback screener" phase that cost ~12 iterations this run.
- You start with a logically sound baseline that generates meaningful trades from day one.
- The loop immediately has something to work with and can focus on sector-specific threshold tuning.

If instead you start from the original pullback screener, you'll likely waste the first 10–15 iterations re-discovering that the pullback strategy generates too few signals, and then re-inventing the momentum breakout. That is not a good use of the iteration budget.

**The one exception:** if you deliberately want to test whether a *pullback* strategy works better for the new sector (e.g. defensive sectors like utilities or healthcare, where mean-reversion is historically more reliable than trend-following), then starting from the original pullback screener makes sense. The logic would be: "energy is momentum-driven, but utilities are mean-reverting — the optimization loop might find a genuine pullback signal there that it couldn't find in energy." In that case, starting from the original and letting the loop discover the right structure is the principled choice.

**Rule of thumb:**
- Sector is growth/cyclical (tech, energy, materials, industrials, discretionary) → start from `e9886df` (momentum breakout baseline).
- Sector is defensive/value (utilities, healthcare, staples, REITs) → start from the original pullback screener. Those sectors have historically rewarded mean-reversion more than momentum.
- Unsure → start from `e9886df` (it's the higher-quality starting point) and let the loop tell you.

#### Step 5: Recommended next sector and tickers

**Tech / semiconductors** as the second sector. Reasons:
- High liquidity, tight spreads, strong data availability.
- Semiconductors are a proxy for the broader tech cycle and tend to have clean trending behavior interrupted by sharp pullbacks — both momentum and pullback strategies can work, making it a good test of whether the loop converges differently.
- Low correlation to energy: if both strategies end up with signal, you get genuine portfolio diversification.
- Suggested universe: `NVDA, AMD, INTC, AVGO, QCOM, MU, AMAT, LRCX, KLAC, TSM, ASML, ARM, SMCI, ON, TXN, ADI, MCHP` (17 tickers to match the energy run).

**Suggested parameters for the tech run:**
- Same timeframe as energy for direct comparison: `2025-12-20 → 2026-03-20`
- Start from: `e9886df` (energy-optimized momentum breakout)
- The loop will likely tighten RSI bounds, widen the breakout window, or adjust volume thresholds for the higher-beta tech names.

---

### Next Agent Instructions — Three Parallel Worktrees (2026-03-20)

Three independent experiments to run in parallel. Each gets its own git worktree so they don't interfere with each other or with master.

---

#### How to create each worktree

Use the `ai-dev-env:create-worktree` skill for each worktree. Invoke it with the Skill tool:

```
Use the Skill tool to invoke ai-dev-env:create-worktree
```

The skill creates an isolated git worktree with its own working directory, sets up the environment, and returns a path you can `cd` into. Each worktree is on its own branch. Run the skill **three times** — once per experiment — before starting any of them.

---

#### Worktree A — Energy out-of-sample validation

**Purpose:** Validate whether the optimized energy strategy works on a *different* time window. No optimization — one run only.

**Create-worktree inputs:**
- Branch name: `autoresearch/energy-oos-sep25`
- Base commit: `e9886df` (tag: `energy-momentum-v1`, on branch `autoresearch/mar20`)
  - Note: base off `autoresearch/mar20`, NOT master — you need the optimized `train.py`

**After worktree is created, configure `prepare.py` USER CONFIGURATION only:**
```python
TICKERS = ["CTVA", "LIN", "XOM", "DBA", "SM", "IYE", "EOG", "APA", "EQT", "CTRA", "APD", "DVN", "BKR", "COP", "VLO", "HEI", "HAL"]
BACKTEST_START = "2025-09-20"
BACKTEST_END   = "2025-12-20"
```

**Also update `train.py` constants to match:**
```python
BACKTEST_START = "2025-09-20"
BACKTEST_END   = "2025-12-20"
```

**Important:** The parquet cache is global (`~/.cache/autoresearch/stock_data/`). Delete the existing energy ticker files before downloading so you get fresh data for the new date range:
```bash
rm ~/.cache/autoresearch/stock_data/CTVA.parquet  # repeat for all 17 tickers
# or: rm ~/.cache/autoresearch/stock_data/*.parquet  (only if not running other worktrees simultaneously)
```

**Run:**
```bash
uv run prepare.py    # downloads Sep 2024 – Dec 2025 for all 17 tickers
uv run train.py      # run ONCE, record Sharpe
```

**DO NOT run the optimization loop.** This is a validation run. Record the result in a file called `oos_result.txt` in the worktree root:
```
oos_result.txt:
Strategy: energy-momentum-v1 (e9886df)
In-sample:      Sharpe 5.791  | 18 trades | 2025-12-20 → 2026-03-20
Out-of-sample:  Sharpe X.XXX  | N trades  | 2025-09-20 → 2025-12-20
Verdict: [PASS if OOS Sharpe >= 2.0 / PARTIAL if 0.5-2.0 / FAIL if < 0.5]
```

**Note:** The price_10am NaN fix (forward-fill of 2026-02-02) was done manually on the global cache. The Sep-Dec 2025 window may have its own NaN day — run `uv run prepare.py` and check for WARNING lines. If any ticker shows NaN days in the backtest window, run the same forward-fill script used in the mar20 run:
```python
# Run via: uv run python -c "..."
import pandas as pd, os, glob
cache = os.path.expanduser('~/.cache/autoresearch/stock_data')
for f in glob.glob(os.path.join(cache, '*.parquet')):
    df = pd.read_parquet(f)
    if df['price_10am'].isna().any():
        df['price_10am'] = df['price_10am'].ffill()
        df.to_parquet(f)
        print(f'Fixed: {os.path.basename(f)}')
```

---

#### Worktree B — Semiconductors optimization (30 iterations)

**Purpose:** Optimize a momentum breakout strategy for the semiconductor sector over the same Dec 2025–Mar 2026 window. Starting from the energy-optimized baseline (which already generates valid signals from day 1).

**Create-worktree inputs:**
- Branch name: `autoresearch/semis-mar20`
- Base commit: `e9886df` (tag: `energy-momentum-v1`, on branch `autoresearch/mar20`)
  - Base off `autoresearch/mar20` — you need the optimized `train.py` with RSI, breakout continuation, etc.

**After worktree is created, configure `prepare.py` USER CONFIGURATION only:**
```python
TICKERS = ["NVDA", "AMD", "INTC", "AVGO", "QCOM", "MU", "AMAT", "LRCX", "KLAC", "TSM", "ASML", "ARM", "SMCI", "ON", "TXN", "ADI", "MCHP"]
BACKTEST_START = "2025-12-20"
BACKTEST_END   = "2026-03-20"
```

**Update `train.py` constants to match** (BACKTEST_START/BACKTEST_END are already correct — double-check they match).

**Run the full 30-iteration optimization loop** per `program.md`. The first run (baseline) will use the energy-optimized strategy as-is on semis tickers — record its Sharpe before touching anything. Then iterate.

**What to expect and try:**
- Semis are higher-beta than energy: ATR is larger relative to price. The SMA50 threshold, RSI bounds, and ATR stop multiplier may all need adjustment.
- Key directions to explore early:
  1. Widen RSI upper bound (75 → 80) — semis can run hotter before topping
  2. Increase ATR stop multiplier (2.0 → 2.5) — more volatile, need more room
  3. Tighten volume requirement (1.0× → 1.2×) — semis have cleaner institutional breakouts with volume
  4. Test SMA20 vs SMA50 as trend filter — shorter MA fits faster-moving names

**Cache note:** Use a **separate cache directory** to avoid collision with the energy run or OOS validation. Add this to the top of `prepare.py` (in the USER CONFIGURATION block, as an additional comment-change — the CACHE_DIR is derived, not a user config variable... so actually just be aware that if you run OOS validation and semis simultaneously they'll share `~/.cache/autoresearch/stock_data/` and may clobber each other). **Best practice: run Worktree A first (OOS validation), then delete the energy files and run Worktree B, OR run them at different times.** Alternatively, accept that they share the cache since the ticker sets don't overlap (OOS energy tickers vs semis tickers are different, so no collision between B and A/C).

**Run the loop and tag the best result:**
```bash
git tag semis-momentum-v1 <best_commit>
```

---

#### Worktree C — Utilities (defensive sector) optimization (30 iterations)

**Purpose:** Optimize a strategy for utilities — a defensive, mean-reverting sector. Starting from the **original pullback/pivot-based screener** (master), NOT the energy-optimized one. Utilities reward patience and mean-reversion; the momentum breakout may not fit.

**Create-worktree inputs:**
- Branch name: `autoresearch/utilities-mar20`
- Base commit: `3e15447` (master — contains the original pivot-based pullback screener)

**After worktree is created, configure `prepare.py` USER CONFIGURATION only:**
```python
TICKERS = ["NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "PCG", "ED", "XEL", "AWK", "ES", "WEC", "ETR", "CEG"]
BACKTEST_START = "2025-12-20"
BACKTEST_END   = "2026-03-20"
```

**Update `train.py` constants to match** (currently `2026-01-01` / `2026-03-01` on master — change both).

**Also apply the price_10am NaN guard** from the mar20 run — this bug exists on master too. Before the first experiment run, add it to `screen_day` in `train.py`:

```python
# In the NaN guard line, add pd.isna(price_10am):
if pd.isna(price_10am) or pd.isna(sma150) or pd.isna(vm30) or ...
```

This fix does not affect strategy logic and should be committed as the "setup" commit (not counted as iteration 1). Also forward-fill any NaN price_10am days after downloading (same script as Worktree A).

**Run the full 30-iteration optimization loop** per `program.md`.

**What to expect and try:**
- Utilities move slowly and have low ATR. The SMA150 trend requirement might be satisfied more often than energy (utilities are stable). The pullback screener may actually fire here.
- If the original screener still generates zero trades, apply the same diagnostic process from the mar20 run: relax CCI threshold first (-50 → -30), then pullback (-8% → -5%), then check if the momentum breakout structure (as in e9886df) is needed.
- Key directions specific to utilities:
  1. Loosen the volume filter (utilities are low-volume) — try 0.7×
  2. Tighten the pullback requirement (utilities don't pull back 8%, try 3–5%)
  3. Consider replacing the CCI filter with an RSI filter: RSI < 40 (oversold) and rising — utilities behave more like bond proxies than commodities
  4. The pivot-low stop logic may work better here than in energy (utilities have cleaner support levels from dividend-support buying)

**Tag the best result:**
```bash
git tag utilities-pullback-v1 <best_commit>
# (or utilities-breakout-v1 if the loop discovers breakout works better)
```

---

#### Parallel execution notes

- Worktrees A, B, and C can run in separate terminals simultaneously. Each is a fully isolated git working directory — no file conflicts.
- The parquet cache at `~/.cache/autoresearch/stock_data/` is **shared**. Ticker sets don't overlap between B (semis) and C (utilities), so those two can run simultaneously without issue. A (energy OOS) shares tickers with the mar20 run but uses a different date range — re-downloading overwrites the existing files. **Run A before B and C, or last. Do not run A simultaneously with any other worktree that uses energy tickers.**
- Each worktree has its own `results.tsv` and `run.log` — these are not tracked by git and do not conflict.
- The `program.md` and overall project structure is the same across all worktrees.

---

#### Step 6: Longer-term architecture — combining strategies

Once you have 2–3 sector-optimized strategies, the natural next step is a **meta-allocation layer**: a simple rule that determines which sector strategies are "on" at any given time based on regime indicators. A minimal version:

1. Each morning, check whether each sector ETF (IYE for energy, XLK for tech) is above its SMA50.
2. Run only the strategies whose sector ETF passes the trend filter.
3. Treat each active strategy as an independent signal; size each position at $500 as currently configured (or scale by portfolio size).

This is not complex code — it could be a simple wrapper script that checks the ETF condition and then invokes the appropriate branch's `train.py` logic on the current day's data. The key insight is that **the optimization loop already produces the sector-specific components**; you just need a lightweight routing layer on top.

---

## Status: All Phases Complete + Multi-Sector Optimization Results + Enhancement PRD — 86/87 tests passing, 1 skipped

---

## Feature: Phase 5 — End-to-End Integration Test + Post-Phase Enhancements
### Planning Phase
**Status**: ✅ Planned
**Started**: 2026-03-19
**Plan File**: .agents/plans/phase-5-end-to-end.md

### Phase 5 Core Implementation (2026-03-19)
**Status**: ✅ Complete — 86/86 tests passing (9 new integration + 77 pre-existing), 0 failures

**Files created/modified:**
- `prepare.py` — set TICKERS to 5-stock list; fixed Windows cp1252 encoding (`→` → `->` in print statements); fixed `price_10am` extraction from 10:00 AM → 9:30 AM bar (yfinance 1h bars labeled at 9:30 ET, no 10 AM bar exists)
- `tests/test_e2e.py` — created (9 integration tests + module-scoped fixtures)
- `tests/test_prepare.py` — updated 2 tests: 9:30 AM bar fix + patched-copy approach for empty-tickers test
- `.gitignore` — added `*.log` entry

**Key outcomes:**
- 5 parquet files cached: AAPL, MSFT, NVDA, JPM, TSLA — 289 rows each, Jan 2025 – Feb 2026
- Criterion 18 (screen_day on real Parquet data) resolved: VERIFIED ✅
- Agent loop viability confirmed: relaxed screener produces ≥ 1 trade across 42-day window

**Pre-existing bugs fixed:**
- `prepare.py`: Windows cp1252 encoding error on `→` arrow character
- `prepare.py`: `price_10am` always NaN — yfinance 1h bars start at 9:30 AM ET, not 10:00 AM

### Multi-Sector Optimization Runs (2026-03-20)

Five parallel worktrees were run to validate the optimization loop across sectors and time windows.

#### Results Summary

| Sector / Run | Branch | Window | Best Sharpe | Trades | Total PnL | Tag |
|---|---|---|---|---|---|---|
| Energy (in-sample) | `autoresearch/mar20` | Dec 20 → Mar 20, 2026 | 5.791 | 18 | — | `energy-momentum-v1` |
| Energy (OOS validation) | `autoresearch/energy-oos-sep25` | Sep 20 → Dec 20, 2025 | -0.010 | 6 | -$85 | — |
| Energy (OOS optimized) | `autoresearch/energy-oos-opt-sep25` | Sep 20 → Dec 20, 2025 | 8.208 | 73 | ~$956 | `energy-oos-v1` |
| Semis | `autoresearch/semis-mar20` | Dec 20 → Mar 20, 2026 | 4.754 | 34 | $602 | `semis-momentum-v1` |
| Utilities | `autoresearch/utilities-mar20` | Dec 20 → Mar 20, 2026 | 4.363 | 29 | $47 | `utilities-breakout-v1` |
| Financials | `autoresearch/financials-mar20` | Dec 20 → Mar 20, 2026 | 6.575 | 45 | **-$60** | `financials-v1` |

#### Cross-Sector Insights

**The pullback screener (master baseline) was wrong for every sector tested.** All runs that started from the pullback screener (utilities, and implicitly the original energy baseline) converged to momentum breakout structures within the first 7–10 iterations. The loop rediscovered the same structure independently each time.

**Sector-specific parameter tuning that emerged:**

| Parameter | Energy (in-sample) | Semis | Utilities | Energy OOS |
|---|---|---|---|---|
| Breakout window | 20-day high | 20-day | 5-day | 20-day |
| Trend filter | SMA50 | SMA20 | SMA30 | SMA50 |
| RSI range | 50–75 | 50–85 | 50–75 | 30–90 |
| Volume | 1.0× | 1.2× | 1.2× | 0.95× |
| ATR stop | 2.0× | 2.5× | 2.0× | 2.5× |
| Breakeven trigger | 1.5× ATR | 2.5× ATR | 1.5× ATR | 5.0× ATR |

**Breakeven trigger was the dominant variable across sectors.** Energy OOS and Financials both converged to 5× ATR as the breakeven trigger, producing the highest Sharpe gains of any single parameter. The original 1.5× trigger moves stops too early for momentum strategies — normal intraday pullbacks trigger it and kill positions before they run.

**OOS validation result: FAIL (Sharpe -0.01).** The in-sample energy strategy (Sharpe 5.79 on Dec–Mar) collapsed to -0.01 on the Sep–Dec window. This confirms the strategy overfit to the specific Dec–Mar 2026 energy regime during 28 iterations. The energy OOS optimization run then found a valid strategy for the Sep–Dec window (Sharpe 8.21) — confirming the window had tradeable signals, but requiring different parameters.

#### Sharpe Metric Flaw Discovered

The financials run produced Sharpe 6.58 with **total PnL of -$60**, exposing a structural flaw in the Sharpe computation:

- `daily_values` is the raw sum of mark-to-market open positions (not returns on capital deployed)
- On days with no positions, the value is `$0` — `diff(daily_values)` includes large entry/exit artifacts, not returns
- Any change that holds positions longer (e.g., raising the breakeven trigger) reduces stop-management events → smoother daily_values curve → lower variance → higher Sharpe, even if P&L worsens
- The optimizer found a degenerate optimum: "do less with stops" maximizes Sharpe while minimizing actual profit

This was compounded by the financials loop selecting RSI 65–90 entries, which tend to be near price exhaustion — entering overbought momentum stocks that frequently reverse.

**Conclusion:** Sharpe as currently computed is an unreliable optimization target. It can be gamed by smoothing the portfolio value trajectory without improving returns.

#### Proposed Enhancements

Six enhancements documented in `prd.md → Enhancements`:

1. **Train/test split** — last 2 weeks held out as test; optimization runs on train only; test P&L tracked per iteration for reporting
2. **Optimize for train P&L** — replace Sharpe as keep/discard criterion with `train_total_pnl`; Sharpe retained as informational metric
3. **Final test run outputs** — after loop completes, write `final_test_data.csv` (full per-ticker daily data for test window) and print per-ticker P&L table
4. **Sector trend summary (`data_trend.md`)** — `prepare.py` writes a one-paragraph trend summary after download (median return, up/down counts, top/bottom movers)
5. **Extended results.tsv** — add `train_pnl`, `test_pnl`, `win_rate` columns alongside existing `sharpe` and `total_trades`
6. **Multi-strategy registry + LLM-driven selector** — publish optimized strategies as named modules in a `strategies/` directory on master; at runtime an LLM selects which strategy to apply to a given ticker based on sector fit, regime match, and recent price behavior — not ticker membership — and provides a plain-language explanation of its choice

**Key architecture decisions for Enhancement 6:**
- Worktree branches are never merged into master. Strategy code is extracted via `git show <tag>:train.py` and committed as `strategies/<name>.py` on master — avoiding all cross-branch conflicts in `train.py`
- `METADATA` in each strategy file stores the full optimization context: sector, tickers, training window, source commit, train P&L/Sharpe
- Strategy selection at runtime is LLM-based: the selector receives all strategy metadata + the target ticker's recent OHLCV snapshot and reasons about sector alignment, regime similarity, and current price behavior. It outputs the chosen strategy and an explanation. A ticker that wasn't in a strategy's training universe can still be selected if the LLM judges the regime match appropriate (e.g. MRVL → semis strategy); a ticker that was in the training universe can be rejected if current conditions don't match (e.g. GS in a mean-reversion phase → no strategy applies)

See `prd.md → Enhancements` for full implementation spec.

---

### Post-Phase-5 Enhancements (2026-03-20)

#### 1. Multi-iteration multi-ticker agent loop test
Added `test_agent_loop_two_iterations_multi_ticker` (Test 9 in `tests/test_e2e.py`). Simulates 2 sequential agent loop iterations on all cached tickers (≥ 2 required). Iteration 1 relaxes CCI and pullback thresholds; Iteration 2 adds an ATR-based stop fallback and lowers the resistance gate. Verifies keep/discard accounting (best_sharpe == max of history) and that ≥ 1 trade fires with full relaxation.

#### 2. `run_backtest()` order swap: manage before screen
Swapped the per-day loop in `run_backtest()` so existing positions are managed (stop updated) **before** screening for new entries. Previously new entries were managed on their entry day, which is incorrect since the stop price was just set. The new order ensures newly opened positions are never managed on the same day they're opened.

#### 3. `manage_position()` ATR multiplier: 1× → 1.5×ATR
The breakeven trigger in `manage_position()` was `entry_price + 1.0 × ATR14`. Changed to `entry_price + 1.5 × ATR14` to match the minimum price-to-stop gap enforced at entry time by `screen_day()`. Both entry guard and ongoing management now apply the same 1.5×ATR buffer.

#### 4. SHA-256 golden hash update + developer comment
After the harness changed (order swap + developer note), updated `GOLDEN_HASH` in `tests/test_optimization.py` from `dca8913b…` to `fcbf75cf…`. Added a comment immediately below the `# ── DO NOT EDIT BELOW THIS LINE` marker in `train.py` instructing future maintainers to update the hash and rerun the relevant test whenever they intentionally change the harness.

#### 5. Configurable agent loop parameters
Made 3 agent loop parameters configurable from the user's Claude Code query, with defaults:

| Parameter | Default |
|-----------|---------|
| Timeframe | Past 3 months before today |
| Tickers | As listed in `prepare.py` TICKERS |
| Iterations | 30 |

**Files updated:**
- `prepare.py` — added developer comment to USER CONFIGURATION block noting it is overwritten by the agent loop setup; no logic changes
- `program.md` — full rewrite: added Parameters section, updated Setup steps (parse parameters → compute dates → edit prepare.py USER CONFIGURATION → edit train.py constants → run prepare.py → print parameter trace), added parameter trace format showing `[user-defined]` / `[default]` labels, changed "LOOP FOREVER" to "LOOP for configured iterations", clarified CANNOT-do constraints for prepare.py

### Reports Generated

**Execution Report:** `.agents/execution-reports/phase-5-end-to-end.md`
- Full pipeline validation: prepare.py → parquet cache → backtester → output block
- 8 integration tests implemented (Phase 5), 1 added post-phase (multi-iteration multi-ticker)
- Two pre-existing bugs discovered and fixed; Test 8 expanded to 4-parameter mutation due to Jan–Mar 2026 market conditions
- All 5 validation levels passed; system ready for autonomous agent handoff

---

## Feature: Phase 4 — Agent Instructions (`program.md`)
### Planning Phase
**Status**: ✅ Planned
**Plan File**: .agents/plans/phase-4-agent-instructions.md

### Implementation
**Status**: ✅ Complete — 74/74 tests passing (23 new + 51 pre-existing), 0 failures

**Files created/modified:**
- `program.md` — full rewrite: nanochat/GPU instructions → stock Sharpe optimization agent instructions with `results.tsv` logging, keep/discard loop, DO NOT EDIT boundary, crash handling, and no-trades guidance
- `tests/test_program_md.py` — created (23 structural tests covering: setup steps, output format, TSV schema, loop instructions, cannot-modify constraints, no legacy references)

**Key design decisions:**
- `results.tsv` schema: `commit | sharpe | total_trades | status | description` (tab-separated; commas break in descriptions)
- Status values: `keep`, `discard`, `crash`
- `NEVER STOP` instruction makes agent autonomous once loop begins
- `git reset --hard HEAD~1` on discard/crash; advance branch on keep
- Baseline run required before any mutations

### Reports Generated

**Execution Report:** `.agents/execution-reports/phase-4-agent-instructions.md`
- Full rewrite of `program.md`: legacy nanochat/GPU → stock Sharpe optimization agent instructions
- 23 structural tests implemented, all passing
- All 3 automated validation levels passed; 51 pre-existing tests unaffected
- 1 minor plan-authoring divergence (test count labeling inconsistency), resolved by following the code listing

---

## Status: Phase 3 Complete (51/51 tests passing) — Ready for Phase 4

## Feature: Phase 1 — Infrastructure Setup
### Planning Phase
**Status**: ✅ Planned
**Plan File**: .agents/plans/phase-1-infrastructure.md

## Feature: Phase 2 — Data Layer (`prepare.py`)
### Planning Phase
**Status**: ✅ Planned
**Plan File**: .agents/plans/phase-2-data-layer.md

### Implementation
**Status**: ✅ Complete — 16/16 tests passing (including integration), 0 failures

**Files created/modified:**
- `prepare.py` — full rewrite: old LLM pipeline → yfinance OHLCV downloader (~107 lines)
- `tests/test_prepare.py` — created (16 tests: 14 mock + 1 integration + 1 subprocess)
- `pyproject.toml` — added `[tool.pytest.ini_options]` with `integration` marker

### Code Review Findings — ✅ All Fixed (2026-03-18)

| Severity | Issue | Fix Applied |
|----------|-------|-------------|
| 🔴 High | `test_index_is_date_objects` false confidence — `isinstance(pd.Timestamp, datetime.date)` is `True` because Timestamp inherits from datetime.datetime; test passed even with wrong index type | Changed to `type(d) is datetime.date` |
| 🟡 Medium | `from datetime import datetime` unused at module level — caused `import datetime as _dt` workaround inside function body | Removed top-level import; promoted to `import datetime` at module level |
| 🟡 Medium | `process_ticker` did not call `os.makedirs(CACHE_DIR)` — would raise `OSError` if called as a library function outside `__main__` | Added `os.makedirs(CACHE_DIR, exist_ok=True)` before `to_parquet` |
| 🟢 Low | `import io` and `import sys` unused in `tests/test_prepare.py` | Removed both |

**Code review report:** `.agents/code-reviews/phase-2-data-layer.md`

### HISTORY_START Fix (2026-03-18)

`HISTORY_START` was `BACKTEST_START - 2 years = 2024-01-01`. yfinance enforces a **730-day rolling limit** for 1h interval data (~2 years from today). With today at 2026-03-18, the cutoff is ~2024-03-18, making `2024-01-01` out of range.

**Fix:** Changed `DateOffset(years=2)` → `DateOffset(years=1)`, giving `HISTORY_START = 2025-01-01`. This provides ~252 trading days of pre-backtest history (above the 200-row warning threshold) while staying well within the 730-day window.

**After fix:** Integration test `test_download_ticker_returns_expected_schema` now passes (was silently skipping with misleading "network unavailable" message).

### Reports Generated

**Execution Report:** `.agents/execution-reports/phase-2-data-layer.md`
- Full rewrite of `prepare.py`: old LLM pipeline → yfinance OHLCV downloader
- 16 tests implemented (14 mock + 1 integration + 1 subprocess)
- All 4 validation levels passed; 20/20 screener tests unaffected
- No divergences from plan

## Feature: Feature 2 — Screener (screen_day)
### Planning Phase
**Status**: ✅ Planned
**Plan File**: .agents/plans/feature-2-screener.md

### Implementation
**Status**: ✅ Executed — all 19 tests passing, changes unstaged

**Files created/modified:**
- `train.py` — full rewrite: `CACHE_DIR`, `load_ticker_data`, 7 indicator helpers, `screen_day` (11 rules), `manage_position` stub
- `tests/__init__.py` — created (empty)
- `tests/test_screener.py` — created (19 tests)

### Acceptance Criteria Validation
**Overall verdict: ACCEPTED** (21/22 PASS, 1 UNVERIFIABLE)

| # | Criterion | Verdict |
|---|-----------|---------|
| 1 | No `import torch`, `import kernels`, or GPU/NLP reference in `train.py` | ✅ PASS |
| 2 | All 7 indicator helpers present and importable | ✅ PASS |
| 3 | `screen_day` implements all 11 rules in exact order | ✅ PASS |
| 4 | `screen_day` uses lowercase column names only | ✅ PASS |
| 5 | `screen_day` uses `price_10am[-1]` (not `close[-1]`) for SMA, pullback, ATR buffer | ✅ PASS |
| 6 | `CACHE_DIR` defined as `~/.cache/autoresearch/stock_data` | ✅ PASS |
| 7 | `load_ticker_data` reads parquet, returns `None` if missing | ✅ PASS |
| 8 | `manage_position` stub present with correct signature | ✅ PASS |
| 9 | `screen_day` returns `None` (not raises) for < 150 rows | ✅ PASS |
| 10 | `screen_day` returns `None` for NaN indicators or zero volume MA30 | ✅ PASS |
| 11 | `screen_day` returns `None` when ATR is zero | ✅ PASS |
| 12 | `screen_day` raises `KeyError` when `price_10am` column is missing | ✅ PASS |
| 13 | `None` from `nearest_resistance_atr` treated as passing in R5 | ✅ PASS |
| 14 | `screen_day` returns only `None` or `dict` | ✅ PASS |
| 15 | Returned dict always contains `'stop'` as `float` | ✅ PASS |
| 16 | `stop < entry_price` always holds | ✅ PASS |
| 17 | `uv run python train.py AAPL` runs without error | ✅ PASS |
| 18 | `screen_day` runs on 10 trailing days of any Parquet file | ⚠️ UNVERIFIABLE — requires Phase 2 parquet cache |
| 19 | Level 1 import command exits 0 | ✅ PASS |
| 20 | `pytest tests/test_screener.py -v` passes 19/19 | ✅ PASS |
| 21 | No `print` inside helper functions or `screen_day` | ✅ PASS |
| 22 | `calc_cci` uses `raw=True` in `.rolling().apply()` | ✅ PASS |

### Code Review Findings — ✅ All Fixed (2026-03-18)

| Severity | Issue | Fix Applied |
|----------|-------|-------------|
| 🔴 High | Vacuous dict-contract tests (`test_return_dict_has_stop_key`, `test_stop_always_below_entry`) | Added `make_signal_df` fixture satisfying all 11 rules; removed `if result is not None:` guards; assertions now always execute |
| 🟡 Medium | `make_passing_df` docstring misleading about Rule 5 | Updated docstring to state it satisfies Rules 1-4 only |
| 🟡 Medium | `is_stalling_at_ceiling` ZeroDivisionError if `h_min == 0` | Added `if h_min == 0: return False` guard (`train.py:108`) |
| 🟢 Low | `c1`/`c2` not in NaN guard in `screen_day` | Added `pd.isna(c1) or pd.isna(c2)` to the NaN guard |
| 🟢 Low | `test_stalling_false_for_trending` relied on implicit linspace spacing | Replaced with explicit high values `[100, 120, 140]` |
| 🟢 Missing | No Rule 4 (CCI not rising) fail-path test | Added `test_rule4_fail_cci_not_rising` |

**Test suite: 20/20 passing** (was 19 tests, added 1 new test for Rule 4 CCI fail path).

**Criterion 18** (real Parquet smoke test) remains unverified — blocked on Phase 3 (`prepare.py`) by design.

### Reports Generated

**Execution Report:** `.agents/execution-reports/feature-2-screener.md`
**Code Review:** `.agents/code-reviews/feature-2-screener.md`

---

---

## Process Learnings

### yfinance 1h interval has a 730-day rolling window limit (2026-03-18)

**What happened:** `HISTORY_START = BACKTEST_START - 2 years` pushed the fetch start to `2024-01-01`, which is ~810 days ago. yfinance silently returns an empty DataFrame for 1h requests older than ~730 days. The integration test's skip message said "network unavailable" — masking the real cause.

**What to do differently:**
- For 1h yfinance data, `HISTORY_START` must stay within ~700 days of today (leave margin).
- 1 year of pre-backtest history gives ~252 trading days — sufficient for SMA150 warmup and well within the limit.
- When an integration test skips with a vague message, always verify the actual API response before assuming a network issue.

---

### Post-execution subagent issues in `ai-dev-env:execute` skill (2026-03-18)

**What happened:** After Phase 2 execution, the skill's post-execution subagents ran incorrectly: only 2 of 3 launched, the wrong `subagent_type` was used for one, and the Output Report was declared before subagents completed.

**Root causes:**
1. The executor stopped reading after launching 2 agents — missed the 3rd (`ai-dev-env:code-review`).
2. `superpowers:code-reviewer` was used as `subagent_type` instead of `general-purpose` — bypassed actual skill invocation.
3. Subagents ran in background after the Output Report, so a REJECTED verdict could never gate completion.

**Fix documented in:** `~/projects/ai-dev-env/subagents/fix.md`

**Rules for post-execution subagents:**
- All 3 are mandatory — none can be skipped.
- Always use `subagent_type: "general-purpose"`; prompt must begin with `"Use the Skill tool to invoke ai-dev-env:<skill-name>"`.
- Launch all 3 **foreground** before writing the Output Report so failures can gate completion.

---

### Phase vs. Feature numbering confusion (2026-03-18)

**What happened:** The PRD defines both *features* (Feature 1, 2, 3 — logical groupings) and *implementation phases* (Phase 1: Infrastructure, Phase 2: Data Layer, Phase 3: Strategy — execution ordering). When asked to "plan feature 2," the planning agent matched on the feature number and planned the Screener, skipping the Data Layer (Phase 2) which the Screener depends on for real-data integration testing.

**The `create-prd` skill is not the issue** — it correctly requires an `## Implementation Phases` section in every PRD. The PRD for this project has it.

**What to do differently:**

- When kicking off a plan, always reference the **Implementation Phases section** of the PRD, not the feature number. Say "plan Phase 2 from the PRD" or "what is the next pending phase?"
- The planning agent should open the PRD, find `## Implementation Phases`, identify which phases are complete vs. pending, and confirm the mapping before writing the plan.
- If a PRD uses both "Feature N" and "Phase N" labels, treat Phase N as the authoritative execution order.

**Current state:** Phase 1 (Infrastructure) ✅ complete. Phase 2 (Data Layer — `prepare.py`) is the correct next step, not yet started.

---

## What Was Done This Session

### Context
Transformed the `autoresearch` project from an LLM-driven nanochat pretraining optimizer into a stock trading strategy optimizer. The structure of the autonomous experiment loop (modify → run → log → keep/revert) is preserved. The optimization target changes from `val_bpb` (lower is better) to Sharpe ratio (higher is better).

### Artifacts Produced

| File | Description |
|------|-------------|
| `initial_request.md` | Structured analysis of the user's request, with a table of open design questions |
| `example-screener.py` | Reference screener implementation (v2) provided by user — source of baseline strategy |
| `prd.md` | Full Product Requirements Document (created via `ai-dev-env:create-prd` skill) |

### Key Decisions Made

| Topic | Decision |
|-------|----------|
| Ticker universe | Fixed `TICKERS = []` placeholder in `prepare.py` — user fills before running |
| Backtest window | Jan 1, 2026 – Mar 1, 2026 (`BACKTEST_START` / `BACKTEST_END` constants) |
| Data granularity | 1h yfinance bars, resampled to daily OHLCV + `price_10am` (Open of 10:00 AM ET bar) |
| Indicator history | Download 2 years before `BACKTEST_START` for SMA150/ATR14/CCI warmup |
| Entry price | `price_10am + $0.03` slippage |
| Position size | $500 / entry_price (fractional shares allowed) |
| Stop detection | Previous day's `low < stop_price` → position closed at stop price |
| Re-entry | Skip ticker if already held in portfolio |
| Sharpe formula | Daily dollar P&L changes, annualized (`× √252`), risk-free rate = 0% |
| End-of-backtest | All open positions closed at last available `price_10am` |
| Baseline strategy | Full v2 screener from `example-screener.py` + breakeven stop manager |
| GPU required | No — pure Python / pandas / numpy |

---

## Feature: Phase 3 — Strategy + Backtester (`train.py`)
### Planning Phase
**Status**: ✅ Planned
**Plan File**: .agents/plans/phase-3-strategy-backtester.md

### Implementation
**Status**: ✅ Complete — 51/51 tests passing (15 new + 20 screener + 16 prepare), 0 failures

**Files created/modified:**
- `train.py` — Added `BACKTEST_START`/`BACKTEST_END` constants, `load_all_ticker_data()`, real `manage_position()`, `run_backtest()`, `print_results()`; replaced `__main__` debug block (+152/-11)
- `tests/test_backtester.py` — Created (15 tests: 5 manage_position + 8 run_backtest + 2 output format)

### Code Review Findings — ✅ All Fixed (2026-03-18)

| Severity | Issue | Fix Applied |
|----------|-------|-------------|
| 🟢 Low | Plan comment stated ATR14 ≈ 2.0 for `atr_spread=2.0`; actual TR = 2×spread = 4.0 | Test fixture comment corrected |
| 🟢 Low | `@pytest.mark.integration` applied to test that requires no network (synthetic DataFrame) | Marker removed; test runs unconditionally |
| 🟢 Low | Missing blank line (PEP 8) between `manage_position` and `run_backtest` | Two blank lines added |

### Acceptance Criteria Validation — ✅ ACCEPTED (2026-03-18)

All 14 criteria passed. Key verifications:
- Stop-hit detection uses `prev_day` low (no look-ahead) ✅
- No double-entry for tickers already in portfolio ✅
- End-of-backtest close at `price_10am.iloc[-1]` ✅
- Sharpe `std == 0` guard returns 0.0 (not inf/nan) ✅
- `grep "^sharpe:"` captures exactly one parseable float ✅
- Exit code 1 on empty cache confirmed ✅

### Reports Generated

**Execution Report:** `.agents/execution-reports/phase-3-strategy-backtester.md`
- Full backtester implementation: `manage_position()`, `run_backtest()`, `print_results()`, updated `__main__`
- 15 tests implemented (5 manage_position + 8 run_backtest + 2 output format)
- All 4 validation levels passed; 36 pre-existing tests unaffected
- 3 minor code-review divergences, all improvements

---

## System Ready

All 5 phases are complete. The pipeline is fully operational:

1. `uv run prepare.py` — downloads and caches OHLCV data for the configured tickers
2. `uv run train.py` — runs the backtest, prints a fixed-format results block
3. Agent loop (via `program.md`) — autonomously mutates `train.py`, commits, backtests, keeps or reverts

To start an experiment session: open a Claude Code conversation in this repo and describe your desired run parameters (tickers, timeframe, iterations). The agent will handle setup and run autonomously.
