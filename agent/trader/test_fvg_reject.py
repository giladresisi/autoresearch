"""`fvg_1h_reject`: the zone read, the signature, and the opening-bar exclusion."""
import pandas as pd
import pytest

from agent.trader.fvg_reject import FvgReject, SKIP_OPENING_BAR, zone_at_0700
from agent.trader.reject_core import capped_stop, closes_with_thesis

TZ = "America/New_York"
DATE = "2026-09-02"
ZONE = {"kind": "bull", "lo": 28995.50, "hi": 29033.00}


def _bar(o, h, l, c):
    return pd.Series({"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1.0})


def _close(hhmm):
    return pd.Timestamp(f"{DATE} {hhmm}", tz=TZ) + pd.Timedelta(minutes=1)


# -- the zone, from the real tape ------------------------------------------------ #

def test_zone_at_0700_matches_the_hand_checked_0902_values():
    from backtest_smt import _main_dir_for_date
    mnq = pd.read_parquet(_main_dir_for_date(DATE) / "MNQ_1s.parquet")
    z = zone_at_0700(mnq, pd.Timestamp(f"{DATE} 09:20", tz=TZ))
    assert z is not None
    assert z["kind"] == "bull"
    assert z["lo"] == pytest.approx(28995.50)
    assert z["hi"] == pytest.approx(29033.00)


def test_zone_is_not_visible_before_its_third_bar_completes():
    """§2's convention: IDENTITY is the middle bar, EXISTENCE is third-bar completion.
    The 07:00 gap's third bar is 08:00, which completes at 09:00."""
    from backtest_smt import _main_dir_for_date
    mnq = pd.read_parquet(_main_dir_for_date(DATE) / "MNQ_1s.parquet")
    assert zone_at_0700(mnq, pd.Timestamp(f"{DATE} 08:30", tz=TZ)) is None
    assert zone_at_0700(mnq, pd.Timestamp(f"{DATE} 09:00", tz=TZ)) is not None


# -- the signature ---------------------------------------------------------------- #

def test_fires_on_a_new_extreme_inside_the_zone_that_closes_back():
    """0902's 09:55 bar: low 29017.25 inside [28995.5, 29033], closes up on its own open."""
    m = FvgReject("UP")
    assert m.on_bar_close(_close("09:54"), _bar(29041.5, 29044.25, 29025.0, 29029.5),
                          ZONE) is None                      # inside, but closes DOWN
    fire = m.on_bar_close(_close("09:55"), _bar(29030.0, 29034.5, 29017.25, 29030.75),
                          ZONE)
    assert fire is not None
    assert fire["mechanism"] == "fvg_1h_reject"
    assert fire["direction"] == "UP"
    assert fire["price"] == pytest.approx(29030.75)
    assert fire["time"] == _close("09:55")                   # entry at 09:56:00
    # stop: swept extreme 29017.25 - 3 = 29014.25, well inside the 25-pt cap
    assert fire["stop"] == pytest.approx(29014.25)


def test_does_not_fire_without_a_new_extreme():
    m = FvgReject("UP")
    m.on_bar_close(_close("09:40"), _bar(29030.0, 29035.0, 29020.0, 29032.0), ZONE)
    m._fired = False                                          # ignore the latch here
    # higher low: inside the zone and closes up, but it is not a NEW extreme
    assert m.on_bar_close(_close("09:41"), _bar(29030.0, 29035.0, 29025.0, 29034.0),
                          ZONE) is None


def test_does_not_fire_when_the_extreme_is_outside_the_zone():
    m = FvgReject("UP")
    assert m.on_bar_close(_close("09:45"), _bar(29060.0, 29065.0, 29040.0, 29062.0),
                          ZONE) is None                      # low 29040 > zone top


def test_does_not_fire_on_a_counter_direction_close():
    m = FvgReject("UP")
    assert m.on_bar_close(_close("09:55"), _bar(29030.0, 29034.5, 29017.25, 29029.0),
                          ZONE) is None                      # closes DOWN


def test_a_bear_zone_is_not_a_continuation_zone_for_an_up_plan():
    m = FvgReject("UP")
    bear = {"kind": "bear", "lo": 28995.50, "hi": 29033.00}
    assert m.on_bar_close(_close("09:55"), _bar(29030.0, 29034.5, 29017.25, 29030.75),
                          bear) is None


def test_it_fires_at_most_once():
    m = FvgReject("UP")
    first = m.on_bar_close(_close("09:55"), _bar(29030.0, 29034.5, 29017.25, 29030.75),
                           ZONE)
    assert first is not None
    again = m.on_bar_close(_close("10:20"), _bar(29020.0, 29025.0, 29010.0, 29024.0),
                           ZONE)
    assert again is None


# -- the opening-bar exclusion ---------------------------------------------------- #

def test_the_0930_bar_cannot_fire():
    """Both measured failures (07-02, 08-20) fired here. On the session's first bar every
    extreme is new by construction, so the signature carries no information."""
    m = FvgReject("UP")
    hh, mm = SKIP_OPENING_BAR
    opening = pd.Timestamp(f"{DATE} {hh:02d}:{mm:02d}", tz=TZ) + pd.Timedelta(minutes=1)
    assert m.on_bar_close(opening, _bar(29030.0, 29034.5, 29017.25, 29030.75),
                          ZONE) is None


def test_the_opening_bar_still_seeds_the_running_extreme():
    """Excluding it from FIRING must not exclude it from the extreme, or the 09:31 bar
    simply inherits 'first extreme of the session' and the problem moves one bar."""
    m = FvgReject("UP")
    hh, mm = SKIP_OPENING_BAR
    opening = pd.Timestamp(f"{DATE} {hh:02d}:{mm:02d}", tz=TZ) + pd.Timedelta(minutes=1)
    m.on_bar_close(opening, _bar(29030.0, 29034.5, 29017.25, 29030.75), ZONE)
    assert m.state()["extreme"] == pytest.approx(29017.25)
    # 09:31 makes a HIGHER low, so it is not a new extreme and must not fire
    assert m.on_bar_close(_close("09:31"), _bar(29025.0, 29032.0, 29020.0, 29031.0),
                          ZONE) is None


# -- the shared vocabulary --------------------------------------------------------- #

def test_capped_stop_takes_the_nearer_of_structural_and_cap():
    # long, anchor far away -> the cap binds
    assert capped_stop(29030.75, 28900.0, buffer_pts=3.0, cap_pts=25.0,
                       short=False) == pytest.approx(29005.75)
    # long, anchor close -> the structural stop binds
    assert capped_stop(29030.75, 29017.25, buffer_pts=3.0, cap_pts=25.0,
                       short=False) == pytest.approx(29014.25)
    # short mirrors
    assert capped_stop(100.0, 110.0, buffer_pts=3.0, cap_pts=25.0,
                       short=True) == pytest.approx(113.0)


def test_closes_with_thesis_is_against_the_bars_own_open():
    assert closes_with_thesis(_bar(10, 12, 9, 11), short=False)
    assert not closes_with_thesis(_bar(10, 12, 9, 9.5), short=False)
    assert closes_with_thesis(_bar(10, 12, 9, 9.5), short=True)
