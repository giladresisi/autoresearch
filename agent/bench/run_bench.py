"""Bench orchestration + CLI (plan 10 Phase 2 + Phase 4).

Wires the offline facts builder (facts.ParquetFactsSource) to decide_thesis (reusing
run_agent.make_backend / decide_thesis + the confidence gate) and the lifecycle engine,
logging per-call decisions.jsonl (full thesis JSON + attempts/violations/usage/latency/
facts_hash) and per-date lifecycles.jsonl, then delegating scoring/scorecards to score.py
+ report.py.

Modes:
  stub     — StubBackend, $0, deterministic (offline).
  real     — a real backend (per-run shell env key), the only mode that spends.
  rescore  — re-run scoring + predicate replay from a previous run's decisions.jsonl, $0,
             byte-stable for identical inputs (no facts rebuild, no API).

CLI:
  python -m agent.bench.run_bench --dates D1,D2 --mode stub|real|rescore [--run-id ...]
     [--latency-sec N] [--safety-net ttl,acceptance_flip,...] [--churn-cap N]
     [--main-dir DIR] [--source-run RUN_ID]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_AGENT)
for _p in (_HERE, _AGENT, os.path.join(_AGENT, "contracts"),
           os.path.join(_REPO, "calibration")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import BenchConfig, SAFETY_NETS  # noqa: E402
from lifecycle import BenchDecision, run_day  # noqa: E402
from facts import ParquetFactsSource  # noqa: E402

from confidence import confidence  # noqa: E402  (agent/confidence.py)
from run_agent import decide_thesis, make_backend  # noqa: E402
from schemas import effective_recall_max_age, failsafe_thesis  # noqa: E402  (agent/contracts)
from validate_contracts import classify_predicates  # noqa: E402  (agent/contracts)

RUNS_ROOT = os.path.join(_HERE, "runs")


# --------------------------------------------------------------------------- #
# Decision provider (facts -> decide_thesis -> gate -> BenchDecision + log)    #
# --------------------------------------------------------------------------- #
def _decision_id(date: str, i: int) -> str:
    return f"th_{date.replace('-', '')}_{i:02d}"


def _enforce_default_recall(thesis: dict) -> None:
    """Plan 12 Fix 1 — clamp the thesis's `recall.max_age_min` to the code-enforced default
    in place (spec §2.1). A non-standing thesis whose model-authored max_age is 0/absent and
    whose recall events never fire otherwise waits forever (07-02 diag: th_04 waited 20.5h).

    Applied at GENERATION time (here), NOT in the lifecycle engine, ON PURPOSE: the bench
    engine (_run_waiting) + `rescore` replay logged decisions verbatim, so a clamp inside
    _run_waiting would retroactively rewrite the lifecycle boundaries of ALREADY-LOGGED runs
    on rescore (breaking replay fidelity — plan 12 Fix-1 REQUIRED). Baking the clamped value
    into the logged thesis instead makes the default TTL manifest only in NEW runs while
    rescore of prior runs (whose logged max_age is untouched) stays byte-stable. Harmless for
    a standing (HIGH/MEDIUM directional) thesis — _run_standing ignores recall entirely."""
    recall = thesis.get("recall")
    if not isinstance(recall, dict):
        recall = {}
    recall.setdefault("events", [])
    recall["max_age_min"] = effective_recall_max_age(recall.get("max_age_min"))
    thesis["recall"] = recall


_EMPTY_MENU = {"n_menu_hit": 0, "n_escape_hatch": 0, "menu_hit_ratio": None,
               "dol_menu_hit": False, "dol_menu_id": None}


def _bench_block(gate, self_report, failsafe, levels, daily_mid, sess_hi, sess_lo, *,
                 gate_source="calibrated", calibrated_gate=None, menu=None) -> dict:
    """The engine-facing inputs, logged so `rescore` can replay the lifecycle without
    rebuilding facts or calling the API. `gate` is the EFFECTIVE gate the engine uses
    (post gate-source override); `calibrated_gate` records the production value for
    reference; `menu` is the menu-hit / escape-hatch telemetry (plan 11)."""
    return {
        "gate": gate, "self_report": self_report, "failsafe": bool(failsafe),
        "gate_source": gate_source, "calibrated_gate": calibrated_gate,
        "levels": levels or {}, "daily_mid": daily_mid,
        "sess_hi": sess_hi, "sess_lo": sess_lo,
        "menu": menu or dict(_EMPTY_MENU),
    }


def _effective_gate(cfg, calibrated_gate, thesis, failsafe: bool) -> str:
    """Resolve the effective standing gate under the (bench-only) gate-source override."""
    src = getattr(cfg, "gate_source", "calibrated")
    if src == "self":
        if failsafe:
            return "LOW"                              # a failsafe never stands, regardless
        sr = (thesis or {}).get("confidence")
        return sr if sr in ("HIGH", "MEDIUM", "LOW") else "LOW"
    if src == "stand-directional":
        bias = (thesis or {}).get("bias")
        return "HIGH" if (not failsafe and bias in ("UP", "DOWN")) else "LOW"
    return calibrated_gate                                   # calibrated (production default)


def make_live_provider(source: ParquetFactsSource, backend, cfg: BenchConfig,
                       date: str, decisions_log: list):
    """A provider that builds facts, calls decide_thesis, computes the confidence gate,
    logs the call, and returns a BenchDecision. Appends one record per call to
    `decisions_log`."""
    counter = {"n": 0}
    # Stub is offline/instant: strip the (microsecond) wall-clock latency so the stub
    # decisions.jsonl is byte-deterministic across runs (no 0.5ms rounding flakiness).
    scrub_latency = getattr(backend, "name", "") == "stub"

    def provide(trigger_ts: pd.Timestamp) -> BenchDecision:
        counter["n"] += 1
        did = _decision_id(date, counter["n"])
        arrival = trigger_ts + cfg.latency()
        fr = source.build_facts(trigger_ts)

        if fr.degraded:
            th = failsafe_thesis()
            th["issued_at"] = trigger_ts.isoformat()
            th["thesis_id"] = did
            decisions_log.append({
                "decision_id": did, "date": date,
                "trigger_ts": trigger_ts.isoformat(), "arrival_ts": arrival.isoformat(),
                "facts_hash": fr.content_hash or None, "degraded": True,
                "degraded_reason": fr.error, "verdict": "failsafe", "fallback": True,
                "retries": 0, "latency_total_sec": 0.0, "usage_total": {},
                "attempts": [], "reasoning": None, "thesis": th,
                "bench": _bench_block("LOW", "LOW", True, {}, None, None, None,
                                      gate_source=cfg.gate_source, calibrated_gate="LOW"),
            })
            return BenchDecision(did, trigger_ts, th, "LOW", "LOW", failsafe=True,
                                 facts_hash=fr.content_hash)

        # Spec §4 failure policy: an API/transport error degrades this call to a failsafe
        # (no thesis stands) rather than aborting the whole multi-date run.
        try:
            # Model prompt = S0–S7 core + the S8 menu block; validator_dict carries the
            # structured menu for the menu-membership check + escape-hatch tagging.
            outcome = decide_thesis(fr.text + fr.menu_text, "", fr.validator_dict, backend)
        except Exception as exc:  # noqa: BLE001 — any backend/transport failure → failsafe
            th = failsafe_thesis()
            th["issued_at"] = trigger_ts.isoformat()
            th["thesis_id"] = did
            th["facts_hash"] = fr.content_hash
            decisions_log.append({
                "decision_id": did, "date": date,
                "trigger_ts": trigger_ts.isoformat(), "arrival_ts": arrival.isoformat(),
                "facts_hash": fr.content_hash, "degraded": True,
                "degraded_reason": f"api_error:{type(exc).__name__}",
                "verdict": "failsafe", "fallback": True, "retries": 0,
                "latency_total_sec": 0.0, "usage_total": {}, "attempts": [],
                "reasoning": None, "thesis": th,
                "bench": _bench_block("LOW", "LOW", True, {}, None, None, None,
                                      gate_source=cfg.gate_source, calibrated_gate="LOW"),
            })
            return BenchDecision(did, trigger_ts, th, "LOW", "LOW", failsafe=True,
                                 facts_hash=fr.content_hash)

        thesis = dict(outcome.block)
        thesis["issued_at"] = trigger_ts.isoformat()
        thesis["thesis_id"] = did
        thesis["facts_hash"] = fr.content_hash
        _enforce_default_recall(thesis)        # plan 12 Fix 1 — default low-conf recall TTL
        calibrated_gate = confidence(thesis, fr.validator_dict)   # production value (audit)
        gate = _effective_gate(cfg, calibrated_gate, thesis, outcome.fallback)
        self_report = thesis.get("confidence")
        # Menu-hit / escape-hatch telemetry over the FINAL (validated) thesis predicates.
        audit = classify_predicates(thesis, fr.validator_dict)
        menu = {"n_menu_hit": audit["n_menu_hit"],
                "n_escape_hatch": audit["n_escape_hatch"],
                "menu_hit_ratio": audit["menu_hit_ratio"],
                "dol_menu_hit": audit["dol_menu_hit"], "dol_menu_id": audit["dol_menu_id"]}

        attempts = outcome.attempts
        latency_total = outcome.latency_total
        if scrub_latency:
            latency_total = 0.0
            attempts = [{**a, "latency_sec": 0.0} for a in attempts]

        decisions_log.append({
            "decision_id": did, "date": date,
            "trigger_ts": trigger_ts.isoformat(), "arrival_ts": arrival.isoformat(),
            "facts_hash": fr.content_hash, "degraded": False,
            "verdict": outcome.verdict, "fallback": outcome.fallback,
            "retries": outcome.retries,
            "latency_total_sec": latency_total,
            "usage_total": outcome.usage_total, "attempts": attempts,
            "reasoning": outcome.reasoning, "thesis": thesis,
            "bench": _bench_block(gate, self_report, outcome.fallback, fr.levels,
                                  fr.daily_mid, fr.sess_hi, fr.sess_lo,
                                  gate_source=cfg.gate_source,
                                  calibrated_gate=calibrated_gate, menu=menu),
        })
        return BenchDecision(
            did, trigger_ts, thesis, gate, self_report, failsafe=outcome.fallback,
            levels=fr.levels, daily_mid=fr.daily_mid, sess_hi=fr.sess_hi,
            sess_lo=fr.sess_lo, facts_hash=fr.content_hash)

    return provide


def make_replay_provider(logged_decisions: list):
    """A $0 provider that replays logged decisions in sequence (rescore). born_ts is
    taken from the engine's trigger (deterministically equal to the logged trigger), so
    the lifecycle replay is byte-stable."""
    queue = list(logged_decisions)
    idx = {"i": 0}

    def provide(trigger_ts: pd.Timestamp) -> BenchDecision:
        rec = queue[idx["i"]]
        idx["i"] += 1
        b = rec.get("bench") or {}
        return BenchDecision(
            decision_id=rec["decision_id"], born_ts=trigger_ts,
            thesis=rec.get("thesis") or {}, gate=b.get("gate", "LOW"),
            self_report=b.get("self_report"), failsafe=bool(b.get("failsafe")),
            levels=b.get("levels") or {}, daily_mid=b.get("daily_mid"),
            sess_hi=b.get("sess_hi"), sess_lo=b.get("sess_lo"),
            facts_hash=rec.get("facts_hash"))

    return provide


# --------------------------------------------------------------------------- #
# Per-date run                                                                 #
# --------------------------------------------------------------------------- #
def _write_jsonl(path: str, records: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")


def _read_jsonl(path: str) -> list:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def run_date(source: ParquetFactsSource, backend, cfg: BenchConfig, date: str,
             run_dir: str) -> dict:
    """Run one date end-to-end (facts + calls + engine), write lifecycles.jsonl +
    decisions.jsonl, return a summary dict {date, day_outcome, n_calls, lifecycles,
    decisions}."""
    open_ts, end_ts = source.session_window(date)
    bars = source.session_bars(date)
    decisions_log: list = []
    provider = make_live_provider(source, backend, cfg, date, decisions_log)
    day = run_day(bars, provider, cfg, open_ts, end_ts, date)

    date_dir = os.path.join(run_dir, date)
    lifecycles = [lc.to_record() for lc in day.lifecycles]
    _write_jsonl(os.path.join(date_dir, "lifecycles.jsonl"), lifecycles)
    _write_jsonl(os.path.join(date_dir, "decisions.jsonl"), decisions_log)
    return {"date": date, "day_outcome": day.day_outcome, "n_calls": day.n_calls,
            "lifecycles": lifecycles, "decisions": decisions_log,
            "session_end": end_ts.isoformat()}


def rescore_date(source: ParquetFactsSource, cfg: BenchConfig, date: str,
                 source_date_dir: str, run_dir: str) -> dict:
    """Replay a previous run's logged decisions through the engine at $0 (byte-stable),
    re-writing lifecycles.jsonl + copying decisions.jsonl into the new run."""
    logged = _read_jsonl(os.path.join(source_date_dir, "decisions.jsonl"))
    open_ts, end_ts = source.session_window(date)
    bars = source.session_bars(date)
    provider = make_replay_provider(logged)
    # Replay EXACTLY the logged number of calls: this reproduces both a normal day and a
    # churn-capped abort identically (a larger cap would over-run the logged queue).
    replay_cfg = _with_churn(cfg, len(logged))
    day = run_day(bars, provider, replay_cfg, open_ts, end_ts, date)

    date_dir = os.path.join(run_dir, date)
    lifecycles = [lc.to_record() for lc in day.lifecycles]
    _write_jsonl(os.path.join(date_dir, "lifecycles.jsonl"), lifecycles)
    _write_jsonl(os.path.join(date_dir, "decisions.jsonl"), logged)
    # A rescore REPRODUCES the logged run, so scorecards must be stamped with the gate
    # source the decisions were produced under — not the CLI cfg (which defaults to
    # "calibrated"). Recover it from the logged bench block so a rescored diagnostic run
    # keeps its [DIAGNOSTIC] header (run_bench threads this into the scoring cfg).
    logged_gate = "calibrated"
    for rec in logged:
        gs = (rec.get("bench") or {}).get("gate_source")
        if gs:
            logged_gate = gs
            break
    return {"date": date, "day_outcome": day.day_outcome, "n_calls": day.n_calls,
            "lifecycles": lifecycles, "decisions": logged,
            "session_end": end_ts.isoformat(), "logged_gate_source": logged_gate}


_REAL_KEYS = {"openrouter": ("OPENROUTER_API_KEY",),
              "anthropic": ("ANTHROPIC_API_KEY",),
              None: ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")}


def _require_shell_key(backend_name: Optional[str]) -> None:
    """Repo-standing rule: real spend only via a per-run SHELL env key, never `.env`
    auto-loading. `make_backend` would happily read the worktree `.env`, so we fail fast
    here unless the key is already present in the actual process environment (which, at
    real-mode entry before any dotenv load, is the shell). Prevents a populated `.env`
    from silently spending."""
    needed = _REAL_KEYS.get(backend_name, _REAL_KEYS[None])
    if not any(os.environ.get(k) for k in needed):
        raise RuntimeError(
            "real mode requires an API key in the SHELL environment (per-run), not .env. "
            f"Set one of {list(needed)} in the shell before the run, e.g. "
            "`OPENROUTER_API_KEY=... python -m agent.bench.run_bench --mode real ...`")


def _with_churn(cfg: BenchConfig, churn_cap: int) -> BenchConfig:
    return BenchConfig(
        latency_sec=cfg.latency_sec, ttl_minutes=dict(cfg.ttl_minutes),
        safety_nets=tuple(cfg.safety_nets), acceptance_flip_n=cfg.acceptance_flip_n,
        churn_cap=churn_cap, regime_map=dict(cfg.regime_map), lookahead_h=cfg.lookahead_h,
        gate_source=cfg.gate_source)


def _with_gate(cfg: BenchConfig, gate_source: str) -> BenchConfig:
    return BenchConfig(
        latency_sec=cfg.latency_sec, ttl_minutes=dict(cfg.ttl_minutes),
        safety_nets=tuple(cfg.safety_nets), acceptance_flip_n=cfg.acceptance_flip_n,
        churn_cap=cfg.churn_cap, regime_map=dict(cfg.regime_map), lookahead_h=cfg.lookahead_h,
        gate_source=gate_source)


# --------------------------------------------------------------------------- #
# Full run                                                                     #
# --------------------------------------------------------------------------- #
def run_bench(dates: list, mode: str, run_id: str, cfg: BenchConfig, *,
              main_dir: Optional[str] = None, backend_name: Optional[str] = None,
              model: Optional[str] = None, source_run: Optional[str] = None,
              runs_root: str = RUNS_ROOT) -> dict:
    """Run the bench over `dates` in one of the three modes; score + report at the end.

    Returns the aggregate summary dict. Real spend happens ONLY in `real` mode (a real
    backend built from the per-run shell env); stub/rescore are $0."""
    import score
    import report

    run_dir = os.path.join(runs_root, run_id)
    os.makedirs(run_dir, exist_ok=True)

    source = ParquetFactsSource(main_dir) if main_dir else ParquetFactsSource()
    per_date = []

    if mode == "rescore":
        src_root = os.path.join(runs_root, source_run) if source_run else None
        if not src_root or not os.path.isdir(src_root):
            raise ValueError(f"--source-run must name an existing run dir (got {source_run!r})")
        for date in dates:
            summary = rescore_date(source, cfg, date, os.path.join(src_root, date), run_dir)
            per_date.append(summary)
        # Rescore reproduces the logged run → stamp the LOGGED gate source, not the CLI
        # default. Adopt it for scoring/reporting; if an explicit non-default --gate
        # conflicts with the logged gate, that is a user error (the replay is fixed).
        logged_gates = {s.get("logged_gate_source", "calibrated") for s in per_date}
        logged_gate = logged_gates.pop() if len(logged_gates) == 1 else "mixed"
        if cfg.gate_source != "calibrated" and cfg.gate_source != logged_gate:
            raise ValueError(
                f"--gate {cfg.gate_source!r} conflicts with the rescored run's logged gate "
                f"{logged_gate!r}; omit --gate to reuse the logged one (rescore replays the "
                "logged decisions verbatim).")
        cfg = _with_gate(cfg, logged_gate)
    else:
        if mode == "real":
            _require_shell_key(backend_name)      # fail fast — no silent .env spend
        backend = (make_backend("stub") if mode == "stub"
                   else make_backend(backend_name, model))
        for date in dates:
            per_date.append(run_date(source, backend, cfg, date, run_dir))

    # Phase 3: score every lifecycle, render per-date scorecards + the aggregate, then
    # REWRITE lifecycles.jsonl with the enrichment so the on-disk record is complete
    # (false_kill / late_kill_adverse land in the jsonl, not only the scorecard).
    scored = []
    for summary in per_date:
        enriched = score.score_date(summary, source, cfg)
        _write_jsonl(os.path.join(run_dir, summary["date"], "lifecycles.jsonl"),
                     enriched["lifecycles"])
        report.write_scorecard(os.path.join(run_dir, summary["date"]), enriched, cfg)
        scored.append(enriched)
    agg = report.write_aggregate(run_dir, scored, cfg, run_id=run_id, mode=mode)
    return {"run_id": run_id, "run_dir": run_dir, "mode": mode, "dates": dates,
            "per_date": scored, "aggregate": agg}


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def _parse_safety_nets(raw: Optional[str]) -> tuple:
    if not raw:
        return ("ttl",)
    names = tuple(n.strip() for n in raw.split(",") if n.strip())
    bad = [n for n in names if n not in SAFETY_NETS]
    if bad:
        raise SystemExit(f"unknown safety-net(s) {bad}; allowed: {list(SAFETY_NETS)}")
    return names


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="L1 lifecycle bench (plan 10)")
    ap.add_argument("--dates", required=True, help="comma-separated trade dates YYYY-MM-DD")
    ap.add_argument("--mode", choices=["stub", "real", "rescore"], default="stub")
    ap.add_argument("--run-id", default=None, help="run folder name (default: mode+timestamp)")
    ap.add_argument("--latency-sec", type=int, default=90)
    ap.add_argument("--safety-net", default="ttl",
                    help="comma-separated enabled nets (ttl,acceptance_flip,opposite_extreme)")
    ap.add_argument("--churn-cap", type=int, default=20)
    ap.add_argument("--gate", default="calibrated",
                    choices=["calibrated", "self", "stand-directional"],
                    help="bench-only diagnostic gate source (default calibrated = "
                         "production). Stamped into run metadata + scorecard headers.")
    ap.add_argument("--backend", default=None, choices=["openrouter", "anthropic"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--main-dir", default=None, help="override the main parquet dir")
    ap.add_argument("--source-run", default=None, help="rescore: the run-id to replay")
    ap.add_argument("--runs-root", default=RUNS_ROOT)
    args = ap.parse_args(argv)

    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    cfg = BenchConfig(latency_sec=args.latency_sec,
                      safety_nets=_parse_safety_nets(args.safety_net),
                      churn_cap=args.churn_cap, gate_source=args.gate)
    run_id = args.run_id or f"{args.mode}_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}"

    out = run_bench(dates, args.mode, run_id, cfg, main_dir=args.main_dir,
                    backend_name=args.backend, model=args.model,
                    source_run=args.source_run, runs_root=args.runs_root)
    agg = out["aggregate"]
    fs = agg.get("failsafe_pct")
    fs_str = "n/a" if fs is None else f"{fs}%"
    mh, eh = agg.get("menu_hit", 0), agg.get("escape_hatch", 0)
    mh_ratio = f"{mh / (mh + eh):.2f}" if (mh + eh) else "n/a"
    print(f"[{run_id}] mode={args.mode} gate={args.gate} dates={len(dates)} "
          f"lifecycles={agg.get('n_lifecycles')} coverage={agg.get('coverage_pct')}% "
          f"failsafe={fs_str} menu_hit={mh_ratio} cost=${agg.get('api_cost_usd', 0):.4f}")
    print(f"  -> {out['run_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
