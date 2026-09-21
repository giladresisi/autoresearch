"""Initial-target lookahead study (measurement only; deliberate lookahead on entry).

Over the last ~30 RTH days: assume the correct 09:30 direction and the optimal entry in
the live entry window, run the PRODUCTION selectors at that entry (`target.select_target`
for T2, `target.level_universe` + `initial_target.select_initial_target` for the initial),
and measure what the tape did relative to both. The selectors see history truncated at the
entry instant (`index <= now`); everything else is lookahead by design.

Run from the repo root:  .venv/Scripts/python agent/study/initial_target_lookahead.py
  --extreme-min {A,F}      plan 35 §2.3 EXTREME_MIN: A = 150 pts (default), F = 0.65·D
  --mid-pref {nearest,farthest,session_mid_first}   MID_PREFERENCE (default: the code's)
  --all-variants           run the whole 2x3 grid in one pass (one bundle build per day)
  --capture-fixtures DIR   also write DIR/<date>.json (direction, anchor, secondary, the
                           level universe at the entry instant) for the selector's fixture
                           tests; combine with explicit dates
  YYYY-MM-DD ...           restrict to these dates
Outputs: agent/study/out/initial_target_lookahead_<A|F>_<pref>.tsv (+ JSON sidecar) per
variant; the pre-v2 run is kept as initial_target_lookahead_v1.tsv. `--all-variants` also
writes out/variants_summary.md (the v1-vs-v2 tables of the write-up).
Not a production path; nothing imports this.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import backtest_smt  # noqa: E402  (for _main_dir_for_date only)
from agent.facts.detectors._common import normalize, truncate  # noqa: E402
from agent.trader.initial_target import (InitialTargetTracker, MIN_DIST_PTS,  # noqa: E402
                                         MID_PREFERENCES, select_initial_target)
from agent.trader.target import level_universe, select_target  # noqa: E402

TZ = "America/New_York"
TICKERS = ("MNQ", "MES")
HISTORY = pd.Timedelta(days=20)          # >= the graft's 17d; selectors slice their own 17d
N_DAYS = 30
SKIP_DATES = {"2026-09-18"}              # RTH not promoted into main yet
STOP_PTS = 15.0                          # §7-style informational stop
OUT_DIR = os.path.join(_HERE, "out")

#: Plan 35 §2.3 grid. Keys are the short tags used in file names.
EXTREME_MINS = {"A": ("pts", 150.0), "F": ("frac", 0.65)}
GRID = [(e, m) for e in ("A", "F") for m in MID_PREFERENCES]
#: The six days the operator reviewed by hand (plan §2.3 table).
REVIEWED_DAYS = ("2026-09-01", "2026-08-27", "2026-08-12", "2026-09-08", "2026-09-15",
                 "2026-08-25")

COLUMNS = ["date", "direction", "O", "up_ex", "down_ex", "entry_time", "entry_price",
           "t2_price", "t2_level", "initial_price", "initial_level", "initial_tier", "variant",
           "initial_reason",
           "primary_reached", "primary_time", "mfe", "mfe_time", "mae", "mark_1300",
           "initial_touched", "touch_time", "initial_flipped", "flip_time", "flip_close",
           "kept_at_flip", "primary_after_flip", "primary_after_flip_time", "post_flip_mae",
           "stop15_hit_before_initial", "class",
           # extra diagnostics
           "flip_on_entry_bar", "initial_dist", "initial_share", "t2_dist", "n_candidates",
           "n_levels", "entry_to_1300_bars",
           # post-flip counterfactuals (tracker.post_flip): back-touch of the initial before
           # the primary / 13:00 (plan §2.5 action A's stop), first opposite 1m close (action B)
           "retrace_to_initial", "retrace_time", "worst_flip_to_primary",
           "cf_opp_close_time", "cf_opp_close_kept"]


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #

_ERA_CACHE: dict = {}


def _era_frames(date_str: str) -> dict:
    """{"MNQ": df, "MES": df} 1m frames of the contract era `date_str` belongs to."""
    d = backtest_smt._main_dir_for_date(date_str)
    key = str(d)
    if key not in _ERA_CACHE:
        fr = {}
        for tk in TICKERS:
            df = pd.read_parquet(os.path.join(key, f"{tk}_1m.parquet"))
            if df.index.tz is None:
                df.index = df.index.tz_localize(TZ)
            else:
                df.index = df.index.tz_convert(TZ)
            fr[tk] = df.sort_index()
        _ERA_CACHE[key] = fr
    return _ERA_CACHE[key]


def _ts(date_str: str, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{date_str} {hhmm}", tz=TZ)


def trading_days(n: int) -> list:
    """Last `n` dates with MNQ 1m bars between 09:30 and 13:00 ET, newest era first
    then the earlier era, dedup'd, SKIP_DATES removed."""
    seen, out = set(), []
    for probe in ("2026-09-18", "2026-09-11"):        # one date per era, newest first
        mnq = _era_frames(probe)["MNQ"]
        t = mnq.index
        rth = mnq[(t.hour * 60 + t.minute >= 9 * 60 + 30) & (t.hour < 13)]
        days = sorted({ts.strftime("%Y-%m-%d") for ts in rth.index}, reverse=True)
        for day in days:
            if day in seen or day in SKIP_DATES:
                continue
            # an era only serves dates it is the resolver's answer for
            if str(backtest_smt._main_dir_for_date(day)) != str(backtest_smt._main_dir_for_date(probe)):
                continue
            # require bars through 12:59 (the 13:00 close)
            dd = rth[rth.index.strftime("%Y-%m-%d") == day]
            if len(dd) < 150 or dd.index[-1] < _ts(day, "12:55"):
                continue
            seen.add(day)
            out.append(day)
            if len(out) >= n:
                return sorted(out)
    return sorted(out)


