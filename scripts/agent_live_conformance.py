"""Post-session conformance: did the LIVE agent decide what a REPLAY of the day decides?

    python scripts/agent_live_conformance.py 2026-09-18
    python scripts/agent_live_conformance.py 2026-09-18 --live-dir <folder>

Plan 38 D12: the first live session is a conformance test, and only a session whose
deltas are ALL explained becomes gate 2's new fixture. This is the tool that produces
the deltas. It reports three things:

  1. the DECISION diff — live `trader_decisions.jsonl` against a replay of the same date,
     compared on gate 2's own `_key` (kind, mechanism, artifact, trigger, stop, reason),
     so this and `test_gate_live_replay_fidelity.py` can never disagree about identity;
  2. every dispatch — per-signal `dispatch_ms`, every ack that was not ok, everything
     suppressed — from `agent_dispatch.jsonl`;
  3. every watchdog kill and supervisor close.

**No model call, by construction.** The replay is served the LIVE thesis out of the
session's `thesis_state.json`, exactly the way gate 2 serves its fixture: the real
backend factory is replaced by a stub for the run, and the thesis cache is pointed at a
throwaway directory so no synthetic recording lands in `<global>/thesis_cache`. Live ran
the OpenRouter route while every warm cache was recorded through the Anthropic one —
same model, different key — so a cache lookup would miss and a re-call would test the
model, not the wiring.

**Before running it:** run parquet-check and promote the day. A stale `main` makes the
replay read yesterday's tape and every delta meaningless.

**What a delta means.** Live stop-outs the replay does not book: the bar shape (the raw
second is not reaching the trader). A `fill_voided`: an entry the dispatcher refused —
paused, a suppress sentinel, the window gate. `external_kill` / `session_disarmed`: the
watchdog or a restart; the reason is on the record. `dispatch_ms` in the seconds: the
pre-close reconcile is running on an agent close, which `skip_recon` exists to prevent.

REPLACING GATE 2'S FIXTURE (`agent/trader/fixtures/live_20260825/`), once every delta
above is explained and none is a defect:

  1. copy `trader_decisions.jsonl`, `plans.json` and `thesis_state.json` from the live
     session folder into `agent/trader/fixtures/live_<YYYYMMDD>/` — copies, never moves;
  2. in `test_gate_live_replay_fidelity.py` point `FIX` / `DATE` at it, drop the
     `arm_hhmm="10:40"` override and the 10:39-11:00 window stub (a real session arms at
     09:20 and runs the standard window), and drop `ACT_TRADER_5M=1` unless the session
     ran with it;
  3. delete the two ERA NOTES and `LIVE_ERA_TAIL`: they exist because the 08-25 fixture
     predates the fill model, and a plan-38 session does not. Compare the FULL streams;
  4. keep the old fixture directory until the new gate has been green under `-m slow`
     once, then remove it in the same commit that re-points the test;
  5. record in the commit message which deltas were accepted and why.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import statistics
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

DECISIONS = "trader_decisions.jsonl"
THESIS = "thesis_state.json"
DISPATCH = "agent_dispatch.jsonl"


def read_jsonl(path) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def decision_key(rec: dict):
    """Gate 2's decision identity — imported, not restated, so the two cannot drift."""
    from agent.trader.test_gate_live_replay_fidelity import _key
    return _key(rec)


def diff_streams(live: list, replay: list, key=None) -> list:
    """Every place the two decision streams differ, in order.

    Each delta is `{"op": "live_only" | "replay_only" | "changed", "live": [...],
    "replay": [...]}` carrying the whole records, so the reader has the times and prices
    the key leaves out."""
    key = key or decision_key
    a, b = [key(r) for r in live], [key(r) for r in replay]
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b,
                                                      autojunk=False).get_opcodes():
        if op == "equal":
            continue
        name = {"delete": "live_only", "insert": "replay_only"}.get(op, "changed")
        out.append({"op": name, "live": live[i1:i2], "replay": replay[j1:j2]})
    return out


def dispatch_report(lines: list) -> dict:
    """What `agent_dispatch.jsonl` says happened on the way to the broker."""
    sent = [l for l in lines if l.get("signal") and l.get("dispatch_ms") is not None]
    times = [float(l["dispatch_ms"]) for l in sent]
    kinds = lambda name: [l for l in lines                       # noqa: E731
                          if (l.get("sim_event") or {}).get("kind") == name]
    return {
        "signals": [{"seq": l.get("seq"), "bar_time": l.get("bar_time"),
                     "kind": l["signal"].get("kind"),
                     "reason": l["signal"].get("reason"),
                     "dispatch_ms": l["dispatch_ms"],
                     "ok": (l.get("ack") or {}).get("ok")} for l in sent],
        "dispatch_ms": ({"n": len(times), "max": max(times),
                         "median": statistics.median(times)} if times else None),
        "ack_failures": [l for l in lines if l.get("ack") and not l["ack"].get("ok")],
        "watchdog_kills": kinds("watchdog_kill"),
        "supervisor_closes": kinds("supervisor_close"),
        "supervise_errors": kinds("supervise_error"),
        "suppressed": [l for l in lines if l.get("suppressed")],
    }


