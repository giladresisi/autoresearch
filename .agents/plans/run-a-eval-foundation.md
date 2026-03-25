# Run A: Eval Foundation + Entry Quality

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

---

## Feature Description

This plan sets up a new optimization worktree whose purpose is to (1) re-baseline the
evaluation infrastructure with wider folds and a stricter minimum-trade floor, and
(2) attack the core structural problem identified in the price-volume-updates post-mortem:
89% of trades live in the 1–5 day bucket at a near-coin-flip win rate, collectively losing
$120.97. The fix hypothesis is **entry quality**, not exit timing: the 381-ticker universe
generates too many marginal breakouts that immediately reverse. This run reduces the
effective universe via a dollar volume threshold in `screen_day()` and lets autoresearch
calibrate both position management and universe quality over 10 iterations.

## User Story

As the strategy optimizer,
I want a correctly configured evaluation baseline with fold-level reliability and a hard
trade-count floor,
So that autoresearch improvements signal true edge rather than noise from sparse folds.

## Problem Statement

The current walk-forward setup (7×40 folds) produces only ~17 test trades per fold after
universe reduction — too few for min_test_pnl to reliably distinguish a real improvement
from a lucky fold. The 381-ticker universe generates ~84 training trades per 40-day fold
but 89% are short-duration losers. position management priority in program.md is
scheduled for iterations 6–10, delaying the highest-leverage parameter. No minimum
trade-count floor exists to prevent the optimizer from keeping over-filtered strategies.

## Solution Statement

Switch to 6×60 folds (wider test windows, ~27 test trades per fold at 150 tickers),
add a `MIN_DOLLAR_VOLUME` filter in `screen_day()` to constrain the effective universe to
~150 high-liquidity tickers, add an 110-trade discard rule to program.md, move position
management experiments to iterations 2–4, and add mean_test_pnl and structural screener
guidance. The optimization runs 10 iterations: baseline calibration, trailing stop sweep,
dollar volume threshold sweep, and structural screener refinements.

## Feature Metadata

**Feature Type**: Enhancement (optimization run setup)
**Complexity**: Medium
**Primary Systems Affected**: `train.py` (screen_day, constants), `program.md`
**Dependencies**: Completed price-volume-updates worktree (provides baseline strategy)
**Breaking Changes**: No — changes are isolated to a new worktree

---

## Input From Previous Run

This is Run A — the first run. No prior optimization gate to satisfy.

Carry-in from `autoresearch/price-volume-updates` post-mortem (source of record:
`harness_upgrade_20260325_1547.md`):
- Best committed strategy: trailing stop activation at 1.5 ATR, SMA20/SMA50 gap >= 1%,
  SMA50/SMA100 gap >= 0.5%, pivot-only stop (no fallback)
- Key finding: 1–5d bucket net −$120.97 at 49% win rate; 6–14d bucket net +$188.50 at
  100% win rate. Profitable signal lives entirely in longer-duration trades.
- Universe: 381 tickers, 2.1 trades/day on entry days (1.17 trades/day across all days)
- train_win_loss_ratio: 0.934 — below 1.0x, structural fragility signal

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` (lines 1–80) — mutable constants block; SESSION SETUP and STRATEGY TUNING
  sections; `FOLD_TEST_DAYS`, `WALK_FORWARD_WINDOWS`, `TRAIN_END` are here
- `train.py` (lines 230–370) — `screen_day()` function; add dollar volume filter after
  the volume check block (lines ~305–320); `MIN_DOLLAR_VOLUME` constant goes in mutable
  constants block
- `train.py` (lines 374–410) — `manage_position()` function; reference for position
  management parameters autoresearch will sweep in iterations 2–4
- `program.md` (lines 149–190) — Experimentation rules section; "Position management
  priority" note (line 160) changes from iterations 6–10 to 2–4; keep/discard goal
  section (line 174) gets 110-trade floor added
- `program.md` (lines 267–300) — Logging results section; results.tsv header and column
  definitions; add `mean_test_pnl` column instruction here
- `harness_upgrade_20260325_1547.md` — source recommendations R1, R2, R4, R5, R6, R9

### New Files to Create

None — this plan modifies existing files only.

### Patterns to Follow

- `MIN_DOLLAR_VOLUME` constant placement: mutable `# ══ STRATEGY TUNING ══` block,
  grouped with other screener threshold constants
