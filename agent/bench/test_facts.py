"""Phase 2 — offline facts builder tests (plan 10).

Parity against the prepare_cuts path (byte-identical facts content hash on a burned cut)
and the no-lookahead guard. These read the machine-local main parquets; skipped when the
main dir is absent (e.g. CI without data).
"""

import hashlib
import os
import sys

import pandas as pd
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.join(os.path.dirname(_HERE), "contracts"),
           os.path.join(os.path.dirname(os.path.dirname(_HERE)), "calibration")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from facts import DEFAULT_MAIN, LOOKBACK, ParquetFactsSource  # noqa: E402
from derive_facts import (  # noqa: E402
    NEAR_MATURITY_WINDOW_MIN, compute_facts, render_facts_text,
)

TZ = "America/New_York"


def _bundle_at(source, boundary):
    """Build the FactsBundle directly (same slices ParquetFactsSource.build_facts uses),
    so a test can assert on the structured bundle.smt_candidates / htf_close_status the
    FactsResult does not surface."""
    prim, ath = {}, {}
    for tk in source.tickers:
        raw = source._raw_1s[tk]
        before = raw[raw.index < boundary]
        ath[tk] = float(before["High"].max())
        norm = source._norm_1s[tk]
        prim[tk] = norm[(norm.index >= boundary - LOOKBACK) & (norm.index < boundary)]
    return compute_facts(prim["MNQ"], prim["MES"], ath_mnq=ath.get("MNQ"),
                         ath_mes=ath.get("MES"), hist_mnq=source._norm_1s.get("MNQ"),
                         hist_mes=source._norm_1s.get("MES"), now=None)
_REPO = os.path.dirname(os.path.dirname(_HERE))
_BURNED = os.path.join(_REPO, "calibration", "cuts", "2026-06-25_0850", "facts.txt")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(DEFAULT_MAIN) or not os.path.exists(_BURNED),
    reason="main parquets / burned cut not available")


@pytest.fixture(scope="module")
def source():
    return ParquetFactsSource()


def test_facts_parity_with_prepare_cuts(source):
    # The 2026-06-25 08:50 burned cut: bench facts must reproduce prepare_cuts' facts.txt
    # byte-for-byte (same content hash) — the drift guard between bench and calibration.
    boundary = pd.Timestamp("2026-06-25 08:50:00", tz=TZ)
    res = source.build_facts(boundary)
    assert not res.degraded
    ref = open(_BURNED, encoding="utf-8").read()
    assert res.content_hash == hashlib.sha256(ref.encode("utf-8")).hexdigest()


def test_lookahead_guard_max_ts_before_boundary(source):
    boundary = pd.Timestamp("2026-06-25 11:00:00", tz=TZ)
    res = source.build_facts(boundary)
    assert not res.degraded
    assert res.max_ts is not None
    assert res.max_ts < boundary                      # strictly before — no lookahead
    # every referenced level carries a price/side + birth sweep state + a threshold.
    assert res.levels
    for name, lv in res.levels.items():
        assert lv["side"] in ("above", "below")
        assert isinstance(lv["price"], (int, float))


def test_degraded_before_data_start(source):
    # 1s data starts 2026-05-01; a boundary before it → degraded (failsafe), never raises.
    res = source.build_facts(pd.Timestamp("2026-04-20 12:00:00", tz=TZ))
    assert res.degraded
    assert res.error


