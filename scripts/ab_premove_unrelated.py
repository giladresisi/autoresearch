"""A/B harness for plan 46 — the UNRELATED pre-move path (`l2-mechanisms.md` §11.5, a
CANDIDATE behind `agent/trader/premove_context.UNRELATED_PATH_MODE`, default "off").

Arms, replayed SEQUENTIALLY in one process (memory budget; never in parallel), the flags
flipped as module attributes that every caller reads at call time:

    A  mode "off"                               today's behaviour, byte-identical
    B  mode "on"                                force against an UNRELATED leg; DIRECTION
                                                only, T2 target as today (what "on" does)
    T  mode "on" + MID_TARGET_ENABLED           B plus the leg-mid take-profit (--arm-t)
    C  mode "on" + stretch_override.ARM1_ENABLED = False   (optional, --arm-c; option (a))
    D  T + the operator's leg start as the origin, on dates whose
       `operator_labels.json` row has a `leg_start` (--arm-d; others D = T, not re-run)

**The thesis is the RECORDING** (arm A reads `<global>/thesis_cache`, never the model):
`--preflight` finds the dates that raise `NetworkCallRefused`; `--seed-missing --yes`
seeds those, arm A only. On an UNRELATED day arm B makes no model call at all; on every
other day it asks the same recording as A (facts text unchanged -> cache key unchanged).

Rows go to `--out` (default `.agents/ab_premove_unrelated.jsonl`), one per (date, arm),
with `--resume`. `--regression-dir` sets `ACT_REGRESSION_DIR` for the run folders.

    python scripts/ab_premove_unrelated.py --preflight --big-days --controls
    python scripts/ab_premove_unrelated.py --seed-missing --yes --dates 2026-06-29
    python scripts/ab_premove_unrelated.py --big-days --controls --arm-t --arm-d
    python scripts/ab_premove_unrelated.py --summarize
    python scripts/ab_premove_unrelated.py --offline-sweep
    python scripts/ab_premove_unrelated.py --sweep --big-days          # descriptive only

The adoption rule is pre-registered in plan 46 §5; this script reports, it decides nothing.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import itertools
import json
import os
import sys

import numpy as np
import pandas as pd

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

TZ = "America/New_York"
DEFAULT_OUT = os.path.join(_REPO, ".agents", "ab_premove_unrelated.jsonl")
BEFORE_DIR = os.path.join(_REPO, ".agents", "ab_premove_unrelated_before")
ARTIFACTS = ("trader_decisions.jsonl", "plans.json", "thesis_state.json")

#: Report / half days, excluded BY HAND (plan 46 §5; no detector is built).
EXCLUDED = ("2026-07-03", "2026-08-12", "2026-09-10", "2026-03-06")

#: The replayable big days of plan 46 §5 (1s data from 2026-05-01, excluded days dropped).
BIG_REPLAYABLE = ("2026-05-08", "2026-05-18", "2026-06-08", "2026-06-09", "2026-06-15",
                  "2026-06-29", "2026-07-21", "2026-07-23", "2026-07-30", "2026-08-18",
                  "2026-08-25", "2026-09-01", "2026-09-17", "2026-09-21")
#: Also scanned by `--big-days` (not scored by the study). 09-29 has 1s only to 10:34.
BIG_SCAN_EXTRA = ("2026-09-25", "2026-09-28")

#: Non-big controls, already warm (plan 46 §5) ...
CONTROLS_WARM = ("2026-08-13", "2026-08-31", "2026-09-02", "2026-09-03", "2026-09-04",
                 "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28")
#: ... plus non-big days where plan 37 arm 1 fires (days.csv age <= 30; model-free), so the
#: control set proves arm 1 is untouched on NOT_BIG days. 06-23 / 08-19 also carry an
#: operator leg start (arm D must equal B there: the operator leg is not big either).
CONTROLS_ARM1 = ("2026-06-05", "2026-06-22", "2026-06-23", "2026-07-22", "2026-08-06",
                 "2026-08-14", "2026-08-19", "2026-09-11")

SWEEP_GRID = {"cut_pts_at_ref": (90.0, 120.0, 150.0), "big_pct": (1.0, 1.14, 1.3),
              "part_k": (0.4, 0.5, 0.6), "part_mult": (1.0, 1.25, 1.5)}


# --------------------------------------------------------------------------- #
# small helpers                                                                 #
# --------------------------------------------------------------------------- #

def _global_root() -> str:
    import paths
    return str(paths.global_root())


def _study_dir() -> str:
    return os.path.join(_global_root(), "studies", "premove_mid_retrace")


def _load_jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _sha(path: str) -> "str | None":
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def operator_labels() -> dict:
    """`labels` of the operator's export (read-only), or {} when absent."""
    blob = _read_json(os.path.join(_study_dir(), "operator_labels.json")) or {}
    return blob.get("labels") or {}


