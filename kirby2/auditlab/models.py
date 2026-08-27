"""Immutable contracts for the Kirby2 model-risk laboratory."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from kirby2.immutable import freeze_json, thaw_json


AUDIT_LAB_SCHEMA_VERSION = 2
AUDIT_PACKET_SCHEMA_VERSION = 2
LEGACY_AUDIT_PACKET_SCHEMA_VERSION = 1
_ACCEPTANCE_ID = re.compile(r"^acceptance-[A-Za-z0-9_-]{1,96}$")
_CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_CELL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CASE_RECORDING_FIELDS = frozenset(
    {
        "expected_outputs",
        "lane",
        "payload",
        "recording_type",
        "schema_version",
        "sha256",
    }
)
_FAULT_OBSERVATION_FIELDS = frozenset(
    {
        "details",
        "detector",
        "fault",
        "injection_event",
        "injection_location",
        "manifest",
        "observed_code",
        "raw_events",
        "raw_issues",
        "subsystem",
    }
)
CASE_RECORDING_SCHEMA_VERSION = 2


class UnsupportedSchemaVersionError(ValueError):
    """Stable production-loader refusal for an unsupported schema version."""

    code = "UNSUPPORTED_SCHEMA_VERSION"

    def __init__(self, *, artifact: str, expected: int, actual: object) -> None:
        self.artifact = artifact
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"unsupported {artifact} schema version: expected {expected}, got {actual!r}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "actual_schema_version": self.actual,
            "artifact": self.artifact,
            "code": self.code,
            "expected_schema_version": self.expected,
            "message": str(self),
        }


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _serialized_int(payload: Mapping[str, object], name: str) -> int:
    value = payload[name]
    if type(value) is not int:
        raise TypeError(f"serialized {name} must be an integer")
    return value


def _serialized_str(payload: Mapping[str, object], name: str) -> str:
    value = payload[name]
    if type(value) is not str:
        raise TypeError(f"serialized {name} must be a string")
    return value


def _frozen_mapping(value: object, name: str) -> Mapping[str, object]:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    return frozen


class FaultKind(str, Enum):
    DUPLICATE_MESSAGE = "DUPLICATE_MESSAGE"
    DROPPED_MARKET_DATA = "DROPPED_MARKET_DATA"
    DELAYED_ACKNOWLEDGEMENT = "DELAYED_ACKNOWLEDGEMENT"
    OUT_OF_ORDER_DELIVERY = "OUT_OF_ORDER_DELIVERY"
    SNAPSHOT_GAP = "SNAPSHOT_GAP"
    CORRUPTED_DATASET_ROW = "CORRUPTED_DATASET_ROW"
    VENUE_REJECTION = "VENUE_REJECTION"
    HALT_DURING_PENDING_ORDER = "HALT_DURING_PENDING_ORDER"
    CANCEL_FILL_RACE = "CANCEL_FILL_RACE"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"


class ExecutorLane(str, Enum):
    CORE_FLOW = "CORE_FLOW"
    MECHANICS = "MECHANICS"
    LATENCY = "LATENCY"
    FRAGMENTED = "FRAGMENTED"
    ECOLOGY = "ECOLOGY"
    ALGORITHM = "ALGORITHM"
    FAULT = "FAULT"


class ExperimentPartition(str, Enum):
    TRAIN = "TRAIN"
    HOLDOUT = "HOLDOUT"
    FAULT = "FAULT"


class ExerciseStatus(str, Enum):
    EXERCISED = "EXERCISED"
    NOT_EXERCISED = "NOT_EXERCISED"


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EXERCISED = "NOT_EXERCISED"


class FailureKind(str, Enum):
    EXECUTION_ERROR = "EXECUTION_ERROR"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"
    REPLAY_MISMATCH = "REPLAY_MISMATCH"
    DETERMINISM_MISMATCH = "DETERMINISM_MISMATCH"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    OBSERVABILITY_LEAK = "OBSERVABILITY_LEAK"
    DATA_INTEGRITY = "DATA_INTEGRITY"
    UNCLASSIFIED = "UNCLASSIFIED"


class FailurePredicateKind(str, Enum):
    STRUCTURAL_CHECK = "STRUCTURAL_CHECK"
    REPLAY_MISMATCH = "REPLAY_MISMATCH"
    DETERMINISM_MISMATCH = "DETERMINISM_MISMATCH"
    FAULT_MISS = "FAULT_MISS"
    SUBSYSTEM_PROBE = "SUBSYSTEM_PROBE"


class AutomatedStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


_GENERATED_CONFIGURATION_FIELDS = frozenset(
    {
        "agent_count",
        "agent_population",
        "auction_state",
        "cell_id",
        "duration_events",
        "duration_us",
        "flow_model",
        "hidden_liquidity",
        "injected_fault",
        "lane",
        "latency",
        "liquidity",
        "objective",
        "order_types",
        "partition",
        "regime",
        "replicate_index",
        "schema_version",
        "seed",
        "sequence",
        "session_phase",
        "strategy",
        "venue_count",
        "volume",
    }
)


@dataclass(frozen=True, slots=True)
class GeneratedConfiguration:
    """One fully declared generative case; no value is read from global state."""

    sequence: int
    lane: ExecutorLane
    cell_id: str
    replicate_index: int
    partition: ExperimentPartition
    seed: int
    duration_us: int
    duration_events: int
    agent_count: int
    flow_model: str
    regime: str
    volume: str
    liquidity: str
    latency: str
    session_phase: str
    order_types: str
    hidden_liquidity: str
    venue_count: int
    auction_state: str
    agent_population: str
    strategy: str
    objective: str
    injected_fault: FaultKind | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("configuration sequence must be positive")
        if not isinstance(self.lane, ExecutorLane):
            raise TypeError("configuration lane must use ExecutorLane")
        if type(self.cell_id) is not str or not _CELL_ID.fullmatch(self.cell_id):
            raise ValueError("configuration cell ID is invalid")
        if type(self.replicate_index) is not int or self.replicate_index < 0:
            raise ValueError("configuration replicate index must be nonnegative")
        if not isinstance(self.partition, ExperimentPartition):
            raise TypeError("configuration partition must use ExperimentPartition")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("configuration seed must be nonnegative")
        if type(self.duration_events) is not int or self.duration_events <= 0:
            raise ValueError("configuration duration must be positive")
        if type(self.duration_us) is not int or self.duration_us <= 0:
            raise ValueError("configuration duration must be positive microseconds")
        if type(self.agent_count) is not int or not 1 <= self.agent_count <= 12:
            raise ValueError("configuration agent count must be in 1..12")
        if type(self.venue_count) is not int or not 1 <= self.venue_count <= 4:
            raise ValueError("configuration venue count must be in 1..4")
        textual = (
            self.flow_model,
            self.regime,
            self.volume,
            self.liquidity,
            self.latency,
            self.session_phase,
            self.order_types,
            self.hidden_liquidity,
            self.auction_state,
            self.agent_population,
            self.strategy,
            self.objective,
        )
        if any(type(value) is not str or not value for value in textual):
            raise TypeError("configuration dimensions must be nonempty strings")
        if self.injected_fault is not None and not isinstance(
            self.injected_fault, FaultKind
        ):
            raise TypeError("injected fault must use FaultKind")
        if self.lane is ExecutorLane.FAULT:
            if self.partition is not ExperimentPartition.FAULT:
                raise ValueError("fault-lane configuration requires FAULT partition")
            if self.injected_fault is None:
                raise ValueError("fault-lane configuration requires an injected fault")
            if self.replicate_index >= len(FaultKind):
                raise ValueError("fault replicate index must address one fault kind")
        else:
            if self.partition is ExperimentPartition.FAULT:
                raise ValueError("scientific configuration cannot use FAULT partition")
            if self.injected_fault is not None:
                raise ValueError("injected faults are confined to the FAULT lane")
            if not 0 <= self.replicate_index < 6:
                raise ValueError("scientific replicate index must be in 0..5")
            expected_partition = (
                ExperimentPartition.TRAIN
                if self.replicate_index < 3
                else ExperimentPartition.HOLDOUT
            )
            if self.partition is not expected_partition:
                raise ValueError("scientific replicate partition is inconsistent")

    def as_dict(self) -> dict[str, object]:
        return {
            "agent_count": self.agent_count,
            "agent_population": self.agent_population,
            "auction_state": self.auction_state,
            "cell_id": self.cell_id,
            "duration_events": self.duration_events,
            "duration_us": self.duration_us,
            "flow_model": self.flow_model,
            "hidden_liquidity": self.hidden_liquidity,
            "injected_fault": (
                None if self.injected_fault is None else self.injected_fault.value
            ),
            "latency": self.latency,
            "lane": self.lane.value,
            "liquidity": self.liquidity,
            "objective": self.objective,
            "order_types": self.order_types,
            "partition": self.partition.value,
            "regime": self.regime,
            "replicate_index": self.replicate_index,
            "schema_version": AUDIT_LAB_SCHEMA_VERSION,
            "seed": self.seed,
            "sequence": self.sequence,
            "session_phase": self.session_phase,
            "strategy": self.strategy,
            "venue_count": self.venue_count,
            "volume": self.volume,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> GeneratedConfiguration:
        fields = set(payload)
        missing = sorted(_GENERATED_CONFIGURATION_FIELDS.difference(fields))
        unknown = sorted(fields.difference(_GENERATED_CONFIGURATION_FIELDS))
        if missing or unknown:
            raise ValueError(
                "generated-configuration fields are not exact: "
                f"missing={missing} unknown={unknown}"
            )
        if (
            _serialized_int(payload, "schema_version")
            != AUDIT_LAB_SCHEMA_VERSION
        ):
            raise UnsupportedSchemaVersionError(
                artifact="audit-lab configuration",
                expected=AUDIT_LAB_SCHEMA_VERSION,
                actual=payload["schema_version"],
            )
        raw_fault = payload.get("injected_fault")
        if raw_fault is not None and type(raw_fault) is not str:
            raise TypeError("serialized injected fault must be a string or null")
        return cls(
            sequence=_serialized_int(payload, "sequence"),
            lane=ExecutorLane(_serialized_str(payload, "lane")),
            cell_id=_serialized_str(payload, "cell_id"),
            replicate_index=_serialized_int(payload, "replicate_index"),
            partition=ExperimentPartition(_serialized_str(payload, "partition")),
            seed=_serialized_int(payload, "seed"),
            duration_us=_serialized_int(payload, "duration_us"),
            duration_events=_serialized_int(payload, "duration_events"),
            agent_count=_serialized_int(payload, "agent_count"),
            flow_model=_serialized_str(payload, "flow_model"),
            regime=_serialized_str(payload, "regime"),
            volume=_serialized_str(payload, "volume"),
            liquidity=_serialized_str(payload, "liquidity"),
            latency=_serialized_str(payload, "latency"),
            session_phase=_serialized_str(payload, "session_phase"),
            order_types=_serialized_str(payload, "order_types"),
            hidden_liquidity=_serialized_str(payload, "hidden_liquidity"),
            venue_count=_serialized_int(payload, "venue_count"),
            auction_state=_serialized_str(payload, "auction_state"),
            agent_population=_serialized_str(payload, "agent_population"),
            strategy=_serialized_str(payload, "strategy"),
            objective=_serialized_str(payload, "objective"),
            injected_fault=None if raw_fault is None else FaultKind(str(raw_fault)),
        )


@dataclass(frozen=True, slots=True)
class ExerciseRecord:
    lane: ExecutorLane
    capability: str
    configured_value: object
    status: ExerciseStatus
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.lane, ExecutorLane):
            raise TypeError("exercise lane must use ExecutorLane")
        if type(self.capability) is not str or not _CAPABILITY_NAME.fullmatch(
            self.capability
        ):
            raise ValueError("exercise capability name is invalid")
        if not isinstance(self.status, ExerciseStatus):
            raise TypeError("exercise status must use ExerciseStatus")
        frozen_value = freeze_json(self.configured_value)
        if isinstance(frozen_value, (Mapping, tuple)):
            raise TypeError("exercise configured value must be a JSON scalar")
        frozen_evidence = _frozen_mapping(self.evidence, "exercise evidence")
        if not frozen_evidence:
            raise ValueError("exercise evidence must identify its source")
        object.__setattr__(self, "configured_value", frozen_value)
        object.__setattr__(self, "evidence", frozen_evidence)

    def as_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "configured_value": thaw_json(self.configured_value),
            "evidence": thaw_json(self.evidence),
            "lane": self.lane.value,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    required: bool
    detail: str
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.name) is not str or not _CAPABILITY_NAME.fullmatch(self.name):
            raise ValueError("check name is invalid")
        if not isinstance(self.status, CheckStatus):
            raise TypeError("check status must use CheckStatus")
        if type(self.required) is not bool:
            raise TypeError("check required flag must be boolean")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("check detail is required")
        frozen = _frozen_mapping(self.evidence, "check evidence")
        if not frozen:
            raise ValueError("check evidence must identify its source or refusal")
        object.__setattr__(self, "evidence", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "evidence": thaw_json(self.evidence),
            "name": self.name,
            "required": self.required,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class FailureObservation:
    kind: FailureKind
    code: str
    message: str
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FailureKind):
            raise TypeError("failure observation kind must use FailureKind")
        if (
            type(self.code) is not str
            or not self.code
            or type(self.message) is not str
            or not self.message
        ):
            raise ValueError("failure observation code and message are required")
        frozen = _frozen_mapping(self.evidence, "failure observation evidence")
        if not frozen:
            raise ValueError("failure observation requires source evidence")
        object.__setattr__(self, "evidence", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "evidence": thaw_json(self.evidence),
            "kind": self.kind.value,
            "message": self.message,
        }

    def identity(
        self,
        *,
        predicate: FailurePredicateKind,
        lane: ExecutorLane,
        field_name: str,
        source_configuration_sha256: str,
        source_recording_sha256: str,
        predicate_parameters: Mapping[str, object] | None = None,
    ) -> FailureIdentity:
        return FailureIdentity(
            predicate=predicate,
            kind=self.kind,
            code=self.code,
            lane=lane,
            field_name=field_name,
            source_configuration_sha256=source_configuration_sha256,
            source_recording_sha256=source_recording_sha256,
            predicate_parameters=(
                {} if predicate_parameters is None else predicate_parameters
            ),
        )


@dataclass(frozen=True, slots=True)
class FailureIdentity:
    predicate: FailurePredicateKind
    kind: FailureKind
    code: str
    lane: ExecutorLane
    field_name: str
    source_configuration_sha256: str
    source_recording_sha256: str
    predicate_parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.predicate, FailurePredicateKind):
            raise TypeError("failure identity predicate is invalid")
        if not isinstance(self.kind, FailureKind):
            raise TypeError("failure identity kind is invalid")
        if not isinstance(self.lane, ExecutorLane):
            raise TypeError("failure identity lane is invalid")
        if any(
            type(value) is not str or not value
            for value in (self.code, self.field_name)
        ):
            raise ValueError("failure identity code and field are required")
        if any(
            type(value) is not str or not _SHA256.fullmatch(value)
            for value in (
                self.source_configuration_sha256,
                self.source_recording_sha256,
            )
        ):
            raise ValueError("failure identity source digests are invalid")
        object.__setattr__(
            self,
            "predicate_parameters",
            _frozen_mapping(
                self.predicate_parameters,
                "failure identity predicate parameters",
            ),
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "field_name": self.field_name,
            "kind": self.kind.value,
            "lane": self.lane.value,
            "predicate": self.predicate.value,
            "predicate_parameters": thaw_json(self.predicate_parameters),
            "source_configuration_sha256": self.source_configuration_sha256,
            "source_recording_sha256": self.source_recording_sha256,
        }

    @property
    def signature(self) -> str:
        return (
            f"{self.predicate.value}:{self.lane.value}:{self.kind.value}:"
            f"{self.code}:{self.field_name}:"
            f"{canonical_sha256(self.identity_dict())[:20]}"
        )

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "signature": self.signature}


@dataclass(frozen=True, slots=True)
class CaseRecording:
    lane: ExecutorLane
    recording_type: str
    payload: Mapping[str, object]
    expected_outputs: Mapping[str, object] | None = None
    schema_version: int = CASE_RECORDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.lane, ExecutorLane):
            raise TypeError("recording lane must use ExecutorLane")
        if type(self.recording_type) is not str or not self.recording_type:
            raise ValueError("recording type is required")
        if (
            type(self.schema_version) is not int
            or self.schema_version != CASE_RECORDING_SCHEMA_VERSION
        ):
            raise UnsupportedSchemaVersionError(
                artifact="case recording",
                expected=CASE_RECORDING_SCHEMA_VERSION,
                actual=self.schema_version,
            )
        frozen = _frozen_mapping(self.payload, "case recording payload")
        if not frozen:
            raise ValueError("case recording payload must not be empty")
        frozen_outputs = _frozen_mapping(
            {} if self.expected_outputs is None else self.expected_outputs,
            "case recording expected outputs",
        )
        object.__setattr__(self, "payload", frozen)
        object.__setattr__(self, "expected_outputs", frozen_outputs)

    def identity_dict(self) -> dict[str, object]:
        return {
            "expected_outputs": thaw_json(self.expected_outputs),
            "lane": self.lane.value,
            "payload": thaw_json(self.payload),
            "recording_type": self.recording_type,
            "schema_version": self.schema_version,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "sha256": self.sha256}

    def with_expected_outputs(
        self,
        expected_outputs: Mapping[str, object],
    ) -> CaseRecording:
        if self.expected_outputs:
            raise ValueError("case recording expectations are already finalized")
        if not expected_outputs:
            raise ValueError("case recording expectations must not be empty")
        return CaseRecording(
            lane=self.lane,
            recording_type=self.recording_type,
            payload=self.payload,
            expected_outputs=expected_outputs,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CaseRecording:
        if not isinstance(payload, Mapping):
            raise TypeError("serialized case recording must be an object")
        fields = set(payload)
        missing = sorted(_CASE_RECORDING_FIELDS.difference(fields))
        unknown = sorted(fields.difference(_CASE_RECORDING_FIELDS))
        if missing or unknown:
            raise ValueError(
                "case-recording fields are not exact: "
                f"missing={missing} unknown={unknown}"
            )
        schema_version = _serialized_int(payload, "schema_version")
        if schema_version != CASE_RECORDING_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                artifact="case recording",
                expected=CASE_RECORDING_SCHEMA_VERSION,
                actual=schema_version,
            )
        raw_payload = payload["payload"]
        raw_outputs = payload["expected_outputs"]
        if not isinstance(raw_payload, Mapping):
            raise TypeError("serialized case recording payload must be an object")
        if not isinstance(raw_outputs, Mapping) or not raw_outputs:
            raise ValueError(
                "serialized case recording expected outputs must be nonempty"
            )
        recording = cls(
            lane=ExecutorLane(_serialized_str(payload, "lane")),
            recording_type=_serialized_str(payload, "recording_type"),
            payload=raw_payload,
            expected_outputs=raw_outputs,
            schema_version=schema_version,
        )
        serialized_sha256 = _serialized_str(payload, "sha256")
        if not _SHA256.fullmatch(serialized_sha256):
            raise ValueError("serialized case recording digest is invalid")
        if recording.sha256 != serialized_sha256:
            raise ValueError("serialized case recording digest does not match content")
        return recording


@dataclass(frozen=True, slots=True)
class FaultObservation:
    """Raw production evidence from one fault injection, without an oracle."""

    fault: FaultKind
    subsystem: str
    detector: str
    injection_location: str
    observed_code: str | None
    injection_event: int
    manifest: Mapping[str, object]
    raw_events: tuple[Mapping[str, object], ...]
    raw_issues: tuple[Mapping[str, object], ...]
    details: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.fault, FaultKind):
            raise TypeError("fault observation must use FaultKind")
        if any(
            type(value) is not str or not value
            for value in (self.subsystem, self.detector, self.injection_location)
        ):
            raise ValueError("fault observation source identity is incomplete")
        if self.observed_code is not None and (
            type(self.observed_code) is not str or not self.observed_code
        ):
            raise ValueError("fault observed code must be nonempty or absent")
        if type(self.injection_event) is not int or self.injection_event <= 0:
            raise ValueError("fault injection event must be positive")
        frozen_manifest = _frozen_mapping(self.manifest, "fault manifest")
        frozen_events = freeze_json(self.raw_events)
        frozen_issues = freeze_json(self.raw_issues)
        frozen_details = _frozen_mapping(self.details, "fault details")
        if not frozen_manifest:
            raise ValueError("fault manifest must not be empty")
        if not isinstance(frozen_events, tuple) or any(
            not isinstance(item, Mapping) for item in frozen_events
        ):
            raise TypeError("fault raw events must contain JSON objects")
        if not isinstance(frozen_issues, tuple) or any(
            not isinstance(item, Mapping) for item in frozen_issues
        ):
            raise TypeError("fault raw issues must contain JSON objects")
        if not frozen_events and not frozen_issues:
            raise ValueError("fault observation requires raw events or issues")
        object.__setattr__(self, "manifest", frozen_manifest)
        object.__setattr__(self, "raw_events", frozen_events)
        object.__setattr__(self, "raw_issues", frozen_issues)
        object.__setattr__(self, "details", frozen_details)

    def as_dict(self) -> dict[str, object]:
        return {
            "details": thaw_json(self.details),
            "detector": self.detector,
            "fault": self.fault.value,
            "injection_event": self.injection_event,
            "injection_location": self.injection_location,
            "manifest": thaw_json(self.manifest),
            "observed_code": self.observed_code,
            "raw_events": thaw_json(self.raw_events),
            "raw_issues": thaw_json(self.raw_issues),
            "subsystem": self.subsystem,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FaultObservation:
        if set(payload) != _FAULT_OBSERVATION_FIELDS:
            raise ValueError("fault-observation fields are not exact")
        raw_manifest = payload["manifest"]
        raw_events = payload["raw_events"]
        raw_issues = payload["raw_issues"]
        raw_details = payload["details"]
        if not isinstance(raw_manifest, Mapping):
            raise TypeError("fault-observation manifest must be an object")
        if not isinstance(raw_events, (tuple, list)) or any(
            not isinstance(item, Mapping) for item in raw_events
        ):
            raise TypeError("fault-observation raw events must be objects")
        if not isinstance(raw_issues, (tuple, list)) or any(
            not isinstance(item, Mapping) for item in raw_issues
        ):
            raise TypeError("fault-observation raw issues must be objects")
        if not isinstance(raw_details, Mapping):
            raise TypeError("fault-observation details must be an object")
        raw_code = payload["observed_code"]
        if raw_code is not None and type(raw_code) is not str:
            raise TypeError("fault-observation code must be a string or null")
        return cls(
            fault=FaultKind(_serialized_str(payload, "fault")),
            subsystem=_serialized_str(payload, "subsystem"),
            detector=_serialized_str(payload, "detector"),
            injection_location=_serialized_str(payload, "injection_location"),
            observed_code=raw_code,
            injection_event=_serialized_int(payload, "injection_event"),
            manifest=raw_manifest,
            raw_events=tuple(raw_events),
            raw_issues=tuple(raw_issues),
            details=raw_details,
        )


@dataclass(frozen=True, slots=True)
class GeneratedCaseResult:
    configuration: GeneratedConfiguration
    lane: ExecutorLane
    recording: CaseRecording
    event_projection: tuple[Mapping[str, object], ...]
    final_state_projection: Mapping[str, object]
    metrics: Mapping[str, object]
    exercises: tuple[ExerciseRecord, ...]
    checks: tuple[CheckResult, ...]
    failures: tuple[FailureObservation, ...]
    observable_projection: Mapping[str, object]
    fault_observation: FaultObservation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.configuration, GeneratedConfiguration):
            raise TypeError("generated case requires GeneratedConfiguration")
        if not isinstance(self.lane, ExecutorLane):
            raise TypeError("generated case lane must use ExecutorLane")
        if self.lane is not self.configuration.lane:
            raise ValueError("generated case lane differs from configuration")
        if not isinstance(self.recording, CaseRecording):
            raise TypeError("generated case requires CaseRecording")
        if self.recording.lane is not self.lane:
            raise ValueError("generated case recording lane differs from result")
        for field_name in ("event_projection", "exercises", "checks", "failures"):
            value = getattr(self, field_name)
            if not isinstance(value, tuple):
                raise TypeError(f"generated case {field_name} must be a tuple")
        frozen_events = freeze_json(self.event_projection)
        if not isinstance(frozen_events, tuple) or any(
            not isinstance(item, Mapping) for item in frozen_events
        ):
            raise TypeError("generated case event projection must contain JSON objects")
        object.__setattr__(self, "event_projection", frozen_events)
        for field_name in (
            "final_state_projection",
            "metrics",
            "observable_projection",
        ):
            object.__setattr__(
                self,
                field_name,
                _frozen_mapping(getattr(self, field_name), f"generated case {field_name}"),
            )
        if any(not isinstance(item, ExerciseRecord) for item in self.exercises):
            raise TypeError("generated case exercises must use ExerciseRecord")
        if any(item.lane is not self.lane for item in self.exercises):
            raise ValueError("generated case exercise lane differs from result")
        if any(not isinstance(item, CheckResult) for item in self.checks):
            raise TypeError("generated case checks must use CheckResult")
        if any(not isinstance(item, FailureObservation) for item in self.failures):
            raise TypeError("generated case failures must use FailureObservation")
        exercise_keys = tuple(
            (item.capability, canonical_json(thaw_json(item.configured_value)))
            for item in self.exercises
        )
        if len(exercise_keys) != len(set(exercise_keys)):
            raise ValueError("generated case exercises must be unique")
        check_names = tuple(item.name for item in self.checks)
        if len(check_names) != len(set(check_names)):
            raise ValueError("generated case check names must be unique")
        if self.fault_observation is not None:
            if not isinstance(self.fault_observation, FaultObservation):
                raise TypeError("fault observation must use FaultObservation")
            if self.configuration.injected_fault is not self.fault_observation.fault:
                raise ValueError("fault observation differs from configuration")

    @property
    def automated_status(self) -> AutomatedStatus:
        failed_check = any(item.status is CheckStatus.FAIL for item in self.checks)
        required_not_exercised = any(
            item.required and item.status is CheckStatus.NOT_EXERCISED
            for item in self.checks
        )
        return (
            AutomatedStatus.FAIL
            if self.failures or failed_check or required_not_exercised
            else AutomatedStatus.PASS
        )

    @property
    def passed(self) -> bool:
        return self.automated_status is AutomatedStatus.PASS

    @property
    def event_sha256(self) -> str:
        return canonical_sha256(thaw_json(self.event_projection))

    @property
    def state_sha256(self) -> str:
        return canonical_sha256(thaw_json(self.final_state_projection))

    def declared_outputs(self) -> dict[str, object]:
        return {
            "automated_status": self.automated_status.value,
            "check_results": [item.as_dict() for item in self.checks],
            "configuration_sha256": self.configuration.sha256,
            "event_sha256": self.event_sha256,
            "exercises": [item.as_dict() for item in self.exercises],
            "fault_observation": (
                None
                if self.fault_observation is None
                else self.fault_observation.as_dict()
            ),
            "failures": [item.as_dict() for item in self.failures],
            "lane": self.lane.value,
            "metrics": thaw_json(self.metrics),
            "observable_projection_sha256": canonical_sha256(
                thaw_json(self.observable_projection)
            ),
            "recording_sha256": self.recording.sha256,
            "state_sha256": self.state_sha256,
        }

    def replay_expectations(self) -> dict[str, object]:
        """Return outputs whose identity is independent of the wrapper digest."""

        declared_outputs = _without_recording_identity(self.declared_outputs())
        if not isinstance(declared_outputs, dict):
            raise TypeError("replay-safe declared outputs must be an object")
        digests = {
            "declared_outputs_sha256": canonical_sha256(declared_outputs),
            "event_sha256": self.event_sha256,
            "metrics_sha256": canonical_sha256(thaw_json(self.metrics)),
            "observable_sha256": canonical_sha256(
                thaw_json(self.observable_projection)
            ),
            "state_sha256": self.state_sha256,
        }
        return {
            "declared_outputs": declared_outputs,
            "digests": digests,
        }

    @property
    def result_sha256(self) -> str:
        return canonical_sha256(self.declared_outputs())

    def as_dict(self) -> dict[str, object]:
        return {
            "configuration": self.configuration.as_dict(),
            "declared_outputs": self.declared_outputs(),
            "event_projection": thaw_json(self.event_projection),
            "final_state_projection": thaw_json(self.final_state_projection),
            "observable_projection": thaw_json(self.observable_projection),
            "recording": self.recording.as_dict(),
            "result_sha256": self.result_sha256,
        }


def _without_recording_identity(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _without_recording_identity(item)
            for key, item in value.items()
            if key != "recording_sha256"
        }
    if isinstance(value, (tuple, list)):
        return [_without_recording_identity(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class MinimizationAttempt:
    sequence: int
    change: Mapping[str, object]
    accepted: bool
    reproduced: bool
    command_count_before: int
    command_count_after: int | None
    observation_sha256: str
    detail: str

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("minimization attempt sequence must be positive")
        object.__setattr__(
            self,
            "change",
            _frozen_mapping(self.change, "minimization attempt change"),
        )
        if type(self.accepted) is not bool or type(self.reproduced) is not bool:
            raise TypeError("minimization attempt outcomes must be boolean")
        if self.accepted and not self.reproduced:
            raise ValueError("accepted minimization must reproduce its predicate")
        if type(self.command_count_before) is not int or self.command_count_before < 0:
            raise ValueError("minimization command count before must be nonnegative")
        if self.command_count_after is not None and (
            type(self.command_count_after) is not int
            or self.command_count_after < 0
        ):
            raise ValueError("minimization command count after must be nonnegative")
        if not _SHA256.fullmatch(self.observation_sha256):
            raise ValueError("minimization observation digest is invalid")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("minimization attempt detail is required")

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "change": thaw_json(self.change),
            "command_count_after": self.command_count_after,
            "command_count_before": self.command_count_before,
            "detail": self.detail,
            "observation_sha256": self.observation_sha256,
            "reproduced": self.reproduced,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class MinimizedFailure:
    identity: FailureIdentity
    minimized_configuration: GeneratedConfiguration
    attempts: tuple[MinimizationAttempt, ...]
    final_recording: CaseRecording
    verification_digests: tuple[str, str]
    verification_reproduced: tuple[bool, bool]

    def __post_init__(self) -> None:
        if not isinstance(self.identity, FailureIdentity):
            raise TypeError("minimized failure requires FailureIdentity")
        if not isinstance(self.minimized_configuration, GeneratedConfiguration):
            raise TypeError("minimized failure requires GeneratedConfiguration")
        if self.minimized_configuration.lane is not self.identity.lane:
            raise ValueError("minimized failure changed executor lane")
        if any(not isinstance(item, MinimizationAttempt) for item in self.attempts):
            raise TypeError("minimized failure attempts are invalid")
        if tuple(item.sequence for item in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("minimized failure attempt sequence is incomplete")
        if not isinstance(self.final_recording, CaseRecording):
            raise TypeError("minimized failure requires a final recording")
        if self.final_recording.lane is not self.identity.lane:
            raise ValueError("minimized recording changed executor lane")
        if not self.final_recording.expected_outputs:
            raise ValueError("minimized final recording is not finalized")
        final_payload = thaw_json(self.final_recording.payload)
        if final_payload.get("configuration") != self.minimized_configuration.as_dict():
            raise ValueError("minimized recording configuration does not reconcile")
        if len(self.verification_digests) != 2:
            raise ValueError("minimized failure requires two verification digests")
        if any(not _SHA256.fullmatch(item) for item in self.verification_digests):
            raise ValueError("minimized verification digests are invalid")
        if len(self.verification_reproduced) != 2:
            raise ValueError("minimized failure requires two verification outcomes")
        if any(type(item) is not bool for item in self.verification_reproduced):
            raise TypeError("minimized verification outcomes must be boolean")

    @property
    def signature(self) -> str:
        return self.identity.signature

    @property
    def source_configuration_sha256(self) -> str:
        return self.identity.source_configuration_sha256

    @property
    def preserved(self) -> bool:
        return all(self.verification_reproduced)

    @property
    def result_digest(self) -> str:
        return self.verification_digests[-1]

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted_reductions": [
                item.as_dict() for item in self.attempts if item.accepted
            ],
            "attempt_count": len(self.attempts),
            "attempts": [item.as_dict() for item in self.attempts],
            "final_recording": self.final_recording.as_dict(),
            "identity": self.identity.as_dict(),
            "minimized_configuration": self.minimized_configuration.as_dict(),
            "preserved": self.preserved,
            "rejected_reductions": [
                item.as_dict() for item in self.attempts if not item.accepted
            ],
            "result_digest": self.result_digest,
            "signature": self.signature,
            "source_configuration_sha256": self.source_configuration_sha256,
            "verification_digests": list(self.verification_digests),
            "verification_reproduced": list(self.verification_reproduced),
        }


@dataclass(frozen=True, slots=True)
class StatisticalCheck:
    name: str
    status: str
    evidence: Mapping[str, object]
    threshold: str

    def __post_init__(self) -> None:
        frozen = freeze_json(self.evidence)
        if not isinstance(frozen, Mapping):
            raise TypeError("statistical evidence must be a JSON object")
        object.__setattr__(self, "evidence", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence": thaw_json(self.evidence),
            "name": self.name,
            "status": self.status,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceRecord:
    record_id: str
    scenario_version: int
    seed: int
    reviewer_decision: str
    observed_characteristics: tuple[str, ...]
    known_defects: tuple[str, ...]
    artifact_digests: Mapping[str, str]
    supersedes_record_id: str | None = None

    def __post_init__(self) -> None:
        frozen_digests = freeze_json(self.artifact_digests)
        if not isinstance(frozen_digests, Mapping):
            raise TypeError("acceptance artifact digests must be a JSON object")
        object.__setattr__(self, "artifact_digests", frozen_digests)
        if not _ACCEPTANCE_ID.fullmatch(self.record_id):
            raise ValueError("acceptance record ID is invalid")
        if type(self.scenario_version) is not int or self.scenario_version <= 0:
            raise ValueError("acceptance scenario version must be positive")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("acceptance seed must be nonnegative")
        if (
            not self.reviewer_decision
            or not self.reviewer_decision.replace("_", "").isalnum()
            or self.reviewer_decision != self.reviewer_decision.upper()
        ):
            raise ValueError("acceptance reviewer decision must be an uppercase identifier")
        if not self.observed_characteristics or any(
            not item for item in self.observed_characteristics
        ):
            raise ValueError("acceptance record requires observed characteristics")
        if any(not item for item in self.known_defects):
            raise ValueError("known-defect entries must be nonempty")
        if not self.artifact_digests or any(
            not name or not _SHA256.fullmatch(digest)
            for name, digest in self.artifact_digests.items()
        ):
            raise ValueError("acceptance artifact digests are invalid")
        if self.supersedes_record_id is not None:
            if (
                not _ACCEPTANCE_ID.fullmatch(self.supersedes_record_id)
                or self.supersedes_record_id == self.record_id
            ):
                raise ValueError("superseded acceptance record ID is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_digests": dict(sorted(self.artifact_digests.items())),
            "known_defects": list(self.known_defects),
            "observed_characteristics": list(self.observed_characteristics),
            "record_id": self.record_id,
            "reviewer_decision": self.reviewer_decision,
            "scenario_version": self.scenario_version,
            "seed": self.seed,
            "supersedes_record_id": self.supersedes_record_id,
        }
