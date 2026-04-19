# Strategy Update Roadmap — SMT + ICT Hypothesis Integration

**Created:** 2026-04-19
**Context:** Written after the session hypothesis system was designed (see `docs/superpowers/specs/2026-04-19-session-hypothesis-design.md` and `.agents/plans/session-hypothesis.md`). Captures everything discussed about future strategy updates before context was lost.

---

## Background

During brainstorm for the session hypothesis feature we researched ICT (Inner Circle Trader) theory in depth. Several ICT concepts emerged that are **not yet in `strategy_smt.py`** but have a plausible edge. Rather than adding them speculatively, we agreed on a disciplined process:

1. Ship the hypothesis system (done — see plan above)
2. Gather data — both from live sessions and from running the deterministic rule engine retrospectively on the backtest corpus
3. Analyze whether hypothesis-aligned signals outperform misaligned ones
4. Only if analysis shows edge: codify the relevant rules as strategy params and optimize

This document is the plan for steps 2–4.

---

## What the Hypothesis System Gives Us

### The `matches_hypothesis` boolean

Every signal dict now carries `matches_hypothesis: True | False | None`. This flows into:

- **Realtime:** `position.json` and the terminal output at signal time
- **Backtest:** a new column in the output TSV — available for every historical signal

The deterministic rule engine (`compute_hypothesis_direction()` in `hypothesis_smt.py`) runs on historical data without any LLM cost, so every fold of the walk-forward backtest will produce this field for every signal.

### What the boolean enables

We can now ask, for every historical session where a signal fired:

- Did the signal direction match the hypothesis direction?
- What was the P&L, win rate, and RR for matching vs. non-matching signals?
- Is there a meaningful performance gap?

If matching signals consistently outperform non-matching ones (across folds, not just in aggregate), that is evidence that the ICT rules have predictive edge on top of the existing SMT signal.

---

## Phase 1 — Data Gathering

### Step 1A: Re-run the full backtest with `matches_hypothesis`

After the hypothesis plan is implemented, run `backtest_smt.py` over the full historical corpus. The output TSV will now include a `matches_hypothesis` column for every trade row.

```bash
uv run python backtest_smt.py
```

No other changes needed — `compute_hypothesis_direction()` is already wired into the backtest.

### Step 1B: Accumulate live session data

Run `signal_smt.py` through real NY sessions. Each session produces `data/sessions/YYYY-MM-DD/hypothesis.json` with:

- The initial hypothesis direction and narrative
- The evidence log (what the market did vs. what was expected)
- Whether any signal fired and whether it matched
- The session summary from the LLM

Aim for **at least 20–30 live sessions** before drawing conclusions from live data. This is a secondary data source — the backtest corpus (500+ sessions) is the primary one.

---

## Phase 2 — Analysis

### Step 2A: Hypothesis alignment analysis (quantitative)

From the backtest TSV, split trades into two groups:

| Group | Definition |
|---|---|
| **Aligned** | `matches_hypothesis == True` |
| **Misaligned** | `matches_hypothesis == False` |
| **No hypothesis** | `matches_hypothesis == None` (insufficient data for that session) |

For each group, compute:
- Trade count and % of total
- Win rate (% profitable)
- Average P&L per trade
- Average RR achieved
- Distribution of exit types (TP / stop / session end)

**Threshold for "meaningful edge":** Aligned win rate exceeds misaligned win rate by ≥10 percentage points AND the difference is consistent across at least 4 of the 6 walk-forward folds. A global aggregate difference that disappears in fold-level analysis is noise.

### Step 2B: Rule-level decomposition

The hypothesis direction is produced by a weighted vote across 5 rules. If the overall alignment shows edge, decompose further:

For each rule independently, ask: when that rule's implied direction matches the signal direction, does performance improve?

| Rule | Metric | Questions |
|---|---|---|
| Rule 1 (PDH/PDL range) | pd_range_bias == signal direction | Which of cases 1.1–1.5 have the strongest predictive edge? Are any cases net negative? |
| Rule 5 day (TDO zone) | day_zone-implied direction == signal direction | Does trading from discount (long) / premium (short) improve results vs. the baseline? |
| Rule 5 week (TWO zone) | week_zone-implied direction == signal direction | Does week-resolution premium/discount add edge beyond day-resolution? |
| Rule 2 (trend) | trend_direction == signal direction | Does multi-day trend alignment add edge? Is it additive to Rule 1? |
| Rule 4 (session extremes) | last_extreme_visited suggests target direction | Does knowing the last visited session extreme help predict today's signal direction? |

This requires logging each rule's output alongside `matches_hypothesis` in the backtest, which means adding a `session_context_summary` column (or writing a separate per-session context TSV) during the backtest run.

