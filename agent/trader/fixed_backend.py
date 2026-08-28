"""The oracle thesis source: a synthetic thesis the experimenter chooses.

Two sources already exist — a real model call (`--seed`) and a recording — and both are
OBSERVED theses. §10-style oracle studies need a SYNTHETIC one: direction and DOL picked
to answer a question, e.g. §10.2's wrong-thesis stress, which INVERTS a day's thesis and
measures the bleed. Every such study in the doc was hand-rolled against the parquets.

**Why this bypasses the cache rather than seeding it.** The recorder's content key (see
`agent/trader/thesis_cache.py`) hashes the exact prompt bytes — KB, facts text, context,
task prompt, schema, model id. An oracle entry is therefore unauthorable without
reproducing the whole prompt, and self-invalidates the moment `thesis.md` changes. Phase 2
edits that file continuously.

(The identifiers of the caching layer are deliberately not spelled out anywhere in this
module: `test_the_backend_never_touches_a_cache` greps the source for them, and a
docstring mention is indistinguishable from a call to a grep.)

**Why validation is explicit.** `_real_backend()` returns
`thesis_via_decide_thesis(make_backend(...))`, so `decide_thesis` — schema, validator,
retry, failsafe — lives INSIDE the inner backend. Replacing the backend at this seam
bypasses all of it, which would make an oracle LESS constrained than a real thesis. The
checks below restore the constraints that matter.

Not checked here, deliberately: `build_thesis_schema` compiles level-name enums from the
call's own facts, so running an oracle through it would reject a synthetic level name for
the wrong reason. An oracle MAY name a level the facts do not carry; it may not be
malformed.
"""
from __future__ import annotations

import copy

from agent.contracts.predicates import validate_predicate_list

BIASES = ("UP", "DOWN", "NEUTRAL")
REGIMES = ("TREND", "RANGE", "HYBRID")
CONFIDENCES = ("HIGH", "MEDIUM", "LOW")


class OracleThesisError(ValueError):
    """An injected thesis that a real one could not have been."""


def validate_oracle_thesis(thesis) -> "list[str]":
    """Errors in an oracle thesis; empty means usable."""
    errs: list[str] = []
    if not isinstance(thesis, dict):
        return ["thesis must be an object"]

    bias = str(thesis.get("bias") or "").upper()
    if bias not in BIASES:
        errs.append(f"bias must be one of {BIASES}, got {thesis.get('bias')!r}")
    if str(thesis.get("regime") or "").upper() not in REGIMES:
        errs.append(f"regime must be one of {REGIMES}")
    if str(thesis.get("confidence") or "").upper() not in CONFIDENCES:
        errs.append(f"confidence must be one of {CONFIDENCES}")

    dol = thesis.get("dol")
    if bias in ("UP", "DOWN"):
        if not isinstance(dol, dict) or dol.get("price") is None:
            errs.append("a directional oracle thesis needs dol {level, price}")

    # Required EXPLICITLY, not defaulted: an empty list means the plan can only die at
    # the DOL, and a stress run measured against a plan that never dies is worthless.
    for field in ("falsified_if", "exhausted_if"):
        preds = thesis.get(field)
        if not preds:
            errs.append(f"{field} must be given explicitly and non-empty")
            continue
        errs += validate_predicate_list(preds, field)

    return errs


class FixedThesisBackend:
    """Serves one synthetic thesis. Drops in where `CachedThesisBackend` sits."""

    def __init__(self, thesis: dict, meta: "dict | None" = None) -> None:
        errs = validate_oracle_thesis(thesis)
        if errs:
            raise OracleThesisError("; ".join(errs))
        self._thesis = copy.deepcopy(thesis)
        self._meta = dict(meta or {})
        self._meta.setdefault("thesis_source", "injected")
        self._meta.setdefault("latency_sec", 0.0)
        self._meta.setdefault("retries", 0)
        self._meta.setdefault("verdict", "injected")
        self.calls = 0

    @property
    def thesis(self) -> dict:
        return copy.deepcopy(self._thesis)

    def __call__(self, facts_text, context_text, facts, *, evidence_magnitude=None):
        self.calls += 1
        return copy.deepcopy(self._thesis), dict(self._meta)
