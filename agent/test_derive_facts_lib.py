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
