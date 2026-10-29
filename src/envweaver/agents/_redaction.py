"""Redact JSON text without modifying encoded media payloads."""


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