- Dollar volume computation pattern: `(hist['close'] * hist['volume']).iloc[-60:].mean()`
  — uses yesterday-only data (`hist = df.iloc[:-1]`), consistent with no-look-ahead rule
- program.md rule additions: match the imperative, concise tone of existing rules in
  the "Experimentation rules" section; one sentence per rule

---

## WORKTREE SETUP (before plan execution)

Run the `prepare-optimization` skill to create a new worktree starting from the
best commit of `autoresearch/price-volume-updates`. Name it to reflect the run purpose
(e.g., `dollar-vol-mar25`). Open a Claude Code session inside the new worktree, then
execute this plan.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
WAVE 1 (parallel): train.py fold config  |  train.py dollar volume filter
                                          |
                          ↓              ↓
WAVE 2 (parallel): program.md position priority  |  program.md 110-trade floor
                                                  |  program.md mean_test_pnl logging
                                                  |  program.md structural guidance
                          ↓
WAVE 3 (sequential): validate baseline run
```

### Interface Contracts

- Wave 1 provides: `MIN_DOLLAR_VOLUME` constant in mutable block + filter in
  `screen_day()` + updated fold constants
- Wave 2 consumes: no code dependency; reads updated program.md only
- Wave 3 consumes: all Wave 1 + Wave 2 changes; runs `uv run train.py` to confirm
  baseline executes, produces output, and total_trades >= 110

---

## IMPLEMENTATION PLAN

### Phase 1: Session Setup Constants (train.py)

Update the SESSION SETUP block in `train.py`. These constants are set once at worktree
creation and must NOT be changed during the optimization loop.

#### Task 1.1: Update fold configuration constants

- **WAVE**: 1
- **AGENT_ROLE**: strategy-configurator
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: Wider fold windows for better test-trade-count reliability
- **IMPLEMENT**:
  - Set `FOLD_TEST_DAYS = 60` (was 40 — wider test window, ~27 test trades per fold
    at 150 tickers vs 17 previously)
  - Set `WALK_FORWARD_WINDOWS = 6` (was 7 — 6 folds × 60 days = 360 bdays of test
    coverage; fold1 training window = ~35 bdays which is thin but acceptable with
    the expanding window)
  - Keep `FOLD_TRAIN_DAYS = 0` (expanding window — unchanged)
  - Keep `TRAIN_END = BACKTEST_END − 14 calendar days` (14-day holdout — do NOT
    extend to 30 days; fold1 training would shrink to 23 bdays, too thin)
  - Update `WALK_FORWARD_WINDOWS` docstring reference from 7 to 6
- **VALIDATE**: `grep "FOLD_TEST_DAYS\|WALK_FORWARD_WINDOWS" train.py`
  Expected: `FOLD_TEST_DAYS = 60`, `WALK_FORWARD_WINDOWS = 6`

#### Task 1.2: Add MIN_DOLLAR_VOLUME constant and screen_day() filter

- **WAVE**: 1
- **AGENT_ROLE**: strategy-configurator
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: Dollar volume filter that constrains effective universe to ~150 tickers
- **IMPLEMENT**:

  Step 1 — Add constant to the mutable `# ══ STRATEGY TUNING ══` block, grouped with
  screener threshold constants. Choose an initial value that targets the top ~150
  tickers by 60-day avg dollar volume. Based on the current S&P 500 / Russell 1000
  composition, $150M/day is a reasonable starting point:
  ```python
  # Dollar volume threshold: skip tickers below this 60-day avg daily $ volume.
  # Targets ~150 highest-liquidity tickers. Autoresearch tunes this value.
  # At 381 tickers: $150M/day ≈ top 150; $100M/day ≈ top 200; $200M/day ≈ top 100.
  MIN_DOLLAR_VOLUME = 150_000_000
  ```

  Step 2 — Add the filter in `screen_day()`, immediately after the existing volume
  check block (after `prev_vol_ratio` check, before the `high20` breakout check).
  Insert after the `if prev_vol_ratio < 0.8: return None` line:
  ```python
  # Dollar volume filter: skip tickers with insufficient daily liquidity.
  # Uses 60-day lookback on yesterday's data only (no look-ahead).
  avg_dol_vol = float((hist['close'] * hist['volume']).iloc[-60:].mean())
  if avg_dol_vol < MIN_DOLLAR_VOLUME:
      return None
  ```

