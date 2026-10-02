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

(Measured 2026-09-27, plan 42.)

- `python -m pytest tests/ -q` → **2 failed / 1578 passed / 4 skipped / 16 errors.**
  The two failures (`test_smt_fill_plot`) and sixteen errors (`test_smt_decouple_active`)
  are PRE-EXISTING. Anything else is a regression.
- `python -m pytest agent/facts agent/trader agent/test_kb_cutover.py
  tests/test_session_pipeline_graft.py -q` → **868 passed / 64 deselected in ~63s.**
  This is the DEFAULT suite and it is meant to stay near a minute; if it creeps over,
  find the new slow test rather than raising the bar.
- `… -m slow` → **64 tests, ~21 minutes, all passing.** `addopts` in `pyproject.toml`
  deselects them by default, which is how 36 of them silently broke between 2026-09-13
  and 2026-09-27 (plan 42). They are not optional extras: they are the tests that make
  the NUMBERS trustworthy — replay determinism (gate 1), replay-reproduces-live (gate 2),
  KB-edit-invalidates-the-recording (gate 4), plan-death-does-not-shorten-the-window
  (gate 6), facts no-lookahead, and the named cases. **Run `-m slow` ONLY when the
  operator explicitly asks for it** (operator decision 2026-09-30): it is never a
  prerequisite for an A/B verdict, a commit, a PR or a merge on its own. When a change
  touches an entry mechanism, the replay, thesis-cache or facts path, say in the report
  that the slow suite was not run, so the operator can ask for it. A behaviour change
  moves named-case figures: re-measure them per
  `docs/entry-mechanism-change-protocol.md` (precedent: `l2-mechanisms.md` §11.3).
  Thesis recordings are keyed per date x code version; when a gate skips or raises
  `NetworkCallRefused`, re-seed that date with `--seed` (one model call; 08-13 and 09-04
  were re-seeded 2026-09-27, 09-04 with `ACT_TRADER_BACKEND=anthropic`).
- Locked 1s regression baselines, run INDIVIDUALLY; the criterion is
  `events=PASS trades=PASS` (line-for-line against the locked recordings):
  `python regression.py --dates 2026-05-18 --mode 1s` → PASS (prints `pnl=1422.00`; was 1572.00)
  `python regression.py --dates 2026-05-19 --mode 1s` → PASS (prints `pnl=-1337.00`; was -367.00)
  The printed pnl moved while every trade still matches the locked recording; that
  reporting difference is not yet traced.

## Runtime flags

- `ACT_TRADER` is **ON by default**, and in the live process (`automation/main.py` ONLY)
  it **selects the brain** (plan 38): on = the AGENT owns the dispatcher — real
  `market-entry` / `market-close` orders through `automation/agent_dispatch.py` — and the
  legacy engine is DARK (`trader_only=True`); `ACT_TRADER=0` (or `false`/`no`/`off`) =
  LEGACY owns it, exactly as before. That is the rollback; there is no "agent observes"
  mode any more. Whether an order reaches the broker is still `LIVE_TRADING` /
  `DISCONNECTED`. `SessionPipeline`'s default, `signal_smt.py`, `backtest_smt.py`,
  `regression.py` and `run_replay` are unaffected.
- A live agent start is **REFUSED** — one `[AGENT-LIVE] REFUSED: <reason>` line, then NO
  brain trades, never a fallback to legacy — when `SMT_PIPELINE != v2`,
  `FORCE_RESET=true`, `ACT_AI_MODE=primary`, or the trader fails to build.
- `ACT_TRADER_BACKEND` unset = auto-select by key (`OPENROUTER_API_KEY` preferred, else
  `ANTHROPIC_API_KEY`), no longer a hard-coded `openrouter`. Leave it and
  `ACT_TRADER_MODEL` unset in live.
- `ACT_PER_TF_LEVEL_STATUS` is **ON by default** (plan 44, thesis.md §2.1g): per asset and
  side, the most extreme suppressed level with a mature 4h read gets that 4h read back in the
  L1 ledger (1h retired; S9 tags it `[PER-TF: ...]`). `0` (or `false`/`no`/`off`) restores the
  all-suppressed ledger; the S9 text differs, so flipping it re-keys the thesis cache.