def leg_starts() -> dict:
    return {d: v["leg_start"] for d, v in operator_labels().items()
            if isinstance(v, dict) and v.get("leg_start")}


_FRAMES: dict = {}


def _mnq_1m(main_dir: str) -> pd.DataFrame:
    """One MNQ 1m parquet per era, cached for the process (read-only)."""
    if main_dir not in _FRAMES:
        _FRAMES[main_dir] = pd.read_parquet(os.path.join(main_dir, "MNQ_1m.parquet"))
    return _FRAMES[main_dir]


def _era_dir(date: str) -> str:
    from backtest_smt import _main_dir_for_date
    return str(_main_dir_for_date(date))


def _study_dir_1m() -> str:
    return os.path.join(_global_root(), "general", "main", "2026-12")


def _boundary(date: str) -> pd.Timestamp:
    return pd.Timestamp(date, tz=TZ) + pd.Timedelta(hours=9, minutes=20)


def offline_context(date: str, main_dir: "str | None" = None, params=None):
    """The production classifier on a 1m parquet (default: the replay era's)."""
    from agent.trader import premove_context as pc
    return pc.classify(_mnq_1m(main_dir or _era_dir(date)), _boundary(date), params)


def _has_session_bars(date: str, min_bars: int = 60) -> bool:
    try:
        day = _mnq_1m(_era_dir(date)).loc[date]
    except KeyError:
        return False
    return len(day.between_time("09:30", "13:00")) >= min_bars


def big_days(start="2026-05-01", end="2026-09-28") -> list:
    """Every weekday with a session in the replay era's 1m parquet whose leg is big
    (UNRELATED / PART, or UNKNOWN with a big leg — the short-history replays), minus the
    hand-excluded report/half days."""
    from agent.trader import premove_context as pc
    out = []
    for ts in pd.date_range(start, end, freq="D"):
        ds = ts.date().isoformat()
        if ts.weekday() >= 5 or ds in EXCLUDED or not _has_session_bars(ds):
            continue
        c = offline_context(ds)
        if c.status in (pc.UNRELATED, pc.PART) or (
                c.size_pct is not None and c.size_pct >= pc.DEFAULT_PARAMS.big_pct):
            out.append(ds)
    return out


# --------------------------------------------------------------------------- #
# one (date, arm) replay                                                        #
# --------------------------------------------------------------------------- #

class _flags:
    """Set the three module flags for one run and restore them, even on a raise."""

    def __init__(self, mode, *, arm1=True, leg_start=None, date=None, params=None,
                 mid_target=False):
        self.mode, self.arm1, self.leg_start, self.date = mode, arm1, leg_start, date
        self.params, self.mid_target = params, mid_target

    def __enter__(self):
        import agent.stretch_override as so
        from agent.trader import premove_context as pc
        self._saved = (pc.UNRELATED_PATH_MODE, dict(pc.LEG_START_OVERRIDES),
                       so.ARM1_ENABLED, pc.DEFAULT_PARAMS, pc.MID_TARGET_ENABLED)
        pc.UNRELATED_PATH_MODE = self.mode
        pc.MID_TARGET_ENABLED = bool(self.mid_target)
        pc.LEG_START_OVERRIDES = ({self.date: self.leg_start} if self.leg_start else {})
        so.ARM1_ENABLED = self.arm1
        if self.params is not None:
            pc.DEFAULT_PARAMS = self.params
        return self

    def __exit__(self, *exc):
        import agent.stretch_override as so
        from agent.trader import premove_context as pc
        (pc.UNRELATED_PATH_MODE, pc.LEG_START_OVERRIDES, so.ARM1_ENABLED,
         pc.DEFAULT_PARAMS, pc.MID_TARGET_ENABLED) = self._saved
        return False


