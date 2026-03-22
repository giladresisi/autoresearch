# Merged Harness Upgrade Recommendations

**Generated:** 2026-03-22 14:03
**Sources:** `autoresearch/energy-mar21`, `autoresearch/nasdaq100-mar21`
**Total worktrees with upgrades:** 2

---

## Recommendations

### R1: Fix look-ahead bias in indicator computation
**Category:** `harness-structure`
**Priority:** `high` — this is a correctness bug, not a tuning suggestion
**Seen in:** `autoresearch/nasdaq100-mar21`
**Rationale:** `screen_day(df, today)` slices with `df.loc[:today]`, which includes today's
full OHLCV (including the close). All indicators — SMA, RSI, ATR — are computed on today's
close, but in live trading only yesterday's close is available at the 9:30am entry time.
The strategy is seeing the future when deciding whether to enter.
**Suggested change:** Compute all indicators on `df.iloc[:-1]` (yesterday and prior) and
read only `price_10am` from today's row for entry/stop evaluation. This may materially
change which trades fire and should produce a less inflated backtest.

---

### R2: Walk-forward cross-validation as the optimization objective
**Category:** `harness-objective`
**Priority:** `high`
**Seen in:** `autoresearch/energy-mar21`, `autoresearch/nasdaq100-mar21`
**Rationale:** Both runs converged to strategies that exploit a single trending window. The
energy run stripped out every entry filter and achieved 88.9% win rate on 18 trades — a
binomial 95% CI of [0.65, 0.97], statistically indistinguishable from luck. The current
single train/test split lets the agent inadvertently tune to the specific path of the
training window. There is no mechanism to detect or penalize regime-specific exploitation.
**Suggested change:** Replace the single split with rolling sub-windows evaluated in one
`uv run train.py` call:
```
Window A: train days  1–50  → test days 51–65
Window B: train days 11–60  → test days 61–75
Window C: train days 21–70  → test days 71–85
```
The keep/discard decision uses the **minimum test PnL across all windows** (pessimistic
objective). A strategy that earns +2,000 in Window A but −500 in Window B scores lower than
one earning +700 in all three. This is the single highest-impact harness change for
generalizability.

---

### R3: Risk-proportional position sizing
**Category:** `harness-structure`
**Priority:** `high`
**Seen in:** `autoresearch/energy-mar21`, `autoresearch/nasdaq100-mar21`
**Rationale:** The current harness sizes every trade at `$500 / entry_price = shares`
regardless of stop distance. A tight pivot stop (0.5 ATR) and a wide fallback stop (2.0 ATR)
both get the same dollar position, carrying 4× different dollar risk. The optimizer can
inflate PnL by favouring strategies that trigger many tight-stop entries — a leverage
artefact, not real alpha. The current energy result (18 trades at $136/trade avg) almost
certainly inflated by this: tight pivot stops got the same position as loose fallback stops.
**Suggested change:**
```python
RISK_PER_TRADE = 50.0  # dollars risked per trade (user-editable constant)
dollar_risk = entry_price - stop_price
shares = RISK_PER_TRADE / dollar_risk
```
Place `RISK_PER_TRADE` in the mutable block so the agent can tune it. Every trade now risks
the same dollar amount regardless of stop distance. PnL then measures risk-adjusted skill,
not position size luck.

---

### R4: Hide test PnL during the optimization loop
**Category:** `harness-structure`
**Priority:** `high`
**Seen in:** `autoresearch/energy-mar21`, `autoresearch/nasdaq100-mar21`
**Rationale:** The agent sees `test_total_pnl` after every experiment. Even without
consciously optimizing for it, seeing the test score 30–100 times creates implicit selection
pressure: experiments that happen to test well tend to be kept even when the improvement is
pure noise. The "test" is not truly held out. Energy run: test PnL oscillated between $200
and $800 across iterations with no clear correlation to train improvement — strong evidence
of noise accumulation. Nasdaq100 run: all 100 iterations had access to the same test window,
making it effectively a second training signal.
**Suggested change (two compatible options):**
- *Simple:* During the experiment loop, print `test_total_pnl: HIDDEN`. Reveal the true
  value only in the final run. This is equivalent to a proper ML holdout.
