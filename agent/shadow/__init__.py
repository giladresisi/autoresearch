"""GIL-44 Phase-3 AI shadow-decisions package.

An importable, silent, side-effect-free (except its own cache/audit files) shadow
decision module that runs ALONGSIDE the hypothesis engine at the same triggers, logs
paired records (hypothesis said X, AI said Y, price did Z), and changes NOTHING about
trades/P&L. Flag-gated, default OFF (see agent/shadow_config.py).
"""
