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
        if name.startswith(("day_", "week_")):
            out[name] = {"name": name, "kind": "level", "price": float(price), "sub": sub}
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
        out[name] = {"name": name, "kind": "level", "price": float(price), "sub": sub}
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

    # Deterministic order so a same-bar opposite SMT can re-arm reproducibly.
    for name in sorted(levels_mnq.keys() & levels_mes.keys()):
        lvl_mnq = levels_mnq[name]
        lvl_mes = levels_mes[name]
        sub = lvl_mnq.get("sub")
        if sub is None or sub != lvl_mes.get("sub"):
            continue
        direction = "short" if sub == "high" else "long"
        side = "bearish" if direction == "short" else "bullish"
        skey = _skey(name, direction)

        # Per-instrument touch value (wick or close).
        if body:
            mnq_val = mnq_close
            mes_val = mes_close
        else:
            mnq_val = float(mnq_bar["high"]) if sub == "high" else float(mnq_bar["low"])
            mes_val = float(mes_bar["high"]) if sub == "high" else float(mes_bar["low"])

        mnq_lvl_price = float(lvl_mnq["price"])
        mes_lvl_price = float(lvl_mes["price"])
        mnq_touch = _touched(sub, mnq_val, mnq_lvl_price, body=body)
        mes_touch = _touched(sub, mes_val, mes_lvl_price, body=body)

        # Determine leader (the one that touched while the other didn't).
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
            st = {"armed": True, "last_cond": False, "fire_price": None, "level_price": mnq_lvl_price}
            state[skey] = st

        # Cooldown: once a (level, direction) fires, it stays DORMANT and does NOT re-fire
        # on subsequent touches of the same level — even as price oscillates across it, and
        # even as a running level's terminal price ticks. It re-arms ONLY when an
        # opposite-direction SMT is created (a genuine regime flip); a fresh re-touch can
        # then fire again. (A points-based opposite-move re-arm was intentionally removed: it
        # cannot suppress chop re-fires when the swing amplitude exceeds the threshold — price
        # wicks the level, runs tens of points away, and wicks it again, re-arming each time.)
        if not st["armed"]:
            if any(r.get("direction") == _opposite(direction) for r in records):
                st["armed"] = True

        fired = None
        if cond and not st["last_cond"] and st["armed"]:
            fired = {
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
            }
            records.append(fired)
            st["armed"] = False
            st["fire_price"] = lead_price
            st["fire_leader"] = leader

        st["last_cond"] = cond
        st["level_price"] = mnq_lvl_price

    # Post-pass: an SMT in this batch re-arms any dormant pair of the OPPOSITE direction
    # (order-independent — the opposite SMT may have been detected after the dormant pair
    # was visited above). The re-arm persists in state so a later fresh re-touch re-fires.
    if records:
        batch_dirs = {r.get("direction") for r in records}
        for skey, st in state.items():
            # Only level-SMT entries (keyed "name|direction"); skip fill entries (FVG
            # names with no "|") which share this dict but have their own re-arm logic.
            if "|" not in skey or st.get("armed"):
                continue
            _dir = skey.rsplit("|", 1)[-1]
            if _opposite(_dir) in batch_dirs:
                st["armed"] = True

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

    Side-aware: a bullish FVG is approached from BELOW (near edge = bottom, far edge =
    top); a bearish FVG from ABOVE (near edge = top, far edge = bottom).

    entered = wick reaches into the zone (crosses the NEAR edge, not the far). Inclusive
              at the near edge. For a bull zone the bar's high reaches the bottom; for a
              bear zone the bar's low reaches the top.
    passed  = wick crosses fully through the FAR edge (inclusive). Bull → high ≥ top;
              bear → low ≤ bottom.
    A bar entirely on the approach side (e.g. a bull bar well below the zone) is neither
    entered nor passed.
    """
    top = float(zone["top"])
    bottom = float(zone["bottom"])
    hi = float(bar["high"])
    lo = float(bar["low"])
    if side == "bear":
        entered = lo <= top          # reached down into the zone from above
        passed = lo <= bottom        # crossed below the far (bottom) edge
    else:  # bull (default)
        entered = hi >= bottom       # reached up into the zone from below
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