def _run_row(date: str, arm: str, run_dir: str, cache: dict) -> dict:
    from scripts.report_replay_pnl import summarize
    recs = _load_jsonl(os.path.join(run_dir, "trader_decisions.jsonl"))
    ts = _read_json(os.path.join(run_dir, "thesis_state.json")) or {}
    thesis = ts.get("thesis") or {}
    pm = _read_json(os.path.join(run_dir, "premove_context.json"))
    plans = _read_json(os.path.join(run_dir, "plans.json"))
    s = summarize(run_dir)
    fills = [r for r in recs if r.get("kind") == "fill"]
    targets = [{"time": r.get("time"), "level": r.get("level"), "target": r.get("target"),
                "premove_mid": r.get("premove_mid")}
               for r in recs if r.get("kind") == "target_selected"]
    dead = [r for r in recs if r.get("kind") == "plan_dead"]
    mid_hit = any(r.get("reason") == "target_reached"
                  and (r.get("detail") or {}).get("level") == "premove_leg_mid" for r in dead)
    src = thesis.get("thesis_source") or ("model" if thesis else None)
    if thesis and (ts.get("call_meta") or {}).get("tiebreak"):
        src = "tiebreak"
    return {
        "date": date, "arm": arm, "run_dir": run_dir, "status": s["status"],
        "thesis_source": src, "direction": thesis.get("bias"),
        "dol": thesis.get("dol"), "stands": ts.get("stands"),
        "n_trades": s["n_trades"], "attempts": len(fills),
        "realised_pts": s["realised_pts"], "marked_pts": s["marked_pts"],
        "total_pts": s["total_pts"],
        "trades": [{k: t.get(k) for k in ("mechanism", "entry_ts", "entry", "exit_ts",
                                           "exit", "exit_kind", "points")}
                   for t in s["trades"]],
        "targets": targets, "mid_hit": mid_hit,
        "plan_dead": s["plan_dead"],
        "first_entry": fills[0].get("time") if fills else None,
        "premove_context": pm, "cache": cache,
        "plans_n": len(plans) if isinstance(plans, (list, dict)) else None,
        "sha": {a: _sha(os.path.join(run_dir, a)) for a in ARTIFACTS},
    }


def run_arm(date: str, arm: str, *, mode: str, arm1: bool = True, leg_start=None,
            params=None, allow_calls: bool = False, mid_target: bool = False) -> dict:
    from agent.trader.replay import run_replay
    with _flags(mode, arm1=arm1, leg_start=leg_start, date=date, params=params,
                mid_target=mid_target):
        res = run_replay([date], allow_calls=allow_calls)
    r = res[date]
    row = _run_row(date, arm, r["run_dir"], r["cache"])
    row.update({"mode": mode, "arm1_enabled": arm1, "leg_start": leg_start,
                "mid_target": mid_target})
    return row


#: B is what "on" does today: the DIRECTION only (MID_TARGET_ENABLED off, operator decision
#: 2026-09-30). T adds the leg-mid take-profit; D is T with the operator's leg start.
ARMS = {
    "A": dict(mode="off"),
    "B": dict(mode="on"),
    "T": dict(mode="on", mid_target=True),
    "C": dict(mode="on", arm1=False),
    "D": dict(mode="on", mid_target=True),
}


def _token_usage(date: str) -> list:
    """Recorded usage of any EARLIER recording for `date` (stale keys), for the preflight
    print: what a re-seed is likely to cost. Read-only on the cache."""
    from agent.trader.thesis_cache import cache_root
    out = []
    root = cache_root()
    if not os.path.isdir(root):
        return out
    for name in os.listdir(root):
        if not name.endswith(".json"):
            continue
        rec = _read_json(os.path.join(root, name)) or {}
        if str(rec.get("boundary") or "").startswith(date):
            meta = rec.get("meta") or {}
            out.append({"key": name[:-5], "usage": meta.get("usage"),
                        "latency_sec": meta.get("latency_sec")})
    return out


# --------------------------------------------------------------------------- #
# modes                                                                         #
# --------------------------------------------------------------------------- #

