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
from agent.facts.detectors._common import normalize
from agent.facts.htf_extremes import menu_rows, pools_at

#: `build_menus` keys its DOL menus by the plan's own direction words.
_DIRECTIONS = ("UP", "DOWN")

#: Plan 40 (`l2-target-selection.md` §7a): the unnested weekly/monthly extremes join the
#: T2 menu at the fill. ON by operator decision (2026-09-22) ahead of the full Wave-3
#: adoption rule; set False to restore pre-plan-40 T2 exactly (`htf=` is then ignored).
HTF_EXTREMES_IN_T2 = True


def _htf_pools(htf, bars, now, ticker):
    """The HTF rows usable at `now`, or [] (flag off / no context). May raise; callers
    decide what a failure costs."""
    if not HTF_EXTREMES_IN_T2 or not htf:
        return []
    return pools_at(htf, (bars or {}).get(ticker), now)

#: One-slot memo of the last bundle built. `select_target` and `level_universe` are
#: called back to back at the SAME fill instant on the SAME bars dict (plan 35), and
#: `build_bundle` is the ~1.5 s cost the module docstring warns about: building it twice
#: inside the live tick callback would double that for no new information. The slot
#: holds a STRONG reference to the bars dict and matches on identity (`is`) plus `now`:
#: a key built from `id(bars)` would be reused by CPython for a later, different dict
#: freed in between, and hand a second Executor the first one's bundle.
_LAST_BUNDLE: dict = {"bars": None, "now": None, "bundle": None, "vd": None}


def _bundle_for(bars: dict, now: pd.Timestamp):
    slot = _LAST_BUNDLE
    if slot["bars"] is bars and slot["bars"] is not None and slot["now"] == now:
        return slot["bundle"]
    bundle = build_bundle(bars, now)
    slot.update(bars=bars, now=now, bundle=bundle, vd=None)
    return bundle


def _validator_dict_for(bars: dict, now: pd.Timestamp, bundle) -> dict:
    """`facts_to_validator_dict(bundle)`, memoised next to the bundle it came from."""
    slot = _LAST_BUNDLE
    if slot["bundle"] is bundle and slot["vd"] is not None:
        return slot["vd"]
    vd = facts_to_validator_dict(bundle) or {}
    if slot["bundle"] is bundle:
        slot["vd"] = vd
    return vd


#: 6h blocks by the ET hour they START, mirroring `derive_facts.sub_blocks`. The bundle
#: closes ny_evening at 17:00 (CME maintenance follows until 18:00); here 17:xx still
#: maps to ny_evening so a stray maintenance-hour bar cannot open a phantom block.
_BLOCK_STARTS = (("asia", 18), ("london", 0), ("ny_morning", 6), ("ny_evening", 12))


def _running_block(frame, now):
    """Running high/low of the 6h block `now` sits in, named as the bundle will name it
    once it closes. The bundle only lists CLOSED blocks; the legacy universe carried the
    in-progress one too (2026-09-18 09:41: ny_morning_low 29764.0 was the live block)."""
    out = []
    try:
        h = int(now.hour)
        name, start_h = next((n, s) for n, s in _BLOCK_STARTS
                             if (s <= h < s + 6) or (s == 18 and h >= 18))
        start = now.normalize() + pd.Timedelta(hours=start_h)
        seg = frame[(frame.index >= start) & (frame.index <= now)]
        if len(seg):
            out.append({"name": f"{name}(cur)_high", "price": float(seg["High"].max()),
                        "swept": False, "depleted": False, "running": True})
            out.append({"name": f"{name}(cur)_low", "price": float(seg["Low"].min()),
                        "swept": False, "depleted": False, "running": True})
    except Exception:
        return []
    return out


#: Plan 35 v2 families. RTH opens 09:30 ET; the NY-morning 6h block runs 06:00-12:00.
_RTH_OPEN = pd.Timedelta(hours=9, minutes=30)
_NY_MORNING = (pd.Timedelta(hours=6), pd.Timedelta(hours=12))
RTH_HIGH, RTH_LOW, NY_MORNING_MID = "rth(cur)_high", "rth(cur)_low", "ny_morning(cur)_mid"


def _rth_running(frame, now):
    """`rth(cur)_high` / `rth(cur)_low`: the running post-09:30 extreme over COMPLETED
    1m bars strictly before `now` — from the 09:30 bar through the last MINUTE that has
    closed (on 1s frames the completed seconds inside the current minute are excluded
    too, so live 1s and the study's 1m frames agree). The minute `now` sits in (the entry
    bar at a fill) is excluded, so the universe never hands the selector the fill's own
    print as a level. Empty before 09:31 (no completed RTH bar yet, e.g. a 09:30 fill).
    Deliberately asymmetric with `_running_block` / `_ny_morning_mid`, which include the
    bar at `now` (the 6h block's extreme IS the running print)."""
    out = []
    try:
        start = now.normalize() + _RTH_OPEN
        end = now.floor("1min")                       # bars labelled before this are complete
        if end <= start:
            return out
        seg = frame[(frame.index >= start) & (frame.index < end)]
        if len(seg):
            out.append({"name": RTH_HIGH, "price": float(seg["High"].max()),
                        "swept": False, "depleted": False, "suppressed": False,
                        "running": True})
            out.append({"name": RTH_LOW, "price": float(seg["Low"].min()),
                        "swept": False, "depleted": False, "suppressed": False,
                        "running": True})
    except Exception:
        return []
    return out


