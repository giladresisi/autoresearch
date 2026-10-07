"""The replay corpus: which dates can be replayed, with which thesis, on which contract
folder — and the operator's two success criteria scored on a run folder.

Shared by `scripts/corpus_manifest.py`, `scripts/score_run.py` and `scripts/ab_replay.py`
so that no study re-derives the contract folder, re-patches a code-built thesis or
re-implements the scoring. Read-only on every input; writes only under
`<global>/studies/corpus/`.

The criteria (`~/.claude/CLAUDE.md`, "Project: auto-co-trader", 2026-10-02):
- ENTRY ideas are judged by each entry's MFE: how far price ran from the entry, assuming
  the stop its mechanism places, up to an ideal exit before a meaningful retrace.
- TARGET / position-management ideas are judged by points gained from an IDEAL entry:
  the post-09:30 farthest price against the trade within the leg that leads to the exit,
  re-anchored after every meaningful opposite retrace.
"Meaningful retrace" = 40 pts, flat, no scaling with the leg (operator, 2026-10-04).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
from pathlib import Path

import pandas as pd

import paths
from agent.trader.fixed_backend import validate_oracle_thesis

ET = "America/New_York"
RUN_FILES = ("plans.json", "trader_decisions.jsonl")
THESIS_FILE = "thesis_state.json"
RTH_OPEN = (9, 30)
WINDOW_END = (13, 0)

DEFAULT_RETRACE_PTS = 40.0          # operator, 2026-10-04: a flat floor
DEFAULT_RETRACE_FRACTION = 0.0      # no scaling with the leg (operator, 2026-10-04)

# The stop each mechanism places, as a cap from the entry (§9), used for the entry
# criterion when the trade did not stop out (a stop-out's own price is exact).
STOP_CAP_BY_MECHANISM = {
    "fvg_1m_post_extreme": 30.0, "extreme_reject_close": 15.0, "tmso_reject": 15.0,
    "micro_smt_reject": 15.0, "fvg_1h_reject": 25.0, "nym_mid_reject": 20.0,
}
DEFAULT_STOP_CAP = 25.0            # resting 5m entries (§9, `executor.STOP_CAP_PTS`)
# How far back a code-built thesis's placeholder falsifier sits, against the bias.
PATCH_FALSIFIER_PTS = 1500.0

_LONG = ("UP", "LONG")
_frames: dict = {}


# -- paths ------------------------------------------------------------------ #

def corpus_dir() -> Path:
    return paths.global_root() / "studies" / "corpus"


def manifest_path() -> Path:
    return corpus_dir() / "manifest.json"


def theses_dir() -> Path:
    return corpus_dir() / "theses"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# -- contract folders ------------------------------------------------------- #

def rollover_ledger() -> list:
    p = paths.general_main_dir() / "rollover_ledger.json"
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def contract_folder(date: str, ledger: "list | None" = None) -> str:
    """The `general/main/<subfolder>` a replay of `date` reads: the ledger entry with the
    latest `prep_date` at or before the date."""
    rows = [(str(e.get("prep_date") or ""), str(e.get("subfolder") or ""))
            for e in (ledger if ledger is not None else rollover_ledger())]
    rows = [r for r in rows if r[1] and r[0] <= date]
    if not rows:
        raise ValueError(f"no rollover ledger entry covers {date}")
    return max(rows)[1]


def load_1s(folder: str, sym: str) -> pd.DataFrame:
    """The whole 1s frame of one contract folder, ET-indexed, cached per process."""
    key = (folder, sym)
    if key not in _frames:
        df = pd.read_parquet(paths.general_main_dir() / folder / f"{sym}_1s.parquet",
                             columns=["Open", "High", "Low", "Close"])
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        _frames[key] = df.tz_convert(ET)
    return _frames[key]


def day_bars(date: str, sym: str = "MNQ") -> pd.DataFrame:
    """One session date's bars, 09:30 -> window end, on the right contract folder."""
    f = load_1s(contract_folder(date), sym)
    day = pd.Timestamp(date, tz=ET)
    t0 = day + pd.Timedelta(hours=RTH_OPEN[0], minutes=RTH_OPEN[1])
    t1 = day + pd.Timedelta(hours=WINDOW_END[0], minutes=WINDOW_END[1])
    return f[(f.index >= t0) & (f.index <= t1)]


