---
name: strategy-experiment
description: >
  Runs an experiment that tests a trading-strategy idea against a PAST session in the auto-co-trader
  project, in one of two modes. DEFAULT = HANDOFF: create a new git worktree, write all context +
  example occurrences into a new Linear issue (via /create-issue), and write a thin `feature.md`
  runbook in the worktree, then hand off to a SEPARATE main agent who runs plan → implement → A/B
  regression → verification there and updates the issue. DIRECT mode (only when the user explicitly
  says to run it now / here / in this worktree / directly): NO new worktree — after creating the
  Linear issue and writing/overriding the local `feature.md`, the CURRENT agent runs ALL phases
  itself. Use this whenever the user wants to TRY / TEST / EXAMINE / VALIDATE / SET UP AN EXPERIMENT
  FOR a strategy or signal idea on a real historical session — e.g. "test this idea on yesterday's
  session", "set up an experiment for this SMT rule on 2026-06-10", "I'd like to try this now in
  this worktree using strategy-experiment" (→ direct), "have another agent try this in a separate
  worktree" (→ handoff). Trigger even if the user doesn't say "skill" or "experiment". REQUIRES at
  least one concrete, replayable example occurrence (a live-session date OR an explicit regression-run
  path on disk) — without one there is nothing to verify the effect against, and the skill must
  refuse to run. Do NOT use it for: a plain regression/backtest run with no idea to implement (use
  the regression-runner), session post-mortems (use session-analysis), or live/orchestrator ops.

---

# strategy-experiment

Test a strategy-behavior idea — **plus one or more concrete example occurrences** — against a real
historical session, end to end: spec it into a Linear issue, implement the change in isolation,
A/B-regress it on the example day(s), and verify at the exact occurrence timestamps whether the
desired effect happened (and what it did to the whole day).

It runs in **two modes**:

- **Handoff (default).** Create a new worktree + the Linear issue + a thin `feature.md` runbook,
  then **stop and hand off** to a separate main agent who runs the heavy stages in that worktree.
  Keeps each context clean and lets the long-running work happen in its own session.
- **Direct.** Only when the user explicitly asks to run it **now / here / in this worktree /
  directly**. No new worktree — the **current** agent creates the Linear issue, writes the local
  `feature.md`, and then runs **all** phases itself.

It never commits, merges, or pushes.

---

## Stage 0 — Gate on a concrete example occurrence (early-exit)

The payoff is a *verified* effect, which needs a real, replayable event to check against. Confirm
the invocation carries **at least one example occurrence**, each fully specified:

- **date** — the session day to replay (`YYYY-MM-DD`).
- **time (ET)** — the clock time of the occurrence (`HH:MM`, optionally `HH:MM:SS`).
- **source** — one of two verifiable forms:
  - `live-session:<YYYY-MM-DD>` → an existing `<global>/sessions/<date>/` folder containing
    `events.jsonl` (global root: `paths.global_root()`, default `~/projects/auto-co-trader/global`).
  - `regression-run:<absolute-path>` → an existing regression run dir containing
    `events_1s.jsonl`/`events.jsonl` + a trades file. The user must state the path explicitly.
- **current behavior** — what the strategy does today at that moment.
- **desired behavior** — what it should do instead after the change (the assertion to verify).
- **window** *(optional)* — inspection window around the time (default `time ± 8 min`).

**Refuse to run if the gate fails:** zero occurrences, a half-specified occurrence (missing any of
date/time/source/current/desired), or a `source` that does not exist on disk → stop and report
exactly why; do not create a worktree or a Linear issue. Verify each source exists before
continuing. Collect the distinct `date`s — they become the **A/B regression dates**, and each
`time`/`window` becomes a **verification probe**.

---

## Stage 1 — Pick the mode (direct vs handoff)

Decide from the invocation. **Default to handoff** when nothing is said about where/who runs it.

