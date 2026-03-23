# Merged Harness Upgrade Recommendations

**Generated:** 2026-03-23 22:49
**Sources:** `autoresearch/multisector-mar23`
**Total worktrees with updates:** 1

---

## Recommendations

### R1: FOLD_TEST_DAYS is too short for this strategy's hold duration
**Category:** `harness-split`
**Priority:** `high`
**Seen in:** `autoresearch/multisector-mar23`
**Rationale:** Many winning positions were held 30–98 days (APP: 49d, 28d; COF: 89d; HD: 93d; NEM: 88d). A 20-business-day test window (~4 calendar weeks) force-closes most test trades mid-hold, measuring only the first ~20 days of a 60-day setup. Fold4 and fold7's test windows recorded 0–2 trades, making min_test_pnl nearly meaningless for those folds. The metric is measuring "can this strategy enter clean breakouts" rather than "does the strategy capture its intended profit structure."
**Suggested change:** Set `FOLD_TEST_DAYS = 40` (or 60) in the harness constants. This keeps 9 folds but each test window captures a fuller position lifecycle. Reduce `WALK_FORWARD_WINDOWS` to 7–8 if the total date range forces it.

---

### R2: min_test_pnl is noise-dominated when test trade count < 5 per fold
**Category:** `harness-objective`
**Priority:** `high`
**Seen in:** `autoresearch/multisector-mar23`
**Rationale:** The optimization repeatedly stalled because a single trade's outcome set the min_test_pnl floor. fold5's −50.00 was exactly 1R (one UNH trade hitting its initial stop) and proved immovable across iterations 27–30 regardless of screener changes. With 0–5 trades per test fold, the minimum is determined by individual trade luck, not strategy quality. Over 12 iterations were spent trying to screen out this single trade rather than improving the overall system.
**Suggested change:** Add a `min_test_trades` guard: only include a fold in the min_test_pnl calculation if the fold had ≥3 test-window trades. Alternatively, switch from `min_test_pnl` to `median_test_pnl` or `mean_test_pnl` as the primary keep/discard criterion, with `min_test_pnl` as a secondary guard (e.g., ≥ −2R per fold).

---

### R3: Auto-calibrate the consistency floor — the hardcoded −$100 caused 6+ wasted iterations
**Category:** `harness-objective`
**Priority:** `high`
**Seen in:** `autoresearch/multisector-mar23`
**Rationale:** The `train_pnl_consistency` floor of `−RISK_PER_TRADE × 2 = −$100` was designed for a small-universe setup but applied unchanged to the 85-ticker run. Iter19 (vol 1.9×, a genuine improvement in min_test from −83 to −56) was tagged `discard-inconsistent` and required manual rollback, re-evaluation, and floor correction. The correct calibrated floor for this universe is approximately `−RISK_PER_TRADE × MAX_SIMULTANEOUS_POSITIONS × 10 = −$2,500`. The miscalibration caused confusion across at least 6 iterations.
**Suggested change:** In `program.md`, replace the hardcoded `−RISK_PER_TRADE × 2` formula with `−RISK_PER_TRADE × MAX_SIMULTANEOUS_POSITIONS × 10`. Add a note that this scales with `MAX_SIMULTANEOUS_POSITIONS` for larger universes.

---

### R8: Add an earnings-proximity filter to screen_day()
**Category:** `harness-structure`
**Priority:** `high`
**Seen in:** `autoresearch/multisector-mar23`
**Rationale:** Post-hoc analysis of all 10 losing trades revealed that 6 entered on or within 14 days of the stock's next earnings release: SCHW (day-of, −$50), ORCL (day-of, −$50), RTX (day-of, −$50), ISRG (6 days out, −$50), UNH Apr 8 (9 days out, −$50), UNH Oct 28 (day-of, −$50). That is −$300 of the −$493.53 total loss attributable to earnings-event entries. Removing the fold5 UNH Oct 28 entry alone would lift min_test_pnl from −$50.00 to −$43.33. One counterexample (GS Oct 15, 2024 earnings day, +$18.97) shows the filter removes some winners too, but the loss-side skew is 6:1.
**Suggested change:** In `prepare.py`, augment each ticker's parquet file with a `next_earnings_date` column using `yf.Ticker(ticker).earnings_dates`. In `screen_day()`, add a guard: `if next_earnings_date is not None and 0 <= (next_earnings_date − today).days <= 14: return None`. Also consider excluding entries within 2 trading days after an earnings release to avoid chasing post-earnings gap-up moves that often fade.

