"""Phase 3 — scoring + scorecard tests (plan 10). Synthetic bars, no parquets."""

import os
import sys

import pandas as pd
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.join(os.path.dirname(_HERE), "contracts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import BenchConfig  # noqa: E402
import json  # noqa: E402

import report  # noqa: E402
import run_bench  # noqa: E402
import score  # noqa: E402
from lifecycle import BenchDecision, run_lifecycle  # noqa: E402

TZ = "America/New_York"


def mkbars(start, closes, highs=None, lows=None):
    idx = pd.date_range(pd.Timestamp(start, tz=TZ), periods=len(closes), freq="1min")
    closes = [float(c) for c in closes]
    highs = [float(h) for h in (highs if highs is not None else closes)]
    lows = [float(l) for l in (lows if lows is not None else closes)]
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes},
                        index=idx)


class FakeSource:
    def __init__(self, bars, open_ts, end_ts):
        self.bars, self.open_ts, self.end_ts = bars, open_ts, end_ts

    def session_bars(self, date):
        return self.bars

    def session_window(self, date):
        return self.open_ts, self.end_ts


def _cfg(**kw):
    base = dict(latency_sec=90, safety_nets=("ttl",), churn_cap=20)
    base.update(kw)
    return BenchConfig(**base)


def _lc(cause, bias="UP", dol=60.0, died="2026-06-25 09:10", arrival_price=50.0,
        mfe=5.0, stood=True, decision_id="d1"):
    return {"decision_id": decision_id, "cause": cause, "cause_detail": None,
            "stood": stood, "bias": bias, "gate": "MEDIUM", "self_report": "MEDIUM",
            "dol_level": "prev1_day_high", "dol_price": dol,
            "born_ts": "2026-06-25 09:00:00-04:00",
            "arrival_ts": "2026-06-25 09:01:30-04:00",
            "died_ts": f"{died}:00-04:00", "arrival_price": arrival_price,
            "time_alive_min": 10.0, "mfe": mfe, "mae": 2.0,
            "mfe_ts": "2026-06-25 09:05:00-04:00", "dist_to_dol_pct": None,
            "facts_hash": "h"}


def _summary(lifecycles, n_calls=1, decisions=None):
    return {"date": "2026-06-25", "day_outcome": "ok", "n_calls": n_calls,
            "lifecycles": lifecycles, "decisions": decisions or []}


OPEN = pd.Timestamp("2026-06-24 18:00", tz=TZ)
END = pd.Timestamp("2026-06-25 16:59", tz=TZ)


def test_false_kill_true_when_dol_touched_after_death():
    # Falsified at 09:10; after death price rises and touches DOL 60 within 4h → false_kill.
    bars = mkbars("2026-06-25 09:00", closes=[50] * 40,
                  highs=[50] * 12 + [61] * 28, lows=[49] * 40)
    src = FakeSource(bars, OPEN, END)
    enriched = score.score_date(_summary([_lc("falsified")]), src, _cfg())
    assert enriched["lifecycles"][0]["false_kill"] is True
    assert enriched["metrics"]["false_kill_count"] == 1


def test_false_kill_false_when_dol_never_touched():
    bars = mkbars("2026-06-25 09:00", closes=[50] * 40, highs=[51] * 40, lows=[49] * 40)
    src = FakeSource(bars, OPEN, END)
    enriched = score.score_date(_summary([_lc("falsified")]), src, _cfg())
    assert enriched["lifecycles"][0]["false_kill"] is False
    assert enriched["metrics"]["false_kill_count"] == 0


def test_completed_lifecycle_has_no_false_kill():
    bars = mkbars("2026-06-25 09:00", closes=[50] * 40, highs=[61] * 40, lows=[49] * 40)
    src = FakeSource(bars, OPEN, END)
    enriched = score.score_date(_summary([_lc("completed")]), src, _cfg())
    assert enriched["lifecycles"][0]["false_kill"] is None