def test_prev3_day_low_smt_2026_07_14(source):
    # plan 15 Task 1 concrete acceptance case: at the 2026-07-14 01:00 ET boundary MNQ swept
    # its prev3_day_low (29395.0) while MES never reached its own prev3_day_low (7516.25) —
    # a genuine, uncontested cross-asset SMT that the pre-plan-15 2-day/1-week depth could not
    # see. MNQ's own HTF close (29425.0) REJECTED the swept low on both 1h and 4h.
    boundary = pd.Timestamp("2026-07-14 01:00:00", tz=TZ)
    bundle = _bundle_at(source, boundary)

    for tkr in ("MNQ", "MES"):
        assert "prev3_day_low" in bundle.levels[tkr], f"prev3_day_low missing on {tkr}"
    assert bundle.levels["MNQ"]["prev3_day_low"][0] == 29395.0
    assert bundle.levels["MES"]["prev3_day_low"][0] == 7516.25

    smt = [c for c in bundle.smt_candidates if c["level"] == "prev3_day_low"]
    assert smt, "prev3_day_low not surfaced as an SMT candidate"
    for c in smt:
        assert c["swept_ticker"] == "MNQ"
        assert c["unswept_ticker"] == "MES"
        assert c["tier"] == "day"
        assert c["meaningful"] is True

    status = bundle.htf_close_status["MNQ"]["prev3_day_low"]
    for tf in ("1h", "4h"):
        assert status[tf] is not None, f"prev3_day_low immature on {tf}"
        assert status[tf]["close"] == 29425.0
        assert status[tf]["beyond"] is False       # closed ABOVE the swept low -> REJECTED


