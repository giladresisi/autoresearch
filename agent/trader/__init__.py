"""Analyzer -> Planner -> Executor (cycle 1).

Deliberately NOT `agent/executor/`: that package holds the v2 one-brain skeleton whose
PRIMARY hook bypasses the legacy engine entirely (`session_pipeline.py:1057-1070`).
Cycle 1 needs the legacy engine and this chain running SIDE BY SIDE, so the graft uses
the additive `_ai_decisions_on_bar` template instead and the code lives here.

Nothing in this package imports `live_orders`, writes `events.jsonl` / `daily.json` /
`hypothesis.json` / `position.json` / `smts.json`, calls any `smt_state` mutator, or
calls `paths.set_state_dir()`.

Cycle 1 stopped at the intended-entry RECORD. Cycle 3 adds a SIMULATED order lifecycle
(`order_sim.py`) so the §2/§8 spine — ladder, cooldown, attempt budget — has a fill to
act on. It is still a simulation: no broker is reached and no order is placed.
"""
