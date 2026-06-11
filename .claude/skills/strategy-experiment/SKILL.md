---
name: strategy-experiment
description: >
  End-to-end harness for testing a trading-strategy idea against a PAST session in an isolated
  git worktree for the auto-co-trader project: it specs the idea, adaptively plans + implements
  the change, A/B-regresses it on the example day(s), and verifies — at the exact timestamps of
  the supplied example occurrences — whether the change produced the desired effect (and what it
  did to the whole day). Use this whenever the user wants to TRY / TEST / EXAMINE / VALIDATE a
  strategy or signal idea on a real historical session before shipping it — phrases like "test
  this idea on yesterday's session", "examine whether this change fixes the 14:50 occurrence",
  "set up an experiment for this SMT rule and verify it on 2026-06-10", "A/B this idea and check
  the example times", "run this strategy tweak through a worktree and confirm the effect", or any
  request that pairs a strategy-behavior idea with one or more concrete example occurrences to
  prove it against. Trigger even if the user doesn't say "skill" or "experiment". This skill
  REQUIRES at least one concrete, replayable example occurrence (a live session date OR an explicit
  regression-run path on disk) — without one there is nothing to verify the effect against, and the
  skill must refuse to run. Do NOT use it for: a plain regression/backtest run with no idea to
  implement (use the regression-runner), session post-mortems (use session-analysis), just specing
  an idea without testing it (use strategy-feature), or live/orchestrator operations.
---

# strategy-experiment

Turn a strategy-behavior idea **plus one or more concrete example occurrences** into a finished,
self-contained experiment: an isolated worktree holding the implemented change, an A/B regression
on the example day(s), and a verification report that says — per occurrence — whether the desired
behavior actually happened, and what the change did to the full session P&L.

This skill is an **orchestrator**. It does not re-implement work that existing skills/agents already
do well; it sequences them and adds the one thing none of them do: **timestamp-anchored
before/after verification tied to real example occurrences.** That verification is the whole point,
which is why a concrete, replayable example is mandatory — see Stage 0.

The loop ends with an unstaged change in the worktree and a verdict for you to review. It never
commits, merges, or pushes. You decide whether the result is worth keeping.

---

## Stage 0 — Gate on a concrete example occurrence (early-exit)

The deliverable is a *verified* effect. You can only verify an effect against a real, replayable
event. So before doing anything else, confirm the invocation carries **at least one example
occurrence**, each fully specified:

- **date** — the session day to replay (`YYYY-MM-DD`).
- **time (ET)** — the clock time of the occurrence (`HH:MM`, optionally `HH:MM:SS`).
- **source** — where this occurrence was observed, in one of two verifiable forms:
  - `live-session:<YYYY-MM-DD>` → must resolve to an existing `<global>/sessions/<date>/` folder
    containing `events.jsonl` (global root: `paths.global_root()`, default
    `~/projects/auto-co-trader/global`).
  - `regression-run:<absolute-path>` → must be an existing regression run directory on this
    machine containing `events_1s.jsonl`/`events.jsonl` + a trades file. The user must state the
    path explicitly — do not guess it.
- **current behavior** — what the strategy does today at that moment (the thing being fixed).
- **desired behavior** — what it should do instead after the change (the assertion to verify).
- **window** *(optional)* — the inspection window around the time (default `time ± 8 min`).

**Refuse to run if the gate fails.** Specifically:

- Zero occurrences supplied → stop. Tell the user the skill needs at least one concrete example
  to verify against, and show the required fields above. Do **not** create a worktree.
- An occurrence is missing `date`, `time`, `source`, `current`, or `desired` → ask the user to
  complete it; do not proceed with a half-specified occurrence.
- A `source` path/folder does **not exist** on disk → stop and report exactly which source was not
  found. A non-existent source cannot be replayed or inspected, so the experiment is unverifiable.

Verify each source exists before continuing (e.g. `test -f <global>/sessions/<date>/events.jsonl`
for a live session; `test -d <path>` + a trades file for a regression run). Only proceed once every
occurrence is complete and its source is confirmed present.

> Why this is strict: an experiment whose effect can't be checked at a real occurrence is just an
> untested code change. The example is what separates this skill from `strategy-feature`.

Collect the distinct `date`s — they become the **A/B regression dates** in Stage 4, and each
occurrence's `time`/`window` becomes a **verification probe** in Stage 5.

---

## Stage 1 — Spec + worktree (delegate to `strategy-feature`)

Assemble the **request body**: the idea (what behavior to change and why) followed by an
`## Example Occurrences` table built from Stage 0 (the exact schema is in
`references/example-occurrences.md` — read it and follow it verbatim, because Stage 5's verifier
parses this table). If the idea came from this conversation, write it to a request file first
(e.g. `requests/<slug>.md`) so there is a durable source; if the user pointed at an existing
file/section, use that.

