# Tests for smt_reversal_lock (GIL-32 Phase-1b: same-liquidity reversal lock).
import smt_reversal_lock as L


def _bear_fire(ref="day_high", price=29137.0, t="2026-06-10T10:12:00-04:00", typ="wick"):
    return {"ref_name": ref, "side": "bearish", "direction": "short",
            "type": typ, "time": t, "mnq_price": price}


def _bull_fire(ref="day_low", price=28600.0, t="2026-06-10T10:12:00-04:00", typ="wick"):
    return {"ref_name": ref, "side": "bullish", "direction": "long",
            "type": typ, "time": t, "mnq_price": price}


# ----------------------------- ingest_fires --------------------------------
def test_ingest_opens_bearish_high_lock():
    locks = L.ingest_fires([], [_bear_fire("day_high")], {"day_high": 29137.0},
                           now_iso="2026-06-10T10:12:00-04:00")
    assert len(locks) == 1
    lk = locks[0]
    assert lk["level_name"] == "day_high" and lk["side"] == "bearish"
    assert lk["locked_dir"] == "down" and lk["level_price"] == 29137.0
    assert lk["protecting"] is False
    assert lk["keys"] == ["day_high|short|wick"]


def test_ingest_opens_bullish_low_lock():
    locks = L.ingest_fires([], [_bull_fire("day_low")], {"day_low": 28600.0},
                           now_iso="2026-06-10T10:12:00-04:00")
    assert len(locks) == 1 and locks[0]["side"] == "bullish" and locks[0]["locked_dir"] == "up"


def test_ingest_ignores_bullish_on_high_and_fvg_fills():
    # bullish-on-high and fvg fills are not reversal-of-a-level SMTs → ignored.
    recs = [{"ref_name": "day_high", "side": "bullish", "direction": "long", "type": "wick",
             "mnq_price": 29137.0, "time": "2026-06-10T10:12:00-04:00"},
            {"ref_name": "fvg_20260609_1100_bear", "side": "bearish", "type": "fill_a",
             "mnq_price": 29000.0, "time": "2026-06-10T10:12:00-04:00"}]
    assert L.ingest_fires([], recs, {}, now_iso="2026-06-10T10:12:00-04:00") == []


def test_ingest_collapses_and_preserves_protecting_and_fire_clock():
    locks = L.ingest_fires([], [_bear_fire("day_high", t="2026-06-10T10:03:00-04:00")],
                           {"day_high": 29137.0}, now_iso="2026-06-10T10:03:00-04:00")
    locks[0]["protecting"] = True
    locks[0]["accept_streak"] = 1
    # a later same-level fire refreshes level_price but preserves protecting / streak / fire_iso.
    locks2 = L.ingest_fires(locks, [_bear_fire("day_high", price=29140.0,
                            t="2026-06-10T10:12:00-04:00", typ="body")],
                            {"day_high": 29140.0}, now_iso="2026-06-10T10:12:00-04:00")
    assert len(locks2) == 1
    assert locks2[0]["level_price"] == 29140.0
    assert locks2[0]["protecting"] is True
    assert locks2[0]["accept_streak"] == 1
    assert locks2[0]["fire_iso"] == "2026-06-10T10:03:00-04:00"
    assert "day_high|short|body" in locks2[0]["keys"]


# ----------------------------- mark_protecting -----------------------------
def _open_bear_lock():
    return L.ingest_fires([], [_bear_fire("day_high")], {"day_high": 29137.0},
                          now_iso="2026-06-10T10:12:00-04:00")


def test_mark_protecting_promotes_matching_lock_on_down_hyp():
    locks = _open_bear_lock()
    assert locks[0]["protecting"] is False
    L.mark_protecting(locks, "down", "day_high")
    assert locks[0]["protecting"] is True
    assert locks[0]["armed_iso"] == locks[0]["fire_iso"]


def test_mark_protecting_noop_on_wrong_direction_or_level():
    locks = _open_bear_lock()
    L.mark_protecting(locks, "up", "day_high")      # up doesn't match a bearish lock
    assert locks[0]["protecting"] is False
    L.mark_protecting(locks, "down", "week_high")   # different level
    assert locks[0]["protecting"] is False


# --------------------------- advance ---------------------------------------
def _lock(level="day_high", side="bearish", price=29137.0, fire="2026-06-10T10:12:00-04:00",
          streak=0, protecting=True, keys=None):
    return {"level_name": level, "side": side, "locked_dir": "down" if side == "bearish" else "up",
            "level_price": price, "fire_iso": fire, "armed_iso": fire, "accept_streak": streak,
            "protecting": protecting,
            "keys": keys if keys is not None else [f"{level}|{'short' if side=='bearish' else 'long'}|wick"]}


