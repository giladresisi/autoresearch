# Mar20 Run: Post-Run Analysis and Next Steps

*Archived from PROGRESS.md on 2026-03-22*

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
