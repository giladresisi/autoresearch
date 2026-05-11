# SMT V2 Strategy Update Methodology

**Created:** 2026-05-09  
**Purpose:** Systematic process for evidence-driven, session-grounded micro-updates to `strategy_smt.py`, `trend.py`, `hypothesis.py`, and/or `daily.py`. Distinct from the walk-forward optimization loop (`program_smt.md`), which searches the parameter space broadly. This methodology targets a specific observed failure, formulates a minimal code change, and validates it through progressively wider regression scopes before committing.

---

## Why This Exists (and Why It's Separate from program_smt.md)

The walk-forward optimizer (`program_smt.md`) maximizes `mean_test_pnl` across a large historical corpus by exploring parameter combinations. It is excellent for finding broadly applicable improvements but cannot easily respond to a specific, recent failure pattern — especially one visible only in a single session's event stream.

This methodology starts from the opposite direction: a concrete, observed underperformance in a known session. That grounding prevents the two main failure modes of the walk-forward approach:

1. Improvements that are real on the full corpus but fail to address the most recent, visible problems.
2. Parameter changes motivated by hypothesis alone rather than observed evidence.

The tradeoff is that session-grounded changes carry their own overfitting risk (to the seed session and to the recent 30-day window). The design below manages this explicitly.

---

## Process at a Glance

```
Phase 0: Pre-flight checks (baseline sync, lock recent sessions)
Phase 1: Session, trade, and opportunity selection (user-guided or auto)
Phase 2: Hypothesis formulation (words first, code second)
Phase 3: Single-session implementation loop
Phase 4: 30-day regression assessment
Phase 5: Iterative refinement (up to 5 iterations)
Phase 6: Recent 60-day fold gate
Phase 7: Document and commit (always — success and failure alike)
```

---

## Phase 0: Pre-Flight Checks

### 0.1 — Lock baselines for the last 60 trading days

Before touching any code, ensure every session in the last 60 trading days has a locked regression baseline that reflects the current committed strategy. Run:

```
uv run python regression.py --dates YYYY-MM-DD:YYYY-MM-DD --skip-lock
```

Any session that returns `SKIP` has no baseline yet. For those sessions, lock them now (still with the current committed code, no changes):

```
uv run python regression.py --dates <skip-dates> --update-baseline
```

**Why this must happen before any code change:** The Phase 6 gate compares the changed strategy's P&L against the locked baseline P&L for each of the last 60 sessions. If a session's baseline was locked after the code change was applied, the comparison is invalid — you'd be comparing the change against itself. Locking baselines is a pre-condition, not an afterthought.

### 0.2 — Verify baseline synchronization for the seed session

Once a seed session is identified (Phase 1), run regression on it with the current committed code and confirm it returns `PASS`:

```
uv run python regression.py --dates YYYY-MM-DD
```

Expected output: `events=PASS trades=PASS`.

**Why:** If this check fails, the seed session's baseline is stale — it was locked against an older version of the strategy. Working against a stale baseline means all diff output is meaningless (changes attributed to your new edit may be residue from an earlier committed change). Re-lock the baseline first, confirm the locked output matches your expectation, then proceed.

---

## Phase 1: Session, Trade, and Opportunity Selection

The process needs three things to start: a **seed session**, a **target trade** within that session, and an **opportunity description** (what change might improve that trade). The user may provide any combination of these in natural language. Whatever the user does not specify is auto-selected by the process.

### 1.1 — User input

The user may describe, in plain text, any or all of:

- **Which session** they want to analyze (e.g., "the session from last Tuesday", "2026-05-06")
- **Which trade** in that session (e.g., "the first trade", "the 09:52 entry that stopped out")
- **What the opportunity is** (e.g., "I think if we pushed the stop further it wouldn't have been knocked out by the initial move")

Accept whatever the user provides and treat it as fixed. Auto-select only what is missing.

### 1.2 — Auto-selection: session

If the user did not specify a session, analyze the **last 5 trading days** as candidates. For each:

1. Run the backtest for that day (`run_backtest_v2`) and read the resulting `trades.tsv`.
2. Identify the worst-outcome trade in that session: the trade with the largest absolute loss, or a trade that stopped out before a large subsequent move in the signal direction.
3. Assess whether a **small, concise code change** could plausibly have improved that trade's outcome. Prefer sessions where the cause of the bad outcome is a single, clearly identifiable condition (one threshold, one gate, one parameter) rather than a complex interaction of multiple conditions.

