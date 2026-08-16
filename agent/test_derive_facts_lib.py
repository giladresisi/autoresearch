"""Tests for the importable derive_facts library (GIL-44 Phase 1, Wave 1.1).

The refactor is a PURE extraction: compute_facts() now holds the maths that used to
be inline in main(), render_facts_text() reproduces the exact stdout, and
facts_to_validator_dict() yields the JSON view the semantic validator consumes. The
gate is IDENTITY — the rendered fact sheet must be byte-for-byte what the pre-refactor
print-script produced.

Fixtures (agent/fixtures/derive_facts_golden/): a compact ~3-trade-day 1s slice of the
2026-05-19 09:28 cut for both tickers, plus golden_facts.txt — the stdout the ORIGINAL
(pre-refactor) derive_facts.py produced on those slices (no --hist-dir, so no S5b).
"""

import datetime
import os

import pandas as pd
import pytest

import derive_facts
from derive_facts import (
    FactsBundle,
    compute_facts,
    facts_to_validator_dict,
    load,
    render_facts_text,
)

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "derive_facts_golden")
# The ATH values passed when the golden was captured (prepare_cuts computes these from
# the full pre-cut history; pinned here so the ATH line matches the golden byte-for-byte).
ATH_MNQ = 30077.0
ATH_MES = 7602.5


def _load_fixture_slices():
    mnq = load(os.path.join(FIX, "MNQ_1s_slice.parquet"))
    mes = load(os.path.join(FIX, "MES_1s_slice.parquet"))
    return mnq, mes


def _fixtures_present():
    return (os.path.exists(os.path.join(FIX, "MNQ_1s_slice.parquet"))
            and os.path.exists(os.path.join(FIX, "golden_facts.txt")))


pytestmark = pytest.mark.skipif(
    not _fixtures_present(), reason="derive_facts golden fixtures not present")


# --------------------------------------------------------------------------- #
# 1. Golden text identity — the core Phase-1 gate.                             #
# --------------------------------------------------------------------------- #
def test_render_facts_text_byte_identical_to_golden():
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    rendered = render_facts_text(bundle)
    with open(os.path.join(FIX, "golden_facts.txt"), encoding="utf-8", newline="") as fh:
        golden = fh.read()
    assert rendered == golden
    assert rendered.encode("utf-8") == golden.encode("utf-8")


# --------------------------------------------------------------------------- #
# 2. Validator-dict parity — facts_to_validator_dict == parse_facts(render).   #
# --------------------------------------------------------------------------- #
def test_validator_dict_matches_parse_facts_fixture():
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    parse_facts = derive_facts._parse_facts()
    ref = parse_facts(render_facts_text(bundle))
    got = facts_to_validator_dict(bundle)
    assert got["now_price"] == ref["now_price"]
    assert got["checkpoint"] == ref["checkpoint"]
    assert got["levels"] == ref["levels"]


def test_menus_do_not_change_s0_s7_or_validator_dict():
    # Plan 11: the S8 menus are a separate, additive overlay — computing them must not
    # change render_facts_text (S0–S7) or the shared facts_to_validator_dict (which is
    # hashed as the shadow engine's facts identity).
    import hashlib
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    core = render_facts_text(bundle)
    core_hash = hashlib.sha256(core.encode("utf-8")).hexdigest()
    vd = facts_to_validator_dict(bundle)                 # this triggers no menu injection
    assert "menus" not in vd                             # shared view stays menu-free
    # Building + rendering the menu leaves the S0–S7 core byte-identical.
    menu_text = derive_facts.render_menus_text(bundle)
    assert render_facts_text(bundle) == core
    assert hashlib.sha256(render_facts_text(bundle).encode("utf-8")).hexdigest() == core_hash
    assert facts_to_validator_dict(bundle) == vd and "menus" not in facts_to_validator_dict(bundle)
    # S8 lives ONLY in the separate block, never in the S0–S7 render.
    assert "## S8" not in core and "MENU" not in core
    assert menu_text.startswith("## S8 MENUS")


def test_validator_dict_parity_on_committed_cuts():
    """Cross-view consistency on >=3 real committed cut fact sheets: a bundle whose
    lines ARE a committed facts.txt renders back to it, so facts_to_validator_dict
    equals parse_facts on the same text (the online semantic layer == the bench)."""
    cuts_root = os.path.join(os.path.dirname(HERE), "calibration", "cuts")
    parse_facts = derive_facts._parse_facts()
    checked = 0
    for cid in sorted(os.listdir(cuts_root)):
        fpath = os.path.join(cuts_root, cid, "facts.txt")
        if not os.path.exists(fpath):
            continue
        with open(fpath, encoding="utf-8", newline="") as fh:
            text = fh.read()
        bundle = FactsBundle(lines=text.splitlines())
        # The committed cuts are CRLF (captured from a Windows subprocess stdout);
        # render_facts_text is LF (print re-adds CRLF at stdout). Compare on LF.
        assert render_facts_text(bundle) == text.replace("\r\n", "\n")
        # parse_facts is line-based (newline-agnostic), so the validator view built
        # from the LF render equals parse_facts on the original CRLF text.
        assert facts_to_validator_dict(bundle) == parse_facts(text)
        checked += 1
        if checked >= 3:
            break
    assert checked >= 3


# --------------------------------------------------------------------------- #
# 3. Empty/short slice — no exception, date universe truncates to today only.  #
# --------------------------------------------------------------------------- #
def test_short_slice_no_crash():
    ts = pd.date_range("2026-05-19 08:00:00", periods=500, freq="1s", tz="America/New_York")
    price = 30000.0 + pd.Series(range(500), index=ts) * 0.0
    stub = pd.DataFrame({"open": price, "high": price + 1, "low": price - 1, "close": price},
                        index=ts)
    bundle = compute_facts(stub, stub)          # must not raise
    text = render_facts_text(bundle)
    assert "## S0 META" in text
    # <1000 bars for the single day → only today in the trade-date universe.
    assert bundle.td_now == datetime.date(2026, 5, 19)


# --------------------------------------------------------------------------- #
# 4. No-ATH path — ath=None omits the ATH line, no crash.                      #
# --------------------------------------------------------------------------- #
def test_no_ath_omits_ath_line():
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=None, ath_mes=None)
    text = render_facts_text(bundle)
    assert "% below" not in text            # the ATH line is the only "% below" line
    assert "## S1 LEVELS MNQ" in text       # everything else still renders


# --------------------------------------------------------------------------- #
# 5. Checkpoint truncation — data after `now` never enters the bundle.         #
# --------------------------------------------------------------------------- #
def test_now_truncation_no_lookahead():
    mnq, mes = _load_fixture_slices()
    full = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    earlier = full.now - pd.Timedelta(hours=1)
    trunc = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES, now=earlier)
    assert trunc.now == earlier
    # No bar after `now` may influence the result: the MNQ "now" price is the last
    # close AT OR BEFORE the truncation point, which differs from the full-run price.
    mnq_trunc = mnq[mnq.index <= earlier]
    assert trunc.now_price == float(mnq_trunc["close"].iloc[-1])
    assert f"now = {earlier}" in render_facts_text(trunc)


