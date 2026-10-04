"""A/B two arms of the trader replay over dates, scored by the operator's criteria.

    uv run python scripts/ab_replay.py --dates 2026-10-01,2026-08-27 --arm ACT_SMT_WAIT_BLOCK=0
    uv run python scripts/ab_replay.py --dates 2026-10-01 --arm agent.trader.episode.FAVOURABLE_CLOSE_GATE=False
    uv run python scripts/ab_replay.py --dates 2026-10-01 --arm ACT_EXTENSION_VETO=0 --label no-veto --skip-a

Arm A is this worktree's code as it stands (no overrides); arm B applies every `--arm`:
`NAME=value` sets an environment variable for the replay process, `module.ATTR=value`
sets a module attribute inside it (the value is a Python literal). Both arms inject the
same thesis — the corpus manifest's (`scripts/corpus_manifest.py`; missing dates are
added to it first) — so they differ only in the code path under test. Never a model call.

Per date, the report compares the two arms by the ENTRY criterion (sum of entry MFE,
stops survived), the TARGET criterion (sum of ideal-entry points) and whether the entry
set changed; the session P&L comes last. Every run is appended to a JSONL under
`<global>/studies/ab_replay/<label>.jsonl` (`--out` overrides).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths  # noqa: E402
from agent.study import corpus  # noqa: E402

_WRAPPER = r"""
import ast, importlib, runpy, sys
knobs = ast.literal_eval(sys.argv[1]); argv = sys.argv[2:]
for dotted, raw in knobs:
    mod, attr = dotted.rsplit(".", 1)
    setattr(importlib.import_module(mod), attr, ast.literal_eval(raw))
sys.argv = ["scripts/replay_session.py"] + argv
runpy.run_path("scripts/replay_session.py", run_name="__main__")
"""


def parse_arms(specs) -> "tuple[dict, list]":
    env, knobs = {}, []
    for spec in specs or []:
        if "=" not in spec:
            raise SystemExit(f"--arm needs NAME=value or module.ATTR=value, got {spec!r}")
        name, value = spec.split("=", 1)
        (knobs.append((name, value)) if "." in name else env.__setitem__(name, value))
    return env, knobs


def run_arm(date: str, thesis_file: str, env_over: dict, knobs: list) -> "tuple[str | None, str]":
    """One replay; returns (run_dir, output tail)."""
    env = dict(os.environ)
    env.update(env_over)
    cmd = [sys.executable, "-c", _WRAPPER, repr(knobs), "--dates", date,
           "--thesis-file", thesis_file]
    p = subprocess.run(cmd, cwd=str(corpus.repo_root()), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"\[replay\] \S+ done -> (.+)", out)
    return (m.group(1).strip() if m else None), out[-2000:]


def _row(date, arm, scored, run_dir, error=None) -> dict:
    row = {"date": date, "arm": arm, "run_dir": run_dir, "error": error}
    if scored:
        row.update({k: scored[k] for k in ("total_pts", "realised_pts", "marked_pts", "vetoes",
                                            "plan_dead", "n_trades", "entry_mfe_sum",
                                            "stops_survived", "ideal_points_sum", "retrace")})
        row["trades"] = scored["trades"]
    return row


def render(date: str, a: "dict | None", b: dict) -> str:
    def line(name, r):
        if r is None:
            return f"  {name}: (not run)"
        if r.get("error"):
            return f"  {name}: ERROR {r['error'][-300:]}"
        return (f"  {name}: entries {r['n_trades']} | ENTRY MFE sum {r['entry_mfe_sum']:+.2f}, "
                f"stops survived {r['stops_survived']}/{r['n_trades']} | TARGET ideal-entry pts "
                f"{r['ideal_points_sum']:+.2f} | P&L {r['total_pts']:+.2f} | vetoes {r['vetoes'] or '{}'}")
    out = [f"=== {date}", line("A", a), line("B", b)]
    if a and b and not a.get("error") and not b.get("error"):
        ka = [(t["entry_ts"], t["mechanism"], t["entry"], t["exit_kind"], t["exit"]) for t in a["trades"]]
        kb = [(t["entry_ts"], t["mechanism"], t["entry"], t["exit_kind"], t["exit"]) for t in b["trades"]]
        if ka == kb:
            out.append("  entries and exits identical")
        else:
            for tag, keys, other in (("A only", ka, kb), ("B only", kb, ka)):
                for k in keys:
                    if k not in other:
                        out.append(f"  {tag}: {str(k[0])[11:19]} {k[1]} @ {k[2]} -> {k[3]} @ {k[4]}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dates", required=True, help="comma-separated YYYY-MM-DD")
    ap.add_argument("--arm", action="append", default=[],
                    help="arm B override, NAME=value (env) or module.ATTR=value (knob); repeatable")
    ap.add_argument("--label", default=None, help="name of the JSONL (default: from the arms)")
    ap.add_argument("--skip-a", action="store_true", help="run arm B only")
    ap.add_argument("--out", default=None, help="JSONL path (default: <global>/studies/ab_replay/<label>.jsonl)")
    ap.add_argument("--retrace-pts", type=float, default=corpus.DEFAULT_RETRACE_PTS)
    ap.add_argument("--retrace-fraction", type=float, default=corpus.DEFAULT_RETRACE_FRACTION)
    args = ap.parse_args()

    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    env_over, knobs = parse_arms(args.arm)
    label = args.label or re.sub(r"[^A-Za-z0-9_.=-]+", "_", "+".join(args.arm) or "noop")
    out_path = Path(args.out) if args.out else paths.global_root() / "studies" / "ab_replay" / f"{label}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = corpus.load_manifest() if corpus.manifest_path().exists() else {"dates": {}}
    missing = [d for d in dates if not (manifest["dates"].get(d) or {}).get("thesis_file")]
    if missing:
        manifest = corpus.build_manifest(missing)
    started = _dt.datetime.now().isoformat(timespec="seconds")
    print(f"[ab] arms: A = no overrides; B = env {env_over or '{}'} knobs {knobs or '[]'}; "
          f"log {out_path}")
    for date in dates:
        tf = (manifest["dates"].get(date) or {}).get("thesis_file")
        if not tf:
            errs = (manifest["dates"].get(date) or {}).get("thesis_errors") or ["no replay run with a thesis"]
            print(f"=== {date}\n  skipped: {'; '.join(errs)}")
            continue
        results = {}
        for arm, (e, k) in (("A", ({}, [])), ("B", (env_over, knobs))):
            if arm == "A" and args.skip_a:
                results["A"] = None
                continue
            run_dir, tail = run_arm(date, tf, e, k)
            scored, err = None, None
            if run_dir is None:
                err = tail
            else:
                try:
                    scored = corpus.score_run(run_dir, date, retrace_pts=args.retrace_pts,
                                              retrace_fraction=args.retrace_fraction)
                except Exception as exc:    # the run exists; the score is what failed
                    err = f"score_run: {type(exc).__name__}: {exc}"
            row = _row(date, arm, scored, run_dir, err)
            row.update({"started": started, "label": label, "overrides": {"env": e, "knobs": k}})
            with open(out_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
            results[arm] = row
        print(render(date, results.get("A"), results["B"]), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
