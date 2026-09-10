"""`scripts/report_replay_pnl.py` — the pairing and the arithmetic.

The report is what an agent driving a replay actually reads, so the failure that matters
is a trade going MISSING from the tally, not a formatting slip. Every test here is about
a trade being counted, counted once, and counted with the right sign.
"""
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from scripts.report_replay_pnl import render, summarize, trades   # noqa: E402


def _rec(kind, time, **kw):
    return {"kind": kind, "time": time, "plan_id": "p1", "mechanism": "m", **kw}


def _write(tmp_path, records):
    with open(os.path.join(str(tmp_path), "trader_decisions.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return str(tmp_path)


def test_a_long_stopped_out_books_negative_points():
    tr = trades([_rec("fill", "t1", price=29964.0, direction="UP"),
                 _rec("stop_out", "t2", price=29939.0, direction="UP", entry=29964.0)])
    assert len(tr) == 1
    assert tr[0]["points"] == -25.0
    assert tr[0]["exit_kind"] == "stop_out"


def test_a_short_that_falls_books_positive_points():
    tr = trades([_rec("fill", "t1", price=29469.25, direction="DOWN"),
                 _rec("take_profit", "t2", price=29337.25, direction="DOWN",
                      entry=29469.25)])
    assert tr[0]["points"] == 132.0


def test_several_fills_in_one_session_are_booked_separately():
    """The attempt budget is 3 per plan, so this is the normal case, not the edge."""
    tr = trades([_rec("fill", "t1", price=100.0, direction="UP"),
                 _rec("stop_out", "t2", price=90.0, direction="UP", entry=100.0),
                 _rec("fill", "t3", price=95.0, direction="UP"),
                 _rec("take_profit", "t4", price=115.0, direction="UP", entry=95.0)])
    assert [t["points"] for t in tr] == [-10.0, 20.0]


def test_a_mark_is_counted_but_reported_apart_from_realised(tmp_path):
    run_dir = _write(tmp_path, [
        _rec("fill", "t1", price=100.0, direction="UP"),
        _rec("stop_out", "t2", price=90.0, direction="UP", entry=100.0),
        _rec("fill", "t3", price=95.0, direction="UP"),
        _rec("mark", "t4", price=125.0, direction="UP", entry=95.0)])
    s = summarize(run_dir)
    assert s["realised_pts"] == -10.0
    assert s["marked_pts"] == 30.0
    assert s["total_pts"] == 20.0
    assert "MARK, not an exit" in render(s)


def test_an_unclosed_fill_is_reported_loudly_never_dropped(tmp_path):
    """A fill with no exit and no mark is a BUG in the runner. Silently omitting it is
    how a session's P&L becomes wrong in the flattering direction."""
    run_dir = _write(tmp_path, [_rec("fill", "t1", price=100.0, direction="UP")])
    s = summarize(run_dir)
    assert s["n_trades"] == 1 and s["n_unclosed"] == 1
    assert "UNCLOSED" in render(s) and "BUG" in render(s)


def test_a_session_with_no_fill_says_so(tmp_path):
    run_dir = _write(tmp_path, [_rec("veto", "t1", reason="max_distance"),
                                _rec("veto", "t2", reason="max_distance"),
                                _rec("plan_dead", "t3", reason="dol_reached")])
    s = summarize(run_dir)
    assert s["total_pts"] == 0.0
    out = render(s)
    assert "NO FILL" in out
    assert "max_distance=2" in out
    assert "dol_reached" in out


def test_the_total_line_names_the_contract_count_it_used(tmp_path):
    """`live_orders.py` defaults to 2 contracts and `orchestrator/relay.py` to 1, so a
    dollar figure that does not say which it used cannot be checked."""
    run_dir = _write(tmp_path, [
        _rec("fill", "t1", price=100.0, direction="UP"),
        _rec("take_profit", "t2", price=110.0, direction="UP", entry=100.0)])
    out = render(summarize(run_dir))
    assert "+10.00 pts" in out and "contract(s)" in out


def test_a_dark_day_is_a_result_not_a_traceback(tmp_path):
    """2026-08-03: a NEUTRAL / LOW thesis with no DOL, so no plan armed and the Executor
    was never built. Nothing is recorded and no decisions file exists. The report must
    say so; raising here turned an ordinary session into a crash."""
    s = summarize(str(tmp_path))
    assert s["status"] == "dark"
    assert s["total_pts"] == 0.0
    assert "DARK DAY" in render(s)


def test_a_plan_that_armed_and_recorded_nothing_is_flagged_as_abnormal(tmp_path):
    """The other side of the same absence, and it is NOT benign."""
    open(os.path.join(str(tmp_path), "plans.json"), "w").write("{}")
    s = summarize(str(tmp_path))
    assert s["status"] == "armed_but_silent"
    assert "investigate" in render(s)
