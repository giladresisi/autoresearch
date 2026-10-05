"""Follow-up entry after the MES-sweep stop (`agent/trader/followup.py`, §11.8 CANDIDATE).

Pure state machine over completed 1m bars: the flag, the window, the two re-anchored
triggers, the budget and the structure veto. The Executor wiring is in
`test_executor_followup.py`.
"""
import pandas as pd
import pytest

from agent.trader import followup
from agent.trader.followup import FollowUp

TZ = "America/New_York"


def _ts(hms, date="2026-10-02"):
    return pd.Timestamp(f"{date} {hms}", tz=TZ)


def _m1(rows, start="09:47"):
    idx = pd.date_range(_ts(start), periods=len(rows), freq="1min")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)


def _open(direction="UP", exit_hms="09:46:17", **kw):
    args = dict(exit_px=31173.75, exit_kind="stop_out_initial", t2=31267.5,
                first_entry=31128.25, counter_extreme=31000.0)
    args.update(kw)
    return FollowUp.open(direction, _ts(exit_hms), **args)


def _drive(fu, m1):
    """Feed `m1` one completed bar at a time, as the bar loop does; the fires per bar."""
    out = []
    for i in range(len(m1)):
        now = m1.index[i] + pd.Timedelta(minutes=1)
        out.append(fu.on_bars(now, m1.iloc[:i + 1]))
    return out


# 1 ------------------------------------------------------------------------- #

def test_the_flag_defaults_off_and_the_env_and_override_decide(monkeypatch):
    monkeypatch.setattr(followup, "FOLLOWUP_ENABLED", None)
    monkeypatch.delenv(followup.ENV_FLAG, raising=False)
    assert followup.enabled() is False
    monkeypatch.setenv(followup.ENV_FLAG, "")
    assert followup.enabled() is False
    for off in ("0", "false", "No", "OFF"):
        monkeypatch.setenv(followup.ENV_FLAG, off)
        assert followup.enabled() is False
    for on in ("1", "true", "Yes", "ON"):
        monkeypatch.setenv(followup.ENV_FLAG, on)
        assert followup.enabled() is True
    monkeypatch.setenv(followup.ENV_FLAG, "0")
    monkeypatch.setattr(followup, "FOLLOWUP_ENABLED", True)
    assert followup.enabled() is True
    monkeypatch.delenv(followup.ENV_FLAG, raising=False)
    monkeypatch.setattr(followup, "FOLLOWUP_ENABLED", False)
    assert followup.enabled() is False


# 2 ------------------------------------------------------------------------- #

def test_the_window_is_twenty_minutes_capped_at_eleven():
    assert _open().window_end == _ts("10:06:17")
    assert _open(exit_hms="10:50:00").window_end == _ts("11:00:00")
    assert _open(exit_hms="10:40:00").window_end == _ts("11:00:00")
    fu = _open()
    assert fu.is_open and fu.closed_reason is None and fu.attempts == 0
    assert fu.state()["window_end"] == str(_ts("10:06:17"))


def test_the_skip_reasons_read_the_t2_distance():
    assert followup.skip_reason("UP", 31173.75, 31128.25, 31267.5) is None
    assert followup.skip_reason("UP", 31173.75, 31128.25, None) == "t2_unbound"
    # 29 pts left of a 139-pt first leg: under the 30-pt floor
    assert followup.skip_reason("UP", 31238.5, 31128.25, 31267.5) == "t2_too_close"
    # 40 pts left but under a quarter of a 200-pt first leg
    assert followup.skip_reason("UP", 31227.5, 31067.5, 31267.5) == "t2_too_close"
    # a short mirrors it
    assert followup.skip_reason("DOWN", 30900.0, 31000.0, 30700.0) is None
    assert followup.skip_reason("DOWN", 30720.0, 31000.0, 30700.0) == "t2_too_close"
    # a T2 already behind the exit is not remaining
    assert followup.skip_reason("UP", 31300.0, 31128.25, 31267.5) == "t2_too_close"


# 3 ------------------------------------------------------------------------- #

def test_reject_close_needs_three_quiet_closes_then_a_new_low_closing_up():
    fu = _open(exit_hms="09:46:17")
    rows = [(31170, 31172, 31150, 31155),      # 09:47 sets the extreme 31150
            (31155, 31160, 31152, 31158),      # quiet 1
            (31158, 31161, 31153, 31159),      # quiet 2
            (31159, 31162, 31151, 31160),      # quiet 3 -> armed
            (31145, 31165, 31140, 31158)]      # new low 31140, closes UP -> fires
    fires = _drive(fu, _m1(rows))
    assert fires[:4] == [[], [], [], []]
    (fire,) = fires[4]
    assert fire["mechanism"] == "followup_reject_close" and fire["followup"] is True
    assert fire["direction"] == "UP" and fire["price"] == 31158.0
    # the wick capped 15: 31140 is inside the 15-pt cap
    assert fire["stop"] == max(31140.0, 31158.0 - 15.0)
    assert fire["time"] == _ts("09:52:00")


def test_reject_close_stop_is_capped_at_fifteen_points():
    fu = _open()
    rows = [(31170, 31172, 31150, 31155), (31155, 31160, 31152, 31158),
            (31158, 31161, 31153, 31159), (31159, 31162, 31151, 31160),
            (31145, 31165, 31100, 31158)]
    (fire,) = _drive(fu, _m1(rows))[4]
    assert fire["stop"] == 31158.0 - 15.0


