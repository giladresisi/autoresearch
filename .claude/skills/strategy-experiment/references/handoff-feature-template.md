# Thin `feature.md` (handoff runbook) — template

The worktree's `feature.md` is **not** the spec — the spec lives in the Linear issue. `feature.md`
is a thin pointer + a runbook for the separate main agent who executes the experiment in this
worktree. Write it using this structure; replace every `<…>`.

````markdown
# Experiment: <short idea title>

**Linear issue (source of truth):** `<GIL-XX>` — <url>
**Worktree / branch:** `autoresearch/<tag>` (off `<base>` @ <sha>)
**Status:** set up — ready for a separate main agent to execute in THIS worktree.

> Read the Linear issue first. It holds the full context: problem, background, current code with
> `file.py:line` anchors, the requirements to implement, and the example occurrences. This file is
> just how to *proceed*.

---

## Runbook — you are the main agent running this experiment here

Work through the stages below **in order**. After **each** stage, post a concise comment to the
Linear issue (`<GIL-XX>`) summarizing what you did and what you found. When all stages are done,
**notify the user** (push notification) with the one-line verdict. Leave ALL changes **UNSTAGED** —
never commit, merge, or push.

- **Stage A — Plan.** Spawn the **`experiment-planner`** subagent on this `feature.md` (+ the Linear
  issue). It explores the code, assesses scope/complexity, and writes `.agents/plans/<slug>.md` —
  choosing the full `/plan-feature` skill (large/cross-cutting) or a lightweight sequential plan it
  writes directly (small/local), and stamping `EXECUTION_MODE: team|lightweight` + an `EXECUTOR
  DIRECTIVE` into the plan header. → Comment: the plan path, `EXECUTION_MODE`, and a one-line
  approach summary.
- **Stage B — Implement.** Spawn the **`plan-executor`** subagent on the plan, telling it to honor
  the plan's `EXECUTION_MODE`: `team` → use the `/execute` skill (planner used `/plan-feature`);
  `lightweight` → implement the sequential plan directly (no `/execute`). It runs its review
  pipeline; run the unit tests named in the issue; leave changes UNSTAGED. → Comment: what changed
  (files), test results.
- **Stage C — A/B regression.** Spawn the `regression-runner` agent in `ab-working-change` mode,
  `1s`, over the example day(s): <dates>. → Comment: baseline vs change `n_trades`/`pnl`, the
  event/trade diff verdict, and the chart paths.
- **Stage D — Verify the occurrences.** Spawn the `experiment-verifier` agent with this `feature.md`
  (for the `## Example Occurrences` table) + the baseline/change run dirs from Stage C. It writes
  `experiment-verification.md` here. → Comment: the per-occurrence PASS/FAIL verdict + whole-day
  P&L/trade delta.
- **Stage E — Notify.** Push a notification to the user: experiment finished, one-line verdict
  (did it do the desired thing at the example(s), and at what whole-day cost/benefit). Leave
  everything UNSTAGED; the user reviews and decides on merge.

---

## Example Occurrences

<the verbatim table — date | time (ET) | source | window | current behavior | desired behavior.
Kept here because the experiment-verifier parses it locally from this file.>
````
