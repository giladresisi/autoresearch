"""POC hard-truth extractor — computes FACTS ONLY from the two bar slices.

Usage (from anywhere):
    python derive_facts.py [--dir <folder-with-slices>] [--ath-mnq X] [--ath-mes Y]

Reads MNQ_1s_slice.parquet + MES_1s_slice.parquet from --dir (default: this file's
folder), treats the last bar as "now", and prints the deterministic context the
decision protocols need: level map (wick + body extremes), sweep times + depletion
states, cross-ticker sweep matrix (wick and 15m-body), equilibrium acceptance,
structure bars, FVGs (both tickers), checkpoint snapshot.

It makes NO decisions: no votes, no ledgers, no SMT fire adjudication, no
direction/confidence. Those are the decision agent's job per
decisions/daily-trend.md and decisions/next-move.md.

Engine-grounded conventions (v2 — aligned to the live engine after POC run 3):
- Bars in the CME maintenance window (after 16:55:00 ET, before 18:00 ET) are DROPPED;
  weekend/stub trade dates (Sat/Sun or < 1000 bars) are excluded from the date universe.
- True Day = 18:00 ET -> 16:55 ET next day; trade date = close date.
- Week extremes/mid use the ENGINE anchor (session_pipeline._week_start_ts):
  Sunday 18:00 ET, EXTENDED for early-week sessions — Monday session -> prev Thursday
  18:00 ET, Tuesday session -> prev Friday 18:00 ET.
- Sweeps are INCLUSIVE (low <= level / high >= level), matching the engine.
- Depletion thresholds: MNQ week 80 / day 40 / session 20; MES week 12 / day 6 /
  session 3 (exact engine tables, not a ratio); DEPLETED at excursion >= threshold.
- Levels carry BODY (close) extremes too; body sweeps are reported on 15min closes
  (hidden-SMT convention, smt_detect HIDDEN_TFS).
- 4hr/1hr bars are MIDNIGHT-anchored resamples (engine convention, hypothesis.py);
  an 18:00-anchored 4hr table is printed as auxiliary only.
- FVGs on COMPLETED 1hr and 4hr bars, BOTH tickers; "visited" counts any 1s re-entry
  strictly after the third bar's OPEN label (daily.py convention).
"""

import argparse
import datetime
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

TZ = "America/New_York"
DEPLETE = {
    "MNQ": {"week": 80.0, "day": 40.0, "session": 20.0},
    "MES": {"week": 12.0, "day": 6.0, "session": 3.0},
}


def load(path):
    df = pd.read_parquet(path).rename(columns=str.lower)
    df = df[["open", "high", "low", "close"]]
    if df.index.tz is None:
        df.index = df.index.tz_localize(TZ)
    # Engine processed window: drop CME maintenance bars (16:55, 18:00) — stray ticks
    # there create phantom sessions and corrupt TDO/prev-day levels (POC run-3 G12).
    t = df.index.time
    keep = (t <= datetime.time(16, 55)) | (t >= datetime.time(18, 0))
    return df[keep]


def trade_date(ts):
    return (ts + pd.Timedelta(hours=7)).date()


def week_start_ts(now):
    """Engine week anchor (session_pipeline._week_start_ts): Sun 18:00 ET, extended
    for early-week sessions (Mon session -> prev Thu 18:00; Tue session -> prev Fri)."""
    today = now.date()
    session_open = today if now.hour >= 18 else today - datetime.timedelta(days=1)
    wd = session_open.weekday()  # Mon=0 .. Sun=6
    if wd == 6:      # Sunday open -> Monday session
        anchor = session_open - datetime.timedelta(days=3)  # prev Thursday
    elif wd == 0:    # Monday open -> Tuesday session
        anchor = session_open - datetime.timedelta(days=3)  # prev Friday
    else:
        anchor = session_open - datetime.timedelta(days=(wd + 1) % 7)
    return pd.Timestamp(datetime.datetime(anchor.year, anchor.month, anchor.day, 18, 0), tz=TZ)


def ohlc(df, rule, offset=None):
    return df.resample(rule, offset=offset).agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    ).dropna()


def session_frame(df, td):
    # Vectorised trade-date mask (identical values to df.index.map(trade_date), but a
    # single C-level shift+.date instead of a Python per-row call — this is on the AI-decisions
    # hot path, called dozens of times per snapshot).
    tds = (df.index + pd.Timedelta(hours=7)).date
    return df[tds == td]


def sub_blocks(sess):
    hours = sess.index.hour
    return {
        "asia": sess[(hours >= 18)],
        "london": sess[(hours >= 0) & (hours < 6)],
        "ny_morning": sess[(hours >= 6) & (hours < 12)],
        "ny_evening": sess[(hours >= 12) & (hours < 17)],
    }


def hl(frame):
    if len(frame) == 0:
        return None, None, None, None
    return (float(frame["high"].max()), float(frame["low"].min()),
            float(frame["close"].max()), float(frame["close"].min()))


def first_cross(frame, level, side):
    """Inclusive wick cross (engine convention: <= / >=)."""
    hits = frame[frame["low"] <= level] if side == "below" else frame[frame["high"] >= level]
    return hits.index[0] if len(hits) else None


def first_body_cross_15m(frame, level, side):
    """First 15min bar CLOSE beyond the level (hidden/body-SMT convention)."""
    if len(frame) == 0:
        return None
    m15 = frame["close"].resample("15min").last().dropna()
    hits = m15[m15 <= level] if side == "below" else m15[m15 >= level]
    return hits.index[0] if len(hits) else None


