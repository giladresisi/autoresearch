# smt_reversal_lock.py
# GIL-32 Phase-1b — same-liquidity reversal lock (protect-existing).
#
# Once a with-SMT reversal hypothesis has formed on a liquidity level L (a "down" hypothesis
# backed by a bearish SMT on a high level, or "up" backed by a bullish SMT on a low),
# DISALLOW the opposite hypothesis on that SAME liquidity until the protecting SMT is either
# LEVEL-ACCEPTED-through (price closes decisively beyond L and holds) or FULFILLED. While the
# lock is protecting, `hypothesis._determine_direction` forces rule2b's decision back to the
# SMT side whenever it would otherwise return the opposite direction off that same swept level
# — which, because the forced side equals the already-formed hypothesis direction, produces NO
# reform/position-reset (none->dir gate, hypothesis.py) and so PRESERVES the in-progress entry
# setup. It does not flip or re-arm anything.
#
# WHY THE LOCK IS ARMED AT SMT FIRE-TIME (not from the live conviction set):
# On a clean reversal day the bearish high-SMT fires EARLY (before price tops), then price runs
# UP the manipulation/stop-run leg PAST the level — which trips the conviction set's (and
# detect_state's) adverse-run eviction (~40pt, close vs fire) — and only THEN reverses, by which
# point the "down" hypothesis finally forms on the swept high but the SMT that should authorize
# it is already gone (06-10: fires 10:03-10:12 @29085-29137, evicted by the 29168->29250 pop,
# down hyp forms 10:23 with an EMPTY conviction set). So the lock keeps its OWN durable ledger:
# a fire opens a (non-protecting) lock whose survival is LEVEL ACCEPTANCE — a close beyond the
# level by a structural buffer, sustained N closes (mirrors GIL-25's level-relative depletion,
# NOT the fire-close adverse-run). The lock becomes `protecting` the bar a same-side reversal
# hypothesis forms on the level; only protecting locks veto.
#
# Pure python: JSON-serializable in/out, no IO, never raises. Every entry point is total.

from __future__ import annotations

# ---------------------------------------------------------------------------
# Tunable module constants (documented together).
# ---------------------------------------------------------------------------
LOCK_ACCEPT_BUFFER_PCT = 0.005  # close must exceed the level by this fraction to count as acceptance.
LOCK_ACCEPT_SUSTAIN = 2         # consecutive accepting closes required to release (the level break).
LOCK_MAX_AGE_MIN = 240          # safety: a lock self-releases this many minutes after the FIRE.


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _is_high_level(name: "str | None") -> bool:
    return bool(name) and str(name).endswith("high")


def _is_low_level(name: "str | None") -> bool:
    return bool(name) and str(name).endswith("low")


def _parse_iso(iso: "str | None"):
    if not iso:
        return None
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(iso))
    except (ValueError, TypeError):
        return None


def _minutes_between(start_iso: "str | None", now_iso: "str | None"):
    a = _parse_iso(start_iso)
    b = _parse_iso(now_iso)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60.0


def _record_status(keys: "list[str] | None", status_map: dict) -> str:
    """Aggregate status over a lock's underlying detect keys: fulfilled if ANY fulfilled;
    gone if ALL present are gone; else unfulfilled. Mirrors smt_conviction._collapsed_status."""
    ks = [k for k in (keys or []) if k]
    if not ks:
        return "unfulfilled"
    sts = [status_map.get(k, "unfulfilled") for k in ks]
    if any(s == "fulfilled" for s in sts):
        return "fulfilled"
    if all(s == "gone" for s in sts):
        return "gone"
    return "unfulfilled"


def _detect_key(ref_name, direction, rec_type) -> str:
    """detect_state key for a record (mirror smt_detect._record_key / smt_conviction)."""
    if rec_type in ("wick", "body"):
        return f"{ref_name}|{direction}|{rec_type}"
    return str(ref_name)


