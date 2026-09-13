"""Builds the `decide_thesis` facts dict from in-memory bars.

**Contract, pinned by `test_assemble.py`:** `agent/run_agent.py::decide_thesis` reads
exactly the 15 keys in `DECIDE_THESIS_KEYS` off the `facts` dict, plus positional
`facts_text` / `context_text` and keyword `evidence_magnitude`.

**Why this routes through `derive_facts.compute_facts` and not through the FactStore.**
Acceptance gate 1 compares this assembler against the offline reference
(`agent/bench/facts.py::ParquetFactsSource.build_facts` → `derive_facts`). Reconstructing
S0–S9 from the new facts layer's own records would be comparing a fork against itself —
and most of the 15 keys (HTF close status, mid reclaim, SMT candidates, evidence
magnitude, the S8/S9 renders) have no representation in the facts layer at all. So the
LLM-facing view is produced by the reference code, reusing `bench.facts.bundle_to_l1_view`
so there is exactly one copy of the overlay logic. `store` is accepted for interface
symmetry and is NOT read — matching the plan's own architecture note that the Analyzer
"ignores the store entirely". Store-derived facts serve the Executor, not the Analyzer.

The output is byte-shaped to match the recorded L1 runs: `facts_text` is
`res.text + res.menu_text + res.evidence_text` joined by blank lines and `context_text`
is `""`, exactly as `manual-l1-thesis/test_l1_thesis_manual.py` assembled them.
"""
from __future__ import annotations

import datetime
import os
import sys

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_AGENT)
for _p in (_AGENT, _REPO, os.path.join(_AGENT, "contracts"),
           os.path.join(_REPO, "calibration")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent.bench.facts import (                                    # noqa: E402
    BoundarySliceError, bundle_for_boundary, bundle_to_l1_view,
)
# Imported as a TOP-LEVEL module (not `agent.derive_facts`) so this shares the single
# module instance `agent/bench/facts.py` and the whole calibration path already use —
# two copies of derive_facts would mean two FactsBundle classes and two module caches.
from derive_facts import TZ, render_facts_text                     # noqa: E402
from agent.facts.requirements import ANALYZER_REQUIREMENT          # noqa: E402

DECIDE_THESIS_KEYS = (
    "levels", "menus", "fvg_zones", "fvg_zone_meta",
    "suppressed_p1_levels", "suppressed_p2_sites",
    "level_htf_close_status", "level_tiers", "smt_candidates",
    "week_extremes", "now_price", "mid_reclaim", "htf_reversal",
    "mid_position", "p1_stale_levels",
)

# The Analyzer's own window. Must equal agent/bench/facts.py LOOKBACK (17 days) or the
# parity gate compares two different views.
LOOKBACK = ANALYZER_REQUIREMENT.windows[list(ANALYZER_REQUIREMENT.windows)[0]]

_MAINT_LO = datetime.time(16, 55)
_MAINT_HI = datetime.time(18, 0)
_COLS = ("open", "high", "low", "close")


def lower_tz(df) -> pd.DataFrame:
    """Lowercase o/h/l/c, tz-aware ET, sorted. Maintenance bars still IN — this mirrors
    `ParquetFactsSource._load_raw`, whose frame is what the reference derives the ATH
    from."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=list(_COLS))
    out = df.rename(columns=str.lower)
    if not all(c in out.columns for c in _COLS):
        return pd.DataFrame(columns=list(_COLS))
    out = out[list(_COLS)]
    if not isinstance(out.index, pd.DatetimeIndex):
        return pd.DataFrame(columns=list(_COLS))
    out.index = (out.index.tz_localize(TZ) if out.index.tz is None
                 else out.index.tz_convert(TZ))
    return out.sort_index()


def drop_maintenance(df: pd.DataFrame) -> pd.DataFrame:
    """`derive_facts.load`'s filter, with its exact inclusive bounds."""
    if len(df) == 0:
        return df
    t = df.index.time
    return df[(t <= _MAINT_LO) | (t >= _MAINT_HI)]


def normalize_for_derive(df) -> pd.DataFrame:
    """The in-memory equivalent of `derive_facts.load`."""
    return drop_maintenance(lower_tz(df))


def _empty_contract() -> dict:
    """A structurally complete but empty view. A degraded facts build must still satisfy
    the 15-key contract — `decide_thesis` reads every key unconditionally.

    Every nested dict is built FRESH. `dict(shared)` would copy the outer mapping while
    leaving all five per-ticker values aliased to the same two inner dicts, so a
    downstream mutation of one key would silently corrupt the other four.
    """
    def _by_ticker():
        return {"MNQ": {}, "MES": {}}

    return {
        "levels": {}, "menus": {"dol": {}}, "fvg_zones": [], "fvg_zone_meta": {},
        "suppressed_p1_levels": {}, "suppressed_p2_sites": {},
        "level_htf_close_status": _by_ticker(),
        "level_tiers": _by_ticker(),
        "smt_candidates": [],
        "week_extremes": {"MNQ": {"hi": None, "lo": None}, "MES": {"hi": None, "lo": None}},
        "now_price": None,
        "mid_reclaim": _by_ticker(),
        "htf_reversal": _by_ticker(),
        "mid_position": _by_ticker(),
        "p1_stale_levels": {"MNQ": [], "MES": []},
        "degraded": True,
    }


def build_bundle(bars: dict, now: pd.Timestamp):
    """`compute_facts` over the 17-day slice strictly before `now`.

    The slicing rule is NOT reimplemented here — it is `bench.facts.bundle_for_boundary`,
    the same function `ParquetFactsSource.build_facts` calls (cycle-1 addendum change
    E). This function's only job is to turn in-memory bars into the two frames that
    rule needs: `raw` (maintenance bars INCLUDED — the ATH is taken from it) and `norm`
    (maintenance dropped — the primary slice and the hist frames come from it).

    Returns None on any degraded input, which is the contract `assemble_facts` expects.
    """
    raw_by, norm_by = {}, {}
    for tk in ("MNQ", "MES"):
        raw = lower_tz((bars or {}).get(tk))
        if len(raw) == 0:
            return None
        raw_by[tk] = raw
        norm_by[tk] = drop_maintenance(raw)
    try:
        bundle, _prim = bundle_for_boundary(raw_by, norm_by, now, lookback=LOOKBACK)
    except BoundarySliceError:
        return None
    return bundle


def assemble_facts(store, bars: dict, now: pd.Timestamp) -> tuple:
    """-> (facts_text, context_text, facts_dict, evidence_magnitude).

    Total: degraded input yields the empty contract, never an exception.
    """
    try:
        bundle = build_bundle(bars, now)
    except Exception:
        bundle = None
    if bundle is None:
        return "", "", _empty_contract(), {}

    try:
        text = render_facts_text(bundle)
        vd, menu_text, evidence_text, magnitude = bundle_to_l1_view(bundle)
    except Exception:
        return "", "", _empty_contract(), {}

    fallback = _empty_contract()
    for key in DECIDE_THESIS_KEYS:
        vd.setdefault(key, fallback[key])
    facts_text = "\n\n".join([text, menu_text, evidence_text])
    return facts_text, "", vd, magnitude
