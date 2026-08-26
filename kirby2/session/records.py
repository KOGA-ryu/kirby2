"""Deterministic input, market-state, and timeline records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class TimelineKind(str, Enum):
    INPUT = "INPUT"
    COMMAND = "COMMAND"
    REJECTED = "REJECTED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILL = "FILL"
    POSITION = "POSITION"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"
    MID = "MID"
    BOOK = "BOOK"


@dataclass(frozen=True, slots=True)
class InputRecord:
    sequence: int
    simulation_time_us: int
    input_key: str
    resolved_command: str | None
    order_parameters: dict[str, Any]
    market_state_id: str
    latency_reference_time_us: int
    action_latency_us: int
    accepted: bool
    rejection_reason: str | None
    resulting_order_id: str | None
    resulting_order_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.sequence <= 0 or self.simulation_time_us < 0:
            raise ValueError("input record sequence and timestamp are invalid")
        if not self.input_key or self.action_latency_us < 0:
            raise ValueError("input record key or latency is invalid")
        if not 0 <= self.latency_reference_time_us <= self.simulation_time_us:
            raise ValueError("input latency reference is outside simulation time")
        if (
            self.simulation_time_us - self.latency_reference_time_us
            != self.action_latency_us
        ):
            raise ValueError("input latency does not reconcile to its reference")
        if self.accepted and self.rejection_reason is not None:
            raise ValueError("accepted input cannot have a rejection reason")
        if not self.accepted and not self.rejection_reason:
            raise ValueError("rejected input must include a reason")
        expected_first = self.resulting_order_ids[0] if self.resulting_order_ids else None
        if self.resulting_order_id != expected_first:
            raise ValueError("resulting order ID must match the first result")

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "action_latency_us": self.action_latency_us,
            "input_key": self.input_key,
            "latency_reference_timestamp": self.latency_reference_time_us,
            "market_state_id": self.market_state_id,
            "order_parameters": self.order_parameters,
            "rejection_reason": self.rejection_reason,
            "resolved_command": self.resolved_command,
            "resulting_order_id": self.resulting_order_id,
            "resulting_order_ids": list(self.resulting_order_ids),
            "sequence": self.sequence,
            "simulation_timestamp": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> InputRecord:
        parameters = payload.get("order_parameters", {})
        order_ids = payload.get("resulting_order_ids", [])
        if not isinstance(parameters, dict) or not isinstance(order_ids, list):
            raise ValueError("invalid input record parameters or order IDs")
        return cls(
            sequence=int(payload["sequence"]),
            simulation_time_us=int(payload["simulation_timestamp"]),
            input_key=str(payload["input_key"]),
            resolved_command=(
                None
                if payload.get("resolved_command") is None
                else str(payload["resolved_command"])
            ),
            order_parameters=dict(parameters),
            market_state_id=str(payload["market_state_id"]),
            latency_reference_time_us=int(payload["latency_reference_timestamp"]),
            action_latency_us=int(payload["action_latency_us"]),
            accepted=bool(payload["accepted"]),
            rejection_reason=(
                None
                if payload.get("rejection_reason") is None
                else str(payload["rejection_reason"])
            ),
            resulting_order_id=(
                None
                if payload.get("resulting_order_id") is None
                else str(payload["resulting_order_id"])
            ),
            resulting_order_ids=tuple(str(value) for value in order_ids),
        )


@dataclass(frozen=True, slots=True)
class MarketStateRecord:
    state_id: str
    simulation_time_us: int
    observed_state_time_us: int
    exchange_event_sequence: int
    snapshot: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.state_id.startswith("MS-"):
            raise ValueError("invalid market-state identifier")
        if self.simulation_time_us < 0 or self.exchange_event_sequence < 0:
            raise ValueError("invalid market-state timestamp or event sequence")
        if not 0 <= self.observed_state_time_us <= self.simulation_time_us:
            raise ValueError("observed-state timestamp is outside simulation time")

    def as_dict(self) -> dict[str, object]:
        return {
            "exchange_event_sequence": self.exchange_event_sequence,
            "market_state_id": self.state_id,
            "observed_state_timestamp": self.observed_state_time_us,
            "simulation_timestamp": self.simulation_time_us,
            "snapshot": self.snapshot,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> MarketStateRecord:
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("market-state snapshot must be an object")
        return cls(
            state_id=str(payload["market_state_id"]),
            simulation_time_us=int(payload["simulation_timestamp"]),
            observed_state_time_us=int(payload["observed_state_timestamp"]),
            exchange_event_sequence=int(payload["exchange_event_sequence"]),
            snapshot=dict(snapshot),
        )


@dataclass(frozen=True, slots=True)
class TimelineRecord:
    sequence: int
    simulation_time_us: int
    kind: TimelineKind
    message: str
    data: dict[str, Any]

    def __post_init__(self) -> None:
        if self.sequence <= 0 or self.simulation_time_us < 0 or not self.message:
            raise ValueError("invalid timeline record")

    def as_dict(self) -> dict[str, object]:
        return {
            "data": self.data,
            "kind": self.kind.value,
            "message": self.message,
            "sequence": self.sequence,
            "simulation_timestamp": self.simulation_time_us,
        }