def excursion_beyond(frame, level, side):
    if side == "below":
        return max(0.0, level - float(frame["low"].min()))
    return max(0.0, float(frame["high"].max()) - level)


def closest_approach(frame, level, side):
    """For a NOT-swept level: how close price came and when (laggard test-and-fail data)."""
    if len(frame) == 0:
        return None, None
    if side == "below":
        return float(frame["low"].min()) - level, frame["low"].idxmin()
    return level - float(frame["high"].max()), frame["high"].idxmax()


def age_min(ts, now):
    return (now - ts).total_seconds() / 60.0


def fvgs(bars):
    out = []
    for i in range(1, len(bars) - 1):
        ph, pl = bars["high"].iloc[i - 1], bars["low"].iloc[i - 1]
        nh, nl = bars["high"].iloc[i + 1], bars["low"].iloc[i + 1]
        if nl > ph:
            out.append((bars.index[i], "bull", float(ph), float(nl), bars.index[i + 1]))
        elif nh < pl:
            out.append((bars.index[i], "bear", float(nh), float(pl), bars.index[i + 1]))
    return out


def mid_cross_table(closes_1m, mid_1m, cap=24):
    j = pd.DataFrame({"close": closes_1m, "mid": mid_1m}).dropna()
    j["above"] = j["close"] > j["mid"]
    flips = j[j["above"] != j["above"].shift()][1:]
    return j, flips.tail(cap)


def compute_two(df, week_tds, now):
    """TWO with daily.py's fallback chain: Monday 18:00 -> (if before it) prev Monday
    18:00 -> Monday 00:00 -> first bar of the week."""
    if not week_tds:
        return None, None
    monday = min(week_tds)
    mon18 = pd.Timestamp(monday, tz=TZ) + pd.Timedelta(hours=18)
    bars = df.loc[mon18:]
    if len(bars) and bars.index[0] - mon18 < pd.Timedelta(hours=1):
        return float(bars["open"].iloc[0]), bars.index[0]
    if now < mon18:
        prev18 = mon18 - pd.Timedelta(days=7)
        bars = df.loc[prev18:]
        if len(bars) and bars.index[0] - prev18 < pd.Timedelta(hours=1):
            return float(bars["open"].iloc[0]), bars.index[0]
    mon00 = pd.Timestamp(monday, tz=TZ)
    bars = df.loc[mon00:]
    if len(bars) and bars.index[0] - mon00 < pd.Timedelta(hours=1):
        return float(bars["open"].iloc[0]), bars.index[0]
    wk = pd.concat([session_frame(df, d) for d in week_tds])
    return (float(wk["open"].iloc[0]), wk.index[0]) if len(wk) else (None, None)


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)


@dataclass
class FactsBundle:
    """Structured result of compute_facts: the rendered fact-sheet lines plus the
    load-bearing structured values (used by the online adapter, records, and the
    validator view). `lines` reproduces the exact stdout the print-script emitted."""

    lines: list = field(default_factory=list)
    now: Optional[pd.Timestamp] = None
    td_now: Optional[datetime.date] = None
    ckpt: Optional[pd.Timestamp] = None
    # levels[tkr][name] = (price, body_price, side, tier, active_from) — the S1 map.
    levels: dict = field(default_factory=dict)
    now_price: Optional[float] = None   # MNQ last close (the decision ticker's price)
    day_mid: Optional[float] = None     # MNQ running day mid at now (S8 menu input)
    weekly_mid: Optional[float] = None  # MNQ running week mid at now (thesis.md P3/P4 input)
    # S8 menus (plan 11): computed lazily by facts_to_validator_dict / render_menus_text.
    menus: Optional[dict] = None


def render_facts_text(bundle: FactsBundle) -> str:
    """Reproduce the exact stdout the print-script produced (byte-for-byte).

    Each captured line corresponds to one `print(...)` call; joining with a
    newline and a trailing newline reconstructs stdout exactly.
    """
    return "\n".join(bundle.lines) + "\n"


def _parse_facts():
    """Lazily import calibration.validate_results.parse_facts (the text→dict
    reverse the semantic validator consumes) without a hard module dependency."""
    calib = os.path.join(REPO_ROOT, "calibration")
    if calib not in sys.path:
        sys.path.insert(0, calib)
    from validate_results import parse_facts  # noqa: E402
    return parse_facts


def facts_to_validator_dict(bundle: FactsBundle) -> dict:
    """The JSON view the semantic validator (validator._check_semantic) + the audit
    consume: {now_price, checkpoint, whipsaw, levels{name:{price, side, swept,
    depleted}}}.

    Implemented as parse_facts(render_facts_text(bundle)) ON PURPOSE — "one source,
    two views" (prod-agent.md). Deriving the validator view from the SAME rendered
    text the offline calibration bench parses is what guarantees the online semantic
    layer behaves identically to the bench (HOLE H4); it also makes the Wave-1.1
    validator-dict-parity test true by construction. tier is intentionally omitted
    here (the validator never reads it — richer per-level data lives in
    bundle.levels for the records/audit layer).

    NOTE (plan 11): the S8 `menus` block is NOT injected here — this dict is hashed as
    the shadow engine's facts identity (facts_adapter._canonical_hash), so it must stay
    byte-stable. Menus are an additive, bench/L1-only overlay: build them with
    `build_menus(bundle, facts_to_validator_dict(bundle))` and attach under a "menus"
    key on a COPY (the bench does this; render_menus_text caches it on the bundle).
    """
    return _parse_facts()(render_facts_text(bundle))


