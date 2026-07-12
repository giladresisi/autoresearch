"""Phase 2/3 — run_bench integration tests (plan 10).

Stub end-to-end (linked ids + determinism) and rescore byte-stability, over the real
main parquets (skipped when absent). Uses one shared source so the parquets load once.
"""

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.join(os.path.dirname(_HERE), "contracts"),
           os.path.join(os.path.dirname(os.path.dirname(_HERE)), "calibration")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import BenchConfig  # noqa: E402
from facts import DEFAULT_MAIN, ParquetFactsSource  # noqa: E402
import run_bench  # noqa: E402
import score  # noqa: E402
import report  # noqa: E402
from run_agent import make_backend  # noqa: E402

DATE = "2026-05-19"

pytestmark = pytest.mark.skipif(
    not os.path.isdir(DEFAULT_MAIN), reason="main parquets not available")


@pytest.fixture(scope="module")
def source():
    return ParquetFactsSource()


def _cfg():
    # churn_cap=2: since plan 11 the stub failsafe carries recall.max_age_min=60, so a
    # failsafe day RE-CALLS (no longer one call spanning the session) — cap it low to keep
    # the full-day stub build fast (each facts build is ~8 s) while still exercising the
    # re-call loop. The determinism / linked-id / coverage assertions hold at any cap.
    return BenchConfig(latency_sec=90, safety_nets=("ttl",), churn_cap=2)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_stub_end_to_end_linked_ids_and_determinism(source, tmp_path):
    stub = make_backend("stub")
    cfg = _cfg()
    run_a = os.path.join(str(tmp_path), "a")
    run_b = os.path.join(str(tmp_path), "b")

    sum_a = run_bench.run_date(source, stub, cfg, DATE, run_a)
    sum_b = run_bench.run_date(source, stub, cfg, DATE, run_b)

    life_a = os.path.join(run_a, DATE, "lifecycles.jsonl")
    dec_a = os.path.join(run_a, DATE, "decisions.jsonl")
    assert os.path.exists(life_a) and os.path.exists(dec_a)
    assert sum_a["lifecycles"] and sum_a["decisions"]

    # Linked ids: every lifecycle's decision_id is present in decisions.jsonl.
    dec_ids = {d["decision_id"] for d in sum_a["decisions"]}
    assert {lc["decision_id"] for lc in sum_a["lifecycles"]} <= dec_ids

    # Stub is deterministic: two runs → byte-identical lifecycles + decisions.
    assert _read(life_a) == _read(os.path.join(run_b, DATE, "lifecycles.jsonl"))
    assert _read(dec_a) == _read(os.path.join(run_b, DATE, "decisions.jsonl"))

    # Stub thesis is NEUTRAL/LOW → waits, zero coverage.
    enriched = score.score_date(sum_a, source, cfg)
    assert enriched["metrics"]["coverage_pct"] == 0.0
    assert enriched["metrics"]["n_directional"] == 0


def test_run_bench_enriches_lifecycles_jsonl(tmp_path, monkeypatch):
    # The full run_bench() path must REWRITE lifecycles.jsonl with the scoring enrichment
    # so the on-disk record is complete (false_kill / lookahead_truncated keys present).
    monkeypatch.setenv("OPENROUTER_API_KEY", "unused-stub")  # stub ignores it
    out = run_bench.run_bench([DATE], "stub", "enrich_check", _cfg(),
                              runs_root=str(tmp_path))
    life = out["per_date"][0]["lifecycles"]
    assert life and all("false_kill" in lc and "lookahead_truncated" in lc for lc in life)
    on_disk = run_bench._read_jsonl(
        os.path.join(str(tmp_path), "enrich_check", DATE, "lifecycles.jsonl"))
    assert on_disk and all("lookahead_truncated" in lc for lc in on_disk)


def test_rescore_byte_stability(source, tmp_path):
    stub = make_backend("stub")
    cfg = _cfg()
    base = os.path.join(str(tmp_path), "base")
    run_bench.run_date(source, stub, cfg, DATE, base)
    base_date_dir = os.path.join(base, DATE)

    r1 = os.path.join(str(tmp_path), "r1")
    r2 = os.path.join(str(tmp_path), "r2")
    s1 = run_bench.rescore_date(source, cfg, DATE, base_date_dir, r1)
    s2 = run_bench.rescore_date(source, cfg, DATE, base_date_dir, r2)

    # Rescored lifecycles are byte-identical to each other AND to the base run.
    assert _read(os.path.join(r1, DATE, "lifecycles.jsonl")) == \
        _read(os.path.join(r2, DATE, "lifecycles.jsonl"))
    assert _read(os.path.join(r1, DATE, "lifecycles.jsonl")) == \
        _read(os.path.join(base_date_dir, "lifecycles.jsonl"))

    # Scorecards render identically across rescores (deterministic, no run-id inside).
    report.write_scorecard(os.path.join(r1, DATE), score.score_date(s1, source, cfg), cfg)
    report.write_scorecard(os.path.join(r2, DATE), score.score_date(s2, source, cfg), cfg)
    assert _read(os.path.join(r1, DATE, "scorecard.md")) == \
        _read(os.path.join(r2, DATE, "scorecard.md"))


def test_rescore_carries_logged_gate_source(source, tmp_path):
    # A rescore must stamp the LOGGED gate source, not the CLI default — a rescored
    # stand-directional (diagnostic) run keeps its [DIAGNOSTIC] header even when --gate
    # is omitted (defaults to calibrated).
    from config import BenchConfig
    diag = BenchConfig(latency_sec=90, safety_nets=("ttl",), churn_cap=2,
                       gate_source="stand-directional")
    run_bench.run_bench([DATE], "stub", "gate_src", diag, runs_root=str(tmp_path))
    default = BenchConfig(latency_sec=90, safety_nets=("ttl",), churn_cap=2)   # calibrated
    run_bench.run_bench([DATE], "rescore", "gate_rescore", default,
                        source_run="gate_src", runs_root=str(tmp_path))
    sc = _read(os.path.join(str(tmp_path), "gate_rescore", DATE, "scorecard.md"))
    agg = _read(os.path.join(str(tmp_path), "gate_rescore", "aggregate.md"))
    assert "stand-directional" in sc and "DIAGNOSTIC" in sc
    assert "stand-directional" in agg and "DIAGNOSTIC" in agg


def test_rescore_conflicting_explicit_gate_errors(source, tmp_path):
    from config import BenchConfig
    diag = BenchConfig(latency_sec=90, safety_nets=("ttl",), churn_cap=2,
                       gate_source="stand-directional")
    run_bench.run_bench([DATE], "stub", "gate_src2", diag, runs_root=str(tmp_path))
    conflicting = BenchConfig(latency_sec=90, safety_nets=("ttl",), churn_cap=2,
                              gate_source="self")     # explicit, != logged
    with pytest.raises(ValueError, match="conflicts with the rescored"):
        run_bench.run_bench([DATE], "rescore", "gate_rescore2", conflicting,
                            source_run="gate_src2", runs_root=str(tmp_path))