def replay_with_live_thesis(date: str, thesis_state: dict) -> str:
    """Replay `date` serving the live thesis. Returns the run dir. NEVER calls a model:
    the backend factory is a stub for the duration, restored afterwards."""
    import agent.trader.replay as R
    from agent.trader.thesis_cache import CACHE_ENV

    thesis = thesis_state.get("thesis")
    meta = dict(thesis_state.get("call_meta") or {})
    latency = meta.get("latency_sec")
    latency = float(latency) if latency else R.DEFAULT_ARRIVAL_LATENCY_SEC

    real_backend, prev_cache = R._real_backend, os.environ.get(CACHE_ENV)
    with tempfile.TemporaryDirectory(prefix="conformance_cache_") as cache_dir:
        R._real_backend = lambda: (lambda *a, **k: (thesis, meta))
        os.environ[CACHE_ENV] = cache_dir
        try:
            res = R.run_replay([date], allow_calls=True, arrival_latency_sec=latency)
        finally:
            R._real_backend = real_backend
            if prev_cache is None:
                os.environ.pop(CACHE_ENV, None)
            else:
                os.environ[CACHE_ENV] = prev_cache
    return res[date]["run_dir"]


def _brief(rec: dict) -> str:
    bits = [str(rec.get("time")), str(rec.get("kind"))]
    for k in ("mechanism", "price", "entry", "trigger", "stop", "reason"):
        if rec.get(k) not in (None, ""):
            bits.append("%s=%s" % (k, rec[k]))
    return " ".join(bits)


def render(date, deltas, report, live_dir, run_dir) -> str:
    out = ["[conformance] %s" % date, "  live:   %s" % live_dir,
           "  replay: %s" % run_dir, ""]
    out.append("DECISIONS: %s" % ("IDENTICAL on gate 2's key" if not deltas
                                  else "%d delta(s) - EXPLAIN EVERY ONE" % len(deltas)))
    for d in deltas:
        out.append("  [%s]" % d["op"])
        out += ["    live   | " + _brief(r) for r in d["live"]]
        out += ["    replay | " + _brief(r) for r in d["replay"]]
    out.append("")
    ms = report["dispatch_ms"]
    out.append("DISPATCH: %s" % ("no signals sent" if ms is None else
                                 "%d signal(s), median %.1f ms, max %.1f ms"
                                 % (ms["n"], ms["median"], ms["max"])))
    for s in report["signals"]:
        out.append("  seq=%s %s %s%s  %.1f ms  ok=%s" % (
            s["seq"], s["bar_time"], s["kind"],
            " (%s)" % s["reason"] if s["reason"] else "", s["dispatch_ms"], s["ok"]))
    for name in ("ack_failures", "watchdog_kills", "supervisor_closes",
                 "supervise_errors", "suppressed"):
        rows = report[name]
        out.append("%s: %d" % (name.upper(), len(rows)))
        out += ["  " + json.dumps(r, default=str) for r in rows]
    return "\n".join(out).encode("ascii", "replace").decode("ascii")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("date", help="the live session date, YYYY-MM-DD")
    ap.add_argument("--live-dir", default=None,
                    help="the live session folder (default: <global>/sessions/<date>)")
    args = ap.parse_args(argv)

    live_dir = args.live_dir
    if live_dir is None:
        import paths
        live_dir = str(paths.sessions_dir() / args.date)
    state_path = os.path.join(live_dir, THESIS)
    if not os.path.exists(state_path):
        print("[conformance] %s: no %s in %s - the agent never armed that session"
              % (args.date, THESIS, live_dir))
        return 2
    with open(state_path, encoding="utf-8") as fh:
        thesis_state = json.load(fh)

    run_dir = replay_with_live_thesis(args.date, thesis_state)
    deltas = diff_streams(read_jsonl(os.path.join(live_dir, DECISIONS)),
                          read_jsonl(os.path.join(run_dir, DECISIONS)))
    report = dispatch_report(read_jsonl(os.path.join(live_dir, DISPATCH)))
    print(render(args.date, deltas, report, live_dir, run_dir))
    clean = not (deltas or report["ack_failures"] or report["watchdog_kills"]
                 or report["supervise_errors"])
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
