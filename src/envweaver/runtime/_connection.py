"""Merge connection settings and select portable fields for built-in backends."""

import json

from .._util import merge_dicts


def merge_connection(defaults, overrides):
    if not isinstance(defaults, dict) or (
        overrides is not None and not isinstance(overrides, dict)
    ):
        raise TypeError("connection must be a dictionary")
    return merge_dicts(defaults, overrides or {})


def fixed_connection(backend, connection, *, strict=True):
    if connection is None:
        return {}
    if not isinstance(connection, dict):
        raise TypeError("connection must be a dictionary")
    allowed = {
        "qemu": {"ssh": {"user", "python"}},
        "docker": {"vnc": {"password_env"}},
    }.get(backend)
    if allowed is None:
        return json.loads(json.dumps(connection, allow_nan=False))
    result = {}
    for group, values in connection.items():
        if group not in allowed:
            if strict:
                raise ValueError(f"Unsupported fixed {backend} connection field: {group}")
            continue
        if not isinstance(values, dict):
            raise ValueError(f"connection.{group} must be a dictionary")
        if strict and set(values) - allowed[group]:
            raise ValueError(f"Unsupported fixed {backend} connection fields: {group}")
        fields = {key: value for key, value in values.items() if key in allowed[group]}
        if any(not isinstance(value, str) or not value.strip() for value in fields.values()):
            raise ValueError(f"connection.{group} values must be nonempty strings")
        if fields:
            result[group] = fields
    return result