def frames_at(date_str: str, now: pd.Timestamp) -> dict:
    """Production-shaped frames: ~20 calendar days of 1m history up to and including
    `now`, strictly no bar after `now` (the one place lookahead is forbidden)."""
    era = _era_frames(date_str)
    out = {}
    for tk in TICKERS:
        df = era[tk]
        sl = df[(df.index >= now - HISTORY) & (df.index <= now)]
        sl = truncate(normalize(sl), now)
        assert len(sl) == 0 or sl.index[-1] <= now, "lookahead leak into selector frames"
        out[tk] = sl
    return out


# --------------------------------------------------------------------------- #
# per-day
# --------------------------------------------------------------------------- #

def _fav(direction: str, entry: float, price: float) -> float:
    return (entry - price) if direction == "DOWN" else (price - entry)


def day_context(date_str: str) -> dict:
    """Everything a day needs BEFORE the initial is picked: direction, the lookahead
    entry, the production T2 pick and the level universe at the entry instant. Shared
    by every variant (one bundle build per day) and by the fixture capture."""
    mnq_all = _era_frames(date_str)["MNQ"]
    day = mnq_all[(mnq_all.index >= _ts(date_str, "09:30")) & (mnq_all.index < _ts(date_str, "13:00"))]
    ctx = {"date": date_str, "day": day, "direction": None, "O": None, "up_ex": None,
           "down_ex": None, "entry_time": None, "entry_price": None, "t2_price": None,
           "t2_level": None, "levels": [], "t2_reason": None}
    if len(day) == 0:
        ctx["t2_reason"] = "no_bars"
        return ctx

    # 1. direction from the 09:30 open
    O = float(day.iloc[0]["Open"])
    up_ex = float(day["High"].max() - O)
    down_ex = float(O - day["Low"].min())
    direction = "UP" if up_ex > down_ex else "DOWN"
    ctx.update(direction=direction, O=O, up_ex=round(up_ex, 2), down_ex=round(down_ex, 2))

    # 2. optimal entry inside the live window 09:30..10:29 bars (bar labels)
    win = day[day.index <= _ts(date_str, "10:29")]
    if direction == "DOWN":
        entry_time = win["High"].idxmax()
        entry_price = float(win.loc[entry_time, "High"])
    else:
        entry_time = win["Low"].idxmin()
        entry_price = float(win.loc[entry_time, "Low"])
    ctx.update(entry_time=entry_time, entry_price=entry_price)

    # 3. production selectors at now = entry_time
    now = entry_time
    bars = frames_at(date_str, now)
    for tk in TICKERS:
        assert (bars[tk].index > now).sum() == 0
    pick = select_target(bars, now, direction, "MNQ")
    ctx["t2_price"] = None if not pick else pick.get("price")
    ctx["t2_level"] = None if not pick else pick.get("level")
    ctx["levels"] = level_universe(bars, now, "MNQ")
    if ctx["t2_price"] is None:
        ctx["t2_reason"] = "no_t2"
    elif _fav(direction, entry_price, float(ctx["t2_price"])) <= 0:
        ctx["t2_reason"] = "t2_wrong_side"
    return ctx


