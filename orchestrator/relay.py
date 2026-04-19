# orchestrator/relay.py
# Relays signal_smt.py stdout to the output channel and parses SIGNAL/EXIT lines into structured events.
import re
from orchestrator.output import OutputChannel

_SIGNAL_RE = re.compile(
    r'\[(\d{2}:\d{2}:\d{2})\] SIGNAL\s+(long|short)\s*\|'
    r'\s*entry ~([\d.]+).*?\|\s*stop ([\d.]+)\s*\|\s*TP ([\d.]+)\s*\|\s*RR ~([\d.]+)x'
)
_EXIT_RE = re.compile(
    r'\[(\d{2}:\d{2}:\d{2})\] EXIT\s+(\S+)\s*\|'
    r'\s*filled ([\d.]+)\s*\|\s*P&L ([+\-])\$([\d.]+)\s*\|\s*(\d+) MNQ'
)


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
            self._events.append({
                "type": "SIGNAL",
                "time": m.group(1),
                "direction": m.group(2),
                "entry": float(m.group(3)),
                "stop": float(m.group(4)),
                "tp": float(m.group(5)),
                "rr": float(m.group(6)),
            })
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