# --------------------------------------------------------------------------- #
# 6. weekly_mid fact field — MNQ running week mid at now.                     #
# --------------------------------------------------------------------------- #
def test_weekly_mid_computed_for_mnq():
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    assert bundle.weekly_mid is not None
    assert isinstance(bundle.weekly_mid, float)


def test_weekly_mid_matches_rendered_week_running_line():
    import re
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    text = render_facts_text(bundle)
    m = re.search(
        r"week running \[ENGINE anchor [^\]]+\]: high=([\-0-9.]+) low=([\-0-9.]+)", text)
    assert m is not None
    high, low = float(m.group(1)), float(m.group(2))
    assert bundle.weekly_mid == round((high + low) / 2.0, 2)


# --------------------------------------------------------------------------- #
# 7. swept_at fact field — per-level sweep timestamps.                       #
# --------------------------------------------------------------------------- #
def test_swept_at_matches_rendered_sweep_lines():
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    text = render_facts_text(bundle)
    assert "MNQ" in bundle.swept_at and "MES" in bundle.swept_at
    for tkr in ("MNQ", "MES"):
        start = text.index(f"## S2 SWEEPS {tkr} ")
        end = text.index("## S", start + 5)
        section = text[start:end]
        for line in section.splitlines():
            line = line.strip()
            if not line or line.startswith("##"):
                continue
            name = line.split(" ", 1)[0]
            if ": NOT swept" in line:
                assert bundle.swept_at[tkr].get(name) is None, line
            elif ": swept " in line:
                assert bundle.swept_at[tkr].get(name) is not None, line


def test_swept_at_covers_every_sided_level():
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    for tkr in ("MNQ", "MES"):
        for name, tup in bundle.levels[tkr].items():
            if tup[2] is None:          # side is None -> not tracked (matches S2's own skip)
                continue
            assert name in bundle.swept_at[tkr]


# --------------------------------------------------------------------------- #
# 8. htf_close_status fact field — maturity gate + accept/reject per level.   #
# --------------------------------------------------------------------------- #
def test_htf_close_status_immature_when_no_htf_bar_closed_since_sweep():
    ts = pd.date_range("2026-07-02 09:00:00", periods=40, freq="1min", tz="America/New_York")
    price = pd.Series([100.0] * 40, index=ts)
    df = pd.DataFrame({"open": price, "high": price + 0.5, "low": price - 0.5, "close": price},
                      index=ts)
    swept_at = ts[5]
    now = ts[-1]
    status = derive_facts._htf_close_status(df, swept_at, price=100.0, side="above", now=now)
    assert status["1h"] is None
    assert status["4h"] is None


def test_htf_close_status_accept_beyond_after_1h_close():
    ts = pd.date_range("2026-07-02 09:00:00", periods=180, freq="1min", tz="America/New_York")
    price = pd.Series([99.0] * 60 + [101.0] * 60 + [101.0] * 60, index=ts)
    df = pd.DataFrame({"open": price, "high": price + 0.5, "low": price - 0.5, "close": price},
                      index=ts)
    swept_at = ts[30]      # 09:30, mid the first (09:00-10:00) hour
    now = ts[-1]           # 11:59
    status = derive_facts._htf_close_status(df, swept_at, price=100.0, side="above", now=now)
    assert status["1h"] is not None
    assert status["1h"]["beyond"] is True
    assert status["1h"]["close"] == 101.0
    assert status["1h"]["n_closed_since"] == 2   # the 09:00-10:00 and 10:00-11:00 bars


def test_htf_close_status_reject_before_after_1h_close():
    ts = pd.date_range("2026-07-02 09:00:00", periods=180, freq="1min", tz="America/New_York")
    price = pd.Series([99.0] * 25 + [101.0] * 5 + [99.5] * 150, index=ts)
    df = pd.DataFrame({"open": price, "high": price + 0.5, "low": price - 0.5, "close": price},
                      index=ts)
    swept_at = ts[26]      # 09:26
    now = ts[-1]           # 11:59
    status = derive_facts._htf_close_status(df, swept_at, price=100.0, side="above", now=now)
    assert status["1h"] is not None
    assert status["1h"]["beyond"] is False
    assert status["1h"]["close"] == 99.5


def test_htf_close_status_none_swept_at_is_immature():
    ts = pd.date_range("2026-07-02 09:00:00", periods=180, freq="1min", tz="America/New_York")
    price = pd.Series([100.0] * 180, index=ts)
    df = pd.DataFrame({"open": price, "high": price + 0.5, "low": price - 0.5, "close": price},
                      index=ts)
    status = derive_facts._htf_close_status(df, None, price=100.0, side="above", now=ts[-1])
    assert status == {"1h": None, "4h": None}


def _lv_day_low(prices):
    """Build a minimal lv dict of prevN_day_low entries at the given {n: price}."""
    return {f"prev{n}_day_low": (price, price, "below", "day", pd.Timestamp("2026-01-01"))
            for n, price in prices.items()}


def test_nested_prev_levels_matches_2026_07_14_worked_example():
    # thesis.md §2.1b's own worked example: MNQ day lows prev1=29386.5 prev2=29677.5
    # prev3=29395.0 prev4=28910.25 prev5=29209.75 prev6=29683.25 prev7=29522.5 -- only
    # prev1 (always valid) and prev4 (extends beyond every more-recent level) are NOT
    # nested; every other level is superseded by a more-recent, deeper one.
    lv = _lv_day_low({1: 29386.5, 2: 29677.5, 3: 29395.0, 4: 28910.25, 5: 29209.75,
                      6: 29683.25, 7: 29522.5})
    nested = derive_facts._nested_prev_levels(lv)
    assert nested == {"prev2_day_low", "prev3_day_low", "prev5_day_low",
                      "prev6_day_low", "prev7_day_low"}


def test_nested_prev_levels_high_side_mirrors_low_side():
    # highs mirror lows: nested if a more-recent level's price is AT OR ABOVE this one's.
    # prev1 (110) is the deepest/most-recent high -- prev2/prev3 (lower, older) are nested.
    lv = {"prev1_day_high": (110.0, 110.0, "above", "day", pd.Timestamp("2026-01-01")),
         "prev2_day_high": (105.0, 105.0, "above", "day", pd.Timestamp("2026-01-01")),
         "prev3_day_high": (100.0, 100.0, "above", "day", pd.Timestamp("2026-01-01"))}
    nested = derive_facts._nested_prev_levels(lv)
    assert nested == {"prev2_day_high", "prev3_day_high"}   # neither exceeds prev1's 110


def test_nested_prev_levels_prev1_never_nested():
    lv = _lv_day_low({1: 30000.0, 2: 1.0})     # prev1 shallower than everything -- still valid
    assert "prev1_day_low" not in derive_facts._nested_prev_levels(lv)


