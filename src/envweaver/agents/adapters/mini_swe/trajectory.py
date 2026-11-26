"""Add mini-swe-agent's final native messages and run metadata to its captured calls."""

from ...capture import litellm


def convert(events, output, read_remote=None, native=None, *, session_id):
    native = native or {}
    doc = litellm.convert(
        events,
        output,
        session_id=session_id,
        read_remote=read_remote,
        native_messages=native.get("messages", []),
    )
    if doc is not None:
        doc["extra"]["native_info"] = native.get("info", {})
    return doc
