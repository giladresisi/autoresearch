"""Bench configuration (plan 10 §Design).

Every safety-net trigger, the churn cap, the TTL tiers, and the latency budget are
config — they are the iteration subjects, so they must be toggleable per run without a
code change. Shared constants (latency default, the predicate/confidence machinery) are
imported from production rather than re-defined so the bench and the executor cannot
silently diverge (drift guard, plan 10 §Design).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# Safety-net trigger names (config-toggleable). `ttl` is the confidence-tiered TTL;
# `acceptance_flip` / `opposite_extreme` are the standing code-injected nets.
SAFETY_NETS = ("ttl", "acceptance_flip", "opposite_extreme")

# Bench-only diagnostic gate sources (plan 11). Nothing trades off the bench — this
# override only decides which confidence value the BENCH engine uses for `stands()`:
#   calibrated       — the code-computed confidence gate (spec §8).
#   self             — the model's self-reported confidence (audit-only in production).
#   stand-directional— any valid directional thesis stands.
#
# NOTE (2026-09-10): `calibrated` was the default BECAUSE it mirrored production. It no
# longer does — `trade_director._actionable` and `trader/analyzer.stands` both dropped the
# confidence gate, so `stand-directional` is now the production-shaped source. The default
# is left on `calibrated` DELIBERATELY: every recorded scorecard was measured against it,
# and moving it silently would rewrite the comparison baseline for all of them. Pass
# gate_source="stand-directional" for a production-shaped run, and see move-the-needle.md
# §4 — settling which one is right is exactly what a real corpus is still owed.
# The source is stamped into run metadata + every scorecard header so a diagnostic run is
# never mistaken for a production-config run.
GATE_SOURCES = ("calibrated", "self", "stand-directional")

# false_kill / late_kill lookahead horizon — reuse annotate.py's HORIZON convention.
LOOKAHEAD_H = pd.Timedelta(hours=4)

# Static date -> regime map for the aggregate per-regime split. Extend as the bench
# grows; an unmapped date aggregates under "unknown".
DEFAULT_REGIME_MAP = {
    "2026-06-25": "reversal",
    "2026-05-19": "trend",
    "2026-05-18": "chop",
    "2026-05-01": "trend",
}


@dataclass
class BenchConfig:
    """One bench run's knobs. Defaults: latency 90 s, TTL HIGH 180 / MEDIUM 90 min,
    only the TTL safety-net ON, churn cap 20."""

    latency_sec: int = 90
    ttl_minutes: dict = field(default_factory=lambda: {"HIGH": 180, "MEDIUM": 90})
    # Enabled safety-net triggers (default: ttl only).
    safety_nets: tuple = ("ttl",)
    # acceptance_flip: N consecutive 1m closes on the anti-thesis side of the daily mid.
    acceptance_flip_n: int = 3
    churn_cap: int = 20
    regime_map: dict = field(default_factory=lambda: dict(DEFAULT_REGIME_MAP))
    lookahead_h: pd.Timedelta = LOOKAHEAD_H
    gate_source: str = "calibrated"          # bench-only diagnostic override (see GATE_SOURCES)

    def latency(self) -> pd.Timedelta:
        return pd.Timedelta(seconds=self.latency_sec)

    def ttl_for(self, tier: str):
        """TTL minutes for a confidence tier, or None (LOW is recall-driven, not TTL)."""
        return self.ttl_minutes.get(tier)

    def net_on(self, name: str) -> bool:
        return name in self.safety_nets

    def regime_for(self, date_str: str) -> str:
        return self.regime_map.get(date_str, "unknown")
