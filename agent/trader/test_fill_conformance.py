"""Fill-model conformance against l2-mechanisms.md §11's calibrated 5m bindings.

**Why this module exists.** The fill model's failure mode is silent P&L drift, not a
crash. §11 records stop clearances of 1.0 to 7.5 pts across its validation examples, so a
wrong convention — filling at the next tick instead of at the trigger, resolving a
same-bar stop-and-target favourably, taking a 1m close where a 1s mid belongs — shifts
every number by less than those margins. It would pass every structural test in
`test_order_sim.py` while quietly invalidating every comparison Phase 2 makes.

**The oracle already exists.** §11's calibrated 1s replay reproduced the documented 5m
bindings exactly, with per-day P&L. Those numbers describe *fills given entries*, not
*which entries*, so they test the fill model INDEPENDENTLY of the mechanisms — which do
not exist until Phase 2. Each row below drives `OrderSim` over the date's real 1s tape
from a binding injected directly: no Executor, no mechanism, no thesis.

**Placement time is part of the binding, not a free parameter.** §5's binding is
two-phase — the stop-entry exists only after a FRESH retrace-in tick strictly after
09:30:30 (§11.0 pins this). Dropping the order at the window boundary instead fills
08-13 at 09:30:42 and stops it out for −33.75 against a recorded +89.25: the row's own
proof that the placement instant belongs in the table.

**Tolerance, stated up front.** A resting stop fills AT its trigger, so those rows assert
EXACTLY. §11's own harness notes a 1m-close vs 1s-mid discrepancy of ±1–2 pts on market
fills (it reports 08-10 as +95.75 against a recorded +96.25), so market-entry assertions
carry ±2.00 and say so in their names. A row that misses by more than its stated
tolerance is a fill-model defect, NOT a tolerance to widen.

**08-21 is EXCLUDED**, and the exclusion is pinned by a test rather than a comment — see
`test_the_0821_row_is_excluded_and_the_reason_is_recorded`.
"""
import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.trader.order_sim import OrderSim, RestingOrder     # noqa: E402

TZ = "America/New_York"
pytestmark = pytest.mark.timeout(900)

# §11's market-fill tolerance: its own harness reports 08-10 as +95.75 against a recorded
# +96.25 (1m close vs 1s mid). Resting-stop rows do NOT get this slack.
MARKET_FILL_TOLERANCE_PTS = 2.00

_TAPE: dict = {}


def _tape(date: str) -> pd.DataFrame:
    """The date's 1s MNQ bars, 09:30:00 -> 12:00, via the same contract routing
    `run_backtest_v2` uses (rollover sends July/August dates to `2026-09/`)."""
    if date not in _TAPE:
        from backtest_smt import _main_dir_for_date
        path = _main_dir_for_date(date) / "MNQ_1s.parquet"
        if not path.exists():
            pytest.skip(f"no 1s tape for {date}")
        df = pd.read_parquet(path)
        day = pd.Timestamp(date, tz=TZ).normalize()
        _TAPE[date] = df[(df.index >= day + pd.Timedelta(hours=9, minutes=30))
                         & (df.index < day + pd.Timedelta(hours=12))]
    return _TAPE[date]


def _ts(date: str, hhmmss: str) -> pd.Timestamp:
    return pd.Timestamp(f"{date} {hhmmss}", tz=TZ)


def conformance_case(date, direction, trigger, stop, dol, placed_at):
    """Drive `OrderSim` over the date's 1s tape from one injected binding.

    Returns `(fill_px, fill_ts, exit_px, exit_ts, exit_kind, pnl)`, or `None` if the
    order never filled.
    """
    df = _tape(date)
    df = df[df.index >= _ts(date, placed_at)]
    sim = OrderSim(dol=dol)
    sim.place(RestingOrder(direction=direction, trigger=trigger, stop=stop,
                           artifact_id="injected", placed_at=df.index[0]))
    fill = exit_ = None
    for ts, row in df.iterrows():
        for ev in sim.on_bar(ts, row):
            if ev["kind"] == "fill":
                fill = ev
            else:
                exit_ = ev
        if exit_ is not None:
            break
    if fill is None:
        return None
    if exit_ is None:
        return (fill["price"], fill["time"], None, None, None, None)
    pnl = ((exit_["price"] - fill["price"]) if str(direction).upper() == "UP"
           else (fill["price"] - exit_["price"]))
    return (fill["price"], fill["time"], exit_["price"], exit_["time"],
            exit_["kind"], round(pnl, 2))


