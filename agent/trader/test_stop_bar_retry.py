"""O1 (`l2-mechanisms.md` §7c): the stop-bar retry at MECHANISM level, for `tmso_reject`
and `micro_smt_reject`, over synthetic bars.

The judgement lives in the machines (`arm_retry` / `retry_on_bar_close`); the Executor's
gating around it (cooldown timing, the extension veto, the attempt budget, the cutoff,
never chained, the records) is `test_executor_stop_bar_retry.py`'s job. Every
expectation here is the rule as written in §7c, not read back off the code: bar B is the
1m bar that took the stop, judged at its close; D1 = close with the thesis against B's
own open (D4 = on MES as well, O3 only); the stop is the mechanism's OWN rule; one retry
per stop-out; the per-micro-session latch untouched.
"""
import pandas as pd
import pytest

import agent.trader.micro_smt as micro_smt
import agent.trader.tmso_reject as tr
from agent.trader.micro_smt import MicroSmt
from agent.trader.tmso_reject import TmsoReject

TZ = "America/New_York"
DATE = "2026-09-29"
TMSO = 30000.0


def _ts(hms):
    return pd.Timestamp(f"{DATE} {hms}", tz=TZ)


def _bar(o, h, l, c, label=None):
    return pd.Series({"Open": o, "High": h, "Low": l, "Close": c}, name=label)


def _stop_out(hms, entry):
    return {"kind": "stop_out", "time": _ts(hms), "entry": entry, "direction": "DOWN"}


# --------------------------------------------------------------------------- #
# tmso_reject                                                                  #
# --------------------------------------------------------------------------- #

def _fired_tmso(monkeypatch=None):
    """A DOWN machine that fired at 09:32:00 off a sweep bar (H 30005 over TMSO 30000,
    closes 29995 under it, red): entry 29995, stop min(30005, 30010) = 30005."""
    m = TmsoReject("DOWN")
    fire = m.on_bar_close(_ts("09:32:00"), _bar(30002.0, 30005.0, 29990.0, 29995.0), TMSO)
    assert fire is not None and fire["price"] == 29995.0 and fire["stop"] == 30005.0
    return m


def test_tmso_retry_fires_on_a_favourable_close_with_its_own_stop_rule():
    m = _fired_tmso()
    armed = m.arm_retry(_stop_out("09:33:14", 29995.0))
    assert armed["bar"] == _ts("09:33:00") and armed["retry_at"] == _ts("09:34:00")
    assert m.retry_pending() is not None
    # Bar B: red (D1), high 30008 beyond the swept extreme 30005.
    fire, reason = m.retry_on_bar_close(_ts("09:34:00"),
                                        _bar(30000.0, 30008.0, 29980.0, 29990.0))
    assert reason is None and fire is not None
    assert fire["mechanism"] == "tmso_reject" and fire["direction"] == "DOWN"
    assert fire["time"] == _ts("09:34:00") and fire["price"] == 29990.0
    # The running adverse extreme since the sweep, max(30005, 30008) = 30008, capped at
    # 15 from the retry's entry: min(30008, 30005) = 30005. No +2 buffer: tmso has none.
    assert fire["stop"] == 30005.0
    assert fire["retry_of"] == {"stop_out_time": _ts("09:33:14").isoformat(),
                                "entry": 29995.0, "bar": _ts("09:33:00").isoformat()}
    assert m.retry_pending() is None, "one retry per stop-out: judged, then gone"


def test_tmso_no_retry_on_an_adverse_close():
    m = _fired_tmso()
    m.arm_retry(_stop_out("09:33:14", 29995.0))
    fire, reason = m.retry_on_bar_close(_ts("09:34:00"),
                                        _bar(30000.0, 30008.0, 29990.0, 30003.0))  # green
    assert fire is None and reason == "adverse_close"
    assert m.retry_pending() is None


def test_tmso_retry_judged_on_a_later_bar_is_missed():
    m = _fired_tmso()
    m.arm_retry(_stop_out("09:33:14", 29995.0))
    fire, reason = m.retry_on_bar_close(_ts("09:35:00"),
                                        _bar(30000.0, 30008.0, 29980.0, 29990.0))
    assert fire is None and reason == "missed"


def test_tmso_retry_must_stay_inside_the_fires_micro_session():
    """A stop-out at 10:29:40 is judged at 10:30:00 -- the next micro-session, whose
    TMSO is a different level. No retry."""
    m = TmsoReject("DOWN")
    fire = m.on_bar_close(_ts("10:29:00"), _bar(30002.0, 30005.0, 29990.0, 29995.0), TMSO)
    assert fire is not None
    m.arm_retry(_stop_out("10:29:40", 29995.0))
    fire, reason = m.retry_on_bar_close(_ts("10:30:00"),
                                        _bar(30000.0, 30008.0, 29980.0, 29990.0))
    assert fire is None and reason == "micro_session_ended"


