"""Canonical, monotonically sequenced simulation events."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from kirby2.immutable import freeze_json, thaw_json


EVENT_JOURNAL_CHECKPOINT_SCHEMA_VERSION = 1


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
        raise TypeError("canonical checkpoint state must be bytes")
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_float=lambda _value: (_ for _ in ()).throw(
                TypeError("decimal JSON numbers are forbidden in checkpoint state")
            ),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number is forbidden: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("checkpoint state is not canonical UTF-8 JSON") from error
    if type(value) is not dict:
        raise ValueError("checkpoint state must be a JSON object")
    if _canonical_json_bytes(value) != payload:
        raise ValueError("checkpoint state bytes are not canonical")
    return value


def _require_exact_fields(
    payload: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = frozenset(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(
            f"{label} fields differ: missing={missing} unknown={unknown}"
        )


def _require_nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


class EventType(str, Enum):
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ADDED = "ORDER_ADDED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    ORDER_REPLACED = "ORDER_REPLACED"
    ORDER_REDUCED = "ORDER_REDUCED"
    TRADE = "TRADE"
    PARTIAL_FILL = "PARTIAL_FILL"
    FULL_FILL = "FULL_FILL"
    BEST_BID_CHANGED = "BEST_BID_CHANGED"
    BEST_ASK_CHANGED = "BEST_ASK_CHANGED"
    PLAYER_POSITION_CHANGED = "PLAYER_POSITION_CHANGED"


@dataclass(frozen=True, slots=True)
class SimulationEvent:
    sequence: int
    event_type: EventType
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("simulation event sequence must be a positive integer")
        if not isinstance(self.event_type, EventType):
            raise TypeError("simulation event type must use EventType")
        frozen = freeze_json(self.data)
        if not isinstance(frozen, Mapping):
            raise TypeError("simulation event data must be a JSON object")
        object.__setattr__(self, "data", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "data": thaw_json(self.data),
            "sequence": self.sequence,
            "type": self.event_type.value,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


class EventJournal:
    def __init__(self) -> None:
        self._next_sequence = 1
        self._events: list[SimulationEvent] = []

    @property
    def events(self) -> tuple[SimulationEvent, ...]:
        return tuple(self._events)

    def emit(self, event_type: EventType, **data: object) -> SimulationEvent:
        event = SimulationEvent(self._next_sequence, event_type, data)
        self._events.append(event)
        self._next_sequence += 1
        return event

    def canonical_json_lines(self) -> str:
        return "\n".join(event.to_json() for event in self._events)

    def checkpoint_state(self) -> dict[str, object]:
        """Return the complete strict state needed to resume event allocation."""

        if [event.sequence for event in self._events] != list(
            range(1, len(self._events) + 1)
        ):
            raise RuntimeError("event-journal prefix is not contiguous")
        if self._next_sequence != len(self._events) + 1:
            raise RuntimeError("event-journal allocator is inconsistent")
        payload: dict[str, object] = {
            "events": [event.as_dict() for event in self._events],
            "next_sequence": self._next_sequence,
            "schema_version": EVENT_JOURNAL_CHECKPOINT_SCHEMA_VERSION,
        }
        _validate_strict_checkpoint_json(payload)
        return payload

    def canonical_state_bytes(self) -> bytes:
        return _canonical_json_bytes(self.checkpoint_state())

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
    ) -> EventJournal:
        """Validate a journal prefix completely before allocating a new journal."""

        if not isinstance(payload, Mapping):
            raise TypeError("event-journal checkpoint state must be a mapping")
        _validate_strict_checkpoint_json(payload)
        _require_exact_fields(
            payload,
            frozenset({"events", "next_sequence", "schema_version"}),
            "event-journal checkpoint",
        )
        schema_version = _require_nonnegative_int(
            payload["schema_version"],
            "event-journal schema version",
        )
        if schema_version != EVENT_JOURNAL_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported event-journal checkpoint schema")
        raw_events = payload["events"]
        if type(raw_events) is not list:
            raise ValueError("event-journal events must be an ordered array")
        events: list[SimulationEvent] = []
        for index, raw_event in enumerate(raw_events, start=1):
            if type(raw_event) is not dict:
                raise ValueError("event-journal event rows must be objects")
            _require_exact_fields(
                raw_event,
                frozenset({"data", "sequence", "type"}),
                "event-journal event",
            )
            sequence = _require_nonnegative_int(
                raw_event["sequence"],
                "simulation event sequence",
            )
            if sequence != index:
                raise ValueError(
                    "event-journal events must form a contiguous prefix"
                )
            event_type_value = raw_event["type"]
            if type(event_type_value) is not str:
                raise ValueError("simulation event type must be a string")
            raw_data = raw_event["data"]
            if type(raw_data) is not dict:
                raise ValueError("simulation event data must be an object")
            events.append(
                SimulationEvent(
                    sequence=sequence,
                    event_type=EventType(event_type_value),
                    data=raw_data,
                )
            )
        next_sequence = _require_nonnegative_int(
            payload["next_sequence"],
            "event-journal next sequence",
        )
        if next_sequence != len(events) + 1:
            raise ValueError(
                "event-journal allocator does not follow its contiguous prefix"
            )
        validated = {
            "events": [event.as_dict() for event in events],
            "next_sequence": next_sequence,
            "schema_version": schema_version,
        }
        if _canonical_json_bytes(validated) != _canonical_json_bytes(payload):
            raise ValueError("event-journal checkpoint state is not canonical")
        journal = cls()
        journal._events = events
        journal._next_sequence = next_sequence
        return journal

    @classmethod
    def from_canonical_state_bytes(cls, payload: bytes) -> EventJournal:
        return cls.from_checkpoint_state(_load_canonical_json_object(payload))
