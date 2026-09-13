"""Post-hoc sweep validator (GIL-44 Phase 1).

Reads the freeform `poc-result.md` decision reports the calibration sweep
produces, parses each into the canonical structured decision the reusable
`validator` module checks, and reports per-run `protocol_clean` (sweep mode:
the scorer can then report both the raw and the protocol-clean curves).

Design: the parser is deliberately CONSERVATIVE — it extracts only what it can
read unambiguously (directions/confidences from the RESULT block or the output
schema, the driver audit table's integer votes/contributions, and the net-score
lines). Anything it cannot parse is left ABSENT, and `validator.validate` skips
checks whose inputs are missing — so a parse gap can never manufacture a false
positive. Ledger multiplier arithmetic and full semantics belong to the Phase-2
runner (which emits JSON directly) and the Phase-3 orchestrator; this post-hoc
tool guards the machine-readable protocol surface of the existing reports.

Usage:
    python validate_results.py                 # sweep every calibration/cuts/*/
    python validate_results.py path/to/report.md [more.md ...]
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT_DIR = os.path.join(os.path.dirname(HERE), "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

from validator import DRIVER_WEIGHTS, ValidationResult, validate

_ENUM_DIR = r"(up|down|neutral)"
_ENUM_CONF = r"(high|medium|low)"
_ENUM_REGIME = r"(trend|range|hybrid)"


# --------------------------------------------------------------------------- #
# Number helpers                                                               #
# --------------------------------------------------------------------------- #
def _clean(text: str) -> str:
    """Normalise markdown/unicode noise: bold, backticks, unicode minus."""
    return (text.replace("**", "").replace("`", "")
            .replace("−", "-").strip())


def _num(cell: str) -> float | None:
    """Return the float value iff the whole cell is a single number, else None."""
    s = _clean(cell)
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", s):
        return float(s)
    return None


def _first_number(text: str) -> float | None:
    m = re.search(r"[+-]?\d+(?:\.\d+)?", _clean(text))
    return float(m.group(0)) if m else None


def _tidy_vote(v: float) -> float | int:
    return int(v) if float(v).is_integer() else v


# --------------------------------------------------------------------------- #
# Report parsing                                                               #
# --------------------------------------------------------------------------- #
def parse_report(text: str) -> dict:
    """Parse a freeform poc-result.md into the canonical decision dict."""
    daily_region, next_region = _split_regions(text)
    result_block = _result_block(text)

    daily = _parse_daily(daily_region, result_block)
    nxt = _parse_next(next_region, result_block)
    return {"daily_trend": daily, "next_move": nxt}


def _split_regions(text: str) -> tuple[str, str]:
    """Split at the next-move SECTION boundary so daily/next fields don't cross-talk.

    Anchored on a section header or the `next_move:` schema label — NOT on any
    prose mention of "next-move" (which appears inside the daily driver audit).
    """
    starts = []
    for pat in (r"(?im)^#{1,6}\s+[^\n]*next[_ -]?move",   # "## Section 2 — Next-move"
                r"(?im)^\s*#{0,6}\s*section\s*2\b",         # "## SECTION 2: ..."
                r"(?im)^\s*next_move\s*:"):                 # schema label
        m = re.search(pat, text)
        if m:
            starts.append(m.start())
    if not starts:
        return text, ""
    split = min(starts)
    return text[:split], text[split:]


def _result_block(text: str) -> str:
    idx = text.rfind("RESULT:")
    return text[idx:] if idx != -1 else ""


def _find_enum(region: str, key: str, enum: str) -> str | None:
    m = re.search(rf"(?im)^\s*{key}\s*:\s*{enum}\b", region)
    return m.group(1).lower() if m else None


def _result_value(result_block: str, key: str) -> str | None:
    """Value of a `key: ...` line in the RESULT block; 'none'/empty -> None."""
    m = re.search(rf"(?im)^\s*{key}\s*:\s*(.+)$", result_block)
    if not m:
        return None
    val = _clean(m.group(1))
    return val if val and val.lower() != "none" else None


def _regime_under_heading(region: str) -> str | None:
    """Regime stated in prose under a '### Regime' heading (e.g. 'Classification:
    **HYBRID** (range-leaning)'). Extract only unambiguous anchors — a bare enum
    word in prose ('range-characteristic') must not win."""
    m = re.search(r"(?im)^#{1,6}[^\n]*\bregime\b", region)
    if not m:
        return None
    tail = region[m.end():m.end() + 400]
    for pat in (rf"(?i)classification\s*:[^\n]*?\b{_ENUM_REGIME}\b",
                rf"(?i)\*\*\s*{_ENUM_REGIME}\s*\**"):
        e = re.search(pat, tail)
        if e:
            return e.group(1).lower()
    return None


def _has(region: str, pattern: str) -> bool:
    return re.search(pattern, region, re.IGNORECASE) is not None


def _parse_daily(region: str, result_block: str) -> dict:
    direction = (_find_enum(result_block, "daily_direction", _ENUM_DIR)
                 or _find_enum(region, "direction", _ENUM_DIR))
    confidence = (_find_enum(result_block, "daily_confidence", _ENUM_CONF)
                  or _find_enum(region, "confidence", _ENUM_CONF))
    regime = (_find_enum(result_block, "daily_regime", _ENUM_REGIME)
              or _find_enum(region, "regime", _ENUM_REGIME)
              or _regime_under_heading(region))

    daily: dict = {"direction": direction, "confidence": confidence, "regime": regime,
                   "drivers": _parse_drivers(region)}
    if (_has(region, r"weakens[ _]to[ _]neutral[ _]?if")
            or _result_value(result_block, "weakens_to_neutral_if")):
        daily["weakens_to_neutral_if"] = "present"
    if (_has(region, r"flips?[ _](?:to[ _]\w+[ _])?if\b")
            or _result_value(result_block, "flips_if")):
        daily["flips_if"] = "present"
    if (_has(region, r"day_dol\s*:") or _has(region, r"\(DOL\)")
            or _has(region, r"draw[- ]on[- ]liquidity")
            or _result_value(result_block, "day_dol")):
        daily["day_dol"] = "present"
    cp = re.search(r"(?im)checkpoint[^\n]*?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", region)
    if cp:
        daily["checkpoint"] = cp.group(1)
    return daily


def _parse_next(region: str, result_block: str) -> dict:
    direction = (_find_enum(result_block, "next_direction", _ENUM_DIR)
                 or _find_enum(region, "direction", _ENUM_DIR))
    confidence = (_find_enum(result_block, "next_confidence", _ENUM_CONF)
                  or _find_enum(region, "confidence", _ENUM_CONF))

    nxt: dict = {"direction": direction, "confidence": confidence}

    arm = re.search(r"(?im)^\s*arm_entry_confirmation\s*:\s*(yes|no)\b",
                    result_block or region)
    if not arm:
        arm = re.search(r"(?im)^\s*arm_entry_confirmation\s*:\s*(yes|no)\b", region)
    if not arm:
        # heading style: "### arm_entry_confirmation" then "**no** — ..." below
        arm = re.search(r"(?im)^#{1,6}[^\n]*arm[ _]?entry[ _]?confirmation[^\n]*"
                        r"\n+\s*\**\s*(yes|no)\b", region)
    if arm:
        nxt["arm_entry_confirmation"] = arm.group(1).lower()

    # Net-score line: capital-N "N = A - B = <value>" (>=2 '='). The capital N and
    # the second '=' disqualify prose like "acceptance ckpt->now (n=220)".
    for line in region.splitlines():
        if line.count("=") >= 2 and re.search(r"\bN\s*=", line):
            n = _first_number(line.rsplit("=", 1)[-1])
            if n is not None:
                nxt["N"] = n
                break

    if direction == "neutral":
        if _has(region, r"long[ _]+if") and _has(region, r"short[ _]+if"):
            nxt["resolution"] = {"long_if": _parse_resolution(region, "long"),
                                 "short_if": _parse_resolution(region, "short")}
    elif direction in ("up", "down"):
        nxt["move_target"] = _parse_move_target(region)
    return nxt


def _parse_resolution(region: str, side: str) -> dict:
    m = re.search(rf"(?im)^\s*{side}[ _]+if\s*:?\s*(.*)$", region)
    price = _first_number(m.group(1)) if m else None
    return {"condition": "present", "price": price, "target": None}


def _parse_move_target(region: str) -> dict | None:
    m = re.search(r"(?im)^\s*move_target\s*:\s*(.*)$", region)
    if not m:
        return None
    tail = _clean(m.group(1))
    price = _first_number(tail)
    level = None
    lvl = re.search(r"(?im)^\s*level\s*:\s*(.+)$", region[m.start():m.start() + 300])
    if lvl:
        level = _clean(lvl.group(1))
    if price is None:
        pr = re.search(r"(?im)^\s*price\s*:\s*(.+)$", region[m.start():m.start() + 300])
        price = _first_number(pr.group(1)) if pr else None
    if not tail and level is None and price is None:
        return {"level": "present", "price": None}   # nested schema we couldn't read fully
    return {"level": level or tail or "present", "price": price}


def _parse_drivers(region: str) -> list[dict]:
    """Extract D1..D6 votes/weights/contributions from the driver audit table.

    Column order varies across reports (weight may live in the driver name, or be
    a column before/after the contribution). We anchor on the KNOWN doc weight per
    driver and read only cells that are a pure number, then resolve the smallest-
    magnitude as the vote and the largest as the contribution.
    """
    seen: dict[str, dict] = {}
    for line in region.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        m = re.match(r"\**\s*(D[1-6])\b", _clean(cells[0]))
        if not m:
            continue
        did = m.group(1)
        if did in seen:
            continue
        pure = [v for v in (_num(c) for c in cells) if v is not None]
        weight = DRIVER_WEIGHTS[did]
        if len(pure) > 2:
            for i, v in enumerate(pure):
                if abs(v - weight) < 1e-9:
                    pure.pop(i)
                    break
        if not pure:
            continue
        vote = min(pure, key=abs)
        contribution = max(pure, key=abs)
        seen[did] = {"id": did, "vote": _tidy_vote(vote),
                     "weight": weight, "contribution": contribution}
    return [seen[d] for d in DRIVER_WEIGHTS if d in seen]


# --------------------------------------------------------------------------- #
# Facts parsing (minimal — enables the semantic layer when a fact sheet exists)#
# --------------------------------------------------------------------------- #
def parse_facts(text: str) -> dict:
    facts: dict = {"now_price": None, "checkpoint": None, "whipsaw": None, "levels": {}}

    cp = re.search(r"(?im)checkpoint[^\n]*?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", text)
    if cp:
        facts["checkpoint"] = cp.group(1)

    # First "last close:" in the sheet is the decision ticker (MNQ, S1).
    lc = re.search(r"(?im)^\s*last close\s*:\s*([\d.]+)", text)
    if lc:
        facts["now_price"] = float(lc.group(1))

    ws = re.search(r"(?im)whipsaw window\s*:\s*(True|False)", text)
    if ws:
        facts["whipsaw"] = ws.group(1) == "True"

    # S2 MNQ sweep lines: "<name> <price> [above|below]: <rest>"
    for line in text.splitlines():
        lm = re.match(r"^([A-Za-z][\w()]*)\s+([\d.]+)\s+\[(above|below)\]\s*:\s*(.*)$", line)
        if not lm:
            continue
        name, price, side, rest = lm.groups()
        if name in facts["levels"]:
            continue
        swept = ("swept" in rest.lower()) and ("not swept" not in rest.lower())
        depleted = "DEPLETED" in rest
        facts["levels"][name] = {"price": float(price), "side": side,
                                 "swept": swept, "depleted": depleted}
    return facts


# --------------------------------------------------------------------------- #
# Driver                                                                        #
# --------------------------------------------------------------------------- #
def validate_report(report_path: str, facts_path: str | None = None) -> ValidationResult:
    with open(report_path, encoding="utf-8", errors="replace") as fh:
        decision = parse_report(fh.read())
    facts = None
    if facts_path and os.path.exists(facts_path):
        with open(facts_path, encoding="utf-8", errors="replace") as fh:
            facts = parse_facts(fh.read())
    return validate(decision, facts=facts)


def _iter_cut_reports() -> list[str]:
    cuts = os.path.join(HERE, "cuts")
    out = []
    if os.path.isdir(cuts):
        for cid in sorted(os.listdir(cuts)):
            p = os.path.join(cuts, cid, "poc-result.md")
            if os.path.exists(p):
                out.append(p)
    return out


def main(argv: list[str]) -> int:
    reports = argv or _iter_cut_reports()
    if not reports:
        print("no reports found (pass paths, or run after the sweep produces "
              "calibration/cuts/*/poc-result.md)")
        return 0

    clean = 0
    for path in reports:
        facts_path = os.path.join(os.path.dirname(path), "facts.txt")
        result = validate_report(path, facts_path if os.path.exists(facts_path) else None)
        label = os.path.relpath(path, HERE)
        if result.ok:
            clean += 1
            print(f"protocol_clean=true   {label}")
        else:
            print(f"protocol_clean=false  {label}")
            for v in result.violations:
                print(f"    {v}")
    print(f"\n{clean}/{len(reports)} reports protocol_clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
