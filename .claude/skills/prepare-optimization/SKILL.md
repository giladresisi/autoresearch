---
name: prepare-optimization
description: >
  Creates a new git worktree to serve as the starting point for an autoresearch optimization
  run in the auto-co-trader project. Use this skill when the user wants to CREATE or SET UP
  a new worktree — phrases like "prepare a new optimization worktree", "set up a worktree for
  optimization", "create a new optimization worktree", "prep a new run", "new worktree for
  autoresearch", "prepare optimization from [strategy]", or "create a worktree using [strategy]".
  Do NOT use this skill when the user is already in a worktree and wants to start/run/begin
  the experiment loop — that is handled by program.md in the worktree session.
---

# prepare-optimization

Creates a new git worktree for an autoresearch optimization run and optionally seeds it with
a named starting strategy. Timeframe, tickers, and iterations are left at their defaults and
configured by the agent in the new session when the user starts the loop.

---

## Step 0: Branch Context Check

Before doing anything else, check which branch you are on:

```bash
git branch --show-current
```

### Case A — On master, but user asked to START/RUN an optimization

If the user's request is to **start, run, or begin** an optimization (not to prepare/create a
worktree), and the current branch is `master`, stop and tell the user:

> You're on the `master` branch. Optimization runs should be started inside a dedicated
> worktree, not on master.
>
> Would you like me to create a new worktree for this run? Here are the defaults — let me know
> if you'd like to change any of them before I create it:
>
> | Parameter | Default |
> |-----------|---------|
> | Tickers | AAPL, MSFT, NVDA, AMD, META, GOOGL, AMZN, TSLA, AVGO, ORCL, CRM, ADBE, QCOM, MU, AMAT, NOW, PLTR, MSTR, APP, SMCI, NFLX, COIN, CRWD, ZS, PANW, JPM, GS, BAC, WFC, MS, BLK, SCHW, AXP, COF, SPGI, V, MA, UNH, LLY, ABBV, JNJ, MRK, PFE, TMO, ISRG, AMGN, GILD, REGN, VRTX, XOM, CVX, COP, SLB, EOG, MPC, VLO, OXY, WMT, PG, KO, PEP, COST, TGT, PM, CL, CAT, DE, UPS, FDX, GE, HON, RTX, LMT, HD, MCD, NKE, SBUX, LOW, F, GM, LIN, APD, NEM, FCX, NUE |
> | Timeframe | Past 1 year (train: first ~10.5 months, test: final ~6 weeks) |
> | Iterations | 30 |

Do not proceed further until the user confirms.

### Case B — On a non-master worktree, but user asked to PREPARE/CREATE a worktree

If the user's request is to **prepare, create, or set up** a new worktree, and the current
branch is **not** `master`, stop and tell the user:

> You're on branch `<current-branch>`, not `master`. Worktree preparation should be done from
> the `master` branch so each run starts from a clean baseline.
>
> It looks like you're already in an optimization worktree. Would you like to start a new
> optimization run here instead? The current parameters are:
>
> - **Tickers**: `<list from train.py TICKERS variable>`
> - **Timeframe**: `<BACKTEST_START> → <BACKTEST_END>` (from train.py)
> - **Iterations**: 30 (default — you can ask to change this)
>
> You can ask me to change any of these before starting.

To get the current parameters, read the worktree's `train.py` and extract `TICKERS`,
`BACKTEST_START`, and `BACKTEST_END`.

Do not proceed further until the user confirms.

### Case C — On master, user asked to PREPARE/CREATE a worktree

This is the normal flow. Continue to Step 1 below.

---

## Step 1: Identify Starting Strategy

Check whether the user named a starting strategy in their message:

- **Named** (e.g. "start from energy_momentum_v1", "use financials_mar20"):
  - Locate `strategies/<name>.py` in the current repo.
  - Its functions will be injected into train.py in the worktree (see Step 4).
- **Not named (default)**: Use train.py as-is — skip Step 4.

---

## Step 2: Determine Branch Name

The branch name needs a descriptive prefix so the worktree folder is self-explanatory
(e.g. `energy-mar21`, `semis-apr01`, `mag7-mar21`). Derive it from, in order:

1. **The user's message** — if it mentions a sector, theme, or set of tickers (e.g. "energy
   stocks", "semiconductors", "mag7", "defensive names"), use that as the prefix.
2. **The named starting strategy** — if the strategy name implies a sector
   (e.g. `energy_momentum_v1` → `energy`, `semis_mar20` → `semis`), use that.
3. **Neither** — if there is no usable context, ask the user before proceeding:
   > To name the worktree, can you briefly describe what you want to optimize for?
   > (e.g. "energy stocks", "semiconductors", "mag7") — no need to list exact tickers.

Once you have a prefix, the tag is `<prefix>-<month><day>` (e.g. `energy-mar21`).

