"""Task 6: §6's episode machine and §6.1's four normative clauses.

Short side unless stated. The gap is [29100.00, 29120.00]; for a short the near edge AND
the exit-side edge are both 29100.00, so the early-runaway trigger is 29075.00.
"""
import pandas as pd
import pytest

from agent.trader.episode import (EARLY_RUNAWAY_PTS, SL_BUFFER_PTS, SL_CAP_PTS,
                                  Episode, evaluation_order)

TZ = "America/New_York"
LO, HI = 29100.0, 29120.0


def _t(s):
    return pd.Timestamp(f"2026-07-21 {s}", tz=TZ)


def _bar(open_, close, high=None, low=None):
    return {"Open": open_, "Close": close,
            "High": high if high is not None else max(open_, close),
            "Low": low if low is not None else min(open_, close)}


def _short(**kw):
    return Episode(LO, HI, "DOWN", gap_id="A", **kw)


def _long(**kw):
    return Episode(LO, HI, "UP", gap_id="A", **kw)


def _enter(ep, price=29110.0, at="09:33:10", bar_open=29105.0):
    ep.on_tick(_t(at), price, bar_open=bar_open)
    return ep


# --- the episode begins ------------------------------------------------------ #

def test_an_episode_begins_only_strictly_inside_the_gap():
    ep = _short()
    ep.on_tick(_t("09:33:05"), 29095.0, bar_open=29095.0)     # below, outside
    assert ep.state()["cycle"] == "idle"
    ep.on_tick(_t("09:33:10"), 29110.0, bar_open=29095.0)
    assert ep.state()["cycle"] == "entering"


def test_an_edge_touch_does_not_begin_an_episode():
    """'at least one tick beyond the near edge; edge touches don't count'."""
    ep = _short()
    ep.on_tick(_t("09:33:10"), LO, bar_open=29095.0)
    assert ep.state()["cycle"] == "idle"


def test_a_long_mirrors_the_near_edge():
    ep = _long()
    ep.on_tick(_t("09:33:10"), HI, bar_open=29125.0)
    assert ep.state()["cycle"] == "idle"
    ep.on_tick(_t("09:33:11"), 29110.0, bar_open=29125.0)
    assert ep.state()["cycle"] == "entering"


def test_no_traversal_of_the_gap_is_required():
    ep = _short()
    ep.on_tick(_t("09:33:10"), LO + 0.25, bar_open=29095.0)
    assert ep.state()["cycle"] == "entering"


# --- clause 1: intra-bar ordering -------------------------------------------- #

def test_the_excursion_extreme_is_tracked_from_the_entering_tick_only():
    ep = _short()
    ep.on_tick(_t("09:33:01"), 29200.0, bar_open=29105.0)    # BEFORE the episode
    _enter(ep, 29110.0, "09:33:10")
    assert ep.state()["excursion"] == 29110.0
    ep.on_tick(_t("09:33:20"), 29118.0, bar_open=29105.0)
    assert ep.state()["excursion"] == 29118.0


def test_a_print_before_the_entering_tick_cannot_fire_a_runaway():
    """08-06's 09:38 bar prints gap-top+25 at 09:38:14, THIRTEEN SECONDS before price
    enters at 09:38:27. Scanning the whole bar fires a phantom runaway."""
    ep = _long()
    early = HI + EARLY_RUNAWAY_PTS + 1
    assert ep.on_tick(_t("09:38:14"), early, bar_open=29110.0) is None
    assert ep.state()["cycle"] == "idle", "the phantom print must not start anything"
    _enter(ep, 29110.0, "09:38:27", bar_open=29110.0)
    assert ep.state()["excursion"] == 29110.0


# --- the entering bar -------------------------------------------------------- #

def test_the_entering_bar_never_triggers_intra_bar():
    """Only the early-runaway exception fires before its close."""
    ep = _enter(_short())
    assert ep.on_tick(_t("09:33:40"), 29099.0, bar_open=29105.0) is None
    assert ep.pending_entry() is None