- **Direct** — the user explicitly indicated running it **now / here / in this worktree / by you /
  directly** (e.g. "I'd like to try this now in this worktree using strategy-experiment", "run it
  directly here", "do it in the current worktree now"). → No new worktree; the current agent runs
  every phase.
- **Handoff (default)** — the user asked for a **separate worktree / another agent** ("have another
  agent try this in a separate worktree"), OR said nothing about mode. → New worktree + issue +
  thin runbook, then hand off.

State which mode you picked and why before proceeding.

---

## Stage 2 — Ground the idea (and, handoff only, create the worktree)

Assemble the **request body**: the idea (what to change and why) + an `## Example Occurrences` table
built from Stage 0 (schema in `references/example-occurrences.md` — follow it verbatim; the verifier
parses this table). If the idea came from this conversation, write it to a request file first (e.g.
`requests/<slug>.md`) for a durable source.

- **Handoff:** invoke the **`strategy-feature`** skill with that request. It creates the worktree
  (via `/new-co-trader-worktree`, off the current branch — proceed with the current branch as base
  when warned you're not on `master`), copies the gitignored data + `.env`, and **grounds the idea
  in the codebase** (real `file.py:line` anchors + the backtest-vs-live path note). Treat the
  grounded `feature.md` it writes as your **draft context** for the Linear issue; you replace it in
  Stage 4. Record the worktree path `../<tag>` and branch `autoresearch/<tag>`.
- **Direct:** do **not** create a worktree — you work in the **current** worktree. Ground the idea
  inline (Explore/Grep/Read) so the Linear issue and `feature.md` cite real `file.py:line` anchors
  and the backtest-vs-live path.

---

## Stage 3 — Create the Linear issue (both modes; delegate to `/create-issue`)

Invoke the **`create-issue`** skill to open a Linear issue (project **`ai-trader`**, In Progress)
holding **all the context**: summary/problem + why it matters; background with the occurrences'
numbers/timestamps; current code & where the change lives (the `file.py:line` anchors + backtest-vs-
live note); requirements/recommendation; the full **example-occurrences table**; and the experiment
plan (plan → implement → A/B 1s regression on the example day(s) → verify). Capture the returned
**issue identifier (e.g. `GIL-42`) and URL** — Stage 4 writes them into `feature.md`.

---

## Stage 4 — Write / overwrite the local `feature.md` (both modes)

Write `feature.md` as a **thin pointer + runbook** (the full spec lives in the Linear issue) — in
the new worktree (handoff) or the current worktree (direct). Read
`references/handoff-feature-template.md` and follow it: header (Linear ID + URL as source of truth,
branch, status), the A–E runbook, and the `## Example Occurrences` table verbatim (the
`experiment-verifier` parses it locally from this file).

> **Overwrite any pre-existing `feature.md`.** A worktree checked out from the base branch often
> carries a **stale, tracked `feature.md`** from an earlier experiment — replace it **wholesale**,
> never append, never fail because it exists. The Write tool requires a Read first when a file
> exists: read the stale file (confirm it's an unrelated leftover), then write over it.

---

## Stage 5 — Branch on mode

### Handoff (default) — hand off, then STOP
Report and stop; do **not** run the stages yourself:
- worktree path + branch; the Linear issue ID + URL (the tracker); the `feature.md` runbook path.
- Next step for the user: start a separate main agent in the worktree —
  `cd ../<tag>` then `claude`, and ask it to "run the experiment per feature.md". It progresses
  through the stages, comments its findings on the Linear issue, and notifies the user when done.

Leave everything **unstaged**. The skill's job ends at the handoff.

### Direct — run all phases yourself (in the current worktree)
You are the agent running the experiment. Execute the `feature.md` runbook A→E here, and after
**each** stage post a concise comment to the Linear issue summarizing what you did/found:

- **A — Plan.** Spawn the **`experiment-planner`** subagent on `feature.md` (+ the Linear issue). It
  explores the code, assesses scope/complexity, and writes `.agents/plans/<slug>.md` — choosing the
  full `/plan-feature` skill (large/cross-cutting) or a lightweight sequential plan it writes directly
  (small/local), and stamping `EXECUTION_MODE: team|lightweight` + an `EXECUTOR DIRECTIVE` into the
  plan header.
- **B — Implement.** Spawn the **`plan-executor`** subagent on `.agents/plans/<slug>.md`, telling it
  to honor the plan's `EXECUTION_MODE`: `team` → use the `/execute` skill (the planner used
  `/plan-feature`); `lightweight` → implement the sequential plan directly (no `/execute`). It then
  runs its own review pipeline. Run the unit tests named in the issue; leave changes **UNSTAGED**.
- **C — A/B regression.** `regression-runner` agent, `ab-working-change` `1s`, on the example
  day(s). Capture baseline vs change run dirs, `n_trades`/`pnl`, diff verdict, chart paths.
- **D — Verify.** `experiment-verifier` agent with `feature.md` + the baseline/change run dirs →
  per-occurrence PASS/FAIL + whole-day delta; it writes `experiment-verification.md`.
- **E — Notify.** Notify the user (push notification) with the one-line verdict.

These stages are long-running (regression is CPU-heavy) — run each to completion in-turn rather than
backgrounding and returning early. Leave everything **unstaged**; the user reviews and decides on
merge. Do **not** commit/merge/push.

---

## Notes

- **One idea → one Linear issue → one experiment** (+ one worktree in handoff mode).
- **Keep scope faithful to the request.** If the idea is ambiguous about what to build, ask before
  Stage 2.
- **Default is handoff** — only go direct on an explicit "run it here/now/directly" signal.
