"""The reconstruction is pinned against the ONE archived snapshot that exists.

`<global>/sessions/2026-08-25/daily.json` is the only session archive predating the
retention change, and it is the only external check available. All three readings below
are asserted rather than one: an exact match at the archive's own boundary proves the
assembly, and the two earlier boundaries prove the TIME-DEPENDENCE is real rather than a
frozen answer that happens to agree once.
"""
import json
import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.study import legacy_liquidities as ll                   # noqa: E402

TZ = "America/New_York"
SNAPSHOT_DATE = "2026-08-25"
#: The boundary the archive was taken at, established by search rather than assumed: it
#: is the only minute at which the reconstructed NAME SET and every value agree. Before
#: 12:00 `ny_evening` has not opened; from 12:01 it has, and the archive lacks it.
SNAPSHOT_BOUNDARY = "11:59"

pytestmark = pytest.mark.timeout(300)


def _snapshot():
    import paths
    path = os.path.join(str(paths.global_root()), "sessions", SNAPSHOT_DATE,
                        "daily.json")
    if not os.path.exists(path):
        pytest.skip("no archived session state for %s" % SNAPSHOT_DATE)
    with open(path, encoding="utf-8") as fh:
        return {l["name"]: l for l in json.load(fh).get("liquidities", [])}


@pytest.fixture(scope="module")
def bars():
    try:
        return ll.load_1m("MNQ")
    except (FileNotFoundError, OSError):                    # pragma: no cover
        pytest.skip("1m parquet unavailable")


def _recon(bars, hhmm):
    now = pd.Timestamp(f"{SNAPSHOT_DATE} {hhmm}", tz=TZ)
    return {l["name"]: l for l in ll.reconstruct(bars, now)}


def test_it_reproduces_the_archived_snapshot_exactly_at_its_own_boundary(bars):
    snap = _snapshot()
    rec = _recon(bars, SNAPSHOT_BOUNDARY)

    assert set(rec) == set(snap), (set(rec) ^ set(snap))
    for name, want in snap.items():
        got = rec[name]
        assert got["kind"] == want["kind"], name
        if want["kind"] == "fvg":
            assert got["top"] == pytest.approx(want["top"]), name
            assert got["bottom"] == pytest.approx(want["bottom"]), name
        else:
            assert got["price"] == pytest.approx(want["price"]), name


def test_the_pruned_fvg_is_the_one_that_was_still_live_earlier(bars):
    """The FVG set is the component most at risk — it is the only membership that depends
    on the visited-pruning pass rather than on an extreme."""
    snap = _snapshot()
    assert "fvg_20260818_0100_bear" in snap
    assert snap["fvg_20260818_0100_bear"]["top"] == pytest.approx(30062.0)
    assert snap["fvg_20260818_0100_bear"]["bottom"] == pytest.approx(29919.25)

    early = _recon(bars, "09:21")
    assert "fvg_20260818_0100_bear" in early
    # ... and one MORE gap was still unvisited at 09:21, pruned by the archive boundary.
    assert "fvg_20260825_0100_bull" in early
    assert "fvg_20260825_0100_bull" not in snap


def test_the_session_extremes_are_still_forming_at_an_earlier_boundary(bars):
    """A running extreme that already equalled its final value at 09:21 would mean the
    reconstruction is reading the whole day, i.e. lookahead."""
    snap = _snapshot()
    early = _recon(bars, "09:21")
    assert early["ny_morning_high"]["price"] != pytest.approx(
        snap["ny_morning_high"]["price"])
    assert early["ny_morning_low"]["price"] != pytest.approx(
        snap["ny_morning_low"]["price"])


def test_ny_evening_appears_only_once_its_window_has_opened(bars):
    assert "ny_evening_high" not in _recon(bars, "11:59")
    assert "ny_evening_high" in _recon(bars, "12:01")


def test_nothing_after_the_boundary_is_read(bars):
    """Totality of the no-lookahead claim: the same boundary on a truncated frame gives
    the same answer."""
    now = pd.Timestamp(f"{SNAPSHOT_DATE} 09:21", tz=TZ)
    full = {l["name"]: l for l in ll.reconstruct(bars, now)}
    cut = {l["name"]: l for l in ll.reconstruct(bars[bars.index < now], now)}
    assert full == cut


def test_the_sep2_initial_target_matches_the_plans_own_derivation(bars):
    """Plan 33 §3 records `cautious_price_initial = 29147.25`, level `ny_morning_high`,
    at the 2026-09-02 fill of 29097.00 UP — derived independently of this module."""
    from hypothesis import compute_cautious_prices

    now = pd.Timestamp("2026-09-02 10:07", tz=TZ)
    liq = ll.reconstruct(bars, now)
    cp = compute_cautious_prices("up", 29097.0, liq, ll.historical_ath(bars, now), 0,
                                 invalidated_names=None, now=now)
    assert cp["cautious_price_initial"] == pytest.approx(29147.25)
    assert cp["cautious_price_initial_level"] == "ny_morning_high"


def test_it_writes_no_legacy_state(bars, tmp_path, monkeypatch):
    """READ-ONLY is the module's own claim. `save_daily` and friends are already refused
    by the AST gate; this catches an indirect write through a production builder."""
    import smt_state

    for name in ("save_daily", "save_position", "save_hypothesis", "save_smts",
                 "save_global"):
        if hasattr(smt_state, name):
            monkeypatch.setattr(smt_state, name, _boom(name))
    ll.reconstruct(bars, pd.Timestamp(f"{SNAPSHOT_DATE} 09:21", tz=TZ))


def _boom(name):
    def _f(*_a, **_kw):
        raise AssertionError(f"{name}() called from a read-only reconstruction")
    return _f
