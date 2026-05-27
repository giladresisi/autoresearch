# orchestrator/output.py
# Output channel abstraction: stdout sink, file sink, and fan-out channel.
# Separates output routing from business logic so tests can inject mock sinks.
import datetime
import json as _json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _et_now() -> str:
    return datetime.datetime.now(tz=_ET).isoformat()


class StdoutSink:
    def write(self, text: str) -> None:
        try:
            print(text, end="", flush=True)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            safe = text.encode(enc, errors="replace").decode(enc)
            try:
                print(safe, end="", flush=True)
            except OSError:
                pass
        except OSError:
            pass


class FileSink:
    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, text: str) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(text)
            f.flush()


class TimestampedFileSink:
    """Like FileSink but prepends [HH:MM:SS ET] to each line."""
    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, text: str) -> None:
        ts = _et_now()
        with open(self._path, "a", encoding="utf-8") as f:
            for line in text.splitlines(keepends=True):
                f.write(f"[{ts}] {line}")
            f.flush()


class JsonlFileSink:
    """Appends lines that are valid JSON objects to a .jsonl file; skips plain-text lines."""
    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, text: str) -> None:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                obj = _json.loads(stripped)
            except (ValueError, _json.JSONDecodeError):
                continue
            kind = obj.pop("kind", None)
            obj.pop("time", None)
            stamped: dict = {}
            if kind is not None:
                stamped["kind"] = kind
            stamped["time"] = _et_now()
            for _pf in ("direction", "side"):
                if _pf in obj:
                    stamped[_pf] = obj.pop(_pf)
            stamped.update(sorted(obj.items()))
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(stamped) + "\n")
                f.flush()


class OutputChannel:
    def __init__(self) -> None:
        self._sinks: list = []

    def add_sink(self, sink) -> None:
        self._sinks.append(sink)

    def write(self, text: str) -> None:
        for sink in self._sinks:
            sink.write(text)

    def writeln(self, text: str) -> None:
        self.write(text + "\n")
