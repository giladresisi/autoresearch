"""Task 7: §6 validated against §6.2's CURRENT-rules column, over the REAL 1s tape.

§6.2 is explicit that the four recorded §6 day figures were produced under rule sets that
no longer exist and that TWO of the four change under current rules BY DESIGN. Everything
here reads the CURRENT column, never the recorded one.

WHAT THIS MODULE DOES AND DOES NOT CLAIM. It drives `Episode` over each day's real
candidate gaps and asserts the entries §6.2 names — the exact runaway fill, the exit-tick
second, the candidate the height raise admits, the veto that silences 08-10. It does NOT
run the whole day's P&L: the shared attempt budget, the stop-out cooldown and the plan's
death all live in the Executor, and §6 is not wired into its entry path in this cycle.
Every figure that therefore cannot be reproduced is registered in
`named_cases.NO_DAY_REPRODUCTION` rather than quietly asserted around.
"""
import pandas as pd
import pytest

from agent.facts.detectors.fvg import detect_fvgs, update_fvg_states
from agent.trader import named_cases as nc
from agent.trader import tape
from agent.trader.episode import Episode, evaluation_order
from agent.trader.executor import (DOL_FLOOR_PTS, MAX_FVG_HEIGHT_PTS,
                                   MIN_FVG_HEIGHT_PTS)

TZ = "America/New_York"
MAX_HEIGHT_MAX35_ERA = 35.0       # the knob §6's records were made under


def _skip_without_tape(date):
    if tape.tape_1s(date) is None:
        pytest.skip(f"no 1s tape for {date}")


def adverse_extreme(date, direction, *, start="09:30", end="12:00"):
    """The FIRST post-open instant price prints a NEW 24h day extreme against the thesis.

    The judas sweep, not the session's global maximum. Taking the maximum instead picks
    up 07-21's 11:27 rally high — hours after the plan completed — and leaves §6 with no
    candidates at all, which is the same scope error §7 warns about.
    """
    prior, _ = tape.counter_thesis_extreme(
        date, direction, pd.Timestamp(f"{date} {start}", tz=TZ))
    win = tape.window_1s(date, start, end)
    if str(direction).upper() == "DOWN":
        hit = win[win["High"] > prior]
    else:
        hit = win[win["Low"] < prior]
    if not len(hit):
        return None
    return hit.index[0]


def candidates(date, direction, since, *, end="12:00", max_height=MAX_FVG_HEIGHT_PTS):
    """Thesis-direction (continuation) 1m FVGs created since the adverse extreme.

    EXISTENCE-side, per §2: a gap created since the extreme is one whose THIRD bar
    completed after it. `max_height` is a parameter only so 08-06's era interaction can
    be measured — it is not a knob this cycle moves.
    """
    want = "bear" if str(direction).upper() == "DOWN" else "bull"
    bars = tape.bars(date, "1min", start="09:00", end=end)
    facts, _ = detect_fvgs(bars, "1min", "MNQ", {})
    update_fvg_states(facts, bars, "1min")
    out = []
    for f in facts:
        if f.extra["direction"] != want:
            continue
        if pd.Timestamp(f.extra["exists_from"]) < since:
            continue
        height = float(f.price_high) - float(f.price_low)
        if not (MIN_FVG_HEIGHT_PTS <= height <= max_height):
            continue
        out.append(f)
    return out


def fires(date, direction, *, end="12:00", max_height=MAX_FVG_HEIGHT_PTS):
    """Every §6 fire on `date`, in tape order, with no spine gating applied."""
    _skip_without_tape(date)
    since = adverse_extreme(date, direction, end=end)
    assert since is not None, f"{date}: no adverse day extreme printed"
    cands = candidates(date, direction, since, end=end, max_height=max_height)
    eps = {c.id: Episode(c.price_low, c.price_high, direction, gap_id=c.id)
           for c in cands}
    born = {c.id: pd.Timestamp(c.extra["exists_from"]) for c in cands}

    ticks = tape.window_1s(date, "09:30", end)
    bars = tape.bars(date, "1min", start="09:30", end=end)
    opens = {label: float(b["Open"]) for label, b in bars.iterrows()}

    out, minute = [], None
    for ts, row in ticks.iterrows():
        cur = ts.floor("1min")
        if minute is not None and cur != minute and minute in bars.index:
            bar = bars.loc[minute]
            close_at = minute + pd.Timedelta(minutes=1)
            mid = (float(bar["High"]) + float(bar["Low"])) / 2.0
            for ep in evaluation_order(list(eps.values())):
                if born[ep.gap_id] > close_at:
                    continue
                fire = ep.on_bar_close(close_at, bar, mid=mid)
                if fire:
                    out.append(fire)
                    break
        minute = cur
        mid = (float(row["High"]) + float(row["Low"])) / 2.0
        for ep in evaluation_order(list(eps.values())):
            if born[ep.gap_id] > ts:
                continue
            fire = ep.on_tick(ts, float(row["Close"]), bar_open=opens.get(cur), mid=mid)
            if fire:
                out.append(fire)
                break
    return out, cands


