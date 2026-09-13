"""Durable plan collection, keyed by `plan_id`.

A COLLECTION, not a singleton, deliberately: contingent / two-plan theses are recorded
intent for a later cycle, and a singleton would have to be torn out to get there. Cycle
1 puts exactly one plan in it.

Writes `plans.json` under the injected state dir. Never `hypothesis.json` — that is the
legacy engine's file and it shares this directory.
"""
from __future__ import annotations

import json
import os
import tempfile

PLANS_FILE = "plans.json"


class PlanStore:
    def __init__(self, state_dir) -> None:
        self.state_dir = str(state_dir)
        self._plans: dict = {}
        self._load()

    @property
    def path(self) -> str:
        return os.path.join(self.state_dir, PLANS_FILE)

    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as fh:
                    blob = json.load(fh)
                self._plans = {k: v for k, v in (blob or {}).items() if isinstance(v, dict)}
        except Exception:
            self._plans = {}

    def _save(self) -> None:
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".plans_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._plans, fh, default=str)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # -- collection API -------------------------------------------------------- #

    def put(self, plan: dict) -> None:
        if not isinstance(plan, dict) or not plan.get("plan_id"):
            return
        self._plans[str(plan["plan_id"])] = dict(plan)
        self._save()

    def get(self, plan_id: str) -> "dict | None":
        return self._plans.get(str(plan_id))

    def all(self) -> list:
        return list(self._plans.values())

    def remove(self, plan_id: str) -> None:
        if self._plans.pop(str(plan_id), None) is not None:
            self._save()
