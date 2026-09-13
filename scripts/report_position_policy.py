"""Plan 33 Task 5: run the position policy over the corpus and report it.

Writes `.agents/rule-search/position_policy.json`: one record per session per track per
variant, carrying the fill, every stop move with its trigger, the exit and its reason,
P&L, capture and the scratch flag — plus §6's aggregates.

**2026-09-02 is excluded from every corpus statistic.** It generated the rule; its result
is an acceptance test (`agent/study/test_position_policy_sep2.py`) and never evidence. The
study range ends 2026-08-31 so it is not in the corpus to begin with, and the exclusion is
asserted rather than assumed.

**Tracks are never pooled** (§4 (b)). Sessions where a track has no DOL get no position on
that track, so the two tracks cover different session sets and a mean across them would be
meaningless. Every figure is reported on the common subset AND on each track's own set,
with counts.

Usage:
    python scripts/report_position_policy.py [--entries PATH] [--out PATH]
                                             [--limit N] [--dates D [D ...]]

`--entries` defaults to the Task 1 sweep's output. Building it takes ~25 minutes of real
Executor replay, so it is an input rather than something this script rebuilds.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys

import pandas as pd

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study import policy_baselines as pb                      # noqa: E402
from agent.study import position_policy as pp                       # noqa: E402
from agent.study import targets_for_policy as tfp                   # noqa: E402
from agent.study.entries import SessionTape                         # noqa: E402
from agent.trader.executor import MIN_FVG_HEIGHT_PTS                # noqa: E402

RULE_SEARCH = os.path.join(_REPO, ".agents", "rule-search")
DEFAULT_ENTRIES = os.path.join(RULE_SEARCH, "position_policy_entries.jsonl")
DEFAULT_OUT = os.path.join(RULE_SEARCH, "position_policy.json")
SKELETON = os.path.join(_REPO, ".agents", "session-skeleton", "session_skeleton.jsonl")

#: The date that generated the rule. Never a corpus statistic.
ACCEPTANCE_DATE = "2026-09-02"

#: §6's FVG-height variants, fixed before any result was seen. The default is NO filter;
#: the alternative is production's own `MIN_FVG_HEIGHT_PTS`, taken from the Executor
#: rather than chosen here so that nothing in the comparison set is fitted.
VARIANTS = {"no_filter": None, "min_height": MIN_FVG_HEIGHT_PTS}

#: The 2x2 clause ABLATION, and none of it is a §6 variant. §6's comparison set was frozen
#: before results existed and nothing may be added to it; these are a different kind of
#: object — the same policy with clause groups withheld, to ask which of them carry any
#: weight. §2.1 already makes the clause-12 rate a reportable quantity, which is what makes
#: the regime axis a natural thing to measure rather than an invention.
#:
#: `(use_breakeven, use_regime)`. The regime axis is expressed by WITHHOLDING the initial
#: target, which is clause 12's own documented inert path rather than a new code branch.
ARM_FULL = "A_full"
ARM_NO_REGIME = "B_no_regime"
ARM_NO_BREAKEVEN = "C_no_breakeven"
ARM_PURE_TRAIL = "D_pure_trail"
ARMS = {
    ARM_FULL: (True, True),            # all fourteen clauses
    ARM_NO_REGIME: (True, False),      # minus 6, 7
    ARM_NO_BREAKEVEN: (False, True),   # minus 3, 4
    ARM_PURE_TRAIL: (False, False),    # minus 3, 4, 6, 7 -> clause 5 + monotone + exits
}

#: §6's external reference points, recorded rather than recomputed. Each is quoted from
#: the plan; none is derived here, and none may be re-parameterised now results exist.
REFERENCE_BASELINES = {
    "legacy_cautious_ladder": {"capture": 0.44, "note": "both rungs filled 8/8"},
    "best_fixed_D_trail": {"capture": 0.4665, "note": "D = 1.20 x avg_1h, LOSO"},
    "best_time_conditioned_trail": {"capture": 0.5370,
                                    "note": "D0 = 1.60, k = -0.100, LOSO"},
}


def load_primaries(path: str = SKELETON) -> dict:
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("role") == "primary":
                out[row["date"]] = row
    return out


def load_entries(path: str) -> list:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def initial_target_for(bars_1m_full, entry):
    """`(price, level)` for one filled session, or `(None, None)`.

    Hoisted out of `run_one` because it is the expensive step — it rebuilds the legacy
    liquidity list from the 1m parquet — and it does not depend on the height variant or
    the arm. Computing it once per session rather than once per run is what keeps the
    ablation from doubling the report's cost.
    """
    fill = entry.get("fill") or {}
    if entry.get("reason") != "filled" or not fill:
        return None, None
    return tfp.initial_target(bars_1m_full, fill["direction"], float(fill["price"]),
                              pd.Timestamp(fill["ts"]))


def run_one(tape, entry, primary, variant_name, min_height, *, initial=None,
            initial_level=None, arm=ARM_FULL):
    """One session, one track, one variant, one arm -> a record, or None if no fill."""
    if entry.get("reason") != "filled" or not entry.get("fill"):
        return None
    fill = entry["fill"]
    date, track = entry["date"], entry["track"]
    direction = fill["direction"]
    fill_ts = pd.Timestamp(fill["ts"])
    fill_px = float(fill["price"])
    dol = entry.get("dol")

    bars_1m, bars_1s = tape.policy_frames(date)
    if not len(bars_1s):
        return None

    use_breakeven, use_regime = ARMS[arm]
    if not use_regime:
        # Withheld, not absent-by-accident: every session runs as if clause 12 applied.
        initial, initial_level = None, None
    run = pp.run_policy(bars_1m, bars_1s, date=date, track=track, direction=direction,
                        fill_ts=fill_ts, fill_price=fill_px,
                        initial_stop=float(fill["stop"]), dol=dol,
                        initial_target=initial, min_fvg_height=min_height,
                        use_breakeven=use_breakeven)

    tight = pb.b_tight(bars_1m, bars_1s, date=date, direction=direction,
                       fill_ts=fill_ts, fill_price=fill_px,
                       close_et=pb.POLICY_CLOSE_ET)
    loose = pb.b_loose(bars_1m, bars_1s, date=date, direction=direction,
                       fill_ts=fill_ts, fill_price=fill_px,
                       close_et=pb.POLICY_CLOSE_ET)

    extreme_ts = pd.Timestamp(primary["extreme_ts"])
    ceil = pb.ceiling(fill_px, float(primary["extreme_price"]), direction)
    rec = run.to_dict()
    rec.update({
        "variant": variant_name, "min_fvg_height": min_height, "arm": arm,
        "initial_target_level": initial_level,
        "extent": primary["extent"], "extreme_ts": primary["extreme_ts"],
        "extreme_price": primary["extreme_price"], "censored": primary["censored"],
        "ceiling": ceil, "capture": pb.capture(run.pnl, ceil),
        "scratch": pb.is_scratch(run.exit_reason, run.exit_ts, extreme_ts),
        "mechanism": fill.get("mechanism"), "dol_level": entry.get("dol_level"),
        "baselines": {
            "B-tight": dict(tight.to_dict(), ceiling=ceil,
                            capture=pb.capture(tight.pnl, ceil),
                            scratch=pb.is_scratch(tight.exit_reason, tight.exit_ts,
                                                  extreme_ts)),
            "B-loose": dict(loose.to_dict(), ceiling=ceil,
                            capture=pb.capture(loose.pnl, ceil),
                            scratch=pb.is_scratch(loose.exit_reason, loose.exit_ts,
                                                  extreme_ts)),
        },
    })
    return rec


def _rate(hits, total) -> "float | None":
    return None if not total else round(hits / total, 4)


def _pct(sorted_vals, q):
    """Nearest-rank percentile on an already-sorted list. Small n throughout, so an
    interpolating percentile would invent precision the sample does not have."""
    if not sorted_vals:
        return None
    if q <= 0:
        return sorted_vals[0]
    if q >= 100:
        return sorted_vals[-1]
    import math
    return sorted_vals[min(len(sorted_vals) - 1,
                           max(0, math.ceil(q / 100 * len(sorted_vals)) - 1))]


def summarise(rows) -> dict:
    """§6's aggregates for one (track, variant) set."""
    if not rows:
        return {"n": 0}
    exits = {}
    for r in rows:
        exits[r["exit_reason"]] = exits.get(r["exit_reason"], 0) + 1
    n = len(rows)
    armed = [r for r in rows if r["ratchet_armed"]]

    def _bl(name):
        sub = [dict(r["baselines"][name], ceiling=r["ceiling"],
                    extent=r["extent"]) for r in rows]
        return {"capture": pb.pooled_capture(sub),
                "scratch_rate": _rate(sum(1 for s in sub if s["scratch"]), n),
                "mark_rate": _rate(sum(1 for s in sub if s["is_mark"]), n),
                "points": round(sum(s["pnl"] for s in sub if s["pnl"] is not None), 2)}

    # THE LOSS SIDE. Breakeven exists to prevent losses, so capture alone flatters its
    # removal: capture has no floor, and a rule that lifts the mean while growing a fat
    # left tail is not an improvement. Reported per arm so the tail is always visible
    # beside the mean.
    pnls = sorted(float(r["pnl"]) for r in rows if r["pnl"] is not None)
    losses = [x for x in pnls if x < 0]
    tail = {
        "n_below_breakeven": len(losses),
        "rate_below_breakeven": _rate(len(losses), len(pnls)),
        "loss_total": round(sum(losses), 2),
        "loss_median": round(st.median(losses), 2) if losses else None,
        "loss_worst": round(min(losses), 2) if losses else None,
        "pnl_total": round(sum(pnls), 2),
        "pnl_mean": round(st.mean(pnls), 2) if pnls else None,
        "pnl_pctiles": {q: round(_pct(pnls, q), 2) for q in (0, 5, 10, 25, 50, 75, 90, 100)}
        if pnls else {},
    }

    return {
        "n": n,
        "capture": pb.pooled_capture(rows),
        "loss_tail": tail,
        "capture_ci95_session_clustered": pb.session_bootstrap(rows),
        "extent_terciles": pb.extent_terciles(rows),
        "points": round(sum(r["pnl"] for r in rows if r["pnl"] is not None), 2),
        "scratch_rate": _rate(sum(1 for r in rows if r["scratch"]), n),
        "exit_reasons": exits,
        "no_ceiling": sum(1 for r in rows if not r["ceiling"]),
        # §2.1: the clause-12/13 rates are a RESULT, not an implementation detail.
        "clause_12_absent_target": _rate(
            sum(1 for r in rows if r["target_status"] == pp.TARGET_ABSENT), n),
        "clause_13_target_beyond_dol": _rate(
            sum(1 for r in rows if r["target_status"] == pp.TARGET_BEYOND_DOL), n),
        "ratchet_never_armed": _rate(n - len(armed), n),
        "flip_rate_when_armed": _rate(sum(1 for r in armed if r["flipped_at"]),
                                      len(armed)),
        "clause_7_fired": _rate(
            sum(1 for r in rows if any(m["clause"] == 7 for m in r["moves"])), n),
        "baselines": {"B-tight": _bl("B-tight"), "B-loose": _bl("B-loose")},
    }