def _dates(args) -> list:
    out = []
    if args.dates:
        out += [d.strip() for d in args.dates.split(",") if d.strip()]
    if args.big_days:
        scanned = big_days()
        extra = [d for d in scanned if d not in BIG_REPLAYABLE]
        if extra:
            print(f"[ab] --big-days found big days beyond plan 46's list: {extra}")
        out += list(BIG_REPLAYABLE) + [d for d in BIG_SCAN_EXTRA if d in scanned] + extra
    if args.controls:
        out += list(CONTROLS_WARM) + list(CONTROLS_ARM1)
    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def preflight(dates, out_path) -> int:
    """Arm A with calls refused. Records the BEFORE artifacts (hashes in the row) and the
    dates that need `--seed`."""
    from agent.trader.cached_backend import NetworkCallRefused
    need = []
    with open(out_path, "a", encoding="utf-8") as fh:
        for d in dates:
            try:
                row = run_arm(d, "A", **ARMS["A"])
                row["preflight"] = True
            except NetworkCallRefused as exc:
                row = {"date": d, "arm": "A", "status": "needs_seed", "error": str(exc),
                       "recorded_usage": _token_usage(d), "preflight": True}
                need.append(d)
            except Exception as exc:
                row = {"date": d, "arm": "A", "status": "error", "preflight": True,
                       "error": f"{type(exc).__name__}: {exc}"}
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
            print(f"[preflight] {d}: {row.get('status')} src={row.get('thesis_source')} "
                  f"pts={row.get('total_pts')}")
    print(f"[preflight] needs --seed ({len(need)}): {','.join(need)}")
    for d in need:
        print(f"[preflight]   {d}: earlier recordings {_token_usage(d)}")
    return 0


def seed_missing(dates, yes: bool, out_path) -> int:
    print(f"[seed] arm A only, ONE model call per date, on: {dates}")
    if not yes:
        print("[seed] refusing without --yes")
        return 2
    with open(out_path, "a", encoding="utf-8") as fh:
        for d in dates:
            try:
                row = run_arm(d, "A", allow_calls=True, **ARMS["A"])
                row["seeded"] = True
            except Exception as exc:
                row = {"date": d, "arm": "A", "status": "error", "seeded": True,
                       "error": f"{type(exc).__name__}: {exc}"}
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
            print(f"[seed] {d}: {row.get('status')} src={row.get('thesis_source')} "
                  f"bias={row.get('direction')} cache={row.get('cache')}")
    return 0


def run_ab(dates, out_path, *, arm_c: bool, arm_d: bool, resume: bool,
           arm_t: bool = False) -> int:
    starts = leg_starts()
    done = ({(r["date"], r["arm"]) for r in _load_jsonl(out_path)
             if not r.get("preflight") and not r.get("seeded") and not r.get("sweep")
             and r.get("status") != "error"}
            if resume else set())
    arms = (["A", "B"] + (["T"] if arm_t else []) + (["C"] if arm_c else [])
            + (["D"] if arm_d else []))
    with open(out_path, "a", encoding="utf-8") as fh:
        for d in dates:
            try:
                era = os.path.basename(_era_dir(d))
                off = offline_context(d)
                study = offline_context(d, _study_dir_1m())
                off_info = {"offline_status": off.status,
                            "offline_leg": [off.leg_direction, off.size, off.origin,
                                            off.extreme],
                            "study_status": study.status}
            except Exception as exc:
                era, off_info = None, {"offline_status": None,
                                       "offline_error": f"{type(exc).__name__}: {exc}"}
            for arm in arms:
                if (d, arm) in done:
                    continue
                if arm == "D" and d not in starts:
                    continue                                   # D = T, not re-run
                kw = dict(ARMS[arm])
                if arm == "D":
                    kw["leg_start"] = starts[d]
                try:
                    row = run_arm(d, arm, **kw)
                except Exception as exc:
                    row = {"date": d, "arm": arm, "status": "error",
                           "error": f"{type(exc).__name__}: {exc}"}
                if arm == "A" and row.get("sha"):
                    before = {a: _sha(os.path.join(BEFORE_DIR, d, a)) for a in ARTIFACTS}
                    row["before_identical"] = (None if not any(before.values())
                                               else before == row["sha"])
                row.update({"era": era, **off_info,
                            "control": d in CONTROLS_WARM or d in CONTROLS_ARM1})
                fh.write(json.dumps(row, default=str) + "\n")
                fh.flush()
                print(f"[ab] {d} {arm}: {row.get('status')} src={row.get('thesis_source')} "
                      f"dir={row.get('direction')} n={row.get('n_trades')} "
                      f"pts={row.get('total_pts')} mid_hit={row.get('mid_hit')}")
    return 0


# --------------------------------------------------------------------------- #
# summary                                                                       #
# --------------------------------------------------------------------------- #

def _latest(rows) -> dict:
    by = {}
    for r in rows:
        if r.get("preflight") or r.get("seeded") or r.get("sweep"):
            continue
        by.setdefault(r["date"], {})[r["arm"]] = r
    return by