# -- run folders ------------------------------------------------------------ #

def worktrees() -> list:
    """Every worktree of this repo (the current one included)."""
    try:
        out = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=str(repo_root()),
                             capture_output=True, text=True, check=True).stdout
    except Exception:
        return [repo_root()]
    roots = [Path(l[len("worktree "):].strip()) for l in out.splitlines()
             if l.startswith("worktree ")]
    return roots or [repo_root()]


def find_runs(dates=None, roots=None) -> dict:
    """{date: [{"run", "worktree", "mtime", "has_thesis"}, ...]} newest first."""
    want = set(dates) if dates else None
    found: dict = {}
    for root in (roots or worktrees()):
        base = Path(root) / "regression" / "sessions"
        if not base.is_dir():
            continue
        for dd in base.iterdir():
            if not dd.is_dir() or (want and dd.name not in want):
                continue
            for run in dd.iterdir():
                if not all((run / n).exists() for n in RUN_FILES):
                    continue
                found.setdefault(dd.name, []).append({
                    "run": str(run), "worktree": str(root),
                    "mtime": (run / "trader_decisions.jsonl").stat().st_mtime,
                    "has_thesis": (run / THESIS_FILE).exists()})
    for d in found:
        found[d].sort(key=lambda r: r["mtime"], reverse=True)
    return found


def plan_direction(run_dir) -> "str | None":
    try:
        with open(Path(run_dir) / "plans.json", encoding="utf-8") as fh:
            plans = json.load(fh)
        for v in plans.values():
            if isinstance(v, dict) and v.get("direction"):
                return str(v["direction"]).upper()
    except Exception:
        return None
    return None


def oracle_thesis(run_dir) -> "tuple[dict | None, list, list]":
    """(thesis usable by `--thesis-file`, fields patched, validator errors left).

    A code-built thesis (the stretch / UNRELATED overrides) carries no regime, confidence
    or falsifier, and the oracle validator refuses all three. They are filled with
    neutral values and a far placeholder falsifier: plans no longer die on
    falsification, and both arms of an A/B get the same thesis.
    """
    p = Path(run_dir) / THESIS_FILE
    if not p.exists():
        return None, [], ["no thesis_state.json"]
    with open(p, encoding="utf-8") as fh:
        thesis = dict((json.load(fh) or {}).get("thesis") or {})
    patched = []
    if "exhausted_if" in thesis:          # retired 2026-08-29; the validator refuses it
        thesis.pop("exhausted_if")
        patched.append("exhausted_if")
    if str(thesis.get("regime") or "").upper() not in ("TREND", "RANGE", "HYBRID"):
        thesis["regime"] = "TREND"
        patched.append("regime")
    if str(thesis.get("confidence") or "").upper() not in ("HIGH", "MEDIUM", "LOW"):
        thesis["confidence"] = "MEDIUM"
        patched.append("confidence")
    dol = (thesis.get("dol") or {}).get("price")
    if not thesis.get("falsified_if") and dol is not None:
        up = str(thesis.get("bias") or "").upper() in _LONG
        thesis["falsified_if"] = [{"type": "price_beyond",
                                   "price": float(dol) - PATCH_FALSIFIER_PTS if up
                                   else float(dol) + PATCH_FALSIFIER_PTS,
                                   "side": "below" if up else "above"}]
        patched.append("falsified_if")
    return thesis, patched, validate_oracle_thesis(thesis)


# -- the manifest ----------------------------------------------------------- #

