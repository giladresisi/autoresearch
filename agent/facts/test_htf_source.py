"""Plan 40: the disk side of the HTF extremes (`agent/facts/htf_source.py`).

Every test runs against a tmp `ACT_GLOBAL_DIR`; nothing here reads the real global dir.
"""
import json

import numpy as np
import pandas as pd
import pytest

from agent.facts import htf_source
from agent.facts.htf_extremes import TZ, session_as_of

DATE = "2026-07-22"            # a Wednesday; as_of Tue Jul 21 18:00


def _frame(end, seed=0, start="2026-06-08 18:00"):
    idx = pd.date_range(start, end, freq="15min", tz=TZ)
    t = idx.time
    import datetime
    idx = idx[[(x <= datetime.time(16, 55)) or (x >= datetime.time(18, 0)) for x in t]]
    idx = idx[idx.dayofweek < 5]
    rng = np.random.default_rng(seed)
    px = 20000 + np.cumsum(rng.normal(0, 5, len(idx)))
    return pd.DataFrame({"Open": px, "High": px + 3, "Low": px - 3, "Close": px,
                         "Volume": 1.0}, index=idx)


@pytest.fixture
def gdir(tmp_path, monkeypatch):
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    return tmp_path


def _write(dirpath, end, seed=0):
    dirpath.mkdir(parents=True, exist_ok=True)
    for i, tk in enumerate(("MNQ", "MES")):
        _frame(end, seed=seed + i).to_parquet(dirpath / f"{tk}_1m.parquet")


def test_replay_routes_through_the_rollover_ledger(gdir):
    main = gdir / "general" / "main"
    _write(main / "2026-09", "2026-07-21 16:59", seed=1)        # the routed era
    _write(main, "2026-07-21 16:59", seed=99)                    # the flat fallback
    (main / "rollover_ledger.json").write_text(json.dumps(
        [{"prep_date": "2026-06-13", "subfolder": "2026-09"},
         {"prep_date": "0001-01-01", "subfolder": "2026-06"}]), encoding="utf-8")
    ext = htf_source.load_session_extremes(DATE, source="replay")
    path = ext["meta"]["tickers"]["MNQ"]["path"]
    assert "2026-09" in path and path.endswith("MNQ_1m.parquet")
    assert ext["meta"]["kind"] == "replay"
    assert ext["meta"]["as_of"] == session_as_of(DATE).isoformat()
    assert ext["MNQ"] and ext["MES"]


def test_live_reads_general_live_dir(gdir):
    _write(gdir / "general" / "live", "2026-07-22 09:00", seed=3)   # past the open too
    ext = htf_source.load_session_extremes(DATE, source="live")
    assert ext["meta"]["tickers"]["MNQ"]["path"] == str(
        gdir / "general" / "live" / "MNQ_1m.parquet")
    # nothing at or after the open is read
    assert pd.Timestamp(ext["meta"]["tickers"]["MNQ"]["last_ts"]) < session_as_of(DATE)
    with pytest.raises(ValueError):
        htf_source.load_session_extremes(DATE, source="bogus")


def test_stale_source_raises(gdir):
    _write(gdir / "general" / "live", "2026-07-17 16:59")          # last Friday
    with pytest.raises(htf_source.HtfSourceStale):
        htf_source.load_session_extremes(DATE, source="live")
    # a Monday session: the prior close is FRIDAY 17:00, so Friday's data is fresh
    ext = htf_source.load_session_extremes("2026-07-20", source="live")
    assert ext["MNQ"]
    # an early close (13:15) is inside the tolerance
    _write(gdir / "general" / "live", "2026-07-21 13:15")
    assert htf_source.load_session_extremes(DATE, source="live")["MNQ"]


def test_missing_file_raises(gdir):
    with pytest.raises(Exception):
        htf_source.load_session_extremes(DATE, source="live")


def test_artifact_is_deterministic_and_carries_fingerprint(gdir, tmp_path):
    _write(gdir / "general" / "live", "2026-07-21 16:59")
    a = htf_source.load_session_extremes(DATE, source="live")
    b = htf_source.load_session_extremes(DATE, source="live")
    assert a["meta"]["sha"] == b["meta"]["sha"] == htf_source.fingerprint(a)
    p1 = htf_source.write_artifact(tmp_path / "r1", a)
    p2 = htf_source.write_artifact(tmp_path / "r2", b)
    assert open(p1, "rb").read() == open(p2, "rb").read()
    back = htf_source.read_artifact(tmp_path / "r1")
    assert back["MNQ"] == a["MNQ"] and back["MES"] == a["MES"]
    assert back["seed"] == a["seed"]
    assert htf_source.fingerprint(back) == a["meta"]["sha"]
    ctx = htf_source.executor_context(back, "MNQ")
    assert ctx["as_of"] == session_as_of(DATE) and ctx["extremes"] == a["MNQ"]


def test_error_artifact_is_not_read_back_as_a_list(tmp_path):
    htf_source.write_error_artifact(tmp_path, DATE, "replay", "HtfSourceStale: x")
    raw = json.loads((tmp_path / htf_source.ARTIFACT).read_text(encoding="utf-8"))
    assert raw["meta"]["error"] == "HtfSourceStale: x"
    assert htf_source.read_artifact(tmp_path) is None


def test_a_full_cme_closure_is_not_a_stale_file():
    """Mon 2026-12-28: Fri 12-25 has no session, so the prior close is Thu 12-24."""
    import datetime
    as_of = session_as_of("2026-12-28")
    assert htf_source.prior_session_close(as_of) == pd.Timestamp("2026-12-24 17:00", tz=TZ)
    assert (htf_source.prior_session_close(session_as_of("2027-01-04"))
            == pd.Timestamp("2026-12-31 17:00", tz=TZ))
    assert (htf_source.prior_session_close(session_as_of("2027-03-29"))
            == pd.Timestamp("2027-03-25 17:00", tz=TZ))
    assert datetime.date(2026, 12, 25).weekday() == 4
