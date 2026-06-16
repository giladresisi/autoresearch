# tests/test_smt_conviction.py
# GIL-32 — unit tests for the standing-SMT-conviction module (smt_conviction.py).
#
# Pure functions: conviction_score (tier weighting, one-sidedness, collapse, normalization,
# empty) and update_standing (residual decay, birth grace, adverse-run sustain, gone-drop).

from __future__ import annotations

import datetime

import smt_conviction as sc


# ── helpers ──────────────────────────────────────────────────────────────────

def _iso(h, m, day=27):
    return datetime.datetime(2026, 4, day, h, m, 0).isoformat()


def _div(ref_name, direction, type_="wick", time=None, mnq_price=100.0, kind="smt"):
    side = "bearish" if direction == "short" else "bullish"
    return {
        "kind": kind, "type": type_, "side": side, "direction": direction,
        "ref_name": ref_name, "time": time or _iso(9, 30), "mnq_price": mnq_price,
    }


def _standing(ref_name, direction, tier, type_="wick", fire_iso=None,
              fire_close=100.0, adverse_streak=0, fulfilled_iso=None):
    side = "bearish" if direction == "short" else "bullish"
    return {
        "ref_name": ref_name, "direction": direction, "side": side, "tier": tier,
        "type": type_, "fire_iso": fire_iso or _iso(9, 30), "fire_close": fire_close,
        "adverse_streak": adverse_streak, "fulfilled_iso": fulfilled_iso,
    }


# ══ conviction_score ══════════════════════════════════════════════════════════

def test_conviction_empty_set_is_zero():
    assert sc.conviction_score([], _iso(10, 0)) == (
        0.0, {"n": 0, "n_bear": 0, "n_bull": 0, "top_tier": None, "refs": []}
    )
    assert sc.conviction_score(None, _iso(10, 0))[0] == 0.0


def test_conviction_one_sided_bearish_near_minus_one():
    """3 bearish, 0 bullish → score near −1 (fully one-sided short)."""
    standing = [
        _standing("week_high", "short", "week"),
        _standing("day_high", "short", "day"),
        _standing("ny_morning_high", "short", "session"),
    ]
    score, inputs = sc.conviction_score(standing, _iso(10, 0))
    assert score == -1.0, f"fully bearish should be −1.0, got {score}"
    assert inputs["n"] == 3 and inputs["n_bear"] == 3 and inputs["n_bull"] == 0


def test_conviction_one_sided_bullish_near_plus_one():
    standing = [
        _standing("week_low", "long", "week"),
        _standing("day_low", "long", "day"),
    ]
    score, inputs = sc.conviction_score(standing, _iso(10, 0))
    assert score == 1.0
    assert inputs["n_bull"] == 2 and inputs["n_bear"] == 0


def test_conviction_mixed_equal_weight_cancels_to_zero():
    """A week-tier short and a week-tier long cancel exactly → 0.0."""
    standing = [
        _standing("week_high", "short", "week"),
        _standing("week_low", "long", "week"),
    ]
    score, _ = sc.conviction_score(standing, _iso(10, 0))
    assert score == 0.0, f"equal-and-opposite week-tier SMTs should cancel, got {score}"


def test_conviction_tier_weighting_week_beats_session():
    """A week short (weight 3) vs a session long (weight 1) → net bearish, not zero."""
    standing = [
        _standing("week_high", "short", "week"),       # weight 3, sign −
        _standing("ny_morning_low", "long", "session"),  # weight 1, sign +
    ]
    score, inputs = sc.conviction_score(standing, _iso(10, 0))
    # (−3 + 1) / (3 + 1) = −0.5
    assert abs(score - (-0.5)) < 1e-9, f"expected −0.5, got {score}"
    assert inputs["top_tier"] == "week"


def test_conviction_collapses_by_ref_and_direction():
    """Wick+body variants of the SAME logical (ref,dir) SMT count ONCE (defensive collapse)."""
    standing = [
        _standing("week_high", "short", "week", type_="wick"),
        _standing("week_high", "short", "week", type_="body"),
    ]
    score, inputs = sc.conviction_score(standing, _iso(10, 0))
    assert score == -1.0
    assert inputs["n"] == 1, f"wick+body of same logical SMT must count once, got n={inputs['n']}"