---

### R4: train_total_pnl is too unstable as a training metric
**Category:** `harness-objective`
**Priority:** `medium`
**Seen in:** `autoresearch/multisector-mar23`
**Rationale:** Training P&L varied from −$35.48 (iter3) to +$408.17 (iter8) non-monotonically across kept iterations. Iter27 (kept, min_test improved) had train_pnl=$97.84, while iter28 (discarded, same min_test) had train_pnl=$212.24 — better train but same test outcome. The metric also scales with trade count: a filter that removes many trades lowers train_pnl even when the remaining trades are higher quality.
**Suggested change:** Add `train_avg_pnl_per_trade` and `train_win_rate` to the primary diagnostic output. Consider replacing or supplementing `train_total_pnl` in results.tsv with `train_avg_pnl_per_trade × 100` to make it comparable across runs with different trade counts.

---

### R5: Deadlock detection — the worst fold dominated for 12+ consecutive iterations
**Category:** `harness-structure`
**Priority:** `medium`
**Seen in:** `autoresearch/multisector-mar23`
**Rationale:** From iter3–iter19, fold5 was locked at exactly −83.09 for 17 iterations (vol changes, RSI changes, breakout changes — nothing moved it). After iter19 it moved to −50.00 but locked again for iterations 27–30. Both deadlocks came from a single stop-hit trade in one fold with very few trades. 17 iterations of deadlock represents a large fraction of the 30-iteration budget consumed on an unscreenable single event (PEP Sep 2025, UNH Oct 2025).
**Suggested change:** In `program.md` loop instructions: if `min_test_pnl` has not changed for 4 consecutive iterations, pivot to optimizing `second_worst_fold_pnl` (or `mean_test_pnl`) for the next 3 iterations before returning to `min_test_pnl`. This prevents burning the full budget on an unimprovable single-trade outcome.

---

### R6: Position management changes were discovered late and combined late
**Category:** `params-iterations`
**Priority:** `medium`
**Seen in:** `autoresearch/multisector-mar23`
**Rationale:** Iter27 (trail trigger 2.0 ATR) and iter28 (prev-day close>open) both showed significant fold-distribution improvements — fold2 from −43 to −7, fold3 from −56 to +1, fold8 from −5 to +39 — but neither was tested until iteration 27–28 out of 30. The iteration budget was spent largely on screener entry conditions (SMA variants, RSI ranges, vol thresholds) before trying position management changes, which tend to affect all folds simultaneously.
**Suggested change:** In the `program.md` loop instructions, explicitly suggest testing position management changes (trailing stop activation threshold, stop distance, breakeven trigger) in iterations 6–10, not only after screener ideas are exhausted.

---

### R7: Single-stock catastrophic risk is not guarded at the screener level
**Category:** `harness-structure`
**Priority:** `low`
**Seen in:** `autoresearch/multisector-mar23`
**Rationale:** UNH lost 35% from entry in 6 days (Oct 28 to Nov 3, 2025). SCHW, ORCL, RTX, APP, ISRG also produced full −1R stops. 10 of 43 total trades (train+test) resulted in full −1R stops — these are not "momentum faded" losses but single-stock events. No screener condition can predict these; they require portfolio-level guards.
**Suggested change:** Consider a sector-concentration guard (no more than 2 simultaneous positions in the same sector) and/or tightening `MAX_SIMULTANEOUS_POSITIONS` from 5 to 3 to reduce drawdown in stop-out clusters.

---

### R9: Reject fallback-stop entries in the screener — 25% win rate vs 79% for pivot stops
**Category:** `harness-structure`
**Priority:** `high`
**Seen in:** `autoresearch/multisector-mar23`
**Rationale:** Pivot-stop trades (39 of 43): 79% win rate, +$233 net. Fallback-stop trades (4 of 43): 25% win rate, −$135 net. The fallback stop fires when `find_stop_price()` finds no structural pivot, defaulting to `entry − 2.0×ATR`. The absence of structural support is already computed inside `screen_day()` before returning — it is observable before entry. Three of four fallback trades (SCHW, RTX, PEP) were full −$50 stop hits; only META (+$14.53) was a winner. Eliminating fallback entries saves $135 in losses at the cost of $14.53 in foregone wins — a net +$120 improvement — and also removes PEP (Sep 2, 2025 fallback, −$50) from fold3's test window, directly lifting a second deadlocked fold.
**Suggested change:** In `screen_day()`, after `stop = find_stop_price(...)`, change the fallback branch from assigning `entry − 2.0×ATR` to `return None`. One line change.