def test_nested_prev_levels_families_independent():
    # a day_low family and a week_low family with the same N must not cross-contaminate.
    # prev2_day_low (50) is DEEPER than prev1 (100) -> not nested; prev2_week_low (200) is
    # SHALLOWER than prev1_week_low (150) -> nested.
    lv = {"prev1_day_low": (100.0, 100.0, "below", "day", pd.Timestamp("2026-01-01")),
         "prev2_day_low": (50.0, 50.0, "below", "day", pd.Timestamp("2026-01-01")),
         "prev1_week_low": (150.0, 150.0, "below", "week", pd.Timestamp("2026-01-01")),
         "prev2_week_low": (200.0, 200.0, "below", "week", pd.Timestamp("2026-01-01"))}
    nested = derive_facts._nested_prev_levels(lv)
    assert nested == {"prev2_week_low"}


def test_duplicate_sweep_losers_keeps_highest_tier():
    t = pd.Timestamp("2026-07-13 18:00:00", tz="America/New_York")
    lv = {"prev1_day_low": (100.0, 100.0, "below", "day", pd.Timestamp("2026-01-01")),
         "asia(prev1)_low": (100.0, 100.0, "below", "session", pd.Timestamp("2026-01-01"))}
    swept_at = {"prev1_day_low": t, "asia(prev1)_low": t}
    losers = derive_facts._duplicate_sweep_losers(lv, swept_at)
    assert losers == {"asia(prev1)_low"}


def test_duplicate_sweep_losers_session_open_cascade_collapses_regardless_of_price_spread():
    # legitimate case (2026-07-13/07-16 examples): the session's OWN opening bar is when
    # several already-stale, genuinely different-priced old lows all register as "swept"
    # simultaneously (they were breached before this session's visible history began) --
    # this MUST still collapse to one representative regardless of the price spread.
    t = pd.Timestamp("2026-07-13 18:00:00", tz="America/New_York")
    lv = {"TDO": (29500.0, None, None, "session", t),
         "asia(prev1)_low": (100.0, 100.0, "below", "session", pd.Timestamp("2026-01-01")),
         "london(prev1)_low": (95.0, 95.0, "below", "session", pd.Timestamp("2026-01-01")),
         "ny_morning(prev1)_low": (90.0, 90.0, "below", "session", pd.Timestamp("2026-01-01"))}
    swept_at = {"asia(prev1)_low": t, "london(prev1)_low": t, "ny_morning(prev1)_low": t}
    losers = derive_facts._duplicate_sweep_losers(lv, swept_at)
    assert losers == {"asia(prev1)_low", "london(prev1)_low"}    # 90.0 is the deepest low


def test_duplicate_sweep_losers_non_open_different_prices_not_collapsed():
    # regression case (2026-07-10 12:00 ET MES): prev1_day_high (7595.0) and prev1_week_high
    # (7594.0) crossed within the SAME 1-second tick well after the session opened -- a fast
    # multi-level break, NOT the same physical level. Must NOT collapse: prev1_day_high is
    # index-1 and must never be suppressible.
    t = pd.Timestamp("2026-07-10 08:13:57", tz="America/New_York")
    lv = {"TDO": (7600.0, None, None, "session", pd.Timestamp("2026-07-09 18:00:00",
                                                              tz="America/New_York")),
         "prev1_day_high": (7595.0, 7595.0, "above", "day", pd.Timestamp("2026-01-01")),
         "prev1_week_high": (7594.0, 7594.0, "above", "week", pd.Timestamp("2026-01-01"))}
    swept_at = {"prev1_day_high": t, "prev1_week_high": t}
    assert derive_facts._duplicate_sweep_losers(lv, swept_at) == set()


def test_duplicate_sweep_losers_different_timestamps_not_collapsed():
    lv = {"prev1_day_low": (100.0, 100.0, "below", "day", pd.Timestamp("2026-01-01")),
         "prev2_day_low": (100.0, 100.0, "below", "day", pd.Timestamp("2026-01-01"))}
    swept_at = {"prev1_day_low": pd.Timestamp("2026-07-13 18:00:00", tz="America/New_York"),
               "prev2_day_low": pd.Timestamp("2026-07-13 19:00:00", tz="America/New_York")}
    assert derive_facts._duplicate_sweep_losers(lv, swept_at) == set()


def test_duplicate_sweep_losers_never_swept_ignored():
    lv = _lv_day_low({1: 100.0, 2: 100.0})
    losers = derive_facts._duplicate_sweep_losers(lv, {"prev1_day_low": None, "prev2_day_low": None})
    assert losers == set()


def test_suppressed_p1_levels_wired_into_compute_facts():
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    assert "MNQ" in bundle.suppressed_p1_levels and "MES" in bundle.suppressed_p1_levels
    for tkr in ("MNQ", "MES"):
        assert isinstance(bundle.suppressed_p1_levels[tkr], set)


def test_htf_close_status_wired_into_compute_facts_both_tickers():
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    assert "MNQ" in bundle.htf_close_status and "MES" in bundle.htf_close_status
    for tkr in ("MNQ", "MES"):
        for name, swept_ts in bundle.swept_at[tkr].items():
            status = bundle.htf_close_status[tkr].get(name)
            assert status is not None, name
            if swept_ts is None:
                assert status == {"1h": None, "4h": None}


def test_smt_candidates_meaningful_flag_matches_tier():
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    for cand in bundle.smt_candidates:
        assert cand["meaningful"] == (cand["tier"] in ("day", "week"))
        assert cand["swept_ticker"] != cand["unswept_ticker"]
        assert cand["swept_ticker"] in ("MNQ", "MES")
        assert cand["type"] in ("wick", "body")


def test_smt_candidates_wick_count_matches_rendered_tags():
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    text = render_facts_text(bundle)
    wick_candidates = [c for c in bundle.smt_candidates if c["type"] == "wick"]
    assert len(wick_candidates) == text.count("WICK DIVERGENCE CANDIDATE")


def test_smt_candidates_body_count_matches_rendered_tags():
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    text = render_facts_text(bundle)
    body_candidates = [c for c in bundle.smt_candidates if c["type"] == "body"]
    assert len(body_candidates) == text.count("BODY(15m) DIVERGENCE CANDIDATE")


# --------------------------------------------------------------------------- #
# 9. render_evidence_text — S9 additive block for thesis.md P1-P4 evidence.  #
# --------------------------------------------------------------------------- #
def test_evidence_text_separate_from_core_and_menus():
    import hashlib
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    core = render_facts_text(bundle)
    core_hash = hashlib.sha256(core.encode("utf-8")).hexdigest()
    vd = facts_to_validator_dict(bundle)
    ev = derive_facts.render_evidence_text(bundle)
    assert render_facts_text(bundle) == core
    assert hashlib.sha256(render_facts_text(bundle).encode("utf-8")).hexdigest() == core_hash
    assert facts_to_validator_dict(bundle) == vd and "menus" not in facts_to_validator_dict(bundle)
    assert "## S9" not in core
    assert ev.startswith("## S9 THESIS EVIDENCE")


def test_evidence_text_deterministic():
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    a = derive_facts.render_evidence_text(bundle)
    b = derive_facts.render_evidence_text(bundle)
    assert a == b