- **VALIDATE**:
  ```bash
  grep -n "MIN_DOLLAR_VOLUME\|avg_dol_vol" train.py
  ```
  Expected: constant in mutable block, filter in screen_day() body.
  Also confirm `hist` is defined before the new line (it is — `hist = df.iloc[:-1]`
  is assigned near the top of `screen_day()`).

### Phase 2: program.md Updates

All four program.md changes are independent and can be applied in the same wave.

#### Task 2.1: Move position management priority to iterations 2–4

- **WAVE**: 2
- **AGENT_ROLE**: process-configurator
- **DEPENDS_ON**: []
- **BLOCKS**: []
- **IMPLEMENT**: In the "Experimentation rules → What you CAN do" section, find the
  existing "Position management priority" note (currently says "iterations 6–10"):
  ```
  **Position management priority**: Explicitly test trailing-stop distance, breakeven
  trigger level, and stop-distance changes in iterations 6–10...
  ```
  Replace with:
  ```
  **Position management priority**: Explicitly test trailing-stop distance, breakeven
  trigger level, and stop-distance changes in iterations 2–4, BEFORE screener work.
  These are the highest-leverage parameters (trailing stop activation 2.0→1.5 ATR was
  the single largest improvement in the previous run, +$34.79 min_test_pnl). Screening
  results are meaningless until position management is calibrated against the new fold
  configuration.
  ```
- **VALIDATE**: `grep -n "Position management priority" program.md`

#### Task 2.2: Add 110-trade discard floor

- **WAVE**: 2
- **AGENT_ROLE**: process-configurator
- **DEPENDS_ON**: []
- **BLOCKS**: []
- **IMPLEMENT**: In the "Goal" subsection of "Experimentation rules", after the existing
  keep/discard paragraph, add:
  ```
  **Minimum trade floor**: If `total_trades` (last fold's training trade count) < 110,
  mark the result `discard-thin` regardless of min_test_pnl. A strategy with fewer than
  110 training trades is statistically unreliable — min_test_pnl from sparse folds is
  noise-dominated. This floor prevents over-filtering: any screener change that cuts
  more than ~27% of trades from the baseline is automatically rejected.
  ```
- **VALIDATE**: `grep -n "discard-thin\|110" program.md`

#### Task 2.3: Add mean_test_pnl logging instruction

