import datetime

import pandas as pd
import pytest

from agent.study.skeleton import (
    Skeleton, Segment, session_skeleton, STATUS_OK, STATUS_NO_MOVE)

TZ = "America/New_York"
DATE = datetime.date(2026, 8, 13)


def _frame(rows):
    """rows = [(hhmm, open, high, low, close)] -> a 5m OHLC frame on DATE."""
    idx, data = [], []
    for hhmm, o, h, l, c in rows:
        idx.append(pd.Timestamp(f"2026-08-13 {hhmm}", tz=TZ))
        data.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1.0})
    return pd.DataFrame(data, index=pd.DatetimeIndex(idx))


def _ramp(start_hhmm, n, base, step, *, up=True):
    """n consecutive 5m bars marching `step` points per bar."""
    rows, t = [], pd.Timestamp(f"2026-08-13 {start_hhmm}", tz=TZ)
    price = base
    for _ in range(n):
        nxt = price + step if up else price - step
        rows.append((t.strftime("%H:%M"), price, max(price, nxt), min(price, nxt), nxt))
        price = nxt
        t += pd.Timedelta(minutes=5)
    return rows


def test_an_up_move_then_a_retrace_yields_an_up_primary():
    rows = _ramp("09:30", 8, 29900.0, 20.0)          # +160 pts, qualifies (>=50)
    rows += _ramp("10:10", 6, 30060.0, 20.0, up=False)   # -120 pts, closes the leg
    sk = session_skeleton(_frame(rows), DATE)
    assert sk.status == STATUS_OK
    assert sk.direction == "up"
    primary = sk.segments[0]
    assert primary.role == "primary"
    assert primary.direction == "up"
    assert primary.extreme_price == pytest.approx(30060.0)
    assert primary.extent == pytest.approx(160.0)


def test_a_down_move_yields_a_down_primary():
    rows = _ramp("09:30", 8, 30060.0, 20.0, up=False)
    rows += _ramp("10:10", 6, 29900.0, 20.0)
    sk = session_skeleton(_frame(rows), DATE)
    assert sk.direction == "down"
    assert sk.segments[0].extreme_price == pytest.approx(29900.0)


def test_move_start_price_is_the_start_bars_low_for_an_up_leg():
    """NOT the leg's price_low, which is the minimum across the whole span and can sit
    on a later bar."""
    rows = _ramp("09:30", 8, 29900.0, 20.0)
    rows += _ramp("10:10", 6, 30060.0, 20.0, up=False)
    sk = session_skeleton(_frame(rows), DATE)
    assert sk.segments[0].start_price == pytest.approx(29900.0)
    assert sk.segments[0].start_ts.strftime("%H:%M") == "09:30"


def test_a_flat_session_reports_no_move_and_no_segments():
    rows = [(f"{9 + i // 12:02d}:{(30 + 5 * i) % 60:02d}", 29900.0, 29905.0, 29895.0,
             29900.0) for i in range(12)]
    sk = session_skeleton(_frame(rows), DATE)
    assert sk.status == STATUS_NO_MOVE
    assert sk.segments == ()
    assert sk.direction is None


def test_a_move_still_running_at_the_window_end_is_censored():
    rows = _ramp("09:30", 20, 29900.0, 20.0)         # never retraces
    sk = session_skeleton(_frame(rows), DATE)
    assert sk.status == STATUS_OK
    assert sk.censored is True
    assert sk.segments[0].closed is False


def test_a_completed_move_is_not_censored():
    """The tail must exceed NO_EXTENSION_HORIZON, or `censored` is genuinely undecided:
    a frame that ends before the horizon elapses never satisfied the no-extension test,
    and calling that move 'completed' would fabricate an ending the tape never showed."""
    rows = _ramp("09:30", 8, 29900.0, 20.0)              # up +160 to 30060 at 10:05
    rows += _ramp("10:10", 12, 30060.0, 20.0, up=False)  # 60 min of lower prices
    sk = session_skeleton(_frame(rows), DATE)
    primary = sk.segments[0]
    assert primary.direction == "up"
    assert primary.extreme_price == pytest.approx(30060.0)
    assert primary.closed is True
    assert sk.censored is False


def test_a_second_same_direction_leg_is_a_secondary_segment():
    rows = _ramp("09:30", 8, 29900.0, 20.0)              # up   +160
    rows += _ramp("10:10", 6, 30060.0, 20.0, up=False)   # down -120
    rows += _ramp("10:40", 8, 29940.0, 20.0)             # up   +160 again
    sk = session_skeleton(_frame(rows), DATE)
    roles = [s.role for s in sk.segments]
    assert roles[0] == "primary"
    assert "secondary" in roles
    secondary = next(s for s in sk.segments if s.role == "secondary")
    assert secondary.direction == sk.direction


def test_counter_legs_are_recorded_as_observations():
    rows = _ramp("09:30", 8, 29900.0, 20.0)
    rows += _ramp("10:10", 6, 30060.0, 20.0, up=False)
    rows += _ramp("10:40", 8, 29940.0, 20.0)
    sk = session_skeleton(_frame(rows), DATE)
    counters = [s for s in sk.segments if s.role == "counter"]
    assert counters and counters[0].direction != sk.direction


def test_segment_indices_are_dense_and_ordered_by_time():
    rows = _ramp("09:30", 8, 29900.0, 20.0)
    rows += _ramp("10:10", 6, 30060.0, 20.0, up=False)
    rows += _ramp("10:40", 8, 29940.0, 20.0)
    sk = session_skeleton(_frame(rows), DATE)
    assert [s.index for s in sk.segments] == list(range(len(sk.segments)))
    stamps = [s.start_ts for s in sk.segments]
    assert stamps == sorted(stamps)


def test_an_empty_frame_reports_no_move_rather_than_raising():
    sk = session_skeleton(pd.DataFrame(), DATE)
    assert sk.status == STATUS_NO_MOVE
    assert sk.censored is False


def test_the_skeleton_never_reads_a_wall_clock():
    """Bar time only -- a wall clock would make the study non-reproducible."""
    import inspect

    import agent.study.skeleton as mod
    src = inspect.getsource(mod)
    assert "datetime.now" not in src
    assert "get_et_now" not in src
