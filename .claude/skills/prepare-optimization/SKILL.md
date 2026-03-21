---
name: prepare-optimization
description: >
  Prepares a fresh git worktree for a new autoresearch optimization run in the auto-co-trader
  project — resolves parameters, creates the worktree, configures files, and downloads data,
  then instructs the user to open a new Claude Code session there to run the experiment loop.
  Use this skill whenever the user says anything like: "prepare a new optimization",
  "set up an optimization", "create an optimization worktree", "start a new optimization",
  "kick off an optimization", "run autoresearch on [sector/tickers]", "optimize with [tickers]",
  "new optimization run", "begin optimizing", "start autoresearch", "optimize [sector] stocks",
  "run an optimization for [description]", or any phrase indicating they want to set up a
  new strategy optimization experiment — even if they don't use the word "optimization"
  explicitly. Always invoke this skill first, before any worktree creation or parameter setup.
---

# prepare-optimization

Sets up a fresh git worktree for a new autoresearch optimization run: collects parameters,
resolves tickers, creates the worktree, configures `prepare.py` and `train.py`, optionally
seeds a starting strategy, and downloads ticker data.

---

## Step 1: Collect Parameters

Gather the four parameters from the user's message. For anything not specified, use the defaults.

### 1a. Timeframe

- **User-specified**: Parse the date range into `BACKTEST_START` and `BACKTEST_END` (YYYY-MM-DD).
  The window must span at least 3 months.
- **Default**: Past 3 months.
  - `BACKTEST_END` = today's date
  - `BACKTEST_START` = 3 months prior (same day-of-month, prior year if needed)
  - Example: today `2026-03-21` → `BACKTEST_START = "2025-12-21"`, `BACKTEST_END = "2026-03-21"`

**Compute train/test split** (always):
- `TRAIN_END` = `BACKTEST_END` − 14 calendar days
- `TEST_START` = same as `TRAIN_END`
- Example: `BACKTEST_END = "2026-03-21"` → `TRAIN_END = TEST_START = "2026-03-07"`

### 1b. Tickers

The user can provide tickers in several ways:

| Input type | How to handle |
|---|---|
| Explicit list (e.g. `AAPL, NVDA, MSFT`) | Use as-is |
| Sector name (e.g. "energy sector", "semiconductors") | Use the well-known tickers for that sector (reference list below) |
| Descriptive text (e.g. "mag7", "defensive stocks", "leading high-beta stocks below $15") | Use your knowledge + web search if needed to build a concrete ticker list |
| Not specified | Ask the user with `AskUserQuestion` |

**You need at least 15 tickers.** If you have fewer and can't confidently expand the list,
use `AskUserQuestion` to ask the user for more tickers or guidance before proceeding.

**Reference lists** (starting points — not exhaustive, add related tickers as needed):
- **Mag7**: AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA
- **Energy**: XOM, CVX, COP, EOG, DVN, BKR, HAL, VLO, MPC, PSX, APA, EQT, CTRA, SM, OXY, SLB, HES
- **Semiconductors**: NVDA, AMD, AVGO, QCOM, MU, INTC, AMAT, KLAC, LRCX, MRVL, ON, TXN, NXPI, SWKS, MCHP, ASML, ARM
- **Financials**: JPM, BAC, WFC, GS, MS, C, BLK, SCHW, AXP, USB, PNC, COF, TFC, SPGI, CME, V, MA
- **Utilities/Defensive**: NEE, DUK, SO, D, AEP, EXC, PCG, ED, ES, WEC, XEL, AWK, ETR, DTE, PPL, WM, RSG
- **Healthcare**: JNJ, UNH, PFE, ABT, LLY, MRK, TMO, DHR, AMGN, GILD, BMY, CVS, ELV, CI, HUM, ISRG, VRTX
- **Technology**: MSFT, AAPL, GOOGL, META, AMZN, ORCL, CRM, ADBE, NOW, INTU, IBM, HPQ, DELL, PANW, CRWD
- **Consumer**: AMZN, TSLA, HD, LOW, MCD, SBUX, NKE, TGT, WMT, COST, TJX, ROST, DG, DLTR, YUM, CMG

If the description is vague (e.g. "leading stocks that fell last month", "high beta names"),
use web search to identify currently relevant tickers matching the description.

### 1c. Iterations

- **User-specified**: Use the number they gave.
- **Default**: 30

