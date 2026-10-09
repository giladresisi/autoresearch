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

**Operator decision 2026-10-07: the pytest suites are no longer maintained.** Never run any
of them (default, `-m slow`, `tests/`, any subset) unless the operator explicitly asks, and
do not add or update tests alongside code changes. Do not delete the suites yet. The
baselines below are historical.

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
  `false`/`no`/`off`) disables it; read once per Executor. Live it reaches the broker as
  one `update-stop-loss` signal (`source: agent`, the same call as `trade.py update-sl`);
  a `stop_moved` record carrying `far_side` means the dispatcher did not confirm it — the
  model keeps the tighter stop and closes by market on a touch. The FVG one-behind trail
  in the same module is study code, `trail.TRAIL_FVG_MOVES = False`, not a rule.
- `ACT_SMT_WAIT_BLOCK` is **ON by default** (`l2-mechanisms.md` §11.6 CANDIDATE, operator
  decision 2026-10-03): a market-mechanism fire is blocked while exactly one of MNQ/MES is
  within 0.3 avg-1h-ranges of its London extreme (before either touches it post-09:30), and on
  the sweeping 1m bar plus the next after a one-asset touch (`veto` reasons `smt_wait_proximity`
  / `smt_wait_sweep`; no attempt spent; a blocked `tmso_reject` keeps its micro-session fire).
  `0` (or `false`/`no`/`off`) disables it. A plain replay does not load `.env`: set it in the
  shell for a replay.
- `ACT_MES_SWEEP_STOP` (`l2-mechanisms.md` §11.7 CANDIDATE, 2026-10-02) is **ON by
  default** (operator decision 2026-10-03): unset or empty = on; `0` (or
  `false`/`no`/`off`) is the rollback, read once per Executor. With a position open, MES
  taking its 24h-session extreme in the trade's direction with a 1m bar that opens before
  10:30 ET and closes at or inside it moves the MNQ stop, on that bar's close, to the
  completed MNQ 1m bar's close less 5 pts, if that tightens it and the new stop is at or
  beyond the entry (`stop_moved` reason `mes_sweep`; otherwise a `veto`
  `mes_sweep_not_in_profit` / `mes_sweep_not_tighter`). The stop is meant to
  be hit; its follow-up entry is `ACT_FOLLOWUP_ENTRY` below. Still a CANDIDATE, not a
  rule; a plain replay does not load `.env`, so set the rollback in the shell.
- `ACT_FOLLOWUP_ENTRY` (`l2-mechanisms.md` §11.8 CANDIDATE, plan 50, 2026-10-03) is **OFF
  by default** (operator decision 2026-10-05; parked: the forced-entry rig found 2 winners
  in 15 trades): unset or empty = off, so the sweep stop's profitable exit ends the plan at
  once; `1` (or `true`/`yes`/`on`) enables it, read once per Executor;
  `followup.FOLLOWUP_ENABLED` overrides it for a harness. When the first position's MES
  sweep stop is touched in profit with at least 30 pts (and 25% of the first leg) of T2
  left, the plan's `positive_close` death is DEFERRED for up to 20 min (never past 11:00 ET)
  and two triggers anchored at the retrace low may re-enter by market, past the 10:30
  cutoff: `followup_reject_close` (a fresh §7 machine) and `followup_fvg_1m`. Budget 2
  follow-up stop-outs (not counted in `attempts_used`); the regular mechanisms stay shut
  meanwhile (`entry_block` `followup_only`). Records `followup_opened` / `_skipped` /
  `_closed`. A plain replay does not load `.env`: set the rollback in the shell.
- `ACT_NYM_MID_REJECT` (`l2-mechanisms.md` §11.9 CANDIDATE, 2026-10-06) is **ON by default**
  (`nym_mid_reject.DEFAULT_ENABLED`, one constant; operator decision 2026-10-07):
  unset or empty = the default; `0`/`false`/`no`/`off` = off (the bar loop and the plan's
  `armed_classes` exactly as before); `1`/`true`/`yes`/`on` = on; read once per Executor
  (and by the Planner at the arm); `nym_mid_reject.NYM_MID_REJECT_ENABLED` overrides it for a
  harness. A market mechanism: the 09:30 open on the thesis side of the MNQ NY-morning mid
  (06:00 range high+low / 2, recomputed every 1m close), a bar trading through the mid
  against the thesis, and that bar or the next closing back across it with the thesis
  against its own open -> enter at that close, stop 20 pts; one FILL a day; sweep bars
  from 09:31 (the 09:30 opening bar is excluded, operator 2026-10-07), no fire at/after
  10:30; every market-mechanism gate and veto applies. Same bar as `tmso_reject` = one entry,
  `nym_mid_reject`'s, with the bigger stop (`also_fired` on the fill). A plain replay does
  not load `.env`: set it in the shell for a replay.
