"""Plan 40 on real data (`-m slow`): the 09-21 list, the 09-21 oracle-UP replay, and
live/replay source parity. Figures come from `agent/trader/named_cases.py`."""
import glob
import json
import os

import pandas as pd
import pytest

import paths
from agent.facts import htf_source
from agent.facts.htf_extremes import (compute_unnested_extremes, running_period_seed,
                                      session_as_of)
from agent.trader import named_cases as nc

pytestmark = [pytest.mark.slow, pytest.mark.timeout(1800)]

DATE = "2026-09-21"


def _as_map(rows):
    return {r.name: (r.price, tuple(r.aliases)) for r in rows}


@pytest.fixture(scope="module")
def replay_ext():
    return htf_source.load_session_extremes(DATE, source="replay")


def test_0921_list_as_of_session_open(replay_ext):
    assert replay_ext["meta"]["as_of"] == nc.HTF_0921_AS_OF
    assert "2026-12" in replay_ext["meta"]["tickers"]["MNQ"]["path"]
    assert _as_map(replay_ext["MNQ"]) == nc.HTF_0921_MNQ
    assert _as_map(replay_ext["MES"]) == nc.HTF_0921_MES
    highs = [r for r in replay_ext["MNQ"] if r.side == "above"]
    top = max(highs, key=lambda r: r.price)
    assert top.is_window_extreme and "htf_week_high_20260616" in top.aliases
    lows = [r for r in replay_ext["MNQ"] if r.side == "below"]
    assert min(lows, key=lambda r: r.price).price == 27499.75


def test_named_0921_up_fills_target_the_aug17_high(monkeypatch, tmp_path):
    """Flag ON, Q1 = P1: both fills pick the Aug 17 high (the August month row)."""
    from agent.trader import target
    from agent.trader.replay import run_replay
    monkeypatch.setattr(target, "HTF_EXTREMES_IN_T2", True)
    monkeypatch.setenv("ACT_REGRESSION_DIR", str(tmp_path))
    res = run_replay([DATE], thesis=nc.ORACLE_0921_UP)[DATE]
    recs = [json.loads(l) for l in open(os.path.join(res["run_dir"],
                                                     "trader_decisions.jsonl"),
                                        encoding="utf-8") if l.strip()]
    fills = [(r["time"], r["price"]) for r in recs if r["kind"] == "fill"]
    assert tuple(fills) == nc.HTF_0921_UP_FILLS
    picks = [r for r in recs if r["kind"] == "target_selected"]
    assert [(p["level"], p["target"]) for p in picks] == [nc.HTF_0921_UP_T2] * 2
    inits = [r for r in recs if r["kind"] == "initial_target_selected"]
    assert [i["level"] for i in inits] == ["synthetic_85pct"] * 2
    assert [i["price"] for i in inits] == pytest.approx(list(nc.HTF_0921_UP_INITIAL))
    assert os.path.exists(os.path.join(res["run_dir"], htf_source.ARTIFACT))


def test_replay_extremes_equal_the_live_artifact(replay_ext):
    """A live session's artifact vs the replay's recomputation of the same date."""
    found = sorted(glob.glob(str(paths.sessions_dir() / "*" / htf_source.ARTIFACT)))
    if not found:
        pytest.skip("no live session has written htf_extremes.json yet")
    checked = 0
    for p in found:
        live = htf_source.read_artifact(os.path.dirname(p))
        if live is None:
            continue
        d = live["meta"]["trade_date"]
        rep = replay_ext if d == DATE else htf_source.load_session_extremes(
            d, source="replay")
        assert htf_source.fingerprint(live) == htf_source.fingerprint(rep), d
        checked += 1
    if not checked:
        pytest.skip("only error artifacts on disk")


def test_live_1m_and_main_era_1m_agree_before_as_of(replay_ext):
    """Same file family on both paths: the live 1m and the main-era 1m give the same
    list and seed as of the 09-21 open (source drift after a parquet-check repair would
    show here)."""
    live_path = str(paths.general_live_dir() / "MNQ_1m.parquet")
    if not os.path.exists(live_path):
        pytest.skip("no live 1m parquet on this machine")
    as_of = session_as_of(DATE)
    for tk in ("MNQ", "MES"):
        df = pd.read_parquet(str(paths.general_live_dir() / f"{tk}_1m.parquet"))
        df = df[df.index < as_of]
        assert compute_unnested_extremes(df, as_of, ticker=tk) == replay_ext[tk], tk
        assert running_period_seed(df, as_of, ticker=tk) == replay_ext["seed"][tk], tk