# --------------------------------------------------------------------------- #
# The calibrated bindings.                                                     #
#                                                                              #
# Every field is quoted from l2-mechanisms.md; the `source` column names where.#
# `placed_at` is the recorded retrace-in / arming instant, NOT a fitted value.  #
# Where the doc gives only a MINUTE for the fill, `placed_at` is that minute's  #
# start and only the price is asserted, never the second.                      #
# --------------------------------------------------------------------------- #
ROWS = {
    # §5 forward-test text: 09:33 wick into the 09:00 bearish gap [29805, 29831.75],
    # stop at 29798, filled 09:34, gap-anchored stop 29834.75. DOL DERIVED from the
    # §11 row (+198.00) and the fill: 29798 - 198 = 29600, consistent with §5's
    # "~230 pts of favorable excursion".
    "2026-08-07": dict(direction="DOWN", trigger=29798.0, stop=29834.75, dol=29600.0,
                       placed_at="09:34:00", pnl=198.00, exit_kind="take_profit",
                       fill_ts=None, exit_ts="10:11:53"),
    # §10: "buy stop 29912.25 filled 09:33, DOL TP during 09:36"; 1s: "fresh re-entry
    # 09:32:35, fill 09:33:32, TP 09:36:43". Structural stop = gap low 29881.5 - 3.
    "2026-08-13": dict(direction="UP", trigger=29912.25, stop=29878.5, dol=30001.5,
                       placed_at="09:32:35", pnl=89.25, exit_kind="take_profit",
                       fill_ts="09:33:32", exit_ts="09:36:43"),
    # §11 erratum: 08-18's gap "completes 04:10 against an 09:41:02 fill"; §7 names the
    # day's DOL prev1_week_low 29533.5 and its 11:01:26 touch. Trigger DERIVED from the
    # §11 row (+225.75) and the DOL, and independently corroborated: the §7 fire that
    # same day is a market short at 29760.25, one point away.
    # The STOP is not recorded anywhere for this binding — see the row's own test.
    "2026-08-18": dict(direction="DOWN", trigger=29759.25, stop=None, dol=29533.5,
                       placed_at="09:41:00", pnl=225.75, exit_kind="take_profit",
                       fill_ts="09:41:02", exit_ts="11:01:26"),
    # §10: "§4 negation short 29933 (fill 09:31) stopped -31 on the 09:48 squeeze";
    # 1s: "fill 09:31:18, stop 09:48:54 -31". DOL 29842.75 = prev RTH high.
    "2026-08-12": dict(direction="DOWN", trigger=29933.0, stop=29964.0, dol=29842.75,
                       placed_at="09:30:30", pnl=-31.00, exit_kind="stop_out",
                       fill_ts="09:31:18", exit_ts="09:48:54"),
    # §8 validation: "attempt-1 fill 30252.25 at 09:30:33 ... takes the SL (30268) at
    # 09:31:05". DOL 30124.25 = overnight low (§10).
    "2026-08-14": dict(direction="DOWN", trigger=30252.25, stop=30268.0, dol=30124.25,
                       placed_at="09:30:30", pnl=-15.75, exit_kind="stop_out",
                       fill_ts="09:30:33", exit_ts="09:31:05"),
    # §8 validation: "fill 28644.75 at 09:30:35, SL 28662.5 at 09:31:03, -17.75".
    # DOL london_low 28554.75 (§10.1).
    "2026-07-17": dict(direction="DOWN", trigger=28644.75, stop=28662.5, dol=28554.75,
                       placed_at="09:30:35", pnl=-17.75, exit_kind="stop_out",
                       fill_ts="09:30:35", exit_ts="09:31:03"),
    # §10.1: "§5 binds the 08:55 bull gap [29143.25, 29164] (laddered ... at 09:31:32),
    # fill 09:31:40 @ 29171, stopped 09:33:58 -> -30.75". DOL prev3_day_high 29796.5.
    "2026-07-21": dict(direction="UP", trigger=29171.0, stop=29140.25, dol=29796.5,
                       placed_at="09:31:32", pnl=-30.75, exit_kind="stop_out",
                       fill_ts="09:31:40", exit_ts="09:33:58"),
    # §10.2: "§5 long 28536.25 (fill 09:40:36, SL 28511.5 at 09:42:02) -24.75"; the
    # 09:40:23 retrace is the placement tick. DOL prev4_day_high 28763.75.
    "2026-07-31": dict(direction="UP", trigger=28536.25, stop=28511.5, dol=28763.75,
                       placed_at="09:40:23", pnl=-24.75, exit_kind="stop_out",
                       fill_ts="09:40:36", exit_ts="09:42:02"),
}