Invoke the **`strategy-feature`** skill with that request. It creates the worktree (via
`/new-co-trader-worktree`, based off the current branch — proceed with the current branch as base
when it warns you're not on `master`), copies the gitignored data + `.env`, grounds the spec in
real `file.py:line` anchors with the backtest-vs-live path note, and writes a self-contained
`feature.md`.

> **Overwrite any pre-existing `feature.md`.** The worktree is a checkout of the base branch,
> which often carries a **stale, tracked `feature.md`** from an earlier experiment. That is
> expected — this experiment's `feature.md` must **replace it wholesale**, never append to or
> merge with it, and never fail because it already exists. The Write tool requires a Read first
> when a file exists: read the stale file (to confirm it's an unrelated leftover), then write the
> new spec over it, replacing the entire contents.

After `strategy-feature` returns, **ensure `feature.md` contains the `## Example Occurrences`
table verbatim** (append it if `strategy-feature` did not carry it through). This table is the
contract Stage 5 reads — the worktree must be self-describing.

Record: worktree path `../<tag>`, branch `autoresearch/<tag>`, and the `feature.md` path. **All
subsequent work happens inside the worktree.**

---

## Stage 2 — Adaptive planning (signal the execution weight)

Spawn a planning agent (`gsd-planner`, or a `general-purpose` agent carrying the `/plan-feature`
methodology) to read `feature.md`, explore the real code touchpoints, and **assess complexity**,
then choose the plan shape and stamp an execution directive:

- **Lightweight / sequential** when the change is small and local: ≤2 files, modifies existing
  logic only (no new modules/interfaces/state-schema), unit-testable in isolation, doesn't fork
  the backtest and live paths. The typical signal-rule tweak.
- **Full `/plan-feature`** when the change is genuinely large: new modules/interfaces, ≥3 files
  across subsystems, state-file/schema changes, a real parallelizable surface, or it must live in
  **both** the backtest (`SimulatedBrokerExecutor` via `regression.py` → `backtest_smt.run_backtest_v2`)
  and live executor paths. When delegating to `/plan-feature`, **pass a non-interactive marker**
  (e.g. `[non-interactive] auto-approve acceptance criteria — invoked indirectly by
  strategy-experiment`) so its acceptance-criteria step auto-approves the proposed criteria instead
  of blocking on a user prompt — this harness runs unattended.

Output: `.agents/plans/<slug>.md` whose header carries **`EXECUTION_MODE: lightweight`** or
**`EXECUTION_MODE: team`** plus a one-line rationale. This is the planner→executor signal — the
planner owns the decision and records it in the plan, so the next stage just reads it.

> Don't force the heavy machinery. Most strategy-rule changes are a single-file edit + a unit test
> + a regression check; making a 4-agent team design that is pure friction. The gate exists so the
> weight matches the change.

---

## Stage 3 — Adaptive execution (route on `EXECUTION_MODE`)

Read the plan's `EXECUTION_MODE` and route:

- `lightweight` → spawn the **`plan-executor`** agent on `.agents/plans/<slug>.md`. It implements
  the plan sequentially, then runs its own review pipeline (code-review →
  acceptance-criteria-validate → execution-report) and fixes genuine issues. Leaves all changes
  **unstaged**.
- `team` → invoke the **`/execute`** skill on the plan (team-based parallel waves). Same
  no-commit / unstaged contract.

Either way: **no `git add` / commit / push / merge.** The change stays unstaged in the worktree so
it can be A/B-regressed cleanly and discarded if it doesn't pan out.

---

## Stage 4 — A/B regression on the example day(s) (delegate to `regression-runner`)

Spawn the **`regression-runner`** agent in **`ab-working-change`** mode, **`1s`**, over the Stage-0
dates. This compares the worktree's change (WITH) against a clean HEAD baseline (WITHOUT) on the
*same* day — the only way to attribute an effect to the change.

Require from its report (per date): both run directories (baseline + change), per-side
`n_trades` + `pnl`, the event/trade diff verdict, and the chart paths. If the runner reports the
two sides are byte-identical, the change is behavior-neutral on that day — surface that prominently,
because it usually means the new rule never engaged at the occurrence (a likely Stage-5 FAIL).

---

## Stage 5 — Timestamp verification (delegate to `experiment-verifier`)

Spawn the **`experiment-verifier`** agent with: the `feature.md` path (for the
`## Example Occurrences` table) and the baseline + change regression run dirs per date. For each
occurrence it slices the `window` around `time` in both runs' `events`/`trades`, and checks whether
the **desired behavior** now holds in the change run where the **current behavior** held in the
baseline — i.e. it confirms the change *caused* the intended difference at that exact moment. It
also reports the whole-day `n_trades`/`pnl` delta per date.

Output: a written `experiment-verification.md` in the worktree, plus a returned verdict —
per-occurrence **PASS/FAIL with evidence** and the full-day delta.

---

## Stage 6 — Hand off (no merge)

Report concisely to the user:

- Worktree path + branch; `feature.md`, plan (+ `EXECUTION_MODE`), and `experiment-verification.md`
  paths.
- The per-occurrence PASS/FAIL verdict and the whole-day P&L/trade delta per date.
- The A/B chart paths so they can eyeball the effect.
- A one-line bottom line: did the idea do what it was meant to at the example(s), and at what
  whole-day cost/benefit.

Then stop. Leave everything **unstaged**; the user reviews the evidence and decides whether to
keep, refine, or discard. Only commit/merge if they explicitly ask in a later request.

---

## Notes

- **One idea → one worktree → one experiment.** For several independent ideas, run the skill once
  per idea so each gets its own isolated, verifiable branch.
- **Keep scope faithful to the request.** Don't expand the idea; if it's ambiguous about what to
  build, ask before Stage 1.
- **Long-running.** Stages 3–4 can take many minutes (regression is CPU-heavy). The delegated
  agents run their work to completion in-turn; let them finish rather than backgrounding and
  returning early.
- **Multiple occurrences across multiple days** are fine — Stage 4 loops the dates and Stage 5
  probes every occurrence. More occurrences = a stronger verdict.
