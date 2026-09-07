"""Behavioural tests for the offline target-selection harness.

Every test here runs on the 1m source: it is the same `bundle_for_boundary` rule at ~8x
the speed, and none of these behaviours is resolution-specific. The Aug-24 report itself
runs at 1s -- that is a reporting choice, not a correctness one.
"""
import copy
import inspect

import pandas as pd
import pytest

from agent.study import target_offline as tgt
from agent.study.hazard import PoolObs, predict_stop_rank
from derive_facts import (DOL_MIN_DRAW_DISTANCE_PTS, DOL_MIN_DRAW_RATIO, build_menus,
                          facts_to_validator_dict)

TZ = "America/New_York"
DATE = "2026-08-24"
ENTRY = "09:30:00"
DIRECTION = "DOWN"


@pytest.fixture(scope="module")
def facts():
    return tgt.OfflineFacts(source="1m")


@pytest.fixture(scope="module")
def corpus():
    return tgt.load_corpus(tgt.CORPUS_DIR)


@pytest.fixture(scope="module")
def tracks(facts, corpus):
    labels, cands = corpus
    return tgt.build_tracks(facts, DATE, ENTRY, DIRECTION, "MNQ",
                            labels=labels, cands=cands)


# --- the menu is production's own, not a second copy of the rule ------------- #

def test_the_entry_menu_is_what_build_menus_itself_returns(facts, tracks):
    """T2 must be `build_menus` on the bundle at the instant -- not a re-derivation."""
    ts = tgt.instant_ts(DATE, ENTRY)
    bundle = facts.bundle_at(ts)
    expected = build_menus(bundle, facts_to_validator_dict(bundle))["dol"][DIRECTION]
    t2 = next(t for t in tracks if t.key == "T2")
    assert list(t2.rows) == expected


def test_the_0920_menu_is_built_at_the_l1_boundary_not_at_the_entry(tracks):
    t1 = next(t for t in tracks if t.key == "T1")
    t2 = next(t for t in tracks if t.key == "T2")
    assert t1.boundary == tgt.instant_ts(DATE, tgt.L1_BOUNDARY_HHMM)
    assert t2.boundary == tgt.instant_ts(DATE, ENTRY)
    assert t1.boundary < t2.boundary


def test_now_price_is_the_last_close_strictly_before_the_instant(facts, tracks):
    bars = facts.bars("MNQ")
    for track in tracks:
        if track.now_price is None:
            continue
        assert track.now_ts < track.boundary
        head = bars[bars.index < track.boundary]
        assert track.now_ts == head.index[-1]
        assert track.now_price == pytest.approx(float(head["close"].iloc[-1]))


def test_a_pool_nearer_than_the_draw_floor_is_in_no_track_menu(tracks):
    """max(5pts, 1.0x avg_1h) -- production's proximity guard, inherited not re-applied."""
    for track in tracks:
        if track.avg_range_1h is None:
            continue
        floor = max(DOL_MIN_DRAW_DISTANCE_PTS, DOL_MIN_DRAW_RATIO * track.avg_range_1h)
        for row in track.rows:
            assert abs(row["price"] - track.now_price) >= floor, (track.key, row)


# --- T3 rides on T2's universe ---------------------------------------------- #

def test_b8g_ranks_only_pools_the_entry_menu_contains(tracks):
    """The recorded MUST-FIX: cycle 4 scored a universe production never offers."""
    t2 = next(t for t in tracks if t.key == "T2")
    t3 = next(t for t in tracks if t.key == "T3")
    assert list(t3.rows) == list(t2.rows)
    if t3.pick is not None:
        assert t3.pick in t2.rows


def test_the_b8g_prediction_path_never_reads_a_stop_field(tracks, corpus):
    """`stop` is a training field. Flipping it on every constructed observation must
    leave the prediction untouched."""
    labels, cands = corpus
    t2 = next(t for t in tracks if t.key == "T2")
    fit = tgt.fit_b8g(labels, cands, exclude_date=DATE)
    obs = tgt.pool_observations_from_menu(t2.rows)
    assert obs, "the entry menu must yield observations for this test to mean anything"
    assert all(o.stop is False for o in obs)

    honest = predict_stop_rank(obs, fit["hazard"], fit["pooled"], "B8g")
    poisoned = [PoolObs(**{**o.__dict__, "stop": True}) for o in obs]
    assert predict_stop_rank(poisoned, fit["hazard"], fit["pooled"], "B8g") == honest


def test_a_date_in_the_label_corpus_is_excluded_from_the_fit_and_the_output_says_so(corpus):
    labels, cands = corpus
    assert any(r["date"] == DATE for r in labels), "fixture assumes 08-24 is in the corpus"
    full = tgt.fit_b8g(labels, cands, exclude_date=None)
    held = tgt.fit_b8g(labels, cands, exclude_date=DATE)
    assert held["excluded"] is True
    assert DATE in held["note"]
    assert held["n_sessions"] == full["n_sessions"] - 1
    assert all(o_date != DATE for (o_date, _seg) in held["sessions"])