Select the session with the highest-confidence, highest-value opportunity. When multiple sessions look similar, prefer recency.

**Why "small and concise" is a selection criterion:** The entire methodology depends on being able to attribute changes in the 30-day regression to the code change. If the change touches multiple code paths or introduces compound conditions, trades can change for multiple interacting reasons — making the reason table in Phase 5 noisy and the proposed adjustments unreliable. A change that touches one mechanism produces clean attribution. If no session in the last 5 days offers a single-mechanism improvement opportunity, expand the search to 10 days.

### 1.3 — Auto-selection: trade

If the user did not specify a trade, select the trade within the seed session that represents the clearest recoverable loss:

- Highest absolute loss, OR
- Stopped out before a move of ≥ 20 pts in the signal direction

Read both `trades.tsv` and `events.jsonl` for the session to confirm the cause is visible in the event stream, not just in the P&L.

### 1.4 — Auto-selection: opportunity

If the user did not specify an opportunity, read the events.jsonl for the target trade and identify the specific event where the outcome diverged from the optimal path (e.g., `STOPPED_OUT` at a level that price immediately reversed from). Formulate in plain text what single mechanical change would have produced a better outcome.

### 1.5 — Communicate selections to the user

**Before proceeding to Phase 2**, present a summary of all selections to the user, whether auto-selected or user-specified:

```
Session selected: YYYY-MM-DD
  Reason: [why this session — e.g., "the 09:52 short was stopped out 1.5 pts before a 35pt reversal,
           the clearest single-mechanism failure in the last 5 sessions"]

Target trade: HH:MM entry, [direction], exited at HH:MM via [exit reason], P&L: $X
  Reason: [why this trade — e.g., "largest recoverable loss in the session; exit was 1.5 pts inside
           a level that held as support for the next 40 minutes"]

Opportunity: [plain-text description of proposed change]
  Reason: [why this change — e.g., "the STOP_PLACED event shows stop was set at 09:52:03 at price X;
           price reached X+1.5 at 09:53:15 before reversing; a 3pt wider stop would have survived
           the sweep with no other baseline trades affected in this session"]
```

Wait for the user to confirm or redirect before proceeding.

---

## Phase 2: Hypothesis Formulation

**Before writing any code**, document in plain text:

1. **The observed failure**: what happened in the seed session and why it was suboptimal. Name the trade by entry time, the exit reason, and the P&L outcome.
2. **The proposed mechanism**: what one mechanical change would have produced a different outcome on this specific trade. Be specific about the code location and what changes (e.g., "increase `INITIAL_STOP_BUFFER_PTS` from 0 to 3 in `strategy_smt.py`").
3. **Scope conditions**: under what circumstances should the change apply? If the change is unconditional, say so explicitly and explain why unconditional is correct. If it should be gated, define the gate.
4. **The risk of being wrong**: what would happen in sessions where this change makes things worse? Being explicit about the failure mode before implementing makes the Phase 5 reason table easier to write — you'll often find that the actual failures match the risk you predicted here.

**Why words-before-code:** Writing the hypothesis in natural language forces a coherent causal story before touching code. This document also becomes the opening section of the Phase 7 update record (whether success or failure), so the work is never wasted.

---

## Phase 3: Single-Session Implementation Loop

### 3.1 — Implement the change (keep unstaged)

Make the minimal code change that implements the hypothesis. The change may span `strategy_smt.py`, `trend.py`, `hypothesis.py`, and/or `daily.py` — but it should touch as few files and as few lines as possible. Do not commit. Do not add unrelated changes.

**Why minimal:** A change that touches three subsystems cannot be attributed cleanly during reason analysis in Phase 5. If the hypothesis requires touching multiple subsystems, reconsider whether you have one hypothesis or two. A two-hypothesis update should be run as two separate process instances, not one.

### 3.2 — Run seed-session regression

```
uv run python regression.py --dates YYYY-MM-DD
```

The output will show `events=FAIL trades=FAIL` because the code change altered behavior. That is expected.

### 3.3 — Verify the target trade

Read `data/regression/YYYY-MM-DD/trades.tsv` and diff it against `data/regression/YYYY-MM-DD/baseline_trades.tsv`. A trade is considered **changed** if its start time or end time differs from the corresponding trade in the baseline, or if it is new/absent. Confirm that the specific target trade (identified in Phase 2 by entry time) has changed in the way you predicted.

