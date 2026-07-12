"""Path wiring for the contracts unit tests — make the package modules importable by
their bare module names (schemas / predicates / validate_contracts) both under pytest
and as CLI, matching the agent/decisions convention."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
for _p in (_HERE, _AGENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
