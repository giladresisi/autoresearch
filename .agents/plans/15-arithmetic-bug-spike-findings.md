# Task 1b — standing arithmetic-execution bug: design spike findings

**Verdict: DEFER the scratch-tally schema field (Task 8-as-written). Implement the cheaper
validator-retry-message lever instead.**

## What the failure actually is

The bug is a disagreement between the model's declared `bias` and the SIGN of
`score_thesis_evidence(evidence)` — the code's deterministic tally of the model's own declared
evidence ledger. It is caught today by `validate_contracts.validate_thesis`'s `ARI_THESIS_BIAS`
check (a retry, not a silent override).

Concrete transcripts re-read this spike (`manual-l1-thesis/*.json`, `attempts` list):

- **#4 — 2026-07-14 01:00 (`20260714_0100000400_openrouter.json`)** — 3 attempts, all rejected,
  ends in the NEUTRAL/LOW failsafe:
  - attempt 0: bias **UP**, but its 24-item ledger nets **−22.5** (expected DOWN).
  - attempt 1: bias **DOWN**, but it *rewrote* the ledger to 4 items netting **+6.25** (expected UP).
  - attempt 2: bias **UP**, but its 6-item ledger nets **−10.0** (expected DOWN).
- **#13-adjacent — 2026-07-15 05:00 (`20260715_0500000400_openrouter.json`)** — converged clean in
  one attempt (bias UP, net > 0). Shows the bug is intermittent, not universal.

## Where the mismatch sits in generation order

The schema already orders `evidence → reasoning → bias`. So the model generates every evidence
item, then narrates, then commits to `bias`. The mismatch is therefore NOT "bias generated before
evidence"; that lever was already pulled (plan 13). The mismatch is that the model computes its own
mental tally of per-item **points** (tier ×0.5/0.75/1.0 × tf ×1.0/1.5 × clearance
×0.75/1.0/1.25) and **sign** (level high/low polarity × accept/reject) — a large multi-factor sum
over up to 24 items — and gets it wrong, so the `bias` it picks does not match the code's exact
recomputation of the same items.

## (a) Would a per-item running-sum `scratch_tally` field help?

**No — it would become a second thing the model also miscomputes.** The model does not know the
code's exact multipliers/sign rule; a scratch field only forces it to write DOWN its own (wrong)
per-item points and running sum. Its `bias` would then agree with its *scratch* sum, but the
scratch sum is computed with the same faulty mental multiplier model, so it would STILL disagree
with `score_thesis_evidence` and `ARI_THESIS_BIAS` would still reject. The scratch field makes the
model internally consistent with its own wrong arithmetic — not consistent with code, which is the
only thing the validator checks. Points and sign are 100% code-derived by design (the model must
NOT self-report them); asking the model to also tally them re-introduces exactly the model-owned
arithmetic the "model judges, code computes" architecture removed.

## (b) Cheaper/different lever

**Yes.** The transcripts show the real failure signature on retry: the model treats each
`ARI_THESIS_BIAS` retry as "redo the whole decision," *rewriting its evidence items* and
re-diverging (attempt 0's 24 items → attempt 1's 4 items → attempt 2's 6 items). The generic
retry message already quotes the expected bias and net score, yet the model keeps churning the
ledger instead of just relabeling `bias`.

The cheaper lever is a **targeted `ARI_THESIS_BIAS` retry message** that:
1. States the code's net score is AUTHORITATIVE and the model's own tally is not.
2. Instructs the model to KEEP its evidence items fixed (do not re-pick/re-tally them) and simply
   set `bias` to the sign of the recomputed net — unless a specific evidence item is factually
   wrong (wrong level/direction/mature), in which case fix that ONE item and let bias follow.
This is a small change to `run_agent._retry_prompt` / a dedicated ARI-specific message, no schema
surface, no new model-owned arithmetic. It directly attacks the observed "rewrite everything on
retry" churn that the generic message does not prevent.

## (c) Recommendation

- **Defer** the `scratch_tally` schema field (Task 8-as-written) — it does not target the root
  cause and risks adding a new miscomputation surface.
- **Implement the cheaper lever** as Task 8's explicitly-sanctioned alternative: an
  `ARI_THESIS_BIAS`-specific retry message in `run_agent.py` that tells the model to trust the
  code-computed net sign and keep its evidence ledger stable rather than re-deriving it. Scoped as
  a small follow-up to the retry-message construction, with a `test_run_agent.py` assertion on the
  new message content.
