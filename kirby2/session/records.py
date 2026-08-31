"""Deterministic input, market-state, and timeline records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from kirby2.immutable import freeze_json, thaw_json


class TimelineKind(str, Enum):
    INPUT = "INPUT"
    COMMAND = "COMMAND"
    REJECTED = "REJECTED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILL = "FILL"
    POSITION = "POSITION"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"
    TRAFFIC = "TRAFFIC"
    STRATEGY_EVALUATION = "STRATEGY_EVALUATION"
    OBJECTIVE = "OBJECTIVE"
    CURRICULUM = "CURRICULUM"
    MID = "MID"
    BOOK = "BOOK"


class RecoveryBoundaryKindV1(str, Enum):
    SESSION_OPENED = "SESSION_OPENED"
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_PAUSED = "SESSION_PAUSED"
    ADVANCE_COMMITTED = "ADVANCE_COMMITTED"
    ACTION_PENDING = "ACTION_PENDING"
    ACTION_ACKNOWLEDGED = "ACTION_ACKNOWLEDGED"
    CLIENT_MESSAGE_PENDING = "CLIENT_MESSAGE_PENDING"
    CLIENT_MESSAGE_ACKNOWLEDGED = "CLIENT_MESSAGE_ACKNOWLEDGED"
    CHECKPOINT_COMMITTED = "CHECKPOINT_COMMITTED"
    PACK_ACTIVATION_PENDING = "PACK_ACTIVATION_PENDING"
    PACK_ACTIVATION_COMMITTED = "PACK_ACTIVATION_COMMITTED"
    PROFILE_UPDATE_PENDING = "PROFILE_UPDATE_PENDING"
    PROFILE_UPDATE_COMMITTED = "PROFILE_UPDATE_COMMITTED"
    RECOVERY_COMPLETED = "RECOVERY_COMPLETED"
    SESSION_CLOSED = "SESSION_CLOSED"
    SESSION_ABANDONED = "SESSION_ABANDONED"


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class RecoveryEvidenceRecordV1:
    """One durable recovery boundary projected into immutable run evidence."""

    sequence: int
    session_id: str
    boundary: RecoveryBoundaryKindV1
    simulation_time_us: int
    transaction_id: str | None
    checkpoint_id: str | None
    event_prefix_count: int
    event_prefix_sha256: str
    ledger_prefix_count: int
    ledger_prefix_sha256: str
    disposition: str | None
    reason_code: str | None
    details: Mapping[str, object]
    record_sha256: str

    def __post_init__(self) -> None:
        frozen = freeze_json(self.details)
        if not isinstance(frozen, Mapping):
            raise TypeError("recovery evidence details must be a JSON object")
        object.__setattr__(self, "details", frozen)
        if self.sequence <= 0 or self.simulation_time_us < 0:
            raise ValueError("recovery evidence sequence or timestamp is invalid")
        if not self.session_id.startswith("live-session-"):
            raise ValueError("recovery evidence session ID is invalid")
        if type(self.boundary) is not RecoveryBoundaryKindV1:
            raise TypeError("recovery evidence boundary is invalid")
        if self.event_prefix_count < 0 or self.ledger_prefix_count < 0:
            raise ValueError("recovery evidence prefix counts must be nonnegative")
        for value, label in (
            (self.event_prefix_sha256, "event-prefix"),
            (self.ledger_prefix_sha256, "ledger-prefix"),
            (self.record_sha256, "record"),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError(f"recovery evidence {label} digest is invalid")
        for value, label in (
            (self.transaction_id, "transaction ID"),
            (self.checkpoint_id, "checkpoint ID"),
            (self.disposition, "disposition"),
            (self.reason_code, "reason code"),
        ):
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"recovery evidence {label} is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "boundary": self.boundary.value,
            "checkpoint_id": self.checkpoint_id,
            "details": thaw_json(self.details),
            "disposition": self.disposition,
            "event_prefix_count": self.event_prefix_count,
            "event_prefix_sha256": self.event_prefix_sha256,
            "ledger_prefix_count": self.ledger_prefix_count,
            "ledger_prefix_sha256": self.ledger_prefix_sha256,
            "reason_code": self.reason_code,
            "record_sha256": self.record_sha256,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "simulation_time_us": self.simulation_time_us,
            "transaction_id": self.transaction_id,
        }


@dataclass(frozen=True, slots=True)
class InputRecord:
    sequence: int
    simulation_time_us: int
    input_key: str
    resolved_command: str | None
    order_parameters: Mapping[str, object]
    market_state_id: str
    latency_reference_time_us: int
    action_latency_us: int
    accepted: bool
    rejection_reason: str | None
    resulting_order_id: str | None
    resulting_order_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        frozen_parameters = freeze_json(self.order_parameters)
        if not isinstance(frozen_parameters, Mapping):
            raise TypeError("input order parameters must be a JSON object")
        frozen_order_ids = freeze_json(self.resulting_order_ids)
        if not isinstance(frozen_order_ids, tuple) or any(
            type(value) is not str for value in frozen_order_ids
        ):
            raise TypeError("resulting order IDs must be a JSON string sequence")
        object.__setattr__(self, "order_parameters", frozen_parameters)
        object.__setattr__(self, "resulting_order_ids", frozen_order_ids)
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
            "order_parameters": thaw_json(self.order_parameters),
            "rejection_reason": self.rejection_reason,
            "resolved_command": self.resolved_command,
            "resulting_order_id": self.resulting_order_id,
            "resulting_order_ids": list(self.resulting_order_ids),
            "sequence": self.sequence,
            "simulation_timestamp": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> InputRecord:
        parameters = payload.get("order_parameters", {})
        order_ids = payload.get("resulting_order_ids", [])
        if not isinstance(parameters, Mapping) or not isinstance(order_ids, list):
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
    snapshot: Mapping[str, object]

    def __post_init__(self) -> None:
        frozen = freeze_json(self.snapshot)
        if not isinstance(frozen, Mapping):
            raise TypeError("market-state snapshot must be a JSON object")
        object.__setattr__(self, "snapshot", frozen)
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
            "snapshot": thaw_json(self.snapshot),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> MarketStateRecord:
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, Mapping):
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
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        frozen = freeze_json(self.data)
        if not isinstance(frozen, Mapping):
            raise TypeError("timeline data must be a JSON object")
        object.__setattr__(self, "data", frozen)
        if self.sequence <= 0 or self.simulation_time_us < 0 or not self.message:
            raise ValueError("invalid timeline record")

    def as_dict(self) -> dict[str, object]:
        return {
            "data": thaw_json(self.data),
            "kind": self.kind.value,
            "message": self.message,
            "sequence": self.sequence,
            "simulation_timestamp": self.simulation_time_us,
        }