def build_manifest(dates=None, roots=None) -> dict:
    """Write `<global>/studies/corpus/manifest.json` and one thesis file per date.

    One row per date that has a replay run folder with a thesis. `latest_run` is the
    newest run across the worktrees — runs come from different code versions, so its
    entries are indicative, never exact; `n_runs` and `worktree` say where it came from.
    Re-running merges into the existing manifest, so a partial `dates=` refresh keeps the
    other rows.
    """
    ledger = rollover_ledger()
    manifest = load_manifest() if manifest_path().exists() else {"dates": {}}
    rows = manifest.setdefault("dates", {})
    theses_dir().mkdir(parents=True, exist_ok=True)
    for date, runs in sorted(find_runs(dates, roots).items()):
        with_thesis = [r for r in runs if r["has_thesis"]]
        src = with_thesis[0] if with_thesis else runs[0]
        thesis, patched, errs = oracle_thesis(src["run"])
        row = {
            "contract_folder": contract_folder(date, ledger),
            "direction": plan_direction(src["run"]),
            "latest_run": src["run"], "worktree": src["worktree"], "n_runs": len(runs),
            "run_mtime": _dt.datetime.fromtimestamp(src["mtime"]).isoformat(timespec="seconds"),
            "thesis_file": None, "thesis_patched": patched, "thesis_errors": errs,
            "thesis_source": (thesis or {}).get("thesis_source"),
        }
        if thesis is not None and not errs:
            tf = theses_dir() / f"{date}.json"
            with open(tf, "w", encoding="utf-8") as fh:
                json.dump(thesis, fh, indent=1)
            row["thesis_file"] = str(tf)
        rows[date] = row
    manifest["built_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    manifest["ledger"] = [{"prep_date": e.get("prep_date"), "subfolder": e.get("subfolder")}
                          for e in ledger]
    corpus_dir().mkdir(parents=True, exist_ok=True)
    with open(manifest_path(), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    return manifest


def load_manifest() -> dict:
    with open(manifest_path(), encoding="utf-8") as fh:
        return json.load(fh)


def replayable_dates(manifest=None) -> list:
    m = manifest if manifest is not None else load_manifest()
    return sorted(d for d, r in m.get("dates", {}).items() if r.get("thesis_file"))


# -- the criteria ----------------------------------------------------------- #

def retrace_threshold(leg_pts: float, pts=DEFAULT_RETRACE_PTS,
                      fraction=DEFAULT_RETRACE_FRACTION) -> float:
    return max(float(pts), float(fraction) * max(float(leg_pts), 0.0))


def score_entry(bars: pd.DataFrame, entry_ts, entry: float, direction: str,
                stop_pts: float, *, retrace_pts=DEFAULT_RETRACE_PTS,
                retrace_fraction=DEFAULT_RETRACE_FRACTION) -> dict:
    """The ENTRY criterion for one entry.

    From the entry forward, per 1s bar: the favourable extreme runs until the first of
    (a) the stop being hit — adverse excursion from the entry >= `stop_pts`, (b) a
    meaningful retrace — price giving back `retrace_threshold(mfe so far)` from the
    favourable extreme, (c) the window end. `mfe` is the favourable extreme at that
    point: what an ideal exit before the retrace could have taken. `stop_survived` is
    False when (a) came first.
    """
    short = str(direction or "").upper() not in _LONG
    sign = -1.0 if short else 1.0
    seg = bars[bars.index >= pd.Timestamp(entry_ts)]
    mfe, mfe_ts, ended_by, ended_at = 0.0, None, "window", None
    for ts, row in seg.iterrows():
        adverse = (float(row["High"]) - entry) if short else (entry - float(row["Low"]))
        fav = (entry - float(row["Low"])) if short else (float(row["High"]) - entry)
        if adverse >= stop_pts:
            # Conservative within the 1s bar: the stop counts before the bar's own
            # favourable print.
            ended_by, ended_at = "stop", ts
            break
        if fav > mfe:
            mfe, mfe_ts = fav, ts
        # A retrace is measured from the favourable extreme, in the trade's own terms.
        close_fav = sign * (float(row["Close"]) - entry)
        if mfe > 0 and (mfe - close_fav) >= retrace_threshold(mfe, retrace_pts, retrace_fraction):
            ended_by, ended_at = "retrace", ts
            break
    return {"mfe": round(mfe, 2), "mfe_ts": str(mfe_ts) if mfe_ts is not None else None,
            "ended_by": ended_by, "ended_at": str(ended_at) if ended_at is not None else None,
            "stop_pts": float(stop_pts), "stop_survived": ended_by != "stop",
            "r_multiple": round(mfe / stop_pts, 2) if stop_pts else None}


def ideal_entry(bars: pd.DataFrame, direction: str, exit_ts, *,
                retrace_pts=DEFAULT_RETRACE_PTS,
                retrace_fraction=DEFAULT_RETRACE_FRACTION) -> dict:
    """The TARGET criterion's anchor: a PROXY for the ideal entry of an exit at `exit_ts`.

    The operator has no rule for the ideal entry (2026-10-04: "that's why it's ideal"),
    so this is an approximation to be read, not trusted: the farthest price against the
    trade within the leg that leads to the exit, re-anchored on a retrace of
    `retrace_pts`. On 09-08 the operator placed it at 09:31:00 where this gives the
    09:39:15 retrace high; on 10-01 and 08-27 the two agree. Every score prints the
    anchor's time so a reader can override it by eye.

    Walk from 09:30 to the exit, segmenting into legs in the trade direction: a leg's
    origin is its adverse extreme (the highest high for a short); the leg runs while
    price makes new favourable extremes; a meaningful retrace against the leg — the
    threshold of the leg's own size, measured from its favourable extreme — ends it, and
    the next leg's origin starts at that retrace's extreme. The ideal entry is the
    origin of the leg that contains the exit.
    """
    short = str(direction or "").upper() not in _LONG
    seg = bars[bars.index <= pd.Timestamp(exit_ts)]
    if not len(seg):
        return {"ideal_entry": None, "ideal_entry_ts": None, "leg_extreme": None}
    adv_col, fav_col = ("High", "Low") if short else ("Low", "High")
    beyond_adv = (lambda x, ref: x > ref) if short else (lambda x, ref: x < ref)
    beyond_fav = (lambda x, ref: x < ref) if short else (lambda x, ref: x > ref)
    origin, origin_ts = float(seg.iloc[0][adv_col]), seg.index[0]
    fav, fav_ts = float(seg.iloc[0][fav_col]), seg.index[0]
    retracing, re_origin, re_origin_ts = False, None, None
    for ts, row in seg.iterrows():
        a, f = float(row[adv_col]), float(row[fav_col])
        if not retracing:
            leg = abs(fav - origin)
            if leg < retrace_pts and beyond_adv(a, origin):
                # No leg of meaningful size yet: the origin keeps extending to each new
                # adverse extreme, and the leg restarts from there.
                origin, origin_ts, fav, fav_ts = a, ts, f, ts
            if beyond_fav(f, fav):
                fav, fav_ts = f, ts
            leg = abs(fav - origin)
            pulled = abs(a - fav) if beyond_adv(a, fav) else 0.0
            if leg >= retrace_pts and pulled >= retrace_threshold(leg, retrace_pts, retrace_fraction):
                retracing, re_origin, re_origin_ts = True, a, ts
        else:
            # Inside the retrace its extreme is the next leg's origin; the next leg has
            # begun once price makes a new favourable extreme beyond the last leg's.
            # Until then an exit here is anchored to the retrace's extreme.
            if beyond_adv(a, re_origin):
                re_origin, re_origin_ts = a, ts
            if beyond_fav(f, fav):
                origin, origin_ts, fav, fav_ts, retracing = re_origin, re_origin_ts, f, ts, False
    anchor, anchor_ts = (re_origin, re_origin_ts) if retracing else (origin, origin_ts)
    # The stop the ideal entry would have needed: its adverse excursion up to the exit
    # (operator, 2026-10-04: 08-27's ideal 10:01:00 short "with a > 15 pts s/l").
    held = seg[seg.index >= anchor_ts]
    mae = (float(held["High"].max()) - anchor) if short else (anchor - float(held["Low"].min()))
    return {"ideal_entry": anchor, "ideal_entry_ts": str(anchor_ts),
            "ideal_stop_needed": round(max(mae, 0.0), 2),
            "leg_extreme": fav, "leg_extreme_ts": str(fav_ts)}


def score_run(run_dir, date: "str | None" = None, *, retrace_pts=DEFAULT_RETRACE_PTS,
              retrace_fraction=DEFAULT_RETRACE_FRACTION) -> dict:
    """Both criteria over every trade of a run folder, plus the run's own P&L summary."""
    from scripts.report_replay_pnl import summarize   # scripts/ is a package
    run_dir = str(run_dir)
    summary = summarize(run_dir)
    if date is None:
        date = Path(run_dir).parent.name
    bars = day_bars(date, "MNQ")
    trades = []
    for t in summary["trades"]:
        direction = t.get("direction")
        entry = float(t["entry"])
        # The stop the entry was taken with: on the fill record since 2026-10-03; on
        # older runs the stop-out price when there was one, else the mechanism's cap.
        if t.get("stop") is not None:
            stop_pts = abs(float(t["stop"]) - entry)
        elif t.get("exit_kind") == "stop_out" and t.get("exit") is not None:
            stop_pts = abs(float(t["exit"]) - entry)
        else:
            stop_pts = STOP_CAP_BY_MECHANISM.get(t.get("mechanism"), DEFAULT_STOP_CAP)
        row = {k: t.get(k) for k in ("entry_ts", "entry", "direction", "mechanism", "stop",
                                      "exit_ts", "exit", "exit_kind", "points")}
        row["entry_score"] = score_entry(bars, t["entry_ts"], entry, direction, stop_pts,
                                         retrace_pts=retrace_pts,
                                         retrace_fraction=retrace_fraction)
        if t.get("exit_ts") is not None and t.get("exit") is not None:
            ie = ideal_entry(bars, direction, t["exit_ts"], retrace_pts=retrace_pts,
                             retrace_fraction=retrace_fraction)
            if ie["ideal_entry"] is not None:
                sign = 1.0 if str(direction or "").upper() in _LONG else -1.0
                ie["ideal_points"] = round(sign * (float(t["exit"]) - float(ie["ideal_entry"])), 2)
            row["exit_score"] = ie
        else:
            row["exit_score"] = None
        trades.append(row)
    return {
        "run_dir": run_dir, "date": date, "status": summary["status"],
        "total_pts": summary["total_pts"], "realised_pts": summary["realised_pts"],
        "marked_pts": summary["marked_pts"], "vetoes": summary["vetoes"],
        "plan_dead": summary["plan_dead"], "n_trades": len(trades),
        "entry_mfe_sum": round(sum(r["entry_score"]["mfe"] for r in trades), 2),
        "stops_survived": sum(1 for r in trades if r["entry_score"]["stop_survived"]),
        "ideal_points_sum": round(sum((r["exit_score"] or {}).get("ideal_points") or 0.0
                                      for r in trades), 2),
        "retrace": {"pts": retrace_pts, "fraction": retrace_fraction},
        "trades": trades,
    }


def entry_keys(scored: dict) -> list:
    """What an A/B compares: (time, mechanism, price, exit kind, exit price) per trade."""
    return [(t["entry_ts"], t["mechanism"], t["entry"], t["exit_kind"], t["exit"])
            for t in scored["trades"]]