If it has not changed: your code change did not affect the code path that governed this trade. Investigate and adjust. Do not proceed to Phase 4 until this check passes — a mismatch here means the hypothesis was mechanistically wrong, not just the parameter value.

**Why this check is non-negotiable:** A code change can improve session P&L for the wrong reason (e.g., it inadvertently blocks a different losing trade while leaving the target unchanged). Verifying the specific target trade proves the mechanism is correct. Without this, you are correlating rather than causing.

### 3.4 — Check seed-session total P&L (Gate 1)

Sum the P&L of all trades in `trades.tsv` and compare to the baseline total.

**Gate 1: session P&L delta > $0 → proceed to Phase 4.**

If delta ≤ $0: the change helped the target trade but hurt other trades in the same session enough to net negative. An agent should analyze the trade-level changes within this session (using the same time-based diff from step 3.3) to understand which trades got worse and why, then propose an adjustment to the code change. The adjustment does not need to be a filter or constraint — it could be a different parameter value, a tighter threshold, a different condition entirely — but it must be **small and simple** (one additional condition, one changed constant). Implement the adjustment and return to step 3.2.

Allow up to **3 adjustment attempts** on the seed session. If Gate 1 has not been passed after 3 attempts, the hypothesis is too narrow or too disruptive for this seed session. Stop, document (Phase 7), and select a different seed session.

**Why $0 and not a higher bar:** The seed session is one day — a $0 gate merely confirms the mechanism doesn't immediately self-destruct. The real profitability test is the 30-day regression. A higher gate here would be statistically premature.

---

## Phase 4: 30-Day Regression Assessment

### 4.1 — Define the regression window

Run regression over the 30 most recent trading days for which baselines exist:

```
uv run python regression.py --dates YYYY-MM-DD:YYYY-MM-DD
```

30 trading days (~6 calendar weeks) is long enough to capture varied conditions while remaining recency-relevant. Less than 20 days produces high-variance conclusions; more than 60 starts to overlap with the Phase 6 window.

### 4.2 — Produce the trade-level diff

For each session in the regression window, diff `trades.tsv` against `baseline_trades.tsv` at the trade level. A trade is **changed** if its start time or end time differs from the corresponding trade in the baseline. New trades (present in current, absent in baseline) and removed trades (absent in current, present in baseline) are also changed.

For each changed trade, record:

```
trade_key:            <date>_<entry_time>
status:               new | removed | modified
baseline_start_time:  <HH:MM:SS | null>
baseline_end_time:    <HH:MM:SS | null>
current_start_time:   <HH:MM:SS | null>
current_end_time:     <HH:MM:SS | null>
baseline_pnl:         <float | null>
current_pnl:          <float | null>
pnl_delta:            <float>              # positive = improvement
```

Sum `pnl_delta` across all sessions and all changed trades to get `total_30d_pnl_delta`.