# --------------------------------------------------------------------------- #
# S8 menus (plan 11): DOL menu + predicate menu, config-driven                 #
# --------------------------------------------------------------------------- #
_EPS = 1e-6

# plan 12 Fix 2 — minimum distance (pts) a DOL pool must sit beyond current price to be offered
# as a draw. A pool nearer than this is one that price is effectively already sitting on: there
# is no forward draw to it, and offering it is race-prone — price can cross the level within the
# call-latency window before the thesis stands, satisfying `price_beyond(DOL)` at arrival for a
# bogus minutes-long "completion" (07-02 th_02: DOL was 0.75 pts away). Deliberately conservative
# (catches the already-at-the-pool case without over-excluding legitimate near-term targets); the
# validator's SEM_DOL_WRONG_SIDE check + the scoring suspect_completion flag are the
# belt-and-suspenders for the residual latency race.
DOL_MIN_DRAW_DISTANCE_PTS = 5.0

# Predicate-menu generation config: families × level-classes × param variants. Adding a
# family / level-class / variant here changes the menu WITHOUT touching the generator, and
# nothing here names a specific level — level names are resolved from the facts at build
# time (the "config-driven, not hardcoded per level name" requirement).
_MENU_PREDICATE_CFG = (
    # (family, id_prefix, level_class, builder, variants, max_levels)
    ("falsification", "F", "daily_mid", "n_closes_beyond",
     ({"tf": "5m", "n": 2},), None),
    ("falsification", "F", "anti_pools", "price_beyond", ({},), 3),
    ("exhaustion", "X", "dol_pools", "price_beyond", ({},), None),
    ("recall", "R", "daily_mid", "n_closes_beyond", ({"tf": "5m", "n": 3},), None),
    ("recall", "R", "clock", "time_elapsed", ({"minutes": 60}, {"minutes": 120}), None),
)


def _anti_side(direction: str) -> str:
    return "below" if direction == "UP" else "above"


def _thesis_side(direction: str) -> str:
    return "above" if direction == "UP" else "below"


def _dol_menu(mnq_levels: dict, vlevels: dict, now_price: float) -> dict:
    """Eligible target pools per direction: in-facts, unswept AND undepleted, on the
    correct side of current price, AND at least DOL_MIN_DRAW_DISTANCE_PTS away. UP draws sit
    above price (nearest first); DOWN below.

    Proximity guard (plan 12 Fix 2): a pool nearer than DOL_MIN_DRAW_DISTANCE_PTS to price is
    EXCLUDED — price is already sitting on it, so there is no forward draw, and offering it is
    race-prone: between the facts snapshot and the thesis's arrival (born + call latency) price
    can cross the level, so the exhaustion (`price_beyond(DOL)`) is already satisfied at arrival
    and the thesis "completes" in minutes on a level it never actually drew to. This was the
    07-02 th_02 bug: DOL prev1_day_low was 0.75 pts below price at facts build; price crossed it
    during the latency window → bogus 2-minute completion."""
    out = {"UP": [], "DOWN": []}
    if not isinstance(now_price, (int, float)):
        return out
    for name, tup in mnq_levels.items():
        price, body, side, tier, _active = tup
        if side not in ("above", "below") or not isinstance(price, (int, float)):
            continue
        v = vlevels.get(name, {})
        if v.get("swept") or v.get("depleted"):
            continue
        # Too close to be a draw (price is already sitting on the pool) → race-prone, spent.
        if abs(price - now_price) < DOL_MIN_DRAW_DISTANCE_PTS:
            continue
        # A draw is a resistance ABOVE price (up) or a support BELOW price (down); a
        # level whose price sits on the wrong side of its own tag (a resistance now below
        # price, or vice-versa) is spent, not a draw — excluded.
        if side == "above" and price > now_price + _EPS:
            out["UP"].append((name, price, body, tier, side))
        elif side == "below" and price < now_price - _EPS:
            out["DOWN"].append((name, price, body, tier, side))
    # Nearest draw first (UP ascending, DOWN descending), then assign stable IDs.
    out["UP"].sort(key=lambda e: e[1])
    out["DOWN"].sort(key=lambda e: -e[1])
    menu = {}
    for direction, entries in out.items():
        menu[direction] = [
            {"id": f"D{i + 1}", "level": name, "price": price,
             "body": body, "tier": tier, "side": side}
            for i, (name, price, body, tier, side) in enumerate(entries)
        ]
    return menu


def _resolve_level_class(level_class: str, direction: str, mnq_levels: dict,
                         vlevels: dict, now_price: float, day_mid, dol_menu: dict,
                         max_levels) -> list:
    """Resolve a level-class to a list of (price, side) the builder fills into predicates."""
    if level_class == "daily_mid":
        if not isinstance(day_mid, (int, float)):
            return []
        return [(day_mid, _anti_side(direction))]
    if level_class == "clock":
        return [(None, None)]
    if level_class == "dol_pools":
        side = _thesis_side(direction)
        return [(e["price"], side) for e in dol_menu.get(direction, [])]
    if level_class == "anti_pools":
        # Nearest unswept pools on the ANTI-thesis side of price (support below an UP
        # thesis / resistance above a DOWN thesis); breaking them falsifies the thesis.
        side = _anti_side(direction)
        pools = []
        for name, tup in mnq_levels.items():
            price, _body, lside, _tier, _active = tup
            if lside not in ("above", "below") or not isinstance(price, (int, float)):
                continue
            v = vlevels.get(name, {})
            if v.get("swept") or v.get("depleted"):
                continue
            # Anti-thesis pools are the support UNDER an up-thesis (side below, below
            # price) or the resistance OVER a down-thesis (side above, above price).
            if direction == "UP" and lside == "below" and price < now_price - _EPS:
                pools.append(price)
            elif direction == "DOWN" and lside == "above" and price > now_price + _EPS:
                pools.append(price)
        pools.sort(key=lambda p: abs(p - now_price))   # nearest first
        if max_levels:
            pools = pools[:max_levels]
        return [(p, side) for p in pools]
    return []


