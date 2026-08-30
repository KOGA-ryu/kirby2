"""Immutable contracts for source-linked mechanistic replay traces."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from kirby2.immutable import freeze_json, thaw_json


TRACE_SOURCE_SCHEMA_ID = "KIRBY2_RECORDED_TRACE_SOURCE_V1"
TRACE_SOURCE_SCHEMA_VERSION = 1
TRACE_INDEX_SCHEMA_ID = "KIRBY2_MECHANISTIC_TRACE_INDEX_V1"
TRACE_INDEX_SCHEMA_VERSION = 1
MECHANISTIC_INTERPRETATION = "MECHANISTIC_TRACE_WITHIN_KIRBY2_MODEL"

_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9]+(?:[-_.:][A-Za-z0-9]+)*$")


class TraceStage(str, Enum):
    OBSERVABLE_EVENT = "OBSERVABLE_EVENT"
    FEATURE_UPDATE = "FEATURE_UPDATE"
    STRATEGY_RULE_EVALUATION = "STRATEGY_RULE_EVALUATION"
    TRAFFIC_LIGHT_TRANSITION = "TRAFFIC_LIGHT_TRANSITION"
    PLAYER_INPUT = "PLAYER_INPUT"
    CLIENT_ORDER_CREATION = "CLIENT_ORDER_CREATION"
    ROUTING = "ROUTING"
    VENUE_RECEIPT = "VENUE_RECEIPT"
    QUEUE_PLACEMENT = "QUEUE_PLACEMENT"
    FILL_OR_CANCEL = "FILL_OR_CANCEL"
    LATER_ADVERSE_SELECTION = "LATER_ADVERSE_SELECTION"


TRACE_STAGE_ORDER = tuple(TraceStage)


class TraceArtifactKind(str, Enum):
    OBSERVABLE_EVENT = "OBSERVABLE_EVENT"
    FEATURE_UPDATE = "FEATURE_UPDATE"
    RECORDED_STRATEGY_DECISION = "RECORDED_STRATEGY_DECISION"
    TRAFFIC_LIGHT_TRANSITION = "TRAFFIC_LIGHT_TRANSITION"
    PLAYER_INPUT = "PLAYER_INPUT"
    CLIENT_ORDER = "CLIENT_ORDER"
    ROUTE_DECISION = "ROUTE_DECISION"
    VENUE_RECEIPT = "VENUE_RECEIPT"
    QUEUE_STATE = "QUEUE_STATE"
    ORDER_OUTCOME = "ORDER_OUTCOME"
    ADVERSE_SELECTION = "ADVERSE_SELECTION"


EXPECTED_ARTIFACT_KIND = {
    TraceStage.OBSERVABLE_EVENT: TraceArtifactKind.OBSERVABLE_EVENT,
    TraceStage.FEATURE_UPDATE: TraceArtifactKind.FEATURE_UPDATE,
    TraceStage.STRATEGY_RULE_EVALUATION: (
        TraceArtifactKind.RECORDED_STRATEGY_DECISION
    ),
    TraceStage.TRAFFIC_LIGHT_TRANSITION: (
        TraceArtifactKind.TRAFFIC_LIGHT_TRANSITION
    ),
    TraceStage.PLAYER_INPUT: TraceArtifactKind.PLAYER_INPUT,
    TraceStage.CLIENT_ORDER_CREATION: TraceArtifactKind.CLIENT_ORDER,
    TraceStage.ROUTING: TraceArtifactKind.ROUTE_DECISION,
    TraceStage.VENUE_RECEIPT: TraceArtifactKind.VENUE_RECEIPT,
    TraceStage.QUEUE_PLACEMENT: TraceArtifactKind.QUEUE_STATE,
    TraceStage.FILL_OR_CANCEL: TraceArtifactKind.ORDER_OUTCOME,
    TraceStage.LATER_ADVERSE_SELECTION: TraceArtifactKind.ADVERSE_SELECTION,
}


class TraceEdgeKind(str, Enum):
    OBSERVATION_TO_FEATURE = "OBSERVATION_TO_FEATURE"
    FEATURE_TO_RULE = "FEATURE_TO_RULE"
    RULE_TO_TRAFFIC_LIGHT = "RULE_TO_TRAFFIC_LIGHT"
    TRAFFIC_LIGHT_TO_PLAYER_INPUT = "TRAFFIC_LIGHT_TO_PLAYER_INPUT"
    PLAYER_INPUT_TO_CLIENT_ORDER = "PLAYER_INPUT_TO_CLIENT_ORDER"
    CLIENT_ORDER_TO_ROUTING = "CLIENT_ORDER_TO_ROUTING"
    ROUTING_TO_VENUE_RECEIPT = "ROUTING_TO_VENUE_RECEIPT"
    VENUE_RECEIPT_TO_QUEUE = "VENUE_RECEIPT_TO_QUEUE"
    QUEUE_TO_FILL_OR_CANCEL = "QUEUE_TO_FILL_OR_CANCEL"
    FILL_OR_CANCEL_TO_ADVERSE_SELECTION = (
        "FILL_OR_CANCEL_TO_ADVERSE_SELECTION"
    )


TRACE_EDGE_ORDER = tuple(TraceEdgeKind)


class TraceAvailability(str, Enum):
    RECORDED = "RECORDED"
    UNAVAILABLE = "UNAVAILABLE"


class TraceLinkStatus(str, Enum):
    LINKED = "LINKED"
    UNAVAILABLE = "UNAVAILABLE"


class TraceUnavailableReason(str, Enum):
    SOURCE_EVENT_MISSING = "SOURCE_EVENT_MISSING"
    AMBIGUOUS_SOURCE_EVENTS = "AMBIGUOUS_SOURCE_EVENTS"
    RECORDED_ARTIFACT_KIND_MISMATCH = "RECORDED_ARTIFACT_KIND_MISMATCH"
    ENDPOINT_UNAVAILABLE = "ENDPOINT_UNAVAILABLE"
    EXPLICIT_PARENT_LINK_MISSING = "EXPLICIT_PARENT_LINK_MISSING"
    CORRELATION_ID_MISSING = "CORRELATION_ID_MISSING"
    RECORDED_ORDER_INVALID = "RECORDED_ORDER_INVALID"


@dataclass(frozen=True, slots=True)
class TraceProvenance:
    run_id: str
    artifact_name: str
    artifact_sha256: str
    schema_id: str
    schema_version: int
    event_sequence: int

    def __post_init__(self) -> None:
        if not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("trace provenance run ID is invalid")
        _require_identifier(self.artifact_name, "trace provenance artifact name")
        _require_identifier(self.schema_id, "trace provenance schema ID")
        if not _SHA256.fullmatch(self.artifact_sha256):
            raise ValueError("trace provenance artifact digest is invalid")
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValueError("trace provenance schema version must be positive")
        if type(self.event_sequence) is not int or self.event_sequence <= 0:
            raise ValueError("trace provenance event sequence must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_name": self.artifact_name,
            "artifact_sha256": self.artifact_sha256,
            "event_sequence": self.event_sequence,
            "run_id": self.run_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class RecordedTraceEvent:
    event_id: str
    action_id: str
    stage: TraceStage
    artifact_kind: TraceArtifactKind
    simulation_time_us: int
    correlation_ids: tuple[str, ...]
    parent_event_ids: tuple[str, ...]
    provenance: TraceProvenance
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_identifier(self.event_id, "trace event ID")
        _require_identifier(self.action_id, "trace action ID")
        if not isinstance(self.stage, TraceStage):
            raise TypeError("trace event stage must use TraceStage")
        if not isinstance(self.artifact_kind, TraceArtifactKind):
            raise TypeError("trace event artifact kind must use TraceArtifactKind")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("trace event simulation time must be nonnegative")
        correlations = _canonical_identifiers(
            self.correlation_ids,
            "trace event correlation IDs",
        )
        parents = _canonical_identifiers(
            self.parent_event_ids,
            "trace event parent IDs",
        )
        if self.event_id in parents:
            raise ValueError("trace event cannot be its own parent")
        frozen = freeze_json(self.payload)
        if not isinstance(frozen, Mapping):
            raise TypeError("trace event payload must be a JSON object")
        object.__setattr__(self, "correlation_ids", correlations)
        object.__setattr__(self, "parent_event_ids", parents)
        object.__setattr__(self, "payload", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "artifact_kind": self.artifact_kind.value,
            "correlation_ids": list(self.correlation_ids),
            "event_id": self.event_id,
            "parent_event_ids": list(self.parent_event_ids),
            "payload": thaw_json(self.payload),
            "provenance": self.provenance.as_dict(),
            "simulation_time_us": self.simulation_time_us,
            "stage": self.stage.value,
        }


@dataclass(frozen=True, slots=True)
class TraceSourceRecording:
    run_id: str
    events: tuple[RecordedTraceEvent, ...]
    schema_id: str = TRACE_SOURCE_SCHEMA_ID
    schema_version: int = TRACE_SOURCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("trace source run ID is invalid")
        if self.schema_id != TRACE_SOURCE_SCHEMA_ID:
            raise ValueError("unsupported trace source schema ID")
        if self.schema_version != TRACE_SOURCE_SCHEMA_VERSION:
            raise ValueError("unsupported trace source schema version")
        events = tuple(
            sorted(
                self.events,
                key=lambda item: (item.provenance.event_sequence, item.event_id),
            )
        )
        if not events:
            raise ValueError("trace source recording must contain events")
        event_ids = tuple(item.event_id for item in events)
        sequences = tuple(item.provenance.event_sequence for item in events)
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("trace source event IDs must be unique")
        if len(sequences) != len(set(sequences)):
            raise ValueError("trace source event sequences must be unique")
        if any(item.provenance.run_id != self.run_id for item in events):
            raise ValueError("trace source provenance points at another run")
        player_events = tuple(
            item for item in events if item.stage is TraceStage.PLAYER_INPUT
        )
        if not player_events:
            raise ValueError("trace source recording must contain a player action")
        if any(
            item.artifact_kind is not TraceArtifactKind.PLAYER_INPUT
            for item in player_events
        ):
            raise ValueError("recorded player input has the wrong artifact kind")
        player_actions = tuple(item.action_id for item in player_events)
        if len(player_actions) != len(set(player_actions)):
            raise ValueError("player input action IDs must be unique")
        object.__setattr__(self, "events", events)

    @property
    def player_action_ids(self) -> tuple[str, ...]:
        return tuple(
            item.action_id
            for item in self.events
            if item.stage is TraceStage.PLAYER_INPUT
        )

    @property
    def source_event_sha256(self) -> str:
        return _canonical_sha256([item.as_dict() for item in self.events])

    def as_dict(self) -> dict[str, object]:
        return {
            "events": [item.as_dict() for item in self.events],
            "run_id": self.run_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class TraceNode:
    stage: TraceStage
    availability: TraceAvailability
    source_event_id: str | None
    provenance: TraceProvenance | None
    unavailable_reason: TraceUnavailableReason | None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, TraceStage):
            raise TypeError("trace node stage must use TraceStage")
        if not isinstance(self.availability, TraceAvailability):
            raise TypeError("trace node availability must use TraceAvailability")
        if self.source_event_id is not None:
            _require_identifier(self.source_event_id, "trace node source event ID")
        if self.availability is TraceAvailability.RECORDED:
            if (
                self.source_event_id is None
                or self.provenance is None
                or self.unavailable_reason is not None
            ):
                raise ValueError("recorded trace node requires one exact source")
        elif self.unavailable_reason is None:
            raise ValueError("unavailable trace node requires a typed reason")

    def as_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "provenance": (
                None if self.provenance is None else self.provenance.as_dict()
            ),
            "source_event_id": self.source_event_id,
            "stage": self.stage.value,
            "unavailable_reason": (
                None
                if self.unavailable_reason is None
                else self.unavailable_reason.value
            ),
        }


@dataclass(frozen=True, slots=True)
class TraceEdge:
    kind: TraceEdgeKind
    from_stage: TraceStage
    to_stage: TraceStage
    status: TraceLinkStatus
    from_event_id: str | None
    to_event_id: str | None
    correlation_ids: tuple[str, ...]
    provenance: tuple[TraceProvenance, ...]
    unavailable_reason: TraceUnavailableReason | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TraceEdgeKind):
            raise TypeError("trace edge kind must use TraceEdgeKind")
        if not isinstance(self.from_stage, TraceStage) or not isinstance(
            self.to_stage,
            TraceStage,
        ):
            raise TypeError("trace edge endpoints must use TraceStage")
        if not isinstance(self.status, TraceLinkStatus):
            raise TypeError("trace edge status must use TraceLinkStatus")
        correlations = _canonical_identifiers(
            self.correlation_ids,
            "trace edge correlation IDs",
        )
        for event_id in (self.from_event_id, self.to_event_id):
            if event_id is not None:
                _require_identifier(event_id, "trace edge source event ID")
        if self.status is TraceLinkStatus.LINKED:
            if (
                self.from_event_id is None
                or self.to_event_id is None
                or not correlations
                or len(self.provenance) != 2
                or self.unavailable_reason is not None
            ):
                raise ValueError(
                    "linked trace edge requires endpoints, correlation, and provenance"
                )
        elif self.unavailable_reason is None:
            raise ValueError("unavailable trace edge requires a typed reason")
        object.__setattr__(self, "correlation_ids", correlations)

    def as_dict(self) -> dict[str, object]:
        return {
            "correlation_ids": list(self.correlation_ids),
            "from_event_id": self.from_event_id,
            "from_stage": self.from_stage.value,
            "kind": self.kind.value,
            "provenance": [item.as_dict() for item in self.provenance],
            "status": self.status.value,
            "to_event_id": self.to_event_id,
            "to_stage": self.to_stage.value,
            "unavailable_reason": (
                None
                if self.unavailable_reason is None
                else self.unavailable_reason.value
            ),
        }


@dataclass(frozen=True, slots=True)
class PlayerActionTrace:
    action_id: str
    player_input_event_id: str
    nodes: tuple[TraceNode, ...]
    edges: tuple[TraceEdge, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.action_id, "player action trace ID")
        _require_identifier(
            self.player_input_event_id,
            "player action input event ID",
        )
        if tuple(item.stage for item in self.nodes) != TRACE_STAGE_ORDER:
            raise ValueError("player action trace node inventory or order differs")
        if tuple(item.kind for item in self.edges) != TRACE_EDGE_ORDER:
            raise ValueError("player action trace edge inventory or order differs")
        player_node = self.nodes[TRACE_STAGE_ORDER.index(TraceStage.PLAYER_INPUT)]
        if (
            player_node.availability is not TraceAvailability.RECORDED
            or player_node.source_event_id != self.player_input_event_id
        ):
            raise ValueError("player action trace does not bind its input event")

    @property
    def complete(self) -> bool:
        return all(
            item.availability is TraceAvailability.RECORDED for item in self.nodes
        ) and all(item.status is TraceLinkStatus.LINKED for item in self.edges)

    @property
    def unavailable_node_count(self) -> int:
        return sum(
            item.availability is TraceAvailability.UNAVAILABLE for item in self.nodes
        )

    @property
    def unavailable_edge_count(self) -> int:
        return sum(item.status is TraceLinkStatus.UNAVAILABLE for item in self.edges)

    def as_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "complete": self.complete,
            "edges": [item.as_dict() for item in self.edges],
            "nodes": [item.as_dict() for item in self.nodes],
            "player_input_event_id": self.player_input_event_id,
            "unavailable_edge_count": self.unavailable_edge_count,
            "unavailable_node_count": self.unavailable_node_count,
        }


@dataclass(frozen=True, slots=True)
class MechanisticTraceIndex:
    source_run_id: str
    source_event_sha256: str
    traces: tuple[PlayerActionTrace, ...]
    schema_id: str = TRACE_INDEX_SCHEMA_ID
    schema_version: int = TRACE_INDEX_SCHEMA_VERSION
    interpretation: str = MECHANISTIC_INTERPRETATION
    lineage_sha256: str = field(init=False)
    index_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not _RUN_ID.fullmatch(self.source_run_id):
            raise ValueError("mechanistic trace source run ID is invalid")
        if not _SHA256.fullmatch(self.source_event_sha256):
            raise ValueError("mechanistic trace source digest is invalid")
        if self.schema_id != TRACE_INDEX_SCHEMA_ID:
            raise ValueError("unsupported mechanistic trace index schema ID")
        if self.schema_version != TRACE_INDEX_SCHEMA_VERSION:
            raise ValueError("unsupported mechanistic trace index schema version")
        if self.interpretation != MECHANISTIC_INTERPRETATION:
            raise ValueError("mechanistic trace interpretation is not exact")
        if not self.traces:
            raise ValueError("mechanistic trace index must contain player actions")
        action_ids = tuple(item.action_id for item in self.traces)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("mechanistic trace action IDs must be unique")
        lineage_sha256 = _canonical_sha256(
            [item.as_dict() for item in self.traces]
        )
        object.__setattr__(self, "lineage_sha256", lineage_sha256)
        object.__setattr__(
            self,
            "index_id",
            "trace-index-" + _canonical_sha256(self.identity_dict())[:24],
        )

    @property
    def complete_action_count(self) -> int:
        return sum(item.complete for item in self.traces)

    def identity_dict(self) -> dict[str, object]:
        return {
            "interpretation": self.interpretation,
            "lineage_sha256": self.lineage_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "index_id": self.index_id,
            "interpretation": self.interpretation,
            "lineage_sha256": self.lineage_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "traces": [item.as_dict() for item in self.traces],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


def _canonical_identifiers(
    values: tuple[str, ...],
    label: str,
) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise TypeError(f"{label} must be a tuple")
    for value in values:
        _require_identifier(value, label)
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return tuple(sorted(values))


def _require_identifier(value: str, label: str) -> None:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
