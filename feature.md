# Experiment: strategy-agent-poc — strategy-knowledge docs + decision-agent POC

**Linear issue (source of truth):** `GIL-44` — https://linear.app/gilad-resisi/issue/GIL-44/strategy-agent-poc-strategy-knowledge-docs-agent-docsstrategy-decision
**Worktree / branch:** `autoresearch/strategy-agent-poc` (off `master` @ 5356c21)
**Status:** set up — ready for a separate main agent to execute in THIS worktree.

> Read GIL-44 first. It holds the full context: the decision-agent vision, all deliverables, the
> grounded `file.py:line` anchors, and the **SMT knowledge digest** you must use as source
> material (you have NO access to the setting-up agent's memory — the digest + the full request
> file `requests/strategy-agent-poc.md` in this worktree replace it). This file is just how to
> *proceed*.

**This is NOT a strategy-code experiment.** No strategy behavior change, no A/B regression, no
`experiment-verifier`. The deliverables are knowledge documents + a blind POC harness. Everything
stays UNSTAGED; never commit, merge, or push.

Key framing (from the user): a future AI agent will make in-session decisions (next-move
direction, when to start looking for entry confirmation, estimated target/DOL, pauses) on top of
the **hard-truth data the scripts keep providing** (SMTs, liquidity levels, sweeps, equilibrium,
TDO/TWO, FVGs). The docs you write ARE that agent's context. They are version-controlled and any
future change to them must be **backtested before merging to master**. No agent is added to the
strategy now; no evaluation system now.

---

## Runbook — you are the main agent running this experiment here

Work the stages in order. After **each** stage, post a concise comment to **GIL-44**. When all
done, **notify the user** (push) with the one-line verdict. Leave ALL changes **UNSTAGED**.

- **Stage A — smt.md.** Create `agent-docs/strategy/` and write `smt.md` per GIL-44's deliverable
  1: definitions first, but MOSTLY effects (how SMTs move / fail to move the graph — reversals,
  sweeps, DOLs) and how the effect **blends** with daily trend, daily/weekly equilibrium,
  session-of-day, sweep side, clustering. Source material: the GIL-44 digest +
  `requests/strategy-agent-poc.md` + `.agents/experiment-log.md` + the code anchors (re-verify
  them). Encode DISPROVEN results (Phase-3 dominant-direction, GIL-16/17/19/31/38/41) as
  prominently as what works. Structure it so a human can navigate and update it; include a "How
  to update this document" section stating the backtest-before-merge rule. → Comment: doc path +
  section outline.
- **Stage B — Propose next doc topics.** From the code (what the scripts actually flag) and the
  vision, propose to the USER (AskUserQuestion / the GIL-44 comment) the next
  `agent-docs/strategy/*.md` topics with 2–3 bullet scope each (candidate seeds in GIL-44
  deliverable 2). Write the ones the user picks — or, if running unattended, write the 2–3 you
  judge most load-bearing for the POC (liquidity-levels/sweeps and daily-bias/equilibrium are
  strong candidates) and flag the rest as proposed. → Comment: proposed list + which were written.
- **Stage C — POC prep.** Find a **recent meaningful trend** in the MNQ 1s parquet data (global
  root `main/<YYYY-MM>/` subfolders or `data/MNQ_1s.parquet`; mind the June→Sept contract roll
  back-adjustment). Copy aside a slice that **ENDS BEFORE the trend starts** (e.g. into
  `agent-docs/strategy/poc/`) — zero lookahead. Write `poc.md` instructing a separate fresh agent
  to use ONLY the `agent-docs/strategy/` docs + the slice to estimate the next move's
  **direction** and **expected end/target**, and report to the user. **`poc.md` must leak
  NOTHING about the subsequent trend** (not direction, magnitude, nor why this date/time was
  chosen — word it neutrally). Record the ground truth (trend start/end time, direction,
  extent) ONLY in a GIL-44 comment + a separate ground-truth file poc.md tells the agent not to
  read. → Comment: slice path/range, poc.md path, ground truth.
- **Stage D — POC dry-run.** Launch a FRESH agent (clean context — e.g. a subagent given only
  `poc.md`) and let it produce its direction + target estimate. Its correctness is informative,
  not pass/fail — the POC validates the workflow. → Comment: the agent's estimate vs ground
  truth, plus observations on what context/docs it lacked.
- **Stage E — Notify.** Push a notification: experiment finished, one-line verdict (docs written,
  POC prediction vs ground truth). Leave everything UNSTAGED; the user reviews and decides.

---

## Example Occurrences

> **Adapted gate (user-approved deviation):** this experiment has no classic replayable
> strategy-behavior occurrence. The verification event is the **POC trend** selected in Stage C —
> the table below is completed AT THAT POINT (fill in the real values, keep ground truth out of
> `poc.md`). The `experiment-verifier` agent is NOT used; Stage D is the verification.

| # | date | time (ET) | source | window | current behavior | desired behavior |
|---|------|-----------|--------|--------|------------------|------------------|
| 1 | 2026-07-02 | ~10:14 (trend start; slice cut 10:10:00) | MNQ+MES 1s slices (from `general/main/2026-09`, back-adjusted; window 2026-06-15 00:00 → 2026-07-02 10:09:59 ET) | slice end → 13:59 ET low | no decision-agent exists; scripts alone | fresh agent, given docs + lookahead-free slices (both tickers), states next-move direction + expected target; compared to ground truth (`<global-root>/gil44_poc_ground_truth.md`, outside the worktree: DOWN, 30181 → 29329, −852 pts). Run 1 wrong (UP); run 2 (refactored concepts+decisions docs) correct (DOWN/medium, target 29826) with a memory-leak caveat — reports in `<global-root>/gil44_poc_result_run{1,2}.md` |
| 2 | 2026-06-30 | slice cut 09:28:00 (attempt 3, gaps #1/#2/#3/#10 fixed) | slices REPLACED (window 2026-06-15 00:00 → 2026-06-30 09:27:59 ET) | slice end → trend end | — | run 3 = first fully-blind correct call (daily UP/medium; truth UP +565 — `<global-root>/gil44_poc_ground_truth_run3.md`); report `<global-root>/gil44_poc_result_run3.md` |
| 3 | TBD (run 4, facts-only flow) | pre-run derive_facts.py v2 → `poc/facts.txt`; NO slices/script for the agent | cut details + truth ONLY in `<global-root>/gil44_poc_ground_truth_run4.md` + GIL-44 | facts end → trend end | — | run-4 fresh agent (user may use haiku) → `poc-result-4.md`; judgment-only test of the decision protocols |