- **WAVE**: 2
- **AGENT_ROLE**: process-configurator
- **DEPENDS_ON**: []
- **BLOCKS**: []
- **IMPLEMENT**: In the "Logging results" section, extend the results.tsv header
  instruction to include `mean_test_pnl` as an extra column after `min_test_pnl`.
  Add the following after the existing column definitions:
  ```
  14. `mean_test_pnl`: arithmetic mean of all fold test P&Ls (diagnostic). Compute
  from the fold{N}_test_total_pnl lines in run.log:
      grep "^fold[0-9]*_test_total_pnl:" run.log | awk -F: '{sum+=$2; n++} END {printf "%.2f\n", sum/n}'
  A strategy with high min_test_pnl but low or negative mean_test_pnl is being pulled
  up by one or two exceptional folds. Track but do not use as a keep/discard criterion
  in any run — use for pattern diagnostics only.

  **Manual-review flag**: If `mean_test_pnl` improves by > $50 relative to the current
  baseline AND `min_test_pnl` worsened, do NOT use a plain `discard` status. Instead
  mark the iteration `discard-manual-review` and include in the description field:
  `mean_delta=+$X min_delta=-$Y`. This signals a potentially interesting trade-off for
  the user to inspect after the run. The iteration is still reverted — the flag is
  informational only and does not change the keep/discard outcome.
  ```
  Also update the header row example to include the new column.
- **VALIDATE**: `grep -n "mean_test_pnl\|discard-manual-review" program.md`

#### Task 2.4: Add structural screener guidance

#### Task 2.5: Add Session Override block to program.md

- **WAVE**: 2
- **AGENT_ROLE**: process-configurator
- **DEPENDS_ON**: []
- **BLOCKS**: []
- **PURPOSE**: Allows the user to restart from any mid-run discarded commit and run a
  new session with a different keep criterion, without permanently changing the
  optimization rules. The block is inert (Active: NO) by default.
- **IMPLEMENT**: Insert the following section into program.md BEFORE the
  `## Experimentation rules` heading:

  ```markdown
  ## Session Override

  **Active**: NO

  When Active is NO (default), the agent ignores this section entirely and uses the
  standard keep/discard rules from the Goal section.

  When Active is YES, this section takes precedence over the Goal section's
  keep/discard criterion for this session only. Set Active back to NO after the
  session ends.

  **Baseline commit**: [fill in — git hash to use as the session starting point;
  run `git checkout <hash>` in the worktree before starting the session]
  **Keep criterion override**: [fill in — replaces the min_test_pnl criterion;
  example: "keep if mean_test_pnl improves by > $20, regardless of min_test_pnl;
  still apply 110-trade floor and pnl_consistency floor"]
  **Rationale**: [fill in — what triggered the override and what you are testing]
  **Revert after session**: YES — set Active back to NO when done
  ```

- **VALIDATE**: `grep -n "Session Override\|Active.*NO" program.md`

- **WAVE**: 2
- **AGENT_ROLE**: process-configurator
- **DEPENDS_ON**: []
- **BLOCKS**: []
- **IMPLEMENT**: In the "Experimentation rules → Simplicity criterion" section, after
  the existing simplicity note, add:
  ```
  **Structural vs threshold screener changes**: When trade count is high (> 150 per
  training fold on 100+ tickers), prioritize structural screener improvements (SMA
  alignment, trend slope requirements, dollar volume thresholds) over indicator
  threshold tightening (RSI range, volume ratio floors). In the price-volume-updates
  run, RSI tightening degraded min_test_pnl by −$108.56 while structural SMA gap
  filters improved it by +$13.59. RSI/volume threshold tightening cuts signal count
  without improving entry quality; structural filters eliminate weak-trend setups
  entirely.
  ```
- **VALIDATE**: `grep -n "Structural vs threshold" program.md`

### Phase 3: Baseline Validation

#### Task 3.1: Run baseline and verify setup

- **WAVE**: 3
- **AGENT_ROLE**: validator
- **DEPENDS_ON**: [1.1, 1.2, 2.1, 2.2, 2.3, 2.4]
- **IMPLEMENT**:
  1. Run `uv run prepare.py` to ensure cache is populated for current TICKERS.
  2. Run `uv run train.py 2>&1 | tee baseline.log`
  3. Check output:
     - `min_test_pnl_folds_included:` should be 6 (all folds have ≥ 3 test trades)
     - `fold6_train_total_trades:` should be ≥ 110 (discard-thin floor)
     - If fold1 produces < 3 test trades, it will be excluded — that is acceptable;
       `min_test_pnl_folds_included: 5` is acceptable, `< 4` is a warning to note
  4. Extract baseline trade counts per fold to establish dollar volume calibration:
     ```bash
     grep "test_total_trades\|train_total_trades" baseline.log
     ```
  5. If `fold6_train_total_trades < 110`, lower `MIN_DOLLAR_VOLUME` by $25M increments
     until baseline passes the floor. Document the final calibrated value.
