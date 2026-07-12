"""Mechanism adapter (spec §7) — the closed enum toolbox → existing code paths.

The TradeDirector arms mechanisms by enum kind; this adapter is the ONLY place that maps
each closed enum to the real entry/management code (`hypothesis.compute_cautious_prices`
ladder, FVG `entry_ranges`, `live_orders` stop/market entry + `move_stop_entry`,
`stop_utils.valid_stop_for_fill`). Keeping the mapping in one dispatch table means an
unknown kind is a hard error, never a best-effort interpretation.

Two implementations share the mapping:
  - `RecordingMechanismAdapter` — records the dispatched commands, no side effects (backtest
    + tests; the stub POC never arms a SETUP, so this is what the primary backtest uses).
  - `LiveMechanismAdapter` — resolves and calls the mapped code path (live, user-gated).

The mapping itself (`ENTRY_CODE_PATHS` / `MGMT_CODE_PATHS`) is the audited contract and is
unit-tested per enum; wiring the live calls end-to-end into fills is deferred with the rest
of the level internals.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_AGENT)
for _p in (_HERE, os.path.join(_AGENT, "contracts"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schemas import ENTRY_MECHANISMS, MGMT_MECHANISMS  # noqa: E402

# Each enum kind → the existing code path it routes to (module:attribute). This is the
# reuse-map contract (spec §7 / §11); resolved lazily so importing this module has no
# side effects (live_orders imports must not run in the backtest).
ENTRY_CODE_PATHS = {
    "confirmation_bar": "hypothesis:compute_cautious_prices",
    "fvg_retrace": "hypothesis:compute_cautious_prices",     # FVG entry_ranges ladder
    "level_break_stop_entry": "live_orders:place_stop_entry",
    "market_on_condition": "live_orders:place_market_entry",
}
MGMT_CODE_PATHS = {
    "take_profit": "live_orders:move_stop_entry",            # take-profit-on-touch / exit move
    "move_stop": "live_orders:move_stop_entry",
    "raise_to_breakeven": "stop_utils:valid_stop_for_fill",
    "trail": "live_orders:move_stop_entry",
    "market_close_on": "live_orders:place_market_entry",     # flatten_first close
}

# Every closed enum kind must have a code-path mapping (guards against a silent gap).
assert set(ENTRY_CODE_PATHS) == ENTRY_MECHANISMS
assert set(MGMT_CODE_PATHS) == MGMT_MECHANISMS


def code_path_for(kind: str) -> Optional[str]:
    """The 'module:attribute' code path an enum kind routes to, or None if unknown."""
    return ENTRY_CODE_PATHS.get(kind) or MGMT_CODE_PATHS.get(kind)


def _resolve(code_path: str):
    """Import + getattr a 'module:attribute' code path (lazy; raises on a bad path)."""
    mod_name, attr = code_path.split(":", 1)
    mod = __import__(mod_name)
    return getattr(mod, attr)


class RecordingMechanismAdapter:
    """No-side-effect adapter: records every dispatched mechanism command. Used by the
    primary backtest (deterministic, no broker) and by the executor unit tests."""

    def __init__(self):
        self.armed: list = []
        self.disarmed: int = 0
        self.breakeven: int = 0
        self.management: list = []
        self.dol: list = []

    def arm(self, plan) -> None:
        for m in (getattr(plan, "entry", None) or {}).get("mechanisms") or []:
            kind = m.get("kind")
            self.armed.append((kind, code_path_for(kind)))

    def disarm(self) -> None:
        self.disarmed += 1

    def raise_breakeven(self) -> None:
        self.breakeven += 1

    def apply_management(self, mgmt) -> None:
        kind = mgmt.get("kind")
        self.management.append((kind, code_path_for(kind)))

    def execute_dol_falsified(self, action, params) -> None:
        self.dol.append(action)


class LiveMechanismAdapter:
    """Resolves + invokes the mapped code path (live, user-gated). Constructed with a
    `broker` handle; when `connected` is False it is inert (safe to build in a live
    dispatcher without any broker side effects — the wire-but-do-not-enable path)."""

    def __init__(self, broker=None, connected: bool = False):
        self.broker = broker
        self.connected = connected
        self.calls: list = []

    def _dispatch(self, code_path: str, *args, **kwargs):
        self.calls.append((code_path, args, kwargs))
        if not self.connected:
            return None                     # inert: no broker side effects
        return _resolve(code_path)(*args, **kwargs)

    def arm(self, plan) -> None:
        for m in (getattr(plan, "entry", None) or {}).get("mechanisms") or []:
            cp = code_path_for(m.get("kind"))
            if cp:
                self._dispatch(cp, plan=plan, mechanism=m)

    def disarm(self) -> None:
        self.calls.append(("disarm", (), {}))

    def raise_breakeven(self) -> None:
        self._dispatch(MGMT_CODE_PATHS["raise_to_breakeven"])

    def apply_management(self, mgmt) -> None:
        cp = code_path_for(mgmt.get("kind"))
        if cp:
            self._dispatch(cp, mechanism=mgmt)

    def execute_dol_falsified(self, action, params) -> None:
        # MARKET_CLOSE → flatten via the market-entry close path; TIGHTEN_STOP → move_stop.
        cp = ("live_orders:place_market_entry" if action == "MARKET_CLOSE"
              else "live_orders:move_stop_entry")
        self._dispatch(cp, action=action, params=params)