- *Stronger (energy's approach):* Introduce a third segment:
  `BACKTEST_START → SILENT_END` (train), `SILENT_END → TRAIN_END` (visible test, shown
  each iteration), `TRAIN_END → BACKTEST_END` (silent holdout, evaluated only in final run).
  Where `SILENT_END = TRAIN_END − 14d`. The agent can see visible test PnL each iteration;
  the silent holdout is untouched until the loop completes.

---

### R5: Trade-level attribution logging
**Category:** `harness-structure`
**Priority:** `medium`
**Seen in:** `autoresearch/energy-mar21`
**Rationale:** The agent can only observe aggregate metrics (total PnL, trade count, win
rate, Sharpe). It cannot tell whether losing trades all used ATR fallback stops, came from
the same ticker, or clustered in one time window. Without trade-level detail the agent can
only adjust global thresholds — it is flying blind. In the energy run, fallback stops and
pivot stops produced very different outcomes but the agent had no way to observe this.
**Suggested change:** After every run write a `trades.tsv` alongside `run.log`:
```
ticker  entry_date  exit_date  days_held  stop_type  entry_price  exit_price  pnl
XOM     2026-01-03  2026-01-12  9          pivot      89.40        92.15       15.41
HAL     2026-01-05  2026-01-05  0          fallback   36.20        35.80       -2.22
```
`stop_type` is returned in the signal dict from `screen_day`. `days_held` is trading days
between entry and exit. With this the agent can: filter fallback-stop entries if
systematically bad; add a max-hold-period exit; spot ticker or date-cluster weaknesses.

---

### R6: Ticker holdout / out-of-universe generalization test
**Category:** `harness-split`
**Priority:** `medium`
**Seen in:** `autoresearch/energy-mar21`, `autoresearch/nasdaq100-mar21`
**Rationale:** Both runs used the same ticker universe for training and testing, so
overfitting to individual ticker price paths is invisible. With 17 tickers (energy), the
agent can effectively memorize which specific stocks' price paths match its current screener.
With 101 tickers (nasdaq100) there is more diversity, but all test-window stocks were also
in the training window. A strategy with genuine structural edge should transfer to unseen
tickers.
**Suggested change (two compatible approaches):**
- *Per-experiment rotation (energy's approach):* Hold out 3 tickers from training each
  experiment. Evaluate those 3 in a separate "ticker holdout" backtest on the same date
  window. Report both `train_pnl` (N−3 tickers) and `ticker_holdout_pnl` (3 tickers).
  Rotate the held-out set every 5 experiments.
- *Structural split (nasdaq100's approach, for large universes):* Designate tickers 1–50
  as training universe and 51–101 as test universe. Run both. If learned parameters
  generalise across the split, the strategy is structurally sound.

---

### R7: Risk-adjusted optimization objective (Calmar or consistency)
**Category:** `harness-objective`
**Priority:** `medium`
**Seen in:** `autoresearch/nasdaq100-mar21`
**Rationale:** Maximising raw PnL incentivises concentration: one large winner can mask an
unstable strategy. In the nasdaq100 run, $1,974 train PnL came largely from a small number
of large winners — the distribution is wide, not consistent. A strategy making $400/month
consistently is more deployable than one making $1,800 in month 1 and −$600 in month 2,
even though the latter has higher total PnL.
**Suggested change:** Compute and print additional metrics each run:
- `train_calmar`: total PnL / max intra-window drawdown
- `train_pnl_consistency`: min(monthly PnL across training months)
Consider making keep/discard use `train_calmar` as a co-criterion (require both PnL and
Calmar to improve, or use `train_calmar` as the primary objective).

---

### R8: Simultaneous position cap and correlation penalty
**Category:** `harness-structure`
**Priority:** `medium`
**Seen in:** `autoresearch/energy-mar21`
**Rationale:** With 17 correlated energy tickers the harness opened 10+ simultaneous long
positions on the same day. These are not independent bets — they are a leveraged sector bet.
The energy run's high win rate partly reflects this: the entire sector moved together during
the training window. This inflates apparent alpha and masks concentration risk that would
be catastrophic in a sector drawdown.
**Suggested change:**
```python
MAX_SIMULTANEOUS_POSITIONS = 5   # hard cap (user-editable)
CORRELATION_PENALTY_WEIGHT = 0.0  # 0.0 = off; agent can increase
```
`run_backtest()` enforces the cap: no new entry if `len(portfolio) >= MAX_SIMULTANEOUS_POSITIONS`.
Separately, compute average pairwise correlation of entered tickers (from training-window
returns) and subtract `CORRELATION_PENALTY_WEIGHT × avg_correlation × total_pnl` from the
reported metric. Forces the agent to seek uncorrelated entries rather than sector sweeps.

---

### R9: Adversarial noise / robustness perturbation test
**Category:** `harness-objective`
**Priority:** `medium`
**Seen in:** `autoresearch/energy-mar21`, `autoresearch/nasdaq100-mar21`
**Rationale:** Optimized stop levels and entry prices are point estimates. In live trading,
fills are imperfect and stops gap. A strategy with stops placed at exactly 1.5 ATR from a
pivot is fragile to small displacements. Both runs converged to suspiciously exact parameter
values (energy: stop at exactly 1.5 ATR; nasdaq100: SMA gap at exactly 1.001) suggesting
knife-edge fitting to specific historical prices.
**Suggested change (two complementary approaches):**
- *Price perturbation (energy):* Before each evaluation, randomly perturb entry prices
  ±0.5% and stop levels ±0.3 ATR, run 5 seeds, report `pnl_min` and `pnl_max`. Keep/discard
  on `pnl_min`.
- *Date + parameter perturbation (nasdaq100):* Shift `BACKTEST_START` ±7 days and vary
  ATR multiplier ±0.2; if PnL drops > 20% under minor changes, penalise. New objective:
  `train_pnl × robustness_factor`.

---

### R10: Bootstrap confidence interval on final PnL
**Category:** `harness-structure`
**Priority:** `low`
**Seen in:** `autoresearch/energy-mar21`
**Rationale:** 18 trades / 17 trades is statistically thin — the 95% binomial CI on win
rate spans ~30 percentage points. The optimizer has no way to distinguish genuine alpha from
a lucky sequence, and neither does the user reading the final result. A single-path PnL
figure implies precision that the sample size cannot support.
**Suggested change:** In the final test run (when `WRITE_FINAL_OUTPUTS = True`) perform
bootstrap resampling of the trade list: resample with replacement 1,000 times and report:
```
bootstrap_pnl_p5:    412.30
bootstrap_pnl_p50:  2461.88
bootstrap_pnl_p95:  4203.15
```
A narrow CI (e.g. 1,800–3,000) signals consistent per-trade performance; a wide CI
(e.g. −200–5,000) signals a few large outliers driving results. Could direct the agent to
optimise for `pnl_p5` — the downside-robust outcome — rather than the single-path PnL.

---

### R11: Market regime classification with regime-conditional parameters
**Category:** `harness-structure`
**Priority:** `low`
**Seen in:** `autoresearch/energy-mar21`
**Rationale:** The strategy uses identical parameters regardless of whether the sector was
up 20% or down 15% in the training window. The energy run's stripped-down screener works
in a bull-trending sector; applied to a bear or choppy regime with the same parameters it
would likely produce systematic losses. Regime-conditional behaviour is qualitatively
different from parameter tuning — it's structural adaptation.
**Suggested change:** Add to the mutable section:
```python
REGIME_ADAPTIVE = True   # agent can flip this
RSI_UPPER = {'bull': 85, 'neutral': 75, 'bear': 65}  # example
```
Add a `detect_regime(df)` stub (the agent implements the body). Add a harness-level
requirement to print per-regime trade counts and win rates in `run.log`. The agent then
discovers empirically which parameters are regime-appropriate rather than guessing a single
set for all conditions.

---

## Run Parameter Guidance

Both worktrees identified the same root causes for unreliable parameters. These govern
how much information the optimizer actually has — fixing the harness without addressing
these will still produce overfit results.

### Minimum viable trade count
**Do not run optimization at all if the training window produces fewer than ~50 completed
trades.** Below that threshold all metrics (win rate, Sharpe, PnL) have confidence intervals
too wide to optimize meaningfully. The current runs (17–18 trades) fall well below this
floor. Change tickers or extend the window before running a single experiment.

### Tickers: minimum 80–150, at least 4 sectors
A single-sector universe introduces a hidden macro bet. The energy run found a strategy
that is essentially "long energy in a rising oil cycle" — the optimizer stripped out all
filters because every stock in the sector was moving together. Include at minimum:
- Technology (NVDA, MSFT, AAPL) — high beta, momentum-driven
- Financials (JPM, GS, BAC) — rate-sensitive
- Healthcare (UNH, LLY, ABBV) — defensive, different volatility
- Energy (XOM, COP, CVX) — commodity-driven
- Consumer staples (WMT, PG, KO) — low beta, regime stress test

Include 20–30% high-beta / high-ATR names (TSLA, AMD, MSTR, biotech) — an ATR-based
strategy never tested on high-volatility stocks will have miscalibrated ATR thresholds in
live trading.

### Timeframe: training must contain at least one bear market or crash event
A strategy optimized exclusively in rising markets will fail in corrections. The current
training windows (3 months each, all in early 2026) are single-regime samples.

Three concrete recommended windows:
- **Stress-focused (fastest):** `2019-10-01 → 2021-09-30` — pre-COVID bull, crash,
  V-recovery, melt-up; three regimes in two years; single 18-month train / 6-month test
- **Multi-regime (highest confidence):** `2018-01-01 → 2024-12-31` — two full market
  cycles, walk-forward with 12-month rolling train / 3-month test steps
- **Deployment-relevant (most recent):** `2022-01-01 → 2025-03-31` — 2022 bear, 2023–24
  bull with corrections; 9-month rolling train / 3-month test steps

Minimum test window: **3 months** (gives 10–30 trades to evaluate). The current 14-day
holdout is too short by a factor of 6.

### Iterations: scale with universe size and window length
| Setup | Recommended | Reasoning |
|-------|-------------|-----------|
| 100+ tickers, 3+ year window | 30–40 | Highly informative; each run has 100+ trades |
| 100+ tickers, 1 year window | 50–60 | Moderate; slightly more needed |
| < 50 tickers, any window | **Do not optimize** | Too few trades; all results are noise |
| Walk-forward (3–5 windows) | 20–30 | Computationally expensive per iteration |

More iterations does not compensate for insufficient data. Both runs confirmed: energy
gains plateaued after ~20 iterations; nasdaq100 gains plateaued after ~50 iterations.
Running further just overfit the same thin dataset from more angles.

---

## Priority Order for Implementation

| # | Recommendation | Effort | Impact |
|---|---------------|--------|--------|
| R1 | Fix look-ahead bias | Low | Very high — correctness bug |
| R3 | Risk-proportional sizing | Low | High — removes leverage artefact |
| R4 | Hide test PnL / silent holdout | Low | High — prevents test leakage |
| R5 | Trade-level attribution logging | Low | High — gives agent real signal |
| R2 | Walk-forward CV objective | Medium | Very high — core overfitting fix |
| R7 | Risk-adjusted objective (Calmar) | Low | Medium |
| R8 | Position cap + correlation penalty | Low | Medium |
| R6 | Ticker holdout | Medium | Medium |
| R9 | Adversarial noise / robustness | High | Medium |
| R10 | Bootstrap CI | Medium | Low-medium — diagnostic value |
| R11 | Regime-conditional parameters | High | Medium (requires agent cooperation) |

**Prerequisites before adding more sector runs:** R1 (correctness), R3 (removes sizing
artefact), and R2 (walk-forward CV) are load-bearing. Every new run without these will
repeat the same failure mode: a strategy that exploits one trending window with inflated
per-trade PnL from variable stop distances.

---

## Contradictions

### C1: R4 (simple "hide test PnL") vs R2 (walk-forward CV using test PnL for keep/discard)
**Conflict:** R2 makes test PnL the primary optimization signal — the agent keeps a change
only if min test PnL across rolling windows improves. R4's simple option says to print
`test_total_pnl: HIDDEN` during the loop so the agent cannot see it. These directly
contradict: you cannot use test PnL as the keep/discard criterion and simultaneously hide
it from the agent making that decision.

**Verdict:** R4's simple option is superseded the moment R2 is implemented. Once walk-forward
CV is in place, adopt R4's **stronger option only** (the 3-segment silent holdout:
train / visible-test / silent holdout). The "visible test" windows in the walk-forward model
become the target of R2's optimization, which is correct. The silent holdout beyond those
windows remains unseen until the final run. The simple "HIDDEN" option is only meaningful
as a stopgap *before* R2 is implemented — it should not be carried forward alongside R2.

---

### C2: R2 (min test PnL across windows as primary objective) vs R7 (Calmar ratio as primary objective)
**Conflict:** Both recommend replacing raw `train_total_pnl` as the keep/discard criterion,
but they propose different replacements for the same decision point. R2: keep if min test PnL
across rolling windows improves. R7: keep if Calmar ratio (train PnL / max drawdown) improves.
Implemented naïvely as dual primary objectives they will frequently disagree — a strategy
change that improves min test PnL while reducing Calmar, or improves Calmar while hurting
the worst rolling-window test performance, gets an indeterminate verdict.

**Verdict:** R2 is the primary objective; R7 is a secondary printed diagnostic. Rationale:
R2 addresses the core problem (temporal generalization) directly and uses out-of-sample data.
R7 measures intra-training-window consistency, which is a useful signal but entirely
in-sample — it tells you about training stability, not out-of-sample robustness. The
implementation: keep/discard driven solely by R2's min test PnL; print Calmar each run as
a diagnostic column in `results.tsv`; allow the user (not the agent) to manually inspect
the Calmar trend across iterations and use it to inform parameter tuning discussions.
Calmar could be promoted to a co-criterion in a future iteration once R2 is stable.

---

### C3: R9 (date-shift perturbation: shift BACKTEST_START ±7 days) vs R2 (walk-forward rolling windows)
**Conflict:** R9's date-perturbation component shifts `BACKTEST_START` ±7 days and re-runs
the backtest to measure PnL sensitivity to window choice. R2 already achieves this more
systematically — rolling windows with staggered starts explicitly test how sensitive the
strategy is to which time slice is used for training vs testing. Once R2 is in place,
R9's date shift is testing what R2 already tests, at 5× compute cost per iteration (R9
requires multiple re-runs per experiment). Additionally, the two perturbation regimes
interact: if a strategy passes R2's rolling-window criterion, it has implicitly demonstrated
temporal robustness, making R9's date shift redundant.

**Verdict:** After R2 is implemented, **drop R9's date-shift perturbation entirely** — it
adds compute cost without new information. **Preserve R9's price/stop perturbation** (±0.5%
entry price, ±0.3 ATR stop level across 5 seeds): this tests a genuinely orthogonal
dimension of robustness (execution sensitivity / stop-level fragility) that no other
recommendation covers. The simplified R9 post-R2: run 5 seeds with perturbed prices only,
report `pnl_min` as a fragility diagnostic column, use it as a secondary discard filter
(if `pnl_min < 0` when `train_pnl > 0`, flag as fragile but don't auto-discard).

---

### C4: R5 (trade-level attribution logging) vs R9 (5 perturbed seeds, keep/discard on pnl_min)
**Conflict:** R5 writes `trades.tsv` after each run so the agent can attribute outcomes to
specific tickers, stop types, and holding periods. R9 runs 5 randomly perturbed seeds and
makes the keep/discard decision on `pnl_min` — the worst-case seed. If both are active
simultaneously, `trades.tsv` reflects the *nominal* (unperturbed) run while the PnL that
drove the keep/discard decision came from a *different* (perturbed) run with different fill
prices and potentially different stop-hit timing. The agent reading `trades.tsv` to
understand "why did this iteration get discarded?" is looking at the wrong run — the nominal
trades that looked fine, not the perturbed run that produced the bad PnL.

**Verdict:** When R9 is active, write `trades.tsv` from the **nominal seed only**, but add
a header annotation: `# pnl_min (worst perturbation seed): $X.XX`. This makes clear that
the attribution data is from the nominal run and the keep/discard criterion was the
worst-case perturbed PnL. The agent should be instructed (in `program.md`) that when
`trades.tsv` shows all-positive trades but the run was discarded, the cause is stop/entry
fragility (R9 penalised it), not the individual trade structure visible in `trades.tsv`. As
a practical rule: if a run is discarded purely because `pnl_min < threshold` while nominal
PnL is positive, log the status as `discard-fragile` (not just `discard`) so the agent can
distinguish price-fragility discards from genuine underperformance discards.
