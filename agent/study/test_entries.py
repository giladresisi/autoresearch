"""Task 1 (plan 33): the entry feed.

Every fixture here is SYNTHETIC. The real-tape sweep is the runner's job; these tests
pin the contract the sweep depends on:

  * the fill carries the MECHANISM's stop, never a stop this module invents;
  * one position per session, the FIRST fill;
  * no DOL on a track means no position on that track (§4 (a) / clause 14), reproduced
    explicitly rather than inherited from the Executor's silence;
  * the handoff is the fill EVENT — this module never reads `OrderSim.position`;
  * no wall clock is read anywhere in the feed.

The synthetic tape is driven at 1m resolution. The Executor derives its bar-close verdict
from a minute rollover, so one call per 1m bar exercises exactly the bar-close cascade the
1s driver reaches on the last second of each minute; the 1s path adds intra-minute fill
timing, which is the runner's concern and `test_fill_conformance.py`'s.
"""
import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study import entries                                    # noqa: E402
from agent.trader import executor as _ex                           # noqa: E402

TZ = "America/New_York"


@pytest.fixture(autouse=True)
def _thesis_cache_to_tmp(tmp_path, monkeypatch):
    """Task 1 Step 5. `<global>/thesis_cache` is read by every worktree, so a test that
    ever reaches the Analyzer path must not be able to deposit a synthetic recording in
    it. `test_the_thesis_cache_is_never_touched` proves this module does not reach that
    path today; this fixture makes that safe rather than merely true."""
    monkeypatch.setenv("ACT_THESIS_CACHE_DIR", str(tmp_path / "thesis_cache"))


# --------------------------------------------------------------------------- #
# Synthetic tape helpers                                                        #
# --------------------------------------------------------------------------- #