def test_reject_close_for_a_short_mirrors():
    fu = _open("DOWN", first_entry=31300.0, t2=31000.0, exit_px=31200.0,
               counter_extreme=31400.0)
    rows = [(31200, 31230, 31198, 31225), (31225, 31228, 31220, 31222),
            (31222, 31227, 31219, 31221), (31221, 31229, 31218, 31220),
            (31220, 31240, 31219, 31222)]          # new high, closes DOWN? 31222 > 31220
    assert _drive(fu, _m1(rows))[4] == []          # closed up: no short fire
    rows[4] = (31225, 31240, 31219, 31222)         # opens above, closes below -> DOWN
    (fire,) = _drive(_open("DOWN", first_entry=31300.0, t2=31000.0, exit_px=31200.0,
                           counter_extreme=31400.0), _m1(rows))[4]
    assert fire["direction"] == "DOWN" and fire["price"] == 31222.0
    assert fire["stop"] == min(31240.0, 31222.0 + 15.0)


# 4 ------------------------------------------------------------------------- #

def test_fvg_gap_opening_before_the_exit_minute_does_not_fire():
    # exit 09:46:17: the 09:45 bar opens a gap (09:45 high 31150 < 09:47 low 31160), but
    # bars before the exit's minute are not fed to the trigger at all
    fu = _open(exit_hms="09:46:17")
    m1 = _m1([(31140, 31150, 31138, 31148),      # 09:45
              (31148, 31175, 31147, 31172),      # 09:46 (the exit's minute)
              (31172, 31190, 31160, 31188)], start="09:45")
    assert fu.on_bars(_ts("09:48:00"), m1) == []


def test_fvg_gap_entirely_after_the_exit_fires_on_the_third_bar_once():
    fu = _open(exit_hms="09:46:17")
    m1 = _m1([(31175, 31178, 31170, 31176),      # 09:46 first bar of the gap
              (31176, 31195, 31175, 31192),      # 09:47 middle
              (31192, 31200, 31185, 31190)],     # 09:48 third: low 31185 > 31178
             start="09:46")
    out = _drive(fu, m1)
    assert out[0] == [] and out[1] == []
    (fire,) = [f for f in out[2] if f["mechanism"] == "followup_fvg_1m"]
    assert fire["price"] == 31190.0 and fire["direction"] == "UP"
    assert fire["time"] == _ts("09:49:00") and fire["followup"] is True
    # stop = lowest low of the three bars - 0.25 = 31169.75, inside the 25-pt cap
    assert fire["stop"] == 31170.0 - 0.25
    assert fu.on_bars(_ts("09:49:00"), m1) == []                    # never twice


def test_fvg_stop_is_capped_at_twenty_five_points():
    fu = _open(exit_hms="09:46:17")
    m1 = _m1([(31175, 31178, 31100, 31176), (31176, 31200, 31175, 31198),
              (31198, 31215, 31185, 31212)], start="09:46")
    (fire,) = [f for f in _drive(fu, m1)[2] if f["mechanism"] == "followup_fvg_1m"]
    assert fire["stop"] == 31212.0 - 25.0


def test_fvg_ignores_a_gap_under_one_tick_and_a_counter_gap():
    fu = _open(exit_hms="09:46:17")
    # first.high 31178.00, third.low 31178.00: no gap; a DOWN gap does not count for a long
    m1 = _m1([(31175, 31178, 31170, 31176), (31176, 31190, 31175, 31188),
              (31188, 31200, 31178, 31195),
              (31195, 31196, 31190, 31191), (31191, 31192, 31150, 31152),
              (31152, 31160, 31140, 31141)], start="09:46")
    out = _drive(fu, m1)
    assert all(f["mechanism"] != "followup_fvg_1m" for fires in out for f in fires)


# 5 ------------------------------------------------------------------------- #

def test_two_stop_outs_exhaust_the_budget():
    fu = _open()
    fu.note_exit(profitable=False)
    assert fu.is_open and fu.attempts == 1
    fu.note_exit(profitable=False)
    assert not fu.is_open and fu.closed_reason == "attempts_exhausted" and fu.attempts == 2
    fu.note_exit(profitable=True)                     # closed stays closed
    assert fu.closed_reason == "attempts_exhausted"


def test_a_profitable_exit_closes_it_positive_close():
    fu = _open()
    fu.note_exit(profitable=True)
    assert not fu.is_open and fu.closed_reason == "positive_close"


def test_a_closed_followup_fires_nothing():
    fu = _open()
    fu.close("window_end")
    rows = [(31170, 31172, 31150, 31155)] * 3 + [(31160, 31165, 31140, 31158)]
    assert all(f == [] for f in _drive(fu, _m1(rows)))
    fu.close("falsified")
    assert fu.closed_reason == "window_end"


# 6 ------------------------------------------------------------------------- #

def test_a_tick_beyond_the_counter_extreme_closes_it_structure_broken():
    fu = _open(counter_extreme=31000.0)
    assert fu.on_tick(high=31200.0, low=31000.0) is None       # touching is not beyond
    assert fu.is_open
    assert fu.on_tick(high=31010.0, low=30999.75) == "structure_broken"
    assert fu.closed_reason == "structure_broken"
    assert fu.on_tick(high=31010.0, low=30990.0) is None       # once


def test_the_structure_veto_for_a_short_reads_the_high():
    fu = _open("DOWN", first_entry=31300.0, t2=31000.0, exit_px=31200.0,
               counter_extreme=31400.0)
    assert fu.on_tick(high=31400.0, low=31300.0) is None
    assert fu.on_tick(high=31400.25, low=31300.0) == "structure_broken"


def test_no_counter_extreme_means_no_structure_veto():
    fu = _open(counter_extreme=None)
    assert fu.on_tick(high=99999.0, low=0.0) is None and fu.is_open