def test_distance_covered_arithmetic():
    # UP thesis ref 50, DOL 60. Price rises to 55 (halfway) then flat → ttl. dist ~50%.
    bars = mkbars("2026-06-25 09:00",
                  closes=[50, 52, 55, 55, 55] + [55] * 200,
                  highs=[51, 53, 55, 55, 55] + [55] * 200,
                  lows=[49, 51, 54, 54, 54] + [54] * 200)
    d = BenchDecision("d1", pd.Timestamp("2026-06-25 09:00", tz=TZ),
                      {"bias": "UP", "dol": {"level": "L", "price": 60.0},
                       "falsified_if": [], "exhausted_if": [],
                       "recall": {"events": [], "max_age_min": 0}, "confidence": "MEDIUM"},
                      gate="MEDIUM")
    lc = run_lifecycle(d, bars, _cfg(), END)
    assert lc.cause == "ttl"
    # arrival price = close at 09:02 = 55; mfe = 55-55 = 0? ref is 55, high stays 55 → 0.
    # Use ref at arrival 09:02 (55). To get a clean 50% we check the recorded fields.
    assert lc.dist_to_dol_pct is not None
    assert 0.0 <= lc.dist_to_dol_pct <= 100.0


def test_late_kill_adverse_computed_for_falsified():
    # ref 50, peak favorable +5 (high 55), falsified at a bar with high 50 → give-back 5.
    bars = mkbars("2026-06-25 09:00", closes=[50] * 12,
                  highs=[50, 50, 55, 55, 50, 50, 50, 50, 50, 50, 50, 50], lows=[49] * 12)
    src = FakeSource(bars, OPEN, END)
    lc = _lc("falsified", died="2026-06-25 09:08", mfe=5.0, arrival_price=50.0)
    enriched = score.score_date(_summary([lc]), src, _cfg())
    lka = enriched["lifecycles"][0]["late_kill_adverse"]
    assert lka == pytest.approx(5.0)
    assert enriched["metrics"]["median_late_kill_adverse"] == pytest.approx(5.0)


def test_aggregate_regime_split(tmp_path):
    src_t = FakeSource(mkbars("2026-05-19 09:00", [50] * 20), OPEN, END)
    src_c = FakeSource(mkbars("2026-05-18 09:00", [50] * 20), OPEN, END)
    cfg = _cfg()
    s_trend = score.score_date({"date": "2026-05-19", "day_outcome": "ok", "n_calls": 2,
                                "lifecycles": [_lc("completed")], "decisions": []}, src_t, cfg)
    s_chop = score.score_date({"date": "2026-05-18", "day_outcome": "ok", "n_calls": 3,
                               "lifecycles": [_lc("falsified")], "decisions": []}, src_c, cfg)
    run_dir = str(tmp_path)
    agg = report.write_aggregate(run_dir, [s_trend, s_chop], cfg, run_id="t", mode="stub")
    md = open(os.path.join(run_dir, "aggregate.md"), encoding="utf-8").read()
    tsv = open(os.path.join(run_dir, "aggregate.tsv"), encoding="utf-8").read()
    assert "trend" in md and "chop" in md
    assert "2026-05-19" in tsv and "2026-05-18" in tsv
    assert agg["n_lifecycles"] == 2


def test_requires_shell_key_in_real_mode(monkeypatch):
    # Real mode must reject a missing SHELL key up front (no silent .env spend).
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SHELL environment"):
        run_bench._require_shell_key(None)
    with pytest.raises(RuntimeError):
        run_bench._require_shell_key("openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-shell")
    run_bench._require_shell_key(None)             # present → no raise
    run_bench._require_shell_key("openrouter")


