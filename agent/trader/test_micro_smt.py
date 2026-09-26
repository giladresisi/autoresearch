"""`micro_smt_reject` (O3) / `micro_smt_exit` (O4): the divergence detector, the previous-
micro-session read, no-lookahead, and the 2026-09-24 real-tape reproduction.

See `micro_smt.py`'s docstring and `l2-mechanisms.md` §11 for the CANDIDATE's evidence.
"""
import pandas as pd
import pytest

from agent.trader.micro_smt import MicroSmt, SL_BUFFER_PTS, SL_CAP_PTS, previous_micro_extremes
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
