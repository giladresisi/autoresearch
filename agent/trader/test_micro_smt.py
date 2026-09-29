"""`micro_smt_reject` (O3) / `micro_smt_exit` (O4): the divergence detector, the previous-
micro-session read, no-lookahead, and the 2026-09-24 real-tape reproduction.

See `micro_smt.py`'s docstring and `l2-mechanisms.md` §11 for the CANDIDATE's evidence.
"""
import pandas as pd
import pytest

import agent.trader.micro_smt as micro_smt
from agent.trader import named_cases as nc
from agent.trader.micro_smt import (MicroSmt, SL_BUFFER_PTS, SL_CAP_PTS,
                                    entry_previous_micro_extremes, previous_micro_extremes)
from agent.trader.reject_core import capped_stop

TZ = "America/New_York"
DATE = "2026-09-24"

PREV = {"mnq_high": 30638.0, "mnq_low": 30485.0, "mes_high": 7758.25, "mes_low": 7730.0,
        "session_start": pd.Timestamp(f"{DATE} 10:30", tz=TZ)}


def _bar(o, h, l, c):
    return pd.Series({"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1.0})


def _at(hhmm):
    """The COMPLETION instant of the 1m bar labelled `hhmm` (left-labelled convention)."""
    return pd.Timestamp(f"{DATE} {hhmm}", tz=TZ) + pd.Timedelta(minutes=1)


# -- the divergence + confirmation signature -------------------------------------- #

def test_bearish_fires_on_the_0924_10_36_confirmation_close():
    """The motivating day. Sweep bar 10:35 (MNQ 30643 > 30638, MES 7754.50 <= 7758.25)
    closes GREEN (does not confirm); confirmation bar 10:36 closes RED on both -> fires
    at its completion, 10:37:00, entry = MNQ's own close."""
    m = MicroSmt("bearish")
    sweep = m.on_bar_close(_at("10:35"), _bar(30631.50, 30643.00, 30619.25, 30643.00),
                           _bar(7753.00, 7754.50, 7751.00, 7754.50), PREV)
    assert sweep is None                                  # armed, not confirmed
    fire = m.on_bar_close(_at("10:36"), _bar(30638.00, 30638.00, 30607.00, 30613.25),
                          _bar(7754.75, 7754.75, 7750.75, 7752.25), PREV)
    assert fire is not None
    assert fire["time"] == _at("10:36")
    assert fire["direction"] == "DOWN"
    assert fire["price"] == pytest.approx(30613.25)
    assert fire["swept_by"] == "MNQ"
    # MNQ's own running high this micro-session is 30643.00 (set on the sweep bar).
    assert fire["stop"] == pytest.approx(
        capped_stop(30613.25, 30643.00, buffer_pts=SL_BUFFER_PTS, cap_pts=SL_CAP_PTS,
                   short=True))
    assert fire["stop"] == pytest.approx(30628.25)         # the 15-pt cap binds


def test_bullish_fires_on_the_0924_11_15_confirmation_close():
    """The mirrored exit signature at the lows: MES undercuts 7730.00 at 11:13 while MNQ
    holds above 30485.00; the bar labelled 11:15 closes UP on both -> fires at 11:16:00."""
    m = MicroSmt("bullish")
    fire = None
    bars = {
        "11:11": (30523.50, 30530.00, 30522.25, 30528.75, 7734.25, 7734.75, 7733.00, 7734.00),
        "11:12": (30529.00, 30537.25, 30518.00, 30519.50, 7733.75, 7734.50, 7730.50, 7731.00),
        "11:13": (30519.00, 30520.25, 30505.75, 30512.00, 7730.75, 7730.75, 7727.75, 7727.75),
        "11:14": (30511.50, 30517.50, 30504.00, 30510.00, 7728.00, 7728.75, 7725.25, 7726.50),
        "11:15": (30510.25, 30523.25, 30505.75, 30520.00, 7726.25, 7729.25, 7725.50, 7729.00),
    }
    for hhmm, (mo, mh, ml, mc, eo, eh, el, ec) in bars.items():
        fire = m.on_bar_close(_at(hhmm), _bar(mo, mh, ml, mc), _bar(eo, eh, el, ec), PREV)
        if fire is not None:
            assert hhmm == "11:15"
            break
    assert fire is not None
    assert fire["time"] == _at("11:15")
    assert fire["direction"] == "UP"
    assert fire["price"] == pytest.approx(30520.00)
    assert fire["swept_by"] == "MES"


def test_both_broken_cancels_an_armed_divergence():
    """The pinned reading: if the SECOND asset also breaks its own level before either
    confirms, the divergence is cancelled, not merely left unconfirmed."""
    m = MicroSmt("bearish")
    # MNQ breaks, MES does not -> armed. Neither bar confirms (both green).
    assert m.on_bar_close(_at("10:35"), _bar(30631.5, 30643.0, 30619.25, 30640.0),
                          _bar(7753.0, 7754.5, 7751.0, 7754.0), PREV) is None
    assert m.state()["armed"]
    # MES now ALSO breaks its own previous high -> both broken -> cancelled.
    assert m.on_bar_close(_at("10:36"), _bar(30640.0, 30641.0, 30620.0, 30612.0),
                          _bar(7754.0, 7759.0, 7752.0, 7751.0), PREV) is None
    assert not m.state()["armed"]
    # A later bar that closes red on both must NOT fire: the divergence is dead.
    assert m.on_bar_close(_at("10:40"), _bar(30612.0, 30615.0, 30600.0, 30601.0),
                          _bar(7751.0, 7752.0, 7748.0, 7747.0), PREV) is None


def test_neither_broken_does_not_arm():
    m = MicroSmt("bearish")
    assert m.on_bar_close(_at("10:35"), _bar(30600.0, 30610.0, 30595.0, 30598.0),
                          _bar(7750.0, 7755.0, 7748.0, 7749.0), PREV) is None
    assert not m.state()["armed"]


def test_sweep_bar_itself_may_confirm():
    """A single bar that both sweeps AND closes with the thesis fires immediately,
    mirroring `tmso_reject.SWEEP_BAR_MAY_CONFIRM`."""
    m = MicroSmt("bearish")
    fire = m.on_bar_close(_at("10:35"), _bar(30640.0, 30643.0, 30619.0, 30612.0),
                          _bar(7753.0, 7754.5, 7751.0, 7752.0), PREV)
    assert fire is not None
    assert fire["time"] == _at("10:35")


def test_fires_at_most_once_per_micro_session():
    m = MicroSmt("bearish")
    first = m.on_bar_close(_at("10:35"), _bar(30640.0, 30643.0, 30619.0, 30612.0),
                           _bar(7753.0, 7754.5, 7751.0, 7752.0), PREV)
    assert first is not None
    again = m.on_bar_close(_at("10:50"), _bar(30612.0, 30646.0, 30605.0, 30606.0),
                           _bar(7752.0, 7752.0, 7745.0, 7746.0), PREV)
    assert again is None


def test_a_new_micro_session_resets_the_machine():
    m = MicroSmt("bearish")
    first = m.on_bar_close(_at("10:35"), _bar(30640.0, 30643.0, 30619.0, 30612.0),
                           _bar(7753.0, 7754.5, 7751.0, 7752.0), PREV)
    assert first is not None
    next_prev = dict(PREV, mnq_high=30650.0, mes_high=7760.0,
                     session_start=pd.Timestamp(f"{DATE} 12:00", tz=TZ))
    again = m.on_bar_close(_at("12:00"), _bar(30640.0, 30655.0, 30619.0, 30612.0),
                           _bar(7753.0, 7759.0, 7751.0, 7752.0), next_prev)
    assert again is not None                                # a fresh session, fresh latch


# -- `previous_micro_extremes` ------------------------------------------------------ #

def test_previous_micro_extremes_matches_the_0924_real_tape():
    from backtest_smt import _main_dir_for_date
    d = _main_dir_for_date(DATE)
    mnq = pd.read_parquet(d / "MNQ_1m.parquet")
    mes = pd.read_parquet(d / "MES_1m.parquet")
    prev = previous_micro_extremes(mnq, mes, _at("10:35"))
    assert prev is not None
    assert prev["mnq_high"] == pytest.approx(30638.00)
    assert prev["mes_high"] == pytest.approx(7758.25)
    assert prev["mnq_low"] == pytest.approx(30485.00)
    assert prev["mes_low"] == pytest.approx(7730.00)


def test_previous_micro_extremes_is_none_for_the_first_micro_session():
    mnq = pd.DataFrame({"Open": [1], "High": [1], "Low": [1], "Close": [1]},
                       index=[pd.Timestamp(f"{DATE} 09:05", tz=TZ)])
    mes = mnq.copy()
    assert previous_micro_extremes(mnq, mes, pd.Timestamp(f"{DATE} 09:31", tz=TZ)) is None


def test_previous_micro_extremes_has_no_lookahead():
    """Deleting every bar at or after `now` must not change the reading: the window it
    reads is the COMPLETED prior micro-session only, strictly before `now`."""
    from backtest_smt import _main_dir_for_date
    d = _main_dir_for_date(DATE)
    mnq = pd.read_parquet(d / "MNQ_1m.parquet")
    mes = pd.read_parquet(d / "MES_1m.parquet")
    now = _at("10:35")
    full = previous_micro_extremes(mnq, mes, now)
    truncated = previous_micro_extremes(mnq[mnq.index < now], mes[mes.index < now], now)
    assert full == truncated


# -- the real-tape end-to-end reproduction (skips cleanly if data is missing) ------- #

def test_0924_end_to_end_entry_and_exit_reproduce_the_operator_walk():
    """Drives BOTH detectors bar-by-bar over the real 1m tape and asserts the exact
    entry/stop and exit figures `l2-mechanisms.md` §11 records for 2026-09-24."""
    try:
        from backtest_smt import _main_dir_for_date
        d = _main_dir_for_date(DATE)
        mnq = pd.read_parquet(d / "MNQ_1m.parquet").loc[DATE]
        mes = pd.read_parquet(d / "MES_1m.parquet").loc[DATE]
    except Exception:
        pytest.skip("2026-09-24 main parquet not available in this environment")

    entry_m = MicroSmt("bearish")
    entry_fire = None
    for label, mnq_row in mnq.between_time("10:30", "11:40").iterrows():
        if label not in mes.index:
            continue
        now = label + pd.Timedelta(minutes=1)
        prev = previous_micro_extremes(mnq, mes, now)
        fire = entry_m.on_bar_close(now, mnq_row, mes.loc[label], prev)
        if fire is not None:
            entry_fire = fire
            break
    assert entry_fire is not None
    assert entry_fire["time"] == pd.Timestamp(f"{DATE} 10:37", tz=TZ)
    assert entry_fire["price"] == pytest.approx(30613.25)
    assert entry_fire["stop"] == pytest.approx(30628.25)

    exit_m = MicroSmt("bullish")
    exit_fire = None
    for label, mnq_row in mnq.between_time("10:30", "11:40").iterrows():
        if label not in mes.index:
            continue
        now = label + pd.Timedelta(minutes=1)
        prev = previous_micro_extremes(mnq, mes, now)
        fire = exit_m.on_bar_close(now, mnq_row, mes.loc[label], prev)
        if fire is not None:
            exit_fire = fire
            break
    assert exit_fire is not None
    assert exit_fire["time"] == pd.Timestamp(f"{DATE} 11:16", tz=TZ)
    assert exit_fire["price"] == pytest.approx(30520.00)
    # The hypothetical short's P&L at the O4 exit: entry - exit for a DOWN position.
    assert entry_fire["price"] - exit_fire["price"] == pytest.approx(93.25)


# -- §7a.1: the pre-open pair (07:30-09:00 -> 09:00-10:30) and whole-session state -- #

PRE_DATE = "2026-09-28"


def _pts(hhmm):
    return pd.Timestamp(f"{PRE_DATE} {hhmm}", tz=TZ)


def _frame(rows):
    """{hhmm: (o, h, l, c)} -> a left-labelled 1m OHLC frame on PRE_DATE."""
    return pd.DataFrame([{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1.0}
                         for o, h, l, c in rows.values()],
                        index=[_pts(k) for k in rows]).sort_index()


def _preopen_frames():
    """07:30-09:00 highs MNQ 30753.50 / MES 7781.50 (09-28's), lows 30600 / 7750. A
    07:29 bar and a 09:05 bar sit just outside the window and must NOT be read."""
    mnq = _frame({"07:29": (30790.0, 30800.0, 30500.0, 30790.0),
                  "07:45": (30700.0, 30753.5, 30600.0, 30700.0),
                  "08:59": (30700.0, 30720.0, 30650.0, 30700.0),
                  "09:05": (30700.0, 30790.0, 30550.0, 30700.0)})
    mes = _frame({"07:29": (7790.0, 7795.0, 7740.0, 7790.0),
                  "07:45": (7770.0, 7781.5, 7750.0, 7770.0),
                  "08:59": (7770.0, 7775.0, 7760.0, 7770.0),
                  "09:05": (7770.0, 7790.0, 7745.0, 7770.0)})
    return mnq, mes


def test_entry_pair_for_the_first_micro_session_is_0730_0900_when_on(monkeypatch):
    monkeypatch.setattr(micro_smt, "MICRO_SMT_PREOPEN_PAIR_ENABLED", True)
    mnq, mes = _preopen_frames()
    prev = entry_previous_micro_extremes(mnq, mes, _pts("09:31"))
    assert prev == {"mnq_high": 30753.5, "mnq_low": 30600.0, "mes_high": 7781.5,
                    "mes_low": 7750.0, "session_start": _pts("09:00")}


def test_entry_pair_for_the_first_micro_session_is_none_when_off(monkeypatch):
    monkeypatch.setattr(micro_smt, "MICRO_SMT_PREOPEN_PAIR_ENABLED", False)
    mnq, mes = _preopen_frames()
    assert entry_previous_micro_extremes(mnq, mes, _pts("09:31")) is None


def test_entry_pair_is_the_ordinary_pair_after_1030(monkeypatch):
    monkeypatch.setattr(micro_smt, "MICRO_SMT_PREOPEN_PAIR_ENABLED", True)
    mnq, mes = _preopen_frames()
    now = _pts("10:35")
    got = entry_previous_micro_extremes(mnq, mes, now)
    assert got == previous_micro_extremes(mnq, mes, now)
    assert got["session_start"] == _pts("10:30")


def test_the_exit_reader_stays_inert_before_1030_with_the_pair_on(monkeypatch):
    """§7a.1 extends O3's ENTRY only: O4 reads `previous_micro_extremes`, still None."""
    monkeypatch.setattr(micro_smt, "MICRO_SMT_PREOPEN_PAIR_ENABLED", True)
    mnq, mes = _preopen_frames()
    assert previous_micro_extremes(mnq, mes, _pts("09:31")) is None


def test_entry_pair_is_none_before_the_0900_grid(monkeypatch):
    monkeypatch.setattr(micro_smt, "MICRO_SMT_PREOPEN_PAIR_ENABLED", True)
    mnq, mes = _preopen_frames()
    assert entry_previous_micro_extremes(mnq, mes, _pts("08:59")) is None


PRE_PREV = {"mnq_high": 30753.5, "mnq_low": 30600.0, "mes_high": 7781.5, "mes_low": 7750.0,
            "session_start": _pts("09:00")}
Q_MNQ = (30700.0, 30705.0, 30695.0, 30700.0)       # breaks nothing
Q_MES = (7770.0, 7771.0, 7769.0, 7770.0)
POKE_MES = (7775.0, 7790.0, 7770.0, 7773.0)        # above 7781.50


def test_a_pre_window_mes_break_cancels_an_in_window_mnq_break():
    """The whole-session live state: MES broke 7781.50 at 09:10, before O3 was ever
    asked. At 09:33 MNQ breaks too -> BOTH broken -> cancelled, even though the bar
    closes red on both. Without the history the same bar fires (the old behaviour)."""
    mnq_hist = _frame({"09:05": Q_MNQ, "09:10": Q_MNQ, "09:20": Q_MNQ})
    mes_hist = _frame({"09:05": Q_MES, "09:10": (7775.0, 7783.0, 7774.0, 7776.0),
                       "09:20": Q_MES})
    mnq_bar = _bar(30750.0, 30759.0, 30700.0, 30707.75)
    mes_bar = _bar(7775.0, 7778.0, 7765.0, 7766.5)
    now = _pts("09:34")
    assert MicroSmt("bearish").on_bar_close(now, mnq_bar, mes_bar, PRE_PREV) is not None
    m = MicroSmt("bearish")
    assert m.on_bar_close(now, mnq_bar, mes_bar, PRE_PREV,
                          mnq_hist=mnq_hist, mes_hist=mes_hist) is None
    assert m.state()["mnq_broken"] and m.state()["mes_broken"]


def test_a_pre_window_single_break_fires_on_an_in_window_confirmation():
    """MNQ broke 30753.50 at 09:10 (to 30760.00) while MES held. The first bar O3 is asked
    about breaks nothing but closes red on both -> fires. The stop reads MNQ's running
    high from 09:00, 30760.00, not this bar's own 30758.00."""
    mnq_hist = _frame({"09:05": Q_MNQ, "09:10": (30750.0, 30760.0, 30745.0, 30748.0),
                       "09:20": Q_MNQ})
    mes_hist = _frame({"09:05": Q_MES, "09:10": Q_MES, "09:20": Q_MES})
    fire = MicroSmt("bearish").on_bar_close(
        _pts("09:32"), _bar(30757.0, 30758.0, 30750.0, 30752.0),
        _bar(7775.0, 7776.0, 7772.0, 7773.0), PRE_PREV,
        mnq_hist=mnq_hist, mes_hist=mes_hist)
    assert fire is not None
    assert fire["swept_by"] == "MNQ"
    assert fire["prev_extreme"] == pytest.approx(30753.5)
    assert fire["stop"] == pytest.approx(30762.0)          # 30760.00 + 2, cap not binding
    assert fire["stop"] == pytest.approx(
        capped_stop(30752.0, 30760.0, buffer_pts=SL_BUFFER_PTS, cap_pts=SL_CAP_PTS,
                    short=True))


def test_catch_up_reads_no_bar_at_or_after_the_current_bars_label():
    """No lookahead: history rows labelled at or after the current bar's label (the bar
    itself, and the in-progress minute) belong to the caller's `mnq_bar`/`mes_bar` or to
    the future -- a MES break there must not cancel the fire."""
    mnq_hist = _frame({"09:10": (30750.0, 30760.0, 30745.0, 30748.0), "09:31": Q_MNQ,
                       "09:32": Q_MNQ})
    mes_hist = _frame({"09:10": Q_MES, "09:31": POKE_MES, "09:32": POKE_MES})
    fire = MicroSmt("bearish").on_bar_close(
        _pts("09:32"), _bar(30757.0, 30758.0, 30750.0, 30752.0),
        _bar(7775.0, 7776.0, 7772.0, 7773.0), PRE_PREV, mnq_hist=mnq_hist,
        mes_hist=mes_hist)
    assert fire is not None


def test_catch_up_ignores_bars_before_the_micro_session_opened():
    """An 08:59 MES poke above 7781.50 belongs to the PREVIOUS micro-session; it is not
    a break of the current one."""
    mnq_hist = _frame({"08:59": Q_MNQ, "09:10": (30750.0, 30760.0, 30745.0, 30748.0)})
    mes_hist = _frame({"08:59": POKE_MES, "09:10": Q_MES})
    fire = MicroSmt("bearish").on_bar_close(
        _pts("09:32"), _bar(30757.0, 30758.0, 30750.0, 30752.0),
        _bar(7775.0, 7776.0, 7772.0, 7773.0), PRE_PREV, mnq_hist=mnq_hist,
        mes_hist=mes_hist)
    assert fire is not None


@pytest.mark.parametrize("mes_poke, fires", [(False, True), (True, False)])
def test_market_mechanisms_entry_reads_the_preopen_pair_and_the_session_history(
        monkeypatch, mes_poke, fires):
    """The seam: `micro_smt_entry_on_bar_close` reads 07:30-09:00 as the predecessor and
    hands the frames over as history, so a 09:10 MES poke it was never asked about still
    cancels the 09:33 MNQ break."""
    from agent.trader.market_mechanisms import MarketMechanisms
    monkeypatch.setattr(micro_smt, "MICRO_SMT_PREOPEN_PAIR_ENABLED", True)
    mnq = _frame({"07:45": (30700.0, 30753.5, 30600.0, 30700.0), "09:10": Q_MNQ,
                  "09:33": (30750.0, 30759.0, 30700.0, 30707.75)})
    mes = _frame({"07:45": (7770.0, 7781.5, 7750.0, 7770.0),
                  "09:10": POKE_MES if mes_poke else Q_MES,
                  "09:33": (7775.0, 7778.0, 7765.0, 7766.5)})
    mm = MarketMechanisms("DOWN", _pts("09:20"))
    fire = mm.micro_smt_entry_on_bar_close(_pts("09:34"), mnq.loc[_pts("09:33")],
                                           mes.loc[_pts("09:33")], mnq, mes)
    assert (fire is not None) is fires
    if fires:
        assert fire["mechanism"] == "micro_smt_reject"
        assert fire["time"] == _pts("09:34")
    # O4 is untouched by §7a.1: no predecessor before 10:30, so it cannot fire.
    assert mm.micro_smt_exit_on_bar_close(_pts("09:34"), mnq.loc[_pts("09:33")],
                                          mes.loc[_pts("09:33")], mnq, mes) is None


RED_MNQ = (30750.0, 30752.0, 30740.0, 30742.0)    # closes down, breaks nothing
RED_MES = (7775.0, 7776.0, 7771.0, 7772.0)
BREAK_MNQ = (30750.0, 30760.0, 30745.0, 30748.0)   # MNQ above 30753.50
LATCH_0930 = _pts("09:30")


def test_an_unasked_in_window_confirmation_consumes_the_session():
    """Operator 2026-09-29: MNQ broke at 09:10; bar 09:33 (completing 09:34, in the
    window) closes red on BOTH while O3 was not asked (a position open). That spends the
    session's fire: the 09:47 red bar O3 IS asked about must not fire."""
    mnq_hist = _frame({"09:10": BREAK_MNQ, "09:33": RED_MNQ, "09:40": Q_MNQ})
    mes_hist = _frame({"09:10": Q_MES, "09:33": RED_MES, "09:40": Q_MES})
    m = MicroSmt("bearish")
    assert m.on_bar_close(_pts("09:48"), _bar(*RED_MNQ), _bar(*RED_MES), PRE_PREV,
                          mnq_hist=mnq_hist, mes_hist=mes_hist,
                          latch_from=LATCH_0930) is None
    assert m.state()["fired_sessions"] == [str(_pts("09:00"))]


def test_a_pre_window_confirmation_does_not_consume_the_session():
    """The same shape with the confirming bar at 09:20 (completing 09:21, before the
    09:30 window) does NOT latch: the first asked in-window confirmation fires."""
    mnq_hist = _frame({"09:10": BREAK_MNQ, "09:20": RED_MNQ, "09:40": Q_MNQ})
    mes_hist = _frame({"09:10": Q_MES, "09:20": RED_MES, "09:40": Q_MES})
    fire = MicroSmt("bearish").on_bar_close(
        _pts("09:48"), _bar(*RED_MNQ), _bar(*RED_MES), PRE_PREV,
        mnq_hist=mnq_hist, mes_hist=mes_hist, latch_from=LATCH_0930)
    assert fire is not None and fire["time"] == _pts("09:48")


def test_a_confirmation_on_the_boundary_bar_0929_is_in_window():
    """Bar 09:29 completes at 09:30:00, the window's first instant -> it latches."""
    mnq_hist = _frame({"09:10": BREAK_MNQ, "09:29": RED_MNQ})
    mes_hist = _frame({"09:10": Q_MES, "09:29": RED_MES})
    assert MicroSmt("bearish").on_bar_close(
        _pts("09:48"), _bar(*RED_MNQ), _bar(*RED_MES), PRE_PREV,
        mnq_hist=mnq_hist, mes_hist=mes_hist, latch_from=LATCH_0930) is None


def test_the_unasked_confirmation_is_read_from_1s_history_as_completed_1m_bars():
    """1s history: the 09:33 minute is resampled into one left-labelled 1m bar (first
    Open, last Close). Its individual seconds alternate colour; only the MINUTE's
    close-vs-open counts, and it is red on both -> latched."""
    def secs(hhmm, o, c, hi, lo):
        start = _pts(hhmm)
        idx = pd.date_range(start, start + pd.Timedelta(seconds=59), freq="1s")
        px = [o + (c - o) * i / 59 + (1.0 if i % 2 else -1.0) for i in range(60)]
        px[0], px[-1] = o, c
        return pd.DataFrame({"Open": px, "High": [max(p, hi) if i == 30 else p + 0.25
                                                  for i, p in enumerate(px)],
                             "Low": [min(p, lo) if i == 30 else p - 0.25
                                     for i, p in enumerate(px)],
                             "Close": px, "Volume": 1.0}, index=idx)
    mnq_hist = pd.concat([secs("09:10", 30750.0, 30748.0, 30760.0, 30745.0),
                          secs("09:33", 30750.0, 30742.0, 30752.0, 30740.0)])
    mes_hist = pd.concat([secs("09:10", 7770.0, 7770.0, 7771.0, 7769.0),
                          secs("09:33", 7775.0, 7772.0, 7776.0, 7771.0)])
    m = MicroSmt("bearish")
    assert m.on_bar_close(_pts("09:48"), _bar(*RED_MNQ), _bar(*RED_MES), PRE_PREV,
                          mnq_hist=mnq_hist, mes_hist=mes_hist,
                          latch_from=LATCH_0930) is None
    assert m.state()["fired_sessions"] == [str(_pts("09:00"))]


def test_the_latch_ignores_the_current_bar_and_later_history():
    """No lookahead: a confirming bar in the history AT the current bar's label (or
    after) is not catch-up material -- the current bar is judged, and fires, normally."""
    mnq_hist = _frame({"09:10": BREAK_MNQ, "09:47": RED_MNQ, "09:48": RED_MNQ})
    mes_hist = _frame({"09:10": Q_MES, "09:47": RED_MES, "09:48": RED_MES})
    fire = MicroSmt("bearish").on_bar_close(
        _pts("09:48"), _bar(*RED_MNQ), _bar(*RED_MES), PRE_PREV,
        mnq_hist=mnq_hist, mes_hist=mes_hist, latch_from=LATCH_0930)
    assert fire is not None


def test_entry_latch_from_is_the_window_open_of_the_sessions_own_pair(monkeypatch):
    monkeypatch.setattr(micro_smt, "MICRO_SMT_PREOPEN_PAIR_ENABLED", True)
    assert micro_smt.entry_latch_from(_pts("09:00")) == _pts("09:30")
    assert micro_smt.entry_latch_from(_pts("10:30")) == _pts("10:30")
    assert micro_smt.entry_latch_from(_pts("12:00")) == _pts("12:00")


def test_market_mechanisms_entry_latches_on_an_unasked_confirmation(monkeypatch):
    """The seam passes the pair's window open, so the executor-level behaviour holds."""
    from agent.trader.market_mechanisms import MarketMechanisms
    monkeypatch.setattr(micro_smt, "MICRO_SMT_PREOPEN_PAIR_ENABLED", True)
    mnq = _frame({"07:45": (30700.0, 30753.5, 30600.0, 30700.0), "09:10": BREAK_MNQ,
                  "09:33": RED_MNQ, "09:47": RED_MNQ})
    mes = _frame({"07:45": (7770.0, 7781.5, 7750.0, 7770.0), "09:10": Q_MES,
                  "09:33": RED_MES, "09:47": RED_MES})
    mm = MarketMechanisms("DOWN", _pts("09:20"))
    assert mm.micro_smt_entry_on_bar_close(_pts("09:48"), mnq.loc[_pts("09:47")],
                                           mes.loc[_pts("09:47")], mnq, mes) is None


def test_0928_real_tape_preopen_pair_fires_at_0934(monkeypatch):
    """§7a.1's motivating day, over the real 1m tape: 07:30-09:00 highs MNQ 30753.50
    (08:57), MES 7781.50 (08:26); bar 09:32 takes MNQ to 30759.00 (MES 7775.25) and
    closes up; bar 09:33 closes down on both -> fires 09:34:00 @ 30707.75, stop
    min(30761.00, 30722.75) = 30722.75. Figures read from registry key `sec7a1-0928`."""
    monkeypatch.setattr(micro_smt, "MICRO_SMT_PREOPEN_PAIR_ENABLED", True)
    case = nc.by_key("sec7a1-0928")
    try:
        from backtest_smt import _main_dir_for_date
        d = _main_dir_for_date(PRE_DATE)
        mnq = pd.read_parquet(d / "MNQ_1m.parquet").loc[PRE_DATE]
        mes = pd.read_parquet(d / "MES_1m.parquet").loc[PRE_DATE]
    except Exception:
        pytest.skip("2026-09-28 main 1m parquet not available in this environment")
    for df in (mnq, mes):
        if not len(df) or df.index.min() > _pts("07:30") or df.index.max() < _pts("09:40"):
            pytest.skip("2026-09-28 1m bars do not cover 07:30-09:40 in this environment")

    pre = entry_previous_micro_extremes(mnq, mes, _pts("09:31"))
    assert pre["mnq_high"] == pytest.approx(30753.50)
    assert pre["mes_high"] == pytest.approx(7781.50)

    m = MicroSmt("bearish")
    fire = None
    for label, mnq_row in mnq.between_time("09:30", "10:28").iterrows():
        if label not in mes.index:
            continue
        now = label + pd.Timedelta(minutes=1)
        fire = m.on_bar_close(now, mnq_row, mes.loc[label],
                              entry_previous_micro_extremes(mnq, mes, now),
                              mnq_hist=mnq[mnq.index < now], mes_hist=mes[mes.index < now])
        if fire is not None:
            break
    assert fire is not None
    assert fire["time"] == _pts(case.entry_time)
    assert fire["price"] == pytest.approx(case.entry_price)
    assert fire["stop"] == pytest.approx(case.stop)
    assert fire["swept_by"] == "MNQ"
    assert fire["prev_extreme"] == pytest.approx(30753.50)


@pytest.mark.slow
@pytest.mark.timeout(900)
def test_0928_counterfactual_down_replay_is_this_one_trade(tmp_path, monkeypatch):
    """`sec7a1-0928` end to end: a real replay of 2026-09-28 under the operator's
    COUNTERFACTUAL DOWN thesis takes exactly one trade, O3 on the pre-open pair at
    09:34:00, to T2 london(cur)_low 30535.0 at 10:11:14."""
    import json
    import os
    from agent.trader.replay import run_replay
    from scripts.report_replay_pnl import summarize

    case = nc.by_key("sec7a1-0928")
    monkeypatch.setenv("ACT_THESIS_CACHE_DIR", str(tmp_path / "thesis_cache"))
    monkeypatch.delenv("ACT_TRADER_5M", raising=False)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_ENTRY_ENABLED", True)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_EXIT_ENABLED", True)
    monkeypatch.setattr(micro_smt, "MICRO_SMT_PREOPEN_PAIR_ENABLED", True)
    try:
        res = run_replay([case.date], allow_calls=False,
                         thesis=nc.thesis_for(case.key))[case.date]
    except Exception as exc:                        # no 09-28 tape in this environment
        pytest.skip(f"2026-09-28 replay unavailable: {type(exc).__name__}: {exc}")
    rows = [json.loads(line) for line in open(
        os.path.join(res["run_dir"], "trader_decisions.jsonl"), encoding="utf-8")
        if line.strip()]
    fills = [r for r in rows if r.get("kind") == "fill"]
    assert len(fills) == case.attempts_used, [(f["time"], f["mechanism"]) for f in fills]
    assert fills[0]["mechanism"] == case.mechanism
    assert fills[0]["time"].endswith(f"{case.entry_time}-04:00")
    assert fills[0]["price"] == case.entry_price
    tps = [r for r in rows if r.get("kind") == "take_profit"]
    assert tps and tps[-1]["time"].endswith(f"{case.exit_time}-04:00")
    assert tps[-1]["price"] == case.exit_price
    assert round(summarize(res["run_dir"])["total_pts"], 2) == case.pnl