def test_a_date_absent_from_the_corpus_excludes_nothing(corpus):
    labels, cands = corpus
    fit = tgt.fit_b8g(labels, cands, exclude_date="2024-01-02")
    assert fit["excluded"] is False
    assert fit["n_sessions"] == tgt.fit_b8g(labels, cands)["n_sessions"]


# --- scope ------------------------------------------------------------------- #

def test_mes_raises_rather_than_returning_a_menu(facts, corpus):
    labels, cands = corpus
    with pytest.raises(NotImplementedError) as exc:
        tgt.build_tracks(facts, DATE, ENTRY, DIRECTION, "MES",
                         labels=labels, cands=cands)
    assert "1.3" in str(exc.value)


# --- lookahead is quarantined ------------------------------------------------ #

@pytest.mark.timeout(600)
def test_no_track_can_see_past_the_entry_instant(facts, corpus, tracks):
    """Truncate every bar at or after the instant and rebuild: identical tracks."""
    labels, cands = corpus
    ts = tgt.instant_ts(DATE, ENTRY)
    blind = copy.copy(facts)
    blind._cache = {}
    blind._norm = {tk: df[df.index < ts] for tk, df in facts._norm.items()}
    blind._raw = {tk: df[df.index < ts] for tk, df in facts._raw.items()}

    rebuilt = tgt.build_tracks(blind, DATE, ENTRY, DIRECTION, "MNQ",
                               labels=labels, cands=cands)
    assert [t.to_dict() for t in rebuilt] == [t.to_dict() for t in tracks]


def test_the_report_carries_the_lookahead_block_and_marks_it(facts, corpus):
    labels, cands = corpus
    report = tgt.build_report(facts, DATE, ENTRY, DIRECTION, "MNQ",
                              labels=labels, cands=cands)
    look = report["lookahead"]
    assert "LOOKAHEAD" in look["warning"]
    assert look["entries"] and "draw" in look and "verdicts" in look
    for track in report["tracks"]:
        assert not (set(track) & set(look)), track["key"]
        assert "lookahead" not in track


def test_the_evaluation_names_the_draw_by_labellings_own_rule(facts, corpus):
    """Not a second draw rule: the reached-last / near-miss-in-band rule from
    `agent.study.labelling`, applied to the menu's own entries."""
    labels, cands = corpus
    report = tgt.build_report(facts, DATE, ENTRY, DIRECTION, "MNQ",
                              labels=labels, cands=cands)
    draw = report["lookahead"]["draw"]
    assert draw["status"] in ("labelled", "unexplained")
    if draw["status"] == "labelled":
        assert draw["via"] in ("reached_last", "near_miss")
        assert draw["price"] in [r["price"] for r in report["tracks"][1]["rows"]]


def test_every_verdict_is_one_of_the_four_declared_outcomes(facts, corpus):
    labels, cands = corpus
    report = tgt.build_report(facts, DATE, ENTRY, DIRECTION, "MNQ",
                              labels=labels, cands=cands)
    for key, v in report["lookahead"]["verdicts"].items():
        assert v["verdict"] in tgt.VERDICTS, (key, v)


# --- recorded thesis is optional -------------------------------------------- #

def test_a_date_with_no_recording_degrades_to_none(facts):
    assert tgt.recorded_theses("2024-01-02") == []


def test_the_manual_run_is_labelled_as_manual_at_its_own_boundary():
    found = tgt.recorded_theses(DATE)
    assert found, "the 08-24 manual run is committed under manual-l1-thesis/"
    rec = found[0]
    assert rec["kind"] == "manual"
    assert rec["boundary"].startswith("2026-08-24 08:42")
    assert rec["dol"]["level"] == "london(cur)_low"


# --- hard constraints -------------------------------------------------------- #

def test_the_module_never_reads_a_wall_clock():
    src = inspect.getsource(tgt)
    assert "datetime.now" not in src
    assert "get_et_now" not in src
    assert "live_orders" not in src


def test_the_source_used_is_reported(facts, corpus):
    labels, cands = corpus
    report = tgt.build_report(facts, DATE, ENTRY, DIRECTION, "MNQ",
                              labels=labels, cands=cands)
    assert report["request"]["source"] == "1m"
    assert report["request"]["date"] == DATE


def test_the_source_selects_the_matching_parquet():
    for res in ("1s", "1m"):
        assert tgt.OfflineFacts.__mro__[1].source_path is not None
    paths = [tgt.OfflineFacts.source_path(_Stub(res), "MNQ") for res in ("1s", "1m")]
    assert paths[0].endswith("MNQ_1s.parquet")
    assert paths[1].endswith("MNQ_1m.parquet")


class _Stub:
    def __init__(self, source):
        self.source = source
        self.main_dir = "/tmp/main"


# --- scoring a pick that is not in T2's menu under the same id --------------- #

class _StubFacts:
    """Just what `evaluate` reads: a bar frame and the resolution's name."""
    source = "1m"

    def __init__(self, frame):
        self._frame = frame

    def bars(self, _ticker):
        return self._frame


