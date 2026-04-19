from pathlib import Path
from unittest.mock import MagicMock

from orchestrator.output import FileSink, OutputChannel, StdoutSink


def test_stdout_sink_writes_to_stdout(capsys):
    StdoutSink().write("hello")
    captured = capsys.readouterr()
    assert captured.out == "hello"


def test_file_sink_creates_and_appends(tmp_path: Path):
    log_path = tmp_path / "test.log"
    sink = FileSink(log_path)
    sink.write("line\n")
    sink.write("line\n")
    assert log_path.read_text(encoding="utf-8") == "line\nline\n"


def test_file_sink_immediate_flush(tmp_path: Path):
    log_path = tmp_path / "flush.log"
    sink = FileSink(log_path)
    sink.write("flushed")
    assert log_path.read_text(encoding="utf-8") == "flushed"


def test_output_channel_calls_all_sinks():
    sink_a = MagicMock()
    sink_b = MagicMock()
    channel = OutputChannel()
    channel.add_sink(sink_a)
    channel.add_sink(sink_b)
    channel.write("payload")
    sink_a.write.assert_called_once_with("payload")
    sink_b.write.assert_called_once_with("payload")


def test_output_channel_writeln_adds_newline():
    sink = MagicMock()
    channel = OutputChannel()
    channel.add_sink(sink)
    channel.writeln("x")
    sink.write.assert_called_once_with("x\n")


def test_output_channel_empty_sinks_no_error():
    OutputChannel().write("x")
