"""L1 lifecycle bench (plan 10).

A measurement instrument for the AI-trader v2 L1-internals effort: it replays full
days through the L1 thesis lifecycle (call -> standing thesis -> code-evaluated
predicates per bar -> death by falsification/exhaustion/TTL/safety-net -> re-call) and
scores lifecycle quality, so every internals iteration (KB docs, prompts, menus) shows
up as metric deltas on the same days.

NEW code only — the bench imports and reuses production modules (eval_predicate +
MarketView from agent/contracts/predicates.py, the confidence gate from
agent/confidence.py, backends via run_agent.make_backend / decide_thesis, the offline
facts path mirroring calibration/prepare_cuts.py). It never modifies production
behaviour. Run artifacts live under agent/bench/runs/ (gitignored).
"""
