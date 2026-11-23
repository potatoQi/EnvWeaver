"""Incremental collection must preserve full-snapshot contents and redaction."""

import base64
import json
import os
import sys

import pytest

from envweaver import AgentOutput
from envweaver.agents.capture import NativeFiles, json_lines
from tests.agents.test_cli_agents import LocalTransport


class RecordingTransport(LocalTransport):
    def __init__(self):
        super().__init__()
        self.transferred = []

    def exec(self, *args, **kwargs):
        result = super().exec(*args, **kwargs)
        self.transferred.append(sum(
            len(base64.b64decode(item["data"])) for item in json.loads(result[1]).values()
        ))
        return result


@pytest.fixture
def capture(tmp_path):
    runtime = RecordingTransport()
    output = AgentOutput(
        tmp_path / "output", {"agent": "test", "agent_version": "test", "model": "test"},
    )
    collector = NativeFiles(
        runtime=runtime, instance=None, python=sys.executable, output=output,
        redact=lambda text: text.replace("secret", "MASKED"),
    )
    source = tmp_path / "events.jsonl"
    return collector, source, runtime


def poll(collector, source):
    return collector.poll({"test": str(source)})


def assert_saved(collector, expected):
    name = "test/events.jsonl"
    text = collector.files[name]
    assert json_lines(text) == expected
    assert (collector.output.directory / "logs/native" / name).read_text() == text
    assert "secret" not in text


def test_append_does_not_transfer_or_redact_history(capture):
    collector, source, runtime = capture
    first = json.dumps({"text": "secret" + "x" * 1_000_000}) + "\n"
    source.write_text(first)
    assert poll(collector, source)
    seen = []
    collector.redact = lambda text: seen.append(text) or text.replace("secret", "MASKED")
    second = '{"text":"secret中文"}\n'
    with source.open("a") as stream:
        stream.write(second)
    assert poll(collector, source)
    assert runtime.transferred == [len(first.encode()), len(second.encode())]
    assert max(map(len, seen)) < 100
    assert_saved(collector, [{"text": "MASKED" + "x" * 1_000_000}, {"text": "MASKED中文"}])
    assert not poll(collector, source)
    assert runtime.transferred[-1] == 0


def test_tentative_tail_and_split_utf8_are_not_duplicated(capture):
    collector, source, runtime = capture
    prefix = b'{"text":"secret"}\n'
    source.write_bytes(prefix + b'{"text":"tail"}')
    poll(collector, source)
    assert_saved(collector, [{"text": "MASKED"}, {"text": "tail"}])
    suffix = '\n{"text":"中文secret"}\n'.encode()
    split = suffix.index("中".encode()) + 1
    with source.open("ab") as stream:
        stream.write(suffix[:split])
    poll(collector, source)
    assert_saved(collector, [{"text": "MASKED"}, {"text": "tail"}])
    with source.open("ab") as stream:
        stream.write(suffix[split:])
    poll(collector, source)
    assert_saved(collector, [
        {"text": "MASKED"}, {"text": "tail"}, {"text": "中文MASKED"},
    ])


@pytest.mark.parametrize("change", ["truncate", "replace", "same_size", "grow_rewrite"])
def test_rewritten_file_discards_previous_records(capture, change):
    collector, source, _ = capture
    source.write_text('{"text":"old secret"}\n')
    poll(collector, source)
    before = source.stat()
    replacement = '{"text":"new secret"}\n'
    if change == "truncate":
        replacement = '{}\n'
        source.write_text(replacement)
    elif change == "replace":
        replacement = '{"text":"a much longer new secret"}\n'
        temporary = source.with_suffix(".new")
        temporary.write_text(replacement)
        temporary.replace(source)
    elif change == "same_size":
        source.write_text(replacement)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    else:
        replacement = '{"text":"a longer replacement secret"}\n'
        source.write_text(replacement)
    assert poll(collector, source)
    assert_saved(collector, [json.loads(replacement.replace("secret", "MASKED"))])


def test_bad_completed_line_is_not_silently_dropped(capture):
    collector, source, _ = capture
    source.write_text('{}\n{"text":')
    poll(collector, source)
    with source.open("a") as stream:
        stream.write('bad}\n')
    with pytest.raises(json.JSONDecodeError):
        poll(collector, source)
    assert_saved(collector, [{}])


def test_failed_write_can_be_retried_without_duplicate_records(capture, monkeypatch):
    collector, source, _ = capture
    source.write_text('{}\n')
    poll(collector, source)
    with source.open("a") as stream:
        stream.write('{"text":"secret"}\n')
    original = type(source).open
    with monkeypatch.context() as patch:
        def fail(path, mode="r", *args, **kwargs):
            if mode == "r+b":
                raise OSError("disk unavailable")
            return original(path, mode, *args, **kwargs)
        patch.setattr(type(source), "open", fail)
        with pytest.raises(OSError, match="disk unavailable"):
            poll(collector, source)
    assert poll(collector, source)
    assert_saved(collector, [{}, {"text": "MASKED"}])
