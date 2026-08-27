"""Canonical, monotonically sequenced simulation events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from kirby2.immutable import freeze_json, thaw_json


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