def build(entries_path, out_path, dates=None, limit=None) -> dict:
    primaries = load_primaries()
    entries = load_entries(entries_path)
    if dates:
        entries = [e for e in entries if e["date"] in set(dates)]
    if limit:
        keep = sorted({e["date"] for e in entries})[:limit]
        entries = [e for e in entries if e["date"] in set(keep)]

    excluded = [e for e in entries if e["date"] == ACCEPTANCE_DATE]
    entries = [e for e in entries if e["date"] != ACCEPTANCE_DATE]

    tape = SessionTape()
    from agent.study import legacy_liquidities as ll
    bars_1m_full = ll.load_1m("MNQ")

    records, skipped = [], 0
    for i, entry in enumerate(entries):
        primary = primaries.get(entry["date"])
        if primary is None:
            skipped += 1
            continue
        initial, initial_level = initial_target_for(bars_1m_full, entry)
        for vname, mh in VARIANTS.items():
            for arm in ARMS:
                rec = run_one(tape, entry, primary, vname, mh, initial=initial,
                              initial_level=initial_level, arm=arm)
                if rec is not None:
                    records.append(rec)
        if i % 20 == 0:
            print(f"  {i}/{len(entries)} ...", flush=True)

    tracks = sorted({r["track"] for r in records})
    full = [r for r in records if r["arm"] == ARM_FULL]
    summary = {}
    for vname in VARIANTS:
        for track in tracks:
            rows = [r for r in full if r["track"] == track and r["variant"] == vname]
            summary[f"{track}/{vname}"] = summarise(rows)

    # The 2x2, in its own block. Every arm runs on the SAME sessions as arm A, so each
    # difference is the withheld clause group and nothing else.
    ablation = {}
    for arm in ARMS:
        for vname in VARIANTS:
            for track in tracks:
                rows = [r for r in records if r["track"] == track
                        and r["variant"] == vname and r["arm"] == arm]
                ablation[f"{arm}/{track}/{vname}"] = summarise(rows)

    # Paired against arm A, session by session, because the aggregate hides how few
    # sessions carry a difference.
    paired = {}
    for arm in ARMS:
        if arm == ARM_FULL:
            continue
        for vname in VARIANTS:
            for track in tracks:
                base = {r["date"]: r for r in records if r["track"] == track
                        and r["variant"] == vname and r["arm"] == ARM_FULL}
                alt = {r["date"]: r for r in records if r["track"] == track
                       and r["variant"] == vname and r["arm"] == arm}
                shared = sorted(set(base) & set(alt))
                deltas = [alt[d]["pnl"] - base[d]["pnl"] for d in shared
                          if base[d]["pnl"] is not None and alt[d]["pnl"] is not None]
                same = sum(1 for x in deltas if abs(x) < 1e-9)
                paired[f"{arm}/{track}/{vname}"] = {
                    "n": len(deltas), "identical": same,
                    "arm_better": sum(1 for x in deltas if x > 0),
                    "arm_worse": sum(1 for x in deltas if x < 0),
                    "delta_total": round(sum(deltas), 2),
                    "delta_mean": round(st.mean(deltas), 2) if deltas else None,
                    "delta_worst": round(min(deltas), 2) if deltas else None,
                    "delta_best": round(max(deltas), 2) if deltas else None,
                    "n_carrying_the_difference": len(deltas) - same,
                }

    # §4 (b): the common subset, computed per variant so the tracks are compared on the
    # SAME sessions rather than on a mean across different ones.
    common = {}
    for vname in VARIANTS:
        sets = [{r["date"] for r in full
                 if r["track"] == t and r["variant"] == vname} for t in tracks]
        # With one track the intersection is that track's own set, which would publish
        # `per_track` a second time under a key asserting a cross-track comparison.
        shared = set.intersection(*sets) if len(sets) >= 2 else set()
        for track in tracks:
            rows = [r for r in full if r["track"] == track
                    and r["variant"] == vname and r["date"] in shared]
            common[f"{track}/{vname}"] = summarise(rows)
        common[f"_n_common/{vname}"] = len(shared)

    out = {
        "generated_from": {"entries": os.path.relpath(entries_path, _REPO),
                           "n_entry_rows": len(entries),
                           "acceptance_date_excluded": ACCEPTANCE_DATE,
                           "n_excluded_rows": len(excluded),
                           "sessions_without_a_skeleton_primary": skipped},
        "conventions": {
            "variants": {k: v for k, v in VARIANTS.items()},
            "arms": {k: {"use_breakeven": v[0], "use_regime": v[1]}
                     for k, v in ARMS.items()},
            "ablation_note": ("clause_ablation_2x2 is NOT a §6 baseline; §6 was frozen "
                              "before results existed. It is the same policy with clause "
                              "groups withheld: breakeven = clauses 3/4, regime = 6/7."),
            "policy_close_et": list(pb.POLICY_CLOSE_ET),
            "touch_basis": "1s", "logic_basis": "1m",
            "trail_buffer_pts": pp.TRAIL_BUFFER_PTS,
            "opposite_body_pts": pp.OPPOSITE_BODY_PTS,
            "be_fallback_fraction": pp.BE_FALLBACK_FRACTION,
        },
        "reference_baselines": REFERENCE_BASELINES,
        "per_track": summary,
        "clause_ablation_2x2": ablation,
        "clause_ablation_paired_vs_full": paired,
        "common_subset": common,
        "records": records,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=1, sort_keys=True, default=str)
    return out