def _ny_morning_mid(frame, now):
    """`ny_morning(cur)_mid`: (high + low) / 2 of the running NY-morning block (06:00 ->
    `now`, the bar at `now` included like `_running_block`; frozen at 12:00 once the block
    has closed). Empty before 06:00."""
    out = []
    try:
        start = now.normalize() + _NY_MORNING[0]
        if now < start:
            return out
        seg = frame[(frame.index >= start) & (frame.index <= now)
                    & (frame.index < now.normalize() + _NY_MORNING[1])]
        if len(seg):
            mid = (float(seg["High"].max()) + float(seg["Low"].min())) / 2.0
            out.append({"name": NY_MORNING_MID, "price": mid, "swept": False,
                        "depleted": False, "suppressed": False, "running": True})
    except Exception:
        return []
    return out


def level_universe(bars: dict, now: pd.Timestamp, ticker: str = "MNQ",
                   htf=None) -> list:
    """The named-level universe behind the T2 menu, as
    [{name, price, swept, depleted, suppressed}].

    Same `bundle.levels[ticker]` map `build_menus` reads, exposed unfiltered but
    FLAGGED (the selector applies the DOL menu's eligibility itself): `swept` /
    `depleted` from the validator dict, `suppressed` = the name is in the bundle's
    `suppressed_p1_levels[ticker]` nested/duplicate set that `derive_facts._dol_menu`
    excludes. Plus the families the legacy universe also carried and the bundle keeps
    elsewhere: the running day high/low/mid, the week high/low/mid, the IN-PROGRESS 6h
    block's extremes, and (plan 35 v2) the post-09:30 running extremes `rth(cur)_high` /
    `rth(cur)_low` over completed bars before `now` and the NY-morning block's midpoint
    `ny_morning(cur)_mid`. Empty on degraded input; never raises. FVG edges are not
    included: `bundle.fvg_zones` has no equivalent of the legacy `keep` flag.

    Plan 40: with `HTF_EXTREMES_IN_T2` on and `htf` given, the HTF rows T2 could use are
    appended flagged `htf: True` (a price already in the universe is not repeated). The
    initial-target selector draws from none of their families, so they are audit rows.
    """
    out: list = []
    try:
        bundle = _bundle_for(bars, now)
        if bundle is None:
            return out
        vlevels = _validator_dict_for(bars, now, bundle).get("levels") or {}
        suppressed = set((getattr(bundle, "suppressed_p1_levels", None) or {})
                         .get(ticker) or ())
        for name, tup in ((bundle.levels or {}).get(ticker) or {}).items():
            try:
                price = tup[0]
            except (TypeError, IndexError):
                continue
            if not isinstance(price, (int, float)) or isinstance(price, bool):
                continue
            flags = vlevels.get(name) or {}
            out.append({"name": str(name), "price": float(price),
                        "swept": bool(flags.get("swept")),
                        "depleted": bool(flags.get("depleted")),
                        "suppressed": name in suppressed})

        def _num(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool)

        extra = (("day_high", (getattr(bundle, "day_hi", None) or {}).get(ticker)),
                 ("day_low", (getattr(bundle, "day_lo", None) or {}).get(ticker)),
                 ("day_mid", getattr(bundle, "day_mid", None)),
                 ("week_high", (getattr(bundle, "week_hi", None) or {}).get(ticker)),
                 ("week_low", (getattr(bundle, "week_lo", None) or {}).get(ticker)),
                 ("week_mid", getattr(bundle, "weekly_mid", None)))
        for name, price in extra:
            if _num(price):
                out.append({"name": name, "price": float(price),
                            "swept": False, "depleted": False, "suppressed": False})
        frame = normalize((bars or {}).get(ticker))
        have = {lv["name"] for lv in out}
        for lv in _running_block(frame, now) + _rth_running(frame, now) + _ny_morning_mid(frame, now):
            if lv["name"] not in have:
                lv.setdefault("suppressed", False)
                out.append(lv)
                have.add(lv["name"])
        try:
            prices = {lv["price"] for lv in out}
            for e in _htf_pools(htf, bars, now, ticker):
                if e.name in have or float(e.price) in prices:
                    continue
                out.append({"name": e.name, "price": float(e.price), "swept": False,
                            "depleted": False, "suppressed": False, "htf": True})
                have.add(e.name)
        except Exception:
            pass                  # audit rows only: never cost the rest of the universe
    except Exception:
        return []
    return out