def _bars(date: str, rows) -> pd.DataFrame:
    """`rows` = [(hh:mm, open, high, low, close)] -> a 1m OHLCV frame."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"{date} {r[0]}", tz=TZ) for r in rows])
    return pd.DataFrame(
        {"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
         "Volume": [100.0] * len(rows)}, index=idx)


def _flat(date, start, minutes, price, step=0.0):
    out, p = [], price
    t = pd.Timestamp(f"{date} {start}", tz=TZ)
    for i in range(minutes):
        ts = (t + pd.Timedelta(minutes=i)).strftime("%H:%M")
        out.append((ts, p, p + 1.0, p - 1.0, p))
        p += step
    return out


def _feed(mnq: pd.DataFrame, mes: pd.DataFrame, start_ts):
    """(now, bars) per 1m bar at or after `start_ts`, frames truncated to `now`."""
    for ts in mnq.index:
        if ts < start_ts:
            continue
        yield ts, {"MNQ": mnq[mnq.index <= ts], "MES": mes[mes.index <= ts]}


def _plan(direction, dol, mechanism="fvg_negation_reversal"):
    return {"plan_id": "test", "direction": direction,
            "dol": None if dol is None else {"level": "test_dol", "price": dol},
            "valid_while": [], "armed_classes": [mechanism], "attempts_used": 0,
            "blacklist": []}


def _bull_gap_then_short(date="2026-06-10"):
    """A bull 5m FVG built pre-open, then a DOWN plan that binds it (§4 negation).

    The gap spans [28100, 28140] on the 5m grid (bar1 high 28100, bar3 low 28140), so
    the sell-stop trigger is 28100 - 3 = 28097 and the structural stop 28140 + 3 =
    28143 -- 46 pts, well beyond the 25-pt cap, which is the point: the fill must carry
    `min(structural, trigger + cap)` = 28122, and nothing else.
    """
    rows = []
    rows += _flat(date, "09:00", 5, 28090.0)                     # 5m bar1: high 28091
    rows += [("09:05", 28091.0, 28100.0, 28090.0, 28099.0)]      # bar1 high -> 28100
    rows += _flat(date, "09:06", 4, 28099.0)
    rows += _flat(date, "09:10", 5, 28150.0)                     # bar2 (the displacement)
    rows += [("09:15", 28150.0, 28160.0, 28140.0, 28150.0)]      # bar3 low -> 28140
    rows += _flat(date, "09:16", 4, 28150.0)
    rows += _flat(date, "09:20", 11, 28150.0)                    # arm .. settle
    rows += _flat(date, "09:31", 4, 28150.0)
    # the break: price trades down through 28097
    rows += [("09:35", 28150.0, 28151.0, 28090.0, 28092.0)]
    rows += _flat(date, "09:36", 20, 28092.0, step=-1.0)
    return _bars(date, rows)


# --------------------------------------------------------------------------- #
# The contract                                                                  #
# --------------------------------------------------------------------------- #

def test_the_fill_carries_the_mechanisms_own_stop_not_a_synthetic_one():
    date = "2026-06-10"
    mnq = _bull_gap_then_short(date)
    arm = pd.Timestamp(f"{date} 09:20", tz=TZ)
    res = entries.drive(_plan("DOWN", 27900.0), arm,
                        _feed(mnq, mnq, arm), date=date, track="test")

    assert res.fill is not None, res.reason
    # trigger 28097.0; structural 28143.0 is 46 pts away, so the 25-pt cap governs.
    assert res.fill.stop == pytest.approx(28097.0 + _ex.STOP_CAP_PTS)
    # ... and it is emphatically NOT a stop this module invented from the fill price.
    assert res.fill.stop != pytest.approx(res.fill.price + 25.0) or \
        res.fill.price == pytest.approx(28097.0)
    assert res.fill.mechanism == "fvg_negation_reversal"
    assert res.fill.artifact_label


def test_only_the_first_fill_of_a_session_is_returned():
    """Clause 1, and the discriminating part is that the feed STOPS.

    Asserting `n_fills == 1` alone is tautological — the loop breaks on the first fill,
    so it could never be anything else. What separates "the first fill" from "the last
    fill" is that the remaining tape is never consumed, so a later fill cannot displace
    it. That is what this asserts.
    """
    date = "2026-06-10"
    mnq = _bull_gap_then_short(date)
    # Extend the tape so a stop-out and a second entry are at least possible.
    extra = _bars(date, _flat(date, "09:56", 30, 28072.0, step=2.0))
    mnq = pd.concat([mnq, extra])
    arm = pd.Timestamp(f"{date} 09:20", tz=TZ)
    feed = _feed(mnq, mnq, arm)
    res = entries.drive(_plan("DOWN", 27900.0), arm, feed, date=date, track="test")

    assert res.fill is not None
    assert res.n_fills == 1
    assert res.fill is res.all_fills[0]
    # the tape is NOT exhausted: bars after the fill were never driven
    remaining = list(feed)
    assert remaining, "the feed ran to the end, so nothing stopped a later fill"
    assert pd.Timestamp(remaining[0][0]) > res.fill.ts
    assert res.last_bar == str(res.fill.ts)


def test_a_session_with_no_dol_on_this_track_yields_no_fill():
    """(a) / clause 14: reproduce the contract-layer gate the Executor does not enforce.

    `validate_contracts` refuses a directional thesis without `dol.price`, so an empty
    menu yields NEUTRAL -> no plan -> no arming. The Executor itself SKIPS the DOL-floor
    veto when `dol is None` and would happily enter, so the gate has to be reproduced
    here or the no-DOL sessions would silently become the easiest entries in the corpus.
    """
    date = "2026-06-10"
    mnq = _bull_gap_then_short(date)
    arm = pd.Timestamp(f"{date} 09:20", tz=TZ)

    res = entries.first_fill(date, "DOWN", None, tape=_StubTape(mnq, arm),
                             track="test")
    assert res.fill is None
    assert res.reason == entries.NO_DOL
    assert res.executor_ran is False


def test_a_dol_already_behind_price_at_the_arm_is_not_a_draw():
    """The oracle label is 'the last pool the move REACHED', so it is often already taken
    by the arm. Handed to the Executor it is an instant `dol_reached` death, which scores
    the session on the mechanisms' silence rather than on the policy. Excluded as its own
    category, never as an absence."""
    date = "2026-06-10"
    mnq = _bull_gap_then_short(date)
    arm = pd.Timestamp(f"{date} 09:20", tz=TZ)          # price there is 28150.0
    res = entries.first_fill(date, "DOWN", 28200.0, tape=_StubTape(mnq, arm),
                             track="test")
    assert res.reason == entries.DOL_BEHIND
    assert res.executor_ran is False

    # ... and the same price on the other side of the trade IS a draw.
    assert entries.is_forward_draw(28200.0, "UP", 28150.0)
    assert not entries.is_forward_draw(28100.0, "UP", 28150.0)
    assert entries.is_forward_draw(28100.0, "DOWN", 28150.0)


def test_the_arm_is_0921_because_that_is_the_first_bar_an_executor_sees():
    """09:20 is the replay WINDOW; the Executor does not exist until the thesis lands
    (~40 s) and the plan is derived at the next bar close."""
    assert entries.ARM_ET == (9, 21)
    w0, w1 = entries.SessionTape.window("2026-06-10")
    assert (w0.hour, w0.minute) == (9, 21)
    assert (w1.hour, w1.minute) == (13, 0)


def test_the_partial_minute_carries_RAW_per_second_extremes_for_mnq():
    """`backtest_smt.py:1690` verbatim, and it is not cosmetic.

    The 1s driver feeds MNQ's running minute with the CURRENT SECOND's High/Low and MES's
    with cumulative ones. The Executor reads that row for `now_mid` — §11's calibrated
    market-fill price — and for the crossed-trigger test, so a cumulative MNQ bar widens
    the mid over the whole elapsed minute and prices every market fill on a basis the
    mechanisms were never calibrated against.
    """
    date = "2026-06-10"
    idx = pd.DatetimeIndex(
        [pd.Timestamp(f"{date} 09:21:0{i}", tz=TZ) for i in range(3)])
    tape = pd.DataFrame({"Open": [10.0, 10.0, 10.0], "High": [20.0, 12.0, 13.0],
                         "Low": [5.0, 9.0, 8.0], "Close": [11.0, 11.0, 11.0],
                         "Volume": [1.0, 1.0, 1.0]}, index=idx)
    hist = {"MNQ": pd.DataFrame(columns=list(entries.COLS)),
            "MES": pd.DataFrame(columns=list(entries.COLS))}
    # Read INSIDE the loop: the yielded frames alias one reused buffer (see
    # `_replay_frames`), so collecting them and reading afterwards shows only the last
    # state. That aliasing is the backtest's own no-copy trick and is what makes the
    # sweep affordable; the consumer must use each frame before advancing.
    mnq_hi, mnq_lo, mes_hi, mes_lo = [], [], [], []
    for _now, bars in entries._replay_frames(hist, tape, tape):
        mnq_hi.append(float(bars["MNQ"]["High"].iloc[-1]))
        mnq_lo.append(float(bars["MNQ"]["Low"].iloc[-1]))
        mes_hi.append(float(bars["MES"]["High"].iloc[-1]))
        mes_lo.append(float(bars["MES"]["Low"].iloc[-1]))

    assert mnq_hi == [20.0, 12.0, 13.0]      # RAW per second, not 20/20/20
    assert mnq_lo == [5.0, 9.0, 8.0]         # RAW per second, not 5/5/5
    # MES keeps the cumulative convention the same loop uses for it
    assert mes_hi == [20.0, 20.0, 20.0]
    assert mes_lo == [5.0, 5.0, 5.0]


def test_a_fill_with_no_bind_record_raises_rather_than_carrying_a_null_stop():
    """The mechanism's own stop is the whole handoff (§4 (d)). A stopless Fill would push
    the failure into the policy, where it reads as a session with no risk."""
    assert entries.stop_for([], "missing", None) == (None, None)
    rec = entries._Capture()
    rec.records = [{"kind": "fill", "time": pd.Timestamp("2026-06-10 09:35", tz=TZ),
                    "price": 100.0, "direction": "UP", "artifact_id": "orphan"}]

    class _OneShot:
        def __init__(self):
            self.n = 0

        def on_bar(self, *_a, **_kw):
            self.n += 1

    import unittest.mock as mock
    with mock.patch.object(entries, "Executor", lambda *a, **kw: _OneShot()),             mock.patch.object(entries, "_Capture", lambda: rec):
        with pytest.raises(RuntimeError, match="no bind record"):
            entries.drive(_plan("UP", 200.0), pd.Timestamp("2026-06-10 09:21", tz=TZ),
                          iter([(pd.Timestamp("2026-06-10 09:35", tz=TZ), {})]),
                          date="2026-06-10", track="test")


def test_the_feed_never_reads_order_sim_position():
    """(d): the handoff is `(time, price, direction, initial_stop, mechanism)`.

    Two owners of one stop is a defect waiting to happen, so the feed reads the
    Executor's EMITTED records and nothing else.
    """
    src = _source(entries)
    for forbidden in ("_sim", ".position", "OrderSim"):
        assert forbidden not in src, forbidden


def test_no_wall_clock_is_read():
    src = _source(entries)
    for forbidden in ("datetime.now", "get_et_now", "time.time", "Timestamp.now",
                      "utcnow", "today()"):
        assert forbidden not in src, forbidden


def test_the_stop_comes_from_the_bind_record_for_the_filled_artifact():
    """Not from the most recent bind of any artifact: the binding layer churns."""
    records = [
        {"kind": "bind", "artifact_id": "A", "artifact_label": "gap A",
         "mechanism": "m", "trigger": 100.0, "stop": 110.0,
         "time": "2026-06-10T09:31:00-04:00"},
        {"kind": "bind", "artifact_id": "B", "artifact_label": "gap B",
         "mechanism": "m", "trigger": 200.0, "stop": 210.0,
         "time": "2026-06-10T09:32:00-04:00"},
    ]
    assert entries.stop_for(records, "A", pd.Timestamp("2026-06-10 09:33", tz=TZ)) \
        == (110.0, "gap A")


def test_a_dead_plan_before_any_fill_is_reported_with_its_reason():
    """A DOL already taken kills the plan; that is a distinct outcome from 'no trigger'
    and the yield report must be able to tell them apart."""
    date = "2026-06-10"
    mnq = _bull_gap_then_short(date)
    arm = pd.Timestamp(f"{date} 09:20", tz=TZ)
    # DOL sits just below the arm price, so it is reached long before any trigger.
    res = entries.drive(_plan("DOWN", 28149.0), arm,
                        _feed(mnq, mnq, arm), date=date, track="test")
    assert res.fill is None
    assert res.plan_dead == "dol_reached"
    assert res.reason == entries.PLAN_DEAD


def test_the_feed_is_silent():
    """Production convention: diagnostics live in the returned record, not stdout."""
    src = _source(entries)
    assert "print(" not in src


def test_the_thesis_cache_is_never_touched():
    """The feed drives the Executor from a SYNTHETIC plan, so the Analyzer -- and with
    it the shared thesis cache every worktree reads -- is never on the path."""
    src = _source(entries)
    for forbidden in ("Analyzer", "ThesisCache", "thesis_cache", "run_replay"):
        assert forbidden not in src, forbidden


# --------------------------------------------------------------------------- #
# Fidelity: the reconstructed tape against a RECORDED thesis                     #
# --------------------------------------------------------------------------- #

@pytest.mark.timeout(300)
def test_the_reconstructed_tape_binds_the_documented_0813_gap():
    """The feed rebuilds the 1s replay's frames rather than running the backtest, so
    the reconstruction itself has to be pinned against something independent.

    2026-08-13 has a RECORDED L1 thesis (`<global>/thesis_cache`, boundary 2026-08-13,
    bias UP, DOL `prev1_day_high` 30001.5) and `l2-mechanisms.md` §10/§11 document the
    entry it produced: a buy stop off the 5m bull FVG whose bar1 High is 29881.50, filled
    inside the 09:33 minute.

    The GAP reproduces the doc EXACTLY — §11 names `[29881.5, 29905.25]` and so does the
    tape. The prices differ only by the parameters the doc itself records as changed on
    2026-08-22 (§9's table, "3 pts (was 7 until 2026-08-22)"):

        doc trigger 29912.25 = 29905.25 + 7   (the old entry buffer)
        here        29908.25 = 29905.25 + 3   (ENTRY_BUFFER_PTS today)
        doc stop    29878.50 = 29881.50 - 3   (structural, before the cap existed)
        here        29883.25 = trigger - 25   (STOP_CAP_PTS, adopted the same day)

    So the assertion is written against today's constants rather than the doc's numbers,
    and it is a fidelity check on the TAPE — which is the thing this module reconstructs.
    """
    from agent.study.entries import SessionTape, first_fill
    try:
        tape = SessionTape()
    except (FileNotFoundError, OSError):                    # pragma: no cover
        pytest.skip("1s parquets unavailable")

    res = first_fill("2026-08-13", "UP", 30001.5, tape=tape, track="recorded_thesis")
    assert res.reason == entries.FILLED, res.reason
    assert res.fill.mechanism == "fvg_return_continuation"
    assert "29881.5" in res.fill.artifact_label          # the documented gap's bar1 High
    assert res.fill.price == pytest.approx(29908.25)     # 29905.25 + ENTRY_BUFFER_PTS
    assert res.fill.stop == pytest.approx(29908.25 - _ex.STOP_CAP_PTS)
    assert res.fill.ts.strftime("%H:%M") == "09:33"


# --------------------------------------------------------------------------- #

class _StubTape:
    def __init__(self, mnq, arm):
        self._mnq, self._arm = mnq, arm

    def feed(self, date):
        return self._arm, _feed(self._mnq, self._mnq, self._arm)

    def arm_frame(self, date):
        return self._mnq[self._mnq.index <= self._arm]


def _source(mod) -> str:
    """The module's CODE, with docstrings and comments removed.

    A raw `grep` cannot tell code from a docstring that merely names the forbidden thing,
    and every module here documents what it must not touch — so a prose scan reports its
    own warning labels as violations. `test_gate_no_legacy_writes.py` had to solve the
    same problem and solved it the same way: parse, then inspect.
    """
    import ast

    with open(mod.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=mod.__file__)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)
