"""Monotonic simulation time independent from the wall clock."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from kirby2.immutable import freeze_json, thaw_json


MICROSECONDS_PER_SECOND = 1_000_000
SIMULATION_CLOCK_CHECKPOINT_SCHEMA_VERSION = 1


def _validate_strict_checkpoint_json(
    value: object,
    active: set[int] | None = None,
) -> None:
    active = set() if active is None else active
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("checkpoint JSON strings must be NFC-normalized")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("checkpoint JSON strings must be Unicode scalar values")
        return
    if type(value) is float:
        raise TypeError("binary floats are forbidden in checkpoint JSON")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("checkpoint JSON object keys must be strings")
        identity = id(value)
        if identity in active:
            raise ValueError("checkpoint JSON must not contain reference cycles")
        active.add(identity)
        try:
            for key in sorted(value):
                _validate_strict_checkpoint_json(key, active)
                _validate_strict_checkpoint_json(value[key], active)
        finally:
            active.remove(identity)
        return
    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in active:
            raise ValueError("checkpoint JSON must not contain reference cycles")
        active.add(identity)
        try:
            for item in value:
                _validate_strict_checkpoint_json(item, active)
        finally:
            active.remove(identity)
        return
    raise TypeError(f"unsupported checkpoint JSON value: {type(value).__name__}")


def _canonical_json_bytes(value: object) -> bytes:
    _validate_strict_checkpoint_json(value)
    detached = thaw_json(freeze_json(value))
    return json.dumps(
        detached,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_canonical_json_object(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes:
        raise TypeError("canonical simulation-clock state must be bytes")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_float=lambda _value: (_ for _ in ()).throw(
                TypeError("decimal JSON numbers are forbidden in checkpoint state")
            ),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number is forbidden: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("simulation-clock state is not canonical UTF-8 JSON") from error
    if type(value) is not dict:
        raise ValueError("simulation-clock state must be a JSON object")
    if _canonical_json_bytes(value) != payload:
        raise ValueError("simulation-clock state bytes are not canonical")
    return value


@dataclass(slots=True)
class SimulationClock:
    current_time_us: int = 0

    def __post_init__(self) -> None:
        if type(self.current_time_us) is not int or self.current_time_us < 0:
            raise ValueError("simulation time must be a nonnegative integer number of microseconds")

    def advance_to(self, time_us: int) -> None:
        if type(time_us) is not int:
            raise TypeError("simulation time must be integer microseconds")
        if time_us < self.current_time_us:
            raise ValueError("simulation clock cannot move backward")
        self.current_time_us = time_us

    def advance_by(self, delta_us: int) -> None:
        if type(delta_us) is not int or delta_us < 0:
            raise ValueError("simulation delta must be a nonnegative integer")
        self.advance_to(self.current_time_us + delta_us)

    def checkpoint_state(self) -> dict[str, object]:
        if type(self.current_time_us) is not int or self.current_time_us < 0:
            raise RuntimeError("simulation-clock state is invalid")
        payload: dict[str, object] = {
            "current_time_us": self.current_time_us,
            "schema_version": SIMULATION_CLOCK_CHECKPOINT_SCHEMA_VERSION,
        }
        _validate_strict_checkpoint_json(payload)
        return payload

    def canonical_state_bytes(self) -> bytes:
        return _canonical_json_bytes(self.checkpoint_state())

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
    ) -> SimulationClock:
        if not isinstance(payload, Mapping):
            raise TypeError("simulation-clock checkpoint state must be a mapping")
        _validate_strict_checkpoint_json(payload)
        expected = frozenset({"current_time_us", "schema_version"})
        actual = frozenset(payload)
        if actual != expected:
            raise ValueError(
                "simulation-clock fields differ: "
                f"missing={sorted(expected - actual)} "
                f"unknown={sorted(actual - expected)}"
            )
        schema_version = payload["schema_version"]
        if (
            type(schema_version) is not int
            or schema_version != SIMULATION_CLOCK_CHECKPOINT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported simulation-clock checkpoint schema")
        current_time_us = payload["current_time_us"]
        if type(current_time_us) is not int or current_time_us < 0:
            raise ValueError(
                "simulation-clock time must be nonnegative integer microseconds"
            )
        validated = {
            "current_time_us": current_time_us,
            "schema_version": schema_version,
        }
        if _canonical_json_bytes(validated) != _canonical_json_bytes(payload):
            raise ValueError("simulation-clock checkpoint state is not canonical")
        return cls(current_time_us=current_time_us)

    @classmethod
    def from_canonical_state_bytes(cls, payload: bytes) -> SimulationClock:
        return cls.from_checkpoint_state(_load_canonical_json_object(payload))