def test_the_entering_bar_enters_on_a_red_close_beyond_the_exit_side():
    ep = _enter(_short())
    fire = ep.on_bar_close(_t("09:34:00"), _bar(29112.0, 29095.0), mid=29094.5)
    assert fire is not None and fire["kind"] == "close_verdict"
    assert fire["price"] == 29094.5


def test_a_with_trend_coloured_close_skips_and_voids_the_cycle():
    """07-21: the 09:36 green skip required the 09:38->09:39 re-entry before the winning
    exit-tick entry. The cycle is CONSUMED even though price now sits beyond the gap."""
    ep = _enter(_short())
    assert ep.on_bar_close(_t("09:34:00"), _bar(29090.0, 29095.0)) is None
    assert ep.state()["cycle"] == "idle" and ep.state()["voided"] == 1


def test_a_skip_voids_the_cycle_and_no_entry_fires_on_carried_over_state():
    """§2's crossed-trigger rule does NOT apply within this mechanism."""
    ep = _enter(_short())
    ep.on_bar_close(_t("09:34:00"), _bar(29090.0, 29095.0))       # green skip -> void
    assert ep.on_tick(_t("09:34:05"), 29080.0) is None            # already beyond exit
    assert ep.pending_entry() is None


def test_a_fresh_re_entry_after_a_void_starts_a_new_cycle():
    ep = _enter(_short())
    ep.on_bar_close(_t("09:34:00"), _bar(29090.0, 29095.0))
    ep.on_tick(_t("09:34:30"), 29110.0, bar_open=29095.0)
    assert ep.state()["cycle"] == "entering"


def test_an_entering_bar_closing_inside_leaves_the_cycle_live():
    """08-14 / 07-17: 'the 09:31 entering bar closes inside -> cycle live'."""
    ep = _enter(_short())
    assert ep.on_bar_close(_t("09:34:00"), _bar(29112.0, 29110.0)) is None
    assert ep.state()["cycle"] == "live"


def test_an_adverse_close_beyond_the_gap_does_not_invert_the_candidate():
    """Validated 08-05: the winning entry came two bars AFTER a close above the gap
    top. Adverse 1m closes do NOT invert a candidate for this mechanism."""
    ep = _enter(_short())
    ep.on_bar_close(_t("09:34:00"), _bar(29110.0, 29130.0))   # green, above the top
    assert ep.state()["cycle"] == "live"


# --- the early runaway ------------------------------------------------------- #

def test_early_runaway_fires_at_25_pts_beyond_the_exit_edge():
    ep = _enter(_short(), bar_open=29105.0)
    fire = ep.on_tick(_t("09:33:40"), LO - EARLY_RUNAWAY_PTS, bar_open=29105.0)
    assert fire is not None and fire["kind"] == "early_runaway"


def test_early_runaway_fills_at_the_trigger_price_not_the_mid():
    """07-21 gap B fills 29149.75 = 29174.75 - 25 exactly."""
    ep = Episode(29174.75, 29190.0, "DOWN", gap_id="B")
    ep.on_tick(_t("09:33:10"), 29180.0, bar_open=29178.0)
    fire = ep.on_tick(_t("09:33:40"), 29140.0, bar_open=29178.0, mid=29139.5)
    assert fire["price"] == 29149.75


def test_early_runaway_requires_price_beyond_the_bars_open():
    """Load-bearing: it blocked a false fire on 08-05 where the runaway price was still
    above the bar's open."""
    ep = _enter(_short(), bar_open=29050.0)         # open already below the trigger
    assert ep.on_tick(_t("09:33:40"), LO - EARLY_RUNAWAY_PTS, bar_open=29050.0) is None


def test_early_runaway_does_not_fire_short_of_the_trigger():
    ep = _enter(_short(), bar_open=29105.0)
    assert ep.on_tick(_t("09:33:40"), LO - EARLY_RUNAWAY_PTS + 0.25,
                      bar_open=29105.0) is None


# --- subsequent bars --------------------------------------------------------- #

