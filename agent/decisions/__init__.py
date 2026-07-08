"""GIL-44 Phase-3 AI decisions engine package.

An importable, silent, side-effect-free (except its own audit files) observation-only
decision module that runs ALONGSIDE the hypothesis engine at the same triggers, logs
paired records (hypothesis said X, AI said Y, price did Z), and changes NOTHING about
trades/P&L. Flag-gated, default OFF (see agent/decisions_config.py).
"""