### Step 2C: Live session qualitative review

For each live session, read `data/sessions/YYYY-MM-DD/hypothesis.json` and review:

- Did the market behave as the expected scenario described?
- Were the key levels useful? Which ones were hit vs. ignored?
- Did the LLM's narrative correctly identify the dominant ICT structure?
- Were hypothesis revisions justified or premature?

Build a short session log (manual notes or a separate analysis script) noting patterns the quantitative analysis misses.

---

## Phase 3 — Decision Framework

After analysis, there are four possible outcomes:

### Outcome A: No meaningful edge from hypothesis alignment

The aligned/misaligned P&L gap is small (<5pp win rate difference) or inconsistent across folds.

**Action:** Keep the hypothesis system as-is (it has training/contextual value even without statistical edge). Do not add ICT-derived params to `strategy_smt.py`. Revisit after more sessions.

### Outcome B: Significant edge from overall hypothesis alignment but unclear which rules drive it

Aligned signals clearly outperform but decomposition is inconclusive.

**Action:** Add `HYPOTHESIS_FILTER = True` as an optional backtest flag (not the default). When True, only signals where `matches_hypothesis == True` are taken. Walk-forward the filtered strategy to verify the edge survives. If it does, the filter is a candidate for the live strategy.

### Outcome C: Specific rules show strong edge

E.g., Rule 1 case 1.3 + discount vs. TDO strongly predicts signal direction. Rule 2 (trend) adds noise.

**Action:** Codify only the rules that show edge as explicit strategy params (see candidate params below). Optimize those params via the existing walk-forward framework in `backtest_smt.py`. Do NOT add params that showed no edge.

### Outcome D: The ICT structure suggests new signal types beyond hypothesis filtering

E.g., FVGs between price and TDO are consistently visited before/after the SMT signal fires, or the overnight liquidity sweep is a reliable pre-signal pattern.

**Action:** These become new signal features, not just filters. Design separately, require their own backtesting pass before adding to `strategy_smt.py`.

---

## Phase 4 — Candidate Strategy Updates (if analysis supports)

The following are ICT-derived params and features that emerged from the brainstorm. **None of these should be added without phase 2–3 evidence.** They are listed here so the context is not lost.

### 4.1 — TWO (True Week Open) as a reference level

**What:** The open of the first 1m bar on Tuesday of the current week. Analogous to TDO but at weekly scale. Premium (above TWO) = expect short; discount (below TWO) = expect long.

**Current state:** TDO is already used via `TDO_VALIDITY_CHECK` and `compute_tdo()`. TWO is computed in the hypothesis rule engine but not in `strategy_smt.py`.

**Candidate param:** `TWO_VALIDITY_CHECK: bool` — skip signals where the take-profit target direction is inverted relative to TWO (same logic as `TDO_VALIDITY_CHECK` but using TWO). Could be combined with TDO check or used independently.

**Optimization search space:** `[True, False]`

**Prerequisite:** Rule 5 week-zone analysis (Step 2B) shows consistent edge.

### 4.2 — Minimum TWO distance filter

**What:** Analogous to `MIN_TDO_DISTANCE_PTS`. Skip signals where `|entry - TWO|` is below a threshold. Filters out sessions where price is too close to the weekly open to have a clear directional context.

**Candidate param:** `MIN_TWO_DISTANCE_PTS: float`

**Optimization search space:** `[0.0, 10.0, 15.0, 20.0, 25.0]`

**Prerequisite:** TWO_VALIDITY_CHECK shows edge first.

### 4.3 — Previous Day Range case filter

**What:** The hypothesis rule engine assigns one of cases 1.1–1.5 to each session. If certain cases (e.g., 1.3) have much better signal alignment than others (e.g., 1.5), we can skip signals on the low-confidence cases.

**Candidate param:** `ALLOWED_PD_RANGE_CASES: frozenset[str]` — e.g., `frozenset({"1.1", "1.2", "1.3", "1.4"})` to exclude case 1.5.

**Optimization search space:** all subsets of {1.1, 1.2, 1.3, 1.4, 1.5}; use the full set as baseline.

**Prerequisite:** Step 2B rule decomposition shows case-level variance in performance.

### 4.4 — Weekly premium/discount direction filter

**What:** Only take long signals when price is in weekly discount (below TWO); only take short signals when in weekly premium (above TWO). A stronger version of `TDO_VALIDITY_CHECK` at the weekly scale.

**Candidate param:** `WEEK_ZONE_DIRECTION_FILTER: bool`

**Optimization search space:** `[True, False]`

**Note:** This overlaps with `TWO_VALIDITY_CHECK` above — evaluate them together.

### 4.5 — Overnight liquidity sweep gate

