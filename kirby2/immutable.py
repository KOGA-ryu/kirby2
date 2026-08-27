"""Strict ownership helpers for immutable JSON evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType


def freeze_json(value: object) -> object:
    """Copy a JSON value into recursively immutable, deterministically ordered storage."""

    return _freeze_json(value, set())


def thaw_json(value: object) -> object:
    """Return a fully detached mutable JSON tree from frozen JSON storage."""

    return _thaw_json(value, set())


def _freeze_json(value: object, active: set[int]) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON floats must be finite")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("JSON object keys must be strings")
        identity = id(value)
        if identity in active:
            raise ValueError("JSON values must not contain reference cycles")
        active.add(identity)
        try:
            frozen = {
                key: _freeze_json(value[key], active)
                for key in sorted(value)
            }
        finally:
            active.remove(identity)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError("JSON values must not contain reference cycles")
        active.add(identity)
        try:
            return tuple(_freeze_json(item, active) for item in value)
        finally:
            active.remove(identity)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _thaw_json(value: object, active: set[int]) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON floats must be finite")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("JSON object keys must be strings")
        identity = id(value)
        if identity in active:
            raise ValueError("JSON values must not contain reference cycles")
        active.add(identity)
        try:
            return {
                key: _thaw_json(value[key], active)
                for key in sorted(value)
            }
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError("JSON values must not contain reference cycles")
        active.add(identity)
        try:
            return [_thaw_json(item, active) for item in value]
        finally:
            active.remove(identity)
    raise TypeError(f"unsupported frozen JSON value: {type(value).__name__}")
