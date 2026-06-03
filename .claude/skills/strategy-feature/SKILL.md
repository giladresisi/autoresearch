---
name: strategy-feature
description: Turn a strategy-change request for the auto-co-trader project into a ready-to-backtest git worktree that contains a structured feature.md spec — and it stops there; it never writes the implementation. Use this whenever the user wants to capture, scope, spec out, write up, or "turn into a feature/worktree" a change to the trading strategy — e.g. "create a strategy-feature for X", "make a feature worktree for this cautious-target change", "spec this strategy update", "turn this note/file into a feature.md so another agent can build + backtest it", or when they point to a request file/section to be turned into a spec. Trigger even if the user doesn't say the word "skill". Quick contrast on the word "feature.md": "turn this note/request INTO a feature.md", "scope this", or "spec this change" → USE this skill; but "IMPLEMENT the changes in feature.md", "build the feature", "write the code", or "commit and run the tests" → do NOT use it. Do NOT trigger when the user wants to IMPLEMENT or execute an already-specced change — writing the code, committing, or running the tests for an existing feature.md or plan is out of scope (that happens later, inside the worktree, via /plan-feature and a coding agent). Also do NOT use it for non-strategy tasks: data maintenance, plotting, running the orchestrator, session analysis, fetching strategies into the registry, or regression/optimization runs.
---

# Strategy Feature

Turn a strategy-change request into (a) an isolated git worktree with the gitignored data
copied in (ready for backtesting + git ops) and (b) a `feature.md` spec inside it, written in
the project's standard feature-spec format so a separate agent can implement and backtest it.

The point of the worktree is **safety and backtestability**: strategy/execution code must be
backtested before it reaches production, and editing it in the live working directory is
unsafe (the live `automation.main` re-imports modules from disk on restart). So this skill
never edits strategy code — it only produces the worktree + spec.

## Step 1 — Resolve the request, then gate (early-exit)

Figure out **where the request body lives**, then validate it. There are two cases.

**A. A file is named in the invocation** (e.g. "implement the cautious-targets note in
`sessions/2026-06-03/comments.md`", "from `requests/foo.md`", "per `docs/change.md` section
X"):
- If the file does not exist → **exit early**: tell the user the specified file was not found
  and stop. Do not create a worktree.
- Read the file and locate the **specific item/section** the request points to (a named note,
  heading, or section). If that item is not present in the file → **exit early**: tell the
  user the file doesn't contain the referenced item and stop.
- Otherwise the **request body** = that item/section's content.

**B. No file is named** → the **inline text of the invocation request** is the request body.
- If there is no inline request, or the text is not actually a strategy-change request (it's
  empty, a greeting, or unrelated) → **exit early**: tell the user no strategy-change request
  was found and stop.

Only proceed past this gate when you have a concrete request body to turn into a spec.

## Step 2 — Ground the spec in the codebase

A good `feature.md` cites real code anchors (`file.py:line`) so the implementing agent isn't
starting from zero. Before writing it, investigate the current codebase for the areas the
request touches: the functions, constants, signal kinds, and state files involved. Use Grep
/ Read to find exact locations. Capture:
- where the relevant logic lives now (files + line ranges),
- the constants / parameters being changed,
- which executor/pipeline path runs in **backtest** vs **live** (this affects where a change
  must live to be backtestable — backtests run through `SimulatedBrokerExecutor` via
  `regression.py` → `backtest_smt.run_backtest_v2`, not the live `PickMyTradeExecutor`),
- any state files (`data/position.json`, `data/hypothesis.json`, `data/daily.json`) the change
  reads or writes.

Do this research in the **current** working directory (before the worktree exists). You'll
fold the findings into the spec's "Current code" and "Requirements" sections.

## Step 3 — Create the worktree

Invoke the `/new-co-trader-worktree` skill (the `ai-dev-env:new-co-trader-worktree` skill) to
create the worktree, basing it off the **current branch** (pass a short descriptive purpose
derived from the request so it names the worktree sensibly). When that skill notes you're not
on `master`, choose to **proceed with the current branch as the base** — strategy features
are normally branched off whatever branch the request came from (often `live`).

That skill copies the gitignored `data/*.parquet` and `data/regression/<date>/` folders into
the worktree. After it finishes, **also copy `.env`** if present (it isn't copied by
`/new-co-trader-worktree`, and backtest/git tooling may need it):

```bash
[ -f .env ] && cp .env ../<tag>/.env && echo ".env copied"
```

Note the new worktree path (`../<tag>`) and branch (`autoresearch/<tag>`) it reports — you
write `feature.md` there next.

## Step 4 — Write feature.md in the new worktree

Read `references/feature-template.md` (in this skill's directory) and write
`../<tag>/feature.md` following it exactly. Fill every section from the request body (Step 1)
and your codebase research (Step 2). The spec must be **self-contained** — a fresh agent
opening only `feature.md` should understand the what, where, and how, and be able to
implement, test, and backtest without re-deriving context.

Required sections (see the template for the full annotated structure):
1. **Header** — branch, "spec only" status, who executes, "must be backtested before merge",
   a **Planning** line instructing the executing agent to turn this spec into a detailed
   implementation plan with the `/plan-feature` skill before writing code, and a pointer to
   the original request source (file+section, or that it was inline).
2. **Background** — why this change; the motivating behavior/incident.
3. **Current code** — what exists today, with `file.py:line` anchors, incl. the backtest-vs-live
   path note where relevant.
4. **Requirements to implement** — the concrete changes, each grounded in code locations.
5. **Open design decisions** — the judgment calls to settle while implementing.
6. **Commit plan** — separate, ordered commits (one logical change each).
7. **Testing & backtest validation** — unit tests to add (name the cases), and the
   before/after `regression.py` procedure (the copied data is already in place).
8. **Acceptance criteria** — a checklist.
9. **Out of scope** — related-but-separate items, so the change stays focused.

Do **not** implement any code changes, and do **not** commit `feature.md` unless the user
explicitly asks — leave it untracked so the executing agent commits it with their work.

## Step 5 — Hand off

Report concisely: the worktree path + branch, that data/.env are in place, the `feature.md`
path, and a one-line summary of the spec. Then give the open-in-worktree instructions:

```
cd ../<tag>
claude
```
…and an example follow-up: "Implement the changes in feature.md, commit each separately, run
the tests." Mention the first `uv run` builds the worktree venv automatically, and that the
branch is based off the current branch (so it'll need merging onto production after backtest).

## Notes
- Keep the worktree creation and the spec faithful to the request — don't expand scope.
  If the request is ambiguous about what to build, ask the user before writing the spec.
- One request → one worktree → one `feature.md`. For multiple independent changes, run the
  skill once per change so each gets its own isolated, backtestable branch.