Check for collision:
```bash
git branch -a | grep "autoresearch/<tag>"
```
If it exists, append a suffix: `energy-mar21-b`, etc.

Full branch name: `autoresearch/<tag>`.

---

## Step 3: Create Worktree

Get the current branch:
```bash
git branch --show-current
```

Use the `create-worktree` skill:
- **Branch name**: `autoresearch/<tag>`
- **Base branch**: the current branch (as above)

The worktree lands at `../<tag>`.

> All subsequent file edits go into the new worktree, not the current repo.

---

## Step 4: Inject Starting Strategy (only if user named one)

*Skip if using the default train.py.*

Replace the strategy functions in the worktree's `train.py` with those from the named strategy,
keeping the infrastructure preamble intact.

1. **Read** `strategies/<name>.py` from the current repo.
2. **Extract strategy functions**: find the first `# ──` section comment or `def` that follows
   the closing `}` of the METADATA dict, and take from there to end of file.
3. **In the worktree's `train.py`**, find the same boundary (the `# ── Indicators` comment, or
   the first `def` after `load_all_ticker_data`). Replace from there to (but not including)
   `# DO NOT EDIT BELOW THIS LINE` with the extracted functions.
4. The date constants (`BACKTEST_START`, `BACKTEST_END`, `TRAIN_END`, `TEST_START`) live above
   the indicators section — do not change them.
5. If the strategy file contains `# LEGACY_OBJECTIVE: sharpe`, carry that comment into the
   worktree's `train.py` as-is — Step 5 below will detect and neutralize it.

Resulting structure:
```
[preamble: docstring, imports, CACHE_DIR, date constants, WRITE_FINAL_OUTPUTS]
[load_ticker_data / load_all_ticker_data]          ← unchanged
[strategy functions from named strategy]           ← replaced
# DO NOT EDIT BELOW THIS LINE
[harness]                                          ← unchanged
```

---

## Step 5: Check and Neutralize Legacy Sharpe Objective

```bash
grep "LEGACY_OBJECTIVE: sharpe" "../<tag>/train.py"
```

**If found**: The strategy was tuned to maximize Sharpe, not P&L — its thresholds likely
suppress trade count to inflate the ratio. Reset any that look overly restrictive (narrow RSI
bands, very high volume multiples, extremely strict CCI cutoffs) to more neutral values so the
screener produces a reasonable trade count. Remove the `# LEGACY_OBJECTIVE: sharpe` comment
when done.

**If not found**: No action needed.

---

## Step 6: Commit Initial State

If train.py was modified (strategy injection or legacy-objective neutralization), commit it now:

```bash
cd "../<tag>"
git add train.py
git commit -m "setup(<tag>): seed from <strategy_name>, neutralize legacy Sharpe objective"
```

If train.py was not modified (default strategy, no legacy marker), skip this step — the
worktree starts from the base branch commit with no additional changes needed.

---

## Step 7: Hand Off

Tell the user the worktree is ready:

```
── Worktree prepared ────────────────────────────────
Worktree:  ../<tag>
Branch:    autoresearch/<tag>
Strategy:  <name> (from strategies/)  OR  current train.py
Legacy:    neutralized  OR  n/a
─────────────────────────────────────────────────────
```

Then give the user these instructions:

---

Open a new Claude Code session in the worktree and ask Claude to start the optimization:

```
cd ../<tag>
claude
```

**Example requests** (use any combination of parameters, or omit any for the default):

```
Run the optimization
```
```
Run the optimization for 50 iterations
```
```
Run the optimization for AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA, AVGO, COST, NFLX,
ADBE, AMD, QCOM, TXN, INTC, AMAT, MU, ASML, ARM, MRVL
```
```
Run the optimization for nasdaq100 stocks over the past 6 months
```
```
Run the optimization for the past 4 months, 40 iterations
```

**Defaults** (applied when a parameter is not specified):

| Parameter | Default |
|-----------|---------|
| Tickers | AAPL, MSFT, NVDA, AMD, META, GOOGL, AMZN, TSLA, AVGO, ORCL, CRM, ADBE, QCOM, MU, AMAT, NOW, PLTR, MSTR, APP, SMCI, NFLX, COIN, CRWD, ZS, PANW, JPM, GS, BAC, WFC, MS, BLK, SCHW, AXP, COF, SPGI, V, MA, UNH, LLY, ABBV, JNJ, MRK, PFE, TMO, ISRG, AMGN, GILD, REGN, VRTX, XOM, CVX, COP, SLB, EOG, MPC, VLO, OXY, WMT, PG, KO, PEP, COST, TGT, PM, CL, CAT, DE, UPS, FDX, GE, HON, RTX, LMT, HD, MCD, NKE, SBUX, LOW, F, GM, LIN, APD, NEM, FCX, NUE |
| Timeframe | Past 1 year (train: first ~10.5 months, test: final ~6 weeks) |
| Iterations | 30 |

---