def test_smt_candidates_carry_suggested_exhausted(source):
    # plan 15 Task 5: every SMT candidate gets a code-computed suggested_exhausted (bool) and
    # a stretch_since_fire (float or None) from tier-relative stretch of the swept ticker.
    from derive_facts import SMT_SHELF_LIFE
    # ladder: each tier ~double the shelf life of the tier below (session < day < week).
    assert SMT_SHELF_LIFE["session"] < SMT_SHELF_LIFE["day"] < SMT_SHELF_LIFE["week"]
    boundary = pd.Timestamp("2026-07-14 01:00:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    assert bundle.smt_candidates, "expected SMT candidates at this boundary"
    for c in bundle.smt_candidates:
        assert isinstance(c["suggested_exhausted"], bool)
        assert c["stretch_since_fire"] is None or isinstance(c["stretch_since_fire"], float)
        if c["stretch_since_fire"] is not None:
            # the flag is exactly "stretch beyond this tier's shelf life"
            assert c["suggested_exhausted"] == (
                c["stretch_since_fire"] > SMT_SHELF_LIFE[c["tier"]])


def test_nested_day_levels_suppressed_2026_07_14(source):
    # thesis.md §2.1b refinement: at the 2026-07-14 01:00 ET boundary, MNQ's day lows are
    # prev1=29386.5 prev2=29677.5 prev3=29395.0 prev4=28910.25 prev5=29209.75 prev6=29683.25
    # prev7=29522.5 -- only prev1 (always valid) and prev4 (the sole level whose price
    # extends beyond every more-recent same-family level) are NOT nested; prev2/3/5/6/7 are
    # each superseded by a more-recent, deeper level and must be excluded from fresh P1
    # evidence, regardless of whether they were ever swept.
    from derive_facts import render_evidence_text, build_evidence_magnitude

    boundary = pd.Timestamp("2026-07-14 01:00:00", tz=TZ)
    bundle = _bundle_at(source, boundary)

    suppressed = bundle.suppressed_p1_levels["MNQ"]
    for name in ("prev2_day_low", "prev3_day_low", "prev5_day_low",
                 "prev6_day_low", "prev7_day_low"):
        assert name in suppressed, f"{name} should be nested/suppressed"
    for name in ("prev1_day_low", "prev4_day_low"):
        assert name not in suppressed, f"{name} should NOT be nested/suppressed"

    # prev3_day_low is nested (suppressed for P1) but is ALSO a live SMT candidate — its
    # close-status must still render (P2 context), tagged, not silently dropped. Its WICK
    # divergence fired 2026-07-13 15:38 ET (BEFORE nesting) -- grandfathered, must NOT be
    # p2_suppressed, even though a later, unrelated body-close instance at the same level
    # fired after nesting (thesis.md §2.1b's grandfather clause + SMT_LOOKBACK_HOURS wide
    # scan, both needed together: a session-scoped scan would misattribute the wick's
    # swept_at to that later instance and wrongly suppress the whole site).
    assert "prev3_day_low" not in bundle.suppressed_p2_sites["MNQ"]
    text = render_evidence_text(bundle, magnitude=build_evidence_magnitude(bundle))
    assert "MNQ prev3_day_low [1h]" in text
    assert "[nested/duplicate" in text
    # prev2_day_low is nested and NOT an SMT-candidate site -- must not render as a fresh
    # P1 item at all.
    assert "MNQ prev2_day_low [1h]" not in text


def test_day_hi_lo_extended_window_not_degenerate_right_after_1800(source):
    # Right after the mandatory 18:00 ET session-open call, the CURRENT session has ~1
    # minute of bars — a narrow session-only window would give a near-zero day_hi-day_lo
    # range. The extended window (_day_start_ts, Asia case: today at 06:00 ET) reaches back
    # into the prior session's NY-morning-through-close, so the range must be real.
    boundary = pd.Timestamp("2026-07-16 18:01:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    dh, dl = bundle.day_hi["MNQ"], bundle.day_lo["MNQ"]
    assert dh is not None and dl is not None
    assert dh - dl > 10.0, "extended window must cover more than a single opening tick"

    # The S1 "day running" TEXT line stays hash-sensitive (shared with the old KB docs) and
    # unaffected by the extended structured fields — test_facts_parity_with_prepare_cuts is
    # the authoritative proof of that; here just confirm the line still renders normally.
    text = render_facts_text(bundle)
    assert "day running:" in text


def test_near_maturity_candidates_wired_2026_07_16_2000(source):
    # thesis.md §3a motivating case: at 2026-07-16 20:00 ET (now = 19:59:59, one minute
    # before the 4h close), MNQ's prev1_day_low sweep at 19:26 is a near-maturity candidate.
    boundary = pd.Timestamp("2026-07-16 20:00:00", tz=TZ)
    res = source.build_facts(boundary)
    assert not res.degraded
    cands = res.validator_dict["near_maturity_candidates"]
    assert isinstance(cands, list) and cands
    for c in cands:
        for key in ("asset", "level", "tier", "tf", "resolves_at", "minutes_remaining",
                    "now_distance_ratio", "implied_direction", "distance_safe",
                    "corroborated", "preconfirm_eligible"):
            assert key in c
        assert c["minutes_remaining"] <= NEAR_MATURITY_WINDOW_MIN
        assert c["tier"] in ("day", "week")
    site = next((c for c in cands if c["asset"] == "MNQ" and c["level"] == "prev1_day_low"
                and c["tf"] == "4h"), None)
    assert site is not None, "the motivating MNQ prev1_day_low 4h candidate must be present"


def test_prev_day_levels_absent_without_hist(source):
    # plan 15 Task 1 edge case: no hist supplied -> the deeper levels are simply absent,
    # not an error; prev1/prev2_day and prev1_week are unaffected.
    boundary = pd.Timestamp("2026-07-14 01:00:00", tz=TZ)
    prim = {}
    for tk in source.tickers:
        norm = source._norm_1s[tk]
        prim[tk] = norm[(norm.index >= boundary - LOOKBACK) & (norm.index < boundary)]
    bundle = compute_facts(prim["MNQ"], prim["MES"], now=None)   # no hist_mnq/hist_mes
    lv = bundle.levels["MNQ"]
    assert "prev1_day_low" in lv and "prev2_day_low" in lv       # existing path intact
    assert not any(k.startswith(("prev3_day", "prev4_day", "prev2_week", "prev3_week"))
                   for k in lv)


def test_evidence_text_populated_and_not_in_content_hash(source):
    boundary = pd.Timestamp("2026-06-25 08:50:00", tz=TZ)
    res = source.build_facts(boundary)
    assert not res.degraded
    assert res.evidence_text
    assert res.evidence_text.startswith("## S9 THESIS EVIDENCE")
    # the core content_hash (S0-S7 only) must not change when evidence_text exists.
    assert res.content_hash == hashlib.sha256(res.text.encode("utf-8")).hexdigest()