def test_advance_survives_sub_buffer_pop():
    # 06-10: 29250 peak = +0.39% above 29137 < 0.5% buffer → NOT accepting → lock survives.
    out = L.advance([_lock()], {"day_high": 29137.0}, {}, mnq_close=29250.0,
                    now_iso="2026-06-10T10:35:00-04:00")
    assert len(out) == 1 and out[0]["accept_streak"] == 0


def test_advance_releases_on_sustained_acceptance():
    lp = {"day_high": 29137.0}
    accept_px = 29137.0 * (1 + L.LOCK_ACCEPT_BUFFER_PCT) + 5
    locks = L.advance([_lock()], lp, {}, mnq_close=accept_px, now_iso="2026-06-10T11:00:00-04:00")
    assert len(locks) == 1 and locks[0]["accept_streak"] == 1
    locks = L.advance(locks, lp, {}, mnq_close=accept_px, now_iso="2026-06-10T11:01:00-04:00")
    assert locks == []


def test_advance_accept_streak_resets_on_non_accepting_close():
    lp = {"day_high": 29137.0}
    accept_px = 29137.0 * (1 + L.LOCK_ACCEPT_BUFFER_PCT) + 5
    locks = L.advance([_lock()], lp, {}, mnq_close=accept_px, now_iso="2026-06-10T11:00:00-04:00")
    assert locks[0]["accept_streak"] == 1
    locks = L.advance(locks, lp, {}, mnq_close=29150.0, now_iso="2026-06-10T11:01:00-04:00")
    assert len(locks) == 1 and locks[0]["accept_streak"] == 0


def test_advance_releases_on_fulfilled_status():
    out = L.advance([_lock()], {"day_high": 29137.0}, {"day_high|short|wick": "fulfilled"},
                    mnq_close=29100.0, now_iso="2026-06-10T11:00:00-04:00")
    assert out == []


def test_advance_releases_on_gone_status():
    out = L.advance([_lock()], {"day_high": 29137.0}, {"day_high|short|wick": "gone"},
                    mnq_close=29100.0, now_iso="2026-06-10T11:00:00-04:00")
    assert out == []


def test_advance_releases_on_age_out():
    out = L.advance([_lock(fire="2026-06-10T05:00:00-04:00")], {"day_high": 29137.0}, {},
                    mnq_close=29100.0, now_iso="2026-06-10T10:00:00-04:00")  # 5h > 240m
    assert out == []


def test_advance_bullish_acceptance_is_below_buffer():
    lp = {"day_low": 28600.0}
    accept_px = 28600.0 * (1 - L.LOCK_ACCEPT_BUFFER_PCT) - 5
    locks = [_lock(level="day_low", side="bullish", price=28600.0)]
    locks = L.advance(locks, lp, {}, mnq_close=accept_px, now_iso="2026-06-10T10:40:00-04:00")
    locks = L.advance(locks, lp, {}, mnq_close=accept_px, now_iso="2026-06-10T10:41:00-04:00")
    assert locks == []


# ---------------------------- vetoes ---------------------------------------
def test_vetoes_protecting_bearish_lock_forces_up_to_down():
    assert L.vetoes([_lock(protecting=True)], "up", "day_high") == "down"


def test_vetoes_protecting_bullish_lock_forces_down_to_up():
    assert L.vetoes([_lock(level="day_low", side="bullish", protecting=True)], "down", "day_low") == "up"


def test_vetoes_noop_when_lock_not_protecting():
    assert L.vetoes([_lock(protecting=False)], "up", "day_high") is None


def test_vetoes_noop_on_different_level():
    assert L.vetoes([_lock(protecting=True)], "up", "week_high") is None


def test_vetoes_noop_when_dir_aligns_with_lock_side():
    assert L.vetoes([_lock(protecting=True)], "down", "day_high") is None


def test_vetoes_empty_is_none():
    assert L.vetoes([], "up", "day_high") is None
    assert L.vetoes(None, "up", "day_high") is None


# ---------------------------- totality -------------------------------------
def test_all_entrypoints_total_on_garbage():
    assert L.ingest_fires(None, None, None, "x") == []
    assert L.advance(None, None, None, mnq_close="nan", now_iso="x") == []
    assert L.mark_protecting(None, None, None) == []
    assert L.vetoes([{"bad": 1}], "up", "day_high") is None
