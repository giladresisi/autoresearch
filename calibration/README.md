# GIL-44 Calibration Sweep

Purpose: measure the decision protocols' direction hit-rate per confidence bucket over many
systematic cut-points, and set the entry-gate confidence threshold from the resulting curve
(instead of a priori). Also isolates the flagged CANDIDATE item types (laggard-fail) and the
G4/G5/G6/G9/G10 levers.

## Layout

- `prepare_cuts.py` — generates `cuts/<date>_<HHMM>/{facts.txt, context-at-cut.md, poc.md}`
  for every trading day 2026-05-19..2026-07-01 (alternating 09:28 / 13:00 ET cuts; POC-burned
  dates excluded) and writes the ground truth to
  `<global>/gil44_calibration_truth.tsv` (OUTSIDE the worktree — agents never see it).
- `poc_template.md` — the per-cut agent instructions. Each run must end its
  `cuts/<id>/poc-result.md` with the machine-readable `RESULT:` block.
- `score_results.py` — parses all `poc-result.md` RESULT blocks, joins the truth TSV, prints
  call×truth matrices, hit-rate per confidence bucket, and the entry-gate threshold curve.
  Truth-label thresholds (MOVE_MIN/RATIO/DAY_MIN) live at the top of this script.

## Judgment-variance triplicates

Every 7th cut gets `__r2`/`__r3` sibling folders — identical facts, own `poc.md`/output — so
the same input runs 3× (same model). `score_results.py` reports per-field unanimity across
replicas; high-variance judgment slots need sharper criteria or a precedent in the docs, not
more formula. Replicas are excluded from the hit-rate stats (base run only) to avoid
double-weighting. `prepare_cuts.py --replicates-only` adds them to an existing `cuts/` tree.

## Running the sweep

One agent per cut, fresh context, ONE fixed model for the whole sweep (model is a confound
otherwise). Prompt per cut:

    Read calibration/cuts/<id>/poc.md and follow it exactly.

Options:
1. **User-spawned terminal agents** (max blindness — same protocol as POC runs 1–4).
2. **Subagent fan-out from the main session** (fast: all cuts in parallel batches). The
   per-cut prompt is the fixed one-liner above; ground truth is outside the worktree and not
   in agent memory, so the leak surface is the same as option 1 except trust in the spawning
   agent's prompt (kept to the fixed line).

After runs complete:

    .venv\Scripts\python.exe calibration\score_results.py

## Validating decisions (GIL-44 Phase 1)

`agent/validator.py` (worktree root — the production `agent/` package, alongside
`agent/derive_facts.py`; the Phase-2 runner joins them) is the deterministic
trust boundary: it machine-verifies a
structured decision (the daily-trend + next-move outputs) across three layers —
syntactic (schema/enums/required fields), arithmetic (integer votes,
contribution = weight x vote with D1 halving only, S = Σ contributions, ledger
item score = product of multipliers, N = Σbull − Σbear, direction consistent
with the ±3 gate, confidence not over-claimed, item types in the closed list,
correlation-audit double counts), and semantic (target present in the facts,
un-swept/un-depleted, on the correct side of price; resolution sides; checkpoint
matches S0). It reads OUTPUTS only — never the ground truth, never the cut
inputs — and is reused unchanged by the Phase-2 bench runner and Phase-3
orchestrator.

`validate_results.py` is the post-hoc sweep driver (calibration-only — it parses
markdown, which production never does; the runner emits structured JSON). It
parses each freeform `poc-result.md` into the structured decision and reports
per-run `protocol_clean` (the scorer can then report the raw and protocol-clean
curves). The `RESULT:` block in `poc_template.md` is the machine-readable
contract and carries every field the validator requires (direction/confidence
for both layers, regime, day_dol, weakens/flips, arm_entry_confirmation,
targets/resolutions); for fields outside that block, parsing is conservative —
unreadable fields are left absent and their checks are skipped. Caveat: the
syntactic required-field check flags absent fields, so a field the parser can't
see IS a false positive there (run 6 proved it) — which is why every required
field must live in the RESULT block, not in freeform headings.

    .venv\Scripts\python.exe calibration\validate_results.py           # sweep cuts/
    .venv\Scripts\python.exe calibration\validate_results.py report.md # explicit files

Failure policy: sweep mode flags the run `protocol_clean=false`; the
runner/production caller re-prompts quoting the violations and, on repeated
failure, falls back to `validator.failsafe_decision()` (NEUTRAL / LOW → scripts
baseline unchanged). Tests: `pytest agent/test_validator.py
calibration/test_validate_results.py`.

## Hygiene rules

- Never write per-cut ground truth (direction/extent) into any memory file or into the
  worktree; it lives only in the global TSV + GIL-44.
- Each cut's poc.md forbids reading OTHER cut folders (results of earlier cuts could bias).
- Re-generating cuts after a docs change: delete `cuts/` and rerun `prepare_cuts.py`, then
  rerun the sweep — doc changes are strategy changes (backtest-before-merge applies to the
  decision docs; this sweep IS their offline backtest).
