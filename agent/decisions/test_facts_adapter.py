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


def test_empty_mes_frame_handled():
    frames = load_live_frames()
    frames["mes_today"] = frames["mes_today"].iloc[0:0]   # empty MES (session open)
    snap = build_snapshot(frames)                          # must not raise
    assert snap.degraded is True
    assert snap.error == "insufficient-data"