- `ACT_SEC7_QUIET_AFTER_STOP` (`l2-mechanisms.md` §11.11 CANDIDATE, 2026-10-09) is **ON by
  default** (operator decision 2026-10-09): unset or empty = on; `0` (or `false`/`no`/`off`)
  is the rollback, read once per Executor. Once an `extreme_reject_close` position stops out,
  §7 enters nothing more for the rest of the plan (its fires become `veto` records, reason
  `sec7_quiet_after_stop`; no attempt spent). Separately, `tmso_reject` and
  `extreme_reject_close` stop at 10:30 (`executor.MECHANISM_CUTOFF_ET`) while the shared
  entry cutoff is 11:00. A plain replay does not load `.env`: set the rollback in the shell.
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

## R2 data sync (plan 50)

`trade.py publish` / `trade.py sync` move `<global>` (main, live, sessions, thesis cache) to/from a
private R2 bucket (`scripts/r2_sync.py`; `R2_*` in `.env`). `promote`, `rollover-prep` and the
parquet-check session-end publish automatically; session-analysis Step 0 runs `sync`. Baton rule:
a parquet decides by LAST INDEX TIMESTAMP, so `sync` never overwrites a local file that is ahead of
R2, a truncated local copy is replaced (old one kept as `.r2prev`), and a two-sided move is a
CONFLICT (exit 1; `--force` picks a side). Both commands refuse while the orchestrator runs.
Plan 51 (two machines, one live at a time): with R2 configured, `trade.py start` first checks stale
data (`sync --dry-run` of main/live would download or conflict), HEAD (the `live` manifest's git_sha
must be in this checkout; tracked files clean) and the R2 live lock `live_owner.json` (other host
holds it = refused; same host = restart allowed), then acquires the lock. Fail closed when R2 is
unreachable. `promote` / `rollover-prep` / session-end release the lock after a good publish.
Overrides are their own flags, never `--force`: `start --take-over`, `start --skip-r2-checks`;
`trade.py live-lock [status|release|take-over]`. Host name: optional `ACT_HOST_ID`.

## Cross-platform code (Windows, macOS, Linux)

This project runs on all three platforms (Windows now, a Mac next, a Linux VPS later). All new and
changed code must work on all three: no Windows-only or POSIX-only calls outside
`platform_compat.py` (the single OS switch), `pathlib` for paths, no hard-coded drive letters or
`\` separators, no shell syntax tied to one OS, explicit `ZoneInfo` instead of the machine
timezone. A test that needs the other OS must simulate it, not skip.

## macOS (plan 52)

`platform_compat.py` is the ONLY OS switch: `is_python_process_name` (matches `python3.12`, `Python`,
`python.exe`), `detached_popen_kwargs` (`trade.py start`: `creationflags` on Windows, `start_new_session`
on POSIX) and `prevent_idle_sleep` (Windows execution state / macOS `caffeinate -dimsu -w <orchestrator
pid>`, started once by the orchestrator). Do not add `os.name` / `sys.platform` branches elsewhere.
Stop is file-based (platform-independent); SIGTERM has no handler, so `finally` blocks do not run on
`terminate`, same as the Windows hard kill. First-run checklist for the Mac: plan 52 section 6
(`.agents/plans/52.macos-posix-live-support.md`): .env by hand, one IB login per account, AC power,
smoke `start`, confirm one orchestrator + one automation.main + a `caffeinate` child, `terminate` leaves none.

## Never commit

`feature.md`'s deletion, `agent-optimizations.md`, `refinement-proposals.md`, and the
`manual-l1-thesis/` run directories. They are working state, not deliverables.
