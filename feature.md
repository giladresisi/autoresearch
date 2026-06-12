# Experiment: SMT V2 relevance filter — sweep-confirmation gate + wick/body collapse

**Linear issue (source of truth):** `GIL-17` — https://linear.app/gilad-resisi/issue/GIL-17/smt-v2-relevance-filter-sweep-confirmation-gate-wickbody-collapse
**Worktree / branch:** `autoresearch/entry-stuff` (DIRECT mode — current worktree, off HEAD @ b4a4498)
**Status:** set up — running DIRECT (the current agent executes all stages in THIS worktree).

> Read the Linear issue (GIL-17) first. It holds the full context: problem, the June 3 evidence
> (gap-vs-trend cohorts + price arc), current code with `file.py:line` anchors, the two structural
> changes to implement, the verification approach, and the example occurrences. This file is just
> how to *proceed*.

---

## Runbook — DIRECT mode (current agent runs this experiment here)

Work through the stages **in order**. After **each** stage, post a concise comment to GIL-17. When
all stages are done, **notify the user** (push notification) with the one-line verdict. Leave ALL
changes **UNSTAGED** — never commit, merge, or push.

- **Stage A — Plan.** Spawn the **`experiment-planner`** subagent on this `feature.md` + GIL-17. It
  explores the code, assesses scope, and writes `.agents/plans/<slug>.md` with `EXECUTION_MODE:
  team|lightweight` + an `EXECUTOR DIRECTIVE`. Expected: lightweight (local to `hypothesis.py` pure
  functions + `tests/test_smt_relevance.py`). → Comment: plan path, `EXECUTION_MODE`, one-line approach.
- **Stage B — Implement.** Spawn the **`plan-executor`** subagent on the plan, honoring its
  `EXECUTION_MODE`. It runs its review pipeline; run `tests/test_smt_relevance.py` (+ `tests/` quick
  full run, `-m 'not integration'`); leave changes UNSTAGED. → Comment: files changed, test results.
- **Stage C — A/B regression (no-regression sanity).** Spawn the `regression-runner` agent in
  `ab-working-change` mode, `1s`, on **2026-06-03**. The filter is shadow → expect **IDENTICAL
  events/trades, P&L Δ=0**. A DIFFER verdict here is a RED FLAG (means the change leaked into
  behavior). → Comment: baseline vs change `n_trades`/`pnl`, diff verdict (must be IDENTICAL), chart paths.
- **Stage D — Verify (decision-quality scorecard — the real signal).** This experiment is NOT
  verified by P&L. Build/extend the offline harness (see `C:\Users\gilad\AppData\Local\Temp\smt_phase3_preview.py`
  and `..\june3_prox.py` as references) that replays the run's 35-SMT stream through BOTH the OLD
  filter (HEAD `ingest_smts`/`dominant`) and the NEW filter, scoring each SMT's direction against the
  next-30m move (RIGHT/wrong) and recording each filter's admit/suppress + was-dominant decision. A
  decision is **correct** when it admits a RIGHT SMT or suppresses a wrong one. Produce a per-SMT
  old-vs-new table flagged **better/worse/same**, plus a whole-session tally, and check the 3 example
  occurrences below (suppress #1, keep-dominant #2, collapse #3). Write `experiment-verification.md`.
  → Comment: per-occurrence PASS/FAIL + the old-vs-new correct-decision tally.
- **Stage E — Notify.** Push a notification: one-line verdict (did the new filter suppress the
  premature reversals while keeping the at-level sweeps; net better/worse/same decision count; A/B
  confirmed flat). Leave everything UNSTAGED; the user reviews and decides on merge.

**Success criterion:** new filter strictly improves the correct-decision count (suppresses the
premature-reversal cohort, keeps the at-level sweep cohort) with ZERO regression on the guardrail
(day_high DOWN @09:31 must stay admitted + dominant), and the A/B is byte-identical.

---

## Example Occurrences

| # | date | time (ET) | source | window | current behavior | desired behavior |
|---|------|-----------|--------|--------|------------------|------------------|
| 1 | 2026-06-03 | 09:40 | regression-run:C:\Users\gilad\projects\auto-co-trader\entry-stuff\regression\sessions\2026-06-03\17-53-08 | 09:32–09:50 | day_low UP wick+body SMTs (#18-21) admitted while price (~30700) is ~140pt above the day_low and still falling toward the 09:50 low (30496); can drive an UP hypothesis prematurely (next-30m −32…−76, wrong). | Sweep-confirmation gate SUPPRESSES these UP SMTs (price has not reached the low extreme + momentum is down). Eligible only after price sweeps the 09:50 low (#22-27, fired at/below the low, admit). |
| 2 | 2026-06-03 | 09:31 | regression-run:C:\Users\gilad\projects\auto-co-trader\entry-stuff\regression\sessions\2026-06-03\17-53-08 | 09:23–09:39 | day_high DOWN wick SMT (#17) admitted and dominant; fired at the high (gap +5) and preceded the −196 crash. | Guardrail: gate must STILL admit and keep this SMT dominant (sweep confirmed). Must NOT over-suppress the best signal — same-or-better decision. |
| 3 | 2026-06-03 | 05:22 | regression-run:C:\Users\gilad\projects\auto-co-trader\entry-stuff\regression\sessions\2026-06-03\17-53-08 | 05:14–05:30 | week_high fires both a wick (#10) and a body (#12) divergence within 5s → two separate active-set records (week_high\|down\|wick and week_high\|down\|body). | Collapse into ONE logical SMT per (ref_name, direction): one week_high-down member with wick as confirmation strength; the active set holds one member, not two. |
