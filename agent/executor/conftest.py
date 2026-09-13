"""Path wiring for the executor unit tests — make the executor + contracts modules
importable by bare name both under pytest and as CLI (agent/decisions convention)."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
for _p in (_HERE, _AGENT, os.path.join(_AGENT, "contracts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