def _build_predicate(builder: str, price, side, variant: dict) -> Optional[dict]:
    if builder == "price_beyond":
        return {"type": "price_beyond", "price": price, "side": side}
    if builder == "n_closes_beyond":
        return {"type": "n_closes_beyond", "price": price, "side": side,
                "tf": variant["tf"], "n": variant["n"]}
    if builder == "time_elapsed":
        return {"type": "time_elapsed", "minutes": variant["minutes"]}
    return None


def _predicate_menu(mnq_levels: dict, vlevels: dict, now_price: float, day_mid,
                    dol_menu: dict) -> dict:
    """Per-direction candidate falsification / exhaustion / recall predicates with unique
    IDs and concrete params, generated from _MENU_PREDICATE_CFG (config-driven)."""
    out = {}
    for direction in ("UP", "DOWN"):
        entries = []
        counters = {}
        seen = set()
        for family, prefix, level_class, builder, variants, max_levels in _MENU_PREDICATE_CFG:
            targets = _resolve_level_class(level_class, direction, mnq_levels, vlevels,
                                           now_price, day_mid, dol_menu, max_levels)
            for price, side in targets:
                for variant in variants:
                    pred = _build_predicate(builder, price, side, variant)
                    if pred is None:
                        continue
                    key = json.dumps(pred, sort_keys=True)
                    if key in seen:            # dedupe identical concrete predicates
                        continue
                    seen.add(key)
                    counters[prefix] = counters.get(prefix, 0) + 1
                    entries.append({"id": f"{prefix}{counters[prefix]}",
                                    "family": family, "predicate": pred})
        out[direction] = entries
    return out


def build_menus(bundle: FactsBundle, vd: dict) -> dict:
    """The S8 menu object: {now_price, dol{UP,DOWN}, predicates{UP,DOWN}} — the structured
    menu shared by the rendered facts text (render_menus_text) and the validator's
    menu-membership check. Deterministic given the same facts."""
    mnq_levels = (bundle.levels or {}).get("MNQ", {})
    vlevels = vd.get("levels", {}) if isinstance(vd, dict) else {}
    now_price = vd.get("now_price") if isinstance(vd, dict) else bundle.now_price
    if not isinstance(now_price, (int, float)):
        now_price = bundle.now_price
    dol = _dol_menu(mnq_levels, vlevels, now_price)
    preds = _predicate_menu(mnq_levels, vlevels, now_price, bundle.day_mid, dol)
    return {"now_price": now_price, "daily_mid": bundle.day_mid,
            "dol": dol, "predicates": preds}


def render_menus_text(bundle: FactsBundle) -> str:
    """Render the S8 menu block (compact tables with IDs). Byte-stable given the same
    facts. Kept SEPARATE from render_facts_text so the S0–S7 core (and its content hash)
    is unchanged — this block is appended to the model-facing prompt, not to bundle.lines."""
    if bundle.menus is None:
        bundle.menus = build_menus(bundle, facts_to_validator_dict(bundle))
    m = bundle.menus or {}
    out: list = []
    A = out.append
    A("## S8 MENUS (candidate DOLs + predicates; select by copying params exactly; "
      "escape hatch = any schema-valid, facts-grounded predicate)")
    A(f"now_price = {m.get('now_price')} | daily_mid = {m.get('daily_mid')}")
    dol = m.get("dol") or {}
    for direction in ("UP", "DOWN"):
        A(f"\nDOL menu [{direction}] (eligible draws: in-facts, unswept, undepleted, "
          f"correct side):")
        rows = dol.get(direction) or []
        if not rows:
            A("  (none eligible)")
        for e in rows:
            body = "" if e.get("body") is None else f" body={e['body']}"
            A(f"  {e['id']}: {e['level']} price={e['price']}{body} "
              f"[{e['side']}, {e['tier']}]")
    preds = m.get("predicates") or {}
    for direction in ("UP", "DOWN"):
        A(f"\nPredicate menu [{direction}] (falsification F / exhaustion X / recall R):")
        rows = preds.get(direction) or []
        if not rows:
            A("  (none)")
        for e in rows:
            A(f"  {e['id']} [{e['family']}]: {_fmt_predicate(e['predicate'])}")
    return "\n".join(out) + "\n"


def _fmt_predicate(pred: dict) -> str:
    """Compact human form of a predicate for the menu table (params echo the JSON)."""
    t = pred.get("type")
    if t == "price_beyond":
        return f"price_beyond(price={pred['price']}, side={pred['side']})"
    if t == "n_closes_beyond":
        return (f"n_closes_beyond(price={pred['price']}, side={pred['side']}, "
                f"tf={pred['tf']}, n={pred['n']})")
    if t == "time_elapsed":
        return f"time_elapsed(minutes={pred['minutes']})"
    return json.dumps(pred, sort_keys=True)


