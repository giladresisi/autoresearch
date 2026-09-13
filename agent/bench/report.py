"""Scorecard + aggregate rendering (plan 10 Phase 3).

`write_scorecard` renders the per-date scorecard.md (per-day metrics + the per-lifecycle
table from the design). `write_aggregate` rolls the run up into aggregate.md + aggregate.tsv
with the per-regime split. Both are deterministic over the scored input (no wall-clock, no
run-id inside the per-date scorecard) so `rescore` is byte-stable.
"""

from __future__ import annotations

import os
import statistics


def _fmt(v, dash="-"):
    if v is None:
        return dash
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _metrics_lines(m: dict) -> list:
    return [
        f"- lifecycles: {m['n_lifecycles']}  (directional standing: {m['n_directional']})",
        f"- completion rate (directional): {_fmt(m['completion_rate'])}"
        f"  ({m['n_completed']}/{m['n_directional']})",
        f"- suspect completions (race artifacts): {m.get('suspect_completion_count', 0)}",
        f"- false-kill count: {m['false_kill_count']}",
        f"- median late-kill adverse (pts): {_fmt(m['median_late_kill_adverse'])}",
        f"- churn (L1 calls): {m['churn']}",
        f"- coverage (session min under a standing thesis): {_fmt(m['coverage_pct'])}%",
        f"- failsafe rate: {_fmt(m['failsafe_rate'])}",
        f"- menu-hit ratio (predicates): {_fmt(m.get('menu_hit_ratio'))}  "
        f"(menu-hit {m.get('menu_hit', 0)} / escape-hatch {m.get('escape_hatch', 0)})",
        f"- DOL menu-hit rate (directional calls): {_fmt(m.get('dol_menu_hit_rate'))}",
        f"- API cost: ${m['api_cost_usd']:.6f}  "
        f"(tok in {m['tokens_in']} / out {m['tokens_out']} / cache-read {m['cache_read']})",
        f"- per-call latency (s): mean {_fmt(m['latency_mean'])} / "
        f"median {_fmt(m['latency_median'])}",
    ]


_LC_COLS = ("decision_id", "cause", "cause_detail", "bias", "gate", "self_report",
            "born_ts", "arrival_ts", "died_ts", "time_alive_min", "arrival_price",
            "dol_level", "dol_price", "mfe", "mae", "dist_to_dol_pct",
            "false_kill", "lookahead_truncated", "late_kill_adverse", "suspect_completion")


def _lifecycle_table(lifecycles: list) -> list:
    lines = ["| " + " | ".join(_LC_COLS) + " |",
             "|" + "|".join(["---"] * len(_LC_COLS)) + "|"]
    for lc in lifecycles:
        cells = []
        for c in _LC_COLS:
            v = lc.get(c)
            if c.endswith("_ts") and isinstance(v, str) and len(v) >= 19:
                v = v[11:19]                     # HH:MM:SS — the date is in the header
            cells.append(_fmt(v))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def write_scorecard(date_dir: str, enriched: dict, cfg) -> str:
    os.makedirs(date_dir, exist_ok=True)
    m = enriched["metrics"]
    gate_src = m.get("gate_source") or getattr(cfg, "gate_source", "calibrated")
    gate_note = "" if gate_src == "calibrated" else "  **[DIAGNOSTIC GATE]**"
    lines = [
        f"# Bench scorecard — {enriched['date']} ({enriched['regime']})",
        "",
        f"Gate source: **{gate_src}**{gate_note}",
        "",
        f"Day outcome: **{enriched['day_outcome']}**  ·  "
        f"session {enriched['session_open'][11:16]}→{enriched['session_end'][11:16]} "
        f"({enriched['session_minutes']:g} min)",
        "",
        "## Per-day metrics",
        *_metrics_lines(m),
        "",
        "## Lifecycles",
    ]
    if enriched["lifecycles"]:
        lines += _lifecycle_table(enriched["lifecycles"])
    else:
        lines.append("(no lifecycles)")
    lines.append("")
    text = "\n".join(lines)
    path = os.path.join(date_dir, "scorecard.md")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


_AGG_COLS = ("date", "regime", "day_outcome", "n_lifecycles", "n_directional",
             "n_completed", "completion_rate", "false_kill_count",
             "median_late_kill_adverse", "churn", "coverage_pct", "failsafe_rate",
             "api_cost_usd", "latency_mean")


