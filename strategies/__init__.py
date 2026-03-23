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
from strategies import energy_momentum_v1
from strategies import energy_materials_mar20
from strategies import energy_oos_opt_sep25
from strategies import energy_oos_sep25
from strategies import financials_mar20
from strategies import semis_mar20
from strategies import utilities_mar20
from strategies import energy_mar21
from strategies import nasdaq100_mar21
from strategies import multisector_mar23

REGISTRY: dict = {
    "energy-momentum-v1": energy_momentum_v1,
    "energy-materials-mar20": energy_materials_mar20,
    "energy-oos-opt-sep25": energy_oos_opt_sep25,
    "energy-oos-sep25": energy_oos_sep25,
    "financials-mar20": financials_mar20,
    "semis-mar20": semis_mar20,
    "utilities-mar20": utilities_mar20,
    "energy-mar21": energy_mar21,
    "nasdaq100-mar21": nasdaq100_mar21,
    "multisector-mar23": multisector_mar23,
}
