"""Offline target selection: three selectors, one instant, side by side.

    python -m scripts.report_target_offline --date 2026-08-24 --time 09:30:00 \
        --direction DOWN --ticker MNQ [--source 1s] [--json out.json]

Prints what T1 (09:20 prod), T2 (entry prod) and T3 (entry B8g, WIP) would each name as the
target, then -- separately and explicitly marked -- where price actually went. Read-only: it
touches the parquets and the committed label corpus and writes nothing but the optional
`--json`.

**Nothing here is a production path.** Production picks its DOL once at 09:20 and never
reconsiders it; T2 and T3 are measurements of what a later pick WOULD have said.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study.target_offline import (DEFAULT_EVAL_END_HHMM, OfflineFacts,  # noqa: E402
                                        RESOLUTIONS, SELECTORS, build_report,
                                        select_target)

_W = 92


def _rule(ch="-"):
    return ch * _W


def _hdr(title, ch="="):
    return "\n%s\n%s\n%s" % (_rule(ch), title, _rule(ch))


def _fmt(v, nd=2):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v + 0.0:.{nd}f}" if v else f"{0.0:.{nd}f}"
    return str(v)


def _menu_table(rows, pick_id) -> "list[str]":
    if not rows:
        return ["    (none eligible)"]
    out = ["    %-4s %-22s %12s %-10s %10s %-11s" %
           ("id", "level", "price", "tier", "dist_x1h", "band"),
           "    " + "-" * 74]
    for r in rows:
        mark = " <-- PICK" if r["id"] == pick_id else ""
        out.append("    %-4s %-22s %12s %-10s %10s %-11s%s" % (
            r["id"], r["level"], _fmt(r["price"]), r["tier"],
            _fmt(r["dist_ratio"], 3), r["band"] or "-", mark))
    return out


def render(rep: dict) -> str:
    req = rep["request"]
    L = []
    A = L.append
    A(_hdr("OFFLINE TARGET SELECTION -- %s %s ET  %s  %s"
           % (req["date"], req["time"], req["direction"], req["ticker"])))
    A("source           : %s parquets (%s)" % (req["source"], req["main_dir"]))
    A("lookahead window : entry -> %s ET" % req["eval_end"])
    A("")
    A("NOTE: " + rep["production_context"])

    for t in rep["tracks"]:
        A(_hdr("%s -- %s" % (t["key"], t["label"]), "-"))
        A("  boundary   : %s" % t["boundary"])
        A("  now_price  : %s  @ %s   (last close STRICTLY before the boundary)"
          % (_fmt(t["now_price"]), t["now_ts"]))
        A("  avg_1h     : %s" % _fmt(t["avg_range_1h"], 3))
        A("  menu [%s], nearest-first:" % req["direction"])
        pick = t["pick"]
        L.extend(_menu_table(t["rows"], (pick or {}).get("id")))
        if pick is None:
            A("  PICK       : (none) -- %s" % t["note"])
        else:
            A("  PICK       : %s  %s @ %s  [%s, dist=%s x avg_1h]"
              % (pick["id"], pick["level"], _fmt(pick["price"]), pick["band"],
                 _fmt(pick["dist_ratio"], 3)))
            A("               %s" % t["note"])
        if t["key"] == "T3":
            f = rep["b8g_fit"]
            A("  fit        : %s on %d pool-observations from %d sessions; pooled "
              "hazard=%s" % (f["family"], f["n_obs"], f["n_sessions"],
                             _fmt(f["pooled"], 4)))
            A("               %s" % f["note"])

    A(_hdr("RECORDED L1 THESES for this date", "-"))
    if not rep["recorded_theses"]:
        A("  (none) -- most dates have none; the production thesis_cache holds only a "
          "handful of entries.")
    for r in rep["recorded_theses"]:
        A("  [%s] %s @ boundary %s" % (r["kind"].upper(), r["path"], r["boundary"]))
        A("      bias=%s regime=%s confidence=%s" % (r["bias"], r["regime"],
                                                     r["confidence"]))
        A("      dol = %s" % json.dumps(r["dol"]))
        if r.get("matches_request") is False:
            A("      *** NOT COMPARABLE: this recording's bias (%s) is the OPPOSITE of "
              "the %s direction" % (r["bias"], req["direction"]))
            A("          under analysis, so its DOL came off the other side of the menu.")
        A("      CAVEAT: %s" % r["caveat"])

    look = rep["lookahead"]
    A("\n\n" + _rule("#"))
    A("### " + look["warning"])
    A(_rule("#"))
    w = look["window"]
    A("window: %s -> %s   (%d %s bars)" % (w["start"], w["end"], w["n_bars"],
                                           w["resolution"]))
    ex = look["extreme"]
    A("extreme in the trade direction: %s @ %s   (%s pts of excursion from now_price)"
      % (_fmt(ex["price"]), ex["ts"], _fmt(ex["excursion_pts"])))
    A("")
    A("  per menu entry (T2's universe):")
    A("    %-4s %-22s %12s %-8s %-27s %12s" %
      ("id", "level", "price", "reached", "reached_ts", "closest"))
    A("    " + "-" * 88)
    for e in look["entries"]:
        A("    %-4s %-22s %12s %-8s %-27s %12s" % (
            e["id"], e["level"], _fmt(e["price"]), "YES" if e["reached"] else "no",
            e["reached_ts"] or "-", _fmt(e["closest_approach"])))

    d = look["draw"]
    A("")
    if d["status"] == "labelled":
        A("  DRAW (labelling.py's own rule, applied to the menu's entries):")
        A("    %s %s @ %s   via=%s  reached %s" % (d.get("id"), d.get("level"),
                                                   _fmt(d.get("price")), d.get("via"),
                                                   d.get("reached_ts")))
        A("    overshoot beyond the draw = %s pts   (own extreme %s)"
          % (_fmt(d.get("overshoot")), _fmt(d.get("own_extreme"))))
    else:
        A("  DRAW: UNEXPLAINED -- %s" % d.get("reason"))
        A("    (own extreme %s; %s eligible entries, %s reached)"
          % (_fmt(d.get("own_extreme")), d.get("n_eligible"), d.get("n_reached")))

    A("")
    A("  verdicts:")
    A("    %-5s %-4s %-20s %11s %9s %8s %-10s %11s" %
      ("track", "pick", "level", "price", "err_pts", "err_x1h", "verdict",
       "vs_turn_pts"))
    A("    " + "-" * 86)
    for key in ("T1", "T2", "T3"):
        v = look["verdicts"].get(key, {})
        A("    %-5s %-4s %-20s %11s %9s %8s %-10s %11s" % (
            key, v.get("pick") or "-", (v.get("pick_level") or "-")[:20],
            _fmt(v.get("pick_price")), _fmt(v.get("error_pts")),
            _fmt(v.get("error_ratio"), 3), v.get("verdict", "-").upper(),
            _fmt(v.get("extreme_error_pts"))))
    A("    err_* is signed against the DRAW (+ = beyond it); vs_turn_pts is signed the "
      "same way against")
    A("    the actual EXTREME. They are different questions and a track can be HIT on "
      "one and far off the other.")
    for key in ("T1", "T2", "T3"):
        v = look["verdicts"].get(key, {})
        reached = v.get("pick_reached")
        tail = ("" if reached is None else
                ("; the pick itself was reached at %s" % v.get("pick_reached_ts"))
                if reached else "; the pick itself was never reached")
        A("      %s: %s%s" % (key, v.get("why"), tail))

    ag = look["agreement"]
    A("")
    A("  agreement: %d distinct target price(s) across the three tracks%s"
      % (ag["n_distinct"], " -- ALL AGREE" if ag["all_agree"] else ""))
    A("             %s" % ag["note"])
    A(_rule("#"))
    return "\n".join(L)


def render_selection(sel: dict) -> str:
    """Selection only -- no lookahead block, because there is no lookahead to show."""
    L = []
    A = L.append
    A(_hdr("TARGET SELECTION -- %s %s ET  %s  %s   [selector: %s]"
           % (sel["date"], sel["time"], sel["direction"], sel["ticker"], sel["selector"])))
    A("source     : %s parquets" % sel["source"])
    A("boundary   : %s" % sel["boundary"])
    A("now_price  : %s  @ %s   (last close STRICTLY before the boundary)"
      % (_fmt(sel["now_price"]), sel["now_ts"]))
    A("avg_1h     : %s" % _fmt(sel["avg_range_1h"], 3))
    A("menu [%s], nearest-first:" % sel["direction"])
    pick = sel["pick"]
    L.extend(_menu_table(sel["menu"], (pick or {}).get("id")))
    if pick is None:
        A("SELECTED   : (none) -- the menu is empty at this instant; production can name")
        A("             no target here, and no ranking rule can change that.")
    else:
        A("SELECTED   : %s  %s @ %s  [%s, dist=%s x avg_1h]  rank %s"
          % (pick["id"], pick["level"], _fmt(pick["price"]), pick["band"],
             _fmt(pick["dist_ratio"], 3), sel["pick_rank"]))
    A("             %s" % sel["note"])
    if "fit" in sel:
        f = sel["fit"]
        A("fit        : %s on %d pool-observations from %d sessions; %s"
          % (f["family"], f["n_obs"], f["n_sessions"], f["note"]))
    A("")
    A("NOTE: selection only. Nothing above reads a bar at or after the instant, so this")
    A("      output is what the selector would have said live at %s." % sel["time"])
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", required=True, help="ISO trade date, e.g. 2026-08-24")
    ap.add_argument("--time", required=True, help="ET wall clock, e.g. 09:30:00")
    ap.add_argument("--direction", required=True, choices=("UP", "DOWN"))
    ap.add_argument("--ticker", default="MNQ")
    ap.add_argument("--source", default="1s", choices=RESOLUTIONS)
    ap.add_argument("--until", default=DEFAULT_EVAL_END_HHMM,
                    help="ET end of the lookahead window (default %(default)s)")
    ap.add_argument("--select-only", action="store_true",
                    help="print just the selected target -- no lookahead block at all")
    ap.add_argument("--selector", default="nearest", choices=SELECTORS,
                    help="which selector to run in --select-only mode "
                         "(default %(default)s = production's nearest-first rule)")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args(argv)

    facts = OfflineFacts(source=args.source)
    if args.select_only:
        rep = select_target(facts, args.date, args.time, args.direction, args.ticker,
                            selector=args.selector)
        print(render_selection(rep))
    else:
        rep = build_report(facts, args.date, args.time, args.direction, args.ticker,
                           eval_end=args.until)
        print(render(rep))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, default=str)
        print("\nwrote %s" % args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
