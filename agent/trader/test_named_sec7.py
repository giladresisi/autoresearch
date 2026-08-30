"""§7's named regression cases, driven over the REAL 1s tape.

The state machine is fed completed 1m bars from `tape.bars` — left-labelled, left-closed,
the pinned convention — with the track chosen by the AGE of the counter-thesis 24h day
extreme measured AT THE PLAN ARM. Expectations come from `named_cases.py`.

These are reproductions of the document's own figures, not of the Executor's integration:
§7's fire is asserted here at mechanism level. What the Executor would then do with it —
the shared attempt budget, the DOL-floor veto, plan scope — is §2/§9 machinery asserted
elsewhere.
"""
import pandas as pd
import pytest

from agent.trader import named_cases as nc
from agent.trader import tape
from agent.trader.extreme_reject import ExtremeReject, choose_track

TZ = "America/New_York"
ARM = "09:20"


def _fires(date, direction, *, end="12:00"):
    """Every §7 fire on `date`, plus the track it followed and the extreme's age."""
    if tape.tape_1s(date) is None:
        pytest.skip(f"no 1s tape for {date}")
    arm = pd.Timestamp(f"{date} {ARM}", tz=TZ)
    price, at = tape.counter_thesis_extreme(date, direction, arm)
    age = None if at is None else arm - at
    track = choose_track(age)
    # On the post-09:30 track the machine starts from the RTH extreme, so it is NOT
    # seeded; on the strict track it carries the standing 24h extreme in.
    m = ExtremeReject(direction, arm, track=track,
                      extreme=price if track == "24h" else None)
    out = []
    for label, bar in tape.bars(date, "1min", end=end).iterrows():
        fire = m.on_bar_close(label + pd.Timedelta(minutes=1), bar)
        if fire is not None:
            out.append(fire)
    return out, track, age


def _hhmmss(ts):
    return pd.Timestamp(ts).strftime("%H:%M:%S")


# --- 08-18: the fallback's founding case ------------------------------------- #

def test_0818_fires_once_the_days_only_entry():
    """Extreme 12.65h old -> fallback ACTIVE. Market short 29760.25 at 09:42:00,
    TP prev1_week_low 29533.5 at 11:01:26, +226.75."""
    case = nc.by_key("sec7-0818")
    fires, track, age = _fires(case.date, "DOWN")
    assert track == "post_0930", f"age {age} should have activated the fallback"
    assert len(fires) == 1, [(_hhmmss(f["time"]), f["price"]) for f in fires]
    assert _hhmmss(fires[0]["time"]) == case.entry_time
    assert fires[0]["price"] == case.entry_price


def test_0818s_stop_is_the_documented_cap15_level():
    """§7: 'cap15 SL 29770 survives the 09:42:24 push (29767.25) by 2.75 pts'."""
    fires, _, _ = _fires("2026-08-18", "DOWN")
    assert fires[0]["stop"] == 29770.0


# --- 08-25: the case that retired the distance key --------------------------- #

def test_0825_fires_and_is_the_case_that_retired_the_distance_key():
    """Extreme 3.55h old -> fallback ACTIVE. Market short 29392.75 at 09:50:00 off the
    09:49 new-extreme red bar, cap15 SL 29407.75 never approached, TP daily_mid
    29218.38 at 10:23:21, +174.37 — the day's ONLY entry."""
    case = nc.by_key("sec7-0825")
    fires, track, age = _fires(case.date, "DOWN")
    assert track == "post_0930"
    assert len(fires) == 1, [(_hhmmss(f["time"]), f["price"]) for f in fires]
    assert _hhmmss(fires[0]["time"]) == case.entry_time
    assert fires[0]["price"] == case.entry_price
    assert fires[0]["stop"] == case.stop