def _pm_status(row) -> "str | None":
    pm = (row or {}).get("premove_context") or {}
    return (pm.get("context") or {}).get("status")


def _tgt(row) -> str:
    out = []
    for t in (row or {}).get("targets") or ():
        lvl = t.get("level")
        px = t.get("target")
        out.append(f"{lvl}@{px:.2f}" if isinstance(px, (int, float)) else f"{lvl}@-")
    return ";".join(out) or "-"


def summarize(out_path) -> int:
    by = _latest(_load_jsonl(out_path))
    if not by:
        print(f"[ab] nothing in {out_path}")
        return 1
    hdr = (f"{'date':<11} {'ctx':<9} {'off':<9} {'study':<9} | "
           f"{'A src':<17} {'A':<4} {'n':>2} {'pts':>8} | {'B src':<17} {'B':<4} {'n':>2} "
           f"{'pts':>8} | {'T pts':>8} {'mid':<4} | {'D pts':>8} {'Dmid':<4} | {'B-A':>8}")
    print("A = today   B = direction only (what 'on' does)   T = B + leg-mid target   "
          "D = T with the operator's leg start")
    print(hdr)
    print("-" * len(hdr))
    tot = {"A": 0.0, "B": 0.0, "T": 0.0, "D": 0.0}
    split = {"same_dir": 0.0, "dir_changed": 0.0, "unrelated": 0.0}
    bad_controls, parity, no_t = [], [], []
    for d in sorted(by):
        arms = by[d]
        a, b, t, dd = arms.get("A"), arms.get("B"), arms.get("T"), arms.get("D")
        if a is None or b is None or "error" in (a.get("status"), b.get("status")):
            print(f"{d:<11} INCOMPLETE/ERROR: "
                  + "; ".join(f"{k}:{v.get('error')}" for k, v in arms.items()
                              if v.get("status") == "error")
                  + ("" if b is not None else " (no arm B row)"))
            continue
        if t is not None and t.get("status") == "error":
            t = None
        if dd is not None and dd.get("status") == "error":
            print(f"{d:<11} arm D ERROR ({dd.get('error')}) -- D scored as T")
            dd = None
        if t is None:
            no_t.append(d)
        ctx = _pm_status(b) or "-"
        tt = t or b                      # no T row: the target arm was not run, score as B
        dpts = (dd or tt)["total_pts"]
        tot["A"] += a["total_pts"]
        tot["B"] += b["total_pts"]
        tot["T"] += tt["total_pts"]
        tot["D"] += dpts
        delta = b["total_pts"] - a["total_pts"]
        if b.get("thesis_source") == "premove_unrelated":
            split["unrelated"] += delta
            same_dir = (a.get("thesis_source") == "stretch_override"
                        and a.get("direction") == b.get("direction"))
            split["same_dir" if same_dir else "dir_changed"] += delta
        # Controls: every non-big control, and every PART day where arm 1 did not fire
        # in A (on PART the path suppresses arm 1, so an arm-1 PART day may differ).
        is_ctrl = b.get("control") or (ctx == "PART"
                                       and a.get("thesis_source") != "stretch_override")
        if is_ctrl:
            if b.get("sha") != a.get("sha"):
                bad_controls.append(d)
        if ctx in ("UNRELATED", "PART", "NOT_BIG") and ctx != b.get("offline_status"):
            parity.append((d, ctx, b.get("offline_status")))
        if b.get("offline_status") != b.get("study_status"):
            parity.append((d, f"era {b.get('offline_status')}",
                           f"2026-12 {b.get('study_status')}"))
        print(f"{d:<11} {ctx:<9} {str(b.get('offline_status')):<9} "
              f"{str(b.get('study_status')):<9} | "
              f"{str(a.get('thesis_source')):<17} {str(a.get('direction')):<4} "
              f"{a['n_trades']:>2} {a['total_pts']:>+8.2f} | "
              f"{str(b.get('thesis_source')):<17} {str(b.get('direction')):<4} "
              f"{b['n_trades']:>2} {b['total_pts']:>+8.2f} | "
              f"{tt['total_pts']:>+8.2f} {'Y' if tt.get('mid_hit') else '-':<4} | "
              f"{dpts:>+8.2f} {('Y' if (dd or tt).get('mid_hit') else '-'):<4} | {delta:>+8.2f}")
        for arm, r in (("A", a), ("B", b), ("T", t), ("D", dd)):
            if r is not None and r.get("targets"):
                print(f"{'':<11}   {arm} targets: {_tgt(r)}  first entry {r.get('first_entry')}")
    print("-" * len(hdr))
    print(f"TOTAL  A {tot['A']:+.2f}  B {tot['B']:+.2f}  T {tot['T']:+.2f}  D {tot['D']:+.2f}")
    print(f"  direction only  B-A {tot['B'] - tot['A']:+.2f}   "
          f"adding the mid target  T-B {tot['T'] - tot['B']:+.2f}   "
          f"operator leg start  D-T {tot['D'] - tot['T']:+.2f}")
    print(f"  UNRELATED-only B-A {split['unrelated']:+.2f}  (arm 1 already forced the same "
          f"direction {split['same_dir']:+.2f}, direction changed {split['dir_changed']:+.2f})")
    if no_t:
        print(f"  no arm T row (scored as B): {','.join(no_t)}")
    print(f"  controls/PART byte-identical A vs B: "
          f"{'YES' if not bad_controls else 'NO -> ' + ','.join(bad_controls)}")
    befores = {d: arms["A"].get("before_identical") for d, arms in by.items()
               if "A" in arms and arms["A"].get("before_identical") is not None}
    print(f"  A-after == BEFORE (Wave 0 capture): "
          f"{sum(1 for v in befores.values() if v)}/{len(befores)} dates"
          + ("" if all(befores.values()) else
             " -> DIFFER: " + ",".join(d for d, v in befores.items() if not v)))
    print(f"  classifier parity notes: {parity or 'none'}")
    return 0 if not bad_controls and all(befores.values()) else 3


