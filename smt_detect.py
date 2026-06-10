# smt_detect.py
# SMT V2 — pure detection engine + accumulation buffers + reference consumer.
#
# Three pure detection functions (regular wick SMT, hidden body SMT, SMT-fill),
# an SmtBuffer (per-minute + 5m accumulator) and a PendingSmtWatch reference
# consumer (accumulate → preserve-by-copy → invalidate). Everything here is pure
# python/pandas with no broker/network/IO side effects; detection functions are
# total (return ([], state) on degenerate input, never raise) and take prior
# state + bars and return (new_events, updated_state) so they unit-test in
# isolation. The per-target state is a JSON-serializable dict so it persists via
# smts.json (smt_state.load_smts/save_smts).
#
# Direction conventions (mirror strategy_smt.py): a swept HIGH → short/bearish;
# a swept LOW → long/bullish; a bullish FVG fill → long; a bearish FVG fill → short.

from __future__ import annotations

import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Tunable thresholds (first-guess defaults; per-instrument scale). Exposed as
# module constants for later tuning — correctness tests assert behavior at/above/
# below threshold, not specific magnitudes.
# ---------------------------------------------------------------------------
MIN_REARM_OPP_MOVE_PTS_MNQ = 20.0   # MNQ leader opposite move (pts) that re-arms a level/FVG
MIN_REARM_OPP_MOVE_PTS_MES = 3.0    # MES counterpart (≈ MNQ/6.7 scale)
WATCH_CONFIRM_PTS_MNQ = 20.0        # trend-confirmation distance that invalidates a retained SMT
WATCH_CONFIRM_PTS_MES = 3.0

# Fulfillment thresholds (pts) by tier+instrument. After a level SMT fires, the swept
# level is "fulfilled" once price travels this far past the swept level in the SMT's
# direction (informational for fixed levels; a re-arm trigger for dynamic levels).
# First-guess defaults; tunable.
FULFILL_PTS_MNQ = {"week": 80.0, "day": 40.0, "session": 20.0}
FULFILL_PTS_MES = {"week": 12.0, "day": 6.0, "session": 3.0}

# Adverse-run invalidation — mirror of FULFILL_PTS. A fired, not-yet-fulfilled SMT is
# "invalidated" when MNQ close runs AGAINST its direction past the fire close by this much.
# Informational only this iteration (NOT a re-arm trigger; shadow-only — not wired to trades).
# `day` was widened 20→40 from an 8-day multi-regime shadow sweep (_shadow_smt_analysis.py): at
# 20 the fixed prev-day invalidations were ~57% "premature" (thesis still fulfilled within 180m);
# 40 cuts that to ~48% while keeping ~all the correct invalidations. (week/session left as the
# half-of-FULFILL default pending their own sweep.)
INVALIDATE_PTS_MNQ = {"week": 40.0, "day": 40.0, "session": 10.0}
INVALIDATE_PTS_MES = {"week": 6.0, "day": 6.0, "session": 1.5}

HIDDEN_TFS = ("15min", "30min")

