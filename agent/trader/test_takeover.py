"""Task 8: the §8 deeper-gap takeover.

A SHORT plan unless stated: the adverse path is UP, so "deeper" means higher.
"""
import pandas as pd

from agent.trader.episode import Episode
from agent.trader.takeover import (COOLDOWN_RESOLUTION_ORDER, Takeover, crossed,
                                   deepest_penetrated, is_deeper, penetrated,
                                   resolve_cooldown_end)
from agent.facts.records import Fact, FactClass, FactState

TZ = "America/New_York"


def _gap(lo, hi, *, tf="5min", ref="2026-08-14 09:11", direction="bear", gid=None):
    return Fact(id=gid or f"{tf}-{lo}-{hi}", cls=FactClass.FVG, ticker="MNQ",
                label=f"MNQ {tf} {direction} FVG [{lo}, {hi}]", name=None,
                reference_ts=pd.Timestamp(ref, tz=TZ), price=None,
                price_low=lo, price_high=hi, timeframe=tf, resolution=tf,
                state=FactState.LIVE, state_ts=pd.Timestamp(ref, tz=TZ),
                provenance={}, extra={"direction": direction,
                                      "max_anti_excursion": 0.0})


FAILED = _gap(30240.0, 30252.0, gid="failed-5m")


# --- the scanner ------------------------------------------------------------- #

def test_the_scanner_enumerates_all_timeframes():
    """§11's named failure: 07-21's takeover gap is a 1m gap [29137.25, 29138.5]."""
    one = _gap(29137.25, 29138.5, tf="1min", gid="tk-1m")
    failed = _gap(29120.0, 29130.0, gid="failed")
    got = deepest_penetrated([one], failed, "DOWN", low=29137.0, high=29139.0)
    assert got is not None and got.id == "tk-1m" and got.timeframe == "1min"


def test_a_5m_only_scan_is_what_this_module_exists_to_prevent():
    """The regression the doc escalates to an imperative: filtering to 5m loses the
    takeover entirely on 07-21 and mis-binds it on 07-31."""
    one = _gap(29137.25, 29138.5, tf="1min", gid="tk-1m")
    failed = _gap(29120.0, 29130.0, gid="failed")
    five_only = [g for g in [one] if g.timeframe == "5min"]
    assert deepest_penetrated(five_only, failed, "DOWN",
                              low=29137.0, high=29139.0) is None


def test_the_stop_out_bar_itself_is_included_in_the_penetration_window():
    """08-14: the SL tick and the takeover penetration are the SAME tick, so the test is
    on the observed RANGE, not on a later close."""
    deep = _gap(30266.5, 30269.5, tf="1min", gid="tk")
    assert penetrated(deep, low=30240.0, high=30268.75)


def test_the_deepest_penetrated_gap_wins():
    """07-31 binds [28455.75, 28477] (1m, 08:53), NOT the shallower 5m
    [28496.75, 28508.5]. A LONG plan: the adverse path is DOWN, so deeper means lower."""
    failed = _gap(28514.5, 28529.25, direction="bull", gid="failed-long")
    shallow = _gap(28496.75, 28508.5, direction="bull", gid="5m-shallow")
    deep = _gap(28455.75, 28477.0, tf="1min", direction="bull", gid="1m-deep")
    got = deepest_penetrated([shallow, deep], failed, "UP",
                             low=28450.0, high=28530.0)
    assert got.id == "1m-deep"


def test_a_shallower_gap_is_never_a_takeover():
    shallow = _gap(30200.0, 30210.0, gid="shallow")
    assert not is_deeper(shallow, FAILED, "DOWN")
    assert deepest_penetrated([shallow], FAILED, "DOWN",
                              low=30190.0, high=30260.0) is None


def test_the_failed_gap_is_never_its_own_takeover():
    assert not is_deeper(FAILED, FAILED, "DOWN")


def test_a_deeper_gap_that_price_never_reached_is_not_a_takeover():
    deep = _gap(30300.0, 30310.0, tf="1min", gid="tk")
    assert deepest_penetrated([deep], FAILED, "DOWN",
                              low=30240.0, high=30268.75) is None


# --- the blacklist ----------------------------------------------------------- #

def test_the_blacklist_is_conditional_on_a_deeper_penetration():
    """With no deeper gap penetrated the same failed gap may re-bind — 07-23's validated
    same-gap re-entry depends on this."""
    tk = Takeover("DOWN")
    assert tk.on_stop_out(failed=FAILED, candidates=[], low=30240.0,
                          high=30250.0) is None
    assert tk.blacklist == [], "an unconditional blacklist breaks 07-23"