def test_0825s_extreme_is_adjacent_in_DISTANCE_to_0807_but_not_in_AGE():
    """The whole argument for the re-key, checked against the tape rather than quoted.
    08-25's counter-thesis high sits 123.50 pts away and 08-07's 152.50 — adjacent, and
    they need OPPOSITE answers. Their ages, 3.55h and 0.60h, separate cleanly."""
    arm25 = pd.Timestamp("2026-08-25 09:20", tz=TZ)
    arm07 = pd.Timestamp("2026-08-07 09:20", tz=TZ)
    if tape.tape_1s("2026-08-25") is None or tape.tape_1s("2026-08-07") is None:
        pytest.skip("no 1s tape")
    _p25, at25 = tape.counter_thesis_extreme("2026-08-25", "DOWN", arm25)
    _p07, at07 = tape.counter_thesis_extreme("2026-08-07", "DOWN", arm07)
    age25, age07 = (arm25 - at25), (arm07 - at07)
    assert age25 > pd.Timedelta(hours=3) and age07 < pd.Timedelta(hours=1)
    assert choose_track(age25) != choose_track(age07)


# --- 08-07: the documented wrong-arm guard ----------------------------------- #

def test_0807_must_not_activate_the_fallback():
    """Extreme 0.60h old. The strict 24h rule governs; no wrong arm; §5's validated
    +230 ride is untouched. This is the documented wrong-arm guard."""
    case = nc.by_key("sec7-0807-strict")
    fires, track, age = _fires(case.date, "DOWN")
    assert track == "24h", f"age {age} must NOT activate the fallback"
    assert fires == [], [(_hhmmss(f["time"]), f["price"]) for f in fires]


# --- 08-10: §7 takes the day from §6 ----------------------------------------- #

def test_0810_fires_once_at_0942_and_the_price_delta_is_the_fill_convention():
    """§7's own validation: 'armed 09:39; the 09:41 bar prints 29719 and closes green ->
    market 29755.25 at 09:42:00'.

    The machine reports the BAR CLOSE, 29755.75. The 0.50 difference is the documented
    fill convention — §7's re-keyed backcheck records exactly this: '08-10 +95.75 vs the
    recorded +96.25 (1m close vs 1s mid)'. It is an EXPLAINED delta; the Executor fills
    §6/§7 market entries at the 1s mid at placement.
    """
    fires, track, _ = _fires("2026-08-10", "UP")
    assert track == "24h"
    assert len(fires) == 1, [(_hhmmss(f["time"]), f["price"]) for f in fires]
    assert _hhmmss(fires[0]["time"]) == "09:42:00"
    assert abs(fires[0]["price"] - 29755.25) <= 0.50


def test_0810_does_not_arm_before_a_post_open_extreme_is_printed():
    """§7's FIRST clause: 'After 09:30 ET price moves against the thesis and prints a NEW
    day extreme. From the NEXT 1m bar, count consecutive 1m closes ...'.

    Without that gate the seeded overnight low arms the machine off three ordinary
    opening bars and fires at 09:36:00 — two bars before the day's real new low even
    prints. This test is the only thing standing between the implementation and that
    spurious entry.
    """
    fires, _, _ = _fires("2026-08-10", "UP")
    assert all(_hhmmss(f["time"]) >= "09:39:00" for f in fires), \
        "a fire before the first post-open sweep means the quiet counter ran too early"


# --- scope ------------------------------------------------------------------- #

def test_the_fire_is_scoped_to_the_plan():
    """§7 lives and dies with the plan's valid_while. UNSCOPED it fires two losing shorts
    into the 07-21 11:14/11:28 new-day-high rally, hours after the plan completed.

    The assertion is that those late fires EXIST when the machine is driven past the
    plan's life — `len(late) >= len(early)` alone is tautological, since one window is a
    prefix of the other, so it would pass on a day with no late fire at all.
    """
    late, _, _ = _fires("2026-07-21", "DOWN", end="12:00")
    early, _, _ = _fires("2026-07-21", "DOWN", end="11:00")
    after_11 = [f for f in late if _hhmmss(f["time"]) >= "11:00:00"]
    assert after_11, ("07-21 prints no post-11:00 fire, so this test cannot demonstrate "
                      "what plan scope is for — re-pin it against the doc")
    assert len(late) > len(early), "the late fires must be the ones scope suppresses"
    assert all(_hhmmss(f["time"]) < "11:00:00" for f in early)


def test_every_registered_sec7_case_is_exercised_here():
    """The registry names the test that pins each case; this is the other direction —
    no §7 case may be registered without appearing in this module."""
    import inspect

    src = inspect.getsource(__import__(__name__, fromlist=["x"]))
    for case in nc.CASES:
        if case.mechanism != "extreme_reject_close":
            continue
        assert case.key in src, f"{case.key} is registered but never driven here"
