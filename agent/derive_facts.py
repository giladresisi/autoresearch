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
import os

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
    tds = pd.Series(df.index.map(trade_date), index=df.index)
    return df[tds.values == td]


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--ath-mnq", type=float, default=None)
    ap.add_argument("--ath-mes", type=float, default=None)
    ap.add_argument("--hist-dir", default=None,
                    help="Optional dir with full {MNQ,MES}_1s.parquet for the long-horizon "
                         "daily/weekly tables (S5b). Data is truncated at 'now' — no lookahead.")
    args = ap.parse_args()

    data = {
        "MNQ": load(os.path.join(args.dir, "MNQ_1s_slice.parquet")),
        "MES": load(os.path.join(args.dir, "MES_1s_slice.parquet")),
    }
    now = min(df.index[-1] for df in data.values())
    td_now = trade_date(now)

    # Trading-date universe: Mon-Fri only, real sessions only (>=1000 bars) — stray
    # ticks must not mint phantom prev-days (POC run-3 G12).
    counts = pd.Series(data["MNQ"].index.map(trade_date)).value_counts()
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

    print("## S0 META")
    print(f"now = {now}  (trade date {td_now}, {now.strftime('%A')})")
    subsess = ("asia" if now.hour >= 18 else "london" if now.hour < 6
               else "ny_morning" if now.hour < 12 else "ny_evening")
    in_whip = (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) < (11, 30)
    print(f"sub-session at cut: {subsess} | inside 09:15-11:30 whipsaw window: {in_whip}")
    print(f"daily-trend checkpoint (most recent at/before now): {ckpt}")
    print(f"current session first bar: {sess_now_start}")
    print(f"prev1 trade date: {prev1_td} | prev2: {prev2_td}")
    print(f"current-week trade dates: {week_tds} | prev1-week: {prev1_week_tds}")
    print(f"ENGINE week anchor (session_pipeline._week_start_ts): {wk_anchor}")

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
        print(f"\n## S1 LEVELS {tkr} (price = wick extreme; body = close extreme)")
        tdo = float(sess_now["open"].iloc[0])
        print(f"TDO (session open {sess_now.index[0]}): {tdo}")
        two, two_ts = compute_two(df, week_tds, now)
        print(f"TWO (bar {two_ts}): {two}")

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
            print(f"{lbl} ({d}): high={h} (body {ch}) low={l} (body {cl}) close={c} (range pos {pos:.2f})")
            lv[f"{lbl}_high"] = (h, ch, "above", "day", sess_now.index[0])
            lv[f"{lbl}_low"] = (l, cl, "below", "day", sess_now.index[0])
        if prev1_week_tds:
            pw = pd.concat([session_frame(df, d) for d in prev1_week_tds])
            h, l, ch, cl = hl(pw)
            print(f"prev1_week ({prev1_week_tds[0]}..{prev1_week_tds[-1]}): high={h} (body {ch}) low={l} (body {cl})")
            lv["prev1_week_high"] = (h, ch, "above", "week", sess_now.index[0])
            lv["prev1_week_low"] = (l, cl, "below", "week", sess_now.index[0])

        wkf = df.loc[wk_anchor:now]
        h, l, ch, cl = hl(wkf)
        print(f"week running [ENGINE anchor {wk_anchor}]: high={h} low={l} mid={(h + l) / 2:.3f}")
        dh, dl, dch, dcl = hl(sess_now)
        print(f"day running: high={dh} (at {sess_now['high'].idxmax()}) low={dl} "
              f"(at {sess_now['low'].idxmin()}) mid={(dh + dl) / 2}")
        print(f"last close: {float(df['close'].iloc[-1])}")
        ath = args.ath_mnq if tkr == "MNQ" else args.ath_mes
        if ath:
            print(f"ATH {ath}: last close is {(ath - float(df['close'].iloc[-1])) / ath * 100:.2f}% below")

        if prev1_td is not None:
            pblocks = sub_blocks(session_frame(df, prev1_td))
            for name, frame in pblocks.items():
                h, l, ch, cl = hl(frame)
                if h is None:
                    continue
                print(f"{name}(prev1): high={h} (body {ch}) low={l} (body {cl})")
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
            print(f"{name}(cur, closed): high={h} (body {ch}) low={l} (body {cl})")
            lv[f"{name}(cur)_high"] = (h, ch, "above", "session", closes_at[name])
            lv[f"{name}(cur)_low"] = (l, cl, "below", "session", closes_at[name])
        levels[tkr] = lv

        print(f"\n## S2 SWEEPS {tkr} (current True Day; inclusive wick cross; body = first 15m close beyond; ages vs now)")
        for name, (price, body, side, tier, active_from) in sorted(lv.items(), key=lambda kv: -kv[1][0]):
            if side is None:
                for s in ("above", "below"):
                    t = first_cross(sess_now.loc[active_from:], price, s)
                    if t is not None:
                        print(f"{name} {price}: first {s}-cross {t} (age {age_min(t, now):.0f}m)")
                continue
            frame = sess_now.loc[active_from:]
            t = first_cross(frame, price, side)
            if t is None:
                dist, ats = closest_approach(frame, price, side)
                extra = (f" (closest approach {dist:.2f} short @ {ats}, age {age_min(ats, now):.0f}m)"
                         if dist is not None else "")
                print(f"{name} {price} [{side}]: NOT swept{extra}")
                continue
            exc = excursion_beyond(frame.loc[t:], price, side)
            thr = DEPLETE[tkr][tier]
            tb = first_body_cross_15m(frame, price, side)
            tb_s = f"{tb} (age {age_min(tb, now):.0f}m)" if tb is not None else "—"
            print(f"{name} {price} [{side}]: swept {t} (age {age_min(t, now):.0f}m) | "
                  f"max excursion beyond {exc:.2f} "
                  f"({'DEPLETED' if exc >= thr else 'not depleted'}, thr {thr:.1f}) | body15m: {tb_s}")
            add_card("sweep", tkr, name, price, side, t, tier,
                     " | DEPLETED" if exc >= thr else "")

    print("\n## S3 CROSS-TICKER SWEEP MATRIX (same level name; one swept + other not = divergence candidate)")
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
        print(f"{name} [{side}, {tier}]: wick MNQ {t1 or 'not swept'} | MES {t2 or 'not swept'}{tag}")
        print(f"{'':>{len(name)}}   body MNQ {tb1 or '—'} | MES {tb2 or '—'}{btag}")
        # Laggard reach: how close the NON-sweeping ticker came, and when — the freshness of
        # the FAILURE leg (laggard test-and-fail candidate item, next-move.md §2).
        for lg, tl, ff, pp in (("MNQ", t1, f1, p1), ("MES", t2, f2, p2)):
            if tl is None:
                dist, ats = closest_approach(ff, pp, side)
                if dist is not None:
                    print(f"{'':>{len(name)}}   laggard {lg} max reach: {dist:.2f} short of "
                          f"{pp} @ {ats} (age {age_min(ats, now):.0f}m)")
                    # laggard test-and-fail candidate: other ticker swept, this one
                    # reached within 25% of its depletion threshold and failed
                    other_swept = (t2 if lg == "MNQ" else t1) is not None
                    gate = 0.25 * DEPLETE[lg][tier]
                    if other_swept and dist <= gate:
                        add_card("CANDIDATE laggard-fail", lg, name, pp, side, ats, tier,
                                 f" | reach {dist:.2f} short (25%-of-thr gate {gate:.2f}: QUALIFIES)")

    print("\n## S3b CANDIDATE ITEM CARDS (precomputed scoring inputs; multipliers per decisions/next-move.md §2)")
    print("score = tier_weight × session_side × alignment × freshness × whipsaw = base × session_side × alignment × whipsaw")
    print("Copy tier w and freshness from the card — do NOT recompute the exponent. session_side/")
    print("alignment/whipsaw are read-dependent: take them from the next-move.md tables.")
    print("REMINDER: whipsaw ×0.5 applies ONLY to intraday STRUCTURE items inside 09:15-11:30;")
    print("level sweeps/SMTs are NOT structure items (their whipsaw = 1.0).")
    print("Cards cover swept fixed levels (freshness = sweep time) and qualifying laggard-fail")
    print("candidates (freshness = failure time); items >6h old omitted (spent). Dynamic-extreme")
    print("events and continuation items are not carded — compute their freshness from S4/S5 times.")
    if cards:
        for line in cards:
            print(line)
    else:
        print("(no candidate items within the 6h window)")

    print("\n## S4 EQUILIBRIUM (1m closes vs RUNNING mids, current session; weekly mid = ENGINE anchor)")
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
            print(f"\n{tkr} {label} mid crosses (last {len(flips)}):")
            for ts, row in flips.iterrows():
                print(f"  {ts}  close {row['close']:.2f} {'ABOVE' if row['above'] else 'below'} mid {row['mid']:.2f}")
            for a, b, lbl in ((sess_now.index[0], ckpt, "session->ckpt"),
                              (ckpt - pd.Timedelta(hours=6), ckpt, "6h->ckpt"),
                              (ckpt, now, "ckpt->now")):
                seg = j.loc[a:b]
                if len(seg):
                    print(f"  acceptance {lbl}: {seg['above'].mean() * 100:.0f}% of closes above (n={len(seg)})")

    print("\n## S5 STRUCTURE BARS (MNQ; 4hr/1hr are MIDNIGHT-anchored = engine convention)")
    df = data["MNQ"]
    print("\nTrue-Day bars (rows = session open time):")
    tds_series = pd.Series(df.index.map(trade_date), index=df.index)
    rows = []
    for d in all_tds:
        s = df[tds_series.values == d]
        if len(s) == 0:
            continue
        rows.append((s.index[0], float(s['open'].iloc[0]), float(s['high'].max()),
                     float(s['low'].min()), float(s['close'].iloc[-1])))
    print(pd.DataFrame(rows, columns=["open_ts", "open", "high", "low", "close"]).to_string(index=False))
    h4 = ohlc(df, "4h")
    print("\n4hr bars (midnight-anchored, ENGINE), last 24:")
    print(h4.tail(24).to_string())
    h1 = ohlc(df, "1h")
    print("\n1hr bars, last 24:")
    print(h1.tail(24).to_string())
    print("\n30m bars, last 16:")
    print(ohlc(df, "30min").tail(16).to_string())
    print("\n15m bars, last 16:")
    print(ohlc(df, "15min").tail(16).to_string())
    print(f"\n5m bars, checkpoint {ckpt} -> now:")
    print(ohlc(df.loc[ckpt - pd.Timedelta(minutes=5):], "5min").loc[ckpt:].to_string())

    if args.hist_dir:
        print("\n## S5b LONG-HORIZON CONTEXT (full history truncated at now; True-Day daily bars + trade-week weekly bars)")
        for tkr in ("MNQ", "MES"):
            hp = os.path.join(args.hist_dir, f"{tkr}_1s_slice.parquet")
            if not os.path.exists(hp):
                hp = os.path.join(args.hist_dir, f"{tkr}_1s.parquet")
            hf = load(hp)
            hf = hf[hf.index <= now]
            htd = pd.Series(hf.index.map(trade_date), index=hf.index)
            counts = htd.value_counts()
            days = sorted(d for d, n in counts.items() if d.weekday() < 5 and n >= 1000)
            rows = []
            for d in days[-60:]:
                s = hf[htd.values == d]
                rows.append((d, float(s["open"].iloc[0]), float(s["high"].max()),
                             float(s["low"].min()), float(s["close"].iloc[-1])))
            dtab = pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close"])
            print(f"\n{tkr} daily (True-Day) bars, last {len(dtab)} sessions:")
            print(dtab.to_string(index=False))
            dtab["iso"] = dtab["trade_date"].map(lambda d: d.isocalendar()[:2])
            wk = dtab.groupby("iso").agg(
                start=("trade_date", "first"), open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"))
            print(f"\n{tkr} weekly bars (trade weeks), last {min(len(wk), 12)}:")
            print(wk.tail(12).to_string(index=False))

    print("\n## S6 FVGs (both tickers, completed bars only; visited = 1s tape re-entered zone after the 3rd bar's OPEN label — daily.py convention)")
    for tkr, dft in data.items():
        h1t = ohlc(dft, "1h")
        h4t = ohlc(dft, "4h")
        for label, bars in ((f"{tkr} 1hr", h1t.iloc[:-1]), (f"{tkr} 4hr", h4t.iloc[:-1])):
            recent = bars[bars.index >= now - pd.Timedelta(days=10)]
            for ts, kind, lo, hi, third_ts in fvgs(recent):
                seg = dft[dft.index > third_ts]
                touched = bool(((seg["low"] <= hi) & (seg["high"] >= lo)).any()) if len(seg) else False
                print(f"{label} {ts} {kind} zone {lo}-{hi} -> {'visited' if touched else 'UNVISITED'}")

    print("\n## S7 CHECKPOINT SNAPSHOT")
    for tkr, dft in data.items():
        m1 = dft["close"].resample("1min").last().dropna()
        print(f"{tkr} close at checkpoint {ckpt}: {m1.asof(ckpt)} | at now: {float(dft['close'].iloc[-1])}")
    print("\nReminders (facts end here — decisions are yours):")
    print("- SMT fire adjudication, votes, correlation audit, ledgers, vetoes: NOT computed here.")
    print("- Week extremes/mid use the ENGINE anchor printed in S0 (equilibrium.md pins this).")
    print("- Dynamic day/week extremes and re-arm/depart states must be reasoned from S1/S5 times.")
    print("- Depletion uses exact per-ticker engine tables; boundary is >= (depleted at exactly thr).")


if __name__ == "__main__":
    main()