- **VALIDATE**:
  ```bash
  grep "fold6_train_total_trades\|min_test_pnl_folds_included" baseline.log
  ```
  Expected: fold6 training trades ≥ 110, folds_included ≥ 4.

---

## OPTIMIZATION AGENDA

This section instructs the autoresearch agent on what to test and in what order.
**Append this block verbatim to program.md under a new "## Run A Agenda" section.**

```markdown
## Run A Agenda

Run exactly 10 iterations after the baseline. Follow this sequence:

### Iterations 2–4: Position Management Calibration
Re-validate trailing stop activation with the new 6×60 fold configuration.
The 1.5 ATR activation (from price-volume-updates run) was optimal for 7×40 folds;
verify it holds under 6×60 before using it as the baseline for screener experiments.

Test in order (one per iteration):
- Iter 2: trailing stop activation at 1.2 ATR (more aggressive protection)
- Iter 3: trailing stop activation at 1.8 ATR (more patient)
- Iter 4: trailing stop trail distance at 1.0 ATR vs current 1.2 ATR

Keep whichever produces best min_test_pnl. If 1.5 ATR still wins after iter 3,
that is the validated baseline — proceed to iter 4 regardless.

### Iterations 5–8: Dollar Volume Threshold Calibration
Test the effect of different MIN_DOLLAR_VOLUME thresholds. Goal: find the floor
that maximizes entry quality (min_test_pnl improvement) while keeping
total_trades >= 110.

Test in order:
- Iter 5: MIN_DOLLAR_VOLUME = $100M/day (broader, ~200 tickers)
- Iter 6: MIN_DOLLAR_VOLUME = $200M/day (stricter, ~100 tickers)
- Iter 7: MIN_DOLLAR_VOLUME = $250M/day (tightest, ~80 tickers; expect thin trades —
  discard-thin if total_trades < 110)
- Iter 8: (if iter 7 was discarded) try $175M/day as intermediate

After iter 8: record the kept MIN_DOLLAR_VOLUME level. Also record
fold6_train_total_trades at the kept baseline for Run B gate assessment.

### Iterations 9–10: Structural Screener Additions
With position management calibrated and universe size fixed, attempt one structural
screener improvement:
- Iter 9: Add SMA20 slope requirement — require SMA20 today > SMA20 5 days ago ×
  1.002 (stock must be in active upslope, not flat or declining). This is a stricter
  form of the existing slope_floor check.
- Iter 10: Add minimum days-since-52-week-low requirement — reject entries where
  the stock made a 52-week low within the last 30 trading days. Use
  `hist['close'].iloc[-252:].min()` vs `hist['close'].iloc[-30:].min()`.
  Discard if this proves overly restrictive (total_trades < 110).
```

---

## GATE CONDITIONS FOR RUN B

After the 10-iteration loop completes, evaluate these conditions to decide whether
Run B should proceed:

**Gate 1 — Trade count floor maintained**: The final kept strategy has
`fold6_train_total_trades >= 110`. If not, the dollar volume calibration did not
succeed; re-run iterations 5–8 with looser thresholds before proceeding.

**Gate 2 — Signal observed**: At least one iteration improved min_test_pnl vs the
Run A baseline (even a small improvement counts). If zero iterations kept, the
baseline strategy is not improvable under the new fold configuration — escalate to user.

