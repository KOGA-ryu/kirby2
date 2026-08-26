"""Canonical, monotonically sequenced simulation events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class EventType(str, Enum):
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ADDED = "ORDER_ADDED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    ORDER_REPLACED = "ORDER_REPLACED"
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
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"data": self.data, "sequence": self.sequence, "type": self.event_type.value}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


class EventJournal:
    def __init__(self) -> None:
        self._next_sequence = 1
        self._events: list[SimulationEvent] = []

    @property
    def events(self) -> tuple[SimulationEvent, ...]:
        return tuple(self._events)

    def emit(self, event_type: EventType, **data: Any) -> SimulationEvent:
        event = SimulationEvent(self._next_sequence, event_type, data)
        self._events.append(event)
        self._next_sequence += 1
        return event

    def canonical_json_lines(self) -> str:
        return "\n".join(event.to_json() for event in self._events)