def target_menu(bars: dict, now: pd.Timestamp, direction: str,
                ticker: str = "MNQ", htf=None) -> list:
    """The WHOLE ranked menu for `direction` (nearest first), not just D1.

    Plan 41: `select_target` answers "what does the Executor bind", which is row D1. The
    operator's `trade.py agent-target --list` needs the rows D1 is chosen from, so the
    override can name one instead of typing a price. Same builder, same eligibility, same
    order — this is the menu, and `select_target` is its head. Never raises; an empty
    list is a real outcome (see `select_target`).
    """
    want = str(direction or "").upper()
    if want not in _DIRECTIONS:
        return []
    try:
        bundle = _bundle_for(bars, now)
        if bundle is None:
            return []
        extra = None
        try:
            extra = menu_rows(_htf_pools(htf, bars, now, ticker)) or None
        except Exception:
            extra = None
        menus = build_menus(bundle, _validator_dict_for(bars, now, bundle),
                            extra_pools=extra)
        rows = (menus.get("dol") or {}).get(want) or ()
        return [dict(r) for r in rows if r.get("price") is not None]
    except Exception:
        return []


#: The running families `running_rows` shows: the day and week extremes/mids plus every
#: `level_universe` row flagged `running` (the in-progress 6h block, rth(cur), the
#: NY-morning mid).
_RUNNING_NAMES = ("day_high", "day_low", "day_mid", "week_high", "week_low", "week_mid")


def running_rows(bars: dict, now: pd.Timestamp, direction: str,
                 ticker: str = "MNQ", exclude_prices=()) -> list:
    """DISPLAY rows for `trade.py agent-target --list` (2026-09-23 operator comment): the
    running day / week / 6h-block levels ahead of price in `direction`, nearest first,
    ids R1..Rn, `band` "RUNNING".

    They are appended to the printed menu only, so an operator can bind one by id. They
    never enter `build_menus` and `select_target` never sees them: the automatic T2 is
    unchanged. No draw floor is applied -- the operator is choosing, and the Executor's
    override guard still refuses a price behind the entry. A price already on the DOL menu
    (`exclude_prices`) is not repeated. Never raises."""
    want = str(direction or "").upper()
    if want not in _DIRECTIONS:
        return []
    try:
        bundle = _bundle_for(bars, now)
        if bundle is None:
            return []
        vd = _validator_dict_for(bars, now, bundle)
        now_price = vd.get("now_price")
        if not isinstance(now_price, (int, float)):
            now_price = bundle.now_price
        if not isinstance(now_price, (int, float)):
            return []
        ar = (bundle.avg_range_1h or {}).get(ticker)
        ar = ar if isinstance(ar, (int, float)) and ar > 0 else None
        seen = {float(p) for p in exclude_prices if isinstance(p, (int, float))}
        picked = []
        for lv in level_universe(bars, now, ticker):
            if lv["name"] not in _RUNNING_NAMES and not lv.get("running"):
                continue
            price = float(lv["price"])
            ahead = price < now_price if want == "DOWN" else price > now_price
            if not ahead or price in seen:
                continue
            seen.add(price)
            picked.append((lv["name"], price))
        picked.sort(key=lambda e: -e[1] if want == "DOWN" else e[1])
        side = "below" if want == "DOWN" else "above"
        return [{"id": f"R{i + 1}", "level": name, "price": price, "body": None,
                 "tier": "running", "side": side,
                 "dist_ratio": (round(abs(price - now_price) / ar, 4)
                                if ar is not None else None),
                 "band": "RUNNING"}
                for i, (name, price) in enumerate(picked)]
    except Exception:
        return []


def select_target(bars: dict, now: pd.Timestamp, direction: str,
                  ticker: str = "MNQ", htf=None) -> "dict | None":
    """The D1 menu row for `direction` as of strictly before `now`, or None.

    `None` is a REAL outcome, not an error: `l2-target-selection.md` §5 measures the
    direction's menu as empty on 6.0% of sessions at 09:20, and an empty menu at the fill
    is the same condition read later. The caller must treat it as "no target", never as
    a reason to skip the fill that already happened.

    `htf` (plan 40) = {"as_of", "extremes", "seed"}: with `HTF_EXTREMES_IN_T2` on, the
    unnested weekly/monthly extremes still standing at `now` join the menu as
    `extra_pools` (and suppress the projection, Q1 = P1). A failure computing them costs
    the HTF rows only -- the menu is built without them and the row carries `htf_error`.
    """
    want = str(direction or "").upper()
    if want not in _DIRECTIONS:
        return None
    try:
        bundle = _bundle_for(bars, now)
        if bundle is None:
            return None
        extra, htf_error = None, None
        try:
            extra = menu_rows(_htf_pools(htf, bars, now, ticker)) or None
        except Exception as exc:
            htf_error = f"{type(exc).__name__}: {exc}"
        menus = build_menus(bundle, _validator_dict_for(bars, now, bundle),
                            extra_pools=extra)
        rows = (menus.get("dol") or {}).get(want) or ()
        if not rows:
            return None
        # Nearest-first D1 — `build_menus` already emits the rows in that order, which is
        # the same thing `study.target_offline._nearest_first_pick` relies on.
        row = dict(rows[0])
        if htf_error is not None:
            row["htf_error"] = htf_error
        return row if row.get("price") is not None else None
    except Exception:
        # Swallowed deliberately; see the module docstring. The Executor records the
        # miss as `target_selected` with pick=None, so it is visible in the artifact.
        return None
