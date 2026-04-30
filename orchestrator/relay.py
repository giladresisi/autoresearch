# orchestrator/relay.py
# Relays signal_smt.py stdout to the output channel and parses SIGNAL/EXIT lines into structured events.
import datetime
import re
from pathlib import Path

from orchestrator.output import OutputChannel

# entry_time group is optional so lines without it (e.g. legacy test fixtures) still parse.
_SIGNAL_RE = re.compile(
    r'\[(\d{2}:\d{2}:\d{2})\] SIGNAL\s+(long|short)\s*\|'
    r'(?:\s*entry_time (\d{2}:\d{2}:\d{2})\s*\|)?'
    r'\s*entry ~([\d.]+).*?\|\s*stop ([\d.]+)\s*\|\s*TP ([\d.]+)\s*\|\s*RR ~([\d.]+)x'
)
_EXIT_RE = re.compile(
    r'\[(\d{2}:\d{2}:\d{2})\] EXIT\s+(\S+)\s*\|'
    r'\s*filled ([\d.]+)\s*\|\s*P&L ([+\-])\$([\d.]+)\s*\|\s*(\d+) MNQ'
)

_MNQ_PNL_PER_POINT = 2.0
_TSV_HEADERS = [
    "entry_time", "entry_price", "direction", "contracts",
    "exit_time", "exit_price", "exit_reason", "pnl_points", "pnl_dollars",
]


class SessionRelay:
    """Relays signal_smt.py stdout lines; parses SIGNAL/EXIT into structured events."""

    def __init__(self, channel: OutputChannel) -> None:
        self._channel = channel
        self._events: list[dict] = []

    def emit(self, line: str) -> None:
        """Write line to output channel and parse if SIGNAL/EXIT."""
        self._channel.write(line if line.endswith("\n") else line + "\n")
        self._try_parse(line)

    def _try_parse(self, line: str) -> None:
        m = _SIGNAL_RE.search(line)
        if m:
            evt: dict = {
                "type": "SIGNAL",
                "time": m.group(1),
                "direction": m.group(2),
                "entry": float(m.group(4)),
                "stop": float(m.group(5)),
                "tp": float(m.group(6)),
                "rr": float(m.group(7)),
            }
            if m.group(3):
                evt["entry_time"] = m.group(3)
            self._events.append(evt)
            return
        m = _EXIT_RE.search(line)
        if m:
            self._events.append({
                "type": "EXIT",
                "time": m.group(1),
                "exit_kind": m.group(2),
                "filled": float(m.group(3)),
                "pnl": float(m.group(4) + m.group(5)),
                "contracts": int(m.group(6)),
            })

    def get_events(self) -> list[dict]:
        return list(self._events)

    def reset(self) -> None:
        self._events.clear()

    def write_trades_tsv(self, path: Path, date: "datetime.date | None" = None) -> None:
        """Write SIGNAL+EXIT pairs to a trades.tsv file matching regression schema."""
        signals = [e for e in self._events if e["type"] == "SIGNAL"]
        exits = [e for e in self._events if e["type"] == "EXIT"]
        date_prefix = str(date) if date is not None else ""
        trades = []
        for sig, ex in zip(signals, exits):
            entry_t = sig.get("entry_time", sig["time"])
            entry_ts = f"{date_prefix}T{entry_t}" if date_prefix else entry_t
            exit_ts = f"{date_prefix}T{ex['time']}" if date_prefix else ex["time"]
            pnl_pts = round(ex["pnl"] / (ex["contracts"] * _MNQ_PNL_PER_POINT), 4)
            trades.append({
                "entry_time":  entry_ts,
                "entry_price": sig["entry"],
                "direction":   sig["direction"],
                "contracts":   ex["contracts"],
                "exit_time":   exit_ts,
                "exit_price":  ex["filled"],
                "exit_reason": ex["exit_kind"],
                "pnl_points":  pnl_pts,
                "pnl_dollars": ex["pnl"],
            })
        lines = ["\t".join(_TSV_HEADERS)]
        for t in trades:
            lines.append("\t".join(str(t[h]) for h in _TSV_HEADERS))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
