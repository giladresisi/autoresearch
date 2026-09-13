"""The planless-day shadow ledger — THE LOGGING HOOK ONLY (§11.0).

On any session where the 09:20 boundary resolves to NEUTRAL (no plan armed) but a
mechanism setup WOULD have fired under an oracle plan, log the paper outcome: mechanism,
entry, stop, result. **Log it; act on nothing.**

Why now, and why only the hook. §2's stay-dark policy — no standing L1 plan means the
mechanisms place nothing and track state only — currently rests on a SINGLE known
instance (07-17, a +91.25 oracle day left untraded). This ledger decides empirically
whether the foregone-setup cost is material. It is cheap, and the data CANNOT be
collected retroactively, which is the entire argument for building it before the decision
it informs. The DECISION itself is deferred until the ledger has entries; and if the cost
ever proves material, the fix is UPSTREAM — recall cadence, projection stretch-gates —
not an L2-side plan source. Nothing here should ever grow an order path.

Its own file, never `trader_decisions.jsonl`: those records are decisions the Executor
actually made, and mixing counterfactuals into them would corrupt the one artifact the
regression cases read. And never `events.jsonl` — that is the legacy stream the
regression diffs line-for-line against locked baselines.
"""
from __future__ import annotations

import json
import os

LEDGER_FILE = "shadow_ledger.jsonl"


def _iso(ts):
    if ts is None:
        return None
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


class ShadowLedger:
    """Append-only paper record of setups that a planless session left untaken.

    Every write is best-effort, exactly like `DecisionRecorder`: a ledger failure must
    never propagate into the bar loop. This thing is instrumentation; it does not get to
    take the session down.
    """

    def __init__(self, state_dir) -> None:
        self.state_dir = str(state_dir)
        self.logged = 0

    @property
    def path(self) -> str:
        return os.path.join(self.state_dir, LEDGER_FILE)

    def log(self, *, now, date, mechanism, entry, stop, result=None,
            plan_armed: bool = False, **extra) -> bool:
        """Record one foregone setup. Returns whether anything was written.

        `plan_armed=True` is the no-op case and it is checked HERE rather than at the
        call site: the ledger's entire meaning is "what a PLANLESS session gave up", and
        a row logged on a day that traded would silently inflate the cost the stay-dark
        decision is weighed against.
        """
        if plan_armed:
            return False
        record = {"kind": "shadow_setup", "time": _iso(now), "date": str(date),
                  "mechanism": mechanism, "entry": entry, "stop": stop,
                  "result": result, "acted": False}
        record.update(extra)
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception:
            return False
        self.logged += 1
        return True

    def read(self) -> "list[dict]":
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