def test_conviction_normalized_within_range():
    """Many same-side SMTs never exceed [-1, 1]."""
    standing = [_standing(f"lvl{i}_high", "short", "week") for i in range(20)]
    # distinct ref_names so they don't collapse
    score, _ = sc.conviction_score(standing, _iso(10, 0))
    assert -1.0 <= score <= 1.0 and score == -1.0


def test_conviction_residual_decay_halves_weight_at_half_window():
    """A fulfilled SMT's residual_factor decays linearly: at RESIDUAL_MIN/2 it is ~0.5."""
    half = sc.CONVICTION_RESIDUAL_MIN // 2
    standing = [
        # bearish week short, fulfilled half a window ago → residual_factor ≈ 0.5, weight 3*0.5=1.5
        _standing("week_high", "short", "week", fulfilled_iso=_iso(9, 0)),
        # bullish day long, unfulfilled → weight 2
        _standing("day_low", "long", "day"),
    ]
    now = (datetime.datetime.fromisoformat(_iso(9, 0))
           + datetime.timedelta(minutes=half)).isoformat()
    score, _ = sc.conviction_score(standing, now)
    # signed = (-1.5) + (+2.0) = 0.5 ; abs = 3.5 → 0.1428...
    assert abs(score - (0.5 / 3.5)) < 1e-6, f"residual-decayed score wrong: {score}"


def test_conviction_fulfilled_past_window_drops_to_zero_weight():
    """A fulfilled SMT older than RESIDUAL_MIN contributes 0 weight (score ignores it)."""
    standing = [
        _standing("week_high", "short", "week",
                  fulfilled_iso=_iso(8, 0)),  # long past the window at 'now'
    ]
    now = (datetime.datetime.fromisoformat(_iso(8, 0))
           + datetime.timedelta(minutes=sc.CONVICTION_RESIDUAL_MIN + 30)).isoformat()
    score, inputs = sc.conviction_score(standing, now)
    assert score == 0.0 and inputs["n"] == 0


# ══ update_standing ═══════════════════════════════════════════════════════════

def test_update_adds_new_div_and_collapses_wick_body():
    """A new wick + body on the same (ref,dir) collapse to a single standing record."""
    divs = [
        _div("week_high", "short", type_="body", time=_iso(9, 40)),
        _div("week_high", "short", type_="wick", time=_iso(9, 41)),
    ]
    out = sc.update_standing([], divs, {}, mnq_close=100.0, now_iso=_iso(9, 41))
    keys = [(r["ref_name"], r["direction"]) for r in out]
    assert keys == [("week_high", "short")], f"should collapse to one, got {keys}"
    # wick supersedes body.
    assert out[0]["type"] == "wick"


def test_update_collapsed_union_status_any_fulfilled():
    """A collapsed wick+body record aggregates status over BOTH folded detect keys:
    if EITHER variant is fulfilled in the status map, the standing record is fulfilled."""
    divs = [
        _div("week_high", "short", type_="body", time=_iso(9, 40)),
        _div("week_high", "short", type_="wick", time=_iso(9, 41)),
    ]
    out = sc.update_standing([], divs, {}, mnq_close=100.0, now_iso=_iso(9, 41))
    assert len(out) == 1
    assert set(out[0]["keys"]) == {"week_high|short|body", "week_high|short|wick"}
    # Only the BODY variant fulfilled in detect_state; union → fulfilled → stamps residual.
    status = {"week_high|short|body": "fulfilled", "week_high|short|wick": "unfulfilled"}
    out2 = sc.update_standing(out, [], status, mnq_close=100.0, now_iso=_iso(9, 42))
    assert len(out2) == 1 and out2[0]["fulfilled_iso"] == _iso(9, 42), (
        f"any-fulfilled union must stamp the residual, got {out2}"
    )
    # Both variants gone → drop.
    gone = {"week_high|short|body": "gone", "week_high|short|wick": "gone"}
    out3 = sc.update_standing(out, [], gone, mnq_close=100.0, now_iso=_iso(9, 43))
    assert out3 == [], "all-gone union must drop the record"


def test_update_gone_key_dropped():
    """A standing record whose detect key is 'gone' in the status map is dropped."""
    prev = [_standing("week_high", "short", "week", type_="wick")]
    status = {"week_high|short|wick": "gone"}
    out = sc.update_standing(prev, [], status, mnq_close=100.0, now_iso=_iso(10, 0))
    assert out == [], f"gone key must drop the standing record, got {out}"