def test_evidence_text_flags_immature_and_shows_leader_lagger_labels():
    # NOTE: htf_close_status[tkr][name] == None for a (tf) entry means EITHER "never
    # swept" OR "swept but no qualifying HTF close yet" — the two are indistinguishable
    # from htf_close_status alone (bundle.swept_at disambiguates them). Since the S9
    # block only renders swept levels (never-swept levels are skipped entirely — see
    # test_evidence_text_never_swept_level_omitted_but_swept_immature_shown below),
    # "immature" can only legitimately appear in `ev` when a GENUINELY SWEPT level has a
    # None tf entry. Restrict has_immature to swept levels so this test's premise matches
    # post-fix rendering behavior (a never-swept-only fixture would make has_immature
    # False here, correctly expecting no "immature" text).
    mnq, mes = _load_fixture_slices()
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    ev = derive_facts.render_evidence_text(bundle)
    has_immature = any(
        v is None
        for tkr, status in bundle.htf_close_status.items()
        for name, tf_map in status.items()
        if bundle.swept_at.get(tkr, {}).get(name) is not None
        for v in tf_map.values()
    )
    if has_immature:
        assert "immature" in ev
    if bundle.smt_candidates:
        assert "(lagger)" in ev and "(leader)" in ev


def test_evidence_text_never_swept_level_omitted_but_swept_immature_shown():
    # Finding 1 fix: never-swept levels must not appear in the HTF close-status section
    # at all (they are indistinguishable from genuinely-swept-but-immature levels
    # otherwise), while a genuinely swept-but-immature level must still render as
    # "immature" — that IS real, gate-relevant evidence (thesis.md P1's maturity gate).
    bundle = derive_facts.FactsBundle()
    bundle.swept_at = {"MNQ": {"never_swept_pool": None, "swept_immature_pool": pd.Timestamp(
        "2026-07-02 10:00", tz="America/New_York")}, "MES": {}}
    bundle.htf_close_status = {
        "MNQ": {
            "never_swept_pool": {"1h": None, "4h": None},
            "swept_immature_pool": {"1h": None, "4h": None},
        },
        "MES": {},
    }
    ev = derive_facts.render_evidence_text(bundle)
    assert "never_swept_pool" not in ev
    assert "swept_immature_pool" in ev
    assert "immature" in ev


# --- day-extreme window extension (hypothesis.py::compute_live_hl_mid parity) ---------- #

def test_day_start_ts_asia_looks_back_to_prior_ny_morning():
    now = pd.Timestamp("2026-07-16 19:30:00", tz="America/New_York")
    assert derive_facts._day_start_ts(now) == pd.Timestamp(
        "2026-07-16 06:00:00", tz="America/New_York")


def test_day_start_ts_london_looks_back_to_prior_ny_evening():
    now = pd.Timestamp("2026-07-16 02:00:00", tz="America/New_York")
    assert derive_facts._day_start_ts(now) == pd.Timestamp(
        "2026-07-15 12:00:00", tz="America/New_York")


def test_day_start_ts_ny_morning_onward_is_the_current_session_open():
    now = pd.Timestamp("2026-07-16 10:00:00", tz="America/New_York")
    assert derive_facts._day_start_ts(now) == pd.Timestamp(
        "2026-07-15 18:00:00", tz="America/New_York")


# --- thesis.md §2.1b: P2/SMT nesting suppression (2026-08-03: no grandfather exception) - #

def _gf_bundle():
    """MNQ day-low/day-high families with prev1 deeper than prev2 on BOTH sides -- so
    prev2_day_low/prev2_day_high are CURRENTLY nested. Two SMT candidates, one per side,
    to exercise the suppress/no-op split."""
    b = derive_facts.FactsBundle()
    b.levels = {"MNQ": {
        "prev1_day_low": (50.0, 50.0, "below", "day", None),
        "prev2_day_low": (70.0, 70.0, "below", "day", None),
        "prev1_day_high": (200.0, 200.0, "above", "day", None),
        "prev2_day_high": (150.0, 150.0, "above", "day", None),
    }, "MES": {}}
    b.suppressed_p1_levels = {"MNQ": {"prev2_day_low", "prev2_day_high"}, "MES": set()}
    b.smt_candidates = [
        {"level": "prev2_day_low", "tier": "day", "side": "below", "swept_ticker": "MNQ",
         "unswept_ticker": "MES", "swept_at": pd.Timestamp("2026-07-12 10:00", tz="America/New_York"),
         "type": "wick", "meaningful": True, "suggested_exhausted": False},
        {"level": "prev2_day_high", "tier": "day", "side": "above", "swept_ticker": "MNQ",
         "unswept_ticker": "MES", "swept_at": pd.Timestamp("2026-07-16 10:00", tz="America/New_York"),
         "type": "wick", "meaningful": True, "suggested_exhausted": False},
        {"level": "prev1_day_low", "tier": "day", "side": "below", "swept_ticker": "MNQ",
         "unswept_ticker": "MES", "swept_at": pd.Timestamp("2026-07-16 11:00", tz="America/New_York"),
         "type": "wick", "meaningful": True, "suggested_exhausted": False},
    ]
    return b


def test_p2_nested_level_suppressed_regardless_of_when_it_fired():
    # 2026-08-03: the grandfather exception is gone -- prev2_day_low's divergence fired
    # 2026-07-12, well before prev1_day_low ever existed as a name, but nesting is a STATIC
    # price fact about the CURRENT level table, not a claim about firing order. It is
    # suppressed exactly like prev2_day_high (which fired AFTER nesting).
    b = _gf_bundle()
    out = derive_facts._p2_nesting_suppression(b)
    low_cand = next(c for c in b.smt_candidates if c["level"] == "prev2_day_low")
    high_cand = next(c for c in b.smt_candidates if c["level"] == "prev2_day_high")
    assert low_cand["p2_suppressed"] is True
    assert high_cand["p2_suppressed"] is True
    assert out["MNQ"] == {"prev2_day_low", "prev2_day_high"}


def test_p2_un_nested_frontier_level_not_suppressed():
    # prev1_day_low is never nested (nothing more recent exists in its family) -- its own
    # candidate, once it fires, stays valid P2 evidence.
    b = _gf_bundle()
    out = derive_facts._p2_nesting_suppression(b)
    frontier_cand = next(c for c in b.smt_candidates if c["level"] == "prev1_day_low")
    assert frontier_cand["p2_suppressed"] is False
    assert "prev1_day_low" not in out["MNQ"]


def test_p2_nesting_suppression_noop_when_not_currently_nested():
    b = _gf_bundle()
    b.suppressed_p1_levels = {"MNQ": set(), "MES": set()}   # nothing nested at all
    out = derive_facts._p2_nesting_suppression(b)
    for cand in b.smt_candidates:
        assert cand["p2_suppressed"] is False
    assert out == {"MNQ": set(), "MES": set()}


# --- thesis.md §10 (2026-08-05): _last_completed_bar / _mid_tf_state / _htf_reversal_tier #

def _minute_df(opens, highs, lows, closes, start="2026-07-22 08:00", freq="1min"):
    idx = pd.date_range(start, periods=len(opens), freq=freq, tz="America/New_York")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes}, index=idx)


