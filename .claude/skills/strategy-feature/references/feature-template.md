# feature.md template

Write `feature.md` in the new worktree using this exact section structure. Replace every
`<…>` placeholder. Keep the prose tight and concrete; ground claims in real `file.py:line`
anchors found during codebase research. The reader is a fresh agent who has only this file.

The annotated skeleton below mirrors the project's standard strategy-feature spec. Italic
`(guidance)` lines are instructions to you — do not copy them into the output.

---

````markdown
# Feature: <short feature title>

**Branch:** `autoresearch/<tag>` (worktree off `<base-branch>` @ <short-sha>)
**Status:** spec only — NOT yet implemented. Implement, test, and commit per this file.
**Owner of execution:** a separate agent invoked in THIS worktree.
**Must be backtested before merge to production (`live`/`master`).**
**Planning:** before writing any code, turn this spec into a detailed implementation plan
using the `/plan-feature` skill (it does the codebase analysis + execution strategy). This
`feature.md` is the requirements/spec; `/plan-feature` produces the step-by-step plan file.

Source of this spec: <the request origin — e.g. "the `live` worktree's
`sessions/<date>/comments.md`, note '<note title>'", or "inline request to the
strategy-feature skill on <date>">.

---

## 1. Background — why this change

<(guidance) 1–2 short paragraphs + bullets: the motivating behavior, incident, or goal.
Include concrete numbers/timestamps from the request if it had them. Explain the problem this
change solves so the implementer understands intent, not just mechanics.>

---

## 2. Current code (what we're changing)

<(guidance) Map the relevant current implementation with `file.py:line` anchors: the
functions, the constants/params, the signal kinds, the state files. Where it matters, note
the BACKTEST-vs-LIVE path — backtests run through `SimulatedBrokerExecutor` via
`regression.py` → `backtest_smt.run_backtest_v2`; live runs through `PickMyTradeExecutor`.
A change must live where the backtest exercises it (usually the strategy/signal layer:
`strategy.py`, `trend.py`, `hypothesis.py`, `daily.py`, `session_pipeline.py`) to be
backtestable, and where state (`data/position.json`, `data/hypothesis.json`) is owned to stay
in sync.>

---

## 3. Requirements to implement

<(guidance) The concrete changes, numbered. For each: what to change, where (file:line), and
the directional/edge-case logic. If the request listed multiple items, give each its own
numbered requirement. Be precise enough that the implementer doesn't have to re-derive the
design, but leave genuine judgment calls to section 4.>

### Requirement 1 — <title>
<details + code anchors>

### Requirement 2 — <title>
<details + code anchors>

<…more as needed…>

---

## 4. Open design decisions (settle before/while implementing)
<(guidance) The judgment calls: exact constants/curves, where a new counter/field lives,
naming of new event `kind`s for plotting/debugging, cadence (1m vs 1s), interactions with
existing guards. List them so they're decided deliberately, not silently.>

---

## 5. Commit plan (separate commits)
<(guidance) Ordered, one logical change per commit, with conventional-commit subjects. Note
"suite green after each commit". Add a final plotting/logging commit if new signals are
introduced.>

1. `<type(scope): subject>` — <what>
2. `<type(scope): subject>` — <what>

---

## 6. Testing & backtest validation
<(guidance)>
- **Unit tests** (name the file, e.g. `tests/test_smt_strategy_v2.py`): list the specific
  cases to add — happy path, the primary edge/error path, and a no-op/regression check that
  unrelated behavior is unchanged. Name each case; unnamed cases get skipped.
- **Backtest / regression:** the worktree already has `data/*.parquet` and
  `data/regression/<date>/` copied. Run `regression.py` before/after over a window that
  includes the motivating session plus a broader sample; compare trade count, P&L, win rate,
  and the metric the change targets. Tune any new constants from results. First `uv run`
  builds the worktree venv automatically.
- Establish a baseline: run the full suite BEFORE changes and record pass/fail.
- Do not merge to `live`/`master` until the user approves the backtest results.

---

## 7. Acceptance criteria
<(guidance) A checklist mirroring the requirements + tests-green + backtest-approved.>
- [ ] <criterion>
- [ ] New unit tests pass; full suite green after each commit.
- [ ] Backtest completed and shared; constants tuned; user approval before any merge.

---

## 8. Out of scope (track separately, not here)
<(guidance) Related-but-separate items so the change stays focused — name them and where they
live (other branches/notes).>
````