def test_a_deeper_penetration_blacklists_the_failed_gap():
    tk = Takeover("DOWN")
    deep = _gap(30266.5, 30269.5, tf="1min", gid="tk")
    assert tk.on_stop_out(failed=FAILED, candidates=[deep], low=30240.0,
                          high=30268.75) is deep
    assert tk.blacklist == [FAILED.id]


def test_the_blacklist_does_not_duplicate_across_stop_outs():
    tk = Takeover("DOWN")
    deep = _gap(30266.5, 30269.5, tf="1min", gid="tk")
    for _ in range(3):
        tk.on_stop_out(failed=FAILED, candidates=[deep], low=30240.0, high=30268.75)
    assert tk.blacklist == [FAILED.id]


# --- the exemptions ---------------------------------------------------------- #

def test_the_takeover_gap_is_min_height_exempt():
    """08-14's takeover gap is 3.0 pts tall — below the 5-pt initial-binding minimum."""
    tiny = _gap(30266.5, 30269.5, tf="1min", gid="tk")
    assert float(tiny.price_high) - float(tiny.price_low) == 3.0
    got = deepest_penetrated([tiny], FAILED, "DOWN", low=30240.0, high=30268.75)
    assert got is tiny, "the deepest-penetration role is min-height EXEMPT"


def test_the_takeover_gap_is_exempt_from_the_0930_creating_bar_filter():
    """08-14's takeover gap was created 09:11 — before the RTH open."""
    early = _gap(30266.5, 30269.5, tf="1min", ref="2026-08-14 09:11", gid="tk")
    assert early.reference_ts.strftime("%H:%M") == "09:11"
    assert deepest_penetrated([early], FAILED, "DOWN",
                              low=30240.0, high=30268.75) is early


# --- re-entry mode ----------------------------------------------------------- #

def test_re_entry_uses_the_episode_machinery_not_a_resting_stop():
    ep = Episode(30266.5, 30269.5, "DOWN", gap_id="tk", sl_cap_gate=True)
    ep.on_tick(pd.Timestamp("2026-08-14 09:31:05", tz=TZ), 30268.0, bar_open=30266.0)
    assert ep.state()["cycle"] == "entering"


def test_a_close_verdict_entry_over_the_30pt_cap_is_skipped():
    """08-14: the 09:46 red close (extreme 30275.5, dist 36) and 09:49 (dist 43.5)."""
    ep = Episode(30266.5, 30269.5, "DOWN", gap_id="tk", sl_cap_gate=True)
    t = pd.Timestamp("2026-08-14 09:46:00", tz=TZ)
    ep.on_tick(t, 30268.0, bar_open=30266.0)
    ep.on_tick(t + pd.Timedelta(seconds=20), 30275.5, bar_open=30266.0)
    fire = ep.on_bar_close(t + pd.Timedelta(minutes=1),
                           {"Open": 30268.0, "Close": 30241.5,
                            "High": 30275.5, "Low": 30240.0}, mid=30241.5)
    assert fire is None and ep.state()["sl_cap_skips"] == 1


def test_an_exit_tick_entry_is_never_length_gated():
    """07-17: the 51-pt breakdown bar enters via exit-tick; a fixed length gate forfeits
    that day's entire winner."""
    ep = Episode(28660.0, 28680.0, "DOWN", gap_id="tk", sl_cap_gate=True)
    t = pd.Timestamp("2026-07-17 09:31:14", tz=TZ)
    ep.on_tick(t, 28670.0, bar_open=28665.0)
    ep.on_tick(t + pd.Timedelta(seconds=10), 28720.0, bar_open=28665.0)   # 50 pts away
    ep.on_bar_close(t + pd.Timedelta(seconds=46),
                    {"Open": 28665.0, "Close": 28670.0,
                     "High": 28720.0, "Low": 28662.0})                    # closes inside
    fire = ep.on_tick(pd.Timestamp("2026-07-17 09:32:05", tz=TZ), 28655.0, mid=28655.0)
    assert fire is not None and fire["kind"] == "exit_tick"


# --- crossed-trigger precedence ---------------------------------------------- #

def test_a_crossed_failed_trigger_at_cooldown_end_takes_precedence():
    """08-14: at 09:32:00 the old trigger 30252.25 IS crossed -> market at the 1s mid
    30245.5, SL 30268 (the failed binding's)."""
    assert crossed(30252.25, 30245.5, "DOWN")
    assert resolve_cooldown_end(failed_trigger=30252.25, price=30245.5,
                                direction="DOWN") == "crossed_trigger_market"


