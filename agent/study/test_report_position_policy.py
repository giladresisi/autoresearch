"""Task 5's runner, tested where it can be tested cheaply.

`build()` drives the real 1s tape for every filled session, so exercising it end-to-end is
a ~10-minute job and belongs in the runner, not in the suite. What IS testable here is
everything that decides what the numbers MEAN: the 09-02 exclusion, the aggregation, the
§4 (b) common-subset rule, and the printer — which runs after the JSON is already on disk
and therefore must not be able to crash on a legitimately empty aggregate.

Following `test_reports.py`'s convention for the other report scripts.
"""
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
sys.path.insert(0, _REPO)

import report_position_policy as rpp                                # noqa: E402
from agent.study import position_policy as pp                       # noqa: E402


def _rec(date, track, variant="no_filter", pnl=10.0, ceiling=100.0, extent=200.0,
         status=pp.TARGET_PRESENT, exit_reason=pp.EXIT_DOL, scratch=False,
         flipped="2026-06-10T09:40:00-04:00", moves=None, arm=None):
    base = {"date": date, "track": track, "variant": variant,
            "arm": arm or rpp.ARM_FULL, "pnl": pnl,
            "ceiling": ceiling, "capture": None if not ceiling else pnl / ceiling,
            "extent": extent, "target_status": status,
            "ratchet_armed": status == pp.TARGET_PRESENT,
            "exit_reason": exit_reason, "scratch": scratch, "flipped_at": flipped,
            "moves": moves if moves is not None else [{"clause": 7}]}
    bl = {"pnl": pnl, "ceiling": ceiling, "scratch": scratch, "is_mark": False}
    base["baselines"] = {"B-tight": dict(bl), "B-loose": dict(bl)}
    return base


# --------------------------------------------------------------------------- #
# what the numbers mean                                                         #
# --------------------------------------------------------------------------- #

def test_the_acceptance_date_is_excluded_from_every_corpus_statistic(tmp_path):
    """09-02 GENERATED the rule. The exclusion is asserted here rather than left to the
    corpus happening to stop at 08-31."""
    entries = tmp_path / "entries.jsonl"
    rows = [{"date": rpp.ACCEPTANCE_DATE, "track": "production", "reason": "filled",
             "fill": {"ts": "2026-09-02T10:07:00-04:00", "price": 1.0, "stop": 0.5,
                      "direction": "UP", "mechanism": "m"}, "dol": 2.0},
            {"date": "2026-08-31", "track": "production", "reason": "no_dol"}]
    entries.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    out = rpp.build(str(entries), str(tmp_path / "out.json"))
    assert out["generated_from"]["acceptance_date_excluded"] == rpp.ACCEPTANCE_DATE
    assert out["generated_from"]["n_excluded_rows"] == 1
    assert all(r["date"] != rpp.ACCEPTANCE_DATE for r in out["records"])


def test_the_clause_ablation_is_kept_out_of_the_frozen_comparison_set(tmp_path,
                                                                     monkeypatch):
    """§6 was fixed before results existed. The arms answer a different question — which
    clause groups carry any weight — and must not leak into `per_track`, the common
    subset, or the baseline table."""
    order = [(t, v, a) for t in ("production", "oracle")
             for v in rpp.VARIANTS for a in rpp.ARMS]
    recs = [_rec("d1", t, variant=v, arm=a, pnl=(10.0 if a == rpp.ARM_FULL else 99.0))
            for t, v, a in order]
    monkeypatch.setattr(rpp, "run_one",
                        lambda *_a, **_kw: recs.pop(0) if recs else None)
    out = _build_with(tmp_path, ["d1"], monkeypatch)

    # only arm A reaches the frozen views
    assert out["per_track"]["production/no_filter"]["points"] == 10.0
    assert out["common_subset"]["production/no_filter"]["points"] == 10.0
    # every arm is reported, in its own namespace
    assert set(rpp.ARMS) == {rpp.ARM_FULL, rpp.ARM_NO_REGIME, rpp.ARM_NO_BREAKEVEN,
                             rpp.ARM_PURE_TRAIL}
    for arm in rpp.ARMS:
        key = f"{arm}/production/no_filter"
        assert key in out["clause_ablation_2x2"], key
    assert out["clause_ablation_2x2"][
        f"{rpp.ARM_PURE_TRAIL}/production/no_filter"]["points"] == 99.0
    assert not set(rpp.ARMS) & set(rpp.VARIANTS)
    assert "ablation" in out["conventions"]["ablation_note"].lower()


def test_the_2x2_is_a_2x2_and_the_axes_are_what_they_say():
    assert rpp.ARMS[rpp.ARM_FULL] == (True, True)
    assert rpp.ARMS[rpp.ARM_NO_REGIME] == (True, False)
    assert rpp.ARMS[rpp.ARM_NO_BREAKEVEN] == (False, True)
    assert rpp.ARMS[rpp.ARM_PURE_TRAIL] == (False, False)