def _flat_hour(price, start, n=60):
    idx = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    return pd.DataFrame({"open": [price] * n, "high": [price + 0.25] * n,
                        "low": [price - 0.25] * n, "close": [price] * n}, index=idx)


def test_last_completed_bar_reads_most_recent_completed_hour():
    # Two full completed hours (08:00-09:00, 09:00-10:00) + a partial 10:00-10:05 bar --
    # must read the 09:00-10:00 bar, not the still-forming 10:00 one.
    df = pd.concat([
        _flat_hour(100.0, "2026-07-22 08:00"),
        _flat_hour(110.0, "2026-07-22 09:00"),
        _flat_hour(999.0, "2026-07-22 10:00", n=5),
    ])
    now = pd.Timestamp("2026-07-22 10:05", tz="America/New_York")
    bar = derive_facts._last_completed_bar(df, "1h", now)
    assert bar["close"] == 110.0
    assert bar["closed_at"] == pd.Timestamp("2026-07-22 10:00", tz="America/New_York")


def test_last_completed_bar_none_before_any_bar_closes():
    df = _flat_hour(100.0, "2026-07-22 08:00", n=10)
    now = pd.Timestamp("2026-07-22 08:10", tz="America/New_York")
    assert derive_facts._last_completed_bar(df, "1h", now) is None


# --- _htf_reversal_tier: none / discount / omit / reverse ------------------------------ #

_REV_BAR = {"open": 29055.00, "high": 29072.00, "low": 28961.25, "close": 29016.75}   # bearish


def test_reversal_none_when_level_not_recrossed():
    # now_price still below the level -- agrees with the bar's own bearish close.
    assert derive_facts._htf_reversal_tier(_REV_BAR, 29031.75, 29020.0) == "none"


def test_reversal_discount_when_recrossed_but_not_past_bar_open():
    assert derive_facts._htf_reversal_tier(_REV_BAR, 29031.75, 29040.0) == "discount"


def test_reversal_omit_when_past_bar_open_but_not_wick():
    assert derive_facts._htf_reversal_tier(_REV_BAR, 29031.75, 29060.0) == "omit"


def test_reversal_reverse_when_past_bar_wick_2026_07_22_case():
    # the real MNQ 08:00-09:00 1h bar + weekly_mid (29031.75) / london(cur)_low (29026.0) +
    # now_price 29079.25 (09:20 ET) -- clear MSS on both levels.
    assert derive_facts._htf_reversal_tier(_REV_BAR, 29031.75, 29079.25) == "reverse"
    assert derive_facts._htf_reversal_tier(_REV_BAR, 29026.00, 29079.25) == "reverse"


def test_reversal_tiers_symmetric_for_a_bullish_completed_bar():
    bar = {"open": 100.0, "high": 106.0, "low": 99.0, "close": 104.0}   # bullish close
    assert derive_facts._htf_reversal_tier(bar, 102.0, 105.0) == "none"          # still above
    assert derive_facts._htf_reversal_tier(bar, 102.0, 101.0) == "discount"      # below level, above open
    assert derive_facts._htf_reversal_tier(bar, 102.0, 99.5) == "omit"           # below open, above low
    assert derive_facts._htf_reversal_tier(bar, 102.0, 98.0) == "reverse"        # below the wick too


# --- _mid_tf_state: per-tf status + freshness, anchored to COMPLETED bar crossings ----- #

def test_mid_tf_state_none_when_mid_never_crossed_among_completed_bars():
    df = pd.concat([_flat_hour(110.0, "2026-07-22 08:00"), _flat_hour(111.0, "2026-07-22 09:00")])
    now = pd.Timestamp("2026-07-22 09:30", tz="America/New_York")
    status, reclaim = derive_facts._mid_tf_state(df, 100.0, "1h", now, None, None)
    assert status is None and reclaim is None


def test_mid_tf_state_verdict_reads_latest_completed_bar_not_hidden_by_forming_bar_touch():
    # The exact 2026-07-22 09:00-09:20 ET MNQ weekly_mid shape: the 08:00-09:00 bar swept
    # the mid from above and closed below it; a re-touch happens INSIDE the still-forming
    # 09:00-10:00 bar (price back above by 09:02). The verdict must still read the
    # COMPLETED 08:00-09:00 bar's own close as "below" (reject-of-above / accept-of-below),
    # not be hidden by the later, immature re-touch.
    df = pd.concat([
        _flat_hour(29055.0, "2026-07-22 07:00"),                          # above mid all hour
        _minute_df(                                                        # 08:00-09:00: sweeps below, closes below
            [29055.0] + [29020.0] * 59,
            [29072.0] + [29030.0] * 59,
            [29028.5] + [28961.25] + [29010.0] * 58,
            [29038.0] * 2 + [29016.75] * 58,
            start="2026-07-22 08:00"),
        _minute_df(                                                        # 09:00-09:20: re-touches above at minute 2
            [29017.0, 29020.0, 29040.0] + [29079.25] * 17,
            [29020.0, 29040.0, 29079.25] + [29079.25] * 17,
            [29017.0, 29017.0, 29020.0] + [29079.25] * 17,
            [29017.0, 29040.0, 29079.25] + [29079.25] * 17,
            start="2026-07-22 09:00"),
    ])
    now = pd.Timestamp("2026-07-22 09:20", tz="America/New_York")
    status, reclaim = derive_facts._mid_tf_state(df, 29031.75, "1h", now, None, None)
    assert status is not None
    assert status["close"] == 29016.75
    assert status["beyond"] is False                        # closed below the mid
    assert status["closed_at"] == pd.Timestamp("2026-07-22 09:00", tz="America/New_York")
    assert reclaim["cross_dir"] == "down"


def test_mid_tf_state_freshness_stale_when_tier_extreme_postdates_crossing():
    # daily_mid crossed within the 07:00-08:00 bar (crosses to CLOSE below 100), but the
    # day's own low (lo_ts) was set later, inside the FOLLOWING 08:00-09:00 bar -- a later,
    # deeper move has superseded the crossing -- stale (matches the real MNQ daily_mid
    # case: crossed ~07:04 ET, but the day's own low was set at 08:55 ET, a later hour).
    df = pd.concat([
        _flat_hour(105.0, "2026-07-22 06:00"),
        _flat_hour(95.0, "2026-07-22 07:00"),       # crossing bar: closes below 100
        _flat_hour(90.0, "2026-07-22 08:00"),       # deeper low set THIS hour
        _flat_hour(90.0, "2026-07-22 09:00", n=5),
    ])
    now = pd.Timestamp("2026-07-22 09:05", tz="America/New_York")
    lo_ts = pd.Timestamp("2026-07-22 08:30", tz="America/New_York")   # day's own low, later hour
    status, reclaim = derive_facts._mid_tf_state(df, 100.0, "1h", now, None, lo_ts)
    assert status["beyond"] is False
    assert reclaim == {"fresh": False, "cross_dir": "down"}