def fixture_from_context(ctx: dict) -> dict:
    """The selector's inputs on one day, JSON-serialisable (the fixture tests replay
    `select_initial_target` on exactly this list)."""
    return {"date": ctx["date"], "direction": ctx["direction"],
            "entry_time": (ctx["entry_time"].strftime("%H:%M") if ctx["entry_time"] is not None
                           else None),
            "anchor": ctx["entry_price"], "secondary": ctx["t2_price"],
            "t2_level": ctx["t2_level"],
            "levels": [{k: v for k, v in lv.items() if k != "running"} for lv in ctx["levels"]]}


def study_day(date_str: str, variant=None, ctx: dict = None) -> dict:
    """One row: the selector under `variant` = (extreme_min_tag, mid_pref) (None = the
    code's defaults) at the lookahead entry, then the tape's verdict."""
    ctx = ctx or day_context(date_str)
    day = ctx["day"]
    row = {c: None for c in COLUMNS}
    row["date"] = date_str
    if len(day) == 0:
        row["class"] = "E"
        row["initial_reason"] = "no_bars"
        return row
    direction, entry_time, entry_price = ctx["direction"], ctx["entry_time"], ctx["entry_price"]
    row.update(direction=direction, O=ctx["O"], up_ex=ctx["up_ex"], down_ex=ctx["down_ex"],
               entry_time=entry_time.strftime("%H:%M"), entry_price=entry_price,
               t2_price=ctx["t2_price"], t2_level=ctx["t2_level"], n_levels=len(ctx["levels"]))
    t2_price = ctx["t2_price"]
    kw = {}
    if variant is not None:
        kw = {"extreme_min": EXTREME_MINS[variant[0]], "mid_preference": variant[1]}
    initial = None
    if ctx["t2_reason"] is not None:
        row["initial_reason"] = ctx["t2_reason"]
        if t2_price is not None:
            row["t2_dist"] = round(_fav(direction, entry_price, float(t2_price)), 2)
    else:
        t2_price = float(t2_price)
        row["t2_dist"] = round(_fav(direction, entry_price, t2_price), 2)
        initial = select_initial_target(direction, entry_price, t2_price, ctx["levels"], **kw)
        if initial is None:
            row["initial_reason"] = f"none:t2_dist<{(MIN_DIST_PTS + 2) / 0.85:.0f}"
        else:
            row["initial_reason"] = ("structural" if initial.get("level_price") is not None
                                     else "synthetic")
            row["initial_price"] = initial["price"]
            row["initial_level"] = initial["level"]
            row["initial_tier"] = initial.get("tier")
            row["n_candidates"] = initial.get("n_candidates")
            d_ini = _fav(direction, entry_price, float(initial["price"]))
            row["initial_dist"] = round(d_ini, 2)
            row["initial_share"] = round(d_ini / _fav(direction, entry_price, t2_price), 3)
        row["variant"] = (initial or {}).get("variant") or (
            f"{EXTREME_MINS[variant[0]][0]}:{EXTREME_MINS[variant[0]][1]:g}|{variant[1]}"
            if variant else None)

    # 4. walk bars strictly after the entry bar through 12:59
    after = day[day.index > entry_time]
    row["entry_to_1300_bars"] = len(after)
    if len(after) == 0:
        row["class"] = "E" if initial is None else "D"
        return row
    fav_hi = after["High"].map(lambda p: _fav(direction, entry_price, p))
    fav_lo = after["Low"].map(lambda p: _fav(direction, entry_price, p))
    fav_best = fav_hi if direction == "UP" else fav_lo      # favorable side extreme
    fav_worst = fav_lo if direction == "UP" else fav_hi     # adverse side extreme
    mfe_t = fav_best.idxmax()
    row["mfe"] = round(float(fav_best.max()), 2)
    row["mfe_time"] = mfe_t.strftime("%H:%M")
    row["mae"] = round(float(-fav_worst.min()), 2) if fav_worst.min() < 0 else 0.0
    row["mark_1300"] = round(_fav(direction, entry_price, float(after.iloc[-1]["Close"])), 2)

    # primary touch
    primary_time = None
    if t2_price is not None:
        hit = fav_best >= _fav(direction, entry_price, float(t2_price))
        if hit.any():
            primary_time = hit[hit].index[0]
    row["primary_reached"] = bool(primary_time is not None) if t2_price is not None else None
    row["primary_time"] = primary_time.strftime("%H:%M") if primary_time is not None else None

    # stop-15 before the initial touch (informational)
    if initial is not None:
        ini = float(initial["price"])
        d_ini = _fav(direction, entry_price, ini)
        touched = fav_best >= d_ini
        touch_time = touched[touched].index[0] if touched.any() else None
        stopped = fav_worst <= -STOP_PTS
        stop_time = stopped[stopped].index[0] if stopped.any() else None
        row["initial_touched"] = bool(touch_time is not None)
        row["touch_time"] = touch_time.strftime("%H:%M") if touch_time is not None else None
        if stop_time is not None and (touch_time is None or stop_time <= touch_time):
            row["stop15_hit_before_initial"] = True      # same bar: stop wins (tracker rule)
        else:
            row["stop15_hit_before_initial"] = False

        # the reached rule on COMPLETED 1m bars, the fill's own minute included as production
        tracker = InitialTargetTracker(direction, ini, level=initial.get("level"))
        flip = None
        for ts, bar in pd.concat([day.loc[[entry_time]], after]).iterrows():
            ev = tracker.on_bar_close(bar)
            if ev is not None:
                flip = (ts, float(bar["Close"]))
                break
        row["initial_flipped"] = flip is not None
        row["flip_on_entry_bar"] = bool(flip is not None and flip[0] == entry_time)
        if flip is not None:
            flip_t, flip_c = flip
            row["flip_time"] = flip_t.strftime("%H:%M")
            row["flip_close"] = flip_c
            row["kept_at_flip"] = round(_fav(direction, entry_price, flip_c), 2)
            post = after[after.index > flip_t]
            for ts, bar in post.iterrows():
                for ev in tracker.post_flip(bar):
                    if ev["kind"] == "cf_stop":
                        row["retrace_to_initial"] = True
                        row["retrace_time"] = ts.strftime("%H:%M")
                    elif ev["kind"] == "cf_opp_close":
                        row["cf_opp_close_time"] = ts.strftime("%H:%M")
                        row["cf_opp_close_kept"] = round(_fav(direction, entry_price, float(ev["price"])), 2)
            if row["retrace_to_initial"] is None:
                row["retrace_to_initial"] = False
            if len(post):
                pb = post["High" if direction == "UP" else "Low"].map(lambda p: _fav(direction, entry_price, p))
                pw = post["Low" if direction == "UP" else "High"].map(lambda p: _fav(direction, entry_price, p))
                row["post_flip_mae"] = round(float(pw.min()), 2)   # worst mark from ENTRY after the flip
                if t2_price is not None:
                    h2 = pb >= _fav(direction, entry_price, float(t2_price))
                    row["primary_after_flip"] = bool(h2.any())
                    row["primary_after_flip_time"] = h2[h2].index[0].strftime("%H:%M") if h2.any() else None
                    upto = post[post.index <= h2[h2].index[0]] if h2.any() else post
                    pw2 = upto["Low" if direction == "UP" else "High"].map(lambda p: _fav(direction, entry_price, p))
                    row["worst_flip_to_primary"] = round(float(pw2.min()), 2)
                    # a back-touch AFTER the primary is not a retrace "before the primary"
                    if row["retrace_to_initial"] and h2.any() and row["retrace_time"] > row["primary_after_flip_time"]:
                        row["retrace_to_initial"] = False
                        row["retrace_time"] = None
            else:
                row["post_flip_mae"] = row["kept_at_flip"]
                row["primary_after_flip"] = False
        # a flip on the entry bar itself still counts as "touched" for the class
        if row["flip_on_entry_bar"] and not row["initial_touched"]:
            row["initial_touched"] = True
            row["touch_time"] = entry_time.strftime("%H:%M")

    # 5. class
    if t2_price is None or initial is None:
        cls = "E"
    elif row["primary_reached"]:
        cls = "A"
    elif row["initial_flipped"]:
        cls = "B"
    elif row["initial_touched"]:
        cls = "C"
    else:
        cls = "D"
    row["class"] = cls
    return row


