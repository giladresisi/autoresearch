import datetime
import types

import pandas as pd
import pytest

from agent.study.candidates import Candidate, universe
from agent.study.labelling import (
    OUTCOME_DRAW, OUTCOME_INELIGIBLE, OUTCOME_NEAR_MISS_IN, OUTCOME_NEAR_MISS_OUT,
    OUTCOME_REACHED_PASSED, STATUS_LABELLED, STATUS_UNEXPLAINED, VIA_NEAR_MISS,
    VIA_REACHED_LAST, label_segment)

TZ = "America/New_York"
D = "2026-08-13"


# --------------------------------------------------------------------------- #
# synthetic fixtures: a rising 1m tape, hand-made candidates, no parquet
# --------------------------------------------------------------------------- #
def _bars(n=11, base=100.0, step=10.0, start="09:30"):
    """`n` 1m bars from `start`, each 5 wide, marching `step` per minute."""
    idx = pd.date_range(f"{D} {start}", periods=n, freq="1min", tz=TZ)
    return pd.DataFrame({"open": [base + step * i for i in range(n)],
                         "high": [base + 5 + step * i for i in range(n)],
                         "low": [base + step * i for i in range(n)],
                         "close": [base + 4 + step * i for i in range(n)]}, index=idx)


def _seg(direction="up", start="09:30", extreme="09:36"):
    return types.SimpleNamespace(
        direction=direction, start_ts=pd.Timestamp(f"{D} {start}", tz=TZ),
        extreme_ts=pd.Timestamp(f"{D} {extreme}", tz=TZ))


def _bundle(mnq=100.0, mes=100.0):
    return types.SimpleNamespace(avg_range_1h={"MNQ": mnq, "MES": mes})


def _cand(price, ticker="MNQ", name="prev1_day_high", cls="day_extreme", tier="day",
          swept=False):
    return Candidate(ticker=ticker, names=(name,), price=price, cls=cls, tier=tier,
                     swept_before=swept)


def _run(cands, seg=None, bundle=None, bars=None):
    """-> (labels_by_ticker, rows). The tape runs 09:30-09:40 (100 -> 205) and the
    default segment's extreme bar opens 09:36, so the window covers all of it."""
    bars = bars or {"MNQ": _bars(), "MES": _bars()}
    return label_segment(seg or _seg(), bundle or _bundle(), bars, cands)


def _mnq(cands, **kw):
    labels, rows = _run(cands, **kw)
    return labels["MNQ"], [r for r in rows if r.ticker == "MNQ"]


def _by_price(rows):
    return {r.price: r for r in rows}


def test_the_last_reached_candidate_is_the_draw():
    """Rule 2: the move ran through everything nearer and stopped at the last thing
    it completed."""
    label, _ = _mnq([_cand(120.0), _cand(180.0)])
    assert label.status == STATUS_LABELLED
    assert label.via == VIA_REACHED_LAST
    assert label.price == 180.0
    assert label.reached_ts == pd.Timestamp(f"{D} 09:38", tz=TZ)
    assert label.n_reached == 2


def test_the_move_window_runs_to_the_end_of_the_extreme_bar():
    """The skeleton is 5m, so `extreme_ts` is the extreme BAR's open. Truncating the
    window there costs four minutes of tape -- on 2026-08-11 that is the difference
    between labelling the day low and labelling a level 81 points short of it."""
    label, _ = _mnq([_cand(180.0)], seg=_seg(extreme="09:36"))
    assert label.status == STATUS_LABELLED       # 180 is only crossed at 09:38
    assert label.price == 180.0


def test_earlier_reached_candidates_are_marked_passed():
    """A pool the move sailed through is evidence about that class's pull (spec 3.4);
    recording only the winner throws it away."""
    _, rows = _mnq([_cand(120.0), _cand(180.0)])
    r = _by_price(rows)
    assert r[120.0].outcome == OUTCOME_REACHED_PASSED
    assert r[180.0].outcome == OUTCOME_DRAW


def test_a_candidate_behind_the_move_start_is_ineligible():
    """Rule 1: a pool below the origin of an up move is not a target."""
    _, rows = _mnq([_cand(90.0), _cand(180.0)])
    assert _by_price(rows)[90.0].outcome == OUTCOME_INELIGIBLE


def test_a_candidate_already_swept_at_the_boundary_is_ineligible():
    """Rule 1 + spec 3.3: knowledge is monotone, eligibility is not."""
    _, rows = _mnq([_cand(120.0, swept=True), _cand(180.0)])
    assert _by_price(rows)[120.0].outcome == OUTCOME_INELIGIBLE