def test_update_fulfilled_stamps_residual_then_drops_after_window():
    """A newly-fulfilled record is stamped (kept), then dropped once age > RESIDUAL_MIN."""
    prev = [_standing("week_high", "short", "week", type_="wick", fire_iso=_iso(9, 0))]
    status = {"week_high|short|wick": "fulfilled"}
    # First bar: fulfilled now → stamp, still kept.
    out1 = sc.update_standing(prev, [], status, mnq_close=10.0, now_iso=_iso(9, 30))
    assert len(out1) == 1 and out1[0]["fulfilled_iso"] == _iso(9, 30)
    # Later bar within the window → still kept.
    out2 = sc.update_standing(out1, [], status, mnq_close=10.0, now_iso=_iso(11, 0))
    assert len(out2) == 1, "fulfilled record within residual window must survive"
    # Far past the window → dropped.
    far = (datetime.datetime.fromisoformat(_iso(9, 30))
           + datetime.timedelta(minutes=sc.CONVICTION_RESIDUAL_MIN + 5)).isoformat()
    out3 = sc.update_standing(out2, [], status, mnq_close=10.0, now_iso=far)
    assert out3 == [], "fulfilled record past residual window must drop"


def test_update_grace_blocks_adverse_within_grace_min():
    """An adverse close within CONVICTION_GRACE_MIN of fire never increments the streak."""
    fire = _iso(9, 30)
    prev = [_standing("week_high", "short", "week", type_="wick",
                      fire_iso=fire, fire_close=100.0)]
    status = {"week_high|short|wick": "unfulfilled"}
    inv = sc.smt_detect._invalidate_pts("week", "mnq")
    # mnq_close well above fire_close + inv (adverse for a short) but only +2 min after fire.
    now = (datetime.datetime.fromisoformat(fire) + datetime.timedelta(minutes=2)).isoformat()
    out = sc.update_standing(prev, [], status, mnq_close=100.0 + inv + 50, now_iso=now)
    assert len(out) == 1 and out[0]["adverse_streak"] == 0, (
        f"within grace, adverse close must not arm the streak: {out}"
    )


def test_update_sustain_requires_consecutive_adverse_closes():
    """1 adverse close then a non-adverse close does NOT drop; need CONVICTION_SUSTAIN in a row."""
    assert sc.CONVICTION_SUSTAIN == 2
    fire = _iso(9, 0)
    prev = [_standing("week_high", "short", "week", type_="wick",
                      fire_iso=fire, fire_close=100.0)]
    status = {"week_high|short|wick": "unfulfilled"}
    inv = sc.smt_detect._invalidate_pts("week", "mnq")
    adverse_close = 100.0 + inv + 10   # adverse for a short
    benign_close = 100.0 - 5            # favorable
    # Bar 1 (past grace): adverse → streak 1, kept.
    out1 = sc.update_standing(prev, [], status, mnq_close=adverse_close, now_iso=_iso(9, 30))
    assert len(out1) == 1 and out1[0]["adverse_streak"] == 1
    # Bar 2: non-adverse → streak resets to 0, kept.
    out2 = sc.update_standing(out1, [], status, mnq_close=benign_close, now_iso=_iso(9, 31))
    assert len(out2) == 1 and out2[0]["adverse_streak"] == 0, "non-adverse must reset the streak"
    # Bars 3+4: two consecutive adverse → streak hits SUSTAIN → dropped.
    out3 = sc.update_standing(out2, [], status, mnq_close=adverse_close, now_iso=_iso(9, 32))
    assert len(out3) == 1 and out3[0]["adverse_streak"] == 1
    out4 = sc.update_standing(out3, [], status, mnq_close=adverse_close, now_iso=_iso(9, 33))
    assert out4 == [], "two consecutive adverse closes must drop (sustain reached)"


def test_update_never_raises_on_garbage():
    """Total: degenerate inputs never raise; preserve prior on error."""
    assert sc.update_standing(None, None, None, mnq_close=0.0, now_iso="") == []
    out = sc.update_standing([{"bad": "rec"}], [{"also": "bad"}], {}, 0.0, _iso(10, 0))
    assert isinstance(out, list)