# --------------------------------------------------------------------------- #
# variant comparison (the write-up's v1-vs-v2 tables)
# --------------------------------------------------------------------------- #

def _tag(variant) -> str:
    return "v1" if variant is None else f"{variant[0]}_{variant[1]}"


def _stats(vals: list) -> str:
    s = pd.Series([v for v in vals if v is not None], dtype="float64")
    if s.empty:
        return "–"
    return f"{s.mean():.1f} / {s.median():.1f}"


def _minutes(a: str, b: str) -> "int | None":
    try:
        ha, ma = map(int, a.split(":"))
        hb, mb = map(int, b.split(":"))
        return (hb * 60 + mb) - (ha * 60 + ma)
    except Exception:
        return None


def summarize_variants(frames: "dict[str, pd.DataFrame]") -> str:
    """Markdown: per variant, class counts; on failed-primary days kept-at-flip / 13:00 /
    MFE (mean / median) and how often the initial was reached; on winners minutes before
    T2 and points forfeited by exiting at the flip; the initial-distance distribution;
    the six reviewed days' picks. `frames` = {tag: rows DataFrame}, tag "v1" first."""
    out = []
    tags = list(frames)
    out.append("| variant | A | B | C | D | E | structural | synthetic |")
    out.append("|---|---|---|---|---|---|---|---|")
    for tag in tags:
        df = frames[tag]
        c = df["class"].value_counts()
        out.append(f"| {tag} | " + " | ".join(str(int(c.get(k, 0))) for k in "ABCDE")
                   + f" | {int((df['initial_reason'] == 'structural').sum())}"
                   f" | {int((df['initial_reason'] == 'synthetic').sum())} |")
    out.append("")
    out.append("Failed-primary days (T2 never touched; n in brackets): initial reached = the "
               "tracker flipped; pts from entry, mean / median over the flipped days.")
    out.append("")
    out.append("| variant | failed-primary | initial reached | kept at flip | 13:00 mark | MFE | "
               "13:00 beat flip | negative post-flip mark |")
    out.append("|---|---|---|---|---|---|---|---|")
    for tag in tags:
        df = frames[tag]
        fp = df[(df["primary_reached"] == False) & df["t2_price"].notna()]   # noqa: E712
        fl = fp[fp["initial_flipped"] == True]                               # noqa: E712
        beat = int((fl["mark_1300"] > fl["kept_at_flip"]).sum())
        neg = int((fl["post_flip_mae"] < 0).sum())
        out.append(f"| {tag} | {len(fp)} | {len(fl)} ({len(fl) / max(1, len(fp)):.0%}) | "
                   f"{_stats(fl['kept_at_flip'].tolist())} | {_stats(fl['mark_1300'].tolist())} | "
                   f"{_stats(fl['mfe'].tolist())} | {beat}/{len(fl)} | {neg}/{len(fl)} |")
    out.append("")
    out.append("Winners (T2 touched): flips before the T2 touch, minutes from the flip bar to "
               "the T2 touch, and the points an exit at the flip close forfeits vs T2 "
               "(mean / median, total).")
    out.append("")
    out.append("| variant | winners | flipped before T2 | minutes before T2 | forfeited pts | "
               "forfeited total | retrace to initial before T2 |")
    out.append("|---|---|---|---|---|---|---|")
    for tag in tags:
        df = frames[tag]
        w = df[df["primary_reached"] == True]                                # noqa: E712
        fw = w[w["initial_flipped"] == True]                                 # noqa: E712
        mins = [_minutes(a, b) for a, b in zip(fw["flip_time"], fw["primary_time"])]
        forf = (fw["t2_dist"] - fw["kept_at_flip"]).tolist()
        retr = int((fw["retrace_to_initial"] == True).sum())                 # noqa: E712
        out.append(f"| {tag} | {len(w)} | {len(fw)} | {_stats(mins)} | {_stats(forf)} | "
                   f"{sum(v for v in forf if v == v):.0f} | {retr}/{len(fw)} |")
    out.append("")
    out.append("Initial distance from the entry (pts) and share of the entry→T2 distance.")
    out.append("")
    out.append("| variant | n | ≤40 | 40–60 | 60–80 | 80–120 | 120–200 | >200 | mean / median dist | "
               "mean / median share | tier 1 / 2 / 3 / synth |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for tag in tags:
        df = frames[tag]
        d = df["initial_dist"].dropna()
        bins = [(d <= 40).sum(), ((d > 40) & (d <= 60)).sum(), ((d > 60) & (d <= 80)).sum(),
                ((d > 80) & (d <= 120)).sum(), ((d > 120) & (d <= 200)).sum(), (d > 200).sum()]
        tiers = df["initial_tier"].value_counts() if "initial_tier" in df else pd.Series(dtype=int)
        tier_txt = ("–" if tiers.empty else
                    " / ".join(str(int(tiers.get(k, 0))) for k in (1, 2, 3, 0)))
        out.append(f"| {tag} | {len(d)} | " + " | ".join(str(int(b)) for b in bins)
                   + f" | {_stats(d.tolist())} | {_stats(df['initial_share'].dropna().tolist())}"
                   f" | {tier_txt} |")
    out.append("")
    out.append("The six reviewed days (level / pts from entry / share of D / tier; `*` = flipped, "
               "`T2` = primary reached).")
    out.append("")
    out.append("| variant | " + " | ".join(REVIEWED_DAYS) + " |")
    out.append("|---|" + "---|" * len(REVIEWED_DAYS))
    for tag in tags:
        df = frames[tag].set_index("date")
        cells = []
        for d in REVIEWED_DAYS:
            if d not in df.index:
                cells.append("–")
                continue
            r = df.loc[d]
            if r["initial_level"] is None or (isinstance(r["initial_level"], float)
                                              and pd.isna(r["initial_level"])):
                cells.append(f"none ({r['initial_reason']})")
                continue
            tier = r.get("initial_tier")
            tier_txt = "" if tier is None or pd.isna(tier) else f" t{int(tier)}"
            flags = ("*" if r["initial_flipped"] else "") + (" T2" if r["primary_reached"] else "")
            cells.append(f"{r['initial_level']} {r['initial_dist']:.0f} pts {r['initial_share']:.0%}"
                         f"{tier_txt} {flags}".strip())
        out.append(f"| {tag} | " + " | ".join(cells) + " |")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def _write(rows: list, tag: str) -> str:
    df = pd.DataFrame(rows, columns=COLUMNS)
    tsv = os.path.join(OUT_DIR, f"initial_target_lookahead_{tag}.tsv")
    df.to_csv(tsv, sep="\t", index=False)
    with open(os.path.join(OUT_DIR, f"initial_target_lookahead_{tag}.json"), "w",
              encoding="utf-8") as f:
        json.dump(rows, f, indent=1, default=str)
    return tsv


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dates", nargs="*", help="YYYY-MM-DD (default: the last 30 RTH days)")
    ap.add_argument("--extreme-min", choices=sorted(EXTREME_MINS), default=None)
    ap.add_argument("--mid-pref", choices=MID_PREFERENCES, default=None)
    ap.add_argument("--all-variants", action="store_true")
    ap.add_argument("--capture-fixtures", metavar="DIR", default=None)
    args = ap.parse_args(argv)
    os.makedirs(OUT_DIR, exist_ok=True)

    if args.all_variants:
        variants = list(GRID)
    elif args.extreme_min is None and args.mid_pref is None:
        variants = [None]                                  # the code's defaults
    else:
        from agent.trader import initial_target as it
        e = args.extreme_min or next(k for k, v in EXTREME_MINS.items() if v == it.EXTREME_MIN)
        variants = [(e, args.mid_pref or it.MID_PREFERENCE)]

    days = args.dates or trading_days(N_DAYS)
    print(f"{len(days)} days: {days[0]} .. {days[-1]}; variants: "
          f"{[_tag(v) for v in variants]}", flush=True)
    rows: dict = {_tag(v): [] for v in variants}
    t0 = time.time()
    for i, d in enumerate(days, 1):
        t1 = time.time()
        try:
            ctx = day_context(d)
        except Exception as e:  # keep going; a broken day is a row, not a crash
            for v in variants:
                r = {c: None for c in COLUMNS}
                r.update(date=d, **{"class": "ERR", "initial_reason": f"error:{type(e).__name__}:{e}"})
                rows[_tag(v)].append(r)
            print(f"[{i:2d}/{len(days)}] {d} ERROR {type(e).__name__}: {e}", flush=True)
            continue
        if args.capture_fixtures:
            os.makedirs(args.capture_fixtures, exist_ok=True)
            with open(os.path.join(args.capture_fixtures, f"{d}.json"), "w", encoding="utf-8") as f:
                json.dump(fixture_from_context(ctx), f, indent=1, default=str)
        line = []
        for v in variants:
            try:
                r = study_day(d, v, ctx)
            except Exception as e:
                r = {c: None for c in COLUMNS}
                r.update(date=d, **{"class": "ERR", "initial_reason": f"error:{type(e).__name__}:{e}"})
            rows[_tag(v)].append(r)
            line.append(f"{_tag(v)}: {r['initial_level']}@{r['initial_price']} {r['class']}")
        et = ctx["entry_time"].strftime("%H:%M") if ctx["entry_time"] is not None else None
        print(f"[{i:2d}/{len(days)}] {d} {ctx['direction']} entry {et}@{ctx['entry_price']} "
              f"T2={ctx['t2_price']} ({ctx['t2_level']}) | " + " | ".join(line)
              + f" [{time.time() - t1:.1f}s]", flush=True)
    for v in variants:
        tag = _tag(v)
        if v is None:                                      # the defaults: name by their tag
            from agent.trader import initial_target as it
            e = next((k for k, val in EXTREME_MINS.items() if val == it.EXTREME_MIN), "custom")
            tag = f"{e}_{it.MID_PREFERENCE}"
        print(f"wrote {_write(rows[_tag(v)], tag)}", flush=True)
    if args.all_variants and not args.dates:
        frames = {}
        v1 = os.path.join(OUT_DIR, "initial_target_lookahead_v1.tsv")
        if os.path.exists(v1):
            frames["v1"] = pd.read_csv(v1, sep="\t")
        for v in variants:
            frames[_tag(v)] = pd.DataFrame(rows[_tag(v)], columns=COLUMNS)
        md = summarize_variants(frames)
        path = os.path.join(OUT_DIR, "variants_summary.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        print(f"wrote {path}", flush=True)
    print(f"done in {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