# ---------------------------------------------------------------------------
# ingest_fires — open/refresh a (non-protecting) lock for each reversal-SMT fire
# ---------------------------------------------------------------------------
def ingest_fires(
    prev_locks: "list[dict] | None",
    fired_records: "list[dict] | None",
    level_price_map: "dict | None",
    now_iso: str,
) -> list[dict]:
    """Open (or refresh) a lock for each fired record that is a bearish HIGH-level SMT or a
    bullish LOW-level SMT. Collapse by (level_name, side); a refresh PRESERVES `protecting`,
    `armed_iso`, `accept_streak`, and the original `fire_iso` (so the level-acceptance progress
    and age clock are not reset by a later same-level fire). Returns a NEW list; never raises.

    fvg fills (ref_name like 'fvg_…') do not end in 'high'/'low' → naturally excluded; only the
    structural high/low LEVEL SMTs the user scoped this to are tracked.
    """
    try:
        locks: list = [dict(l) for l in (prev_locks or []) if isinstance(l, dict)]
        lpm = level_price_map if isinstance(level_price_map, dict) else {}
        by_key = {(l.get("level_name"), l.get("side")): l for l in locks}

        for raw in (fired_records or []):
            if not isinstance(raw, dict):
                continue
            ref = raw.get("ref_name")
            side = raw.get("side")
            direction = raw.get("direction") or ("short" if side == "bearish" else
                                                 "long" if side == "bullish" else None)
            if not side and direction:
                side = "bearish" if direction == "short" else "bullish"
            # Only bearish-on-high and bullish-on-low are reversal-of-a-level SMTs.
            if side == "bearish" and _is_high_level(ref):
                locked_dir = "down"
            elif side == "bullish" and _is_low_level(ref):
                locked_dir = "up"
            else:
                continue

            # level price: prefer the eligible-level map; fall back to the fire (mnq) price.
            lp = lpm.get(ref)
            if lp is None:
                lp = raw.get("mnq_price")
            try:
                lp = float(lp) if lp is not None else None
            except (TypeError, ValueError):
                lp = None
            if lp is None:
                continue

            rec_type = raw.get("type")
            key = _detect_key(ref, direction, rec_type)

            existing = by_key.get((ref, side))
            if existing is None:
                rec = {
                    "level_name": ref,
                    "side": side,
                    "locked_dir": locked_dir,
                    "level_price": lp,
                    "fire_iso": raw.get("time") or now_iso,
                    "armed_iso": None,        # set when it becomes protecting.
                    "accept_streak": 0,
                    "protecting": False,
                    "keys": [key],
                }
                locks.append(rec)
                by_key[(ref, side)] = rec
            else:
                existing["level_price"] = lp  # refresh to the latest eligible level price.
                ks = list(existing.get("keys") or [])
                if key not in ks:
                    ks.append(key)
                existing["keys"] = ks
        return locks
    except Exception:
        return [dict(l) for l in (prev_locks or []) if isinstance(l, dict)]


