"""Incremental native artifact collection and shared ATIF media storage."""

import base64
import codecs
import json
import mimetypes
from io import BytesIO
from pathlib import Path

from PIL import Image

from .._redaction import redact_data

_SCAN = """import base64,json,sys
from pathlib import Path
sources,previous=json.loads(sys.argv[1])
result={}
for label,folder in sources.items():
 root=Path(folder)
 paths=root.rglob('*') if root.is_dir() else [root]
 for p in paths:
  if p.is_symlink() or not p.is_file() or p.suffix not in ('.json','.jsonl'): continue
  rel=label+'/'+(str(p.relative_to(root)) if root.is_dir() else p.name)
  try:
   stat=p.stat(); stamp=[stat.st_mtime_ns,stat.st_size]
   if previous.get(rel)==stamp: continue
   data=p.read_bytes()
  except FileNotFoundError: continue
  result[rel]={'stamp':stamp,'data':base64.b64encode(data).decode()}
print(json.dumps(result))
"""


class NativeFiles:
    def __init__(self, *, runtime, instance, python, output, redact):
        self.runtime = runtime
        self.instance = instance
        self.python = python
        self.output = output
        self.redact = redact
        self.stamps = {}
        self.files = {}

    def poll(self, sources):
        if not sources:
            return False
        code, stdout, stderr = self.runtime.exec(
            instance=self.instance,
            command=[self.python, "-c", _SCAN, json.dumps([sources, self.stamps])],
            timeout=30,
        )
        if code:
            raise RuntimeError(f"Cannot collect Agent native records: {self.redact(stderr)}")
        changed = json.loads(stdout)
        saved = []
        for name, item in changed.items():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("Native artifact path escapes its directory")
            # A concurrent append may end this snapshot inside a UTF-8 character.
            text = codecs.getincrementaldecoder("utf-8")().decode(
                base64.b64decode(item["data"]), final=False
            )
            if name.endswith(".jsonl"):
                text = "".join(
                    json.dumps(redact_data(item, self.redact), ensure_ascii=False) + "\n"
                    for item in json_lines(text)
                )
            else:
                try:
                    value = json.loads(text)
                except ValueError:
                    continue  # A native snapshot can be observed before its writer finishes.
                text = json.dumps(redact_data(value, self.redact), ensure_ascii=False)
            path = self.output.directory / "logs/native" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            saved.append(name)
            self.files[name] = text
            self.stamps[name] = item["stamp"]
        if saved:
            logs = self.output.result["artifacts"]["logs"]
            logs = list(dict.fromkeys([*logs, *("logs/native/" + n for n in saved)]))
            self.output.update(artifacts={"logs": logs}, sync=False)
        return bool(saved)


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