# --------------------------------------------------------------------------- #
# offline sweep (no replays)                                                    #
# --------------------------------------------------------------------------- #

#: Copied from `<global>/studies/premove_mid_retrace/smt_cont.py::resolve` (2026-09-30),
#: the labelling page's rule; PROVENANCE: that file, unchanged but for this comment.
def resolve(sess, T, up, A, E, size, sc):
    Ex = E
    for h, l in zip(sess.loc[T(9, 20):T(9, 29), "high"], sess.loc[T(9, 20):T(9, 29), "low"]):
        if (l <= (A + Ex) / 2) if up else (h >= (A + Ex) / 2):
            return "PRE"
        Ex = max(Ex, h) if up else min(Ex, l)
    cB = None
    post = sess.loc[T(9, 30):T(12, 30)]
    for h, l in zip(post["high"].values, post["low"].values):
        xp, cp = (h, l) if up else (l, h)
        if (xp > Ex) if up else (xp < Ex):
            if cB is not None and abs(Ex - cB) >= 80 * sc:
                return "STOPPED"
            Ex, cB = xp, None
            if abs(Ex - E) >= 0.5 * size:
                return "CONT"
            continue
        cB = cp if cB is None else (min(cB, cp) if up else max(cB, cp))
        if (cB <= (A + Ex) / 2) if up else (cB >= (A + Ex) / 2):
            return "MID"
    return "STOPPED" if cB is not None and abs(Ex - cB) >= 80 * sc else "NONE"


def wilson(k, n):
    """Copied from the study's `analyze.wilson`."""
    if n == 0:
        return (np.nan, np.nan)
    p, z = k / n, 1.96
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (100 * (c - h), 100 * (c + h))


STUDY_EXCLUDE = {"2026-08-12", "2026-09-10", "2026-07-03"}   # smt_cont.EXCLUDE
RECENT = "2025-09-22"                                         # smt_cont.RECENT


def _reached(df_low, D, c) -> "tuple[str, bool] | None":
    """(auto outcome, reached) for context `c`: reached = outcome MID or a counter-move
    beyond the static mid by 12:30 (`past_mid`) — context.md's column."""
    T = lambda hh, mm: D + pd.Timedelta(hours=hh, minutes=mm)
    sess = df_low.loc[D - pd.Timedelta(hours=6):T(12, 30)]
    if T(9, 19) not in sess.index or T(12, 30) not in sess.index:
        return None
    up = c.leg_direction == "UP"
    A, E, size = c.origin, c.extreme, c.size
    out = resolve(sess, T, up, A, E, size, c.px_0919 / 29000.0)
    post = sess.loc[T(9, 30):T(12, 30)]
    mid = (A + E) / 2
    past = bool(post["low"].min() < mid) if up else bool(post["high"].max() > mid)
    return out, (out == "MID" or past)