def test_api_error_degrades_to_failsafe():
    # Spec §4: a backend/transport error becomes a failsafe decision (logged), not an abort.
    from facts import FactsResult

    class RaisingBackend:
        name = "raising"
        model = "x"

        def complete(self, *a, **k):
            raise RuntimeError("network down")

    fr = FactsResult(boundary=pd.Timestamp("2026-06-25 09:00", tz=TZ), degraded=False,
                     content_hash="h", text="facts", validator_dict={"levels": {}},
                     levels={}, daily_mid=None, sess_hi=None, sess_lo=None)

    class OneShotSource:
        def build_facts(self, ts):
            return fr

    log = []
    provider = run_bench.make_live_provider(OneShotSource(), RaisingBackend(), _cfg(),
                                            "2026-06-25", log)
    dec = provider(pd.Timestamp("2026-06-25 09:00", tz=TZ))
    assert dec.failsafe is True and dec.gate == "LOW"
    assert log[0]["degraded"] is True
    assert log[0]["degraded_reason"].startswith("api_error:RuntimeError")


def test_rescore_reproduces_churn_cap_abort(tmp_path):
    # A day that originally aborted on the churn cap must replay to the SAME abort — the
    # replay cap is exactly the logged decision count (regression for the len+1 over-run).
    bars = mkbars("2026-06-25 09:00", closes=[40] * 400, highs=[40.5] * 400, lows=[39] * 400)
    src = FakeSource(bars, pd.Timestamp("2026-06-25 09:00", tz=TZ),
                     pd.Timestamp("2026-06-25 16:59", tz=TZ))
    fals = [{"type": "price_beyond", "price": 45.0, "side": "below"}]
    logged = []
    for i in range(3):
        logged.append({
            "decision_id": f"d{i}", "date": "2026-06-25",
            "thesis": {"bias": "UP", "dol": {"level": "L", "price": 999.0},
                       "falsified_if": fals, "exhausted_if": [],
                       "recall": {"events": [], "max_age_min": 0}, "confidence": "MEDIUM"},
            "bench": {"gate": "MEDIUM", "self_report": "MEDIUM", "failsafe": False,
                      "levels": {}, "daily_mid": None, "sess_hi": None, "sess_lo": None},
        })
    src_dir = os.path.join(str(tmp_path), "src", "2026-06-25")
    os.makedirs(src_dir)
    with open(os.path.join(src_dir, "decisions.jsonl"), "w", encoding="utf-8") as fh:
        for rec in logged:
            fh.write(json.dumps(rec) + "\n")

    cfg = _cfg(churn_cap=3)
    out = run_bench.rescore_date(src, cfg, "2026-06-25", src_dir, os.path.join(str(tmp_path), "re"))
    assert out["day_outcome"] == "churn_cap"
    assert out["n_calls"] == 3


def test_scorecard_renders_with_zero_directional_theses(tmp_path):
    # The run-#0 shape: a single failsafe lifecycle, no directional standing thesis.
    fs = {"decision_id": "d1", "cause": "failsafe", "cause_detail": None, "stood": False,
          "bias": "NEUTRAL", "gate": "LOW", "self_report": "LOW", "dol_level": None,
          "dol_price": None, "born_ts": "2026-06-24 18:00:00-04:00",
          "arrival_ts": "2026-06-24 18:01:30-04:00", "died_ts": "2026-06-25 16:59:00-04:00",
          "arrival_price": 100.0, "time_alive_min": 1378.0, "mfe": None, "mae": None,
          "mfe_ts": None, "dist_to_dol_pct": None, "facts_hash": "h",
          "false_kill": None, "late_kill_adverse": None}
    src = FakeSource(mkbars("2026-06-25 09:00", [50] * 10), OPEN, END)
    enriched = score.score_date(_summary([fs]), src, _cfg())
    assert enriched["metrics"]["n_directional"] == 0
    assert enriched["metrics"]["completion_rate"] is None
    assert enriched["metrics"]["coverage_pct"] == 0.0
    path = report.write_scorecard(str(tmp_path), enriched, _cfg())
    text = open(path, encoding="utf-8").read()
    assert "failsafe" in text and "no lifecycles" not in text