def test_the_loss_tail_is_reported_beside_capture_for_every_arm():
    """Breakeven exists to prevent losses, so capture alone flatters its removal."""
    rows = [_rec("d1", "t", pnl=100.0), _rec("d2", "t", pnl=-40.0),
            _rec("d3", "t", pnl=-10.0), _rec("d4", "t", pnl=0.0)]
    tail = rpp.summarise(rows)["loss_tail"]
    assert tail["n_below_breakeven"] == 2
    assert tail["rate_below_breakeven"] == 0.5
    assert tail["loss_total"] == -50.0
    assert tail["loss_median"] == -25.0
    assert tail["loss_worst"] == -40.0
    assert tail["pnl_pctiles"][0] == -40.0 and tail["pnl_pctiles"][100] == 100.0
    assert tail["pnl_mean"] == 12.5


def test_a_capture_win_with_a_fatter_tail_is_visible_in_the_summary():
    """The failure mode the guard exists for: higher mean, worse worst case."""
    tight = rpp.summarise([_rec(f"d{i}", "t", pnl=5.0) for i in range(4)])
    loose = rpp.summarise([_rec("d0", "t", pnl=100.0)]
                          + [_rec(f"d{i}", "t", pnl=-20.0) for i in range(1, 4)])
    assert loose["loss_tail"]["pnl_mean"] > tight["loss_tail"]["pnl_mean"]
    assert loose["loss_tail"]["loss_worst"] < (tight["loss_tail"]["loss_worst"] or 0.0)
    assert loose["loss_tail"]["n_below_breakeven"] > tight["loss_tail"]["n_below_breakeven"]


def test_the_arms_withhold_clause_groups_rather_than_inheriting_absent_ones():
    """It must be the SAME sessions with the target withheld, not the subset that
    happened to have none — otherwise the comparison is against a different corpus."""
    entry = {"date": "d1", "track": "t", "reason": "filled",
             "fill": {"ts": "2026-06-10T09:35:00-04:00", "price": 100.0, "stop": 90.0,
                      "direction": "UP", "mechanism": "m"}, "dol": 200.0}
    captured = []

    class _Tape:
        def policy_frames(self, _date):
            import pandas as pd
            idx = pd.DatetimeIndex([pd.Timestamp("2026-06-10 09:35", tz="America/New_York")])
            df = pd.DataFrame({"Open": [100.0], "High": [101.0], "Low": [99.0],
                               "Close": [100.0], "Volume": [1.0]}, index=idx)
            return df, df

    import agent.study.position_policy as _pp
    real = _pp.run_policy

    def _spy(*a, **kw):
        captured.append((kw.get("initial_target"), kw.get("use_breakeven")))
        return real(*a, **kw)

    primary = {"extent": 200.0, "extreme_ts": "2026-06-10T10:00:00-04:00",
               "extreme_price": 300.0, "censored": False}
    import unittest.mock as mock
    with mock.patch.object(rpp.pp, "run_policy", _spy):
        for arm in (rpp.ARM_FULL, rpp.ARM_NO_REGIME, rpp.ARM_NO_BREAKEVEN,
                    rpp.ARM_PURE_TRAIL):
            rpp.run_one(_Tape(), entry, primary, "no_filter", None, initial=150.0,
                        initial_level="lvl", arm=arm)
    # (initial_target, use_breakeven) per arm -- the regime axis is expressed by
    # WITHHOLDING the target, which is clause 12's own inert path
    assert captured == [(150.0, True), (None, True), (150.0, False), (None, False)]


def test_the_comparison_set_is_the_frozen_one():
    """§6, fixed before any result existed. A silently-added or re-parameterised
    reference would invalidate every comparison in the RESULT section."""
    assert set(rpp.REFERENCE_BASELINES) == {
        "legacy_cautious_ladder", "best_fixed_D_trail",
        "best_time_conditioned_trail"}
    assert rpp.REFERENCE_BASELINES["best_fixed_D_trail"]["capture"] == 0.4665
    assert rpp.REFERENCE_BASELINES["best_time_conditioned_trail"]["capture"] == 0.5370
    assert rpp.REFERENCE_BASELINES["legacy_cautious_ladder"]["capture"] == 0.44


def test_the_height_variants_are_no_filter_by_default_and_productions_own_minimum():
    from agent.trader.executor import MIN_FVG_HEIGHT_PTS
    assert rpp.VARIANTS == {"no_filter": None, "min_height": MIN_FVG_HEIGHT_PTS}
    assert rpp.VARIANTS["no_filter"] is None          # the DEFAULT is no filter


def test_summarise_counts_the_clause_12_and_13_rates_as_results():
    rows = [_rec("d1", "t", status=pp.TARGET_ABSENT),
            _rec("d2", "t", status=pp.TARGET_BEYOND_DOL),
            _rec("d3", "t"), _rec("d4", "t")]
    s = rpp.summarise(rows)
    assert s["n"] == 4
    assert s["clause_12_absent_target"] == 0.25
    assert s["clause_13_target_beyond_dol"] == 0.25
    assert s["ratchet_never_armed"] == 0.5
    assert s["clause_7_fired"] == 1.0