def test_an_uncrossed_trigger_at_cooldown_end_lets_the_takeover_govern():
    """07-21: all three cooldown ends uncrossed (price 29139.5 vs trigger 29171);
    07-31 likewise."""
    assert not crossed(29171.0, 29139.5, "UP")
    assert resolve_cooldown_end(failed_trigger=29171.0, price=29139.5,
                                direction="UP") == "resting"


def test_the_precedence_rule_prefers_a_crossed_trigger_over_a_ready_close_verdict():
    """07-24 in one line: the episode's SL-cap gate would have SKIPPED the +146.5 at
    45.25 pts, recreating the exact lockout the cooldown rule was built to kill."""
    got = resolve_cooldown_end(failed_trigger=28590.0, price=28579.0,
                               direction="DOWN", close_verdict={"price": 28575.0})
    assert got == "crossed_trigger_market"


def test_an_uncrossed_trigger_with_a_ready_close_verdict_takes_the_close_verdict():
    got = resolve_cooldown_end(failed_trigger=29171.0, price=29139.5, direction="UP",
                               close_verdict={"price": 29142.5})
    assert got == "close_verdict"


def test_the_resolution_order_is_the_documented_one():
    assert COOLDOWN_RESOLUTION_ORDER == ("crossed_trigger_market", "close_verdict",
                                         "resting")


def test_a_long_plan_mirrors_the_crossed_test():
    assert crossed(29900.0, 29910.0, "UP")
    assert not crossed(29900.0, 29890.0, "UP")


# --- the seam: the Executor's cooldown end asks §8's question ---------------- #

def test_the_executor_resolves_its_cooldown_end_through_the_sec8_rule(tmp_path):
    """§8's precedence is one rule with a load-bearing ORDER. Two copies of it is how
    they come to disagree, so the Executor delegates rather than re-deriving — and the
    verdict is recorded in state so a replay can be read back."""
    from agent.trader.executor import Executor
    from agent.trader.order_sim import RestingOrder

    ex = Executor(str(tmp_path),
                  {"plan_id": "t", "direction": "DOWN", "dol": {"price": 29000.0},
                   "armed_classes": ["fvg_return_continuation"]},
                  arm_ts=pd.Timestamp("2026-08-14 09:20", tz=TZ))
    ex._state.update({"trigger": 30252.25, "stop": 30268.0, "bound_id": "failed-5m",
                      "now_price": 30245.5, "now_low": 30245.5, "now_high": 30246.0,
                      "now_mid": 30245.5})
    ex._resolve_cooldown_end(pd.Timestamp("2026-08-14 09:32:00", tz=TZ))
    assert ex.bind_state()["cooldown_resolution"] == "crossed_trigger_market"
    assert ex._sim.position is not None, "a crossed trigger executes at market"
    assert ex._sim.position["entry"] == 30245.5, "at the 1s mid at placement"


def test_an_uncrossed_trigger_at_the_executors_cooldown_end_rests(tmp_path):
    """07-21: price 29139.5 against a 29171 trigger — uncrossed, so nothing fires at
    market and the order goes back to resting."""
    from agent.trader.executor import Executor

    ex = Executor(str(tmp_path),
                  {"plan_id": "t", "direction": "UP", "dol": {"price": 29800.0},
                   "armed_classes": ["fvg_return_continuation"]},
                  arm_ts=pd.Timestamp("2026-07-21 09:20", tz=TZ))
    ex._state.update({"trigger": 29171.0, "stop": 29146.0, "bound_id": "failed-5m",
                      "now_price": 29139.5, "now_low": 29135.0, "now_high": 29139.5,
                      "now_mid": 29137.25})
    ex._resolve_cooldown_end(pd.Timestamp("2026-07-21 09:34:00", tz=TZ))
    assert ex.bind_state()["cooldown_resolution"] == "resting"
    assert ex._sim.position is None and ex._sim.resting is not None
    assert ex._sim.resting.trigger == 29171.0


def test_the_close_verdict_branch_is_unreachable_until_the_episode_path_is_wired():
    """Recorded, not hidden. §8's full order is crossed-trigger -> close verdict ->
    resting; the middle branch needs a completed episode close verdict of the stop-out
    bar, and the episode machinery is not on the Executor's entry path in this cycle. The
    RULE is implemented and tested in `takeover.resolve_cooldown_end`; only its third
    input is always None at the call site."""
    import inspect

    from agent.trader.executor import Executor
    src = inspect.getsource(Executor._resolve_cooldown_end)
    assert "close_verdict=None" in src
    assert resolve_cooldown_end(failed_trigger=29171.0, price=29139.5, direction="UP",
                                close_verdict={"price": 29142.5}) == "close_verdict"
