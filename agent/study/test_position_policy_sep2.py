"""Task 4: the 2026-09-02 golden fixture.

**2026-09-02 GENERATED this rule. Its result is an ACCEPTANCE TEST, never evidence.**
Nothing here may be pooled into a corpus statistic; the other 83 sessions are the
evidence (plan 33 §1). A fixture that reproduces the session it was written from proves
the implementation matches the specification, and proves nothing whatsoever about whether
the specification is any good.

**Stated in contract form — given its inputs, expect its outcome — never as a bare
expectation.** That is what makes it independent of §3's D3 surprise: whichever DOL the
target layer ends up producing, this fixture is unchanged, because the DOL is an injected
input (§2.1).

    Given fill 29097.00 UP, initial target 29147.25, DOL 29171.25
    -> exit at 29171.25 on the 10:21 bar, +74.25.

The schedule asserted is the CORRECTED one from §3 (D1/D2/D4/D5), not the user narrative:
the first FVG completes one bar earlier than the narrative says (D1), the narrative
skipped two gaps so the real trail ratchets faster (D2), clause 6 compares against the
offset-adjusted published price (D4), and the buffer is the settled 3 pts, not 5 (D5).
"""
import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study import position_policy as pp                      # noqa: E402

TZ = "America/New_York"
DATE = "2026-09-02"
FILL_TS = pd.Timestamp(f"{DATE} 10:07:00", tz=TZ)
FILL = 29097.00
#: The mechanism's own stop: the 10:04 gap's top edge 29080.50, 16.50 pts away (§3).
INITIAL_STOP = 29080.50
FIXTURE_DOL = 29171.25            # the `day_high` level the user's "~29173" names
INITIAL_TARGET = 29147.25         # `cautious_price_initial`, level `ny_morning_high`

#: §3 D2's corrected ladder: (effective instant, stop, clause).
EXPECTED = [
    ("10:15:00", 29097.00, 3),    # BE, on the 10:14 close -- one bar EARLIER than D1's
    ("10:16:00", 29110.25, 5),    # high(10:12) 29113.25 - 3
    ("10:17:00", 29114.75, 5),    # high(10:13) 29117.75 - 3  == the narrative's "~29115"
    ("10:18:00", 29125.25, 5),    # high(10:14) 29128.25 - 3
    ("10:20:00", 29132.00, 5),    # high(10:15) 29135.00 - 3
    ("10:21:00", 29145.50, 7),    # low(10:20) 29148.50 - 3, at the settled 3-pt buffer
]

pytestmark = pytest.mark.timeout(600)


@pytest.fixture(scope="module")
def frames():
    from agent.study.entries import SessionTape
    try:
        tape = SessionTape()
    except (FileNotFoundError, OSError):                    # pragma: no cover
        pytest.skip("1s parquets unavailable")
    bars_1m, bars_1s = tape.policy_frames(DATE)
    if not len(bars_1s):                                    # pragma: no cover
        pytest.skip("no tape for %s" % DATE)
    return bars_1m, bars_1s


def _run(frames, *, dol, initial_target=INITIAL_TARGET, track="fixture"):
    bars_1m, bars_1s = frames
    return pp.run_policy(bars_1m, bars_1s, date=DATE, track=track, direction="UP",
                         fill_ts=FILL_TS, fill_price=FILL, initial_stop=INITIAL_STOP,
                         dol=dol, initial_target=initial_target)


def _upto_exit(run):
    """The moves that were actually in force. `build_schedule` is pure and keeps
    ratcheting past the exit; a stop move after the position closed is not one."""
    return [m for m in run.moves if m.at <= run.exit_ts]


# --------------------------------------------------------------------------- #
# the contract                                                                  #
# --------------------------------------------------------------------------- #

def test_given_the_fixture_inputs_the_exit_is_the_dol_touch_on_the_1021_bar(frames):
    run = _run(frames, dol=FIXTURE_DOL)
    assert run.exit_reason == pp.EXIT_DOL
    assert run.exit_price == pytest.approx(FIXTURE_DOL)
    assert run.exit_ts.strftime("%H:%M") == "10:21"
    assert run.pnl == pytest.approx(74.25)


def test_the_corrected_ladder_reproduces_every_stop_and_its_instant(frames):
    run = _run(frames, dol=FIXTURE_DOL)
    got = [(m.at.strftime("%H:%M:%S"), m.price, m.clause) for m in _upto_exit(run)]
    assert got == [(t, pytest.approx(p), c) for t, p, c in EXPECTED]


def test_d1_be_fires_one_bar_earlier_than_the_narrative(frames):
    """The narrative names the middle-10:14 FVG completing at 10:15. A bull FVG with
    middle bar 10:13 completes at the 10:14 close, so BE acts at 10:15:00."""
    run = _run(frames, dol=FIXTURE_DOL)
    be = [m for m in run.moves if m.clause == 3]
    assert len(be) == 1
    assert be[0].at == pd.Timestamp(f"{DATE} 10:15:00", tz=TZ)
    assert be[0].price == pytest.approx(FILL)      # the fill price EXACTLY


def test_d2_the_edge_convention_gives_the_narratives_29115_exactly(frames):
    """The narrative's "~29115" is 29114.75 -- but it arrives at 10:17:00, not at the
    10:18:00 the narrative implies. The schedule is one step ahead."""
    run = _run(frames, dol=FIXTURE_DOL)
    at_1017 = [m for m in run.moves
               if m.at == pd.Timestamp(f"{DATE} 10:17:00", tz=TZ)]
    assert len(at_1017) == 1
    assert at_1017[0].price == pytest.approx(29114.75)