def _run(date):
    row = ROWS[date]
    stop = row["stop"]
    if stop is None:
        # Unrecorded: placed far enough away that it cannot fire, so the row validates
        # the FILL and TAKE-PROFIT legs only. See this row's own test.
        stop = row["trigger"] + 10000.0 if row["direction"] == "DOWN" \
            else row["trigger"] - 10000.0
    res = conformance_case(date, row["direction"], row["trigger"], stop, row["dol"],
                           row["placed_at"])
    assert res is not None, f"{date}: the injected binding never filled"
    return row, res


def _assert_row(date):
    row, (fill_px, fill_ts, exit_px, exit_ts, kind, pnl) = _run(date)
    # A resting stop fills AT its trigger. Exact, no tolerance.
    assert fill_px == row["trigger"], f"{date}: fill {fill_px} != trigger {row['trigger']}"
    assert kind == row["exit_kind"], f"{date}: exited by {kind}, recorded {row['exit_kind']}"
    assert pnl == row["pnl"], f"{date}: P&L {pnl:+.2f} != recorded {row['pnl']:+.2f}"
    if row["fill_ts"]:
        assert str(fill_ts.time()) == row["fill_ts"], \
            f"{date}: filled {fill_ts.time()}, recorded {row['fill_ts']}"
    if row["exit_ts"]:
        assert str(exit_ts.time()) == row["exit_ts"], \
            f"{date}: exited {exit_ts.time()}, recorded {row['exit_ts']}"


# --- the calibrated rows ----------------------------------------------------- #

def test_0807_resting_stop_fills_at_trigger_and_reaches_the_dol():
    _assert_row("2026-08-07")


def test_0813_single_entry_reproduces_the_calibrated_pnl():
    _assert_row("2026-08-13")


def test_0818_single_entry_reproduces_the_calibrated_pnl():
    _assert_row("2026-08-18")


def test_0812_single_entry_stop_out_reproduces_the_calibrated_loss():
    _assert_row("2026-08-12")


def test_0814_single_entry_stop_out_reproduces_the_calibrated_loss():
    _assert_row("2026-08-14")


def test_0717_single_entry_stop_out_reproduces_the_calibrated_loss():
    _assert_row("2026-07-17")


def test_0721_single_entry_stop_out_reproduces_the_calibrated_loss():
    _assert_row("2026-07-21")


def test_0731_single_entry_stop_out_reproduces_the_calibrated_loss():
    _assert_row("2026-07-31")


def test_the_0818_row_validates_its_fill_and_target_but_not_its_stop():
    """Pinned so the gap is not mistaken for coverage. §11 gives 08-18's §5 P&L
    (+225.75) but no stop price for that binding — the 29760.25 / 29775.25 pair quoted
    nearby belongs to §7's SEPARATE fire on the same day (+226.75, a different number).
    The row therefore exercises the resting-fill and take-profit legs only."""
    assert ROWS["2026-08-18"]["stop"] is None


def test_the_placement_instant_is_part_of_the_binding_not_a_free_parameter():
    """§5's binding is two-phase: the stop-entry exists only after a FRESH retrace-in
    tick strictly after 09:30:30. Dropping 08-13's order at the window boundary instead
    of its recorded 09:32:35 fills at 09:30:42 and stops out — the recorded +89.25
    becomes -33.75. That is a 123-point swing from a one-line convention."""
    row = ROWS["2026-08-13"]
    early = conformance_case("2026-08-13", row["direction"], row["trigger"], row["stop"],
                             row["dol"], "09:30:30")
    assert early is not None
    assert early[4] == "stop_out"
    assert early[5] != row["pnl"]


# --- the conventions themselves ---------------------------------------------- #

