# orchestrator/relay.py
# Relays automation.main (V2) or signal_smt.py (V1) stdout to the output channel
# and parses entry/exit events into structured trade records for trades.tsv.
import datetime
import json
import re
from pathlib import Path

from orchestrator.output import OutputChannel

# ── V1 (signal_smt.py) regex patterns ────────────────────────────────────────
_SIGNAL_RE = re.compile(
    r'SIGNAL\s+(long|short)\s*\|'
    r'(?:\s*entry_time (\d{2}:\d{2}:\d{2})\s*\|)?'
    r'\s*entry ~([\d.]+).*?\|\s*stop ([\d.]+)\s*\|\s*TP ([\d.]+)\s*\|\s*RR ~([\d.]+)x'
)
_EXIT_RE = re.compile(
    r'EXIT\s+(\S+)\s*\|'
    r'\s*bar_time (\d{2}:\d{2}:\d{2})\s*\|'
    r'\s*filled ([\d.]+)\s*\|\s*P&L ([+\-])\$([\d.]+)\s*\|\s*(\d+) MNQ'
)

# ── V2 (automation.main) JSON event kinds ────────────────────────────────────
_V2_ENTRY_KINDS = {"stop-entry-filled", "market-entry"}
_V2_EXIT_KINDS  = {"stopped-out", "stop-exit"}

_MNQ_PNL_PER_POINT = 2.0
_TSV_HEADERS = [
    "entry_time", "entry_price", "direction", "contracts",
    "exit_time", "exit_price", "exit_reason", "pnl_points", "pnl_dollars",
]


def _iso_to_hms(iso: str) -> str:
    """Extract HH:MM:SS from an ISO timestamp string."""
    try:
        return iso[11:19]
    except Exception:
        return ""


def _normalize_direction(raw: str) -> str:
    return "long" if raw in ("up", "long") else "short" if raw in ("down", "short") else raw


class SessionRelay:
    """Relays automation.main stdout; parses entry/exit events into structured trade records."""

    def __init__(self, channel: OutputChannel) -> None:
        self._channel = channel
        self._events: list[dict] = []

    def emit(self, line: str) -> None:
        """Write line to output channel and parse if entry/exit event."""
        self._channel.write(line if line.endswith("\n") else line + "\n")
        self._try_parse(line)

    def _try_parse(self, line: str) -> None:
        # ── V1: formatted text lines ──────────────────────────────────────────
        m = _SIGNAL_RE.search(line)
        if m:
            evt: dict = {
                "type":      "SIGNAL",
                "time":      m.group(2) or "",
                "direction": m.group(1),
                "entry":     float(m.group(3)),
                "stop":      float(m.group(4)),
                "tp":        float(m.group(5)),
                "rr":        float(m.group(6)),
            }
            if m.group(2):
                evt["entry_time"] = m.group(2)
            self._events.append(evt)
            return
        m = _EXIT_RE.search(line)
        if m:
            self._events.append({
                "type":      "EXIT",
                "time":      m.group(2),
                "exit_kind": m.group(1),
                "filled":    float(m.group(3)),
                "pnl":       float(m.group(4) + m.group(5)),
                "contracts": int(m.group(6)),
            })
            return

        # ── V2: JSON event lines from automation.main ─────────────────────────
        stripped = line.strip()
        if not stripped.startswith("{"):
            return
        try:
            evt = json.loads(stripped)
        except json.JSONDecodeError:
            return

        kind = evt.get("kind")
        if kind in _V2_ENTRY_KINDS:
            # stop-entry-filled: {"price": fill, "stop": sl, "direction": "up"|"down", "time": ISO}
            # market-entry:      {"entry_price": fill, "stop_price": sl, "direction": ..., "time": ISO}
            iso_time = evt.get("time", "")
            self._events.append({
                "type":       "SIGNAL",
                "time":       _iso_to_hms(iso_time),
                "entry_time": _iso_to_hms(iso_time),
                "entry_ts":   iso_time,
                "direction":  _normalize_direction(evt.get("direction", "")),
                "entry":      float(evt.get("price") or evt.get("entry_price", 0)),
                "stop":       float(evt.get("stop") or evt.get("stop_price", 0)),
            })
        elif kind in _V2_EXIT_KINDS:
            # stopped-out: {"price": exit_price, "direction": ..., "time": ISO}
            # stop-exit:   {"price": exit_price, "reason": "...", "direction": ..., "time": ISO}
            iso_time = evt.get("time", "")
            reason = evt.get("reason", kind) if kind == "stop-exit" else kind
            self._events.append({
                "type":      "EXIT",
                "time":      _iso_to_hms(iso_time),
                "exit_ts":   iso_time,
                "exit_kind": reason,
                "filled":    float(evt.get("price", 0)),
                "pnl":       None,   # computed from entry/exit prices when pairing
                "contracts": None,   # resolved from env at write time
            })

    def get_events(self) -> list[dict]:
        return list(self._events)

    def reset(self) -> None:
        self._events.clear()

    def write_trades_tsv(self, path: Path, date: "datetime.date | None" = None) -> None:
        """Write SIGNAL+EXIT pairs to a trades.tsv file matching regression schema."""
        import os
        contracts_default = int(os.environ.get("TRADING_CONTRACTS", "1"))

        signals = [e for e in self._events if e["type"] == "SIGNAL"]
        exits   = [e for e in self._events if e["type"] == "EXIT"]
        date_prefix = str(date) if date is not None else ""
        trades = []
        for sig, ex in zip(signals, exits):
            # Prefer full ISO timestamp stored in entry_ts/exit_ts (V2); fall back to
            # date_prefix + HH:MM:SS assembly (V1).
            if sig.get("entry_ts"):
                entry_ts = sig["entry_ts"]
            else:
                entry_t  = sig.get("entry_time", sig["time"])
                entry_ts = f"{date_prefix}T{entry_t}" if date_prefix else entry_t

            if ex.get("exit_ts"):
                exit_ts = ex["exit_ts"]
            else:
                exit_ts = f"{date_prefix}T{ex['time']}" if date_prefix else ex["time"]

            contracts  = ex["contracts"] if ex["contracts"] is not None else contracts_default
            entry_price = sig["entry"]
            exit_price  = ex["filled"]

            if ex["pnl"] is not None:
                # V1: PnL already in exit record
                pnl_dollars = ex["pnl"]
            else:
                # V2: compute from prices
                sign        = 1.0 if sig["direction"] == "long" else -1.0
                pnl_dollars = round((exit_price - entry_price) * sign * contracts * _MNQ_PNL_PER_POINT, 2)

            pnl_pts = round(pnl_dollars / (contracts * _MNQ_PNL_PER_POINT), 4)

            trades.append({
                "entry_time":  entry_ts,
                "entry_price": entry_price,
                "direction":   sig["direction"],
                "contracts":   contracts,
                "exit_time":   exit_ts,
                "exit_price":  exit_price,
                "exit_reason": ex["exit_kind"],
                "pnl_points":  pnl_pts,
                "pnl_dollars": pnl_dollars,
            })
        lines = ["\t".join(_TSV_HEADERS)]
        for t in trades:
            lines.append("\t".join(str(t[h]) for h in _TSV_HEADERS))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