def test_tmso_constant_false_arms_nothing(monkeypatch):
    monkeypatch.setattr(tr, "STOP_BAR_RETRY", False)
    m = _fired_tmso()
    assert m.arm_retry(_stop_out("09:33:14", 29995.0)) is None
    assert m.retry_pending() is None
    assert m.retry_on_bar_close(_ts("09:34:00"),
                                _bar(30000.0, 30008.0, 29980.0, 29990.0)) == (None, None)


def test_tmso_a_stop_out_before_any_fire_of_ours_arms_nothing():
    m = TmsoReject("DOWN")
    assert m.arm_retry(_stop_out("09:33:14", 29995.0)) is None


def test_tmso_the_retry_does_not_lift_the_latch_and_a_fresh_micro_session_fires_once():
    m = _fired_tmso()
    m.arm_retry(_stop_out("09:33:14", 29995.0))
    fire, _ = m.retry_on_bar_close(_ts("09:34:00"), _bar(30000.0, 30008.0, 29980.0, 29990.0))
    assert fire is not None
    # Same micro-session, another perfect sweep-and-reject bar: latched, as today.
    assert m.on_bar_close(_ts("09:40:00"), _bar(30002.0, 30005.0, 29990.0, 29995.0),
                          TMSO) is None
    # The next micro-session (10:30-12:00) fires once, then latches -- exactly as today.
    tmso2 = 30100.0
    assert m.on_bar_close(_ts("10:55:00"), _bar(30102.0, 30105.0, 30090.0, 30095.0),
                          tmso2) is not None
    assert m.on_bar_close(_ts("10:58:00"), _bar(30102.0, 30105.0, 30090.0, 30095.0),
                          tmso2) is None


# --------------------------------------------------------------------------- #
# micro_smt_reject (O3)                                                        #
# --------------------------------------------------------------------------- #

PREV = {"mnq_high": 30000.0, "mnq_low": 29900.0, "mes_high": 7700.0, "mes_low": 7650.0,
        "session_start": _ts("09:00:00")}
MNQ_SWEEP = _bar(30003.0, 30005.0, 29990.0, 29995.0)      # breaks 30000, closes red
MES_HOLD = _bar(7699.0, 7699.5, 7690.0, 7695.0)           # holds under 7700, closes red


def _fired_o3():
    """A bearish machine that fired at 09:32:00: MNQ broke its 07:30-09:00 high, MES
    did not, both closed red. Entry 29995, stop min(30005 + 2, 29995 + 15) = 30007."""
    m = MicroSmt("bearish")
    fire = m.on_bar_close(_ts("09:32:00"), MNQ_SWEEP, MES_HOLD, PREV)
    assert fire is not None and fire["price"] == 29995.0 and fire["stop"] == 30007.0
    return m


def test_o3_retry_fires_when_both_assets_close_favourable_and_the_divergence_is_live():
    m = _fired_o3()
    armed = m.arm_retry(_stop_out("09:33:14", 29995.0))
    assert armed["bar"] == _ts("09:33:00") and armed["session"] == _ts("09:00:00")
    mnq_b = _bar(30000.0, 30012.0, 29980.0, 29992.0)      # red; new MNQ extreme 30012
    mes_b = _bar(7698.0, 7699.5, 7685.0, 7690.0)          # red; still under 7700
    fire, reason = m.retry_on_bar_close(_ts("09:34:00"), mnq_b, mes_b, PREV)
    assert reason is None and fire is not None
    assert fire["direction"] == "DOWN" and fire["price"] == 29992.0
    # O3's own rule: MNQ's running micro-session extreme (now 30012) + 2, capped at 15
    # from the retry's entry: min(30014, 30007) = 30007.
    assert fire["stop"] == 30007.0
    assert fire["retry_of"]["entry"] == 29995.0
    assert m.retry_pending() is None


def test_o3_no_retry_when_mnq_closed_adverse():
    m = _fired_o3()
    m.arm_retry(_stop_out("09:33:14", 29995.0))
    fire, reason = m.retry_on_bar_close(
        _ts("09:34:00"), _bar(30000.0, 30012.0, 29980.0, 30004.0), MES_HOLD, PREV)
    assert fire is None and reason == "adverse_close"


def test_o3_no_retry_when_mes_closed_adverse():
    """D4: the both-assets test the confirmation uses applies to the retry too."""
    m = _fired_o3()
    m.arm_retry(_stop_out("09:33:14", 29995.0))
    mes_green = _bar(7690.0, 7699.5, 7685.0, 7698.0)
    fire, reason = m.retry_on_bar_close(
        _ts("09:34:00"), _bar(30000.0, 30012.0, 29980.0, 29992.0), mes_green, PREV)
    assert fire is None and reason == "mes_adverse_close"


