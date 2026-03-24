"""
strategies/__init__.py — Strategy Registry

REGISTRY maps strategy name → imported module.
Each module exposes: screen_day(), manage_position(), METADATA dict.

To add a strategy:
  1. Place <name>.py in this directory (use scripts/extract_strategy.py)
  2. Import it here and add to REGISTRY

Strategies reach master only via scripts/extract_strategy.py — never via
direct edits to strategy logic on master (see prd.md §6a branching policy).
"""
from strategies import multisector_mar23
from strategies import global_mar24

REGISTRY: dict = {
    "multisector-mar23": multisector_mar23,
    "global-mar24": global_mar24,
}
