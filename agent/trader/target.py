"""T2 target selection: `build_menus`'s nearest-first D1, re-anchored at the ENTRY FILL.

**What changed and why.** Production used to take its target once, at the 09:20 L1
boundary, and never reconsider it (`planner.derive_plan` copies `thesis["dol"]` verbatim).
`l2-target-selection.md` §3 measured the same rule asked later, at the fill, over 84
corpus dates: **T1 (09:20) 11,814 pts, T2 (fill) 12,966 pts** — +1,152, +9.7%, from
nothing but a later anchor.

**The rule is unchanged; only the instant moves.** The pick is always a `build_menus`
row and it is always D1, the nearest eligible draw. That is deliberate: §4 of the same
document CLOSED the B8g hazard-argmax alternative ("ranks better and selects worse" —
413 fewer points), so re-anchoring is the whole change and no new ranking comes with it.

**Production's own path, not the study's.** `agent/study/target_offline.py` reaches the
same rows through `StudyFacts`, which loads parquets off disk; that tree is a measurement
harness and must not be imported by the Executor. This module goes through
`assemble.build_bundle`, which builds from the IN-MEMORY bars the bar loop already
carries — the same function the Analyzer's `assemble_facts` uses.

**Total by construction.** Called from inside the bar loop at the instant of a fill, so
every failure path returns None rather than raising: a target that cannot be built means
the position is managed by its stop and the window mark, which is a worse trade, not a
dead bar loop.

**COST, and the live gap this leaves open.** `build_bundle` is ~1.5 s over the graft's
bounded 17-day history (~4.5 s unbounded), and it runs synchronously at the fill. Replay
has no wall clock to protect and determinism requires the inline call, but LIVE reaches
this from the same 1s tick callback that drives order execution. Threading or pre-warming
it is a separate decision and is NOT taken here — see plan 16's out-of-scope list.
"""
from __future__ import annotations

import pandas as pd

from agent.derive_facts import build_menus, facts_to_validator_dict
from agent.facts.assemble import build_bundle

#: `build_menus` keys its DOL menus by the plan's own direction words.
_DIRECTIONS = ("UP", "DOWN")


def select_target(bars: dict, now: pd.Timestamp, direction: str,
                  ticker: str = "MNQ") -> "dict | None":
    """The D1 menu row for `direction` as of strictly before `now`, or None.

    `None` is a REAL outcome, not an error: `l2-target-selection.md` §5 measures the
    direction's menu as empty on 6.0% of sessions at 09:20, and an empty menu at the fill
    is the same condition read later. The caller must treat it as "no target", never as
    a reason to skip the fill that already happened.
    """
    want = str(direction or "").upper()
    if want not in _DIRECTIONS:
        return None
    try:
        bundle = build_bundle(bars, now)
        if bundle is None:
            return None
        menus = build_menus(bundle, facts_to_validator_dict(bundle))
        rows = (menus.get("dol") or {}).get(want) or ()
        if not rows:
            return None
        # Nearest-first D1 — `build_menus` already emits the rows in that order, which is
        # the same thing `study.target_offline._nearest_first_pick` relies on.
        row = dict(rows[0])
        return row if row.get("price") is not None else None
    except Exception:
        # Swallowed deliberately; see the module docstring. The Executor records the
        # miss as `target_selected` with pick=None, so it is visible in the artifact.
        return None