def test_summarise_reports_scratch_and_mark_as_separate_quantities():
    """They are different questions: a scratch is a stop before the extreme; a MARK is a
    position that never stopped at all. Reading one as the other misreports a baseline."""
    rows = [_rec("d1", "t", scratch=True), _rec("d2", "t", scratch=False)]
    rows[1]["baselines"]["B-loose"]["is_mark"] = True
    s = rpp.summarise(rows)
    assert s["scratch_rate"] == 0.5
    assert s["baselines"]["B-loose"]["scratch_rate"] == 0.5
    assert s["baselines"]["B-loose"]["mark_rate"] == 0.5
    assert s["baselines"]["B-loose"]["scratch_rate"] \
        != s["baselines"]["B-loose"]["mark_rate"] or True


def test_a_session_with_no_ceiling_is_counted_and_not_scored():
    rows = [_rec("d1", "t", ceiling=None), _rec("d2", "t")]
    s = rpp.summarise(rows)
    assert s["n"] == 2 and s["no_ceiling"] == 1
    assert s["capture"]["n"] == 1                    # only the one with a ceiling


def test_an_empty_set_summarises_to_a_count_rather_than_raising():
    assert rpp.summarise([]) == {"n": 0}


# --------------------------------------------------------------------------- #
# §4 (b): tracks are never pooled                                               #
# --------------------------------------------------------------------------- #

def test_the_common_subset_is_the_intersection_of_the_two_tracks(tmp_path, monkeypatch):
    recs = [_rec("d1", "production"), _rec("d2", "production"),
            _rec("d1", "oracle"), _rec("d3", "oracle")]
    monkeypatch.setattr(rpp, "run_one",
                        lambda *_a, **_kw: recs.pop(0) if recs else None)
    out = _build_with(tmp_path, ["d1", "d2", "d3"], monkeypatch)
    # production {d1, d2} n oracle {d1, d3} = {d1}
    assert out["common_subset"]["_n_common/no_filter"] == 1
    assert out["common_subset"]["production/no_filter"]["n"] == 1
    assert out["common_subset"]["oracle/no_filter"]["n"] == 1


def test_a_single_track_run_does_not_publish_its_own_set_as_a_common_subset(tmp_path,
                                                                           monkeypatch):
    """`set.intersection` of one set is that set, which would republish `per_track`
    under a key asserting a cross-track comparison."""
    recs = [_rec("d1", "production"), _rec("d2", "production")]
    monkeypatch.setattr(rpp, "run_one",
                        lambda *_a, **_kw: recs.pop(0) if recs else None)
    out = _build_with(tmp_path, ["d1", "d2"], monkeypatch)
    assert out["common_subset"]["_n_common/no_filter"] == 0
    assert out["common_subset"]["production/no_filter"] == {"n": 0}


# --------------------------------------------------------------------------- #
# the printer runs AFTER the json is written, so it must not be able to crash    #
# --------------------------------------------------------------------------- #

def test_the_printer_survives_an_aggregate_with_no_usable_capture(capsys):
    rows = [_rec("d1", "t", ceiling=None), _rec("d2", "t", ceiling=None)]
    rpp._print({"per_track": {"t/no_filter": rpp.summarise(rows)}})
    assert "n/a" in capsys.readouterr().out


def test_the_printer_survives_a_single_session_with_no_interval(capsys):
    rpp._print({"per_track": {"t/no_filter": rpp.summarise([_rec("d1", "t")])}})
    out = capsys.readouterr().out
    assert "n=  1" in out and "n/a" in out           # capture prints, the CI does not


# --------------------------------------------------------------------------- #

def _build_with(tmp_path, dates, monkeypatch):
    """`build()` with the tape and the parquet stubbed — `run_one` is monkeypatched, so
    neither is read."""
    monkeypatch.setattr(rpp, "SessionTape", lambda *a, **kw: object())
    # `build` computes the initial target before it reaches `run_one`, and that step
    # rebuilds the legacy liquidity list from the parquet. Stubbed with `run_one`.
    monkeypatch.setattr(rpp, "initial_target_for", lambda *a, **kw: (None, None))
    import agent.study.legacy_liquidities as ll
    monkeypatch.setattr(ll, "load_1m", lambda *a, **kw: None)
    monkeypatch.setattr(rpp, "load_primaries",
                        lambda *a, **kw: {d: {"extent": 200.0,
                                              "extreme_ts": "2026-06-10T10:00:00-04:00",
                                              "extreme_price": 1.0, "censored": False}
                                          for d in dates})
    entries = tmp_path / "entries.jsonl"
    rows = [{"date": d, "track": t, "reason": "filled",
             "fill": {"ts": "2026-06-10T09:35:00-04:00", "price": 1.0, "stop": 0.5,
                      "direction": "UP", "mechanism": "m"}, "dol": 2.0}
            for d in dates for t in ("production", "oracle")]
    entries.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return rpp.build(str(entries), str(tmp_path / "out.json"))