def _at(fires_, hhmmss):
    return [f for f in fires_ if f["time"].strftime("%H:%M:%S") == hhmmss]


# --- 07-21 -------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def f0721():
    return fires("2026-07-21", "DOWN", end="10:30")[0]


def test_0721s_runaway_leg_is_exact(f0721):
    """§6.2's CURRENT row: 'runaway leg exact (29149.75, -30.00)'. §6.1 clause 2 pins the
    price: gap B fills 29174.75 - 25 = 29149.75, the TRIGGER, not the market mid."""
    runaways = [f for f in f0721 if f["kind"] == "early_runaway"]
    assert runaways, "the early-runaway leg did not fire at all"
    first = runaways[0]
    assert first["price"] == 29149.75
    assert first["stop"] - first["price"] == 30.00, "the 30-pt SL cap gives -30.00"
    assert first["time"].strftime("%H:%M") == "09:33"


def test_0721s_winning_exit_tick_entry_is_at_the_documented_second(f0721):
    """§6: 'gap A exit-tick entry 29199 at 09:39:11'. The SECOND is the assertion; the
    price differs by the fill convention (§6 prices market fills at the mid of the 1s bar
    at placement, and the recorded 29199 is the coarser reading)."""
    got = _at(f0721, "09:39:11")
    assert got, [f["time"].strftime("%H:%M:%S") for f in f0721][:8]
    assert got[0]["kind"] == "exit_tick"
    assert abs(got[0]["price"] - 29199.0) <= 2.00


def test_0721_within_one_cycle_of_the_current_rules_figure(f0721):
    """+61.00 on three entries; the runaway leg must be EXACT. The extra 09:38:00
    close-verdict cycle is unexplained by any stated rule and is an ACCEPTED TOLERANCE —
    the doc says treat a reproduction within one cycle as passing. Do NOT chase it.

    This implementation REPRODUCES that anomaly rather than diverging from it, which is
    the strongest available evidence that the two readings of §6.1 agree: the extra cycle
    is a property of the specified rules, not of either implementation.
    """
    case = nc.by_key("sec6-0721-current")
    assert case.key in nc.ACCEPTED_TOLERANCES
    extra = _at(f0721, "09:38:00")
    assert extra and extra[0]["kind"] == "close_verdict", \
        "the documented extra 09:38:00 close-verdict cycle was NOT reproduced"


def test_0721s_first_three_fires_are_the_documented_ones(f0721):
    seq = [(f["time"].strftime("%H:%M:%S"), f["kind"]) for f in f0721[:3]]
    assert seq == [("09:33:55", "early_runaway"),
                   ("09:38:00", "close_verdict"),
                   ("09:39:11", "exit_tick")], seq


# --- 08-06: the era interaction, and §6.1's named trap ------------------------ #

def test_0806_adds_the_negative_cycle_the_height_raise_admits():
    """One entry, +125.12. At max height 45 the 36.75-pt gap [29375.25, 29412.00]
    becomes a candidate and adds a -26.12 cycle the max-35 era never saw.

    This is §6.2's sharpest lesson, and it is checkable directly: the gap is a candidate
    under the CURRENT ceiling and is not one under the ceiling its validation was made
    against.
    """
    _skip_without_tape("2026-08-06")
    since = adverse_extreme("2026-08-06", "UP", end="10:30")
    at45 = candidates("2026-08-06", "UP", since, end="10:30")
    at35 = candidates("2026-08-06", "UP", since, end="10:30",
                      max_height=MAX_HEIGHT_MAX35_ERA)

    def _has(cands):
        return any(abs(c.price_low - 29375.25) < 0.01
                   and abs(c.price_high - 29412.0) < 0.01 for c in cands)

    assert _has(at45), "the 36.75-pt gap must be a candidate at the current ceiling"
    assert not _has(at35), "and must NOT be one under the max-35 era"
    assert MAX_FVG_HEIGHT_PTS == 45.0


