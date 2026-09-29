"""A/B harness for O3 (`micro_smt_reject`) / O4 (`micro_smt_exit`) — ADOPTED 2026-09-26,
see `l2-mechanisms.md` §7a/§7b and `agent/trader/micro_smt.py`. This harness produced the
30-day evidence behind that adoption (adoption PR description); kept as-is (still useful for a
future re-tune of the knobs §7a left UNTUNED) rather than deleted now that the rule ships.

Two arms per date, replayed SEQUENTIALLY in one process (never in parallel — memory is
tight on this machine):

    A = both `agent.trader.micro_smt.MICRO_SMT_ENTRY_ENABLED` /
        `MICRO_SMT_EXIT_ENABLED` False (today's behaviour, byte-identical)
    B = both True

The flags are flipped as plain module attributes and read LIVE by every caller
(`executor.py` and `market_mechanisms.py` both read `micro_smt.<FLAG>` at call time, never
`from ... import <FLAG>` — see `micro_smt.py`'s own docstring on why), so toggling them
between `run_replay` calls in this one process is safe and does not require a subprocess
per arm.

**The thesis is a LOOKAHEAD ORACLE**, by design (operator decision, `feature.md`): this
harness tests the ENTRY/EXIT mechanisms, not L1's direction call, so every date gets the
"right" direction and DOL rather than a recorded or seeded one. Two sources, in order:

  1. `.agents/session-skeleton/session_skeleton.jsonl`'s PRIMARY segment for the date
     gives the 09:30 move's direction; `.agents/label-corpus/labels.jsonl`'s labelled row
     for (date, that segment, ticker MNQ) gives the DOL, if one was labelled.
  2. Neither file covers dates after 2026-08-31 (checked at the time this was written --
     re-check if this is ever re-run against updated corpora), which is MOST of the
     default 30-day window. For those dates the direction and DOL are DERIVED directly
     from the 1m bars: bias = the sign of the larger excursion from the 09:30 OPEN over
     09:30-13:00 (the replay window), DOL = that extreme's own price. This is look-ahead
     BY DESIGN -- an oracle thesis always is -- and is exactly the "documented simple
     rule" the brief asked for if the skeleton doesn't cover a date.

Every date's oracle source is recorded per-row (`thesis_source`: "skeleton" or "bars") so
a reader can see which dates used which, per the brief's requirement.

**`--preopen` (plan 43, `l2-mechanisms.md` §7a.1).** A different pair of arms, O3 and O4
ON in both: A = `MICRO_SMT_PREOPEN_PAIR_ENABLED` False, B = True. `--legacy-arm` adds A0 =
the code before §7a.1 (pair off AND the detector blind to bars it was not asked about --
the whole-session reading is not flag-gated, so A and B both carry it and only A0 shows
what it moved). Rows carry `ab_mode: "preopen"`; the default `--out` is
`.agents/ab_micro_smt_preopen.jsonl`.

Usage:

    python scripts/ab_micro_smt.py --dates 2026-09-24                     # smoke, one date
    python scripts/ab_micro_smt.py --last-30 --out .agents/ab_micro_smt.jsonl
    python scripts/ab_micro_smt.py --last-30 --resume --out ...jsonl
    python scripts/ab_micro_smt.py --summarize --out .agents/ab_micro_smt.jsonl
    python scripts/ab_micro_smt.py --preopen --last-n 10 --end-date 2026-09-28 --legacy-arm
    python scripts/ab_micro_smt.py --preopen --summarize
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import agent.trader.micro_smt as micro_smt                                # noqa: E402
from agent.trader.replay import run_replay                                # noqa: E402
from scripts.report_replay_pnl import MNQ_PNL_PER_POINT, contracts, summarize  # noqa: E402

TZ = "America/New_York"
SKELETON_PATH = os.path.join(_REPO, ".agents", "session-skeleton",
                             "session_skeleton.jsonl")
LABELS_PATH = os.path.join(_REPO, ".agents", "label-corpus", "labels.jsonl")


# --------------------------------------------------------------------------- #
# the lookahead oracle thesis                                                  #
# --------------------------------------------------------------------------- #

def _load_jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _thesis(bias: str, dol_level: str, dol_price: float, *, falsify_at: float) -> dict:
    """Matches `named_cases._thesis`'s shape exactly (not imported from there: that
    module is the RULE registry, and an oracle harness's synthetic theses are not
    rules — see `docs/entry-mechanism-change-protocol.md` step 0)."""
    side_out = "above" if bias == "DOWN" else "below"
    return {"bias": bias, "regime": "TREND", "confidence": "MEDIUM",
            "dol": {"level": dol_level, "price": dol_price},
            "falsified_if": [{"type": "price_beyond", "price": float(falsify_at),
                              "side": side_out}]}


def _from_skeleton(date: str) -> "dict | None":
    """`(thesis, source)` from the session skeleton + label corpus, or None if the
    date is not covered by either file."""
    skeleton = [r for r in _load_jsonl(SKELETON_PATH) if r.get("date") == date]
    primary = next((r for r in skeleton if r.get("role") == "primary"), None)
    if primary is None:
        return None
    bias = str(primary.get("direction") or "").upper()
    if bias not in ("UP", "DOWN"):
        return None
    seg_idx = primary.get("index")
    labels = [r for r in _load_jsonl(LABELS_PATH)
             if r.get("date") == date and r.get("segment") == seg_idx]
    mnq_row = next((r for r in labels if r.get("ticker") == "MNQ"
                    and r.get("status") == "labelled" and r.get("price") is not None),
                   None)
    avg_1h = None
    for r in labels:
        avg = (r.get("avg_range_1h") or {}).get("MNQ")
        if avg:
            avg_1h = float(avg)
            break
    if mnq_row is not None:
        dol_level, dol_price = mnq_row["names"][0], float(mnq_row["price"])
    else:
        dol_level, dol_price = "oracle_dol", float(primary["extreme_price"])
    start_price = float(primary["start_price"])
    move = avg_1h if avg_1h else 60.0     # documented fallback if avg_range_1h is absent
    falsify_at = start_price + move if bias == "DOWN" else start_price - move
    return {"thesis": _thesis(bias, dol_level, dol_price, falsify_at=falsify_at),
            "source": "skeleton", "avg_range_1h": avg_1h}


def _from_bars(date: str) -> "dict | None":
    """Fallback for a date the skeleton/labels do not cover: derive bias and DOL
    directly from the 1m bars. Documented simple rule (operator-sanctioned lookahead,
    since this whole harness IS an oracle):

      bias  = sign of the larger excursion from the 09:30 OPEN over 09:30-13:00
      DOL   = that excursion's own extreme price
      move  = mean(High-Low) of the trailing 24h of 1h bars before 09:30 (an
              avg-1h-range proxy, computed the same way `derive_facts`'s 1h grid is:
              18:00-anchored) -- the falsifier is one such move against the thesis
              from the 09:30 open.
    """
    from backtest_smt import _main_dir_for_date
    d = _main_dir_for_date(date)
    mnq_path = os.path.join(str(d), "MNQ_1m.parquet")
    if not os.path.exists(mnq_path):
        return None
    mnq = pd.read_parquet(mnq_path)
    try:
        day = mnq.loc[date]
    except KeyError:
        return None
    if not len(day):
        return None
    session = day.between_time("09:30", "13:00")
    if not len(session):
        return None
    open_ = float(session.iloc[0]["Open"])
    up_excursion = float(session["High"].max()) - open_
    down_excursion = open_ - float(session["Low"].min())
    if up_excursion >= down_excursion:
        bias, dol_price = "UP", float(session["High"].max())
    else:
        bias, dol_price = "DOWN", float(session["Low"].min())

    anchor = pd.Timestamp(f"{date} 09:30", tz=TZ) - pd.Timedelta(hours=24)
    window = mnq[(mnq.index >= anchor - pd.Timedelta(hours=24))
                & (mnq.index < pd.Timestamp(f"{date} 09:30", tz=TZ))]
    move = 60.0
    if len(window):
        h = window.resample("1h", origin=anchor).agg(
            {"High": "max", "Low": "min"}).dropna()
        if len(h):
            move = float((h["High"] - h["Low"]).mean())
    falsify_at = open_ + move if bias == "DOWN" else open_ - move
    return {"thesis": _thesis(bias, "oracle_dol", dol_price, falsify_at=falsify_at),
            "source": "bars", "avg_range_1h": move}


def oracle_for(date: str) -> dict:
    got = _from_skeleton(date)
    if got is None:
        got = _from_bars(date)
    if got is None:
        raise ValueError(f"{date}: no oracle thesis source (neither the skeleton/labels "
                         f"nor the 1m bars cover it)")
    return got


# --------------------------------------------------------------------------- #
# the 30-date window                                                           #
# --------------------------------------------------------------------------- #

def _has_session_bars(date: str, min_bars: int = 60) -> bool:
    """True if `date`'s own MNQ 1m parquet (per its rollover-routed contract dir) has at
    least `min_bars` bars inside the 09:30-13:00 ET replay window. Reused as the single
    definition of "a real trading date" so the date list and `_from_bars`'s own fallback
    agree on what counts as coverage.

    This is the fix for the 2026-08-23 crash: the prior version collected every calendar
    date present ANYWHERE in the 1s index, which also catches a Sunday whose Globex
    evening bars get normalized onto that calendar date -- a Sunday has zero bars in
    09:30-13:00 ET (the US session hasn't opened), so this predicate correctly excludes
    it without needing a market-calendar dependency."""
    from backtest_smt import _main_dir_for_date
    d = _main_dir_for_date(date)
    mnq_path = os.path.join(str(d), "MNQ_1m.parquet")
    if not os.path.exists(mnq_path):
        return False
    try:
        mnq = pd.read_parquet(mnq_path)
        day = mnq.loc[date]
    except KeyError:
        return False
    if not len(day):
        return False
    return len(day.between_time("09:30", "13:00")) >= min_bars


def last_n_trading_dates(end_date: str, n: int) -> list:
    """The last `n` calendar dates <= `end_date` that are WEEKDAYS with a real 09:30-13:00
    ET session (see `_has_session_bars`) -- excludes weekends/holidays even when the raw
    1s/1m index has rows normalized onto those calendar dates. Walks backward day by day
    from `end_date`, capped at `n * 8` calendar days so a long holiday run can't spin
    forever without silently returning fewer than `n` dates."""
    cursor = pd.Timestamp(end_date)
    kept: list = []
    for _ in range(n * 8):
        if len(kept) >= n:
            break
        ds = cursor.date().isoformat()
        if cursor.weekday() < 5 and _has_session_bars(ds):
            kept.append(ds)
        cursor -= pd.Timedelta(days=1)
    return list(reversed(kept))


# --------------------------------------------------------------------------- #
# one (date, arm) run                                                         #
# --------------------------------------------------------------------------- #

def _mechanism_counts(run_dir: str) -> dict:
    path = os.path.join(run_dir, "trader_decisions.jsonl")
    entries = exits = 0
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if r.get("kind") == "fill" and r.get("mechanism") == "micro_smt_reject":
                    entries += 1
                if r.get("kind") == "micro_smt_exit":
                    exits += 1
    return {"micro_smt_reject_entries": entries, "micro_smt_exit_exits": exits}


_FLAGS = ("MICRO_SMT_ENTRY_ENABLED", "MICRO_SMT_EXIT_ENABLED",
          "MICRO_SMT_PREOPEN_PAIR_ENABLED")


def _blind_on_bar_close(orig):
    """A0's detector: `MicroSmt.on_bar_close` with the session history dropped, i.e. the
    pre-§7a.1 machine that only saw the bars it was asked about."""
    def wrapped(self, now, mnq_bar, mes_bar, prev_extremes, **_):
        return orig(self, now, mnq_bar, mes_bar, prev_extremes)
    return wrapped


def run_arm(date: str, thesis: dict, *, entry_enabled: bool, exit_enabled: bool,
            preopen_enabled: "bool | None" = None, blind: bool = False,
            arm: "str | None" = None) -> dict:
    saved = {f: getattr(micro_smt, f) for f in _FLAGS}
    orig_on_bar_close = micro_smt.MicroSmt.on_bar_close
    micro_smt.MICRO_SMT_ENTRY_ENABLED = entry_enabled
    micro_smt.MICRO_SMT_EXIT_ENABLED = exit_enabled
    if preopen_enabled is not None:
        micro_smt.MICRO_SMT_PREOPEN_PAIR_ENABLED = preopen_enabled
    if blind:
        micro_smt.MicroSmt.on_bar_close = _blind_on_bar_close(orig_on_bar_close)
    try:
        res = run_replay([date], allow_calls=False, thesis=thesis)
    finally:
        # Restore whatever the caller had, even on an exception -- this module is
        # imported, not run in a fresh interpreter.
        for f, v in saved.items():
            setattr(micro_smt, f, v)
        micro_smt.MicroSmt.on_bar_close = orig_on_bar_close
    run_dir = res[date]["run_dir"]
    summary = summarize(run_dir)
    n = contracts()
    row = {
        "date": date,
        "arm": arm or ("B" if (entry_enabled or exit_enabled) else "A"),
        "run_dir": run_dir, "status": summary["status"],
        "realised_pts": summary["realised_pts"], "marked_pts": summary["marked_pts"],
        "total_pts": summary["total_pts"],
        "total_usd": summary["total_pts"] * MNQ_PNL_PER_POINT * n,
        "n_trades": summary["n_trades"],
        "trades": [{"mechanism": t.get("mechanism"), "entry_ts": t.get("entry_ts"),
                    "entry": t.get("entry"), "exit_ts": t.get("exit_ts"),
                    "exit": t.get("exit"), "exit_kind": t.get("exit_kind"),
                    "points": t.get("points")} for t in summary["trades"]],
        "plan_dead": summary["plan_dead"],
    }
    row.update(_mechanism_counts(run_dir))
    return row


# --------------------------------------------------------------------------- #
# the run loop                                                                 #
# --------------------------------------------------------------------------- #

def _done_pairs(out_path: str) -> set:
    done = set()
    if os.path.exists(out_path):
        for r in _load_jsonl(out_path):
            done.add((r["date"], r["arm"]))
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dates", help="comma-separated YYYY-MM-DD (smoke-test subset)")
    ap.add_argument("--last-30", action="store_true",
                    help="the last 30 trading dates with 1s data, ending --end-date")
    ap.add_argument("--last-n", type=int,
                    help="the last N trading dates, ending --end-date")
    ap.add_argument("--end-date", default="2026-09-24")
    ap.add_argument("--out", default=None,
                    help="default .agents/ab_micro_smt.jsonl (--preopen: "
                         ".agents/ab_micro_smt_preopen.jsonl)")
    ap.add_argument("--preopen", action="store_true",
                    help="§7a.1 A/B: O3/O4 on in both arms, pre-open pair off (A) / on (B)")
    ap.add_argument("--legacy-arm", action="store_true",
                    help="with --preopen: also run A0, the pre-§7a.1 detector")
    ap.add_argument("--resume", action="store_true",
                    help="skip (date, arm) pairs already in --out")
    ap.add_argument("--summarize", action="store_true",
                    help="read --out and print the per-date and total B-A delta; runs "
                        "nothing")
    args = ap.parse_args()
    if args.out is None:
        args.out = os.path.join(_REPO, ".agents", "ab_micro_smt_preopen.jsonl"
                                if args.preopen else "ab_micro_smt.jsonl")

    if args.summarize:
        return _print_summary(args.out)

    if args.dates:
        dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    elif args.last_n:
        dates = last_n_trading_dates(args.end_date, args.last_n)
    elif args.last_30:
        dates = last_n_trading_dates(args.end_date, 30)
    else:
        ap.error("give --dates for a smoke test, or --last-n N / --last-30 for a window")
        return 2

    # (arm, run_arm kwargs)
    if args.preopen:
        arms = ([("A0", dict(entry_enabled=True, exit_enabled=True, preopen_enabled=False,
                             blind=True))] if args.legacy_arm else [])
        arms += [("A", dict(entry_enabled=True, exit_enabled=True, preopen_enabled=False)),
                 ("B", dict(entry_enabled=True, exit_enabled=True, preopen_enabled=True))]
    else:
        arms = [("A", dict(entry_enabled=False, exit_enabled=False)),
                ("B", dict(entry_enabled=True, exit_enabled=True))]

    done = _done_pairs(args.out) if args.resume else set()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "a", encoding="utf-8") as fh:
        for date in dates:
            try:
                got = oracle_for(date)
            except Exception as e:
                # A bad date must not kill the other 29 -- record an error row per
                # still-missing arm (so --resume treats this date as "done, but failed"
                # rather than retrying it forever) and move on.
                print(f"[ab] {date}: ORACLE ERROR ({type(e).__name__}): {e} -- "
                     f"recording error rows and continuing")
                for arm, _ in arms:
                    if (date, arm) in done:
                        continue
                    fh.write(json.dumps({
                        "date": date, "arm": arm, "status": "error",
                        "error": f"oracle: {type(e).__name__}: {e}",
                    }) + "\n")
                    fh.flush()
                    done.add((date, arm))
                continue
            thesis, source = got["thesis"], got["source"]
            print(f"[ab] {date}: oracle {thesis['bias']} dol={thesis['dol']} "
                 f"(source={source})")
            for arm, kwargs in arms:
                if (date, arm) in done:
                    print(f"[ab] {date} arm {arm}: already done, skipping (--resume)")
                    continue
                try:
                    row = run_arm(date, thesis, arm=arm, **kwargs)
                    row["thesis_source"] = source
                    if args.preopen:
                        row["ab_mode"] = "preopen"
                except Exception as e:
                    print(f"[ab] {date} arm {arm}: REPLAY ERROR ({type(e).__name__}): "
                         f"{e} -- recording error row and continuing")
                    row = {"date": date, "arm": arm, "status": "error",
                           "error": f"replay: {type(e).__name__}: {e}",
                           "thesis_source": source}
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                done.add((date, arm))
                if row.get("status") == "error":
                    continue
                print(f"[ab] {date} arm {arm}: {row['total_pts']:+.2f} pts "
                     f"(${row['total_usd']:+.2f}), {row['n_trades']} trades, "
                     f"micro_smt entries={row['micro_smt_reject_entries']} "
                     f"exits={row['micro_smt_exit_exits']}")
    return 0


def _is_preopen_o3(t) -> bool:
    """An O3 trade entered before 10:30 ET -- only §7a.1's pair can do that."""
    ts = t.get("entry_ts")
    return (t.get("mechanism") == "micro_smt_reject" and ts is not None
            and pd.Timestamp(ts).tz_convert(TZ).strftime("%H:%M:%S") < "10:30:00")


def _print_preopen_summary(rows) -> int:
    by_date: dict = {}
    for r in rows:
        by_date.setdefault(r["date"], {})[r["arm"]] = r
    tot = {"A0": 0.0, "A": 0.0, "B": 0.0}
    has_a0 = any("A0" in arms for arms in by_date.values())
    print(f"[ab] {'date':<12} {'A0 pts':>9} {'A pts':>9} {'B pts':>9} {'B-A':>9} "
          f"{'A-A0':>9}  pre-open O3 fires in B")
    fires = []
    for date in sorted(by_date):
        arms = by_date[date]
        a, b, a0 = arms.get("A"), arms.get("B"), arms.get("A0")
        if a is None or b is None or any(r.get("status") == "error"
                                         for r in (a, b, a0) if r is not None):
            print(f"[ab] {date:<12} INCOMPLETE/ERROR -- excluded from totals")
            continue
        tot["A"] += a["total_pts"]
        tot["B"] += b["total_pts"]
        a0_pts = a0["total_pts"] if a0 is not None else None
        if a0_pts is not None:
            tot["A0"] += a0_pts
        pre = [t for t in b["trades"] if _is_preopen_o3(t)]
        fires += [(date, t) for t in pre]
        a0s = f"{a0_pts:>9.2f}" if a0_pts is not None else f"{'-':>9}"
        dA = f"{a['total_pts'] - a0_pts:>+9.2f}" if a0_pts is not None else f"{'-':>9}"
        print(f"[ab] {date:<12} {a0s} {a['total_pts']:>9.2f} {b['total_pts']:>9.2f} "
              f"{b['total_pts'] - a['total_pts']:>+9.2f} {dA}  {len(pre)}")
    print("[ab] " + "-" * 72)
    print(f"[ab] TOTAL  A {tot['A']:+.2f}  B {tot['B']:+.2f}  B-A {tot['B'] - tot['A']:+.2f}"
          + (f"  A0 {tot['A0']:+.2f}  A-A0 {tot['A'] - tot['A0']:+.2f}" if has_a0 else ""))
    attributed = sum(t.get("points") or 0.0 for _, t in fires)
    print(f"[ab]   pre-open O3 trades in B: {attributed:+.2f} pts; knock-on (residual) "
          f"{tot['B'] - tot['A'] - attributed:+.2f} pts")
    for date, t in fires:
        print(f"[ab]   {date} {t['entry_ts']} @ {t['entry']} -> {t.get('exit_kind')} "
              f"{t.get('exit_ts')} @ {t.get('exit')}: {t.get('points'):+.2f} pts")
    return 0


def _print_summary(out_path: str) -> int:
    rows = _load_jsonl(out_path)
    if not rows:
        print(f"[ab] nothing in {out_path}")
        return 1
    if any(r.get("ab_mode") == "preopen" for r in rows):
        return _print_preopen_summary(rows)
    by_date: dict = {}
    for r in rows:
        by_date.setdefault(r["date"], {})[r["arm"]] = r
    total_delta_pts = total_delta_usd = 0.0
    total_o3_only = total_o4_only = total_combo = 0.0
    print(f"[ab] {'date':<12} {'A pts':>10} {'B pts':>10} {'delta pts':>10} "
         f"{'delta $':>10}  entries  exits")
    for date in sorted(by_date):
        a, b = by_date[date].get("A"), by_date[date].get("B")
        if a is None or b is None:
            print(f"[ab] {date:<12} INCOMPLETE (missing arm {'A' if a is None else 'B'})")
            continue
        if a.get("status") == "error" or b.get("status") == "error":
            errs = "; ".join(f"{r['arm']}: {r['error']}" for r in (a, b)
                             if r.get("status") == "error")
            print(f"[ab] {date:<12} ERROR -- excluded from totals ({errs})")
            continue
        d_pts = b["total_pts"] - a["total_pts"]
        d_usd = b["total_usd"] - a["total_usd"]
        total_delta_pts += d_pts
        total_delta_usd += d_usd
        print(f"[ab] {date:<12} {a['total_pts']:>10.2f} {b['total_pts']:>10.2f} "
             f"{d_pts:>+10.2f} {d_usd:>+10.2f}  "
             f"{b['micro_smt_reject_entries']:>7} {b['micro_smt_exit_exits']:>6}")
        # Attribute the delta: a trade OPENED by O3 and/or CLOSED by O4 contributes its
        # own points, counted once even when both are true of the same trade (an O3
        # entry that O4 then exits). Everything else that differs between A and B on
        # the same date is a KNOCK-ON (attempt-budget / NO_ENTRY_AFTER_POSITIVE
        # interaction with the other four mechanisms) -- the residual, never measured
        # directly.
        for t in b["trades"]:
            pts = t.get("points") or 0.0
            is_o3 = t.get("mechanism") == "micro_smt_reject"
            is_o4 = t.get("exit_kind") == "micro_smt_exit"
            if is_o3 and is_o4:
                total_combo += pts
            elif is_o3:
                total_o3_only += pts
            elif is_o4:
                total_o4_only += pts
    total_attributed = total_o3_only + total_o4_only + total_combo
    print("[ab] " + "-" * 72)
    print(f"[ab] TOTAL  delta {total_delta_pts:+.2f} pts (${total_delta_usd:+.2f})")
    print(f"[ab]   O3-only trades (entered by O3, exited normally): {total_o3_only:+.2f} pts")
    print(f"[ab]   O4-only trades (entered elsewhere, exited by O4): {total_o4_only:+.2f} pts")
    print(f"[ab]   O3+O4 trades (entered by O3 AND exited by O4)  : {total_combo:+.2f} pts")
    print(f"[ab]   knock-on (other mechanisms, residual): "
         f"{total_delta_pts - total_attributed:+.2f} pts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