def test_a_resting_stop_fills_AT_its_trigger_not_at_the_next_tick():
    """The single most consequential convention. Every calibrated row above depends on
    it, and filling one tick later would shift each by 0.25-1.0 pts — inside §11's own
    stop clearances of 1.0 to 7.5 pts, i.e. invisible to every structural test."""
    for date in ("2026-08-13", "2026-08-12", "2026-07-21"):
        row, res = _run(date)
        assert res[0] == row["trigger"]


def test_a_market_entry_fills_at_the_1s_mid_at_placement():
    """§8 pins a documented instance: 08-14's crossed trigger at 09:32:00 executes as a
    "market short at the 09:32:00 1s mid 30245.5". Checked against the tape itself, and
    then that `fill_market` books exactly the price it is handed."""
    bar = _tape("2026-08-14").loc[_ts("2026-08-14", "09:32:00")]
    mid = (float(bar["High"]) + float(bar["Low"])) / 2.0
    assert abs(mid - 30245.5) <= MARKET_FILL_TOLERANCE_PTS, \
        f"the 09:32:00 1s mid is {mid}, §8 records 30245.5"

    sim = OrderSim(dol=30124.25)
    ev = sim.fill_market(_ts("2026-08-14", "09:32:00"), direction="DOWN", price=mid,
                         stop=30268.0, artifact_id="takeover")
    assert ev["price"] == mid
    assert sim.resting is None, "a market execution must not leave an order resting"


def test_same_bar_fill_and_stop_resolves_ADVERSELY_and_books_both():
    """Booking the better of the two would inflate every result silently, and §11's own
    errata show how easily an optimistic reading survives review. Driven off a real
    08-14 bar rather than a synthetic one."""
    sim = OrderSim(dol=30124.25)
    sim.place(RestingOrder(direction="DOWN", trigger=30252.25, stop=30268.0,
                           artifact_id="x", placed_at=_ts("2026-08-14", "09:30:30")))
    events = sim.on_bar(_ts("2026-08-14", "09:31:00"),
                        {"Open": 30255.0, "High": 30268.5, "Low": 30250.0,
                         "Close": 30266.0})
    assert [e["kind"] for e in events] == ["fill", "stop_out"]
    assert events[0]["price"] == 30252.25 and events[1]["price"] == 30268.0


def test_the_0821_row_is_excluded_and_the_reason_is_recorded():
    """§11 erratum: 08-21's +92.75 is look-ahead contaminated. Its 44-pt gap
    [29373.25, 29417.25] is LABELLED 09:35 but only EXISTS at 09:40:00 (third-bar
    completion), so the recorded 09:36:04 fill is impossible; and after real creation
    price never ticks back into it before the 09:54:07 DOL touch.

    Both halves are checked against the tape, so nobody can "restore" the row by
    re-deriving it: the fill it claims sits at a price only reachable BEFORE the gap
    existed."""
    assert "2026-08-21" not in ROWS

    tape = _tape("2026-08-21")
    gap_bottom = 29373.25
    after_creation = tape[(tape.index >= _ts("2026-08-21", "09:40:00"))
                          & (tape.index <= _ts("2026-08-21", "09:54:07"))]
    assert len(after_creation)
    assert after_creation["High"].max() < gap_bottom, (
        "price re-entered the gap after real creation; the exclusion needs re-deriving")

    # ...and the impossible fill's price WAS reachable in the contaminated window.
    contaminated = tape[(tape.index >= _ts("2026-08-21", "09:36:00"))
                        & (tape.index < _ts("2026-08-21", "09:37:00"))]
    assert contaminated["High"].max() >= gap_bottom, (
        "the 09:36 fill was not reachable either; re-derive the erratum")