def test_mid_tf_state_freshness_fresh_when_no_later_extreme():
    df = pd.concat([
        _flat_hour(105.0, "2026-07-22 07:00"),
        _flat_hour(95.0, "2026-07-22 08:00"),
        _flat_hour(95.0, "2026-07-22 09:00", n=5),
    ])
    now = pd.Timestamp("2026-07-22 09:05", tz="America/New_York")
    lo_ts = pd.Timestamp("2026-07-22 06:00", tz="America/New_York")   # week's low, days earlier
    status, reclaim = derive_facts._mid_tf_state(df, 100.0, "1h", now, None, lo_ts)
    assert reclaim == {"fresh": True, "cross_dir": "down"}


def test_build_evidence_magnitude_covers_synthetic_mid_and_p4_variants():
    # 2026-08-05: daily_mid/weekly_mid previously had no entry in bundle.levels, so
    # build_evidence_magnitude silently skipped them -- every P3 mid item scored at an
    # unweighted x1.0 regardless of how thin its clearance was. bundle.mid_price now
    # supplies the price; the SAME ratio is also registered under both P4-promoted names.
    b = derive_facts.FactsBundle()
    b.avg_range_1h = {"MNQ": 10.0}
    b.htf_close_status = {"MNQ": {"daily_mid": {
        "1h": {"close": 101.0, "beyond": True, "closed_at": None, "n_closed_since": 1},
        "4h": None,
    }}, "MES": {}}
    b.levels = {"MNQ": {}, "MES": {}}
    b.mid_price = {"MNQ": {"daily_mid": 100.0}, "MES": {}}
    out = derive_facts.build_evidence_magnitude(b)
    assert out[("MNQ", "daily_mid", "1h")] == 0.1
    assert out[("MNQ", "daily_mid_high", "1h")] == 0.1
    assert out[("MNQ", "daily_mid_low", "1h")] == 0.1
    assert ("MNQ", "daily_mid", "4h") not in out


# --- thesis.md §2.1c: P1 equilibrium-staleness decay ------------------------------------ #

def test_p1_equilibrium_staleness_flags_level_price_has_since_reached_mid():
    b = derive_facts.FactsBundle()
    swept_ts = pd.Timestamp("2026-07-23 03:30:00", tz="America/New_York")
    b.levels = {"MNQ": {"prev1_day_low": (100.0, 100.0, "below", "day", None)}, "MES": {}}
    b.swept_at = {"MNQ": {"prev1_day_low": swept_ts}, "MES": {}}
    b.day_hi = {"MNQ": 110.0}
    b.day_lo = {"MNQ": 90.0}      # mid = 100.0
    idx = pd.date_range(swept_ts, periods=5, freq="1h", tz="America/New_York")
    data = {"MNQ": pd.DataFrame({"low": [95, 96, 99, 101, 102], "high": [97, 98, 100.5, 103, 104]},
                               index=idx)}
    out = derive_facts._p1_equilibrium_staleness(b, data)
    assert out["MNQ"]["prev1_day_low"] is True    # bar 3 (99-100.5) straddles mid=100


def test_p1_equilibrium_staleness_false_when_mid_never_reached():
    b = derive_facts.FactsBundle()
    swept_ts = pd.Timestamp("2026-07-23 03:30:00", tz="America/New_York")
    b.levels = {"MNQ": {"prev1_day_low": (100.0, 100.0, "below", "day", None)}, "MES": {}}
    b.swept_at = {"MNQ": {"prev1_day_low": swept_ts}, "MES": {}}
    b.day_hi = {"MNQ": 110.0}
    b.day_lo = {"MNQ": 90.0}      # mid = 100.0
    idx = pd.date_range(swept_ts, periods=3, freq="1h", tz="America/New_York")
    data = {"MNQ": pd.DataFrame({"low": [95, 96, 94], "high": [97, 98, 96]}, index=idx)}
    out = derive_facts._p1_equilibrium_staleness(b, data)
    assert out["MNQ"]["prev1_day_low"] is False


# --- thesis.md §3a: near-maturity pre-confirmation candidates -------------------------- #

def _nm_data(mnq_close, mes_close):
    return {
        "MNQ": pd.DataFrame({"close": [mnq_close]}),
        "MES": pd.DataFrame({"close": [mes_close]}),
    }


_NM_NOW = pd.Timestamp("2026-07-16 19:59:00", tz="America/New_York")


def test_near_maturity_distance_safe_and_cross_asset_corroborated():
    """Motivating case (thesis.md §3a, 2026-07-16 20:00 ET): a fresh MNQ prev1_day_low sweep
    one minute from its 4h close, price already far (STRONG clearance) from the level, and
    MES's own copy of the SAME level already closed the same way (DOWN) — should be
    preconfirm_eligible."""
    bundle = derive_facts.FactsBundle()
    swept_ts = _NM_NOW - pd.Timedelta(minutes=34)
    bundle.levels = {
        "MNQ": {"prev1_day_low": (29079.0, 29079.0, "below", "day", None)},
        "MES": {"prev1_day_low": (7500.0, 7500.0, "below", "day", None)},
    }
    bundle.swept_at = {"MNQ": {"prev1_day_low": swept_ts}, "MES": {"prev1_day_low": swept_ts}}
    bundle.htf_close_status = {
        "MNQ": {"prev1_day_low": {"1h": None, "4h": None}},
        "MES": {"prev1_day_low": {"1h": {"close": 7480.0, "closed_at": str(_NM_NOW),
                                          "n_closed_since": 1, "beyond": True}, "4h": None}},
    }
    bundle.suppressed_p1_levels = {"MNQ": set(), "MES": set()}
    bundle.smt_candidates = []
    bundle.avg_range_1h = {"MNQ": 10.0, "MES": 10.0}
    bundle.avg_range_4h = {"MNQ": 10.0, "MES": 10.0}

    cands = derive_facts._near_maturity_candidates(bundle, _nm_data(29050.0, 7495.0), _NM_NOW)
    c4h = next(c for c in cands if c["asset"] == "MNQ" and c["tf"] == "4h")
    assert c4h["implied_direction"] == "DOWN"
    assert c4h["distance_safe"] is True
    assert c4h["corroborated"] is True
    assert c4h["preconfirm_eligible"] is True


def test_near_maturity_contradiction_blocks_corroboration():
    """A mature item on the SAME asset closing the opposite way (UP) must veto corroboration
    outright, even though the cross-asset agreement alone would otherwise qualify."""
    bundle = derive_facts.FactsBundle()
    swept_ts = _NM_NOW - pd.Timedelta(minutes=34)
    bundle.levels = {
        "MNQ": {"prev1_day_low": (29079.0, 29079.0, "below", "day", None),
                "prev2_day_high": (29500.0, 29500.0, "above", "day", None)},
        "MES": {"prev1_day_low": (7500.0, 7500.0, "below", "day", None)},
    }
    bundle.swept_at = {"MNQ": {"prev1_day_low": swept_ts, "prev2_day_high": swept_ts},
                       "MES": {"prev1_day_low": swept_ts}}
    bundle.htf_close_status = {
        "MNQ": {
            "prev1_day_low": {"1h": None, "4h": None},
            "prev2_day_high": {"1h": {"close": 29600.0, "closed_at": str(_NM_NOW),
                                       "n_closed_since": 1, "beyond": True}, "4h": None},
        },
        "MES": {"prev1_day_low": {"1h": {"close": 7480.0, "closed_at": str(_NM_NOW),
                                          "n_closed_since": 1, "beyond": True}, "4h": None}},
    }
    bundle.suppressed_p1_levels = {"MNQ": set(), "MES": set()}
    bundle.smt_candidates = []
    bundle.avg_range_1h = {"MNQ": 10.0, "MES": 10.0}
    bundle.avg_range_4h = {"MNQ": 10.0, "MES": 10.0}

    cands = derive_facts._near_maturity_candidates(bundle, _nm_data(29050.0, 7495.0), _NM_NOW)
    c4h = next(c for c in cands if c["asset"] == "MNQ" and c["level"] == "prev1_day_low"
              and c["tf"] == "4h")
    assert c4h["distance_safe"] is True
    assert c4h["corroborated"] is False
    assert c4h["preconfirm_eligible"] is False


