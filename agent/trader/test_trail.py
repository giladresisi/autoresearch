"""O3 trail (`agent/trader/trail.py`): STUDY code, flag OFF. What is pinned: the gap
reading, the one-behind rule with the mid gate, that the Executor moves the stop through
`move_stop` and books the touch as `stop_out_initial`, and that the flag off is inert."""
import pandas as pd
import pytest

from agent.trader import trail
from agent.trader.executor import Executor
from agent.trader.test_executor_live_rules import (_kinds, _recs, _step, _ts,
                                                   make_executor)

TZ = "America/New_York"


def _m1(rows, start="09:40"):
    idx = pd.date_range(pd.Timestamp(f"2026-09-03 {start}", tz=TZ), periods=len(rows),
                        freq="1min")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)


# A 1m frame AFTER a 29250 fill at 09:40; the gaps of 5 pts or more are B (29275-29290,
# middle 09:44, visible 09:46) and C (29300-29320, middle 09:45, visible 09:47).
_ROWS = [(29250, 29252, 29249, 29251),      # 09:40 (fill bar)
         (29251, 29262, 29251, 29260),      # 09:41
         (29260, 29270, 29256, 29268),      # 09:42
         (29268, 29275, 29266, 29274),      # 09:43
         (29274, 29300, 29272, 29298),      # 09:44
         (29298, 29330, 29290, 29328),      # 09:45 -> the mid (29325) is reached
         (29328, 29335, 29320, 29333),      # 09:46
         (29333, 29345, 29331, 29340),      # 09:47
         (29340, 29350, 29338, 29348)]      # 09:48


def _run_rows(ex):
    frame = _m1(_ROWS)

    def frames(now):
        f = frame[frame.index <= now.floor("1min")]
        return {"MNQ": f, "MES": f.copy()}

    for n in range(1, len(_ROWS) + 1):
        now = frame.index[n - 1] + pd.Timedelta(minutes=1)
        ex.on_bar(now, frames(now))


def test_continuation_gaps_read_the_three_bar_pattern_in_the_trade_direction():
    m1 = _m1([(100, 102, 99, 101), (101, 108, 101, 107), (107, 110, 104, 109),   # gap 102-104
              (109, 109.5, 108, 109), (109, 112, 108.5, 111.5)])                # gap 109.5-? no
    gaps = trail.continuation_gaps(m1, "UP", 0.0)
    assert [(g["edge"], g["size"]) for g in gaps] == [(102.0, 2.0)]
    assert gaps[0]["mid"] == m1.index[1] and gaps[0]["visible"] == m1.index[3]
    assert trail.continuation_gaps(m1, "UP", 5.0) == []
    assert trail.continuation_gaps(m1, "DOWN", 0.0) == []


def test_the_stage_arms_at_the_mid_and_moves_one_gap_behind_only_on_a_new_gap(monkeypatch):
    monkeypatch.setattr(trail, "TRAIL_MIN_GAP_PTS", 0.0)
    st = trail.TrailStage("UP", 100.0, 140.0, pd.Timestamp("2026-09-03 09:40", tz=TZ))
    assert st.mid == 120.0
    assert st.arm(_ts("09:45:00"), 119.0, 110.0) is False
    assert st.arm(_ts("09:46:00"), 120.0, 110.0) is True and st.armed_at == _ts("09:46:00")
    # two gaps on the chart: A (edge 102) visible 09:43, B (edge 109.5) visible 09:46
    rows = [(100, 102, 99, 101), (101, 108, 101, 107), (107, 110, 104, 109),
            (109, 109.5, 108, 109), (109, 114, 110, 113), (113, 118, 112, 117)]
    frame = _m1(rows)
    assert st.on_bar_close(_ts("09:46:00"), frame, 95.0) == {
        "stop": 99.0, "gap": frame.index[1], "gap_edge": 102.0, "gap_size": 2.0, "n_gaps": 2}
    assert st.on_bar_close(_ts("09:47:00"), frame, 99.0) is None     # no NEW gap
    # two more bars add gaps C (edge 114, mid 09:45) and D (edge 118, mid 09:46): the
    # move is to C's edge (one behind D), and only if that tightens the stop
    frame2 = _m1(rows + [(117, 119, 116, 118), (118, 125, 118.5, 124)])
    mv = st.on_bar_close(_ts("09:49:00"), frame2, 99.0)
    assert mv["stop"] == 111.0 and mv["gap"] == frame2.index[5] and mv["n_gaps"] == 4
    st2 = trail.TrailStage("UP", 100.0, 140.0, pd.Timestamp("2026-09-03 09:40", tz=TZ))
    st2.arm(_ts("09:46:00"), 120.0, 110.0)
    assert st2.on_bar_close(_ts("09:49:00"), frame2, 112.0) is None  # would loosen


