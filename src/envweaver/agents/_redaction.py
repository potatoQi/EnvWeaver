"""Handle credential fields and redact JSON text without modifying encoded media."""

import re


def is_credential_name(key, *, request_options=False):
    name = str(key).lower().replace("-", "_")
    if re.search(r"password|secret|credential|authorization|cookie|api_?key|private_?key", name):
        return True
    # Cache and idempotency keys identify requests, including in HTTP headers.
    if request_options and name.removeprefix("x_") in {
        "prompt_cache_key",
        "cache_key",
        "idempotency_key",
    }:
        return False
    return bool(re.search(r"(?:^|_)(?:key|token|auth)(?:$|_)", name))


def redact_data(value, redact):
    if isinstance(value, str):
        if (
            value.startswith(("data:image/", "data:application/octet-stream;base64,"))
            and ";base64," in value
        ):
            return value
        return redact(value)
    if isinstance(value, list):
        return [redact_data(item, redact) for item in value]
    if isinstance(value, dict):
        mime = value.get("mimeType") or value.get("media_type") or value.get("mime_type")
        image = isinstance(mime, str) and mime.startswith("image/")
        return {
            key: item
            if image and key == "data" and isinstance(item, str)
            else redact_data(item, redact)
            for key, item in value.items()
        }
    return value