def _row(rid, level, price):
    return {"id": rid, "level": level, "price": price, "band": "BAND",
            "dist_ratio": 1.0, "tier": "day", "side": "below", "body": None}


def _bars(date, lows):
    idx = pd.date_range(f"{date} 09:30", periods=len(lows), freq="1min", tz=TZ)
    return pd.DataFrame({"open": [l + 5 for l in lows], "high": [l + 5 for l in lows],
                         "low": lows, "close": lows}, index=idx)


def test_a_pick_is_scored_reached_by_its_price_not_by_its_menu_id():
    """T1's menu is built at a different boundary, so its `D1` can name a different
    level than T2's `D1`. Scoring by id would report the wrong pool's outcome."""
    t2_rows = (_row("D1", "near", 100.0), _row("D2", "far", 90.0))
    t1_pick = _row("D1", "far", 90.0)            # same id, different level
    ts = tgt.instant_ts(DATE, "09:30:00")
    tracks = [
        tgt.Track("T1", "t1", ts, ts, 110.0, 10.0, (t1_pick,), t1_pick, 0),
        tgt.Track("T2", "t2", ts, ts, 110.0, 10.0, t2_rows, t2_rows[0], 0),
    ]
    facts = _StubFacts(_bars(DATE, [104.0, 99.0, 96.0, 95.0]))
    look = tgt.evaluate(tracks, facts, DATE, "09:30:00", "DOWN", "MNQ",
                        eval_end="09:40:00")
    assert look["entries"][0]["reached"] is True      # T2 D1 "near" @100 was reached
    assert look["entries"][1]["reached"] is False     # T2 D2 "far"  @90 was not
    assert look["verdicts"]["T1"]["pick_reached"] is False
    assert look["verdicts"]["T2"]["pick_reached"] is True


def test_a_recorded_thesis_whose_bias_opposes_the_request_is_flagged(facts, corpus):
    """A recorded DOL taken under the OTHER bias is not a comparable pick. 2026-08-12's
    production thesis is a live case: bias UP on a day whose 09:30 move was DOWN."""
    labels, cands = corpus
    same = tgt.build_report(facts, DATE, ENTRY, "DOWN", "MNQ",
                            labels=labels, cands=cands)["recorded_theses"][0]
    assert same["bias"] == "DOWN" and same["matches_request"] is True
    opposed = tgt.build_report(facts, DATE, ENTRY, "UP", "MNQ",
                               labels=labels, cands=cands)["recorded_theses"][0]
    assert opposed["matches_request"] is False


# --- the selector as a standalone, offline-runnable decision ----------------- #

def test_select_target_names_the_nearest_menu_entry(facts, corpus):
    labels, cands = corpus
    got = tgt.select_target(facts, DATE, ENTRY, DIRECTION, "MNQ",
                            selector="nearest", labels=labels, cands=cands)
    assert got["selector"] == "nearest"
    assert got["pick"]["id"] == "D1"
    assert got["pick"] == got["menu"][0]
    assert got["pick_rank"] == 0


def test_select_target_can_run_the_b8g_selector_too(facts, corpus):
    labels, cands = corpus
    got = tgt.select_target(facts, DATE, ENTRY, DIRECTION, "MNQ",
                            selector="b8g", labels=labels, cands=cands)
    assert got["selector"] == "b8g"
    assert got["pick"] in got["menu"]
    assert got["fit"]["excluded"] is True          # 08-24 is in the corpus


def test_select_target_carries_no_lookahead_whatsoever(facts, corpus):
    """The selection path must be usable as a decision: nothing derived from the future
    may appear in its output, by key or by value."""
    labels, cands = corpus
    got = tgt.select_target(facts, DATE, ENTRY, DIRECTION, "MNQ",
                            labels=labels, cands=cands)
    forbidden = {"lookahead", "draw", "turn", "extreme", "verdict", "verdicts",
                 "reached", "excursion", "outcome"}
    assert not (set(got) & forbidden)
    assert got["now_ts"] < got["boundary"]


@pytest.mark.timeout(600)
def test_select_target_is_identical_with_the_future_deleted(facts, corpus):
    labels, cands = corpus
    ts = tgt.instant_ts(DATE, ENTRY)
    blind = copy.copy(facts)
    blind._cache = {}
    blind._norm = {tk: df[df.index < ts] for tk, df in facts._norm.items()}
    blind._raw = {tk: df[df.index < ts] for tk, df in facts._raw.items()}
    for sel in tgt.SELECTORS:
        a = tgt.select_target(facts, DATE, ENTRY, DIRECTION, "MNQ", selector=sel,
                              labels=labels, cands=cands)
        b = tgt.select_target(blind, DATE, ENTRY, DIRECTION, "MNQ", selector=sel,
                              labels=labels, cands=cands)
        assert a == b, sel


def test_an_unknown_selector_is_refused(facts, corpus):
    labels, cands = corpus
    with pytest.raises(ValueError) as exc:
        tgt.select_target(facts, DATE, ENTRY, DIRECTION, "MNQ", selector="magic",
                          labels=labels, cands=cands)
    assert "magic" in str(exc.value)
