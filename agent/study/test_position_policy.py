"""Task 3: one test per clause, named for the behaviour.

The fixture is synthetic and deliberately small: three 1m bull FVGs completing in
sequence, a regime flip, and one qualifying opposite bar. Every clause is exercised
against a schedule that can be read off by hand, so a failure names the clause rather
than the session.
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
DATE = "2026-06-10"
FILL_TS = pd.Timestamp(f"{DATE} 09:35:30", tz=TZ)
FILL = 95.0
STOP0 = 80.0
DOL = 200.0

#: (minute, open, high, low, close). EXACTLY three bull gaps, completing at 09:38 / 09:41
#: / 09:44, so their effective instants are 09:39 / 09:42 / 09:45.
#:
#: The lows marked "blocker" are load-bearing. `detect_fvgs` scans EVERY three-bar window,
#: so a monotonically rising tape produces a gap almost everywhere and the ladder stops
#: being readable by hand. Each blocker keeps `bar3.Low <= bar1.High` for the window it
#: would otherwise open, which is what leaves precisely the three intended gaps.
ROWS = [
    ("09:35", 100, 101, 99, 100),
    ("09:36", 100, 102, 99, 101),      # gap1 bar1 -> far edge 102
    ("09:37", 101, 110, 101, 109),
    ("09:38", 109, 112, 105, 111),     # gap1 bar3, Low 105 > 102  => [102, 105]
    ("09:39", 111, 113, 104, 112),     # gap2 bar1 -> far edge 113; 104 blocks i=3
    ("09:40", 112, 120, 112, 119),
    ("09:41", 119, 122, 116, 121),     # gap2 bar3, Low 116 > 113  => [113, 116]
    ("09:42", 121, 124, 110, 123),     # gap3 bar1 -> far edge 124; 110 blocks i=6
    ("09:43", 123, 130, 121, 129),     # first close beyond the 125 target; 121 blocks i=7
    ("09:44", 129, 132, 127, 131),     # gap3 bar3, Low 127 > 124  => [124, 127]
    ("09:45", 131, 131, 120, 124),     # opposite body -7, low 120  => clause 7
    ("09:46", 124, 140, 118, 139),     # 118 clears the 117 stop by 1.0
    ("09:47", 139, 205, 130, 200),     # DOL 200 touched; 130 blocks i=11
    ("09:48", 200, 206, 139, 205),     # 139 blocks i=12
]


def _bars(rows, date=DATE) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(f"{date} {r[0]}", tz=TZ) for r in rows])
    return pd.DataFrame(
        {"Open": [float(r[1]) for r in rows], "High": [float(r[2]) for r in rows],
         "Low": [float(r[3]) for r in rows], "Close": [float(r[4]) for r in rows],
         "Volume": [100.0] * len(rows)}, index=idx)


def _mirror(bars: pd.DataFrame, pivot: float = 300.0) -> pd.DataFrame:
    """Reflect a long fixture into the short one: `p -> pivot - p`, High/Low swapped."""
    out = pd.DataFrame(index=bars.index)
    out["Open"] = pivot - bars["Open"]
    out["High"] = pivot - bars["Low"]
    out["Low"] = pivot - bars["High"]
    out["Close"] = pivot - bars["Close"]
    out["Volume"] = bars["Volume"]
    return out


def _ts(hhmm) -> pd.Timestamp:
    return pd.Timestamp(f"{DATE} {hhmm}", tz=TZ)


def _run(**kw):
    base = dict(date=DATE, track="test", direction="UP", fill_ts=FILL_TS,
                fill_price=FILL, initial_stop=STOP0, dol=DOL, initial_target=125.0)
    base.update(kw)
    bars = base.pop("bars", None)
    if bars is None:
        bars = _bars(ROWS)
    return pp.run_policy(bars, **base)


def _by_clause(run, clause):
    return [m for m in run.moves if m.clause == clause]


# --------------------------------------------------------------------------- #
# clause 3 / 4 -- break-even                                                    #
# --------------------------------------------------------------------------- #

def test_be_fires_on_the_first_completing_fvg_after_the_fill():
    run = _run()
    be = _by_clause(run, 3)
    assert len(be) == 1
    assert be[0].price == pytest.approx(FILL)      # the fill price EXACTLY
    assert be[0].at == _ts("09:39")                # gap1 completes on the 09:38 close


def test_be_fallback_fires_at_fifty_percent_of_the_fill_to_dol_distance():
    """No FVG ever completes here, so clause 3 cannot fire and clause 4 must."""
    rows = [("09:35", 100, 101, 99, 100), ("09:36", 100, 102, 99, 101),
            ("09:37", 101, 103, 100, 102), ("09:38", 102, 148, 101, 147),
            ("09:39", 147, 150, 90, 95)]
    run = _run(bars=_bars(rows))
    assert not _by_clause(run, 3)
    fb = _by_clause(run, 4)
    assert len(fb) == 1
    # half = 95 + 0.5 * (200 - 95) = 147.5, first reached by the 09:38 bar's high
    assert fb[0].price == pytest.approx(FILL)
    assert fb[0].at == _ts("09:39")


def test_the_be_fallback_does_not_fire_when_the_first_fvg_already_moved_the_stop():
    """Clause 4 is a FALLBACK: 'if BE has not fired'. On the main fixture BE fires at
    09:39 and the 50% mark is not reached until 09:47, so clause 4 never applies."""
    run = _run()
    assert not _by_clause(run, 4)


# --------------------------------------------------------------------------- #
# clause 5 -- the trail                                                         #
# --------------------------------------------------------------------------- #

def test_the_trail_uses_the_PREVIOUS_fvgs_far_edge_minus_three():
    run = _run()
    trail = _by_clause(run, 5)
    assert [m.at for m in trail] == [_ts("09:42"), _ts("09:45")]
    # gap2 completes -> stop from GAP1's far edge (102), not gap2's own (113)
    assert trail[0].price == pytest.approx(102.0 - pp.TRAIL_BUFFER_PTS)
    # gap3 completes -> stop from GAP2's far edge (113)
    assert trail[1].price == pytest.approx(113.0 - pp.TRAIL_BUFFER_PTS)


def test_the_trail_leaves_both_gaps_between_price_and_the_stop():
    """The design intent behind 'the PREVIOUS gap': price must traverse both."""
    run = _run()
    gaps = pp.continuation_fvgs(_bars(ROWS), "UP", FILL_TS)
    second = _by_clause(run, 5)[0]
    assert second.price < pp._far_edge(gaps[0], True)      # below gap1's far edge
    assert second.price < pp._far_edge(gaps[1], True)      # and below gap2's


# --------------------------------------------------------------------------- #
# clauses 6 / 7 -- the regime flip and the post-flip ratchet                     #
# --------------------------------------------------------------------------- #

def test_the_regime_flips_on_a_close_beyond_the_initial_target():
    run = _run()
    assert run.flipped_at == _ts("09:44")          # the 09:43 close, 129 > 125
    assert run.target_status == pp.TARGET_PRESENT


def test_post_flip_an_opposite_bar_under_three_points_does_not_ratchet():
    rows = list(ROWS)
    rows[10] = ("09:45", 131, 131, 120, 129.5)     # body -1.5, under the 3-pt floor
    run = _run(bars=_bars(rows))
    assert run.flipped_at == _ts("09:44")
    assert not _by_clause(run, 7)


def test_both_trails_run_after_the_flip_and_the_tighter_wins():
    run = _run()
    seven = _by_clause(run, 7)
    assert len(seven) == 1
    assert seven[0].at == _ts("09:46")
    assert seven[0].price == pytest.approx(120.0 - pp.TRAIL_BUFFER_PTS)
    # clause 5's standing stop at that moment is 110; clause 7's 117 is tighter and wins
    standing = [m for m in run.moves if m.at <= _ts("09:45")][-1]
    assert standing.price == pytest.approx(110.0)
    assert seven[0].price > standing.price


def test_a_looser_post_flip_proposal_is_a_no_op_rather_than_a_move():
    rows = list(ROWS)
    rows[10] = ("09:45", 131, 131, 80, 124)        # wick to 80 -> a stop at 77, looser
    run = _run(bars=_bars(rows))
    assert not _by_clause(run, 7)


# --------------------------------------------------------------------------- #
# clause 8 -- monotone                                                          #
# --------------------------------------------------------------------------- #

def test_the_stop_never_loosens():
    run = _run()
    prices = [m.price for m in run.moves]
    assert prices == sorted(prices)                # a long's stop only ever rises
    assert prices[0] >= run.initial_stop


def test_clause_7_can_never_loosen_the_mechanisms_own_stop():
    """Clause 8 seeded with clause 2's stop, and the case that exposes it.

    A regime flip needs no FVG, so on a session where the target is cleared before any
    continuation gap completes, clause 7 is the FIRST proposal. Folding from `None` would
    accept it unconditionally and a wick-anchored ratchet could move the stop BELOW the
    mechanism's own — clause 8 and clause 2 violated together, silently.
    """
    rows = [("09:35", 100, 101, 99, 100),
            ("09:36", 100, 108, 99, 107),      # closes beyond the 105 target -> flip
            ("09:37", 107, 107, 89, 101),      # opposite body -6; wick 89 -> a stop at 86
            ("09:38", 101, 102, 100, 101)]
    run = _run(bars=_bars(rows), initial_target=105.0, initial_stop=90.0, fill_price=100.0)

    assert run.flipped_at is not None
    assert not _by_clause(run, 3)                    # no FVG completed: clause 7 is first
    assert all(m.price >= 90.0 for m in run.moves), [m.to_dict() for m in run.moves]
    # ... and the 09:37 low of 89 therefore takes the MECHANISM's stop, not a looser one
    assert run.exit_reason == pp.EXIT_STOP
    assert run.exit_price == pytest.approx(90.0)


def test_the_monotone_fold_is_seeded_with_the_initial_stop_not_with_nothing():
    long_moves = [pp.StopMove(pd.Timestamp(f"{DATE} 09:40", tz=TZ), 80.0, 7, "looser"),
                  pp.StopMove(pd.Timestamp(f"{DATE} 09:41", tz=TZ), 96.0, 7, "tighter")]
    kept = pp._monotone(long_moves, 90.0, "UP")
    assert [m.price for m in kept] == [96.0]         # the 80.0 proposal is a no-op
    kept_short = pp._monotone(
        [pp.StopMove(pd.Timestamp(f"{DATE} 09:40", tz=TZ), 120.0, 7, "looser")],
        110.0, "DOWN")
    assert kept_short == ()


def test_clause_7_may_fire_on_the_flip_bar_itself_and_that_is_pinned():
    """An INTERPRETATION, recorded rather than left ambiguous. Clause 6 establishes the
    flip on a bar's close; clause 7 says "post-flip". One bar can do both — close beyond
    the target AND close opposite by >= 3 pts. Both actions become effective at the same
    instant under clause 11, and clause 8 takes the tighter, so admitting the flip bar
    can only ever tighten. The Sep-2 fixture does not discriminate (flip 10:17, ratchet
    10:20), so nothing in the corpus pins it either way."""
    rows = [("09:35", 100, 101, 99, 100),
            ("09:36", 110, 112, 104, 106),     # closes 106 > 105 (flip) AND body -4
            ("09:37", 106, 107, 105, 106),
            ("09:38", 106, 107, 105, 106)]
    run = _run(bars=_bars(rows), initial_target=105.0, initial_stop=90.0,
               fill_price=100.0)
    assert run.flipped_at == _ts("09:37")
    seven = _by_clause(run, 7)
    assert [m.at for m in seven] == [_ts("09:37")]
    assert seven[0].price == pytest.approx(104.0 - pp.TRAIL_BUFFER_PTS)


def test_a_tape_that_never_reaches_1300_is_not_reported_as_a_hard_close():
    rows = [("09:35", 100, 101, 99, 100), ("09:36", 100, 102, 99, 101),
            ("09:37", 101, 103, 100, 102)]
    run = _run(bars=_bars(rows))
    assert run.exit_reason == pp.EXIT_TAPE_END
    assert run.exit_ts == _ts("09:37")


def test_a_tape_that_does_not_cover_its_own_fill_raises_rather_than_booking_zero():
    rows = [("09:20", 100, 101, 99, 100), ("09:21", 100, 102, 99, 101)]
    with pytest.raises(ValueError, match="no 1m bars"):
        _run(bars=_bars(rows))


def test_a_naive_index_raises_rather_than_putting_1300_on_the_machine_clock():
    bars = _bars(ROWS)
    naive = bars.copy()
    naive.index = naive.index.tz_localize(None)
    with pytest.raises(ValueError, match="tz-aware"):
        pp.run_policy(naive, date=DATE, track="t", direction="UP",
                      fill_ts=FILL_TS.tz_localize(None), fill_price=FILL,
                      initial_stop=STOP0, dol=DOL, initial_target=125.0)


def test_the_no_breakeven_arm_drops_clauses_3_and_4_and_keeps_the_trail():
    """Arms C/D. Clause 4 is a FALLBACK for clause 3, so removing one without the other
    would leave a breakeven move under another name — both go together."""
    full = _run()
    no_be = _run(use_breakeven=False)

    assert [m.clause for m in full.moves] == [3, 5, 5, 7]
    assert [m.clause for m in no_be.moves] == [5, 5, 7]
    assert not [m for m in no_be.moves if m.clause in (3, 4)]
    # the trail is UNCHANGED: clause 5 still reads the PREVIOUS gap, so gap 0 remains the
    # anchor it reads from even though it no longer triggers a move of its own
    assert [(m.at, m.price) for m in no_be.moves if m.clause == 5]         == [(m.at, m.price) for m in full.moves if m.clause == 5]
    assert no_be.use_breakeven is False and full.use_breakeven is True


def test_the_no_breakeven_arm_also_suppresses_the_fallback():
    rows = [("09:35", 100, 101, 99, 100), ("09:36", 100, 102, 99, 101),
            ("09:37", 101, 103, 100, 102), ("09:38", 102, 148, 101, 147),
            ("09:39", 147, 150, 90, 95)]
    assert _by_clause(_run(bars=_bars(rows)), 4)                 # fires normally
    arm_c = _run(bars=_bars(rows), use_breakeven=False)
    assert not _by_clause(arm_c, 4) and not _by_clause(arm_c, 3)
    # Arm C keeps 6/7, and this tape flips (the 09:38 close of 147 clears the 125 target),
    # so clause 7 legitimately survives — only the breakeven pair is withheld.
    assert [m.clause for m in arm_c.moves] == [7]
    # Arm D withholds the regime too, and then nothing moves the stop at all.
    assert not _run(bars=_bars(rows), use_breakeven=False, initial_target=None).moves


def test_the_pure_trail_arm_is_clause_5_monotonicity_and_the_exits():
    """Arm D: no breakeven AND no regime (an absent initial target makes 6/7 inert)."""
    run = _run(use_breakeven=False, initial_target=None)
    assert {m.clause for m in run.moves} == {5}
    assert run.flipped_at is None
    assert run.ratchet_armed is False
    prices = [m.price for m in run.moves]
    assert prices == sorted(prices)                              # clause 8 still holds
    assert prices[0] >= run.initial_stop


def test_the_arms_are_recorded_on_the_run_so_a_row_is_self_describing():
    assert _run().to_dict()["use_breakeven"] is True
    assert _run(use_breakeven=False).to_dict()["use_breakeven"] is False


def test_the_tighter_helper_is_the_single_comparison_every_clause_uses():
    assert pp._tighter(100.0, 90.0, True) == 100.0
    assert pp._tighter(100.0, 90.0, False) == 90.0
    assert pp._tighter(None, 90.0, True) == 90.0


# --------------------------------------------------------------------------- #
# clauses 9 / 10 / 11 -- exits and timing                                       #
# --------------------------------------------------------------------------- #

def test_the_dol_exit_is_a_touch_and_the_initial_target_flip_is_a_close():
    run = _run()
    assert run.exit_reason == pp.EXIT_DOL
    assert run.exit_price == pytest.approx(DOL)
    assert run.exit_ts == _ts("09:47")             # touched by the high, not the close

    # ... whereas a bar that WICKS beyond the initial target without closing beyond it
    # does not flip the regime.
    wick_only = [("09:35", 100, 101, 99, 100),
                 ("09:36", 100, 130, 99, 124),     # high 130 > 125, close 124 < 125
                 ("09:37", 124, 124.5, 123, 124),
                 ("09:38", 124, 124.5, 123, 124)]
    assert _run(bars=_bars(wick_only)).flipped_at is None


def test_an_open_position_is_market_closed_at_1300_et():
    rows = [("09:35", 100, 101, 99, 100), ("09:36", 100, 102, 99, 101),
            ("12:58", 101, 103, 100, 102), ("12:59", 102, 104, 101, 103)]
    run = _run(bars=_bars(rows))
    assert run.exit_reason == pp.EXIT_TIME
    assert run.exit_ts == _ts("12:59")
    assert run.exit_price == pytest.approx(103.0)
    assert run.pnl == pytest.approx(103.0 - FILL)


def test_every_action_executes_at_the_next_bar_open():
    """Clause 11, asserted structurally: every effective instant is a 1m boundary one
    minute after the close that triggered it."""
    run = _run()
    span = pd.Timedelta("1min")
    bars = _bars(ROWS)
    for move in run.moves:
        assert move.at.second == 0 and move.at.microsecond == 0
        assert (move.at - span) in bars.index


def test_the_stop_touch_beats_the_dol_touch_on_the_same_bar():
    """Adverse same-bar resolution, per plan 31 §3.1 and `order_sim`."""
    rows = [("09:35", 100, 101, 99, 100),
            ("09:36", 100, 205, 70, 100)]          # reaches the DOL and the stop
    run = _run(bars=_bars(rows))
    assert run.exit_reason == pp.EXIT_STOP
    assert run.exit_price == pytest.approx(STOP0)
    assert run.pnl == pytest.approx(STOP0 - FILL)


# --------------------------------------------------------------------------- #
# mirroring                                                                     #
# --------------------------------------------------------------------------- #

def test_all_clauses_mirror_for_a_short():
    """Reflecting the tape through a pivot must reflect every stop and the P&L exactly."""
    pivot = 300.0
    long_run = _run()
    short_run = _run(bars=_mirror(_bars(ROWS), pivot), direction="DOWN",
                     fill_price=pivot - FILL, initial_stop=pivot - STOP0,
                     dol=pivot - DOL, initial_target=pivot - 125.0)

    assert [m.clause for m in short_run.moves] == [m.clause for m in long_run.moves]
    for a, b in zip(long_run.moves, short_run.moves):
        assert b.at == a.at
        assert b.price == pytest.approx(pivot - a.price)
    assert short_run.flipped_at == long_run.flipped_at
    assert short_run.exit_reason == long_run.exit_reason
    assert short_run.pnl == pytest.approx(long_run.pnl)


# --------------------------------------------------------------------------- #
# clauses 12 / 13 / 14 -- the input contract                                     #
# --------------------------------------------------------------------------- #

def test_an_absent_initial_target_never_flips_the_regime_and_leaves_clause_7_inert():
    run = _run(initial_target=None)
    assert run.target_status == pp.TARGET_ABSENT
    assert run.flipped_at is None
    assert not _by_clause(run, 7)
    assert run.ratchet_armed is False
    # clauses 3, 5 and 8 continue unchanged, and carry the session to the DOL
    assert [m.clause for m in run.moves] == [3, 5, 5]
    assert run.exit_reason == pp.EXIT_DOL


def test_an_initial_target_beyond_the_dol_is_ignored_exactly_as_if_absent():
    """Clause 13 -- not clamped, not relocated."""
    beyond = _run(initial_target=DOL + 50.0)
    absent = _run(initial_target=None)
    assert beyond.target_status == pp.TARGET_BEYOND_DOL
    assert beyond.initial_target == pytest.approx(DOL + 50.0)   # recorded, not used
    assert beyond.flipped_at is None
    assert [m.to_dict() for m in beyond.moves] == [m.to_dict() for m in absent.moves]
    assert beyond.exit_reason == absent.exit_reason
    assert beyond.pnl == pytest.approx(absent.pnl)


def test_an_absent_dol_produces_no_entry_at_all():
    """Clause 14: the contract-layer gate, reproduced explicitly."""
    run = _run(dol=None)
    assert run.exit_reason == pp.NO_ENTRY
    assert run.moves == ()
    assert run.pnl is None
    assert run.fill_ts is None


def test_clause_12_and_13_are_counted_rather_than_merely_handled():
    """§2.1: how often the post-flip ratchet never arms is a RESULT."""
    assert _run(initial_target=None).ratchet_armed is False
    assert _run(initial_target=DOL + 50.0).ratchet_armed is False
    assert _run().ratchet_armed is True
    assert set(_run().to_dict()) >= {"target_status", "ratchet_armed", "flipped_at"}


# --------------------------------------------------------------------------- #
# §1's discipline, asserted                                                     #
# --------------------------------------------------------------------------- #

def test_the_simulator_never_reads_how_either_target_was_selected():
    """§2.1: both targets are injected PRICES; provenance is not an input."""
    src = _code(pp)
    for forbidden in ("build_menus", "compute_cautious_prices", "targets_for_policy",
                      "menu_at", "label_corpus", "oracle_dol", "production_dol",
                      "dol_source", "initial_level"):
        assert forbidden not in src, forbidden
    # The two targets arrive as plain floats and are declared as such.
    import inspect
    sig = inspect.signature(pp.run_policy)
    assert {"dol", "initial_target"} <= set(sig.parameters)


def test_the_simulator_never_reads_the_executors_order_lifecycle():
    """§4 (d): the handoff is the fill event; two owners of one stop is a defect."""
    src = _code(pp)
    for forbidden in ("OrderSim", "order_sim", "_sim", "Executor", "executor"):
        assert forbidden not in src, forbidden


def test_the_simulator_reads_no_value_dated_after_the_bar_it_acts_on():
    """§1's causality claim, asserted rather than asserted-about.

    Every move is re-derived from a frame truncated to the bars that had CLOSED by the
    instant the move becomes effective. A clause peeking forward would produce a
    different move, or none.
    """
    bars = _bars(ROWS)
    span = pd.Timedelta("1min")
    full = _run()
    assert full.moves, "the fixture must produce moves for this to mean anything"

    for move in full.moves:
        visible = bars[bars.index + span <= move.at]
        moves, flipped, _ = pp.build_schedule(
            visible, direction="UP", fill_ts=FILL_TS, fill_price=FILL,
            initial_stop=STOP0, dol=DOL, initial_target=125.0)
        same = [m for m in moves if m.at == move.at and m.clause == move.clause]
        assert same, f"clause {move.clause} at {move.at} needed a later bar"
        assert same[0].price == pytest.approx(move.price)
        if move.clause == 7:
            assert flipped is not None and flipped <= move.at


def test_no_wall_clock_is_read():
    src = _code(pp)
    for forbidden in ("datetime.now", "get_et_now", "time.time", "Timestamp.now",
                      "utcnow", "today()"):
        assert forbidden not in src, forbidden


def test_the_simulator_is_silent():
    assert "print(" not in _code(pp)


# --------------------------------------------------------------------------- #
# the FVG definition, and the pre-registered height variant                      #
# --------------------------------------------------------------------------- #

def test_the_fvg_definition_is_the_codebases_and_completion_is_the_third_bar():
    gaps = pp.continuation_fvgs(_bars(ROWS), "UP", FILL_TS)
    assert len(gaps) == 3
    first = gaps[0]
    assert (first.price_low, first.price_high) == (102.0, 105.0)   # [bar1.High, bar3.Low]
    assert pd.Timestamp(first.extra["confirm_bar_ts"]) == _ts("09:38")
    assert pd.Timestamp(first.extra["exists_from"]) == _ts("09:39")


def test_a_counter_direction_gap_is_not_a_continuation_gap():
    assert pp.continuation_fvgs(_bars(ROWS), "DOWN", FILL_TS) == []


def test_the_minimum_height_filter_is_a_variant_and_the_default_is_no_filter():
    unfiltered = pp.continuation_fvgs(_bars(ROWS), "UP", FILL_TS)
    filtered = pp.continuation_fvgs(_bars(ROWS), "UP", FILL_TS, min_height=4.0)
    assert len(unfiltered) == 3                      # heights 3.0, 3.0, 3.0
    assert filtered == []
    assert len(pp.continuation_fvgs(_bars(ROWS), "UP", FILL_TS, min_height=None)) == 3


def test_a_gap_completing_before_the_fill_is_not_new():
    late = pd.Timestamp(f"{DATE} 09:43:00", tz=TZ)
    gaps = pp.continuation_fvgs(_bars(ROWS), "UP", late)
    assert all(pd.Timestamp(g.extra["exists_from"]) > late for g in gaps)
    assert len(gaps) == 1


# --------------------------------------------------------------------------- #

def _code(mod) -> str:
    """Module CODE without docstrings — the prose names what it must not touch."""
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