**Gate 3 — 1-5d bucket assessment**: Pull `trades.tsv` from the final iteration.
Run the standard days-held analysis:
```bash
python3 - << 'EOF'
import csv
from collections import defaultdict
trades = list(csv.DictReader(open("trades.tsv"), delimiter="\t"))
buckets = defaultdict(lambda: {"n": 0, "net": 0.0})
for t in trades:
    days = int(t["days_held"]); pnl = float(t["pnl"])
    label = "1-5d" if 1 <= days <= 5 else ("6-14d" if 6 <= days <= 14 else "15+d" if days >= 15 else "0d")
    buckets[label]["n"] += 1; buckets[label]["net"] += pnl
for k, v in sorted(buckets.items()):
    print(f"{k}: n={v['n']} net=${v['net']:.2f} ({v['n']/len(trades)*100:.0f}%)")
EOF
```

- If 1–5d bucket is net-positive AND accounts for < 60% of all trades: **skip Run B**
  (entry quality fix solved the problem; proceed directly to Run C if needed).
- If 1–5d bucket is still net-negative OR accounts for > 70% of all trades: **Run B
  is warranted** — exit timing protection is needed.

**Record for Run B** (copy to top of run-b-exit-timing.md when starting):
- Final MIN_DOLLAR_VOLUME value kept
- Final trailing stop activation level kept
- trades.tsv 1-5d bucket net P&L and trade count percentage
- fold6_train_total_trades at final kept iteration
- min_test_pnl at final kept iteration

---

## TESTING STRATEGY

### Setup Validation Tests

**Test 1: Fold configuration**
- **Status**: ✅ Automated
- **Tool**: bash grep
- **Run**: `grep "FOLD_TEST_DAYS\|WALK_FORWARD_WINDOWS" train.py`
- **Expected**: `FOLD_TEST_DAYS = 60`, `WALK_FORWARD_WINDOWS = 6`

**Test 2: Dollar volume filter present**
- **Status**: ✅ Automated
- **Tool**: bash grep
- **Run**: `grep -n "MIN_DOLLAR_VOLUME\|avg_dol_vol" train.py`
- **Expected**: constant in mutable block (line < 230), filter in screen_day body

**Test 3: Baseline run completes without crash**
- **Status**: ✅ Automated
- **Tool**: bash exit code
- **Run**: `uv run train.py > baseline.log 2>&1; echo "exit: $?"`
- **Expected**: exit code 0, `min_test_pnl:` present in output

**Test 4: Fold trade counts above floor**
- **Status**: ✅ Automated
- **Tool**: bash grep + awk
- **Run**: `grep "fold6_train_total_trades" baseline.log`
- **Expected**: value ≥ 110

**Test 5: 6 folds included in min_test_pnl**
- **Status**: ✅ Automated
- **Tool**: bash grep
- **Run**: `grep "min_test_pnl_folds_included" baseline.log`
- **Expected**: value ≥ 4 (fold1 thin training may produce < 3 test trades; 4–6 is fine)

**Test 6: program.md changes present**
- **Status**: ✅ Automated
- **Tool**: bash grep
- **Run**: `grep -c "discard-thin\|mean_test_pnl\|Structural vs threshold\|iterations 2.4\|Session Override" program.md`
- **Expected**: 5 (all five additions present)

**Test 7: Existing test suite passes**
- **Status**: ✅ Automated
- **Tool**: pytest
- **Run**: `uv run pytest tests/ -q --tb=short`
- **Expected**: same passing count as before this plan (no regressions from train.py edits)

### Manual Tests

None — all validation is automatable.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Automated (bash + pytest) | 7 | 100% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 7 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Constants and filter presence
```bash
grep "FOLD_TEST_DAYS\|WALK_FORWARD_WINDOWS\|MIN_DOLLAR_VOLUME\|avg_dol_vol" train.py
grep "discard-thin\|mean_test_pnl\|discard-manual-review\|Structural vs threshold\|Session Override" program.md
grep "iterations 2.4" program.md
```