def _live(ep=None, prev=None):
    ep = ep or _enter(_short())
    ep.on_bar_close(_t("09:34:00"), prev or _bar(29112.0, 29110.0))
    return ep


def test_a_subsequent_bar_exit_tick_fires_when_the_previous_closed_inside():
    ep = _live()
    fire = ep.on_tick(_t("09:34:30"), 29095.0, mid=29094.75)
    assert fire is not None and fire["kind"] == "exit_tick"
    assert fire["price"] == 29094.75


def test_a_subsequent_bar_exit_tick_fires_when_the_previous_closed_with_thesis():
    ep = _live(prev=_bar(29115.0, 29105.0))            # red, inside
    assert ep.on_tick(_t("09:34:30"), 29095.0) is not None


def test_a_with_trend_close_beyond_the_adverse_side_defers_to_the_current_close():
    ep = _live(prev=_bar(29110.0, 29130.0))            # green, ABOVE the gap top
    assert ep.state()["defer"] is True
    assert ep.on_tick(_t("09:34:30"), 29095.0) is None, "no intra-bar entry while deferred"
    fire = ep.on_bar_close(_t("09:35:00"), _bar(29125.0, 29090.0), mid=29089.5)
    assert fire is not None and fire["kind"] == "close_verdict"


def test_a_deferred_bar_that_does_not_close_beyond_the_exit_side_enters_nothing():
    ep = _live(prev=_bar(29110.0, 29130.0))
    assert ep.on_bar_close(_t("09:35:00"), _bar(29125.0, 29110.0)) is None
    assert ep.state()["cycle"] == "live"


def test_an_adverse_close_beyond_the_gap_that_is_red_still_arms_the_exit_tick():
    """The defer branch needs BOTH conditions: beyond the ADVERSE side AND
    with-trend-coloured. A red close above the top is still conviction."""
    ep = _live(prev=_bar(29140.0, 29130.0))            # red, above the top
    assert ep.state()["defer"] is False


# --- the stop ---------------------------------------------------------------- #

def test_the_stop_is_the_excursion_extreme_plus_two():
    ep = _enter(_short(), 29110.0)
    ep.on_tick(_t("09:33:20"), 29118.0, bar_open=29105.0)
    fire = ep.on_bar_close(_t("09:34:00"), _bar(29112.0, 29095.0), mid=29095.0)
    assert fire["stop"] == 29118.0 + SL_BUFFER_PTS


def test_the_stop_is_capped_at_30_pts_from_entry():
    ep = _enter(_short(), 29110.0)
    ep.on_tick(_t("09:33:20"), 29200.0, bar_open=29105.0)     # 90 pts of excursion
    fire = ep.on_bar_close(_t("09:34:00"), _bar(29112.0, 29095.0), mid=29095.0)
    assert fire["stop"] == 29095.0 + SL_CAP_PTS


def test_a_long_stop_mirrors():
    ep = _long()
    ep.on_tick(_t("09:33:10"), 29110.0, bar_open=29115.0)
    ep.on_tick(_t("09:33:20"), 29104.0, bar_open=29115.0)
    fire = ep.on_bar_close(_t("09:34:00"), _bar(29108.0, 29125.0), mid=29125.0)
    assert fire["stop"] == 29104.0 - SL_BUFFER_PTS


# --- §8's SL-cap gate (re-entry mode ONLY) ----------------------------------- #

def test_a_close_verdict_entry_over_the_30pt_cap_is_skipped_in_reentry_mode():
    """08-14: the 09:46 red close (extreme 30275.5, dist 36) and 09:49 (dist 43.5)."""
    ep = _short(sl_cap_gate=True)
    _enter(ep, 29110.0)
    ep.on_tick(_t("09:33:20"), 29200.0, bar_open=29105.0)     # excursion 90 pts away
    assert ep.on_bar_close(_t("09:34:00"), _bar(29112.0, 29095.0), mid=29095.0) is None
    assert ep.state()["sl_cap_skips"] == 1
    assert ep.state()["cycle"] == "idle", "the cycle is CONSUMED"


