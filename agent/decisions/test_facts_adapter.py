"""Online facts adapter tests (Wave 2.1)."""

import os

import pandas as pd
import pytest

from conftest import ATH_MES, ATH_MNQ, FIX, fixtures_present, load_live_frames
from derive_facts import compute_facts, load, render_facts_text
from facts_adapter import build_snapshot

pytestmark = pytest.mark.skipif(not fixtures_present(), reason="golden fixtures absent")


def test_frame_vs_slice_parity():
    """The core correctness claim: a snapshot built from live frames is byte-for-byte
    the fact sheet the offline slice path produces for the same bar."""
    frames = load_live_frames()
    snap = build_snapshot(frames)
    mnq = load(os.path.join(FIX, "MNQ_1s_slice.parquet"))
    mes = load(os.path.join(FIX, "MES_1s_slice.parquet"))
    bundle = compute_facts(mnq, mes, ath_mnq=ATH_MNQ, ath_mes=ATH_MES)
    assert snap.text == render_facts_text(bundle)
    assert not snap.degraded


def test_checkpoint_truncation_no_lookahead():
    frames = load_live_frames()                       # now = 09:27:59
    ckpt = pd.Timestamp("2026-05-19 09:20:00", tz="America/New_York")
    snap = build_snapshot(frames, checkpoint=ckpt)
    assert snap.max_ts <= ckpt                        # no bar after the checkpoint
    assert f"now = {ckpt}" in snap.text               # facts evaluated AT the checkpoint


def test_hash_stability_and_drift():
    frames = load_live_frames()
    h1 = build_snapshot(frames).content_hash
    h2 = build_snapshot(load_live_frames()).content_hash
    assert h1 == h2                                   # identical frames → identical hash

    # A one-bar change (moving the last close) changes now_price → the hash changes.
    changed = load_live_frames()
    mnq = changed["mnq_today"].copy()
    mnq.iloc[-1, mnq.columns.get_loc("Close")] += 25.0
    changed["mnq_today"] = mnq
    assert build_snapshot(changed).content_hash != h1


def test_history_supplies_prior_day_levels():
    """The online-thinness regression (2026-06-25 finding): a SESSION-ONLY today frame
    plus the 1m history must yield the same prev-day/week level universe as the offline
    slice — a session-only primary df silently drops every prior-day level."""
    from derive_facts import session_frame, trade_date

    frames = load_live_frames()
    now = frames["now"]

    # Split the golden slice the way the pipeline sees it: today = session bars only,
    # hist = everything before the session, resampled to 1m (the rolling-history shape).
    for tkr in ("mnq", "mes"):
        full = frames[f"{tkr}_today"]
        sess = session_frame(full.rename(columns=str.lower), trade_date(now))
        today = full.loc[sess.index[0]:]
        hist_1s = full.loc[:sess.index[0] - pd.Timedelta(seconds=1)]
        hist_1m = hist_1s.resample("1min").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
        frames[f"{tkr}_today"] = today
        frames[f"hist_{tkr}"] = hist_1m

    snap = build_snapshot(frames)
    assert not snap.degraded
    # The golden fixture spans ~1.5 days, so exactly ONE prior day is reachable;
    # prev2_day/prev1_week need the pipeline's real 60-day history (validated E2E).
    assert "prev1_day" in snap.text, "prev1_day missing from history-fed snapshot"
    assert "asia(prev1)" in snap.text, "prev-day sub-session levels missing"
    # And the failure mode stays detectable: without hist, prev-day levels vanish.
    bare = load_live_frames()
    bare["mnq_today"] = frames["mnq_today"]
    bare["mes_today"] = frames["mes_today"]
    thin = build_snapshot(bare)
    assert "prev1_day" not in thin.text


def test_empty_mes_frame_handled():
    frames = load_live_frames()
    frames["mes_today"] = frames["mes_today"].iloc[0:0]   # empty MES (session open)
    snap = build_snapshot(frames)                          # must not raise
    assert snap.degraded is True
    assert snap.error == "insufficient-data"
