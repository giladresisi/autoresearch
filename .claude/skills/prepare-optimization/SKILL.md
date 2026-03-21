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

To start the optimization, open a new Claude Code session in the worktree:
```
cd ../<tag>
claude
```
Then tell Claude the tickers, timeframe, and number of iterations you want (or accept
the defaults from `prepare.py`). Claude will read `program.md` and run the experiment loop.