def test_near_maturity_not_distance_safe():
    """Price still close to the level (below the STRONG-clearance bar) must not pre-confirm
    even with a clean corroborating source and no contradiction."""
    bundle = derive_facts.FactsBundle()
    swept_ts = _NM_NOW - pd.Timedelta(minutes=34)
    bundle.levels = {
        "MNQ": {"prev1_day_low": (29079.0, 29079.0, "below", "day", None)},
        "MES": {"prev1_day_low": (7500.0, 7500.0, "below", "day", None)},
    }
    bundle.swept_at = {"MNQ": {"prev1_day_low": swept_ts}, "MES": {"prev1_day_low": swept_ts}}
    bundle.htf_close_status = {
        "MNQ": {"prev1_day_low": {"1h": None, "4h": None}},
        "MES": {"prev1_day_low": {"1h": {"close": 7480.0, "closed_at": str(_NM_NOW),
                                          "n_closed_since": 1, "beyond": True}, "4h": None}},
    }
    bundle.suppressed_p1_levels = {"MNQ": set(), "MES": set()}
    bundle.smt_candidates = []
    bundle.avg_range_1h = {"MNQ": 10.0, "MES": 10.0}
    bundle.avg_range_4h = {"MNQ": 10.0, "MES": 10.0}

    cands = derive_facts._near_maturity_candidates(bundle, _nm_data(29075.0, 7495.0), _NM_NOW)
    c4h = next(c for c in cands if c["asset"] == "MNQ" and c["tf"] == "4h")
    assert c4h["distance_safe"] is False
    assert c4h["preconfirm_eligible"] is False


def test_near_maturity_outside_window_and_never_swept_excluded():
    bundle = derive_facts.FactsBundle()
    far_now = pd.Timestamp("2026-07-16 19:00:00", tz="America/New_York")   # 60m from 20:00 close
    swept_ts = far_now - pd.Timedelta(minutes=34)
    bundle.levels = {
        "MNQ": {"prev1_day_low": (29079.0, 29079.0, "below", "day", None),
                "prev2_day_low": (29200.0, 29200.0, "below", "day", None)},
        "MES": {},
    }
    bundle.swept_at = {"MNQ": {"prev1_day_low": swept_ts, "prev2_day_low": None}, "MES": {}}
    bundle.htf_close_status = {
        "MNQ": {"prev1_day_low": {"1h": None, "4h": None},
                "prev2_day_low": {"1h": None, "4h": None}},
        "MES": {},
    }
    bundle.suppressed_p1_levels = {"MNQ": set(), "MES": set()}
    bundle.smt_candidates = []
    bundle.avg_range_1h = {"MNQ": 10.0, "MES": 10.0}
    bundle.avg_range_4h = {"MNQ": 10.0, "MES": 10.0}

    cands = derive_facts._near_maturity_candidates(bundle, _nm_data(29050.0, 7495.0), far_now)
    assert cands == [], "60 minutes from the next close, and a never-swept level, must not appear"


def test_near_maturity_live_smt_corroborates_without_cross_asset():
    """A level that is itself a live (not suggested-exhausted), meaningful P2 SMT candidate
    corroborates on its own — P2's own cross-asset divergence requirement already supplies the
    second source, so no separate cross-asset/cross-tier agreement is needed."""
    bundle = derive_facts.FactsBundle()
    swept_ts = _NM_NOW - pd.Timedelta(minutes=34)
    bundle.levels = {
        "MNQ": {"prev1_day_low": (29079.0, 29079.0, "below", "day", None)},
        "MES": {},
    }
    bundle.swept_at = {"MNQ": {"prev1_day_low": swept_ts}, "MES": {}}
    bundle.htf_close_status = {
        "MNQ": {"prev1_day_low": {"1h": None, "4h": None}},
        "MES": {},
    }
    bundle.suppressed_p1_levels = {"MNQ": set(), "MES": set()}
    bundle.smt_candidates = [{
        "level": "prev1_day_low", "tier": "day", "side": "below",
        "swept_ticker": "MNQ", "unswept_ticker": "MES", "swept_at": swept_ts, "type": "wick",
        "meaningful": True, "suggested_exhausted": False,
    }]
    bundle.avg_range_1h = {"MNQ": 10.0, "MES": 10.0}
    bundle.avg_range_4h = {"MNQ": 10.0, "MES": 10.0}

    cands = derive_facts._near_maturity_candidates(bundle, _nm_data(29050.0, 7495.0), _NM_NOW)
    c4h = next(c for c in cands if c["asset"] == "MNQ" and c["tf"] == "4h")
    assert c4h["corroborated"] is True
    assert c4h["preconfirm_eligible"] is True


# --------------------------------------------------------------------------- #
# Most-extreme-swept-only P1 + running-extreme promotion + either-side mid     #
# freshness (2026-08-15, from the 2026-08-10 09:20 ET audit)                   #
# --------------------------------------------------------------------------- #
_TZ_NY = "America/New_York"


def test_extremity_shadowed_deeper_swept_low_suppresses_shallower():
    # 2026-08-10: MNQ's london(cur)_low (29799.5) and the deeper asia(cur)_low (29788.0)
    # both swept in the same decline -- only the deepest survives as fresh P1.
    t = pd.Timestamp("2026-08-10 07:12:00", tz=_TZ_NY)
    lv = {"asia(cur)_low": (29788.0, 29790.0, "below", "session", None),
          "london(cur)_low": (29799.5, 29801.0, "below", "session", None)}
    swept = {"asia(cur)_low": pd.Timestamp("2026-08-10 08:36:00", tz=_TZ_NY),
             "london(cur)_low": t}
    assert derive_facts._extremity_shadowed_levels(lv, swept) == {"london(cur)_low"}


def test_extremity_shadowed_unswept_deeper_level_suppresses_nothing():
    # STRICT rule applies among SWEPT levels only -- an unswept deeper level never shadows.
    t = pd.Timestamp("2026-08-10 07:12:00", tz=_TZ_NY)
    lv = {"prev1_day_low": (29455.0, 29460.0, "below", "day", None),
          "london(cur)_low": (29799.5, 29801.0, "below", "session", None)}
    swept = {"prev1_day_low": None, "london(cur)_low": t}
    assert derive_facts._extremity_shadowed_levels(lv, swept) == set()


