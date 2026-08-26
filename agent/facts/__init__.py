"""The deterministic facts layer (cycle 1).

Pure detectors `(state, bars) -> (facts, state)` shared by two drivers: an incremental
driver fed by the live 1 Hz bar loop, and a batch driver (the lookback) invoked when a
per-class coverage precondition fails. Nothing here imports `live_orders`, writes
`events.jsonl` / `daily.json`, or calls any `smt_state` mutator.
"""