# ---------------------------------------------------------------------------
# advance — one-bar lifecycle: level-acceptance release / fulfill / age-out
# ---------------------------------------------------------------------------
def advance(
    prev_locks: "list[dict] | None",
    level_price_map: "dict | None",
    status_map: "dict | None",
    mnq_close: float,
    now_iso: str,
) -> list[dict]:
    """Advance every lock one bar and drop released ones.

    Release a lock when ANY of:
      - LEVEL ACCEPTANCE: `accept_streak >= LOCK_ACCEPT_SUSTAIN`, where a bar is "accepting" when
        bearish: mnq_close > level_price*(1+BUFFER); bullish: mnq_close < level_price*(1-BUFFER).
        (accept_streak resets to 0 on any non-accepting close — a sub-buffer pop never releases.)
      - the protecting SMT is `fulfilled` or `gone` in status_map.
      - age since FIRE exceeds LOCK_MAX_AGE_MIN.

    Returns the kept locks (NEW list). Total; never raises.
    """
    try:
        lpm = level_price_map if isinstance(level_price_map, dict) else {}
        sm = status_map if isinstance(status_map, dict) else {}
        try:
            mc = float(mnq_close)
        except (TypeError, ValueError):
            mc = None

        kept: list = []
        for raw in (prev_locks or []):
            if not isinstance(raw, dict):
                continue
            lock = dict(raw)

            # fulfilled / gone → release.
            status = _record_status(lock.get("keys"), sm)
            if status in ("fulfilled", "gone"):
                continue

            # age-out (from FIRE) → release.
            age = _minutes_between(lock.get("fire_iso"), now_iso)
            if age is not None and age > LOCK_MAX_AGE_MIN:
                continue

            # refresh level price from the current level map if available.
            lvl = lock.get("level_name")
            if lvl in lpm:
                try:
                    lock["level_price"] = float(lpm[lvl])
                except (TypeError, ValueError):
                    pass

            # level-acceptance streak.
            try:
                lp = float(lock.get("level_price"))
            except (TypeError, ValueError):
                lp = None
            if mc is not None and lp is not None:
                if lock.get("side") == "bearish":
                    accepting = mc > lp * (1.0 + LOCK_ACCEPT_BUFFER_PCT)
                else:  # bullish
                    accepting = mc < lp * (1.0 - LOCK_ACCEPT_BUFFER_PCT)
                if accepting:
                    lock["accept_streak"] = int(lock.get("accept_streak") or 0) + 1
                else:
                    lock["accept_streak"] = 0
                if int(lock.get("accept_streak") or 0) >= LOCK_ACCEPT_SUSTAIN:
                    continue  # level accepted → released.

            kept.append(lock)
        return kept
    except Exception:
        return [dict(l) for l in (prev_locks or []) if isinstance(l, dict)]


# ---------------------------------------------------------------------------
# mark_protecting — promote a lock when a same-side reversal hypothesis forms on its level
# ---------------------------------------------------------------------------
def mark_protecting(
    locks: "list[dict] | None",
    direction: "str | None",
    last_swept_level: "str | None",
) -> list[dict]:
    """When a reversal hypothesis forms (down on a HIGH / up on a LOW) on `last_swept_level`,
    flip `protecting=True` on the matching lock (down↔bearish, up↔bullish) and stamp `armed_iso`.
    Only protecting locks veto. Returns the SAME list (mutates the matched lock in place); total.
    """
    try:
        if direction not in ("down", "up") or not last_swept_level:
            return locks or []
        want_side = "bearish" if direction == "down" else "bullish"
        for l in (locks or []):
            if not isinstance(l, dict):
                continue
            if l.get("level_name") == last_swept_level and l.get("side") == want_side:
                if not l.get("protecting"):
                    l["protecting"] = True
                    if not l.get("armed_iso"):
                        l["armed_iso"] = l.get("fire_iso")
        return locks or []
    except Exception:
        return locks or []


# ---------------------------------------------------------------------------
# vetoes — the direction-engine consumer (only PROTECTING locks veto)
# ---------------------------------------------------------------------------
def vetoes(
    locks: "list[dict] | None",
    r2b_dir: "str | None",
    last_swept_level: "str | None",
) -> "str | None":
    """If a PROTECTING lock on `last_swept_level` opposes `r2b_dir`, return the locked (SMT-side)
    direction to force; else None.

      - r2b_dir == "up"   and a protecting bearish lock on last_swept_level  →  "down"
      - r2b_dir == "down" and a protecting bullish lock on last_swept_level  →  "up"

    Total; never raises."""
    try:
        if not last_swept_level or r2b_dir not in ("up", "down"):
            return None
        for l in (locks or []):
            if not isinstance(l, dict):
                continue
            if not l.get("protecting"):
                continue
            if l.get("level_name") != last_swept_level:
                continue
            if r2b_dir == "up" and l.get("side") == "bearish":
                return "down"
            if r2b_dir == "down" and l.get("side") == "bullish":
                return "up"
        return None
    except Exception:
        return None