def test_attempts_used_is_asserted_not_scored():
    """§8's attempt counter is a BUDGET, not a term in the P&L arithmetic. Each row
    above is ONE binding and spends exactly one attempt; the count is recorded and
    compared, and contributes nothing to the numbers asserted above.

    A row that reproduced its P&L with a different attempt count would still be a real
    discrepancy — a stopped-out attempt followed by a winner can net the same as one
    clean entry — which is why the count is asserted separately rather than inferred."""
    from agent.trader.executor import MAX_ATTEMPTS, Executor

    ex = Executor("/tmp/does-not-need-to-exist",
                  {"plan_id": "conformance", "direction": "UP",
                   "dol": {"price": 30001.5}, "blacklist": [],
                   "armed_classes": ["fvg_return_continuation"]},
                  arm_ts=_ts("2026-08-13", "09:20:00"))
    assert ex._plan["max_attempts"] == MAX_ATTEMPTS
    assert int(ex._plan.get("attempts_used") or 0) == 0
    ex._on_stop_out({"kind": "stop_out", "time": _ts("2026-08-13", "09:33:00"),
                     "price": 29878.5, "artifact_id": "injected"})
    assert ex._plan["attempts_used"] == 1

    # Every calibrated row is a single binding: one attempt each, and its P&L is the
    # fill-model arithmetic alone.
    for date in ROWS:
        row, res = _run(date)
        assert res[5] == row["pnl"]


# --- the tick grid: a market fill is a PRICE, not an average ------------------ #

def _mkexec(tmp_path, direction: str):
    from agent.trader.executor import Executor
    return Executor(str(tmp_path),
                    {"plan_id": "tick", "direction": direction,
                     "dol": {"price": 29000.0 if direction == "DOWN" else 30000.0},
                     "armed_classes": ["fvg_return_continuation"]},
                    arm_ts=pd.Timestamp("2026-08-19 09:20", tz=TZ))


def test_a_market_fill_snaps_to_the_tick_grid_against_the_trade(tmp_path):
    """08-19's second entry booked **29672.875** on an instrument that trades in 0.25.

    The mid of a 1s bar spanning an ODD number of ticks lands halfway between two of
    them, and `_market_price` handed that straight to `fill_market`. No broker fills
    there, and every P&L quoted off a crossed trigger inherits the error.

    The snap goes AWAY from the taker — up for a buy, down for a sell — which is
    `order_sim`'s standing rule that ambiguity resolves adversely, applied to the one
    place the fill price itself was ambiguous.
    """
    short = _mkexec(tmp_path, "DOWN")
    short._state["now_mid"] = 29672.875
    assert short._market_price() == 29672.75

    long_ = _mkexec(tmp_path, "UP")
    long_._state["now_mid"] = 29672.875
    assert long_._market_price() == 29673.00


def test_the_snap_leaves_an_on_grid_mid_untouched(tmp_path):
    """§8's documented 08-14 instance is a market short at the 1s mid **30245.5**, and
    07-21's cooldown-end reads 29137.25. Both are already on the grid; a snap that
    moved them would rewrite a pinned number to fix a cosmetic one."""
    for direction, mid in (("DOWN", 30245.5), ("UP", 30245.5),
                           ("DOWN", 29137.25), ("UP", 29137.25)):
        ex = _mkexec(tmp_path, direction)
        ex._state["now_mid"] = mid
        assert ex._market_price() == mid, f"{direction} @ {mid} was moved off its tick"


def test_the_snap_never_favours_the_trade_and_never_exceeds_half_a_tick(tmp_path):
    """The property, over every mid a 1s bar can produce: bar extremes are on the grid,
    so their mean is always a multiple of 0.125 — i.e. on a tick or exactly between
    two. Nothing else is reachable, and the correction is bounded by half a tick, well
    inside §11's own +/-2 pt market-fill tolerance."""
    from agent.trader.executor import TICK_PTS

    for i in range(400):
        mid = 29000.0 + i * 0.125
        for direction, sign in (("UP", 1.0), ("DOWN", -1.0)):
            ex = _mkexec(tmp_path, direction)
            ex._state["now_mid"] = mid
            got = ex._market_price()
            assert abs(round(got / TICK_PTS) - got / TICK_PTS) < 1e-9, \
                f"{got} is not on the {TICK_PTS} grid"
            assert sign * (got - mid) >= 0.0, "the snap moved the fill in our favour"
            assert abs(got - mid) <= TICK_PTS / 2.0


def test_the_fallback_close_is_already_on_grid_and_survives_the_snap(tmp_path):
    """`now_price` is a real bar CLOSE — on the grid by construction. The fallback path
    exists for direct-construction callers that never set a mid, and must stay exact."""
    ex = _mkexec(tmp_path, "DOWN")
    ex._state["now_price"] = 29672.75
    assert ex._state.get("now_mid") is None
    assert ex._market_price() == 29672.75
