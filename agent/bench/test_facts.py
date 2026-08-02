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
    FVG_LOOKBACK_DAYS, NEAR_MATURITY_WINDOW_MIN, compute_facts,
    render_evidence_text, render_facts_text,
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
    # see. MNQ's own HTF close REJECTED the swept low on both 1h and 4h (1h close unaffected
    # by the 18:00-ET-session 4h-bar anchor fix, since 1h bins are anchor-invariant; the 4h
    # close value shifted because the 4h bar boundary moved from the old midnight anchor to
    # the correct 18:00 ET session anchor).
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
    for tf, expected_close in (("1h", 29425.0), ("4h", 29448.25)):
        assert status[tf] is not None, f"prev3_day_low immature on {tf}"
        assert status[tf]["close"] == expected_close
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


def test_nested_session_levels_suppressed_2026_07_15(source):
    # thesis.md §2.1b extension (2026-08-01): confirmed real case -- at 2026-07-15 09:20 ET,
    # MES's asia(cur)_high (7608.5) is nested under its later, deeper london(cur)_high
    # (7613.5). Root-cause audit found this level still surfacing as fresh P1 evidence
    # before this fix (it was invisible to the prevN-only nesting regex).
    boundary = pd.Timestamp("2026-07-15 09:20:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    assert "asia(cur)_high" in bundle.suppressed_p1_levels["MES"]
    # And Fix 1's time-of-day gate independently removes ny_evening(prev1)_high from the
    # facts entirely at this boundary (00:00-17:59 ET branch) -- confirm it's not even a
    # candidate for nesting to act on (both fixes compose without conflict).
    assert "ny_evening(prev1)_high" not in bundle.levels["MNQ"]


def test_nested_session_levels_suppressed_2026_07_27(source):
    # Second confirmed real case: at 2026-07-27 09:20 ET, MNQ's asia(cur)_high (28733.5) is
    # nested under its later london(cur)_high (28763.75), which is ALSO that day's running
    # high (set 05:51 ET).
    boundary = pd.Timestamp("2026-07-27 09:20:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    assert "asia(cur)_high" in bundle.suppressed_p1_levels["MNQ"]


def test_nested_session_levels_prev1_still_nests_under_prev1(source):
    # Sanity: two (prev1) levels of the same side should still nest against each other
    # (not just against (cur)) when the fixed sequence orders one after the other, at a
    # boundary where (prev1) levels ARE actually offered (Asia-forming window, hour>=18).
    from derive_facts import _nested_session_levels
    lv = {
        "asia(prev1)_high": (100.0, 100.0, "above", "session", None),
        "london(prev1)_high": (105.0, 105.0, "above", "session", None),   # later, deeper
        "ny_morning(prev1)_high": (102.0, 102.0, "above", "session", None),  # later, shallower
    }
    nested = _nested_session_levels(lv)
    assert "asia(prev1)_high" in nested        # superseded by london(prev1) (deeper) and ny_morning(prev1)
    assert "london(prev1)_high" not in nested  # nothing later reaches as far
    assert "ny_morning(prev1)_high" not in nested  # nothing LATER than it reaches as far (london is earlier)


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


# --- Fix 1: (prev1) sub-session levels time-gated by ET clock ---

def test_prev1_subsession_levels_gated_london_onward_2026_07_15(source):
    # 2026-07-15 09:20 ET (now.hour = 9, the 00:00-17:59 "London onward" branch): NO
    # (prev1) sub-session level of any sub-session may be offered — their (cur) copies are
    # the only valid reference this late. This is the exact case that motivated the fix
    # (ny_evening(prev1)_high fed 4 evidence citations to a wrong UP call this date).
    boundary = pd.Timestamp("2026-07-15 09:20:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    for tkr in ("MNQ", "MES"):
        for sess in ("asia", "london", "ny_morning", "ny_evening"):
            for edge in ("high", "low"):
                assert f"{sess}(prev1)_{edge}" not in bundle.levels[tkr], \
                    f"{sess}(prev1)_{edge} should be gated out at 09:20 ET on {tkr}"


def test_prev1_subsession_levels_gated_during_asia_2026_07_15_2000(source):
    # 2026-07-15 20:00 ET (now = 19:59:59, now.hour = 19, today's Asia session forming):
    # ONLY ny_morning/ny_evening prev1 copies stay in scope; asia/london prev1 are dropped.
    boundary = pd.Timestamp("2026-07-15 20:00:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    for tkr in ("MNQ", "MES"):
        lv = bundle.levels[tkr]
        assert "ny_morning(prev1)_high" in lv or "ny_morning(prev1)_low" in lv, \
            f"ny_morning(prev1) should be present during Asia on {tkr}"
        assert "ny_evening(prev1)_high" in lv or "ny_evening(prev1)_low" in lv, \
            f"ny_evening(prev1) should be present during Asia on {tkr}"
        for sess in ("asia", "london"):
            for edge in ("high", "low"):
                assert f"{sess}(prev1)_{edge}" not in lv, \
                    f"{sess}(prev1)_{edge} should be gated out during Asia on {tkr}"


def test_cur_subsession_levels_unaffected_by_prev1_gate(source):
    # Happy path: the (cur) sub-session emission is gated only by its own closes_at rule
    # and is completely unaffected by the (prev1) time-gate. At 09:20 ET the asia(cur) and
    # london(cur) sub-sessions have already closed, so they must still be present.
    boundary = pd.Timestamp("2026-07-15 09:20:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    for tkr in ("MNQ", "MES"):
        cur_names = [k for k in bundle.levels[tkr] if "(cur)" in k]
        assert cur_names, f"expected (cur) sub-session levels on {tkr}"
        assert any(k.startswith("asia(cur)") for k in cur_names), \
            f"asia(cur) should be present at 09:20 ET on {tkr}"
        assert any(k.startswith("london(cur)") for k in cur_names), \
            f"london(cur) should be present at 09:20 ET on {tkr}"


# --- Fix 2: curated + verdicted S9 FVG-fill (P5) candidates ---

def _s9_fvg_line(bundle, zone_id):
    """The single S9 FVG-FILL line for zone_id, or None if not curated into S9."""
    text = render_evidence_text(bundle)
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("FVG-FILL CANDIDATES"))
    except StopIteration:
        return None
    for ln in lines[start + 1:]:
        if ln and not ln.startswith("  "):        # left column -> next S9 subsection
            break
        if zone_id in ln:
            return ln.strip()
    return None


def test_fvg_s9_curated_and_verdicted_2026_07_20(source):
    # 2026-07-20 09:20 ET: the S9 FVG list is curated to zones formed within
    # FVG_LOOKBACK_DAYS days and each carries a HELD/VIOLATED verdict. Two real
    # surviving zones: a bear zone that was violated and a bull zone that held.
    boundary = pd.Timestamp("2026-07-20 09:20:00", tz=TZ)
    bundle = _bundle_at(source, boundary)

    violated = _s9_fvg_line(bundle, "MNQ 1hr 2026-07-17 14:00:00-04:00 bear")
    assert violated is not None, "expected the 07-17 14:00 bear zone in the curated S9 list"
    assert "VIOLATED (reject)" in violated

    held = _s9_fvg_line(bundle, "MNQ 1hr 2026-07-19 18:00:00-04:00 bull")
    assert held is not None, "expected the 07-19 18:00 bull zone in the curated S9 list"
    assert "HELD (accept)" in held

    # Curation is doing something: a genuinely visited zone older than FVG_LOOKBACK_DAYS
    # (07-10 12:00, inside the S6 10-day scan but outside the S9 lookback window) is
    # EXCLUDED from S9.
    old_id = "MNQ 1hr 2026-07-10 12:00:00-04:00 bull"
    old_zone = next((z for z in bundle.fvg_zones if z["id"] == old_id), None)
    assert old_zone is not None and old_zone["visited"], "the 07-10 zone should be a real visited zone"
    assert (bundle.now - old_zone["ts"]) > pd.Timedelta(days=FVG_LOOKBACK_DAYS)
    assert _s9_fvg_line(bundle, old_id) is None, "the too-old zone must be curated out of S9"


def test_fvg_s9_too_recent_to_verdict_2026_07_23_1830(source):
    # 2026-07-23 18:30 ET: the MES 1hr 15:00 bull zone was visited at 18:13, after the last
    # qualifying 1h/4h close, so it has NO mature verdict yet — it must render as
    # '(fill too recent to verdict)', never a fabricated HELD.
    boundary = pd.Timestamp("2026-07-23 18:30:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    zid = "MES 1hr 2026-07-23 15:00:00-04:00 bull"
    zone = next((z for z in bundle.fvg_zones if z["id"] == zid), None)
    assert zone is not None and zone["visited"], "expected the 15:00 bull zone visited"
    assert zone["fill_verdict"] is None, "verdict should be immature at this boundary"
    line = _s9_fvg_line(bundle, zid)
    assert line is not None, "the fresh zone is within the lookback window -> should be in S9"
    assert "(fill too recent to verdict)" in line
    assert "HELD" not in line and "VIOLATED" not in line


def test_fvg_s9_lookback_covers_same_time_of_day_4_days_back_2026_07_27(source):
    # Regression for the FVG_LOOKBACK_DAYS=3 -> 5 bug: evaluated at 2026-07-27 09:20 ET, the
    # 2026-07-23 07:00/08:00 ET bearish zones (formed 4 days earlier, same time of day) were
    # silently excluded at 3 days -- `now - 3 days` lands at ~07-24 09:20, AFTER those same-
    # morning zones formed on 07-23. 5 days must include them.
    boundary = pd.Timestamp("2026-07-27 09:20:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    for zid in ("MNQ 1hr 2026-07-23 07:00:00-04:00 bear",
                "MNQ 1hr 2026-07-23 08:00:00-04:00 bear",
                "MNQ 4hr 2026-07-23 06:00:00-04:00 bear"):
        line = _s9_fvg_line(bundle, zid)
        assert line is not None, f"{zid} must survive S9 curation at FVG_LOOKBACK_DAYS={FVG_LOOKBACK_DAYS}"


def test_fvg_s6_raw_list_unchanged_by_s9_curation_2026_07_20(source):
    # S6 stays the full historical list (10-day scan) regardless of the S9-only curation:
    # the 07-10 zone excluded from S9 above is still present in the S6 render.
    boundary = pd.Timestamp("2026-07-20 09:20:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    s6 = render_facts_text(bundle)
    assert "## S6 FVGs" in s6
    assert "MNQ 1hr 2026-07-10 12:00:00-04:00 bull zone" in s6, \
        "S6 must keep the full historical FVG list, uncurated"
    # and the S6 line still uses the original visited/UNVISITED wording, no verdict appended.
    assert "-> VIOLATED" not in s6 and "-> HELD" not in s6


# --- Fix 3: HTF-close verdict for daily_mid / weekly_mid ---

def _mid_render_lines(bundle):
    return [ln.strip() for ln in render_evidence_text(bundle).splitlines()
            if "weekly_mid [" in ln or "daily_mid [" in ln]


def test_weekly_mid_htf_verdict_rejected_2026_07_27(source):
    # Plan Fix 3 test 1 (the motivating wrong-call case): at 2026-07-27 09:20 ET MNQ's 1h
    # closes sat BELOW its weekly mid (28747.75) for consecutive bars despite wicking above
    # intrabar, so the mid must carry a mature REJECTED (failed-reclaim) verdict — not a
    # bare number. side is the fixed "above" mid convention: beyond=False == closed below.
    boundary = pd.Timestamp("2026-07-27 09:20:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    st = bundle.htf_close_status["MNQ"]["weekly_mid"]["1h"]
    assert st is not None, "MNQ weekly_mid 1h must be mature at this boundary"
    assert st["beyond"] is False, "1h closed below the weekly mid -> REJECTED"
    assert bundle.swept_at["MNQ"]["weekly_mid"] is not None
    # renders as a real REJECTED verdict line, the P4 anchor that was previously missing.
    assert any("weekly_mid [1h]" in ln and "REJECTED" in ln for ln in _mid_render_lines(bundle))
    # bare daily_mid scalar line added symmetrically alongside weekly_mid.
    assert "daily_mid = " in render_evidence_text(bundle)


def test_weekly_mid_htf_verdict_accepted_reclaim_2026_07_22(source):
    # Plan Fix 3 test 2 (HTF reclaim-from-below): at 2026-07-22 09:20 ET MES's most recent
    # completed 1h bar closed ABOVE its weekly mid -> ACCEPTED (held reclaim, beyond=True).
    # NOTE (reported divergence from the plan's literal expectation): MNQ's weekly_mid is
    # NOT a mature ACCEPTED here — MNQ price is straddling its own weekly mid at the call
    # (last crossing 09:02:47, no qualifying completed 1h close since), so a faithful
    # implementation must leave it immature rather than fabricate an ACCEPTED verdict.
    boundary = pd.Timestamp("2026-07-22 09:20:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    mes = bundle.htf_close_status["MES"]["weekly_mid"]["1h"]
    assert mes is not None and mes["beyond"] is True, "MES 1h closed above weekly mid -> ACCEPTED"
    # MNQ straddles its mid at this instant -> honestly immature, not a fabricated verdict.
    assert bundle.htf_close_status["MNQ"]["weekly_mid"]["1h"] is None


def test_mid_no_recent_crossing_is_none_2026_07_27(source):
    # Plan Fix 3 test 3: when price stayed on one side of the mid across the whole lookback
    # (never crossed), the synthetic level is left unswept -> status None on both TFs and it
    # produces NO evidence line, exactly as an unswept named level does. MES's weekly mid at
    # 2026-07-27 09:20 ET is such a case (price held one side of it for the full 24h window).
    boundary = pd.Timestamp("2026-07-27 09:20:00", tz=TZ)
    bundle = _bundle_at(source, boundary)
    assert bundle.swept_at["MES"]["weekly_mid"] is None, "no crossing -> unswept"
    st = bundle.htf_close_status["MES"]["weekly_mid"]
    assert st["1h"] is None and st["4h"] is None
    # an unswept mid is skipped by the render loop (never-swept -> not evidence).
    assert not any(ln.startswith("MES weekly_mid [") for ln in _mid_render_lines(bundle))
