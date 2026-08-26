"""Pure detectors: explicit state in, state out, never raise on degenerate input.

Modeled on `smt_detect.py`'s contract (smt_detect.py:1-14) — the codebase's own
architecture notes identify it as its cleanest layer. Every function here is total.
"""