---

### R10: Add a time-based capital-efficiency exit — four trades held 88–98 days for under $13 each
**Category:** `harness-structure`
**Priority:** `medium`
**Seen in:** `autoresearch/multisector-mar23`
**Rationale:** Four trades locked position slots for ~3 months producing negligible returns: COF (89d, +$6.80, $0.08/day), HD May (93d, +$11.38, $0.12/day), AMZN Jul (98d, +$12.99, $0.13/day), NEM (88d, +$11.59, $0.13/day). Together they consumed 368 position-days — equivalent to over a year of one concurrent slot — and earned $42.76. The 15–30 day cohort is the strategy's value zone: 11 of 12 trades profitable, +$266 net, 0 losses.
**Suggested change:** In `manage_position()`, add a time-based exit: if position held > 30 days AND current P&L < 0.3×RISK_PER_TRADE ($15), return stop at current price_10am to force exit. Test with 20d/$10 and 30d/$15 thresholds — goal is to catch COF/AMZN/NEM without cutting APP (49d, +$81) or PLTR (72d, +$40).

---

### R11: Track win/loss dollar ratio in results.tsv — the 0.43× ratio is a fragility warning
**Category:** `harness-objective`
**Priority:** `medium`
**Seen in:** `autoresearch/multisector-mar23`
**Rationale:** Average win ($21.12) is only 0.43× average loss ($49.35). At this ratio, profitability requires maintaining >70% win rate — leaving almost no margin for regime shifts. This is not currently tracked, so iterations that improve min_test_pnl while silently eroding the win/loss ratio would be accepted. The underlying cause is that most losses are full −1R ($50) while most wins are partial exits via trailing stop capturing only 0.3–0.8R.
**Suggested change:** Add `avg_win_loss_ratio` (avg winning PnL ÷ avg losing PnL) as a column in results.tsv. Set a soft floor of ≥0.5× in the keep/discard criteria. Also consider tighter trailing stop distance (1.0–1.2 ATR instead of 1.5) to capture more of strong runs.

---

### R12: Add a "recent loser cooldown" screener rule — repeat entries in broken stocks lose twice
**Category:** `harness-structure`
**Priority:** `medium`
**Seen in:** `autoresearch/multisector-mar23`
**Rationale:** UNH entered twice, lost both (−$100 total). RTX entered three times (loss, breakeven, small win). HD entered twice (win, then −$50). FCX entered twice (breakeven, then −$43). In each case, the second entry followed a stop-out or period of underperformance and the "breakout" was a dead-cat bounce on a structurally impaired stock. UNH was publicly a broken company throughout 2025 (CEO murder, Medicare margin collapse, −44% YTD) yet the screener had no memory of the prior loss.
**Suggested change:** In `screen_day()`, accept a `recently_stopped` set — tickers that hit a full −1R stop within the last 90 calendar days. If `ticker in recently_stopped`, return None. The set is built by the backtest loop from running trade history and threaded into the screener. Try 60, 90, 120 day lookbacks.

---

## Contradictions

### C1: R9 vs R12 — both target the same losses but via incompatible mechanisms for overlapping trades

**Conflict:** R9 rejects all fallback-stop entries (removes SCHW, RTX Jan, PEP). R12 rejects re-entries in recently stopped tickers (removes UNH Oct, HD Oct, FCX Jan, RTX second/third entries). Three trades appear in both lists: RTX Jan 2025 would be blocked by R9 (fallback stop) *and* would also be blocked by R12 if a prior RTX stop had occurred. Implementing both simultaneously makes it impossible to isolate which rule drove a particular outcome change during optimization.

**Verdict:** No true incompatibility — both can coexist. Apply R9 first (one-line, high-certainty gain). Implement R12 in a subsequent iteration and measure the incremental effect independently. Do not combine them in a single commit.

No other contradictions found among the 12 recommendations.
