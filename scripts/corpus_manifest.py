"""Build or print the replay-corpus manifest (`agent/study/corpus.py`).

    uv run python scripts/corpus_manifest.py                 # (re)build for every date found
    uv run python scripts/corpus_manifest.py --dates 2026-10-01,2026-09-25
    uv run python scripts/corpus_manifest.py --print         # the manifest as a table, no rebuild

One row per date that has a replay run folder (any worktree): the contract folder the
rollover ledger assigns, the plan direction, the newest run and where it came from, and a
thesis file that already passes the oracle validator (code-built theses patched, and the
patch recorded). Every study and A/B starts from this file instead of re-deriving it.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.study import corpus  # noqa: E402


def render(manifest: dict) -> str:
    rows = manifest.get("dates", {})
    out = [f"corpus manifest {corpus.manifest_path()} — built {manifest.get('built_at')} — "
           f"{len(rows)} dates, {len(corpus.replayable_dates(manifest))} replayable",
           f"{'date':10} {'dir':4} {'folder':8} {'runs':>4} {'thesis':8} {'patched':22} worktree of latest run"]
    for d, r in sorted(rows.items()):
        thesis = "ok" if r.get("thesis_file") else ("ERR" if r.get("thesis_errors") else "none")
        out.append(f"{d:10} {str(r.get('direction') or '-'):4} {r.get('contract_folder'):8} "
                   f"{r.get('n_runs', 0):>4} {thesis:8} {','.join(r.get('thesis_patched') or []) or '-':22} "
                   f"{os.path.basename(str(r.get('worktree') or ''))}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dates", help="comma-separated YYYY-MM-DD; default: every date found")
    ap.add_argument("--print", action="store_true", help="print the existing manifest, no rebuild")
    args = ap.parse_args()
    if args.print:
        print(render(corpus.load_manifest()))
        return 0
    dates = [d.strip() for d in args.dates.split(",")] if args.dates else None
    print(render(corpus.build_manifest(dates)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
