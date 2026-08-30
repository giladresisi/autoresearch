"""Task 3: §5's fresh-retrace gate.

The whole 08-21 verdict turns on which side of 09:30:30 a tick falls, and on whether
"into" means presence or a crossing. Both are pinned here.
"""
import pandas as pd

from agent.trader.retrace import RetraceGate

TZ = "America/New_York"
SETTLE = pd.Timestamp("2026-08-21 09:30:30", tz=TZ)
LO, HI = 29472.25, 29485.0


def _t(s):
    return pd.Timestamp(f"2026-08-21 {s}", tz=TZ)


def _outside_then(g, gap_id="A", when="09:30:40", price=29500.0):
    """Price above the gap: the state every real fresh retrace crosses in FROM."""
    g.note_tick(gap_id, price, LO, HI, _t(when))


def test_a_tick_inside_after_the_window_is_a_fresh_entry():
    g = RetraceGate(SETTLE)
    _outside_then(g)
    g.note_tick("A", 29480.0, LO, HI, _t("09:31:00"))
    assert g.fresh_entry_seen("A")


def test_a_tick_inside_before_the_window_ends_is_not_fresh():
    """In-window penetrations count for eligibility tracking ONLY (§2)."""
    g = RetraceGate(SETTLE)
    g.note_tick("A", 29500.0, LO, HI, _t("09:29:00"))
    g.note_tick("A", 29480.0, LO, HI, _t("09:30:00"))
    assert not g.fresh_entry_seen("A")


def test_a_pre_arm_penetration_is_not_fresh():
    """08-21's whole verdict: prior penetration only, no fresh re-entry -> FLAT."""
    g = RetraceGate(SETTLE)
    g.note_tick("A", 29500.0, LO, HI, _t("08:04:00"))
    g.note_tick("A", 29480.0, LO, HI, _t("08:05:00"))
    assert not g.fresh_entry_seen("A")


def test_the_window_boundary_is_exclusive():
    """Strictly AFTER 09:30:30. A tick AT the boundary does not qualify."""
    g = RetraceGate(SETTLE)
    g.note_tick("A", 29500.0, LO, HI, _t("09:30:00"))
    g.note_tick("A", 29480.0, LO, HI, SETTLE)
    assert not g.fresh_entry_seen("A")


def test_the_first_second_after_the_boundary_does_qualify():
    g = RetraceGate(SETTLE)
    g.note_tick("A", 29500.0, LO, HI, _t("09:30:00"))
    g.note_tick("A", 29480.0, LO, HI, _t("09:30:31"))
    assert g.fresh_entry_seen("A")


def test_a_tick_outside_the_gap_is_not_an_entry():
    g = RetraceGate(SETTLE)
    g.note_tick("A", 29500.0, LO, HI, _t("09:31:00"))
    assert not g.fresh_entry_seen("A")


def test_the_edges_count_as_inside():
    """§5's 'into' is any tick ENTERING the gap's range. (§6's strictly-inside rule is a
    DIFFERENT test and belongs to the episode machine, not here.)"""
    g = RetraceGate(SETTLE)
    _outside_then(g)
    g.note_tick("A", 29485.0, LO, HI, _t("09:31:00"))
    assert g.fresh_entry_seen("A")


def test_continuous_presence_across_the_boundary_is_NOT_a_fresh_entry():
    """§11's calibration, verbatim: 'continuous presence inside the gap at the boundary
    does NOT count, which is what the validated 08-13 walk did with its fresh 09:32:35
    re-entry'. §5 defines 'into' as a tick ENTERING the range."""
    g = RetraceGate(SETTLE)
    for s in ("09:30:20", "09:30:30", "09:30:31", "09:31:00", "09:32:00"):
        g.note_tick("A", 29480.0, LO, HI, _t(s))
    assert not g.fresh_entry_seen("A")


def test_leaving_and_returning_after_continuous_presence_does_qualify():
    """The 08-13 shape: inside at the boundary, out, then the fresh re-entry."""
    g = RetraceGate(SETTLE)
    g.note_tick("A", 29480.0, LO, HI, _t("09:30:20"))
    g.note_tick("A", 29480.0, LO, HI, _t("09:30:31"))
    g.note_tick("A", 29500.0, LO, HI, _t("09:31:30"))          # leaves
    assert not g.fresh_entry_seen("A")
    g.note_tick("A", 29480.0, LO, HI, _t("09:32:35"))          # returns
    assert g.fresh_entry_seen("A")


def test_a_penetration_straddling_the_boundary_cannot_be_re_read_as_a_crossing():
    """Outside at 09:30:00, inside at 09:30:29 (voided), still inside at 09:30:31. The
    entry happened IN-WINDOW; one second of clock does not launder it."""
    g = RetraceGate(SETTLE)
    g.note_tick("A", 29500.0, LO, HI, _t("09:30:00"))
    g.note_tick("A", 29480.0, LO, HI, _t("09:30:29"))
    g.note_tick("A", 29480.0, LO, HI, _t("09:30:31"))
    assert not g.fresh_entry_seen("A")


def test_a_bar_range_counts_even_when_the_close_is_outside():
    """`OrderSim` fills against the bar's extremes, so the retrace test must read the
    same basis — otherwise an order is placed for a tick the fill model already
    believes was reachable."""
    g = RetraceGate(SETTLE)
    _outside_then(g)
    g.note_tick("A", 29500.0, LO, HI, _t("09:31:00"), low=29470.0, high=29505.0)
    assert g.fresh_entry_seen("A")


def test_each_gap_is_tracked_independently():
    g = RetraceGate(SETTLE)
    _outside_then(g, "A")
    g.note_tick("A", 29480.0, LO, HI, _t("09:31:00"))
    assert g.fresh_entry_seen("A") and not g.fresh_entry_seen("B")


def test_reset_clears_a_gap():
    g = RetraceGate(SETTLE)
    _outside_then(g)
    g.note_tick("A", 29480.0, LO, HI, _t("09:31:00"))
    g.reset("A")
    assert not g.fresh_entry_seen("A")


def test_a_missing_price_or_time_is_ignored_rather_than_raising():
    g = RetraceGate(SETTLE)
    g.note_tick("A", None, LO, HI, _t("09:31:00"))
    g.note_tick("A", 29480.0, LO, HI, None)
    assert not g.fresh_entry_seen("A")


def test_the_gate_never_forgets_a_fresh_entry_on_later_ticks():
    """Once earned, the precondition stands for the plan — §5 has no expiry, and §2's
    cooldown explicitly carries NO fresh-precondition requirement."""
    g = RetraceGate(SETTLE)
    _outside_then(g)
    g.note_tick("A", 29480.0, LO, HI, _t("09:31:00"))
    for s in ("09:35:00", "09:40:00", "10:30:00"):
        g.note_tick("A", 29600.0, LO, HI, _t(s))
    assert g.fresh_entry_seen("A")
