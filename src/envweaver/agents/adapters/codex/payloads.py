"""Resolve Codex path references (v1/v2) and sharded payload references (v3)."""

import json
import re
import struct
from pathlib import PurePosixPath

import zstandard

_HEADER = struct.Struct("<HQ")


def _ordinal(identity):
    match = re.fullmatch(r"raw_payload:([0-9]+)", identity)
    if not match:
        raise ValueError(f"Invalid Codex payload ID: {identity!r}")
    return match[1]


def unpack_shard(name, data):
    """Yield complete JSON records from a sealed or still-growing Zstandard frame."""
    decoder = zstandard.ZstdDecompressor().decompressobj()
    raw = decoder.decompress(data)
    offset = 0
    while len(raw) - offset >= _HEADER.size:
        id_size, body_size = _HEADER.unpack_from(raw, offset)
        start = offset + _HEADER.size
        body = start + id_size
        end = body + body_size
        if end > len(raw):
            break
        ordinal = _ordinal(raw[start:body].decode())
        yield f"{name[:-4]}/{ordinal}.json", raw[body:end]
        offset = end
    if decoder.eof and offset != len(raw):
        raise ValueError(f"Partial record in sealed Codex shard {name}")


def relative_path(ref):
    if "path" in ref:
        path = PurePosixPath(ref["path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Codex payload path escapes its trace directory")
        return str(path)
    return f"payloads/{ref['shard']:06d}/{_ordinal(ref['raw_payload_id'])}.json"


def read_payload(files, base, ref, missing):
    if not ref:
        return None
    if (ref.get("capture") or {}).get("status", "complete") != "complete":
        missing.add(ref["raw_payload_id"])
        return None
    path = relative_path(ref)
    raw = files.get(f"{base}/{path}")
    if raw is None:
        missing.add(path)
        return None
    value = json.loads(raw)
    parts = ref.get("parts", [])
    if not parts:
        return value
    if not isinstance(value, list) or len(value) != len(parts) + 1:
        raise ValueError(f"Invalid Codex JSON fragments in {path}")
    fragments = [value[0]]
    for part, suffix in zip(parts, value[1:], strict=True):
        # Missing children must never turn into fabricated JSON null values.
        gaps = set()
        child = read_payload(files, base, part, gaps)
        if gaps:
            missing.update(gaps)
            return None
        fragments.extend((json.dumps(child, ensure_ascii=False), suffix))
    return json.loads("".join(fragments))
