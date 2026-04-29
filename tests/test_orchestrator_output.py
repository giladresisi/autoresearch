from pathlib import Path
from unittest.mock import MagicMock

from orchestrator.output import FileSink, JsonlFileSink, OutputChannel, StdoutSink


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


def test_jsonl_file_sink_writes_valid_json_line(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    sink = JsonlFileSink(path)
    sink.write('{"signal_type": "LIMIT_PLACED", "direction": "long"}\n')
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert '"signal_type"' in lines[0]


def test_jsonl_file_sink_skips_plain_text(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    sink = JsonlFileSink(path)
    sink.write("[09:30:00] SIGNAL long | entry ~19000.00\n")
    assert not path.exists()


def test_jsonl_file_sink_skips_malformed_json(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    sink = JsonlFileSink(path)
    sink.write("{not valid json\n")
    assert not path.exists()


def test_jsonl_file_sink_mixed_text_and_json(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    sink = JsonlFileSink(path)
    sink.write('[09:30:00] SIGNAL long\n{"signal_type": "A"}\nplain text\n{"signal_type": "B"}\n')
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"A"' in lines[0]
    assert '"B"' in lines[1]


def test_jsonl_file_sink_appends_across_calls(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    sink = JsonlFileSink(path)
    sink.write('{"a": 1}\n')
    sink.write('{"b": 2}\n')
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
