"""Path wiring for the bench tests — make the bench, contracts, and agent modules
importable by bare name both under pytest and as a CLI (agent/decisions convention)."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_AGENT)
for _p in (_HERE, _AGENT, os.path.join(_AGENT, "contracts"),
           os.path.join(_REPO, "calibration")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
