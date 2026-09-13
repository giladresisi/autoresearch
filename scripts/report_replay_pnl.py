"""P&L for one replay run, read back off `trader_decisions.jsonl`.

**Why this exists.** The replay writes every order-lifecycle event to disk and announces
it on stdout, but nothing anywhere adds them up, so "what did this session make?" could
only be answered by hand. That is the question an agent driving a replay actually has,
and leaving it unanswered is how a run gets read by eyeballing the last line.

**What a trade is here.** One `fill`, then the FIRST closing event after it —
`stop_out`, `take_profit`, or `mark`. The Executor's attempt budget is 3 per plan
(`MAX_ATTEMPTS`), so several fills per session are normal and each is booked separately.

**A `mark` is not an exit.** It is the position the window ended on, priced at the last
bar the Executor saw (`OrderSim.mark_open`). It is counted in the total because leaving
it out is what made a runner silently worth zero — but it is labelled everywhere it
appears, and `marked` is reported alongside `realised` so the two are never conflated.

**Points are the unit.** Dollars are derived and stated as such: MNQ is $2.00 a point,
and the contract count is genuinely ambiguous in this repo (`live_orders.py:46` defaults
to 2, `orchestrator/relay.py:134` to 1). `TRADING_CONTRACTS` picks it; the header prints
which number was used so no reader has to guess.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.trader.records import DECISIONS_FILE               # noqa: E402

MNQ_PNL_PER_POINT = 2.0
#: `stop_out` and `take_profit` are exits the simulation TOOK; `mark` is the window
#: ending on an open position. Order matters nowhere — membership does.
CLOSING = ("stop_out", "take_profit", "mark")
_LONG = ("UP", "LONG")


def read_decisions(run_dir: str) -> list:
    """The run's decisions, or [] when the file does not exist.

    Absent is a REAL outcome, not an error: on a dark day the Analyzer returns no
    standing thesis, the graft never builds an Executor, and nothing is ever recorded
    (2026-08-03 is such a day — a NEUTRAL / LOW-confidence thesis with no DOL). Raising
    there turned an ordinary session into a traceback. `summarize` tells the two apart
    by looking for `plans.json`.
    """
    path = os.path.join(run_dir, DECISIONS_FILE)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _points(direction, entry, exit_price) -> float:
    if entry is None or exit_price is None:
        return 0.0
    sign = 1.0 if str(direction or "").upper() in _LONG else -1.0
    return sign * (float(exit_price) - float(entry))


def trades(records: list) -> list:
    """Fills paired with their closing events, in order. An unpaired fill is returned
    with `exit_kind=None` rather than dropped: a trade that vanished from the tally is
    the failure this whole report exists to prevent."""
    out, open_fill = [], None
    for rec in records:
        kind = rec.get("kind")
        if kind == "fill":
            if open_fill is not None:
                out.append(_trade(open_fill, None))
            open_fill = rec
        elif kind in CLOSING and open_fill is not None:
            out.append(_trade(open_fill, rec))
            open_fill = None
    if open_fill is not None:
        out.append(_trade(open_fill, None))
    return out


def _trade(fill: dict, close: "dict | None") -> dict:
    entry = fill.get("price")
    direction = fill.get("direction")
    exit_price = (close or {}).get("price")
    return {
        "entry_ts": fill.get("time"), "entry": entry, "direction": direction,
        "mechanism": fill.get("mechanism"), "label": fill.get("artifact_label"),
        "exit_ts": (close or {}).get("time"), "exit": exit_price,
        "exit_kind": (close or {}).get("kind"),
        "points": _points(direction, entry, exit_price) if close else None,
    }


def summarize(run_dir: str) -> dict:
    recs = read_decisions(run_dir)
    armed = os.path.exists(os.path.join(run_dir, "plans.json"))
    # No records AND no plan = a dark day, which is a result. No records but a plan DID
    # arm = the run wrote a plan and then recorded nothing, which is not.
    status = ("ok" if recs else ("dark" if not armed else "armed_but_silent"))
    tr = trades(recs)
    booked = [t for t in tr if t["points"] is not None]
    realised = sum(t["points"] for t in booked if t["exit_kind"] != "mark")
    marked = sum(t["points"] for t in booked if t["exit_kind"] == "mark")
    vetoes: dict = {}
    for r in recs:
        if r.get("kind") == "veto":
            key = str(r.get("reason"))
            vetoes[key] = vetoes.get(key, 0) + 1
    dead = [r for r in recs if r.get("kind") == "plan_dead"]
    return {
        "run_dir": run_dir, "trades": tr, "status": status,
        "n_trades": len(tr), "n_unclosed": sum(1 for t in tr if t["points"] is None),
        "realised_pts": realised, "marked_pts": marked,
        "total_pts": realised + marked,
        "by_kind": {k: sum(1 for t in booked if t["exit_kind"] == k) for k in CLOSING},
        "vetoes": vetoes,
        "plan_dead": [(d.get("reason"), d.get("time")) for d in dead],
        "last_record_ts": recs[-1].get("time") if recs else None,
    }


def contracts() -> int:
    """`TRADING_CONTRACTS`, resolved the way production resolves it.

    The worktree `.env` is loaded first, through production's own `load_env_file`
    (shell-set values still win, as `os.environ.setdefault` guarantees). Without this
    the same run directory reported a different dollar figure depending on the path
    taken to reach it: a `--seed` replay imports `run_agent`, which loads `.env` as a
    side effect of building the backend, and a cached replay does not — so 08-18 read
    $919.00 one way and $459.50 the other, off identical points.
    """
    try:
        from agent.run_agent import load_env_file
        load_env_file(os.path.join(_REPO, ".env"))
    except Exception:
        pass
    try:
        return int(os.environ.get("TRADING_CONTRACTS", "2"))
    except ValueError:
        return 2


def render(summary: dict, date=None) -> str:
    n = contracts()
    head = f"[pnl] {date or ''} {summary['run_dir']}".strip()
    lines = [head, "[pnl] " + "-" * 72]
    if summary.get("status") == "dark":
        lines.append("[pnl] DARK DAY — no standing thesis, so no plan armed and nothing "
                     "was recorded. Not an error.")
    elif summary.get("status") == "armed_but_silent":
        lines.append("[pnl] A PLAN ARMED BUT NOTHING WAS RECORDED — investigate; this is "
                     "not a normal outcome.")
    elif not summary["trades"]:
        lines.append("[pnl] NO FILL — the session never entered.")
    for t in summary["trades"]:
        if t["points"] is None:
            lines.append(
                f"[pnl] {t['entry_ts']} {t['direction']} @ {t['entry']} "
                f"-> UNCLOSED (no exit and no mark — this is a BUG, not a result)")
            continue
        tag = " (MARK, not an exit)" if t["exit_kind"] == "mark" else ""
        lines.append(
            f"[pnl] {t['entry_ts']} {t['direction']} @ {t['entry']} -> "
            f"{t['exit_ts']} {t['exit_kind']} @ {t['exit']} = "
            f"{t['points']:+.2f} pts{tag}   [{t['mechanism']}]")
    lines.append("[pnl] " + "-" * 72)
    lines.append(
        f"[pnl] TOTAL {summary['total_pts']:+.2f} pts "
        f"(realised {summary['realised_pts']:+.2f}, marked {summary['marked_pts']:+.2f}) "
        f"= ${summary['total_pts'] * MNQ_PNL_PER_POINT * n:+.2f} "
        f"at {n} contract(s) x $2.00/pt")
    lines.append(
        f"[pnl] trades={summary['n_trades']} "
        + " ".join(f"{k}={v}" for k, v in summary["by_kind"].items() if v))
    if summary["vetoes"]:
        lines.append("[pnl] vetoes: "
                     + " ".join(f"{k}={v}" for k, v in sorted(summary["vetoes"].items())))
    for reason, ts in summary["plan_dead"]:
        lines.append(f"[pnl] plan_dead {reason} at {ts}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="P&L for a replay run directory.")
    ap.add_argument("run_dir", nargs="+")
    ap.add_argument("--json", action="store_true", help="emit the summary as JSON")
    args = ap.parse_args()

    total = 0.0
    for rd in args.run_dir:
        s = summarize(rd)
        total += s["total_pts"]
        if args.json:
            print(json.dumps({k: v for k, v in s.items()}, indent=2, default=str))
        else:
            print(render(s))
    if len(args.run_dir) > 1 and not args.json:
        print(f"[pnl] ALL RUNS {total:+.2f} pts "
              f"= ${total * MNQ_PNL_PER_POINT * contracts():+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