def test_when_nothing_is_reached_the_nearest_in_band_near_miss_wins():
    """Rule 3, the test-and-fail case. Tape tops at 205; band = 0.15 * 100 = 15."""
    label, rows = _mnq([_cand(215.0), _cand(300.0)])
    assert label.status == STATUS_LABELLED
    assert label.via == VIA_NEAR_MISS
    assert label.price == 215.0
    assert label.reached_ts is None
    r = _by_price(rows)
    assert r[215.0].outcome == OUTCOME_DRAW
    assert r[300.0].outcome == OUTCOME_NEAR_MISS_OUT
    assert r[215.0].closest_approach == pytest.approx(10.0)


def test_when_nothing_is_reached_and_nothing_is_in_band_it_is_unexplained():
    """Rule 4. The honest outcome and a headline number -- never a fallback label."""
    label, rows = _mnq([_cand(300.0)])
    assert label.status == STATUS_UNEXPLAINED
    assert label.price is None
    assert rows and rows[0].outcome == OUTCOME_NEAR_MISS_OUT


def test_a_near_miss_never_outranks_a_reached_candidate():
    """The trap the rule is shaped around: an unreached candidate's closest approach
    IS the move extreme, so a time-ranked rule would hand it every session."""
    label, rows = _mnq([_cand(120.0), _cand(215.0)])
    assert label.price == 120.0
    assert label.via == VIA_REACHED_LAST
    assert _by_price(rows)[215.0].outcome == OUTCOME_NEAR_MISS_IN


def test_two_prices_crossed_in_the_same_minute_mark_the_session_ambiguous():
    """Rule 5. Within one 1m bar the tape gives no ordering, so the tie-break is
    'further along the move' -- reported as ambiguous, never silently resolved."""
    label, _ = _mnq([_cand(120.0), _cand(125.0)], seg=_seg(extreme="09:28"))
    assert label.ambiguous is True
    assert label.price == 125.0


def test_a_single_price_crossed_alone_is_not_ambiguous():
    label, _ = _mnq([_cand(120.0), _cand(180.0)])
    assert label.ambiguous is False


def test_each_instrument_gets_its_own_label():
    """Spec 2.4 role-swap invariance: MNQ's DOL is an MNQ pool no matter which graph
    did the sweeping. A single cross-instrument argmax would destroy that."""
    bars = {"MNQ": _bars(), "MES": _bars(base=50.0, step=5.0)}
    labels, _ = _run([_cand(120.0), _cand(92.0, ticker="MES")], bars=bars)
    assert labels["MNQ"].price == 120.0 and labels["MNQ"].ticker == "MNQ"
    assert labels["MES"].price == 92.0 and labels["MES"].ticker == "MES"


def test_the_instrument_whose_draw_completed_last_is_the_halt_driver():
    """Spec 3.1's contribution, recorded rather than substituted: MES's pool is
    crossed at 09:39, MNQ's at 09:32."""
    bars = {"MNQ": _bars(), "MES": _bars(base=50.0, step=5.0)}
    labels, _ = _run([_cand(120.0), _cand(92.0, ticker="MES")], bars=bars)
    assert labels["MES"].is_halt_driver is True
    assert labels["MNQ"].is_halt_driver is False


def test_the_band_uses_each_instruments_own_avg_range():
    """Spec 2.3: never applied cross-instrument. MES's band here is 1.5 points, so a
    10-point shortfall is out of band even though MNQ's band is 15."""
    bars = {"MNQ": _bars(), "MES": _bars()}
    labels, rows = label_segment(_seg(), _bundle(mnq=100.0, mes=10.0), bars,
                                 [_cand(215.0), _cand(215.0, ticker="MES")])
    assert labels["MNQ"].status == STATUS_LABELLED       # 10 <= 15
    assert labels["MES"].status == STATUS_UNEXPLAINED    # 10 > 1.5
    assert {r.ticker: r.outcome for r in rows} == {"MNQ": OUTCOME_DRAW,
                                                   "MES": OUTCOME_NEAR_MISS_OUT}


def test_overshoot_and_lag_are_recorded_as_results():
    """Both are computed from the move's end -- lookahead by design, and named as
    result fields so no phase-3 predictor reads them."""
    label, _ = _mnq([_cand(180.0)], seg=_seg(extreme="09:40"))
    assert label.overshoot == pytest.approx(25.0)     # tape tops at 205
    assert label.lag_min == pytest.approx(2.0)        # crossed 09:38, turn 09:40