def offline_sweep() -> int:
    from agent.trader import premove_context as pc
    main = _study_dir_1m()
    df = _mnq_1m(main)
    low = df.rename(columns=str.lower)
    low = low[~low.index.duplicated(keep="last")].sort_index()[["open", "high", "low", "close"]]
    days = os.path.join(_global_root(), "studies", "premove_last_moment_direction", "days.csv")
    dates = list(pd.read_csv(days)["date"]) + ["2026-09-22", "2026-09-23", "2026-09-24"]
    study = pd.read_csv(os.path.join(_study_dir(), "context.csv")).set_index("date")
    rows = []
    for ds in dates:
        D = pd.Timestamp(ds).tz_localize(TZ)
        c = pc.classify(df, D + pd.Timedelta(hours=9, minutes=20))
        if c.status not in (pc.UNRELATED, pc.PART, pc.UNKNOWN) or c.size is None:
            continue
        if c.size_pct is None or c.size_pct < pc.DEFAULT_PARAMS.big_pct:
            continue
        got = _reached(low, D, c)
        if got is None:
            continue
        out, reached = got
        srow = study.loc[ds] if ds in study.index else None
        rows.append(dict(date=ds, status=c.status, dir=c.leg_direction, size=c.size,
                         out=out, reached=reached, excl=ds in STUDY_EXCLUDE,
                         period="recent" if ds >= RECENT else "older",
                         study_ctx=None if srow is None else srow["ctx"]))
    r = pd.DataFrame(rows)
    q = r[~r.excl]
    print("# offline sweep: production classifier (defaults) on main/2026-12, auto outcomes")
    print("| context | period | n | counter reached mid |")
    print("|---|---|---|---|")
    for ctx, sel in (("PART", q.status == "PART"),
                     ("UNRELATED (incl. study COUNTER)", q.status == "UNRELATED"),
                     ("  of which study COUNTER", q.study_ctx == "COUNTER"),
                     ("UNRELATED excl. study COUNTER",
                      (q.status == "UNRELATED") & (q.study_ctx != "COUNTER")),
                     ("UNKNOWN", q.status == "UNKNOWN")):
        for per in ("recent", "older", "all"):
            s = q[sel] if per == "all" else q[sel & (q.period == per)]
            if not len(s):
                continue
            k = int(s.reached.sum())
            lo, hi = wilson(k, len(s))
            print(f"| {ctx} | {per} | {len(s)} | {100 * k / len(s):.0f}% [{lo:.0f}-{hi:.0f}] |")
    mism = r[r.study_ctx.map(lambda x: {"COUNTER": "UNRELATED", "?": "UNKNOWN"}.get(x, x))
             != r.status]
    print(f"\nper-day parity vs context.csv: {len(r)} rows, {len(mism)} mismatches"
          + ("" if mism.empty else "\n" + mism.to_string(index=False)))

    # Operator labels: mid-reach with the auto origin vs the operator's leg start.
    print("\n# operator-labelled days (all labels): auto origin vs operator origin")
    print(f"{'date':<11} {'label':<9} {'start':<6} | {'auto':<9} {'size':>7} {'reach':<5} | "
          f"{'op':<9} {'size':>7} {'reach':<5}")
    agg = {"auto": [0, 0], "op": [0, 0]}
    for ds, lab in sorted(operator_labels().items()):
        D = pd.Timestamp(ds).tz_localize(TZ)
        ca = pc.classify(df, D + pd.Timedelta(hours=9, minutes=20))
        start = lab.get("leg_start")
        saved = pc.LEG_START_OVERRIDES
        try:
            pc.LEG_START_OVERRIDES = {ds: start} if start else {}
            co = pc.classify(df, D + pd.Timedelta(hours=9, minutes=20))
        finally:
            pc.LEG_START_OVERRIDES = saved
        cells = []
        for key, c in (("auto", ca), ("op", co)):
            got = _reached(low, D, c) if c.size else None
            reach = None if got is None else got[1]
            if reach is not None and c.status == pc.UNRELATED:
                agg[key][0] += int(reach)
                agg[key][1] += 1
            cells.append((c.status, c.size, reach))
        (sa, za, ra), (so_, zo, ro) = cells
        print(f"{ds:<11} {str(lab.get('outcome')):<9} {str(start or '-'):<6} | {sa:<9} "
              f"{(za or 0):>7.2f} {str(ra):<5} | {so_:<9} {(zo or 0):>7.2f} {str(ro):<5}")
    for key in ("auto", "op"):
        k, n = agg[key]
        print(f"UNRELATED labelled days, {key} origin: mid reached {k}/{n}")
    return 0


