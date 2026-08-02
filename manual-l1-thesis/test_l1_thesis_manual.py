"""Manual/offline test harness for the L1 thesis KB doc
(agent-docs/strategy/decisions/thesis.md) — NOT wired into production.

Builds the point-in-time facts/menu/evidence text for an arbitrary ET timestamp
(agent/bench/facts.py::ParquetFactsSource), assembles a system prompt that swaps
decisions/daily-trend.md + decisions/next-move.md for decisions/thesis.md, and
runs one L1 thesis call through the SAME backend/schema/validator machinery
agent/run_agent.py::decide_thesis uses. The production KB_FILES list in
run_agent.py is completely untouched by this script — thesis.md still isn't
wired into KB_FILES; this is a side-door for testing the doc's content against
real historical moments before that wiring happens.

Usage (run from anywhere — paths below are all relative to this file, not cwd):
    python manual-l1-thesis/test_l1_thesis_manual.py --datetime "2026-07-07 08:30"
    python manual-l1-thesis/test_l1_thesis_manual.py --datetime "2026-07-02 10:15" --backend stub
    python manual-l1-thesis/test_l1_thesis_manual.py --datetime "2026-07-07 08:30" --backend anthropic --model claude-opus-4-1

--backend stub runs offline (no API key, no network) using run_agent's
StubBackend — useful for smoke-testing the plumbing before spending real calls.

Writes two artifacts per run, alongside this script (manual-l1-thesis/):
- <boundary>_<backend>.json — full history, one file per run: boundary,
  backend/model, the thesis JSON, reasoning, call audit (retries/fallback/
  latency/usage), and a short "what actually happened next" MNQ price recap
  for side-by-side review when deciding whether to revise thesis.md.
- latest_explanation.md — the SAME file every run (overwritten each time, not
  accumulated): the decision plus the model's own explanation of why it
  decided that way, in readable form for a quick read after each test.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
_AGENT = os.path.join(REPO_ROOT, "agent")
_BENCH = os.path.join(_AGENT, "bench")
_CONTRACTS = os.path.join(_AGENT, "contracts")
_CALIB = os.path.join(REPO_ROOT, "calibration")
for _p in (_AGENT, _BENCH, _CONTRACTS, _CALIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from facts import ParquetFactsSource  # noqa: E402  (agent/bench/facts.py)
from lifecycle import _SweepTracker  # noqa: E402  (agent/bench/lifecycle.py)
from predicates import MarketView, eval_any  # noqa: E402  (agent/contracts/predicates.py)
from run_agent import (  # noqa: E402  (agent/run_agent.py)
    MAX_TOKENS,
    _derive_thesis_arithmetic,
    _facts_context,
    _run_call,
    _TASK_THESIS,
    make_backend,
)
from schemas import build_thesis_schema, failsafe_thesis  # noqa: E402  (agent/contracts/schemas.py)
from validate_contracts import validate_thesis  # noqa: E402  (agent/contracts/validate_contracts.py)

TZ = "America/New_York"
DOCS_ROOT = os.path.join(REPO_ROOT, "agent-docs", "strategy")

# thesis.md REPLACES decisions/daily-trend.md + decisions/next-move.md; the concept
# docs it assumes (SMT, liquidity/sweep semantics, equilibrium, session structure,
# entry confirmation) are unchanged and still apply. This list is LOCAL to this
# script — it does NOT touch agent/run_agent.py's production KB_FILES tuple.
MANUAL_KB_FILES = (
    "smt.md",
    "liquidity-levels.md",
    "equilibrium.md",
    "session-structure.md",
    "entry-confirmation.md",
    "decisions/thesis.md",
)
_KB_SEP = "\n\n---\n\n"


def build_manual_system_prompt(docs_root: str = DOCS_ROOT) -> str:
    """Same concatenation convention as run_agent.build_system_prompt, but reading
    MANUAL_KB_FILES instead of the production KB_FILES."""
    blocks = []
    for rel in MANUAL_KB_FILES:
        path = os.path.join(docs_root, rel.replace("/", os.sep))
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
        blocks.append(f"# FILE: {rel}\n\n{text}")
    return _KB_SEP.join(blocks)


def price_recap(source: ParquetFactsSource, boundary: pd.Timestamp,
                lookback_hours: float) -> dict:
    """A short 'what actually happened next' MNQ price summary — high/low/close over
    the window following `boundary` — for manual side-by-side review against the
    thesis the model just produced. Not a predicate-firing simulation (that would
    need a full MarketView walk); just a quick visual sanity check."""
    mnq = source._norm_1m["MNQ"]
    window = mnq[(mnq.index > boundary)
                & (mnq.index <= boundary + pd.Timedelta(hours=lookback_hours))]
    if len(window) == 0:
        return {"note": "no bars found after boundary in this window"}
    return {
        "window_end": str(window.index[-1]),
        "open": float(window["open"].iloc[0]),
        "high": float(window["high"].max()),
        "low": float(window["low"].min()),
        "close": float(window["close"].iloc[-1]),
        "n_bars": int(len(window)),
    }


_TF_FREQ = (("1m", None), ("5m", pd.Timedelta(minutes=5)), ("15m", pd.Timedelta(minutes=15)),
           ("1h", pd.Timedelta(hours=1)), ("4h", pd.Timedelta(hours=4)))
_TF_RULE = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h"}


def _completed_closes_by_tf(bars_upto_now: pd.DataFrame, ts: pd.Timestamp) -> dict:
    """Completed closes per timeframe, oldest first, as of wall-clock `ts`.

    DELIBERATELY NOT agent/bench/lifecycle.py's `_closes_by_tf` — that helper is a
    documented bench approximation which treats a still-FORMING higher-timeframe
    bin's most-recent 1m close as if it were a genuine completed close of that
    timeframe (fine for the bench's own bar-close-agnostic safety nets, but wrong
    for verifying whether an n_closes_beyond(tf=5m/15m/1h/4h) predicate actually
    fired). A bin only counts here once `ts` has reached its true close time
    (bin_start + freq) — same completed-bin discipline as
    agent/derive_facts.py::_htf_close_status. Concretely: at 2026-07-01 11:30 the
    forming [11:30,11:35) 5m bin's partial value (just the 11:30 1m close,
    30335.75) was still above a 30321.5 threshold even though that bin's real
    completed close (at 11:34, 30317.25) ends up below it — the lifecycle.py
    approximation would count the partial value and fire 4 minutes early; this
    version waits for the bin to actually close."""
    out = {"1m": list(bars_upto_now["close"].dropna().values)}
    for tf, freq in _TF_FREQ:
        if tf == "1m":
            continue
        bins = bars_upto_now["close"].resample(_TF_RULE[tf]).last().dropna()
        closed_at = bins.index + freq
        out[tf] = list(bins[closed_at <= ts].values)
    return out


def walk_predicates_forward(mnq_bars_after_boundary: pd.DataFrame, levels: dict,
                            thesis: dict, boundary: pd.Timestamp) -> dict:
    """Walk 1m MNQ bars strictly after `boundary` and report the FIRST bar (if any)
    at which the thesis's dol/falsified_if/exhausted_if/recall.events actually
    fire — using the SAME MarketView/eval_any machinery
    (agent/contracts/predicates.py) and the SAME sweep tracker
    (agent/bench/lifecycle.py::_SweepTracker) the bench uses, but with
    completed-bins-only close tracking (see _completed_closes_by_tf) so a
    tf=5m/15m/1h/4h predicate can't fire on a still-forming bar.

    Returns {dol_touched_at, falsified_at, exhausted_at, recall_fired_at,
    first_terminal_event} — all timestamps are the FIRST bar where that event
    was true, or None if it never fired within the walked window.
    first_terminal_event is whichever of dol_touch/exhausted_if/falsified_if
    happened earliest (whichever would have ended the standing thesis first);
    recall_fired_at is reported separately since a recall event governs
    re-asking, not the thesis's falsification/exhaustion fate.
    """
    falsified_if = thesis.get("falsified_if") or []
    exhausted_if = thesis.get("exhausted_if") or []
    recall_events = (thesis.get("recall") or {}).get("events") or []
    dol = thesis.get("dol") or {}
    dol_price = dol.get("price") if isinstance(dol, dict) else None
    bias = thesis.get("bias")

    out = {"dol_touched_at": None, "falsified_at": None, "exhausted_at": None,
          "recall_fired_at": None, "first_terminal_event": None}
    if len(mnq_bars_after_boundary) == 0:
        return out

    tracker = _SweepTracker(levels)
    for ts in mnq_bars_after_boundary.index:
        upto = mnq_bars_after_boundary.loc[:ts]
        bar = upto.iloc[-1]
        tracker.update(bar)
        hi, lo = float(bar["high"]), float(bar["low"])
        mv = MarketView(price=float(bar["close"]), closes_by_tf=_completed_closes_by_tf(upto, ts),
                        swept=tracker.swept_set(), depleted=tracker.depleted_set(),
                        now=ts, since=boundary)

        if out["dol_touched_at"] is None and isinstance(dol_price, (int, float)) \
                and bias in ("UP", "DOWN"):
            touched = (bias == "UP" and hi >= dol_price) or (bias == "DOWN" and lo <= dol_price)
            if touched:
                out["dol_touched_at"] = str(ts)
        if out["exhausted_at"] is None and exhausted_if and eval_any(exhausted_if, mv):
            out["exhausted_at"] = str(ts)
        if out["falsified_at"] is None and falsified_if and eval_any(falsified_if, mv):
            out["falsified_at"] = str(ts)
        if out["recall_fired_at"] is None and recall_events and eval_any(recall_events, mv):
            out["recall_fired_at"] = str(ts)

        # A terminal event already found on an earlier bar -> stop walking; nothing
        # after the FIRST terminal event changes the answer to "what killed this".
        if out["dol_touched_at"] or out["exhausted_at"] or out["falsified_at"]:
            break

    candidates = [(k, v) for k, v in (
        ("exhausted_if", out["exhausted_at"]),
        ("falsified_if", out["falsified_at"]),
        ("dol_touch", out["dol_touched_at"]),
    ) if v]
    if candidates:
        candidates.sort(key=lambda kv: kv[1])
        out["first_terminal_event"] = candidates[0][0]
    return out


def render_explanation_md(result: dict) -> str:
    """Human-readable explanation of one run's decision — overwritten each run
    (latest_explanation.md), so it always reflects the most recent test rather
    than accumulating across every past run (the JSON artifacts are the
    accumulating history)."""
    t = result["thesis"] or {}
    lines = [
        "# L1 thesis — manual test explanation",
        "",
        f"- **Boundary (ET):** {result['boundary']}",
        f"- **Backend / model:** {result['backend']} / {result['model']}",
        f"- **Call verdict:** {result['verdict']}"
        f"{' (FAILSAFE — model output failed validation, this is the failsafe default)' if result['fallback'] else ''}",
        f"- **Retries:** {result['retries']}",
        "",
        "## Decision",
        "",
        f"- **Bias:** {t.get('bias')}",
        f"- **Regime:** {t.get('regime')}",
        f"- **Confidence (self-reported, audit-only):** {t.get('confidence')}",
        f"- **DOL rationale:** {t.get('dol_rationale')}",
        f"- **DOL:** {t.get('dol')}",
        "",
        f"**falsified_if rationale:** {t.get('falsified_if_rationale')}",
        "",
        "**falsified_if:**",
        "```json",
        json.dumps(t.get("falsified_if"), indent=2, default=str),
        "```",
        "",
        f"**exhausted_if rationale:** {t.get('exhausted_if_rationale')}",
        "",
        "**exhausted_if:**",
        "```json",
        json.dumps(t.get("exhausted_if"), indent=2, default=str),
        "```",
        "",
        "**recall:**",
        "```json",
        json.dumps(t.get("recall"), indent=2, default=str),
        "```",
        "",
        "**evidence ledger (P1/P2 only; points/side are code-derived):**",
        "```json",
        json.dumps(t.get("evidence"), indent=2, default=str),
        "```",
        "",
        "## Why the model decided this (its own explanation)",
        "",
        result.get("reasoning") or "_(no reasoning returned — likely a failsafe call)_",
        "",
        "## What actually happened next (for comparison)",
        "",
        "```json",
        json.dumps(result.get("price_recap_after_boundary"), indent=2, default=str),
        "```",
        "",
        "## Predicate walk-forward (did falsified_if / exhausted_if / recall actually fire?)",
        "",
        "Evaluated bar-by-bar against the real MNQ 1m bars after the boundary, using the "
        "same MarketView/eval_any machinery and sweep tracker the bench uses — not a "
        "human eyeballing a high/low/close summary.",
        "",
        "```json",
        json.dumps(result.get("predicate_walk_forward"), indent=2, default=str),
        "```",
        "",
        f"_Full run history (one JSON file per run, never overwritten) lives in "
        f"the same directory as this file._",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Manual test of decisions/thesis.md at an arbitrary point in time")
    ap.add_argument("--datetime", required=True,
                    help="ET timestamp, e.g. '2026-07-07 08:30' (facts are built "
                         "strictly BEFORE this instant — no lookahead)")
    ap.add_argument("--backend", choices=["stub", "openrouter", "anthropic"], default=None,
                    help="default: OpenRouter if OPENROUTER_API_KEY is set, else "
                         "Anthropic; 'stub' runs fully offline")
    ap.add_argument("--model", default=None, help="override the backend's default model")
    ap.add_argument("--main-dir", default=None,
                    help="override ParquetFactsSource's main parquet directory")
    ap.add_argument("--lookback-hours", type=float, default=4.0,
                    help="hours of MNQ price AFTER the boundary to recap for review")
    ap.add_argument("--out-dir", default=HERE,
                    help="directory to write the run's JSON artifact into "
                         "(default: this script's own directory, manual-l1-thesis/)")
    ap.add_argument("--explanation-md", default=None,
                    help="path to the (always-overwritten) explanation markdown file "
                         "(default: <out-dir>/latest_explanation.md)")
    ap.add_argument("--ignore-near-maturity-wait", action="store_true",
                    help="thesis.md §3a: by default, if the requested --datetime has a day/"
                         "week-tier item near its next qualifying HTF close that is NOT "
                         "preconfirm_eligible, this harness simulates the executor's 'wait' "
                         "branch by retargeting the boundary to that close instead of calling "
                         "at the literal requested instant. Pass this flag to disable that and "
                         "always call at exactly --datetime as given.")
    args = ap.parse_args(argv)

    boundary = pd.Timestamp(args.datetime, tz=TZ)

    kwargs = {"main_dir": args.main_dir} if args.main_dir else {}
    source = ParquetFactsSource(**kwargs)

    # ParquetFactsSource.build_facts only checks "is there ANY data before boundary" —
    # not "does data actually extend close to boundary". If the main parquets haven't
    # been refreshed recently, a boundary far past the last real bar would silently
    # reuse stale historical data as if it were a genuine snapshot at `boundary`,
    # mislabeling the whole run. Refuse explicitly instead.
    last_available = min(source._raw_1s[tk].index[-1] for tk in source.tickers)
    if boundary > last_available + pd.Timedelta(minutes=5):
        print(f"REFUSING: --datetime {boundary} is beyond the data's actual coverage "
              f"(last available bar across MNQ/MES: {last_available}). Running anyway "
              f"would silently reuse that stale data and mislabel the result as a "
              f"genuine {boundary} snapshot. Refresh the main parquets (run "
              f"parquet-check / re-sync), or pick a --datetime at or before "
              f"{last_available}.", file=sys.stderr)
        return 1

    res = source.build_facts(boundary)
    if res.degraded:
        print(f"FACTS DEGRADED at {boundary}: {res.error}", file=sys.stderr)
        return 1

    # thesis.md §3a: harness-level simulation of the executor's "wait" branch. No live
    # executor exists yet for this KB (thesis.md isn't wired into a production loop), so this
    # harness is the only place that can actually exercise the behavior end-to-end: if a day/
    # week-tier item is near its next qualifying HTF close but NOT preconfirm_eligible (not
    # distance-safe and/or not corroborated), retarget the boundary to that close rather than
    # calling at the literal requested instant — erring toward "wait when in doubt" rather than
    # silently guessing. A preconfirm_eligible candidate does NOT trigger a wait (the model may
    # act on it now per the §3a prompt guidance); only a non-eligible near-maturity candidate does.
    if not args.ignore_near_maturity_wait:
        pending = [c for c in (res.validator_dict.get("near_maturity_candidates") or [])
                  if not c.get("preconfirm_eligible")]
        if pending:
            retarget = min(pd.Timestamp(c["resolves_at"]) for c in pending)
            print(f"NEAR-MATURITY WAIT: {len(pending)} day/week-tier item(s) near a "
                  f"qualifying HTF close but not preconfirm_eligible at {boundary} — "
                  f"retargeting to {retarget} (thesis.md §3a). Pass "
                  f"--ignore-near-maturity-wait to call at the literal requested instant.")
            boundary = retarget
            res = source.build_facts(boundary)
            if res.degraded:
                print(f"FACTS DEGRADED at retargeted {boundary}: {res.error}", file=sys.stderr)
                return 1

    system = build_manual_system_prompt()
    facts_text = "\n\n".join([res.text, res.menu_text, res.evidence_text])
    user = _facts_context(facts_text, "") + _TASK_THESIS
    facts = res.validator_dict  # already carries "menus" (bench/facts.py wiring)

    backend = make_backend(args.backend, args.model)

    # Level-name enum constraint (schemas.build_thesis_schema) — mirrors decide_thesis
    # exactly; this harness bypasses decide_thesis and must build its own schema the same
    # way or it silently loses the constraint (bug: it did, for every run before this).
    valid_levels = list((facts.get("levels") or {}).keys())
    thesis_schema = build_thesis_schema(valid_levels,
                                        extra_evidence_levels=facts.get("fvg_zones"))

    menus = facts.get("menus")
    dol_available = None
    if menus is not None:
        dol_menu = menus.get("dol") or {}
        dol_available = {"UP": bool(dol_menu.get("UP")), "DOWN": bool(dol_menu.get("DOWN"))}
    suppressed_p1_levels = facts.get("suppressed_p1_levels")
    suppressed_p2_sites = facts.get("suppressed_p2_sites")
    level_htf_close_status = facts.get("level_htf_close_status")
    level_tiers = facts.get("level_tiers")
    smt_candidates = facts.get("smt_candidates")
    week_extremes = facts.get("week_extremes")
    now_price = facts.get("now_price")
    fvg_zone_meta = facts.get("fvg_zone_meta")

    outcome = _run_call(
        backend, system, user, thesis_schema,
        validate_block=lambda d: validate_thesis(d, facts),
        failsafe_block=failsafe_thesis(),
        # plan 14 Task 6: mirror run_bench — code magnitude-scales the declared ledger from
        # the same facts bundle's evidence_magnitude (facts_text already carries S9 above).
        # dol_available mirrors decide_thesis's no-liquidity override (thesis.md §8).
        # suppressed_p1_levels/suppressed_p2_sites mirror decide_thesis's nested/duplicate-
        # sweep P1/P2 backstops (thesis.md §2.1b/§2.1d). 2026-08-02: level_htf_close_status/
        # level_tiers/smt_candidates/week_extremes/now_price/fvg_zone_meta mirror decide_thesis's
        # P1/P2/P3 auto-derivation + §2.1e tier promotion + extremity-based dominance
        # resolution + P5 same-move dedup -- this harness bypasses decide_thesis and must
        # mirror its wiring exactly or the PERSISTED evidence/points silently omit everything
        # decide_thesis's own derive_block would apply (bias still validates correctly via
        # validate_thesis, which gets the full `facts` dict independently, but the evidence
        # array + confidence-ceiling clamp computed here would not).
        derive_block=lambda d: _derive_thesis_arithmetic(
            d, magnitude=res.evidence_magnitude, dol_available=dol_available,
            suppressed_p1_levels=suppressed_p1_levels, suppressed_p2_sites=suppressed_p2_sites,
            level_htf_close_status=level_htf_close_status, level_tiers=level_tiers,
            smt_candidates=smt_candidates, week_extremes=week_extremes, now_price=now_price,
            fvg_zone_meta=fvg_zone_meta),
    )

    recap = price_recap(source, boundary, args.lookback_hours)

    mnq_after = source._norm_1m["MNQ"]
    walk_window = mnq_after[(mnq_after.index > boundary)
                           & (mnq_after.index <= boundary + pd.Timedelta(hours=args.lookback_hours))]
    walk = walk_predicates_forward(walk_window, res.levels, outcome.block, boundary)

    result = {
        "boundary": str(boundary),
        "backend": backend.name,
        "model": backend.model,
        "verdict": outcome.verdict,
        "fallback": outcome.fallback,
        "retries": outcome.retries,
        "thesis": outcome.block,
        "reasoning": outcome.reasoning,
        "latency_total_sec": outcome.latency_total,
        "usage_total": outcome.usage_total,
        "attempts": outcome.attempts,
        "price_recap_after_boundary": recap,
        "predicate_walk_forward": walk,
    }

    os.makedirs(args.out_dir, exist_ok=True)
    tag = str(boundary).replace(" ", "_").replace(":", "").replace("-", "")
    out_path = os.path.join(args.out_dir, f"{tag}_{backend.name}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)

    explanation_path = args.explanation_md or os.path.join(args.out_dir, "latest_explanation.md")
    os.makedirs(os.path.dirname(os.path.abspath(explanation_path)), exist_ok=True)
    with open(explanation_path, "w", encoding="utf-8") as fh:
        fh.write(render_explanation_md(result))

    t = result["thesis"]
    print(f"{boundary} [{backend.name}:{backend.model}] "
          f"bias={t.get('bias')} regime={t.get('regime')} "
          f"confidence={t.get('confidence')} verdict={outcome.verdict} "
          f"fallback={outcome.fallback}")
    print(f"dol={t.get('dol')}")
    print(f"price after +{args.lookback_hours}h: {recap}")
    print(f"predicate walk-forward: {walk}")
    print(f"full result written to {out_path}")
    print(f"explanation written to {explanation_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