def test_an_exit_tick_entry_is_never_length_gated():
    """07-17: the 51-pt breakdown bar enters via exit-tick; a fixed length gate forfeits
    that day's entire winner."""
    ep = _short(sl_cap_gate=True)
    _enter(ep, 29110.0)
    ep.on_tick(_t("09:33:20"), 29200.0, bar_open=29105.0)
    ep.on_bar_close(_t("09:34:00"), _bar(29112.0, 29110.0))   # closes inside -> live
    fire = ep.on_tick(_t("09:34:30"), 29095.0, mid=29095.0)
    assert fire is not None and fire["kind"] == "exit_tick"


def test_an_early_runaway_is_never_length_gated():
    ep = _short(sl_cap_gate=True)
    _enter(ep, 29110.0, bar_open=29112.0)
    ep.on_tick(_t("09:33:20"), 29200.0, bar_open=29112.0)
    fire = ep.on_tick(_t("09:33:40"), LO - EARLY_RUNAWAY_PTS, bar_open=29112.0)
    assert fire is not None and fire["kind"] == "early_runaway"


def test_sec6_proper_does_NOT_apply_the_sl_cap_gate():
    """§11.0's DO-NOT-IMPLEMENT list: extending the gate to §6-proper is idea only with
    zero net evidence, and on 08-06 it would have skipped a WINNER."""
    ep = _short()                                             # sl_cap_gate defaults off
    _enter(ep, 29110.0)
    ep.on_tick(_t("09:33:20"), 29200.0, bar_open=29105.0)
    assert ep.on_bar_close(_t("09:34:00"), _bar(29112.0, 29095.0),
                           mid=29095.0) is not None


# --- clause 3: the cooldown -------------------------------------------------- #

def test_the_cooldown_resets_every_cycle_to_idle():
    ep = _live()
    ep.reset_cycle()
    assert ep.state()["cycle"] == "idle"
    assert ep.on_tick(_t("09:34:30"), 29095.0) is None, "a fresh re-entry is required"


def test_the_cooldown_reset_keeps_the_episodes_excursion():
    """The excursion measures how deep the rejection ran; it anchors the stop and is not
    a property of a cycle."""
    ep = _enter(_short(), 29110.0)
    ep.on_tick(_t("09:33:20"), 29125.0, bar_open=29105.0)
    ep.reset_cycle()
    assert ep.state()["excursion"] == 29125.0


# --- clause 4: candidate ordering -------------------------------------------- #

def test_the_most_recently_entered_gap_is_evaluated_first():
    a, b = _short(), Episode(29200.0, 29220.0, "DOWN", gap_id="B")
    a.on_tick(_t("09:33:10"), 29110.0, bar_open=29105.0)
    b.on_tick(_t("09:35:10"), 29210.0, bar_open=29205.0)
    assert [e.gap_id for e in evaluation_order([a, b])] == ["B", "A"]


def test_a_never_entered_gap_sorts_last():
    a, b = _short(), Episode(29200.0, 29220.0, "DOWN", gap_id="B")
    a.on_tick(_t("09:33:10"), 29110.0, bar_open=29105.0)
    assert [e.gap_id for e in evaluation_order([a, b])] == ["A", "B"]


# --- knobs ------------------------------------------------------------------- #

@pytest.mark.parametrize("const,value", [(EARLY_RUNAWAY_PTS, 25.0),
                                         (SL_BUFFER_PTS, 2.0), (SL_CAP_PTS, 30.0)])
def test_the_knobs_are_the_sec9_starting_values(const, value):
    assert const == value


def test_the_retired_rr_to_dol_test_is_not_implemented():
    """§6's per-gap RR-to-DOL disqualification test is RETIRED (R* = 0, provably inert).
    Rebuilding it from stale prose is a defect."""
    import inspect

    import agent.trader.episode as mod
    src = inspect.getsource(mod)
    for token in ("rr_to_dol", "risk_reward", "RR_THRESHOLD"):
        assert token not in src
