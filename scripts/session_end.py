"""`trade.py session-end`: the agent-free replacement for the parquet-check skill (session-end).

Runs the same engine the skill drives, `scripts/check_session_parquets.py --mode session-end`
(validate + repair the 1s session files and the 1m mains, merge, promote live -> main, R2
publish), and turns the skill's judgement calls into fixed rules:

  * IB gate: the merge gap-fills the main -> session seam from IB; without IB the seam is baked
    in. The skill refused to run when IB was down, so this does too (a dry run is exempt: it
    reads nothing from IB).
  * Retry: a failed 1s merge (usually an IB pacing / connection hiccup during a targeted fill or
    rebuild) is re-run after a fixed wait — `RETRY_WAIT_SEC` is IB's 10-minute historical-data
    pacing window plus a margin — instead of an operator deciding when to try again. A failed
    promote, publish or 1m repair is not retried: re-running cannot change those outcomes.
  * Exit code: 0 = done (the engine's benign "1 = something was merged" is success), otherwise
    the engine's own failure code (2 merge/repair/publish failed, 3 script error, 4 roll due),
    or 5 = refused because IB is down.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE = REPO_ROOT / "scripts" / "check_session_parquets.py"

RETRY_WAIT_SEC = 650
DEFAULT_RETRIES = 2
EXIT_IB_DOWN = 5


def ib_reachable(timeout: float = 3.0) -> tuple[bool, str]:
    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", "4002"))
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port}"
    except OSError as exc:
        return False, f"{host}:{port} ({exc})"


def run_engine(extra_args: list[str]) -> tuple[int, dict]:
    """One engine run. Its stderr streams through; stdout is its JSON report."""
    proc = subprocess.run(
        [sys.executable, str(ENGINE), "--mode", "session-end", *extra_args],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, text=True)
    try:
        report = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        report = {"error": "unparseable engine output", "raw": proc.stdout[-2000:]}
    return proc.returncode, report


def merge_failed(report: dict) -> bool:
    return any(r.get("merge_success") is False
               for r in (report.get("instruments") or {}).values())


def summarize(report: dict) -> list[str]:
    """The skill's final summary, as plain lines."""
    lines = []
    for inst, r in (report.get("instruments") or {}).items():
        s = (f"{inst} 1s: severity={r.get('severity')} action={r.get('action')} "
             f"merge={r.get('merge_success')}")
        if r.get("merged_rows") is not None:
            s += f" rows={r['merged_rows']}"
        if r.get("reason"):
            s += f" reason={r['reason']}"
        if (r.get("late_start_hours") or 0) > 2.0:
            s += f" LATE START {r['late_start_hours']:.1f}h (verify overnight data)"
        lines.append(s)
    promo = report.get("promotion")
    if promo:
        s = f"promotion: success={promo.get('promote_success')}"
        if promo.get("reason"):
            s += f" reason={promo['reason']}"
        if "publish_success" in promo:
            s += (f" | R2 publish: success={promo.get('publish_success')}"
                  f" configured={promo.get('publish_configured')}")
            if promo.get("publish_error"):
                s += f" error={promo['publish_error']}"
        lines.append(s)
    elif not report.get("dry_run"):
        lines.append("promotion: not run (no successful merge) — main NOT updated")
    for inst, r in (report.get("instruments_1m") or {}).items():
        s = f"{inst} 1m: action={r.get('action')} repair={r.get('repair_success')}"
        if r.get("gapfill_status"):
            s += f" gapfill={r['gapfill_status']}"
        if r.get("seam_issue"):
            s += f" seam_issue={r['seam_issue']}"
        lines.append(s)
    roll = report.get("rollover") or {}
    if report.get("rollover_blocked") or roll.get("due"):
        lines.append("CONTRACT ROLLOVER DUE — run `trade.py rollover-prep` (see --dry-run)")
    if report.get("error"):
        lines.append(f"ERROR: {report['error']}")
    return lines


def run(args: list[str], *, retries: int = DEFAULT_RETRIES, wait_sec: float = RETRY_WAIT_SEC,
        sleep=time.sleep, out=print) -> int:
    dry = "--dry-run" in args
    extra = [a for a in args if a in ("--dry-run", "--full-validate")]
    if not dry:
        ok, where = ib_reachable()
        if not ok:
            out(f"REFUSED: IB Gateway is not reachable at {where}. The session-end merge "
                "gap-fills the 1s seams from IB; without it they are baked into main. "
                "Start IB Gateway and re-run.")
            return EXIT_IB_DOWN
    attempt = 0
    while True:
        code, report = run_engine(extra)
        if code in (0, 1) or not merge_failed(report) or attempt >= retries or dry:
            break
        attempt += 1
        out(f"1s merge failed (engine exit {code}); retry {attempt}/{retries} in "
            f"{int(wait_sec)}s (IB pacing window)")
        sleep(wait_sec)
    for line in summarize(report):
        out(line)
    return 0 if code in (0, 1) else code