def test_the_entry_proxy_curve_is_reported_not_used():
    label, _ = _mnq([_cand(180.0)])
    assert set(label.dist_at_proxy) == {2, 5, 10}
    assert label.dist_from_start == pytest.approx(80.0)
    assert label.dist_at_proxy[2] < label.dist_from_start   # the confirmation tax


# --------------------------------------------------------------------------- #
# real sessions -- the acceptance criteria. Both were established BY HAND before
# this rule existed; a disagreement is information about the rule.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def real():
    from agent.study.facts_source import StudyFacts
    from agent.study.sessions import SessionBars
    return SessionBars(), StudyFacts()


def _label_real(real, date):
    from agent.study.skeleton import session_skeleton
    sb, sf = real
    sk = session_skeleton(sb.window_5m("MNQ", date), date)
    seg = sk.segments[0]
    bundle = sf.bundle_at(seg.start_ts)
    bars = {tk: sf.bars_1m(tk) for tk in ("MNQ", "MES")}
    return label_segment(seg, bundle, bars, universe(bundle))


def test_2026_08_13_labels_prev1_week_high(real):
    """Established by hand in spec 26 §2.4. The move ran 29862.75 -> 30267.00; the
    highest named pool is 30073.25 (prev1_week_high == prev6_day_high) and the
    remaining ~194 points are overshoot, not evidence of a further target."""
    labels, _ = _label_real(real, datetime.date(2026, 8, 13))
    mnq = labels["MNQ"]
    assert mnq.status == STATUS_LABELLED
    assert "prev1_week_high" in mnq.names
    assert mnq.price == pytest.approx(30073.25)
    assert mnq.cls == "week_extreme"
    assert mnq.via == VIA_REACHED_LAST
    assert mnq.overshoot == pytest.approx(193.75, abs=1.0)


def test_2026_08_13_both_instruments_name_the_same_level(real):
    """Spec 2.4's correspondence, and the reason one label per session is wrong: MES
    independently draws to its OWN prev1_week_high, 12 minutes after MNQ reaches its."""
    labels, _ = _label_real(real, datetime.date(2026, 8, 13))
    assert "prev1_week_high" in labels["MES"].names
    assert labels["MES"].price == pytest.approx(7820.25)
    assert labels["MES"].is_halt_driver is True


def test_2026_08_11_labels_the_mnq_day_low(real):
    """Established by hand: MNQ swept the day low around 09:46, running ~30 points
    through it, while MES fell short of its own; both reversed up from ~09:51. The
    day low is `asia(cur)_low` = 29666.00 and the move bottomed at 29636.00."""
    labels, _ = _label_real(real, datetime.date(2026, 8, 11))
    mnq = labels["MNQ"]
    assert mnq.status == STATUS_LABELLED
    assert mnq.via == VIA_REACHED_LAST
    assert "asia(cur)_low" in mnq.names
    assert mnq.price == pytest.approx(29666.00)
    assert mnq.overshoot == pytest.approx(30.0, abs=1.0)


def test_2026_08_11_mes_fell_short_of_the_same_level(real):
    """The cross-instrument asymmetry, spec 3.1's premise pointed at the target. The
    measured shortfall is ~5 MES points; whether that reads as in-band depends on the
    band, and 0.15 * avg_range_1h[MES] is 1.47 -- an OPEN definition question, so the
    test pins the measurement rather than the verdict."""
    labels, rows = _label_real(real, datetime.date(2026, 8, 11))
    mes_row = next(r for r in rows
                   if r.ticker == "MES" and "asia(cur)_low" in r.names)
    assert mes_row.reached_ts is None
    assert mes_row.closest_approach == pytest.approx(5.0, abs=1.5)
    assert labels["MNQ"].counterpart_status.startswith("near_miss")
    assert labels["MNQ"].is_halt_driver is True


def test_every_row_carries_a_class_even_when_it_lost(real):
    """Spec 3.4: the corpus must record what the move passed THROUGH, or the
    attractiveness question cannot be answered later without a full re-run."""
    _, rows = _label_real(real, datetime.date(2026, 8, 13))
    assert all(r.cls and r.tier for r in rows)
    assert {r.outcome for r in rows} >= {OUTCOME_REACHED_PASSED, OUTCOME_DRAW}


def test_the_module_never_reads_a_wall_clock():
    import inspect

    import agent.study.labelling as mod
    src = inspect.getsource(mod)
    assert "datetime.now" not in src
    assert "get_et_now" not in src