### Level 2: Unit tests
```bash
uv run pytest tests/ -q --tb=short
```

### Level 3: Baseline execution
```bash
uv run prepare.py
uv run train.py 2>&1 | tee baseline.log
grep "fold6_train_total_trades\|min_test_pnl_folds_included\|min_test_pnl:" baseline.log
```

### Level 4: Trade floor check
```bash
python3 -c "
import re, sys
log = open('baseline.log').read()
m = re.search(r'fold6_train_total_trades:\s+(\d+)', log)
trades = int(m.group(1)) if m else 0
print(f'fold6 training trades: {trades}')
print('PASS' if trades >= 110 else f'FAIL — below 110 floor, lower MIN_DOLLAR_VOLUME')
"
```

---

## ACCEPTANCE CRITERIA

- [ ] `FOLD_TEST_DAYS = 60` and `WALK_FORWARD_WINDOWS = 6` set in SESSION SETUP block
- [ ] `MIN_DOLLAR_VOLUME` constant present in STRATEGY TUNING mutable block
- [ ] Dollar volume filter present in `screen_day()` using yesterday-only data
- [ ] `program.md`: position management priority moved to iterations 2–4
- [ ] `program.md`: 110-trade discard-thin rule added to Goal section
- [ ] `program.md`: mean_test_pnl logging instruction added
- [ ] `program.md`: structural vs threshold screener guidance added
- [ ] `program.md`: Session Override block added (Active: NO by default)
- [ ] `program.md`: Run A Agenda appended as new section
- [ ] Baseline run exits 0, all 6 folds produce output
- [ ] `fold6_train_total_trades >= 110` at baseline
- [ ] `min_test_pnl_folds_included >= 4` at baseline
- [ ] Existing test suite passes without regressions
- [ ] Gate conditions for Run B documented in run.log or a post-run note

---

## COMPLETION CHECKLIST

- [ ] Task 1.1 complete: fold constants updated
- [ ] Task 1.2 complete: MIN_DOLLAR_VOLUME constant + screen_day filter added
- [ ] Task 2.1 complete: position management priority updated
- [ ] Task 2.2 complete: 110-trade discard floor added
- [ ] Task 2.3 complete: mean_test_pnl logging added
- [ ] Task 2.4 complete: structural screener guidance added
- [ ] Task 2.5 complete: Session Override block added to program.md
- [ ] Task 3.1 complete: baseline validated, trade counts confirmed
- [ ] All validation levels executed
- [ ] All acceptance criteria met
- [ ] **⚠️ Changes UNSTAGED — NOT committed**

---

## NOTES

**Why 14-day holdout (not 30)**: With 6×60 folds, fold1 trains on ~35 bdays. Extending
holdout to 30 days shrinks this to ~23 bdays, which is too thin for reliable fold1
training. The 14-day holdout is the better choice at this fold configuration.

**Why the 110 floor is set at 110 and not 130**: At 150 tickers, the baseline produces
~151 training trades. 110 allows any single screener change that cuts up to ~27% of
trades to be testable before hitting the floor. 130 would only allow ~13% cuts, making
it impossible to test meaningful structural filters one at a time.

**Fold1 thinness**: Fold1 trains on ~35 business days (7 calendar weeks) with expanding
window. This may produce only 10–15 training trades in fold1 and potentially < 3 test
trades. The `_qualified` filter in the harness will exclude it from min_test_pnl if
it has < 3 test trades — that is acceptable. min_test_pnl_folds_included: 5 is fine.

**dollar volume as proxy for universe quality**: Top 150 by dollar volume are not
necessarily the "best" tickers for momentum — they include mega-caps (AAPL, MSFT, NVDA)
that are efficiently priced and may have noisier breakout signals. If baseline min_test_pnl
is worse than the prior run's $-28.12, consider that the dollar volume filter may be
hurting rather than helping. In that case, use iterations 5–8 to find the right balance
rather than assuming tighter = better.