def _print(out: dict) -> None:
    print()
    for key in sorted(out["per_track"]):
        s = out["per_track"][key]
        if not s.get("n"):
            continue
        cap, ci = s["capture"], s["capture_ci95_session_clustered"]
        # `n` counts ROWS; `pooled` is None when no row has a ceiling and the interval is
        # None below two usable rows, so neither can be formatted unguarded.
        f4 = lambda v: "   n/a" if v is None else f"{v:.4f}"
        print(f"{key:26} n={s['n']:>3}  capture {f4(cap['pooled'])} "
              f"[{f4(ci['lo'])}, {f4(ci['hi'])}]  median {f4(cap['median'])}  "
              f"pts {s['points']:>9.2f}  scratch {f4(s['scratch_rate'])}")
        b = s["baselines"]
        print(f"{'':26}   B-tight {f4(b['B-tight']['capture']['pooled'])} "
              f"(scratch {f4(b['B-tight']['scratch_rate'])})   "
              f"B-loose {f4(b['B-loose']['capture']['pooled'])} "
              f"(mark {f4(b['B-loose']['mark_rate'])})")
        print(f"{'':26}   clause12 {f4(s['clause_12_absent_target'])}  "
              f"clause13 {f4(s['clause_13_target_beyond_dol'])}  "
              f"ratchet never armed {f4(s['ratchet_never_armed'])}  "
              f"clause7 fired {f4(s['clause_7_fired'])}")
        print(f"{'':26}   exits {s['exit_reasons']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entries", default=DEFAULT_ENTRIES)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dates", nargs="*", default=None)
    args = ap.parse_args()
    out = build(args.entries, args.out, dates=args.dates, limit=args.limit)
    _print(out)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
