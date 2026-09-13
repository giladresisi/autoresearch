# strategy-agent-poc — project notes

Pointers only. This file is auto-loaded into every session in this worktree, so it costs
context every time; it carries what an agent cannot infer from the code and nothing else.
The global `~/.claude/CLAUDE.md` still applies.

## What this worktree is

The L1/L2/L3 agent stack built beside the legacy engine, not inside it. `agent/facts/`
detects artifacts; `agent/trader/` plans and executes against them; the legacy engine
(`strategy.py`, `hypothesis.py`, `live_orders.py`, …) is untouched and unimported.

**`l2-mechanisms.md` is the SPECIFICATION for the entry mechanisms; the code is the
executable truth.** `agent/trader/named_cases.py` is the single source for every
documented figure, and `agent/trader/test_named_cases.py` fails when the two disagree.

## Before changing any entry mechanism

**Read `docs/entry-mechanism-change-protocol.md`.** A step 0 and then eight steps; it
exists because the expensive failures of this project were all process failures, not
coding ones. Step 0 is the one most often skipped: decide whether the change is a RULE
yet. A proposal with an unresolved rule-level ambiguity belongs in `l2-mechanisms.md`
§11 as a CANDIDATE with its evidence, NOT in §§2-8.
Read `l2-mechanisms.md` §11.0 first as well: it is the authoritative scope split and it
lists what must NOT be built.

## Hard constraints an agent cannot infer

- **Never import `live_orders`** from anywhere under `agent/`. The AST gate
  (`agent/trader/test_gate_no_legacy_writes.py`) enforces it, and `test_executor.py` greps
  the whole Executor source for the module name — docstrings included.
- **Never write** `events.jsonl`, `daily.json`, `hypothesis.json`, `position.json` or
  `smts.json`; never call an `smt_state` mutator or `paths.set_state_dir()`.
  `events.jsonl` in particular is the legacy stream the regression diffs line-for-line
  against locked baselines.
- **Bar time inside the bar loop; `get_et_now()` only outside it.** The Executor contains
  neither `get_et_now` nor `datetime.now`, and a gate asserts that. A wall clock in the
  bar loop makes a replay non-deterministic and a backtest unfalsifiable.
- **The machine's timezone is not ET.** Never read the machine clock as market time.

## Test baselines

- `python -m pytest tests/ -q` → **2 failed / 1391 passed / 10 skipped / 16 errors.**
  All two failures and all sixteen errors are PRE-EXISTING. Anything else is a regression.
- `python -m pytest agent/facts agent/trader agent/test_kb_cutover.py
  tests/test_session_pipeline_graft.py -q` → **499 passed / 59 deselected in ~47s.**
  This is the DEFAULT suite and it is meant to stay under a minute; if it creeps over,
  find the new slow test rather than raising the bar.
- `… -m slow` → **59 tests, ~20 minutes, 100 passed / 3 skipped.** `addopts` in
  `pyproject.toml` deselects them by default (same mechanism as `integration`). They are
  not optional extras: they are the tests that make the NUMBERS trustworthy — replay
  determinism (gate 1), replay-reproduces-live (gate 2), KB-edit-invalidates-the-
  recording (gate 4), plan-death-does-not-shorten-the-window (gate 6), and facts
  no-lookahead. **Run `-m slow` before believing any A/B verdict, and after any change to
  the replay, thesis-cache or facts path.** The 3 skips are a stale thesis cache for
  2026-08-13 (the cache is keyed per date x code version); re-seed with `--seed` to
  actually exercise gate 6's forced-DOL trio.
- Locked 1s regression baselines, run INDIVIDUALLY:
  `python regression.py --dates 2026-05-18 --mode 1s` → `pnl=1572.00`
  `python regression.py --dates 2026-05-19 --mode 1s` → `pnl=-367.00`

## Runtime flags

- `ACT_TRADER` is **ON by default**; `ACT_TRADER=0` (or `false`/`no`/`off`) is the kill
  switch (`agent/trader/graft.py`).
- `ACT_TRADER_ARM_HHMM` overrides the 09:20 arm — fidelity fixtures only. `run_replay`
  restores it after a run; leaving it set re-arms every later run in the same process.
- `ACT_THESIS_CACHE_DIR` redirects the thesis cache. Point it at a tmp dir for any test
  that would otherwise deposit a synthetic recording into `<global>/thesis_cache`, which
  every worktree reads.

## Never commit

`feature.md`'s deletion, `agent-optimizations.md`, `refinement-proposals.md`, and the
`manual-l1-thesis/` run directories. They are working state, not deliverables.