def test_flag_off_is_inert(tmp_path, monkeypatch):
    ex = make_executor(tmp_path, monkeypatch, pick={"id": "D1", "level": "x", "price": 29400.0})
    _step(ex, "09:40:00", 29250.0, fire=True)
    _step(ex, "09:50:00", 29330.0, hi=29335.0)
    assert ex._trail is None and "trail_armed" not in _kinds(tmp_path)


def test_the_executor_trails_through_move_stop_and_books_stop_out_initial(tmp_path,
                                                                           monkeypatch):
    monkeypatch.setattr(trail, "TRAIL_ENABLED", True)
    monkeypatch.setattr(trail, "TRAIL_MIN_GAP_PTS", 5.0)
    monkeypatch.setattr(trail, "TRAIL_BE_AT_ARM", False)         # the pure gap trail
    ex = make_executor(tmp_path, monkeypatch, pick={"id": "D1", "level": "x", "price": 29400.0})
    _step(ex, "09:40:00", 29250.0, fire=True)                   # mid = 29325
    _run_rows(ex)
    assert ex._trail is not None and ex._trail.armed_at == _ts("09:45:00")   # the 09:45 bar's high
    moved = [r for r in _recs(tmp_path) if r["kind"] == "stop_moved"]
    # 09:46: B is new but there is nothing behind it; 09:47: C is new, one behind is B
    # (edge 29275) -> 29272; 09:48, 09:49: nothing new of 5 pts
    assert [(m["time"][11:19], m["price"], m["reason"]) for m in moved] == [
        ("09:47:00", 29272.0, "trail")]
    assert ex.position()["stop"] == 29272.0
    assert "trail_armed" in _kinds(tmp_path)

    _step(ex, "09:50:30", 29275.0, lo=29271.0)
    assert ex.position() is None
    assert [r["price"] for r in _recs(tmp_path) if r["kind"] == "stop_out_initial"] == [29272.0]
    assert ex._positive_close is True and ex._plan["attempts_used"] == 0
    assert ex.bind_state()["plan_alive"] is False


# --------------------------------------------------------------------------- #
# break-even at the arming instant (operator decision 2026-10-01)               #
# --------------------------------------------------------------------------- #

def _stage(direction="UP", entry=100.0, target=140.0):
    return trail.TrailStage(direction, entry, target, pd.Timestamp("2026-09-03 09:40", tz=TZ))


def test_breakeven_is_offered_only_when_it_tightens_and_the_knob_is_on(monkeypatch):
    monkeypatch.setattr(trail, "TRAIL_BE_AT_ARM", True)
    monkeypatch.setattr(trail, "TRAIL_BE_OFFSET_PTS", 0.0)
    assert _stage("UP").breakeven_stop(85.0) == 100.0            # long, stop below entry
    assert _stage("UP").breakeven_stop(None) == 100.0
    assert _stage("UP").breakeven_stop(100.0) is None            # already there
    assert _stage("UP").breakeven_stop(112.0) is None            # already tighter
    assert _stage("DOWN").breakeven_stop(115.0) == 100.0
    assert _stage("DOWN").breakeven_stop(100.0) is None
    assert _stage("DOWN").breakeven_stop(90.0) is None
    monkeypatch.setattr(trail, "TRAIL_BE_AT_ARM", False)
    assert _stage("UP").breakeven_stop(85.0) is None
    assert _stage("DOWN").breakeven_stop(115.0) is None


def test_the_breakeven_offset_is_in_the_trades_favour(monkeypatch):
    monkeypatch.setattr(trail, "TRAIL_BE_AT_ARM", True)
    monkeypatch.setattr(trail, "TRAIL_BE_OFFSET_PTS", 2.0)
    assert _stage("UP").breakeven_stop(85.0) == 102.0
    assert _stage("DOWN").breakeven_stop(115.0) == 98.0
    assert _stage("UP").breakeven_stop(102.0) is None            # not strictly tighter


