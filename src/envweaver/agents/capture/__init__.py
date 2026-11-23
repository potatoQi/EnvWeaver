"""Incremental native artifact collection and shared ATIF media storage."""

import base64
import codecs
import json
import mimetypes
from io import BytesIO
from pathlib import Path

from PIL import Image

from .._redaction import redact_data

_SCAN = Path(__file__).with_name("_scan.py").read_text()


class NativeFiles:
    def __init__(self, *, runtime, instance, python, output, redact):
        self.runtime = runtime
        self.instance = instance
        self.python = python
        self.output = output
        self.redact = redact
        self.stamps = {}
        self.files = {}
        self._prefixes = {}

    def poll(self, sources, *, archives=None):
        if not sources:
            return False
        archives = archives or {}
        code, stdout, stderr = self.runtime.exec(
            instance=self.instance,
            command=[self.python, "-c", _SCAN, json.dumps([sources, self.stamps, list(archives)])],
            timeout=30,
        )
        if code:
            raise RuntimeError(f"Cannot collect Agent native records: {self.redact(stderr)}")
        changed = json.loads(stdout)
        saved = []

        def records():
            for name, item in changed.items():
                data = base64.b64decode(item["data"])
                unpack = archives.get(Path(name).suffix)
                if unpack is None:
                    yield name, item["start"], data
                else:
                    for member, body in unpack(name, data):
                        if member not in self.files:
                            yield member, 0, body
                self.stamps[name] = item["stamp"]

        for name, start, data in records():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("Native artifact path escapes its directory")
            text = codecs.getincrementaldecoder("utf-8")().decode(data, final=False)
            prefix, written = self._prefixes.get(name, (0, 0)) if start else (0, 0)
            if name.endswith(".jsonl"):
                # Only newline-terminated records are committed. A valid last record
                # without LF remains visible, but is re-read when the writer appends.
                boundary = text.rfind("\n") + 1
                complete = self._jsonl(text[:boundary])
                text = complete + self._jsonl(text[boundary:])
            else:
                try:
                    value = json.loads(text)
                except ValueError:
                    continue  # A native snapshot can be observed before its writer finishes.
                text = json.dumps(redact_data(value, self.redact), ensure_ascii=False)
            path = self.output.directory / "logs/native" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            # Replace only the tentative tail; committed, redacted records stay on disk.
            with path.open("r+b" if start else "wb") as stream:
                stream.seek(written)
                stream.write(text.encode("utf-8"))
                stream.truncate()
            saved.append(name)
            self.files[name] = self.files.get(name, "")[:prefix] + text
            if name.endswith(".jsonl"):
                self._prefixes[name] = (
                    prefix + len(complete), written + len(complete.encode("utf-8")),
                )
        if saved:
            logs = self.output.result["artifacts"]["logs"]
            logs = list(dict.fromkeys([*logs, *("logs/native/" + n for n in saved)]))
            self.output.update(artifacts={"logs": logs}, sync=False)
        return bool(saved)

    def _jsonl(self, text):
        return "".join(
            json.dumps(redact_data(item, self.redact), ensure_ascii=False) + "\n"
            for item in json_lines(text)
        )


def json_lines(text):
    """Only a trailing incomplete line may be pending; malformed complete lines fail."""
    # JSON strings may contain U+0085/U+2028/U+2029; only LF separates records.
    lines = text.split("\n")
    result = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            result.append(json.loads(line))
        except ValueError:
            if index != len(lines) - 1:
                raise
    return result


class Media:
    def __init__(self, output, read_remote=None):
        self.output = output
        self.read_remote = read_remote
        self.missing = []
        self._images = {}

    def image(self, source, mime=None):
        if source in self._images:
            return self._images[source]
        if source.startswith("data:"):
            header, encoded = source.split(",", 1)
            if ";base64" not in header:
                raise ValueError("Image data URL must use base64")
            mime = header[5:].split(";")[0]
            data = base64.b64decode(encoded, validate=True)
        else:
            mime = mime or mimetypes.guess_type(source)[0]
            local = self.output.directory / source
            if (
                not Path(source).is_absolute()
                and local.resolve().is_relative_to(self.output.directory.resolve())
                and local.is_file()
            ):
                data = local.read_bytes()
            elif self.read_remote is not None and not source.startswith(("http:", "https:")):
                data = self.read_remote(source)
            else:
                data = None
            if data is None:
                self.missing.append("image:" + source)
                # A valid external reference remains visible, never replaced by fake image bytes.
                return {
                    "type": "image",
                    "source": {"media_type": mime or "image/png", "path": source},
                }
        if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            with Image.open(BytesIO(data)) as picture:
                mime = Image.MIME[picture.format]
        suffix = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(mime)
        if suffix is None:
            raise ValueError(f"Unsupported ATIF image type: {mime}")
        value = {
            "type": "image",
            "source": {"media_type": mime, "path": self.output.save_file(data, suffix)},
        }
        self._images[source] = value
        return value
