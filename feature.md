# Experiment: SMT conviction-seed (warmup + carry, incl. fills) + suppress weekly-mid trend-broken

**Linear issue (source of truth):** `GIL-39` — https://linear.app/gilad-resisi/issue/GIL-39
**Worktree / branch:** `autoresearch/smt-conv-seed-wkmid-trendbroken` (off `origin/master` @ e9272a3)
**Status:** set up — ready for a separate main agent to execute in THIS worktree.

> Read GIL-39 first. It holds the full context: both changes' problems, the `file.py:line`
> anchors, the fix recommendations, the regression-testability nuance, and the example
> occurrences. This file is just how to *proceed*.

**Two flag-gated changes, ONE combined 1s A/B screening run (with plots).** The user will
co-analyze the chain of events to attribute each changed trade to Change A vs Change B (they
fire via different mechanisms at different times), then decide which graduates to full
regression — judged by causal reasoning, NOT purely P&L deltas.

- **Change A** — seed the standing-SMT conviction set from still-standing (unfulfilled, not
  invalidated, not depleted) SMTs **and fills** so they affect hypothesis direction via the
  existing rule2b conviction override. New `smt_conviction.seed_from_standing(...)`, called
  AFTER the force-reset `save_hypothesis` and BEFORE `run_hypothesis` (cold AND warm starts).
  Survivor source: `_detect_state_pending_entries` + `_detect_state_fill_entries`
  (`session_pipeline.py` ~2447/2507). **Flag-gate it.**
- **Change B** — suppress the trend-broken signal emitted on a weekly-mid cross
  (`session_pipeline.py:1345` mid-invalidation path; `trend.py:259-272` `_weekly_mid_cross_guard`).
  **Flag-gate it independently** (so A and B can be ablated separately if needed).

> **CRITICAL nuance for Change A:** the warmup-late-start facet is **LIVE-ONLY** and will NOT
> show in a full-day regression (replay builds conviction from session open). The
> **cross-session carry-seed** facet (carried prior-session unfulfilled SMTs/fills seeding
> conviction at each day's OPEN) **is** regression-testable — inspect the session-open
> hypothesis. Don't conclude "no effect" without checking the open. `conviction_score` is a
> consensus ratio, so an all-bullish carried set → +1.0 → can flip rule2b; consider a gate
> (e.g. `top_tier>=day` OR `>=2` concurring records) so one weak session-tier fill can't flip a day.

---

## Runbook — you are the main agent running this experiment here

Work the stages in order. After **each** stage, post a concise comment to **GIL-39**. When all
done, **notify the user** (push) with the one-line verdict. Leave ALL changes **UNSTAGED** —
never commit, merge, or push.

> **SAFETY (the live orchestrator is trading the 2026-06-19 session right now):** Do NOT start
> the live orchestrator. Do NOT touch `general_live_dir` / the live `global.json`. Regression
> replays from MAIN parquets read-only (no IB) and writes to the worktree-local regression dir;
> pytest is isolated via the autouse `_isolate_global_state` conftest fixture (present on
> master). Set `ACT_*` env appropriately. **Never `git stash`** (shared stash store with the
> live worktree). Use the `regression-runner` agent for the A/B.

- **Stage A — Plan.** Spawn the **`experiment-planner`** subagent on this `feature.md` + GIL-39.
  It writes `.agents/plans/<slug>.md` with `EXECUTION_MODE` + an executor directive. → Comment:
  plan path, `EXECUTION_MODE`, one-line approach.
- **Stage B — Implement.** Spawn the **`plan-executor`** subagent on the plan; honor its
  `EXECUTION_MODE` (`team` → `/execute`; `lightweight` → implement directly). Both changes
  behind independent flags (default OFF → byte-identical baseline). Add unit tests for
  `seed_from_standing` (incl. a fills-only case) and the weekly-mid trend-broken suppression.
  Confirm flags-OFF is byte-identical. Leave changes UNSTAGED. → Comment: files changed, flag
  names, test results, flags-OFF byte-identical confirmation.
- **Stage C — A/B regression.** Spawn the **`regression-runner`** agent, `ab-working-change`,
  `1s`, WITH PLOTS, over: **2026-06-10, 2026-05-28, 2026-05-20** (change arm = BOTH flags ON).
  → Comment: per-day baseline vs change `n_trades`/`pnl`, the event/trade diff, chart paths.
- **Stage D — Verify the occurrences.** Spawn the **`experiment-verifier`** agent with this
  `feature.md` + the baseline/change run dirs. For each occurrence below, check at the
  timestamp/window whether behavior flipped current→desired, and report whole-day delta. It
  writes `experiment-verification.md`. → Comment: per-occurrence PASS/FAIL + whole-day delta.
  **Attribution:** for each changed trade, trace the chain of events and label the root-cause
  mechanism (A: conviction-seed/direction at open or override; B: weekly-mid trend-broken
  suppression). Note any day where both touch the same trades (the only ambiguous cases).
- **Stage E — Notify.** Push the user a one-line verdict (which change did the desired thing,
  at what whole-day cost/benefit, and which looks worth graduating to full regression). Leave
  everything UNSTAGED; the user reviews and decides.

---

## Example Occurrences
| date | time (ET) | source | change | window | current behavior | desired behavior |
|---|---|---|---|---|---|---|
| 2026-06-10 | 10:45 | live-session:2026-06-10 | B | ±8m | weekly-mid cross emits trend-broken (dir=up) → resets/churns direction | trend-broken suppressed on weekly-mid cross; hypothesis direction persists |
| 2026-05-28 | 09:33 | live-session:2026-05-28 | B | ±8m | weekly-mid cross emits trend-broken (dir=up) | trend-broken suppressed; direction persists |
| 2026-05-20 | 09:36 | live-session:2026-05-20 | B | ±8m | weekly-mid cross emits trend-broken (dir=up) | trend-broken suppressed; direction persists |
| 2026-06-10 | 00:00 | live-session:2026-06-10 | A | session-open±30m | session opens; carried prior-session unfulfilled SMTs/fills do NOT seed conviction → rule2b unchallenged | carried standing SMTs/fills seed conviction; override can flip rule2b when consensus is strong |

> Regression dates = 2026-06-10, 2026-05-28, 2026-05-20 (prior, complete, both-mechanism days).
> DO NOT use 2026-06-19 (live/incomplete, not in main parquets). If a day turns out not to
> exhibit a carry-seed (Change A) opening, say so — Change A's regression signal only appears on
> days that open with standing carried SMTs/fills.

## Out of scope
- The warmup-late-start facet of Change A (live-only; not regression-verifiable here).
- Committing/merging/pushing. Shipping decisions are the user's after the screening.