# Per-session ET "forming" window (open_hour, close_hour) — a 6hr-session level is an
# eligible SMT target only once its session has CLOSED, i.e. when the ET hour is OUTSIDE
# this window. asia forms 18:00→24:00 (wraps midnight), so it is eligible during 00:00–18:00.
_SESSION_WINDOW = {
    "asia": (18, 24),        # forming 18:00–24:00 → eligible 00:00–18:00
    "london": (0, 6),        # forming 00:00–06:00 → eligible 06:00–24:00
    "ny_morning": (6, 12),   # forming 06:00–12:00 → eligible 12:00–06:00(next)
    "ny_evening": (12, 17),  # forming 12:00–17:00 → eligible 17:00–12:00(next)
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _rearm_pts(inst: str) -> float:
    return MIN_REARM_OPP_MOVE_PTS_MES if inst == "mes" else MIN_REARM_OPP_MOVE_PTS_MNQ


def _confirm_pts(inst: str) -> float:
    return WATCH_CONFIRM_PTS_MES if inst == "mes" else WATCH_CONFIRM_PTS_MNQ


def _level_class(name: str) -> tuple[str, str]:
    """Classify a level name into (kind, tier).

    kind is "dynamic" (re-armable) or "fixed" (single fire ever); tier selects the
    fulfillment threshold ("week" | "day" | "session").
      - week_*  -> ("dynamic", "week")     (running week high/low)
      - day_*   -> ("dynamic", "day")      (running day high/low)
      - prev*week* -> ("fixed", "week")    (prior-week high/low)
      - prev*day*  -> ("fixed", "day")     (prior-day high/low)
      - else (asia/london/ny_morning/ny_evening _high/_low) -> ("fixed", "session")
    """
    n = name or ""
    if n.startswith("week_"):
        return ("dynamic", "week")
    if n.startswith("day_"):
        return ("dynamic", "day")
    if n.startswith("prev"):
        if "week" in n:
            return ("fixed", "week")
        if "day" in n:
            return ("fixed", "day")
    return ("fixed", "session")


def _fulfill_pts(tier: str, inst: str) -> float:
    table = FULFILL_PTS_MES if inst == "mes" else FULFILL_PTS_MNQ
    return table.get(tier, table["session"])


def _invalidate_pts(tier: str, inst: str) -> float:
    table = INVALIDATE_PTS_MES if inst == "mes" else INVALIDATE_PTS_MNQ
    return table.get(tier, table["session"])


def _opposite(direction: str) -> str:
    return "short" if direction == "long" else "long"


def _skey(name: str, direction: str) -> str:
    """JSON-serializable string key for a (ref_name, direction) target."""
    return f"{name}|{direction}"


def _to_et_time(now: Any) -> "datetime.time | None":
    """ET wall-clock time of `now` (a pandas Timestamp / datetime). None on failure."""
    try:
        if getattr(now, "tzinfo", None) is not None:
            return now.tz_convert("America/New_York").time()
        return now.time()
    except Exception:
        return None


def _sub(name: str) -> "str | None":
    """'high' / 'low' for a named level, else None (mid levels are not touch targets)."""
    if name.endswith("_high"):
        return "high"
    if name.endswith("_low"):
        return "low"
    return None


def eligible_levels(liqs: list[dict], now: Any) -> dict[str, dict]:
    """Map level-name → {name, kind:'level', price, sub:'high'|'low'} for the levels
    eligible as SMT touch targets at `now`.

    Eligible = day_high/low + week_high/low (always) + 6hr-session highs/lows under a
    day-scoped rule (see below). day_mid / week_mid and any non-high/low names are not
    touch targets.

    6hr-session scoping by the ET clock:
      - During today's Asia session (18:00-24:00 ET): only YESTERDAY's NY sessions
        (ny_morning + ny_evening) are eligible. Today's forming Asia and the older
        yesterday sessions (asia/london) are excluded.
      - From London onward (00:00-17:59 ET): only TODAY's sessions that have closed
        since the 18:00 open — asia (>=00:00), london (>=06:00), ny_morning (>=12:00),
        ny_evening (>=17:00). This drops yesterday's stale ny_morning/ny_evening that
        would otherwise leak in during London / NY-morning.
    """
    et = _to_et_time(now)
    out: dict[str, dict] = {}
    for l in liqs or []:
        if l.get("kind") != "level":
            continue
        name = l.get("name", "")
        price = l.get("price")
        if price is None:
            continue
        sub = _sub(name)
        if sub is None:
            continue
        _cp = l.get("close_price")
        _entry_extra = {"close_price": float(_cp)} if _cp is not None else {}
        if name.startswith(("day_", "week_")):
            out[name] = {"name": name, "kind": "level", "price": float(price), "sub": sub, **_entry_extra}
            continue
        # Universe fixed levels (prev-day / prev-week extremes): completed history, so
        # always eligible — no session-window or running-extreme gating.
        if name.startswith("prev") and ("day" in name or "week" in name):
            out[name] = {"name": name, "kind": "level", "price": float(price), "sub": sub, **_entry_extra}
            continue
        # 6hr-session level: day-scoped eligibility (see docstring).
        sess = name.rsplit("_", 1)[0]
        win = _SESSION_WINDOW.get(sess)
        if win is None or et is None:
            continue
        if et.hour >= 18:
            # Today's Asia is forming → only yesterday's NY morning/evening levels.
            if sess not in ("ny_morning", "ny_evening"):
                continue
        else:
            # 00:00-17:59 ET → only today's sessions closed since the 18:00 open.
            # win[1] is the close hour (asia 24 -> 00:00); a session is eligible once
            # the clock has passed its close hour today.
            if et.hour < (win[1] % 24):
                continue
        out[name] = {"name": name, "kind": "level", "price": float(price), "sub": sub, **_entry_extra}
    return out


def _touched(sub: str, price: float, level_price: float, *, body: bool) -> bool:
    """Wick (body=False) or close (body=True) touch of a level.

    For a high: price (High or Close) >= level. For a low: price <= level.
    """
    if sub == "high":
        return price >= level_price
    return price <= level_price


# ---------------------------------------------------------------------------
# Regular (wick) + hidden (body) SMT detection
# ---------------------------------------------------------------------------
def _detect_level_smts(
    levels_mnq: dict[str, dict],
    levels_mes: dict[str, dict],
    mnq_bar: dict,
    mes_bar: dict,
    state: dict,
    *,
    body: bool,
    timeframe: str,
    rec_type: str,
) -> tuple[list[dict], dict]:
    """Shared engine for regular (wick) and hidden (body) SMTs.

    `body=False` uses bar High/Low (wick); `body=True` uses bar Close. A level is
    evaluated only when present in BOTH instruments. State is the dict keyed by
    _skey(name, direction); records are emitted on the armed rising edge with the
    §3.1 dual re-arm. Either instrument may lead (symmetric).
    """
    if state is None:
        state = {}
    records: list[dict] = []
    if not levels_mnq or not levels_mes or not mnq_bar or not mes_bar:
        return records, state

    iso = mnq_bar.get("time") or mes_bar.get("time") or ""
    mnq_close = float(mnq_bar.get("close", mnq_bar.get("Close", 0.0)))
    mes_close = float(mes_bar.get("close", mes_bar.get("Close", 0.0)))

    # Prior-bar MNQ close (the approach reference for fixed-level sweep direction). Stored
    # per rec_type under a reserved no-"|" key so the level-SMT post-pass skips it. Seeded to
    # the current close on the first bar (approach = current side).
    _prevref_key = "__prevref_" + rec_type
    prev_ref = float(state.get(_prevref_key, mnq_close))

    # Deterministic order so a same-bar opposite SMT can re-arm reproducibly.
    for name in sorted(levels_mnq.keys() & levels_mes.keys()):
        lvl_mnq = levels_mnq[name]
        lvl_mes = levels_mes[name]
        sub = lvl_mnq.get("sub")
        if sub is None or sub != lvl_mes.get("sub"):
            continue

        kind_cls, tier = _level_class(name)

        # Comparison level (same for both sweep directions). Hidden (body) SMTs reference the
        # BODY extreme — the lowest CLOSE for *_low / highest CLOSE for *_high over the level's
        # window — carried as `close_price`. Wick SMTs (and a body SMT on a level missing
        # close_price) use the wick `price`.
        if body:
            mnq_lvl_price = float(lvl_mnq.get("close_price", lvl_mnq["price"]))
            mes_lvl_price = float(lvl_mes.get("close_price", lvl_mes["price"]))
        else:
            mnq_lvl_price = float(lvl_mnq["price"])
            mes_lvl_price = float(lvl_mes["price"])

        # Direction by SWEEP/approach (universal per-level take-out), not by the level's
        # high/low name. A DOWN-sweep (price falling ONTO the level) is bullish; an UP-sweep
        # (rising INTO it) is bearish. A DYNAMIC running extreme is only ever swept one way (a
        # running high upward, a low downward), so its proven suffix mapping is kept. A FIXED
        # level (prev-day/week, 6hr-session) can be met from either side, so the approach side
        # — the prior MNQ close vs the level — picks it: above => down-sweep/bullish, below =>
        # up-sweep/bearish (exact tie falls back to the suffix default).
        if kind_cls == "dynamic":
            direction = "short" if sub == "high" else "long"
        elif prev_ref > mnq_lvl_price:
            direction = "long"
        elif prev_ref < mnq_lvl_price:
            direction = "short"
        else:
            direction = "short" if sub == "high" else "long"
        side = "bearish" if direction == "short" else "bullish"
        # State key includes rec_type so wick (regular) and body (hidden) SMTs on the same
        # (level, direction) are tracked INDEPENDENTLY — each fires/re-arms on its own.
        skey = f"{_skey(name, direction)}|{rec_type}"

        # Same-side divergence test for the active sweep direction. Bearish (up-sweep) uses the
        # HIGH wick (CLOSE for body); bullish (down-sweep) the LOW wick (CLOSE for body). The
        # leader is the instrument that takes the level out while the laggard does not.
        if body:
            mnq_val = mnq_close
            mes_val = mes_close
        elif direction == "short":
            mnq_val = float(mnq_bar["high"])
            mes_val = float(mes_bar["high"])
        else:
            mnq_val = float(mnq_bar["low"])
            mes_val = float(mes_bar["low"])

        _touch_sub = "high" if direction == "short" else "low"
        mnq_touch = _touched(_touch_sub, mnq_val, mnq_lvl_price, body=body)
        mes_touch = _touched(_touch_sub, mes_val, mes_lvl_price, body=body)

        # Determine leader (the one that took the level out while the other didn't).
        if mnq_touch and not mes_touch:
            leader, lead_price, lead_lvl = "mnq", mnq_close, mnq_lvl_price
            cond = True
        elif mes_touch and not mnq_touch:
            leader, lead_price, lead_lvl = "mes", mes_close, mes_lvl_price
            cond = True
        else:
            leader, lead_price, lead_lvl = "mnq", mnq_close, mnq_lvl_price
            cond = False

        st = state.get(skey)
        if st is None:
            st = {
                "armed": True,
                "last_cond": False,
                "fired": False,
                "fulfilled": False,
                "invalidated": False,
                "fire_price": None,
                "fire_level_price": None,
                "fire_mnq_close": None,
                "level_price": mnq_lvl_price,
            }
            state[skey] = st

        # (a) Fulfillment — once fired and not yet fulfilled, the SMT is fulfilled when MNQ
        # close FOLLOWS THROUGH FULFILL_PTS[tier] beyond the FIRE close (where price sat when
        # the SMT formed), in the SMT's favorable direction. Measuring from the fire close (not
        # the swept level) requires a genuine follow-through: a wick-sweep SMT closes far from
        # the level, so a level-based check would be satisfied the instant it fires and re-arm
        # immediately (the day_low flood). Computed for both kinds (informational for fixed; a
        # re-arm trigger for dynamic).
        if st.get("fired") and not st.get("fulfilled"):
            fc = st.get("fire_mnq_close")
            if fc is not None:
                pts = _fulfill_pts(tier, "mnq")
                if direction == "short":
                    if mnq_close <= float(fc) - pts:
                        st["fulfilled"] = True
                else:  # long
                    if mnq_close >= float(fc) + pts:
                        st["fulfilled"] = True

        # (a2) Adverse-run invalidation is maintained AFTER this loop (see the dedicated pass
        # below) — NOT here — because it must be checked independently of the current bar's
        # approach direction.

        # (b) Re-arm (only if currently dormant). Fixed levels NEVER re-arm. Dynamic levels
        # re-arm when the swept level was fulfilled OR an opposite-direction SMT is present
        # in this batch (a genuine regime flip).
        if not st["armed"] and kind_cls == "dynamic":
            opp_present = any(r.get("direction") == _opposite(direction) for r in records)
            if st.get("fulfilled") or opp_present:
                st["armed"] = True
                st["fired"] = False
                st["fulfilled"] = False
                st["invalidated"] = False

        # (c) Fire on the armed rising edge.
        if cond and not st["last_cond"] and st["armed"]:
            records.append({
                "kind": "smt",
                "type": rec_type,
                "side": side,
                "direction": direction,
                "timeframe": timeframe,
                "time": iso,
                "leader": leader,
                "ref_name": name,
                "mnq_price": mnq_close,
                "mes_price": mes_close,
                # The MNQ comparison level used for THIS detection (body-extreme when
                # body=True, wick price otherwise) so the smt-div signal/plot label can
                # reference the body extreme for hidden SMTs.
                "mnq_lvl_price": mnq_lvl_price,
                "mes_lvl_price": mes_lvl_price,
            })
            st["armed"] = False
            st["fired"] = True
            st["fulfilled"] = False
            st["invalidated"] = False
            st["fire_time"] = iso
            st["fire_price"] = lead_price
            st["fire_leader"] = leader
            st["fire_level_price"] = mnq_lvl_price
            st["fire_mnq_close"] = mnq_close

        st["last_cond"] = cond
        st["level_price"] = mnq_lvl_price

    # (a2) Adverse-run invalidation — the mirror of fulfillment, maintained INDEPENDENTLY of the
    # current bar's approach direction. The per-level loop above keys state by the direction the
    # current approach implies (prev_ref vs level price); but a fired SMT is invalidated precisely
    # when price runs to the OPPOSITE side of its level, which is exactly when that approach
    # direction flips — so a direction-keyed check would strand the original SMT's key (never
    # re-visited → never invalidated; the prev1_week_high|short 09:49 case). So sweep EVERY
    # fired-open state of this rec_type each bar and test the adverse-run condition against the
    # current MNQ close using the state's OWN stored direction (parsed from its key), regardless
    # of eligibility this bar. Informational only: sets the `invalidated` flag + appends to the
    # reserved trail key; never touches records/fire/fulfill/re-arm, so trades are unaffected.
    # Iterate a snapshot (list) because the first event creates the `__invalidations__` key.
    for skey, st in list(state.items()):
        if "|" not in skey:
            continue
        _parts = skey.split("|")
        if len(_parts) < 3 or _parts[2] != rec_type:
            continue
        if not (st.get("fired") and not st.get("fulfilled") and not st.get("invalidated")):
            continue
        fc = st.get("fire_mnq_close")
        if fc is None:
            continue
        _name, _dir = _parts[0], _parts[1]
        _kind_cls, _tier = _level_class(_name)
        inv = _invalidate_pts(_tier, "mnq")
        adverse = (
            (_dir == "short" and mnq_close >= float(fc) + inv)
            or (_dir == "long" and mnq_close <= float(fc) - inv)
        )
        if adverse:
            st["invalidated"] = True
            st["invalidated_time"] = iso
            st["invalidated_mnq_close"] = mnq_close
            state.setdefault("__invalidations__", []).append({
                "time": iso, "key": skey, "ref_name": _name, "tier": _tier,
                "kind": _kind_cls, "direction": _dir, "type": rec_type,
                "fire_time": st.get("fire_time"), "fire_mnq_close": float(fc),
                "trigger_mnq_close": mnq_close, "threshold_pts": inv,
                "reason": "adverse_run",
            })

    # Post-pass: an SMT in this batch re-arms any dormant DYNAMIC pair of the OPPOSITE
    # direction (order-independent — the opposite SMT may have been detected after the
    # dormant pair was visited above). A FIXED level is NEVER re-armed. The re-arm persists
    # in state so a later fresh re-touch re-fires.
    if records:
        batch_dirs = {r.get("direction") for r in records}
        for skey, st in state.items():
            # Only level-SMT entries (keyed "name|direction|type"); skip fill entries (FVG
            # names with no "|") which share this dict but have their own re-arm logic.
            if "|" not in skey or st.get("armed"):
                continue
            _parts = skey.split("|")
            if len(_parts) < 2:
                continue
            _name, _dir = _parts[0], _parts[1]
            if _level_class(_name)[0] != "dynamic":
                continue
            if _opposite(_dir) in batch_dirs:
                st["armed"] = True
                st["fired"] = False
                st["fulfilled"] = False
                st["invalidated"] = False

    # Persist the approach reference for next bar's fixed-level sweep-direction decision.
    state[_prevref_key] = mnq_close
    return records, state


def detect_regular_smts(
    levels_mnq: dict[str, dict],
    levels_mes: dict[str, dict],
    mnq_bar: dict,
    mes_bar: dict,
    state: dict,
) -> tuple[list[dict], dict]:
    """Regular (wick) SMT — per 1m bar. Leader wick touches its level, laggard doesn't."""
    return _detect_level_smts(
        levels_mnq, levels_mes, mnq_bar, mes_bar, state,
        body=False, timeframe="1m", rec_type="wick",
    )


def detect_hidden_smts(
    levels_mnq: dict[str, dict],
    levels_mes: dict[str, dict],
    mnq_tf_bar: dict,
    mes_tf_bar: dict,
    timeframe: str,
    state: dict,
) -> tuple[list[dict], dict]:
    """Hidden (body) SMT — per completed 15m/30m bar. Close-vs-level instead of wick."""
    return _detect_level_smts(
        levels_mnq, levels_mes, mnq_tf_bar, mes_tf_bar, state,
        body=True, timeframe=timeframe, rec_type="body",
    )


# ---------------------------------------------------------------------------
# SMT-fill detection (against paired 1hr FVGs)
# ---------------------------------------------------------------------------
def _fvg_progress(bar: dict, zone: dict, side: str = "bull") -> tuple[bool, bool]:
    """(entered, passed) for a bar's wick against an FVG zone [bottom, top].

    An FVG is FILLED by a retracement back into the gap:
      - a BULLISH FVG sits BELOW price and is filled by a move DOWN into it (near edge =
        top, far edge = bottom): entered = low <= top, passed = low <= bottom.
      - a BEARISH FVG sits ABOVE price and is filled by a move UP into it (near edge =
        bottom, far edge = top): entered = high >= bottom, passed = high >= top.
    entered = wick reaches the NEAR edge (inclusive); passed = wick crosses the FAR edge.
    A bar entirely on the approach side (e.g. a bull bar still above the zone) is neither.
    """
    top = float(zone["top"])
    bottom = float(zone["bottom"])
    hi = float(bar["high"])
    lo = float(bar["low"])
    if side == "bull":
        entered = lo <= top          # retraced DOWN to the near (top) edge
        passed = lo <= bottom        # crossed below the far (bottom) edge
    else:  # bear
        entered = hi >= bottom       # rallied UP to the near (bottom) edge
        passed = hi >= top           # crossed above the far (top) edge
    # `passed` implies `entered` (you cannot cross the far edge without entering).
    entered = entered or passed
    return entered, passed


def detect_fill_smts(
    paired_fvgs: list[dict],
    mnq_bar: dict,
    mes_bar: dict,
    state: dict,
) -> tuple[list[dict], dict]:
    """SMT-fills against per-instrument 1hr FVGs paired by 1hr bar (timestamp+side).

    `paired_fvgs` items: {name, side('bull'|'bear'), mnq:{top,bottom}, mes:{top,bottom}}.
    Fill-A: leader entered-or-passed, laggard not reached. Fill-B: both entered, one
    passed far edge, other still inside. Fill-B may follow Fill-A on the same FVG in one
    continuous move without re-arm; otherwise the §3.1 dual re-arm gates re-creation.
    """
    if state is None:
        state = {}
    records: list[dict] = []
    if not paired_fvgs or not mnq_bar or not mes_bar:
        return records, state

    iso = mnq_bar.get("time") or mes_bar.get("time") or ""
    mnq_close = float(mnq_bar.get("close", mnq_bar.get("Close", 0.0)))
    mes_close = float(mes_bar.get("close", mes_bar.get("Close", 0.0)))

    for fvg in sorted(paired_fvgs, key=lambda f: str(f.get("name"))):
        name = fvg.get("name")
        side = fvg.get("side")
        direction = "long" if side == "bull" else "short"
        rec_side = "bullish" if direction == "long" else "bearish"
        skey = str(name)

        st = state.get(skey)
        if st is None:
            st = {
                "armed": True,
                "fill_a_fired": False,
                "mnq": {"entered": False, "passed": False},
                "mes": {"entered": False, "passed": False},
                "fire_price": None,
                "direction": direction,
            }
            state[skey] = st

        mnq_entered_now, mnq_passed_now = _fvg_progress(mnq_bar, fvg["mnq"], side)
        mes_entered_now, mes_passed_now = _fvg_progress(mes_bar, fvg["mes"], side)

        # Re-arm: an opposite-direction SMT this batch clears dormancy AND resets
        # fill_a_fired so a fresh approach re-fires. (Points-based opposite-move re-arm
        # removed — same cooldown rationale as the level SMTs: it can't suppress chop
        # re-fires when the swing amplitude exceeds the threshold.)
        if not st["armed"]:
            if any(r.get("direction") == _opposite(direction) for r in records):
                st["armed"] = True
                st["fill_a_fired"] = False
                st["mnq"] = {"entered": False, "passed": False}
                st["mes"] = {"entered": False, "passed": False}

        # Latch cumulative per-instrument progress.
        st["mnq"]["entered"] = st["mnq"]["entered"] or mnq_entered_now
        st["mnq"]["passed"] = st["mnq"]["passed"] or mnq_passed_now
        st["mes"]["entered"] = st["mes"]["entered"] or mes_entered_now
        st["mes"]["passed"] = st["mes"]["passed"] or mes_passed_now

        m_e, m_p = st["mnq"]["entered"], st["mnq"]["passed"]
        s_e, s_p = st["mes"]["entered"], st["mes"]["passed"]

        # Fill-A: leader entered-or-passed AND laggard not reached (not entered).
        a_mnq = (m_e or m_p) and not s_e
        a_mes = (s_e or s_p) and not m_e
        if st["armed"] and not st["fill_a_fired"] and (a_mnq or a_mes):
            leader = "mnq" if a_mnq else "mes"
            records.append({
                "kind": "fill",
                "type": "fill_a",
                "side": rec_side,
                "direction": direction,
                "timeframe": "1h",
                "time": iso,
                "leader": leader,
                "ref_name": name,
                "mnq_price": mnq_close,
                "mes_price": mes_close,
            })
            st["fill_a_fired"] = True
            st["armed"] = False
            # fire_price is the MNQ close at fire time — the re-arm opposite-move gate
            # measures against MNQ consistently (scale-safe regardless of which led).
            st["fire_price"] = mnq_close

        # Fill-B: both entered, one passed far edge, other still inside (entered, not passed).
        b_mnq = m_e and s_e and m_p and not s_p
        b_mes = m_e and s_e and s_p and not m_p
        if not st.get("fill_b_fired") and (st["fill_a_fired"] or st["armed"]) and (b_mnq or b_mes):
            leader = "mnq" if b_mnq else "mes"
            records.append({
                "kind": "fill",
                "type": "fill_b",
                "side": rec_side,
                "direction": direction,
                "timeframe": "1h",
                "time": iso,
                "leader": leader,
                "ref_name": name,
                "mnq_price": mnq_close,
                "mes_price": mes_close,
            })
            st["fill_b_fired"] = True
            st["armed"] = False
            st["fire_price"] = mnq_close

    return records, state


# ---------------------------------------------------------------------------
# Contract C — read-only fulfillment-query API (SMT V2 Phase 2)
# ---------------------------------------------------------------------------
def _record_key(record: dict) -> str:
    """Single source of truth for the `detect_state` key of an SMT/fill record.

    Mirrors the detection engine's key construction EXACTLY so detection and query
    agree by construction:
      - level SMT (kind=="smt"): ``f"{ref_name}|{direction}|{type}"`` (type ∈
        {"wick","body"}) — see ``_detect_level_smts`` (skey at line 224:
        ``f"{_skey(name, direction)}|{rec_type}"``).
      - fill (kind=="fill"): the bare FVG name ``str(ref_name)`` — see
        ``detect_fill_smts`` (skey at line 443: ``skey = str(name)``).

    Total: missing fields → best-effort string; never raises.
    """
    if not isinstance(record, dict):
        return ""
    ref_name = record.get("ref_name")
    if record.get("kind") == "fill":
        return str(ref_name)
    direction = record.get("direction")
    rec_type = record.get("type")
    return f"{_skey(ref_name, direction)}|{rec_type}"


def fulfillment_status(keys: "list[str] | None", detect_state: dict) -> dict[str, str]:
    """Per-key fulfillment status over ``detect_state`` (read-only; never mutates).

    Returns ``{key: "unfulfilled" | "fulfilled" | "gone"}``:
      - ``"gone"``       — key absent from ``detect_state`` (expired / never fired).
      - ``"fulfilled"``  — present and ``st.get("fulfilled") is True``.
      - ``"unfulfilled"``— present and not fulfilled.

    Fills: fill state dicts (keyed by bare FVG name) have NO ``fulfilled`` field, so
    ``st.get("fulfilled")`` is falsy → a present, non-fulfilled fill key is
    ``"unfulfilled"`` by definition in Phase 2 (Phase 3 may extend fill fulfillment).

    Total: ``keys=None`` → ``{}``; never raises.
    """
    out: dict[str, str] = {}
    if not keys:
        return out
    ds = detect_state if isinstance(detect_state, dict) else {}
    for key in keys:
        st = ds.get(key)
        if st is None:
            out[key] = "gone"
        elif isinstance(st, dict) and st.get("fulfilled") is True:
            out[key] = "fulfilled"
        else:
            out[key] = "unfulfilled"
    return out


# ---------------------------------------------------------------------------
# Accumulation buffers
# ---------------------------------------------------------------------------
class SmtBuffer:
    """Per-minute buffer (overwritten each bar) + 5m accumulator (drained at the 5m
    boundary AFTER consumers). 1m-cadence consumers read the per-minute buffer; 5m-
    cadence consumers read the accumulator window since the last drain."""

    def __init__(self) -> None:
        self._per_minute: list[dict] = []
        self._accum: list[dict] = []
        self._last_drain_5m: Any = None

    def add(self, records: list[dict], bar_ts: Any) -> None:
        self._per_minute = list(records or [])
        if records:
            self._accum.extend(records)

    def get_new(self, cadence: str) -> list[dict]:
        if cadence == "1m":
            return list(self._per_minute)
        return list(self._accum)

    def drain_if_boundary(self, now: Any) -> None:
        """Clear the 5m accumulator when the 5m floor advances. Call AFTER consumers."""
        try:
            floor5 = now.floor("5min")
        except Exception:
            floor5 = now
        if floor5 != self._last_drain_5m:
            self._accum = []
            self._last_drain_5m = floor5


# ---------------------------------------------------------------------------
# Reference consumer — PendingSmtWatch (lifecycle only, no emit)
# ---------------------------------------------------------------------------
class PendingSmtWatch:
    """Accumulate → preserve-by-copy → invalidate. While flat, copy new SMTs into a
    retained set so they survive the buffer drain; drop a retained SMT when a
    confirming trend move occurs (the expected move happened) or when contradicted by
    an opposite-direction SMT in the latest ingest. Pure bookkeeping — no events."""

    def __init__(self, retained: "list[dict] | None" = None) -> None:
        self._retained: list[dict] = [dict(r) for r in (retained or [])]

    def ingest(self, records: list[dict]) -> None:
        """Shallow-copy new SMT records into the retained set (detached from the buffer)."""
        for r in records or []:
            rec = dict(r)
            rec.setdefault("_ingest_mnq", rec.get("mnq_price"))
            rec.setdefault("_ingest_mes", rec.get("mes_price"))
            self._retained.append(rec)

    def update(self, now: Any, mnq_price: float, mes_price: float) -> None:
        """Drop retained SMTs whose expected trend move occurred, or that were
        contradicted by an opposite-direction SMT in the retained set."""
        if not self._retained:
            return
        # Contradiction: an opposite-direction SMT among the currently-retained set
        # invalidates the opposite-direction retained SMTs (mutual cancel).
        directions = {r.get("direction") for r in self._retained}
        kept: list[dict] = []
        for r in self._retained:
            direction = r.get("direction")
            # Confirming trend move since retention (per instrument, either suffices).
            base_mnq = float(r.get("_ingest_mnq", r.get("mnq_price", 0.0)) or 0.0)
            base_mes = float(r.get("_ingest_mes", r.get("mes_price", 0.0)) or 0.0)
            if direction == "long":
                moved = (mnq_price - base_mnq) >= _confirm_pts("mnq") or \
                        (mes_price - base_mes) >= _confirm_pts("mes")
            else:
                moved = (base_mnq - mnq_price) >= _confirm_pts("mnq") or \
                        (base_mes - mes_price) >= _confirm_pts("mes")
            contradicted = _opposite(direction) in directions
            if moved or contradicted:
                continue
            kept.append(r)
        self._retained = kept

    def retained(self) -> list[dict]:
        return list(self._retained)

    def to_dict(self) -> dict:
        return {"retained": [dict(r) for r in self._retained]}

    @classmethod
    def from_dict(cls, d: "dict | None") -> "PendingSmtWatch":
        d = d or {}
        return cls(retained=d.get("retained", []))