def test_d2_no_pre_flip_stop_is_touched(frames):
    """The tightest pre-flip stop (29132.00) sits below every subsequent low."""
    _, bars_1s = frames
    run = _run(frames, dol=FIXTURE_DOL)
    tightest_pre_flip = max(m.price for m in _upto_exit(run) if m.clause in (3, 5))
    assert tightest_pre_flip == pytest.approx(29132.00)
    after = bars_1s[(bars_1s.index >= pd.Timestamp(f"{DATE} 10:20:00", tz=TZ))
                    & (bars_1s.index <= run.exit_ts)]
    assert float(after["Low"].min()) > tightest_pre_flip


def test_d4_the_flip_is_against_the_offset_price_and_lands_on_the_1017_close(frames):
    """SETTLED (clause 6): the offset price 29147.25, not the raw level 29149.25. The
    outcome is identical on this date -- the first qualifying opposite bar is 10:20
    either way -- but the rule needed pinning, and this is what pins it."""
    run = _run(frames, dol=FIXTURE_DOL)
    assert run.target_status == pp.TARGET_PRESENT
    # clause 11: the 10:17 close acts at 10:18:00
    assert run.flipped_at == pd.Timestamp(f"{DATE} 10:18:00", tz=TZ)

    raw_level = _run(frames, dol=FIXTURE_DOL, initial_target=29149.25)
    assert raw_level.flipped_at == pd.Timestamp(f"{DATE} 10:19:00", tz=TZ)
    assert raw_level.pnl == pytest.approx(run.pnl)      # same outcome, different rule


def test_d5_the_1021_stop_is_at_the_settled_three_point_buffer(frames):
    """The narrative used 5 pts (29143.75, itself 0.25 off 29148.50 - 5). At the settled
    3 the stop is 29145.50, and the 10:21 low clears it by 4.00."""
    _, bars_1s = frames
    run = _run(frames, dol=FIXTURE_DOL)
    stop = [m for m in run.moves if m.clause == 7][0]
    assert stop.price == pytest.approx(29148.50 - pp.TRAIL_BUFFER_PTS)
    minute = bars_1s[(bars_1s.index >= pd.Timestamp(f"{DATE} 10:21:00", tz=TZ))
                     & (bars_1s.index < pd.Timestamp(f"{DATE} 10:22:00", tz=TZ))]
    assert float(minute["Low"].min()) == pytest.approx(29149.50)
    assert float(minute["Low"].min()) - stop.price == pytest.approx(4.00)


# --------------------------------------------------------------------------- #
# §4 (b): the two tracks diverge on this very date                              #
# --------------------------------------------------------------------------- #

def test_d3_the_production_track_never_touches_its_dol_and_exits_differently(frames):
    """Production's D1 is `projection_up` 29251.25 -- the day high 29171.25 sits 74.25
    above price, INSIDE the 79.91 draw floor, and is excluded. Price never reaches the
    projection (the post-entry high to 13:00 is 29211.75), so this is the
    no-reachable-target session the design set out to examine. A mean across the two
    tracks would average a DOL exit with a stop exit; §4 (b) is what stops that."""
    _, bars_1s = frames
    fixture = _run(frames, dol=FIXTURE_DOL)
    production = _run(frames, dol=29251.25, track="production")

    assert production.exit_reason != fixture.exit_reason
    assert production.exit_reason == pp.EXIT_STOP
    assert production.exit_ts != fixture.exit_ts
    assert production.pnl != pytest.approx(fixture.pnl)

    window = bars_1s[(bars_1s.index >= FILL_TS)
                     & (bars_1s.index <= pd.Timestamp(f"{DATE} 13:00", tz=TZ))]
    assert float(window["High"].max()) == pytest.approx(29211.75)
    assert float(window["High"].max()) < 29251.25          # the DOL is never reached


def test_the_two_tracks_share_every_pre_flip_stop(frames):
    """The DOL enters the schedule only through clause 4, which never fires here, so the
    tracks differ in their EXIT and not in their trail. That is what makes the
    comparison a comparison of the policy rather than of two different trades."""
    fixture = _run(frames, dol=FIXTURE_DOL)
    production = _run(frames, dol=29251.25, track="production")
    pre = lambda r: [(m.at, m.price, m.clause) for m in r.moves
                     if m.at <= fixture.exit_ts]
    assert pre(production) == pre(fixture)


# --------------------------------------------------------------------------- #
# the inputs themselves, so a data change fails loudly rather than silently      #
# --------------------------------------------------------------------------- #

def test_the_fixtures_own_inputs_still_hold_on_the_tape(frames):
    """§3's "Confirmed exactly" rows. If the parquet is revised under us, this fails
    before any of the schedule assertions do, so the failure names the cause."""
    bars_1m, _ = frames
    assert float(bars_1m.loc[pd.Timestamp(f"{DATE} 10:06", tz=TZ), "Close"]) \
        == pytest.approx(FILL)                             # fill == the 10:06 close
    ny_high = bars_1m[(bars_1m.index >= pd.Timestamp(f"{DATE} 08:21", tz=TZ))
                      & (bars_1m.index < pd.Timestamp(f"{DATE} 08:22", tz=TZ))]
    assert float(ny_high["High"].max()) == pytest.approx(29149.25)
    tenten = bars_1m.loc[pd.Timestamp(f"{DATE} 10:21", tz=TZ)]
    assert float(tenten["High"]) >= FIXTURE_DOL            # the target is swept 10:21