- `ACT_THESIS_FAILSAFE` is **OFF by default**: a thesis call that fails validation on
  every retry becomes a code-built `ledger_fallback` thesis (verdict and `thesis_source`
  say so; never written to the thesis cache) instead of the NEUTRAL failsafe. `true`
  restores the failsafe. A clean model NEUTRAL is unaffected.
- `ACT_THESIS_TIEBREAK` is **ON by default**: the Analyzer turns a NEUTRAL thesis into a
  direction (`agent/trader/tiebreak.py`, applied AFTER the thesis cache; `thesis_source:
  "tiebreak"` + `tiebreak_rule`). `0` keeps NEUTRAL days dark.
- `ACT_PREMOVE_UNRELATED` (plan 46, §11.5 CANDIDATE) is **ON by default**: unset or empty
  = `on`; `0`/`false`/`no`/`off` = `off` (today's path unchanged, the rollback); `shadow`
  classifies and writes `premove_context.json` with no behaviour change. `on` forces the
  thesis against a BIG pre-09:20 leg not part of a bigger move (UNRELATED, no model call)
  — the DIRECTION only; the target stays the T2 pick. The leg-mid take-profit is a second
  switch, `premove_context.MID_TARGET_ENABLED`, default `False` (operator decision
  2026-09-30; a module attribute, not an env var). `premove_context.UNRELATED_PATH_MODE`
  overrides the env var for harnesses and tests. A plain replay does not load `.env`: set it
  in the shell for a replay.
- `ACT_EXTENSION_VETO` is **ON by default**: a market-mechanism fire priced more than
  `executor.EXTENSION_MAX_PTS` (100 pts) beyond the post-09:30 counter-extreme is not
  entered (`l2-mechanisms.md` §2; a `veto` record, reason `extension`; no attempt spent).
  `0` (or `false`/`no`/`off`) disables it. A plain replay does not load `.env`: set it in
  the shell for a replay.
- `ACT_STOP_BE` is **ON by default** (`l2-mechanisms.md` §8, 2026-10-02): once a position
  has covered half the way from its entry to T2 the stop moves to the entry (`stop_moved`
  reason `breakeven`; a touch is `stop_out_initial`, no attempt spent). `0` (or
  `false`/`no`/`off`) disables it; read once per Executor. The live port cannot move a
  stop yet, so live records `veto reason=trail_unwired` instead. The FVG one-behind trail
  in the same module is study code, `trail.TRAIL_FVG_MOVES = False`, not a rule.
- `ACT_SMT_WAIT_BLOCK` is **ON by default** (`l2-mechanisms.md` §11.6 CANDIDATE, operator
  decision 2026-10-03): a market-mechanism fire is blocked while exactly one of MNQ/MES is
  within 0.3 avg-1h-ranges of its London extreme (before either touches it post-09:30), and on
  the sweeping 1m bar plus the next after a one-asset touch (`veto` reasons `smt_wait_proximity`
  / `smt_wait_sweep`; no attempt spent; a blocked `tmso_reject` keeps its micro-session fire).
  `0` (or `false`/`no`/`off`) disables it. A plain replay does not load `.env`: set it in the
  shell for a replay.
- `TRADING_CONTRACTS` is also what `position.json["active"]["contracts"]` records.
- `ACT_TRADER_ARM_HHMM` overrides the 09:20 arm — fidelity fixtures only. `run_replay`
  restores it after a run; leaving it set re-arms every later run in the same process.
  Never set it in live: the arm is an exact-minute test, so a process started after 09:20
  ET is a dark day, and a live restart that finds `plans.json` stays dark too.
- `trade.py start --profile` (a flag, not an env var; forwarded orchestrator ->
  automation.main) profiles both processes (`automation/profiling.py`): RSS every 30s;
  tracemalloc (~2x slower) snapshots at 09:15:30 ET and 13:05:30 ET or at `trade.py
  terminate` (~5s freeze each), none otherwise inside the trading window.
- `ACT_THESIS_CACHE_DIR` redirects the thesis cache. Point it at a tmp dir for any test
  that would otherwise deposit a synthetic recording into `<global>/thesis_cache`, which
  every worktree reads.

## Never commit

`feature.md`'s deletion, `agent-optimizations.md`, `refinement-proposals.md`, and the
`manual-l1-thesis/` run directories. They are working state, not deliverables.