*(Iterations are stored in memory only — they're not written to any config file. Just
track the number so you can pass it to program.md's experiment loop.)*

### 1d. Starting Strategy

- **User-specified by name** (e.g. "start from energy_momentum_v1", "use the financials_mar20 strategy"):
  - Locate the file: `strategies/<name>.py` in the current repo.
  - The strategy code will be injected into train.py in the new worktree (see Step 5).
- **Default**: Use train.py as-is (no strategy override needed — skip Step 5).

---

## Step 2: Determine Branch Name

Choose a short, descriptive tag. Default pattern: `<month><day>` (e.g. `mar21`).
If the ticker set maps to a clear sector, prefix it: `energy-mar21`, `semis-apr01`.

Check whether the branch already exists:
```bash
git branch -a | grep "autoresearch/<tag>"
```
If it does, append a suffix: `mar21-b`, `mar21-c`, etc.

The full branch name will be `autoresearch/<tag>`.

---

## Step 3: Create Worktree

Identify the **current branch** (not master — the user may be on a feature branch):
```bash
git branch --show-current
```

Then use the `create-worktree` skill to create the worktree:
- **Branch name**: `autoresearch/<tag>` (from Step 2)
- **Base branch**: the current branch (as determined above)

The worktree lands at `../<tag>` (sibling directory, e.g. `../energy-mar21`).

> **All subsequent file edits go into the new worktree, not the current repo.**

---

## Step 4: Configure prepare.py and train.py

Edit three variables in the `# ── USER CONFIGURATION ──` block of the **worktree's** `prepare.py`:

```python
TICKERS = ["TICK1", "TICK2", ...]   # your resolved list
BACKTEST_START = "YYYY-MM-DD"
BACKTEST_END   = "YYYY-MM-DD"
```

Edit four constants at the top of the **worktree's** `train.py`:

```python
BACKTEST_START = "YYYY-MM-DD"
BACKTEST_END   = "YYYY-MM-DD"
TRAIN_END   = "YYYY-MM-DD"
TEST_START  = "YYYY-MM-DD"
```

Do not touch anything else in either file at this stage.

---

## Step 5: Override Strategy (only if user named a starting strategy)

*Skip this step if using the default (current train.py).*

The goal is to replace the strategy functions in train.py with those from the named strategy,
while preserving the infrastructure preamble (imports, constants, data-loading functions).

1. **Read** `strategies/<name>.py` from the **current repo** (not the worktree).

2. **Extract the strategy functions**: In the strategy file, find the first
   function definition or section comment that follows the `METADATA` dict
   (typically a `# ── Indicators ──` comment or the first `def `). Extract
   everything from that point to the end of the file.

3. **In the worktree's `train.py`**, find the same section boundary — the
   `# ── Indicators` comment (or the first `def` after `load_all_ticker_data`).
   Replace everything from that point to (but **not including**) the
   `# DO NOT EDIT BELOW THIS LINE` boundary with the strategy functions you just extracted.

4. **Verify the four date constants are correct** after the replacement. They live
   above the indicators section, so they should be untouched — but double-check
   that `BACKTEST_START`, `BACKTEST_END`, `TRAIN_END`, and `TEST_START` still
   reflect the values from Step 4, not the strategy file's original dates.

5. **Legacy objective marker**: If the strategy file contains `# LEGACY_OBJECTIVE: sharpe`,
   carry that comment into train.py as-is — Step 6 below will detect it and neutralize
   the thresholds before handing off.

The resulting train.py structure should be:
```
[docstring]
[imports]
CACHE_DIR = ...
BACKTEST_START / BACKTEST_END / TRAIN_END / TEST_START   ← from Step 4
WRITE_FINAL_OUTPUTS = False
load_ticker_data() / load_all_ticker_data()              ← unchanged
[strategy functions from the named strategy file]        ← replaced
# DO NOT EDIT BELOW THIS LINE
[harness — run_backtest, print_results, __main__]        ← unchanged
```

---

## Step 6: Check and Neutralize Legacy Sharpe Objective

Grep the **worktree's** `train.py` for the legacy marker:

```bash
grep "LEGACY_OBJECTIVE: sharpe" "../<tag>/train.py"
```

**If found**: The strategy's thresholds were tuned to maximize Sharpe (not P&L), which means
they likely suppress trade count to inflate the ratio. Before the experiment loop can meaningfully
optimize P&L, reset those thresholds to neutral starting values. Concretely:

- Keep the core entry/exit structure and indicator logic intact.
- Identify any thresholds that are unusually tight in ways that would reduce trade frequency
  (e.g. narrow RSI bands, very high volume multiples, extremely strict CCI cutoffs).
- Reset those specific thresholds to more neutral/relaxed values so the screener generates
  a reasonable number of trades. The first baseline run will establish the P&L baseline
  from this neutral starting point.
- Remove the `# LEGACY_OBJECTIVE: sharpe` comment from train.py once the reset is done,
  so future runs don't re-apply this step.

**If not found**: No action needed — the strategy was already optimized for train P&L.

---

## Step 7: Print Parameter Trace

Output this summary so there's a clear record of what was configured:

```
── New optimization run ─────────────────────────────────
Worktree:    ../<tag>
Branch:      autoresearch/<tag>
Tickers:     TICK1, TICK2, ...  [user-defined / searched / default]
Timeframe:   YYYY-MM-DD → YYYY-MM-DD  [user-defined / default]
  Train:     YYYY-MM-DD → YYYY-MM-DD
  Test:      YYYY-MM-DD → YYYY-MM-DD
Iterations:  30  [user-defined / default]
Strategy:    <name> (from strategies/)  OR  current train.py  [user-defined / default]
Legacy obj:  neutralized  OR  none
─────────────────────────────────────────────────────────
```

Label each line `[user-defined]` or `[default]` as appropriate.

---

## Step 8: Download Data

Run prepare.py in the new worktree to cache ticker data:

```bash
cd "../<tag>" && uv run prepare.py
```

Wait for it to complete. If it fails, report the error and stop — do not proceed to the
experiment loop without valid cached data.

---

## Step 9: Hand Off to User

The worktree is fully prepared. Tell the user:

1. **Open a new Claude Code session** in the worktree directory:
   ```
   cd ../<tag>
   claude
   ```
2. In that session, ask Claude to **start the optimization** — it will read `program.md`
   and execute the experiment loop from step 8 onward (all setup is already done).
3. Include the parameter trace from Step 7 in your message so the user has a record.
4. Mention any notes (e.g. legacy thresholds neutralized, tickers resolved via search).