def test_on_bar_close_counts_gaps_but_moves_nothing_with_fvg_moves_off(monkeypatch):
    monkeypatch.setattr(trail, "TRAIL_MIN_GAP_PTS", 0.0)
    monkeypatch.setattr(trail, "TRAIL_FVG_MOVES", False)
    st = _stage()
    st.arm(_ts("09:46:00"), 120.0, 110.0)
    rows = [(100, 102, 99, 101), (101, 108, 101, 107), (107, 110, 104, 109),
            (109, 109.5, 108, 109), (109, 114, 110, 113), (113, 118, 112, 117)]
    assert st.on_bar_close(_ts("09:46:00"), _m1(rows), 95.0) is None
    assert st.n_seen == 2 and st.moves == 0


def _beh(tmp_path, monkeypatch, **knobs):
    monkeypatch.setattr(trail, "TRAIL_ENABLED", True)
    monkeypatch.setattr(trail, "TRAIL_MIN_GAP_PTS", 5.0)
    for k, v in knobs.items():
        monkeypatch.setattr(trail, k, v)
    return make_executor(tmp_path, monkeypatch, pick={"id": "D1", "level": "x", "price": 29400.0})


def test_the_executor_moves_to_breakeven_on_the_arming_tick(tmp_path, monkeypatch):
    ex = _beh(tmp_path, monkeypatch, TRAIL_BE_AT_ARM=True)
    _step(ex, "09:40:00", 29250.0, fire=True)                   # stop 29235, T2 29400, mid 29325
    _step(ex, "09:41:00", 29300.0, hi=29324.0)                  # below the mid: nothing
    assert ex.position()["stop"] == 29235.0
    assert not [r for r in _recs(tmp_path) if r["kind"] == "stop_moved"]
    now = _step(ex, "09:42:00", 29320.0, hi=29325.0)            # the tick that arms
    moved = [r for r in _recs(tmp_path) if r["kind"] == "stop_moved"]
    assert [(m["time"][11:19], m["price"], m["reason"]) for m in moved] == [
        ("09:42:00", 29250.0, "breakeven")]
    assert ex.position()["stop"] == 29250.0 and ex._trail.armed_at == now
    # a later touch of the break-even stop: a non-failure exit at the entry
    _step(ex, "09:43:00", 29260.0, lo=29249.0)
    assert ex.position() is None
    assert [r["price"] for r in _recs(tmp_path) if r["kind"] == "stop_out_initial"] == [29250.0]
    assert "stop_out" not in _kinds(tmp_path)
    assert ex._positive_close is False and ex._plan["attempts_used"] == 0


def test_fvg_moves_off_records_breakeven_alone(tmp_path, monkeypatch):
    ex = _beh(tmp_path, monkeypatch, TRAIL_BE_AT_ARM=True, TRAIL_FVG_MOVES=False)
    _step(ex, "09:40:00", 29250.0, fire=True)
    _run_rows(ex)
    reasons = [r["reason"] for r in _recs(tmp_path) if r["kind"] == "stop_moved"]
    assert reasons == ["breakeven"]
    assert ex.position()["stop"] == 29250.0


def test_breakeven_then_the_gap_trail_ratchets_from_it(tmp_path, monkeypatch):
    ex = _beh(tmp_path, monkeypatch, TRAIL_BE_AT_ARM=True, TRAIL_FVG_MOVES=True)
    _step(ex, "09:40:00", 29250.0, fire=True)
    _run_rows(ex)
    moved = [(r["price"], r["reason"]) for r in _recs(tmp_path) if r["kind"] == "stop_moved"]
    assert moved == [(29250.0, "breakeven"), (29272.0, "trail")]


def test_breakeven_off_leaves_the_gap_trail_unchanged(tmp_path, monkeypatch):
    ex = _beh(tmp_path, monkeypatch, TRAIL_BE_AT_ARM=False)
    _step(ex, "09:40:00", 29250.0, fire=True)
    _run_rows(ex)
    moved = [(r["price"], r["reason"]) for r in _recs(tmp_path) if r["kind"] == "stop_moved"]
    assert moved == [(29272.0, "trail")]
    assert "trail_armed" in _kinds(tmp_path)