# --------------------------------------------------------------------------- #
# replay sweep (descriptive only)                                               #
# --------------------------------------------------------------------------- #

def replay_sweep(dates, out_path) -> int:
    """Per cell, each date's decision key is (status, A, E) for UNRELATED, "PART", or
    "baseline"; only keys not already in `out_path` are replayed (arm B with that cell's
    params), and each cell's P&L is assembled from the per-key runs. DESCRIPTIVE ONLY."""
    from agent.trader import premove_context as pc
    rows = _load_jsonl(out_path)
    base = {r["date"]: r for r in rows if r.get("arm") == "A" and r.get("status") != "error"
            and not r.get("preflight") and not r.get("seeded") and not r.get("sweep")}
    runs = {(r["date"], r["key"]): r for r in rows if r.get("sweep")}
    keys = list(SWEEP_GRID)
    cells = [dict(zip(keys, vals)) for vals in itertools.product(*SWEEP_GRID.values())]
    table = []
    with open(out_path, "a", encoding="utf-8") as fh:
        for cell in cells:
            params = dataclasses.replace(pc.DEFAULT_PARAMS, **cell)
            tot_a = tot_b = 0.0
            n_unrel = 0
            for d in dates:
                if d not in base:
                    continue
                c = offline_context(d, params=params)
                if c.status == pc.UNRELATED:
                    key = f"U|{c.origin}|{c.extreme}"
                    n_unrel += 1
                elif c.status == pc.PART:
                    key = "PART"
                else:
                    key = "baseline"
                if key == "baseline":
                    pts = base[d]["total_pts"]
                else:
                    if (d, key) not in runs:
                        try:
                            row = run_arm(d, "B", mode="on", params=params)
                        except Exception as exc:
                            row = {"date": d, "status": "error", "error": str(exc)}
                        row.update({"sweep": True, "key": key, "cell": cell})
                        fh.write(json.dumps(row, default=str) + "\n")
                        fh.flush()
                        runs[(d, key)] = row
                    if runs[(d, key)].get("status") == "error":
                        continue                      # incomplete: left out of BOTH sums
                    pts = runs[(d, key)]["total_pts"]
                tot_a += base[d]["total_pts"]
                tot_b += pts
            table.append((cell, n_unrel, tot_a, tot_b))
    print("cut  big%  k    mult | nU |   sumA    sumB    B-A")
    for cell, n, a, b in table:
        print(f"{cell['cut_pts_at_ref']:>4.0f} {cell['big_pct']:>5.2f} {cell['part_k']:>4.1f} "
              f"{cell['part_mult']:>5.2f} | {n:>2} | {a:>+7.2f} {b:>+7.2f} {b - a:>+7.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dates", help="comma-separated YYYY-MM-DD")
    ap.add_argument("--big-days", action="store_true",
                    help="plan 46's replayable big days + any big day the era scan finds")
    ap.add_argument("--controls", action="store_true", help="the non-big control dates")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--regression-dir", default=None, help="sets ACT_REGRESSION_DIR")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--seed-missing", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--arm-c", action="store_true")
    ap.add_argument("--arm-d", action="store_true")
    ap.add_argument("--arm-t", action="store_true",
                    help="direction + leg-mid target (MID_TARGET_ENABLED on)")
    ap.add_argument("--summarize", action="store_true")
    ap.add_argument("--offline-sweep", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()
    if args.regression_dir:
        os.environ["ACT_REGRESSION_DIR"] = args.regression_dir
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    if args.summarize:
        return summarize(args.out)
    if args.offline_sweep:
        return offline_sweep()
    dates = _dates(args)
    if args.seed_missing:
        if not dates:
            need = sorted({r["date"] for r in _load_jsonl(args.out)
                           if r.get("status") == "needs_seed"})
            done = {r["date"] for r in _load_jsonl(args.out) if r.get("seeded")
                    and r.get("status") != "error"}
            dates = [d for d in need if d not in done]
        return seed_missing(dates, args.yes, args.out)
    if not dates:
        ap.error("give --dates, --big-days and/or --controls")
    if args.preflight:
        return preflight(dates, args.out)
    if args.sweep:
        return replay_sweep(dates, args.out)
    return run_ab(dates, args.out, arm_c=args.arm_c, arm_d=args.arm_d, resume=args.resume,
                  arm_t=args.arm_t)


if __name__ == "__main__":
    raise SystemExit(main())
