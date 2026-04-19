# orchestrator/output.py
# Output channel abstraction: stdout sink, file sink, and fan-out channel.
# Separates output routing from business logic so tests can inject mock sinks.
from pathlib import Path


class StdoutSink:
    def write(self, text: str) -> None:
        print(text, end="", flush=True)


class FileSink:
    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, text: str) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(text)
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