def test_o3_no_retry_when_the_divergence_was_cancelled_on_bar_b():
    m = _fired_o3()
    m.arm_retry(_stop_out("09:33:14", 29995.0))
    mes_breaks = _bar(7699.0, 7701.0, 7685.0, 7690.0)     # red, but MES now broke 7700
    fire, reason = m.retry_on_bar_close(
        _ts("09:34:00"), _bar(30000.0, 30012.0, 29980.0, 29992.0), mes_breaks, PREV)
    assert fire is None and reason == "divergence_cancelled"


def test_o3_a_break_while_the_position_was_open_cancels_the_retry():
    """§7a.1's whole-session reading: the machine is not asked while a position is
    open, so the retry folds the frames in first. MES breaks on the 09:32 bar."""
    m = _fired_o3()
    m.arm_retry(_stop_out("09:33:14", 29995.0))
    idx = pd.date_range(_ts("09:00:00"), _ts("09:34:00"), freq="1min")
    mnq = pd.DataFrame({"Open": 29995.0, "High": 29996.0, "Low": 29990.0, "Close": 29992.0,
                        "Volume": 1.0}, index=idx)
    mes = pd.DataFrame({"Open": 7695.0, "High": 7696.0, "Low": 7690.0, "Close": 7692.0,
                        "Volume": 1.0}, index=idx)
    mes.loc[_ts("09:32:00"), "High"] = 7701.0              # the unasked break
    fire, reason = m.retry_on_bar_close(
        _ts("09:34:00"), _bar(30000.0, 30012.0, 29980.0, 29992.0), MES_HOLD, PREV,
        mnq_hist=mnq, mes_hist=mes)
    assert fire is None and reason == "divergence_cancelled"


def test_o3_retry_must_stay_inside_the_fires_micro_session():
    m = MicroSmt("bearish")
    assert m.on_bar_close(_ts("10:29:00"), MNQ_SWEEP, MES_HOLD, PREV) is not None
    m.arm_retry(_stop_out("10:29:40", 29995.0))
    prev2 = dict(PREV, session_start=_ts("10:30:00"))
    fire, reason = m.retry_on_bar_close(
        _ts("10:30:00"), _bar(30000.0, 30012.0, 29980.0, 29992.0), MES_HOLD, prev2)
    assert fire is None and reason == "micro_session_ended"


def test_o3_constant_false_arms_nothing(monkeypatch):
    monkeypatch.setattr(micro_smt, "STOP_BAR_RETRY", False)
    m = _fired_o3()
    assert m.arm_retry(_stop_out("09:33:14", 29995.0)) is None
    assert m.retry_pending() is None


def test_o3_a_stop_out_before_any_fire_of_ours_arms_nothing():
    m = MicroSmt("bearish")
    assert m.arm_retry(_stop_out("09:33:14", 29995.0)) is None


def test_o3_the_retry_does_not_lift_the_latch_and_a_fresh_micro_session_fires_once():
    m = _fired_o3()
    m.arm_retry(_stop_out("09:33:14", 29995.0))
    fire, _ = m.retry_on_bar_close(_ts("09:34:00"),
                                   _bar(30000.0, 30012.0, 29980.0, 29992.0), MES_HOLD, PREV)
    assert fire is not None
    # Same session, another confirmation: latched, as today.
    assert m.on_bar_close(_ts("09:40:00"), MNQ_SWEEP, MES_HOLD, PREV) is None
    # A fresh micro-session (10:30-12:00, its own previous extremes) fires once, then
    # latches -- exactly as today.
    prev2 = {"mnq_high": 30050.0, "mnq_low": 29950.0, "mes_high": 7710.0, "mes_low": 7660.0,
             "session_start": _ts("10:30:00")}
    sweep2 = _bar(30053.0, 30055.0, 30040.0, 30045.0)
    hold2 = _bar(7709.0, 7709.5, 7700.0, 7705.0)
    assert m.on_bar_close(_ts("10:40:00"), sweep2, hold2, prev2) is not None
    assert m.on_bar_close(_ts("10:45:00"), sweep2, hold2, prev2) is None


@pytest.mark.parametrize("flag", [True, False])
def test_the_constants_are_read_live_not_captured(monkeypatch, flag):
    """Like every other flag in these modules: an A/B harness flips the module
    attribute between runs in one process."""
    monkeypatch.setattr(tr, "STOP_BAR_RETRY", flag)
    monkeypatch.setattr(micro_smt, "STOP_BAR_RETRY", flag)
    from agent.trader.market_mechanisms import MarketMechanisms
    mm = MarketMechanisms("DOWN", _ts("09:21:00"))
    assert mm.stop_bar_retry_enabled("tmso_reject") is flag
    assert mm.stop_bar_retry_enabled("micro_smt_reject") is flag
    assert mm.stop_bar_retry_enabled("extreme_reject_close") is False
    assert mm.stop_bar_retry_enabled("fvg_1m_post_extreme") is False