def compute_facts(mnq_df: pd.DataFrame, mes_df: pd.DataFrame, *,
                  ath_mnq: Optional[float] = None, ath_mes: Optional[float] = None,
                  hist_mnq: Optional[pd.DataFrame] = None,
                  hist_mes: Optional[pd.DataFrame] = None,
                  now: Optional[pd.Timestamp] = None) -> FactsBundle:
    """Pure fact computation: the S0–S7 + S3b schema over two load()-normalised bar
    frames (lowercase o/h/l/c, tz-aware ET, CME-maintenance bars already dropped).

    Returns a FactsBundle carrying the rendered lines (render_facts_text) and the
    structured values. No maths changed vs the original print-script — the print
    statements now append to bundle.lines. `now` defaults to the earliest last-bar
    across the two frames; everything after `now` is dropped (no-lookahead).
    """
    bundle = FactsBundle()
    L = bundle.lines.append

    data = {"MNQ": mnq_df, "MES": mes_df}
    if now is None:
        now = min(df.index[-1] for df in data.values())
    # No-lookahead precondition: nothing strictly after `now` enters the bundle.
    data = {k: v[v.index <= now] for k, v in data.items()}
    hist = {"MNQ": hist_mnq, "MES": hist_mes}
    td_now = trade_date(now)

    # Trading-date universe: Mon-Fri only, real sessions only (>=1000 bars) — stray
    # ticks must not mint phantom prev-days (POC run-3 G12).
    counts = pd.Series((data["MNQ"].index + pd.Timedelta(hours=7)).date).value_counts()
    all_tds = sorted(d for d, n in counts.items()
                     if (d.weekday() < 5 and n >= 1000) or d == td_now)
    past_tds = [d for d in all_tds if d < td_now]
    prev1_td = past_tds[-1] if past_tds else None
    prev2_td = past_tds[-2] if len(past_tds) > 1 else None

    iso_now = td_now.isocalendar()[:2]
    week_tds = [d for d in all_tds if d.isocalendar()[:2] == iso_now]
    prev_week_all = sorted({d for d in all_tds if d.isocalendar()[:2] < iso_now})
    prev_iso = prev_week_all[-1].isocalendar()[:2] if prev_week_all else None
    prev1_week_tds = [d for d in all_tds if prev_iso and d.isocalendar()[:2] == prev_iso]

    wk_anchor = week_start_ts(now)

    cands = []
    for d, h, m in ((td_now, 9, 20), (td_now, 13, 0)):
        c = pd.Timestamp(d, tz=TZ) + pd.Timedelta(hours=h, minutes=m)
        if c <= now:
            cands.append(c)
    sess_now_start = session_frame(data["MNQ"], td_now).index[0]
    ckpt = max(cands) if cands else sess_now_start

    bundle.now = now
    bundle.td_now = td_now
    bundle.ckpt = ckpt
    bundle.now_price = float(data["MNQ"]["close"].iloc[-1])

    L("## S0 META")
    L(f"now = {now}  (trade date {td_now}, {now.strftime('%A')})")
    subsess = ("asia" if now.hour >= 18 else "london" if now.hour < 6
               else "ny_morning" if now.hour < 12 else "ny_evening")
    in_whip = (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) < (11, 30)
    L(f"sub-session at cut: {subsess} | inside 09:15-11:30 whipsaw window: {in_whip}")
    L(f"daily-trend checkpoint (most recent at/before now): {ckpt}")
    L(f"current session first bar: {sess_now_start}")
    L(f"prev1 trade date: {prev1_td} | prev2: {prev2_td}")
    L(f"current-week trade dates: {week_tds} | prev1-week: {prev1_week_tds}")
    L(f"ENGINE week anchor (session_pipeline._week_start_ts): {wk_anchor}")

    levels = {}       # levels[tkr][name] = (price, body_price, side, tier, active_from)
    sess_cache = {}
    cards = []        # S3b candidate item cards (precomputed scoring inputs)
    TIER_W = {"week": 3.0, "day": 2.0, "session": 1.0}   # next-move.md §2 (fill 1.5 = FVGs, not carded)

    def window_of(ts):
        return ("asia" if ts.hour >= 18 else "london" if ts.hour < 6
                else "ny_morning" if ts.hour < 12 else "ny_evening")

    def add_card(kind, tkr_, name_, price_, side_, ts_, tier_, note_=""):
        age = age_min(ts_, now)
        if age > 360:
            return                                        # spent (~2× the 3h decay horizon)
        w = TIER_W[tier_]
        fresh = 2 ** (-age / 180)
        cards.append(
            f"{tkr_} {name_} {price_} [{side_}] {kind}: window={window_of(ts_)} @ {ts_} | "
            f"age {age:.0f}m | tier={tier_} w={w} | freshness={fresh:.3f} | "
            f"base(w×freshness)={w * fresh:.2f}{note_}")
    for tkr, df in data.items():
        sess_now = session_frame(df, td_now)
        sess_cache[tkr] = sess_now
        L(f"\n## S1 LEVELS {tkr} (price = wick extreme; body = close extreme)")
        tdo = float(sess_now["open"].iloc[0])
        L(f"TDO (session open {sess_now.index[0]}): {tdo}")
        two, two_ts = compute_two(df, week_tds, now)
        L(f"TWO (bar {two_ts}): {two}")

        lv = {}
        lv["TDO"] = (tdo, None, None, "session", sess_now.index[0])
        if two is not None:
            lv["TWO"] = (two, None, None, "session", sess_now.index[0])
        for lbl, d in (("prev1_day", prev1_td), ("prev2_day", prev2_td)):
            if d is None:
                continue
            s = session_frame(df, d)
            h, l, ch, cl = hl(s)
            c = float(s["close"].iloc[-1])
            pos = (c - l) / (h - l) if h != l else float("nan")
            L(f"{lbl} ({d}): high={h} (body {ch}) low={l} (body {cl}) close={c} (range pos {pos:.2f})")
            lv[f"{lbl}_high"] = (h, ch, "above", "day", sess_now.index[0])
            lv[f"{lbl}_low"] = (l, cl, "below", "day", sess_now.index[0])
        if prev1_week_tds:
            pw = pd.concat([session_frame(df, d) for d in prev1_week_tds])
            h, l, ch, cl = hl(pw)
            L(f"prev1_week ({prev1_week_tds[0]}..{prev1_week_tds[-1]}): high={h} (body {ch}) low={l} (body {cl})")
            lv["prev1_week_high"] = (h, ch, "above", "week", sess_now.index[0])
            lv["prev1_week_low"] = (l, cl, "below", "week", sess_now.index[0])

        wkf = df.loc[wk_anchor:now]
        h, l, ch, cl = hl(wkf)
        L(f"week running [ENGINE anchor {wk_anchor}]: high={h} low={l} mid={(h + l) / 2:.3f}")
        if tkr == "MNQ" and h is not None and l is not None:
            bundle.weekly_mid = round((h + l) / 2.0, 2)   # thesis.md input (not rendered in S1)
        dh, dl, dch, dcl = hl(sess_now)
        L(f"day running: high={dh} (at {sess_now['high'].idxmax()}) low={dl} "
              f"(at {sess_now['low'].idxmin()}) mid={(dh + dl) / 2}")
        if tkr == "MNQ" and dh is not None and dl is not None:
            bundle.day_mid = round((dh + dl) / 2.0, 2)   # S8 menu input (not rendered in S1)
        L(f"last close: {float(df['close'].iloc[-1])}")
        ath = ath_mnq if tkr == "MNQ" else ath_mes
        if ath:
            L(f"ATH {ath}: last close is {(ath - float(df['close'].iloc[-1])) / ath * 100:.2f}% below")

        if prev1_td is not None:
            pblocks = sub_blocks(session_frame(df, prev1_td))
            for name, frame in pblocks.items():
                h, l, ch, cl = hl(frame)
                if h is None:
                    continue
                L(f"{name}(prev1): high={h} (body {ch}) low={l} (body {cl})")
                lv[f"{name}(prev1)_high"] = (h, ch, "above", "session", sess_now.index[0])
                lv[f"{name}(prev1)_low"] = (l, cl, "below", "session", sess_now.index[0])
        cblocks = sub_blocks(sess_now)
        closes_at = {"asia": pd.Timestamp(td_now, tz=TZ),
                     "london": pd.Timestamp(td_now, tz=TZ) + pd.Timedelta(hours=6),
                     "ny_morning": pd.Timestamp(td_now, tz=TZ) + pd.Timedelta(hours=12),
                     "ny_evening": pd.Timestamp(td_now, tz=TZ) + pd.Timedelta(hours=17)}
        for name, frame in cblocks.items():
            if len(frame) == 0 or closes_at[name] > now:
                continue
            h, l, ch, cl = hl(frame)
            L(f"{name}(cur, closed): high={h} (body {ch}) low={l} (body {cl})")
            lv[f"{name}(cur)_high"] = (h, ch, "above", "session", closes_at[name])
            lv[f"{name}(cur)_low"] = (l, cl, "below", "session", closes_at[name])
        levels[tkr] = lv

        L(f"\n## S2 SWEEPS {tkr} (current True Day; inclusive wick cross; body = first 15m close beyond; ages vs now)")
        for name, (price, body, side, tier, active_from) in sorted(lv.items(), key=lambda kv: -kv[1][0]):
            if side is None:
                for s in ("above", "below"):
                    t = first_cross(sess_now.loc[active_from:], price, s)
                    if t is not None:
                        L(f"{name} {price}: first {s}-cross {t} (age {age_min(t, now):.0f}m)")
                continue
            frame = sess_now.loc[active_from:]
            t = first_cross(frame, price, side)
            if t is None:
                dist, ats = closest_approach(frame, price, side)
                extra = (f" (closest approach {dist:.2f} short @ {ats}, age {age_min(ats, now):.0f}m)"
                         if dist is not None else "")
                L(f"{name} {price} [{side}]: NOT swept{extra}")
                continue
            exc = excursion_beyond(frame.loc[t:], price, side)
            thr = DEPLETE[tkr][tier]
            tb = first_body_cross_15m(frame, price, side)
            tb_s = f"{tb} (age {age_min(tb, now):.0f}m)" if tb is not None else "—"
            L(f"{name} {price} [{side}]: swept {t} (age {age_min(t, now):.0f}m) | "
                  f"max excursion beyond {exc:.2f} "
                  f"({'DEPLETED' if exc >= thr else 'not depleted'}, thr {thr:.1f}) | body15m: {tb_s}")
            add_card("sweep", tkr, name, price, side, t, tier,
                     " | DEPLETED" if exc >= thr else "")

    bundle.levels = levels

    L("\n## S3 CROSS-TICKER SWEEP MATRIX (same level name; one swept + other not = divergence candidate)")
    shared = sorted(set(levels["MNQ"]) & set(levels["MES"]))
    for name in shared:
        p1, b1, side, tier, af1 = levels["MNQ"][name]
        p2, b2, _, _, af2 = levels["MES"][name]
        if side is None:
            continue
        f1, f2 = sess_cache["MNQ"].loc[af1:], sess_cache["MES"].loc[af2:]
        t1, t2 = first_cross(f1, p1, side), first_cross(f2, p2, side)
        tb1 = first_body_cross_15m(f1, p1, side)
        tb2 = first_body_cross_15m(f2, p2, side)
        tag = ""
        if (t1 is None) != (t2 is None):
            tag = "  <-- WICK DIVERGENCE CANDIDATE (lead " + ("MNQ" if t1 is not None else "MES") + ")"
        elif t1 is not None and t2 is not None and abs((t1 - t2).total_seconds()) > 900:
            tag = "  <-- both swept, >15min apart (transient divergence window)"
        btag = ""
        if (tb1 is None) != (tb2 is None):
            btag = "  <-- BODY(15m) DIVERGENCE CANDIDATE (lead " + ("MNQ" if tb1 is not None else "MES") + ")"
        L(f"{name} [{side}, {tier}]: wick MNQ {t1 or 'not swept'} | MES {t2 or 'not swept'}{tag}")
        L(f"{'':>{len(name)}}   body MNQ {tb1 or '—'} | MES {tb2 or '—'}{btag}")
        # Laggard reach: how close the NON-sweeping ticker came, and when — the freshness of
        # the FAILURE leg (laggard test-and-fail candidate item, next-move.md §2).
        for lg, tl, ff, pp in (("MNQ", t1, f1, p1), ("MES", t2, f2, p2)):
            if tl is None:
                dist, ats = closest_approach(ff, pp, side)
                if dist is not None:
                    L(f"{'':>{len(name)}}   laggard {lg} max reach: {dist:.2f} short of "
                          f"{pp} @ {ats} (age {age_min(ats, now):.0f}m)")
                    # laggard test-and-fail candidate: other ticker swept, this one
                    # reached within 25% of its depletion threshold and failed
                    other_swept = (t2 if lg == "MNQ" else t1) is not None
                    gate = 0.25 * DEPLETE[lg][tier]
                    if other_swept and dist <= gate:
                        add_card("CANDIDATE laggard-fail", lg, name, pp, side, ats, tier,
                                 f" | reach {dist:.2f} short (25%-of-thr gate {gate:.2f}: QUALIFIES)")

    L("\n## S3b CANDIDATE ITEM CARDS (precomputed scoring inputs; multipliers per decisions/next-move.md §2)")
    L("score = tier_weight × session_side × alignment × freshness × whipsaw = base × session_side × alignment × whipsaw")
    L("Copy tier w and freshness from the card — do NOT recompute the exponent. session_side/")
    L("alignment/whipsaw are read-dependent: take them from the next-move.md tables.")
    L("REMINDER: whipsaw ×0.5 applies ONLY to intraday STRUCTURE items inside 09:15-11:30;")
    L("level sweeps/SMTs are NOT structure items (their whipsaw = 1.0).")
    L("Cards cover swept fixed levels (freshness = sweep time) and qualifying laggard-fail")
    L("candidates (freshness = failure time); items >6h old omitted (spent). Dynamic-extreme")
    L("events and continuation items are not carded — compute their freshness from S4/S5 times.")
    if cards:
        for line in cards:
            L(line)
    else:
        L("(no candidate items within the 6h window)")

    L("\n## S4 EQUILIBRIUM (1m closes vs RUNNING mids, current session; weekly mid = ENGINE anchor)")
    for tkr, df in data.items():
        sess_now = sess_cache[tkr]
        m1 = df["close"].resample("1min").last()
        dmid = (sess_now["high"].resample("1min").max().cummax()
                + sess_now["low"].resample("1min").min().cummin()) / 2
        wkf = df.loc[wk_anchor:]
        wmid = ((wkf["high"].resample("1min").max().cummax()
                 + wkf["low"].resample("1min").min().cummin()) / 2).loc[sess_now.index[0]:]
        for label, mid in (("daily", dmid), ("weekly[engine anchor]", wmid)):
            j, flips = mid_cross_table(m1.reindex(mid.index), mid)
            L(f"\n{tkr} {label} mid crosses (last {len(flips)}):")
            for ts, row in flips.iterrows():
                L(f"  {ts}  close {row['close']:.2f} {'ABOVE' if row['above'] else 'below'} mid {row['mid']:.2f}")
            for a, b, lbl in ((sess_now.index[0], ckpt, "session->ckpt"),
                              (ckpt - pd.Timedelta(hours=6), ckpt, "6h->ckpt"),
                              (ckpt, now, "ckpt->now")):
                seg = j.loc[a:b]
                if len(seg):
                    L(f"  acceptance {lbl}: {seg['above'].mean() * 100:.0f}% of closes above (n={len(seg)})")

    L("\n## S5 STRUCTURE BARS (MNQ; 4hr/1hr are MIDNIGHT-anchored = engine convention)")
    df = data["MNQ"]
    L("\nTrue-Day bars (rows = session open time):")
    tds_series = pd.Series((df.index + pd.Timedelta(hours=7)).date, index=df.index)
    rows = []
    for d in all_tds:
        s = df[tds_series.values == d]
        if len(s) == 0:
            continue
        rows.append((s.index[0], float(s['open'].iloc[0]), float(s['high'].max()),
                     float(s['low'].min()), float(s['close'].iloc[-1])))
    L(pd.DataFrame(rows, columns=["open_ts", "open", "high", "low", "close"]).to_string(index=False))
    h4 = ohlc(df, "4h")
    L("\n4hr bars (midnight-anchored, ENGINE), last 24:")
    L(h4.tail(24).to_string())
    h1 = ohlc(df, "1h")
    L("\n1hr bars, last 24:")
    L(h1.tail(24).to_string())
    L("\n30m bars, last 16:")
    L(ohlc(df, "30min").tail(16).to_string())
    L("\n15m bars, last 16:")
    L(ohlc(df, "15min").tail(16).to_string())
    L(f"\n5m bars, checkpoint {ckpt} -> now:")
    L(ohlc(df.loc[ckpt - pd.Timedelta(minutes=5):], "5min").loc[ckpt:].to_string())

    if hist["MNQ"] is not None and hist["MES"] is not None:
        L("\n## S5b LONG-HORIZON CONTEXT (full history truncated at now; True-Day daily bars + trade-week weekly bars)")
        for tkr in ("MNQ", "MES"):
            hf = hist[tkr]
            hf = hf[hf.index <= now]
            htd = pd.Series((hf.index + pd.Timedelta(hours=7)).date, index=hf.index)
            counts = htd.value_counts()
            days = sorted(d for d, n in counts.items() if d.weekday() < 5 and n >= 1000)
            rows = []
            for d in days[-60:]:
                s = hf[htd.values == d]
                rows.append((d, float(s["open"].iloc[0]), float(s["high"].max()),
                             float(s["low"].min()), float(s["close"].iloc[-1])))
            dtab = pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close"])
            L(f"\n{tkr} daily (True-Day) bars, last {len(dtab)} sessions:")
            L(dtab.to_string(index=False))
            dtab["iso"] = dtab["trade_date"].map(lambda d: d.isocalendar()[:2])
            wk = dtab.groupby("iso").agg(
                start=("trade_date", "first"), open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"))
            L(f"\n{tkr} weekly bars (trade weeks), last {min(len(wk), 12)}:")
            L(wk.tail(12).to_string(index=False))

    L("\n## S6 FVGs (both tickers, completed bars only; visited = 1s tape re-entered zone after the 3rd bar's OPEN label — daily.py convention)")
    for tkr, dft in data.items():
        h1t = ohlc(dft, "1h")
        h4t = ohlc(dft, "4h")
        for label, bars in ((f"{tkr} 1hr", h1t.iloc[:-1]), (f"{tkr} 4hr", h4t.iloc[:-1])):
            recent = bars[bars.index >= now - pd.Timedelta(days=10)]
            for ts, kind, lo, hi, third_ts in fvgs(recent):
                seg = dft[dft.index > third_ts]
                touched = bool(((seg["low"] <= hi) & (seg["high"] >= lo)).any()) if len(seg) else False
                L(f"{label} {ts} {kind} zone {lo}-{hi} -> {'visited' if touched else 'UNVISITED'}")

    L("\n## S7 CHECKPOINT SNAPSHOT")
    for tkr, dft in data.items():
        m1 = dft["close"].resample("1min").last().dropna()
        L(f"{tkr} close at checkpoint {ckpt}: {m1.asof(ckpt)} | at now: {float(dft['close'].iloc[-1])}")
    L("\nReminders (facts end here — decisions are yours):")
    L("- SMT fire adjudication, votes, correlation audit, ledgers, vetoes: NOT computed here.")
    L("- Week extremes/mid use the ENGINE anchor printed in S0 (equilibrium.md pins this).")
    L("- Dynamic day/week extremes and re-arm/depart states must be reasoned from S1/S5 times.")
    L("- Depletion uses exact per-ticker engine tables; boundary is >= (depleted at exactly thr).")

    return bundle


def _load_hist(hist_dir: str, tkr: str) -> pd.DataFrame:
    hp = os.path.join(hist_dir, f"{tkr}_1s_slice.parquet")
    if not os.path.exists(hp):
        hp = os.path.join(hist_dir, f"{tkr}_1s.parquet")
    return load(hp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--ath-mnq", type=float, default=None)
    ap.add_argument("--ath-mes", type=float, default=None)
    ap.add_argument("--hist-dir", default=None,
                    help="Optional dir with full {MNQ,MES}_1s.parquet for the long-horizon "
                         "daily/weekly tables (S5b). Data is truncated at 'now' — no lookahead.")
    args = ap.parse_args()

    mnq_df = load(os.path.join(args.dir, "MNQ_1s_slice.parquet"))
    mes_df = load(os.path.join(args.dir, "MES_1s_slice.parquet"))
    hist_mnq = _load_hist(args.hist_dir, "MNQ") if args.hist_dir else None
    hist_mes = _load_hist(args.hist_dir, "MES") if args.hist_dir else None

    bundle = compute_facts(mnq_df, mes_df, ath_mnq=args.ath_mnq, ath_mes=args.ath_mes,
                           hist_mnq=hist_mnq, hist_mes=hist_mes)
    # end="" — render_facts_text already terminates with the trailing newline that
    # the original per-print stdout carried, so the byte stream is identical.
    print(render_facts_text(bundle), end="")


if __name__ == "__main__":
    main()