def _agg_over(scored: list) -> dict:
    """Aggregate metrics across a set of scored days (weighted where it makes sense)."""
    n_life = sum(s["metrics"]["n_lifecycles"] for s in scored)
    n_dir = sum(s["metrics"]["n_directional"] for s in scored)
    n_comp = sum(s["metrics"]["n_completed"] for s in scored)
    fk = sum(s["metrics"]["false_kill_count"] for s in scored)
    cost = sum(s["metrics"]["api_cost_usd"] for s in scored)
    cov = [s["metrics"]["coverage_pct"] for s in scored]
    fr = [s["metrics"]["failsafe_rate"] for s in scored
          if s["metrics"]["failsafe_rate"] is not None]
    late = []
    for s in scored:
        for lc in s["lifecycles"]:
            if lc.get("cause") == "falsified" and isinstance(
                    lc.get("late_kill_adverse"), (int, float)):
                late.append(lc["late_kill_adverse"])
    return {
        "n_days": len(scored), "n_lifecycles": n_life, "n_directional": n_dir,
        "n_completed": n_comp,
        "completion_rate": round(n_comp / n_dir, 3) if n_dir else None,
        "false_kill_count": fk,
        "median_late_kill_adverse": round(statistics.median(late), 2) if late else None,
        "churn": sum(s["metrics"]["churn"] for s in scored),
        "coverage_pct": round(statistics.mean(cov), 1) if cov else 0.0,
        "failsafe_pct": round(statistics.mean(fr) * 100.0, 1) if fr else None,
        "api_cost_usd": round(cost, 6),
        "menu_hit": sum(s["metrics"].get("menu_hit", 0) for s in scored),
        "escape_hatch": sum(s["metrics"].get("escape_hatch", 0) for s in scored),
    }


def write_aggregate(run_dir: str, scored: list, cfg, *, run_id: str = "",
                    mode: str = "") -> dict:
    os.makedirs(run_dir, exist_ok=True)
    overall = _agg_over(scored)

    # aggregate.tsv — one row per date (machine-readable; no run-varying fields).
    tsv_lines = ["\t".join(_AGG_COLS)]
    for s in sorted(scored, key=lambda x: x["date"]):
        m = s["metrics"]
        row = {"date": s["date"], "regime": s["regime"],
               "day_outcome": s["day_outcome"], **m}
        tsv_lines.append("\t".join(_fmt(row.get(c), dash="") for c in _AGG_COLS))
    with open(os.path.join(run_dir, "aggregate.tsv"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write("\n".join(tsv_lines) + "\n")

    # per-regime split
    regimes = {}
    for s in scored:
        regimes.setdefault(s["regime"], []).append(s)
    regime_lines = ["| regime | days | lifecycles | completion | false_kill | "
                    "coverage% | failsafe% |",
                    "|---|---|---|---|---|---|---|"]
    for reg in sorted(regimes):
        a = _agg_over(regimes[reg])
        regime_lines.append(
            f"| {reg} | {a['n_days']} | {a['n_lifecycles']} | "
            f"{_fmt(a['completion_rate'])} | {a['false_kill_count']} | "
            f"{_fmt(a['coverage_pct'])} | {_fmt(a['failsafe_pct'])} |")

    gate_src = getattr(cfg, "gate_source", "calibrated")
    gate_note = "" if gate_src == "calibrated" else "  **[DIAGNOSTIC — not production config]**"
    md = [
        f"# Bench aggregate — {run_id} ({mode})",
        "",
        f"Gate source: **{gate_src}**{gate_note}",
        f"Dates: {', '.join(sorted(s['date'] for s in scored))}",
        "",
        "## Overall",
        f"- days: {overall['n_days']}  ·  lifecycles: {overall['n_lifecycles']}  "
        f"·  directional standing: {overall['n_directional']}",
        f"- completion rate (directional): {_fmt(overall['completion_rate'])}",
        f"- false-kill count: {overall['false_kill_count']}",
        f"- median late-kill adverse (pts): {_fmt(overall['median_late_kill_adverse'])}",
        f"- churn (total L1 calls): {overall['churn']}",
        f"- coverage (mean of per-day): {_fmt(overall['coverage_pct'])}%",
        f"- failsafe rate (mean of per-day): {_fmt(overall['failsafe_pct'])}%",
        f"- menu-hit ratio (predicates): "
        f"{_fmt(round(overall['menu_hit'] / (overall['menu_hit'] + overall['escape_hatch']), 3) if (overall['menu_hit'] + overall['escape_hatch']) else None)}"
        f"  (menu-hit {overall['menu_hit']} / escape-hatch {overall['escape_hatch']})",
        f"- API cost (total): ${overall['api_cost_usd']:.6f}",
        "",
        "## Per-regime split",
        *regime_lines,
        "",
        "## Per-date",
        "| date | regime | outcome | lifecycles | completion | false_kill | "
        "coverage% | failsafe | cost$ |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in sorted(scored, key=lambda x: x["date"]):
        m = s["metrics"]
        md.append(
            f"| {s['date']} | {s['regime']} | {s['day_outcome']} | {m['n_lifecycles']} | "
            f"{_fmt(m['completion_rate'])} | {m['false_kill_count']} | "
            f"{_fmt(m['coverage_pct'])} | {_fmt(m['failsafe_rate'])} | "
            f"{m['api_cost_usd']:.4f} |")
    md.append("")
    with open(os.path.join(run_dir, "aggregate.md"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write("\n".join(md))

    return {**overall, "coverage_pct": overall["coverage_pct"],
            "failsafe_pct": overall["failsafe_pct"], "gate_source": gate_src}