**What:** ICT theory predicts that the 9:30 open often first sweeps the overnight high or low (a "liquidity grab") before the real directional move begins. If this pattern is consistent, we could require the sweep to have occurred before accepting an entry signal — i.e., add a check that price has already touched the overnight high or low before the signal fires.

**Candidate param:** `REQUIRE_OVERNIGHT_SWEEP: bool` — if True, `screen_session()` skips signals that fire before price has touched within N points of `overnight_high` or `overnight_low`.

**Optimization search space:** `[True, False]` + `OVERNIGHT_SWEEP_TOUCH_PTS: [5.0, 10.0, 15.0]`

**Note:** This is a new concept for the strategy and requires careful backtesting. Could filter out many signals; verify trade count does not drop too far.

**Prerequisite:** Step 2C (live session qualitative review) should confirm this pattern is visible before adding it to the signal logic.

### 4.6 — FVG confluence requirement

**What:** FVGs (Fair Value Gaps — three-bar imbalances) between the 9:00am price and TDO are expected fill targets. If the signal fires but there is an unfilled FVG between entry and take-profit, that FVG may act as resistance/support and reduce the probability of hitting TDO.

**Candidate feature:** FVG detection in `screen_session()` — if a bearish FVG exists between entry and TP on a long signal (or vice versa), reduce confidence or skip the signal.

**Complexity:** Medium — requires three-bar pattern scan inside `screen_session()`.

**Prerequisite:** Outcome D from Phase 3, plus dedicated backtesting as a new signal feature.

### 4.7 — IPDA 20/40/60 day cycle awareness

**What:** ICT's IPDA framework describes 20, 40, and 60 trading day cycles in which price targets a key high or low. If we are at day 19–21 of a cycle (approaching a cycle turning point), a reversal is more probable. This could be a gate (trade only on cycle-turn days) or a bias (increase confidence when aligned with cycle direction).

**Complexity:** High — requires cycle detection logic that is not trivial to implement robustly.

**Priority:** Low. The other candidates above are more directly testable. Revisit only if Rules 1–5 analysis is exhausted.

---

## Phase 5 — Optimization Process (if params are added)

For any params selected from Phase 4:

### Step 5A: Add param to `strategy_smt.py`

Follow the existing pattern:
- Add constant at the top with a comment describing the optimizer search space
- Add the filtering/gating logic in the appropriate function (`screen_session`, `manage_position`, or the blackout check)
- Keep the default value as the current behavior (i.e., the param disabled = baseline)

### Step 5B: Add to optimizer search space

In whichever optimization harness is used (currently `train.py` or similar), add the new param to the grid/search space. Use the search spaces defined above.

### Step 5C: Walk-forward validation

Run the full walk-forward backtest with the new param. Compare:
- Mean test P&L per fold (primary metric — must not regress vs. baseline)
- Min test P&L per fold (must not introduce a bad fold)
- Total trade count (must not drop below a threshold that makes the result statistically meaningless — suggest ≥50 test trades total)
- Win rate and average RR

**Accept the param only if** the optimized value outperforms the baseline (param disabled) on the test folds, not just on the training folds. Walk-forward design already handles this — be strict about not cherry-picking.

### Step 5D: Update `signal_smt.md` and `program_smt.md`

Document the new param, its ICT rationale, and the evidence that justified adding it.

---

## Sequencing Summary

```
[NOW]
  → Implement session hypothesis system (.agents/plans/session-hypothesis.md)

[AFTER IMPLEMENTATION — ~1 week of sessions]
  → Re-run backtest_smt.py → get matches_hypothesis in TSV for all historical signals
  → Accumulate 20–30 live sessions → read hypothesis JSON files

[~1 MONTH AFTER IMPLEMENTATION]
  → Phase 2: Run alignment analysis (Step 2A–2C)
  → Phase 3: Apply decision framework (Outcome A/B/C/D)

[IF OUTCOME B OR C]
  → Phase 4: Select candidate params (only those with evidence)
  → Phase 5: Add to strategy_smt.py → optimize → walk-forward validate

[IF OUTCOME D]
  → Design new signal features separately (requires own spec + plan)
```

---

## What NOT to Do

- Do not add ICT params speculatively without analysis evidence — the existing strategy has a walk-forward validated edge; adding unvalidated params risks breaking it
- Do not add `HYPOTHESIS_FILTER = True` as the live default without walk-forward validation — it will reduce trade count; verify the edge survives the reduced sample
- Do not optimize on the full corpus without walk-forward — overfitting risk is high with many new params
- Do not skip the per-rule decomposition (Step 2B) — the overall `matches_hypothesis` aggregate may be driven by one or two rules, and adding all five rules as strategy params would overfit to whichever rules happen to have been correct over the test period