def test_0806_fires_no_phantom_runaway_before_price_enters_the_gap():
    """§6.1 clause 1's named trap: 08-06's 09:38 bar prints 29437.00 (= gap top + 25) at
    09:38:14, THIRTEEN SECONDS before price enters the gap at 09:38:27. Scanning the
    whole bar fires a phantom runaway on a print that preceded the episode."""
    got, _ = fires("2026-08-06", "UP", end="10:30")
    early = [f for f in got if f["time"] < pd.Timestamp("2026-08-06 09:38:27", tz=TZ)]
    assert early == [], \
        [(f["time"].strftime("%H:%M:%S"), f["kind"], f["price"]) for f in early]


def test_0806s_runaway_is_the_trigger_price_not_the_mid():
    """Clause 2 again, on a second day: the fill is gap top + 25 exactly."""
    got, _ = fires("2026-08-06", "UP", end="10:30")
    runaways = [f for f in got if f["kind"] == "early_runaway"]
    assert runaways and runaways[0]["price"] == 29412.0 + 25.0


# --- 08-10: the DOL-floor veto owns the day ---------------------------------- #

def test_0810_does_not_fire_at_all():
    """CORRECT under current rules: the 09:51/09:59 chase entries are suppressed by the
    §2 DOL-floor veto at 45.5 pts remaining, and the day belongs to §7 (+96.25).

    Asserted the way the veto actually works — every §6 fire BEFORE the DOL is reached
    leaves less than the 60-pt floor between its entry and the target.
    """
    case = nc.by_key("sec6-0810-no-fire")
    assert case.expect == "no_fire"
    dol = nc.thesis_for("sec6-0810-no-fire")["dol"]["price"]
    got, _ = fires("2026-08-10", "UP", end="10:30")
    before_dol = [f for f in got if f["price"] < dol]
    assert before_dol, "no §6 fire at all — the veto claim would be vacuous"
    for fire in before_dol:
        remaining = dol - fire["price"]
        assert remaining < DOL_FLOOR_PTS, (
            f"{fire['time']} @ {fire['price']} leaves {remaining:.2f} pts, which the "
            f"{DOL_FLOOR_PTS}-pt floor would ALLOW")


def test_0810s_veto_margin_is_the_documented_one():
    """§6 records 'the 09:51/09:59 chase entries are veto-suppressed at 45.5 pts
    remaining'. The WIDEST margin is the one that decides the day — the entry closest to
    clearing the floor — so it is pinned exactly rather than left inside a 60-pt band,
    which would pass on a completely different entry set.

    EXPLAINED DELTA, and it is not the fill convention. The widest margin here is 53.25,
    not 45.5, because this harness applies NO spine gating: without the stop-out cooldown
    and the shared attempt budget the episodes keep cycling, so the fire SET is a superset
    of the two entries §6 names and its widest member is a different, earlier one. The
    VERDICT is unchanged and is what §6.2's row asserts — every fire is under the floor,
    so the day does not fire — and 53.25 < 60.00 with 6.75 pts to spare.

    Pinning the observed value makes this a change detector; claiming it reproduces 45.5
    would be claiming a spine this cycle did not wire.
    """
    dol = nc.thesis_for("sec6-0810-no-fire")["dol"]["price"]
    got, _ = fires("2026-08-10", "UP", end="10:30")
    margins = [dol - f["price"] for f in got if f["price"] < dol]
    assert margins, "no §6 fire before the DOL — nothing to veto"
    assert min(margins) > 0.0
    assert abs(max(margins) - 53.25) <= 0.01, f"widest margin {max(margins):.2f}"
    assert max(margins) < DOL_FLOOR_PTS


# --- 08-05: a recorded validation gap ---------------------------------------- #

def test_0805_is_registered_as_unreproducible_with_its_reason():
    """+235.38 on ONE entry cannot be checked here: §6 records 08-05's outcome but never
    its DOL, and no recorded entry price lets one be derived. Registered, not skipped
    silently — an unreproducible case is a validation GAP, and a gap nobody wrote down
    becomes a claim nobody checked."""
    assert "sec6-0805-current" in nc.NO_DAY_REPRODUCTION
    assert "sec6-0805-current" in nc.NO_ORACLE_THESIS
    case = nc.by_key("sec6-0805-current")
    assert case.pnl == 235.38 and case.explained_delta