**Why time-based matching:** Entry and exit times are the natural identity of a trade — if a trade enters at the same time and exits at the same time, it is the same trade regardless of minor P&L differences (which shouldn't exist if times match). If either time shifts, the execution path changed: either a different entry was taken, or the trade was held to a different exit point. This produces unambiguous change detection without requiring a unique trade ID that the strategy may not emit.

### 4.3 — Evaluate the 30-day gate (Gate 2)

**Gate 2: `total_30d_pnl_delta > $0` → proceed to Phase 6.**

If `total_30d_pnl_delta ≤ $0`: enter Phase 5 (iterative refinement).

**Why $0:** A net-positive delta across 30 days is the minimum bar that justifies the change going further. If the aggregate is negative at this stage, no amount of rationalization changes the fact that the change is currently a net liability.

**Note:** There is no trade count guard at this gate. A change that reduces trade count while improving per-trade P&L is acceptable — the strategy's goal is P&L, not trade volume.

---

## Phase 5: Iterative Refinement

Perform at most **5 iterations** of the following loop. If Gate 2 has not been passed after 5 iterations, go to Phase 7.

**Why 5:** Each iteration adds a new constraint or variant of the code change. Beyond 5 iterations the accumulated conditions become complex, hard to reason about, and increasingly overfit to the 30-day window. If 5 targeted adjustments cannot produce a net-positive result, the hypothesis is likely wrong or the applicable condition too rare to justify a strategy-level change.

### 5.1 — Build the initial reason table (per changed trade)

For each changed trade in the 30-day diff, determine the mechanistic reason its outcome changed. For each changed trade, record a detailed entry:

```
trade_key:        <date>_<entry_time>
pnl_delta:        <float>
reason_title:     <brief label, e.g., "stop too wide, hit on reversal">
context:          <detailed description including:
                    - The bar data around the divergence point (open/high/low/close of
                      the 1 or 2 bars where the path diverged, relative to key levels)
                    - The strategy state machine at that moment (what condition was
                      being evaluated, what values it held)
                    - The specific event that first diverged between baseline and current
                      events.jsonl, and what value changed
                    - Any market structure visible in the bar data that the events.jsonl
                      did not capture (e.g., a wick sweep of overnight high, a bar body
                      that crossed a level events.jsonl didn't mention)>
```

**Why include bar data, not just events.jsonl:** The events.jsonl records what the strategy decided, not the full market picture it decided within. An agent reading only events may miss context that explains why the code change produced a given outcome — e.g., that a stop was hit on a bar that had a 0.5pt body but a 4pt wick (suggesting the stop was at the wick extreme, which is rare and not representative). Bars data grounds the reason in market reality, not just strategy logic.

To read bars data for a specific session, load the 1-minute MNQ data for that date from the futures cache and inspect the bars around the divergence timestamp.

### 5.2 — Deduplicate into the reason summary table

With the per-trade reason table complete, analyze all entries and group trades that share the same **general root cause** — not just the same reason title, but the same underlying mechanism. A good grouping asks: "if I made one adjustment to the code, would it address all the trades in this group simultaneously?"

For each group, write:

```
reason:              <generalized root cause, written as a finding, e.g.:
                       "Stop too close — 10/13 stopped-out trades would have continued
                        past the stop-out point if stop had been pushed back ≥ 5pts">
trade_count:         <int>
total_pnl_delta:     <float>   # negative = group collectively got worse
sessions_affected:   [<list of dates>]
representative_example: <date + entry_time of the clearest individual case>
specific_trades_summary: <brief description of what the individual trades had in common
                           w.r.t. the root cause, e.g.:
                           "All 10 stopped-out trades had a wick extend 1–3pts beyond
                            the stop level before reversing; 3 trades stopped on bars
                            with body < 1pt (pure wick sweeps); the largest loss was
                            $180 on 2026-04-22 09:52 entry">
proposed_adjustment: <plain-text description of a code change that addresses this group>
```

Sort by `total_pnl_delta` ascending (most negative group first).

**Why generalize to root cause rather than grouping by title:** Two trades may have the same title ("stop too close") but different root causes — one stopped on a wick sweep, another stopped during a genuine trend continuation. A proposed adjustment that addresses wick sweeps (e.g., a wick-to-body ratio gate) would not help the trend continuation case. Grouping by root cause ensures the proposed adjustment is mechanistically coherent across all trades in the group.

### 5.3 — Pre-validate the top proposed adjustment

Before implementing the adjustment for the most negative group, verify it would change the outcome for the `representative_example`:

1. Implement the proposed adjustment (still unstaged, layered on top of the current change).
2. Run regression on the `representative_example` date only.
3. Confirm the representative trade's start or end time, or P&L, changed relative to the pre-adjustment run.

If unchanged: the adjustment is mechanistically wrong — it didn't reach the code path it was meant to affect. Revise and repeat. Do not run the full 30-day regression until this single-session check passes.

**Why pre-validate on one session:** A full 30-day regression is expensive. A single-session run is cheap. This step prevents implementing adjustments that are logically plausible but empirically inactive — e.g., a condition that only fires at a code point the strategy never reaches during these trades.

### 5.4 — Run full 30-day regression after each adjustment

After the pre-validation passes, run the full 30-day regression and recompute `total_30d_pnl_delta`.

**Always run the full 30-day window, not just the sessions in `sessions_affected` for the reason being fixed.** Fixing a negative group can degrade sessions that were previously neutral (because the adjustment changes a code path those sessions also traverse). Running only the targeted sessions would miss this.

If `total_30d_pnl_delta > $0`: Gate 2 is now passed. Exit Phase 5 and proceed to Phase 6.

If still `≤ $0`: rebuild the reason table from the current diff (the adjustment has changed the distribution of negative reasons), identify the new top reason, and repeat from step 5.3.

### 5.5 — Iteration state tracking

Record at each iteration:

```
Iteration N:
  change_description:      <what was added or modified in this iteration>
  reason_addressed:        <reason group label>
  trades_in_group:         <count>
  pnl_before_adjustment:   <total_30d_pnl_delta entering this iteration>
  pnl_after_adjustment:    <total_30d_pnl_delta after this iteration>
  delta_from_adjustment:   <improvement or regression from this specific adjustment>
  outcome:                 continued | passed_gate | exhausted
```

This becomes the iteration history table in the Phase 7 document.

---

## Phase 6: Recent 60-Day Fold Gate

Once Gate 2 is passed, run a single-fold validation over the **most recent 60 trading days**.

Compute:
- `baseline_60d_pnl` = sum of `baseline_pnl` across all sessions in the last 60 trading days (from the locked `baseline_trades.tsv` files locked in Phase 0)
- `current_60d_pnl` = sum of `current_pnl` across those same sessions from the current run

**Gate 3: `current_60d_pnl > baseline_60d_pnl` → proceed to Phase 7 (commit).**

If `current_60d_pnl ≤ baseline_60d_pnl`: do not commit. Proceed to Phase 7 (document failure).

There is no trade count check at this gate.

**Why recent 60 days rather than the full 6-fold walk-forward:** The full walk-forward spans 2.5 years and evaluates performance across historical market regimes, some of which may be structurally different from today's. A micro-update motivated by a recent session failure should be judged on whether it improves recent performance — the market regime that produced the failure is by definition the current one. Using the full historical corpus as the final gate would be too conservative: a change that correctly adapts to today's regime might look neutral on a 2.5-year corpus that includes periods where today's specific pattern simply didn't occur. The 30-day window (Phase 4) guards against overfitting to a very short sample; the 60-day window (Phase 6) is a wider, more robust version of the same test. Together they establish that the improvement holds across approximately 3 months of recent trading.

**Why this is not just a larger version of Phase 4:** Phase 4 uses 30 days to make a fast go/no-go decision that determines whether refinement is needed. Phase 6 uses the full 60 days as the authoritative final check. The 30-day window is intentionally narrower so that Phase 5 refinement iterations run faster; Phase 6 then validates the final result on the broader recent window.

---

## Phase 7: Document and Commit

**Always write a document and an INDEX entry — for both success and failure.** Future agents benefit from knowing what was tried and why, regardless of outcome.

### 7.1 — Write the update document

Write to `.agents/strategy-updates/YYYY-MM-DD-<short-description>.md`:

```markdown
# Strategy Update — <short description>
**Date:** YYYY-MM-DD
**Status:** SUCCEEDED | FAILED
**Seed session:** YYYY-MM-DD
**Target trade:** <HH:MM entry, direction, exit reason, P&L>
**Files changed:** <list of files the code change touched>

## Hypothesis
<Phase 2 hypothesis verbatim>

## Code Change
<What was changed mechanistically — not a diff, but a description of what the code
now does differently and in which files. Include the final state after all iterations,
not just the initial change.>

## Iteration History
| Iteration | Change | Group addressed | Trades | 30d P&L delta |
|-----------|--------|----------------|--------|---------------|
| 0 (initial) | <initial change description> | — | — | <delta> |
| 1 | <adjustment> | <reason group> | <count> | <delta> |
| ...        | | | | |

## Phase 6 Result
<current_60d_pnl vs baseline_60d_pnl, and whether Gate 3 passed>

## Outcome
**If SUCCEEDED:**
Files committed: <list>
Commit message summary: <one sentence>
New baseline locked: yes/no

**If FAILED:**
Stopped at: Phase N / Gate N
Primary reason: <why convergence was not achieved — was the hypothesis wrong?
 Was the mechanism too narrow? Did 30d pass but 60d fail?>

## For Future Agents
<Specific insights: what was tried, which adjustments had partial positive effects,
what conditions seemed to matter even if not conclusively. This is institutional
memory — write as if explaining to a colleague who is about to attempt a similar
update six months from now.>

## Do Not Retry Unless (if FAILED)
<Conditions under which this hypothesis could be retried productively. Be concrete:
e.g., "only if a distribution analysis of wick-to-body ratios in the recent corpus
shows at least 30 trades where a 3pt wick sweep occurs before a 20pt reversal —
the sample in this attempt was too small to tune the gate reliably.">
```

### 7.2 — Add an INDEX entry

Add one row to `.agents/strategy-updates/INDEX.md`:

```
| YYYY-MM-DD | <short description> | <seed session> | SUCCEEDED/FAILED | <one-sentence summary> |
```

**Why document success too:** A succeeded update that took 3 iterations to pass the 30-day gate contains information about what adjustments were needed, what negative side-effects appeared, and how they were resolved. Future agents attempting a similar update can start from that context rather than rediscovering the same failure modes. Success documents are also the evidence base for understanding which types of hypotheses tend to work in this strategy, reducing the search space for future attempts.

### 7.3 — If succeeded: finalize

1. Stage only the files touched by the code change (`strategy_smt.py`, `trend.py`, `hypothesis.py`, `daily.py` — whichever were modified).
2. Commit with a message summarizing: hypothesis, seed session, mechanism, 30-day P&L delta, 60-day fold result.
3. Re-lock all regression baselines to reflect the committed strategy:
   ```
   uv run python regression.py --update-baseline
   ```
   **Do not skip this step** — stale baselines will produce spurious FAILs on future runs and make the next update impossible to execute correctly.

---

## Appendix A: Trade-Level Diff — Matching Rules

Two trades are the **same trade** if and only if their start time (entry time) AND end time (exit time) are identical. If either differs, the trade is **changed** (`status=modified`). If a trade exists in current but has no matching start time in baseline, it is **new**. If a trade exists in baseline but has no matching start time in current, it is **removed**.

New and removed trades use the start time as the identifier for matching purposes. Where a session contains two trades with identical start times (re-entries at the same bar), use start time + direction as the composite key.

The trade-level diff does not yet exist as a standalone tool. Until built, perform the diff manually by reading the two TSV files and comparing rows by these rules. When built, the tool should:

- Accept `--dates YYYY-MM-DD` or `YYYY-MM-DD:YYYY-MM-DD`
- Output a structured TSV with the fields defined in Phase 4.2
- Print a summary line per session: `<date>: N changed trades, pnl_delta=+/-$X`
- Print a total line: `TOTAL: N changed trades across M sessions, total_pnl_delta=+/-$X`

---

## Appendix B: Reason Attribution — Sources to Read

When determining why a trade's outcome changed, consult these sources in order:

1. **events.jsonl diff** — find the first event that diverges between baseline and current (same event type, same approximate timestamp). That is the divergence point.
2. **1-minute bar data** — load MNQ bars for the session from the futures cache. Read the 2–3 bars surrounding the divergence timestamp. Note open/high/low/close relative to the stop level, entry level, and any nearby reference levels (TDO, overnight high/low). The bar data often reveals context the events.jsonl did not record — e.g., whether the divergence bar had a long wick vs. a large body, whether price swept a level before the divergence event fired.
3. **strategy state machine** — if the divergence point is not obvious from events + bars, read `strategy_smt.py` at the code path that produces the diverging event and trace the state variables from the session start to the divergence point to understand what values led to the different decision.

Do not rely on events.jsonl alone. Events record what the strategy decided; bar data records the market environment it decided in. Both are necessary to write a reason description that another agent can act on.

---

## Appendix C: Threshold Reference

| Decision point | Threshold | Rationale |
|----------------|-----------|-----------|
| Seed session gate (Gate 1) | `session_pnl_delta > $0` | Minimum confirmation the mechanism works without destroying the session |
| Max seed-session adjustment attempts | 3 | Beyond 3, the hypothesis is too fragile or narrow for this seed |
| 30-day P&L gate (Gate 2) | `total_30d_pnl_delta > $0` | Net positive across the recent window; no trade count guard |
| Refinement iterations | Max 5 | Beyond 5, accumulated conditions signal overfitting to the 30d window |
| 60-day fold gate (Gate 3) | `current_60d_pnl > baseline_60d_pnl` | Final arbiter; uses the current market regime as the benchmark |

---

## Appendix D: Interaction with program_smt.md

This methodology and the walk-forward optimization loop (`program_smt.md`) are complementary but must not run simultaneously on the same working tree. The optimizer edits `strategy_smt.py` (and potentially the same other files) iteratively — an in-progress session-driven update with unstaged changes would pollute the optimizer's baseline.

**Before starting a session-driven update:** confirm no `program_smt.md` optimization run is in progress in any worktree.

**After committing a session-driven update:** re-run `backtest_smt.py` to get the new `mean_test_pnl`. Record it in `experiment-log.md` as the new baseline. If a walk-forward optimization is planned next, use this value as the starting point — otherwise the optimizer may optimize against a stale baseline and attribute improvements to parameter changes that are actually explained by the session update.