def test_extremity_shadowed_sides_independent_and_cross_tier():
    # High side keeps the HIGHEST swept high regardless of tier; low side independent.
    t = pd.Timestamp("2026-08-10 07:12:00", tz=_TZ_NY)
    lv = {"prev1_day_high": (30000.0, 29990.0, "above", "day", None),
          "asia(cur)_high": (30010.0, 30005.0, "above", "session", None),
          "asia(cur)_low": (29788.0, 29790.0, "below", "session", None)}
    swept = {"prev1_day_high": t, "asia(cur)_high": t, "asia(cur)_low": t}
    assert derive_facts._extremity_shadowed_levels(lv, swept) == {"prev1_day_high"}


def test_extremity_shadowed_equal_price_tie_keeps_higher_tier():
    # Exact duplicate (the §2.1d shape): identical price under two names -- the higher-tier
    # representative survives.
    t = pd.Timestamp("2026-08-10 07:12:00", tz=_TZ_NY)
    lv = {"prev1_day_low": (29455.0, 29460.0, "below", "day", None),
          "ny_evening(prev1)_low": (29455.0, 29460.0, "below", "session", None)}
    swept = {"prev1_day_low": t, "ny_evening(prev1)_low": t}
    assert derive_facts._extremity_shadowed_levels(lv, swept) == {"ny_evening(prev1)_low"}


def _promo_frame(rows):
    # rows: [(ts_str, low, high)] -> minimal 1-row-per-bar OHLC frame
    idx = pd.DatetimeIndex([pd.Timestamp(ts, tz=_TZ_NY) for ts, _lo, _hi in rows])
    lows = [lo for _ts, lo, _hi in rows]
    highs = [hi for _ts, _lo, hi in rows]
    return pd.DataFrame({"open": highs, "high": highs, "low": lows, "close": highs}, index=idx)


def test_promoted_session_extreme_day_tier_when_running_day_low():
    # asia(cur)_low was the running day low when swept (nothing lower before the sweep) ->
    # promoted to day tier; NOT week (an earlier week-window bar sits lower).
    now = pd.Timestamp("2026-08-10 09:20:00", tz=_TZ_NY)
    wk_anchor = pd.Timestamp("2026-08-06 18:00:00", tz=_TZ_NY)
    swept_t = pd.Timestamp("2026-08-10 08:36:00", tz=_TZ_NY)
    df = _promo_frame([
        ("2026-08-07 10:00:00", 29700.0, 29900.0),   # week window only: deeper low exists
        ("2026-08-09 20:00:00", 29788.0, 29900.0),   # asia: the day's low so far
        ("2026-08-10 07:00:00", 29800.0, 29850.0),
        ("2026-08-10 08:36:00", 29770.0, 29800.0),   # the sweep bar itself (excluded)
    ])
    levels = {"MNQ": {"asia(cur)_low": (29788.0, 29790.0, "below", "session", None)},
              "MES": {"asia(cur)_low": (29788.0, 29790.0, "below", "session", None)}}
    swept = {"MNQ": {"asia(cur)_low": swept_t}, "MES": {"asia(cur)_low": None}}
    out = derive_facts._promoted_session_extremes(
        {"MNQ": df, "MES": df.iloc[:-1]}, levels, swept, now, wk_anchor)
    assert out["MNQ"].get("asia(cur)_low") == "day"
    assert out["MES"].get("asia(cur)_low") == "day"   # unswept: judged at `now`


def test_promoted_session_extreme_not_promoted_when_deeper_low_precedes():
    # A pre-existing deeper low inside the day window means the session low was never the
    # running day extreme -- no promotion.
    now = pd.Timestamp("2026-08-10 09:20:00", tz=_TZ_NY)
    wk_anchor = pd.Timestamp("2026-08-06 18:00:00", tz=_TZ_NY)
    df = _promo_frame([
        ("2026-08-09 19:00:00", 29700.0, 29900.0),   # deeper low INSIDE the day window
        ("2026-08-09 21:00:00", 29788.0, 29880.0),
    ])
    levels = {"MNQ": {"asia(cur)_low": (29788.0, 29790.0, "below", "session", None)},
              "MES": {}}
    swept = {"MNQ": {"asia(cur)_low": None}, "MES": {}}
    out = derive_facts._promoted_session_extremes(
        {"MNQ": df, "MES": df}, levels, swept, now, wk_anchor)
    assert out["MNQ"] == {}


def test_promoted_session_extreme_week_tier_wins_over_day():
    # A session extreme that is ALSO the running week extreme promotes straight to week.
    now = pd.Timestamp("2026-08-10 09:20:00", tz=_TZ_NY)
    wk_anchor = pd.Timestamp("2026-08-06 18:00:00", tz=_TZ_NY)
    df = _promo_frame([
        ("2026-08-07 10:00:00", 29900.0, 30000.0),
        ("2026-08-09 20:00:00", 29788.0, 29950.0),   # the week's own low
    ])
    levels = {"MNQ": {"asia(cur)_low": (29788.0, 29790.0, "below", "session", None)},
              "MES": {}}
    swept = {"MNQ": {"asia(cur)_low": None}, "MES": {}}
    out = derive_facts._promoted_session_extremes(
        {"MNQ": df, "MES": df}, levels, swept, now, wk_anchor)
    assert out["MNQ"].get("asia(cur)_low") == "week"


def test_mid_tf_state_opposite_side_extreme_after_crossing_unfreshens():
    # 2026-08-15 (#2): a new running extreme on EITHER side after the crossing voids P4
    # freshness -- previously only the tested side's extreme was compared, so a wick-only
    # opposite-side extreme (no completed-bar re-crossing) left a dead reclaim "fresh".
    tz = _TZ_NY
    idx = pd.DatetimeIndex([pd.Timestamp(f"2026-08-10 {h:02d}:00:30", tz=tz)
                            for h in (0, 1, 2, 3, 4)])
    closes = [105.0, 106.0, 95.0, 94.0, 93.0]        # down-cross of mid=100 in the 02:00 bar
    df = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes},
                      index=idx)
    now = pd.Timestamp("2026-08-10 04:30:00", tz=tz)
    mid = 100.0
    crossing_close = pd.Timestamp("2026-08-10 03:00:00", tz=tz)   # 02:00 bar's close time
    lo_before = pd.Timestamp("2026-08-10 01:30:00", tz=tz)
    hi_after = pd.Timestamp("2026-08-10 03:45:00", tz=tz)         # wick high AFTER crossing
    _status, reclaim = derive_facts._mid_tf_state(df, mid, "1h", now, hi_after, lo_before)
    assert reclaim is not None and reclaim["cross_dir"] == "down"
    assert reclaim["fresh"] is False   # either-side rule: hi_after postdates the crossing

    _status2, reclaim2 = derive_facts._mid_tf_state(df, mid, "1h", now, lo_before, lo_before)
    assert reclaim2["fresh"] is True   # both extremes predate the crossing -> still fresh
