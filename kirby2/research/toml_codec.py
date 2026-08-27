"""Deterministic TOML encoding for immutable research artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any


_NULL_MARKER = "__kirby2_null_v1__"


def canonical_toml(payload: Mapping[str, object]) -> str:
    """Encode a mapping as sorted, deterministic TOML with a trailing newline."""

    if not isinstance(payload, Mapping):
        raise TypeError("canonical TOML payload must be a mapping")
    lines: list[str] = []
    _emit_table(lines, (), payload)
    return "\n".join(lines).rstrip() + "\n"


def load_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        payload = tomllib.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("TOML artifact must contain a table")
    decoded = _decode_nulls(payload)
    if not isinstance(decoded, dict):
        raise ValueError("decoded TOML artifact must contain a table")
    return decoded


def encode_payload(payload: Mapping[str, object]) -> str:
    return canonical_toml({"payload": _encode_nulls(dict(payload))})


def decode_payload(text: str) -> dict[str, object]:
    parsed = tomllib.loads(text)
    payload = parsed.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("encoded payload lacks its root table")
    decoded = _decode_nulls(payload)
    if not isinstance(decoded, dict):
        raise ValueError("decoded payload must be a table")
    return decoded


def canonical_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_toml(payload).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _emit_table(
    lines: list[str],
    path: tuple[str, ...],
    table: Mapping[str, object],
) -> None:
    scalar_items = [
        (str(key), value)
        for key, value in table.items()
        if not isinstance(value, Mapping)
    ]
    child_items = [
        (str(key), value)
        for key, value in table.items()
        if isinstance(value, Mapping)
    ]
    if path:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("[" + ".".join(_key(part) for part in path) + "]")
    for key, value in sorted(scalar_items):
        lines.append(f"{_key(key)} = {_value(value)}")
    for key, value in sorted(child_items):
        _emit_table(lines, (*path, key), value)


def _key(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _value(value: object) -> str:
    if value is None:
        return '{ "__kirby2_null_v1__" = true }'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("TOML floats must be finite")
        return repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("TOML decimals must be finite")
        return json.dumps(str(value))
    if isinstance(value, Mapping):
        values = ", ".join(
            f"{_key(str(key))} = {_value(item)}"
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
        return "{ " + values + " }"
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise TypeError(f"unsupported canonical TOML value: {type(value).__name__}")


def _encode_nulls(value: Any) -> Any:
    if value is None:
        return {_NULL_MARKER: True}
    if isinstance(value, Mapping):
        return {str(key): _encode_nulls(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_nulls(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def _decode_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        if value == {_NULL_MARKER: True}:
            return None
        return {str(key): _decode_nulls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_nulls(item) for item in value]
    return value
