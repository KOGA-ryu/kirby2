"""Bounded operational resource admission for local and authenticated-LAN work.

Resource state is deliberately outside every scientific work/result identity.  The
records here advertise finite worker capacity, admit or queue exact work requests,
record cancellation, and turn observed budget overruns into operational aborts.  No
decision can manufacture a successful scientific result.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes


ORCHESTRATION_RESOURCE_SCHEMA_VERSION = 1
RESOURCE_LIMITS_SCHEMA_ID = "KIRBY2_ORCHESTRATION_RESOURCE_LIMITS_V1"
WORKER_RESOURCE_ADVERTISEMENT_SCHEMA_ID = (
    "KIRBY2_WORKER_RESOURCE_ADVERTISEMENT_V1"
)
RESOURCE_CLAIM_SCHEMA_ID = "KIRBY2_ORCHESTRATION_RESOURCE_CLAIM_V1"
RESOURCE_CANCELLATION_SCHEMA_ID = "KIRBY2_ORCHESTRATION_CANCELLATION_V1"
RESOURCE_DECISION_SCHEMA_ID = "KIRBY2_ORCHESTRATION_RESOURCE_DECISION_V1"

MAX_RESOURCE_CLASSES_V1 = 256
MAX_CONCURRENT_RUNS_V1 = 4096
MAX_QUEUE_DEPTH_V1 = 1_000_000
MAX_MEMORY_BYTES_PER_RUN_V1 = 1 << 50
MAX_DISK_BYTES_PER_RUN_V1 = 1 << 50
MAX_ELAPSED_SECONDS_PER_RUN_V1 = 31 * 24 * 60 * 60
MAX_MESSAGE_BYTES_V1 = 64 * 1024 * 1024
MAX_STREAM_BYTES_V1 = 1024 * 1024 * 1024
MAX_RESOURCE_RECORD_BYTES_V1 = 1024 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


class ResourceAdmissionStatusV1(str, Enum):
    ADMITTED = "ADMITTED"
    QUEUED = "QUEUED"
    REFUSED = "REFUSED"
    CANCELLED = "CANCELLED"
    RELEASED = "RELEASED"
    ABORTED = "ABORTED"


class ResourceDecisionCodeV1(str, Enum):
    CAPACITY_AVAILABLE = "CAPACITY_AVAILABLE"
    QUEUE_BACKPRESSURE = "QUEUE_BACKPRESSURE"
    QUEUE_FULL = "QUEUE_FULL"
    RESOURCE_CLASS_UNAVAILABLE = "RESOURCE_CLASS_UNAVAILABLE"
    MEMORY_LIMIT_EXCEEDED = "MEMORY_LIMIT_EXCEEDED"
    DISK_LIMIT_EXCEEDED = "DISK_LIMIT_EXCEEDED"
    ELAPSED_LIMIT_EXCEEDED = "ELAPSED_LIMIT_EXCEEDED"
    EXPERIMENT_CANCELLED = "EXPERIMENT_CANCELLED"
    RUN_RELEASED = "RUN_RELEASED"


@dataclass(frozen=True, slots=True)
class ResourceLimitsV1:
    maximum_concurrent_runs: int
    maximum_queue_depth: int
    maximum_memory_bytes_per_run: int
    maximum_disk_bytes_per_run: int
    maximum_elapsed_seconds_per_run: int
    maximum_message_bytes: int = MAX_MESSAGE_BYTES_V1
    maximum_stream_bytes: int = MAX_STREAM_BYTES_V1

    schema_id: ClassVar[str] = RESOURCE_LIMITS_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_RESOURCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _bounded_integer(
            self.maximum_concurrent_runs,
            "maximum concurrent runs",
            1,
            MAX_CONCURRENT_RUNS_V1,
        )
        _bounded_integer(
            self.maximum_queue_depth,
            "maximum queue depth",
            0,
            MAX_QUEUE_DEPTH_V1,
        )
        _bounded_integer(
            self.maximum_memory_bytes_per_run,
            "maximum memory bytes per run",
            1,
            MAX_MEMORY_BYTES_PER_RUN_V1,
        )
        _bounded_integer(
            self.maximum_disk_bytes_per_run,
            "maximum disk bytes per run",
            1,
            MAX_DISK_BYTES_PER_RUN_V1,
        )
        _bounded_integer(
            self.maximum_elapsed_seconds_per_run,
            "maximum elapsed seconds per run",
            1,
            MAX_ELAPSED_SECONDS_PER_RUN_V1,
        )
        _bounded_integer(
            self.maximum_message_bytes,
            "maximum message bytes",
            1,
            MAX_MESSAGE_BYTES_V1,
        )
        _bounded_integer(
            self.maximum_stream_bytes,
            "maximum stream bytes",
            self.maximum_message_bytes,
            MAX_STREAM_BYTES_V1,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "maximum_concurrent_runs": self.maximum_concurrent_runs,
            "maximum_disk_bytes_per_run": self.maximum_disk_bytes_per_run,
            "maximum_elapsed_seconds_per_run": self.maximum_elapsed_seconds_per_run,
            "maximum_memory_bytes_per_run": self.maximum_memory_bytes_per_run,
            "maximum_message_bytes": self.maximum_message_bytes,
            "maximum_queue_depth": self.maximum_queue_depth,
            "maximum_stream_bytes": self.maximum_stream_bytes,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> ResourceLimitsV1:
        row = _exact_object(
            value,
            {
                "maximum_concurrent_runs",
                "maximum_disk_bytes_per_run",
                "maximum_elapsed_seconds_per_run",
                "maximum_memory_bytes_per_run",
                "maximum_message_bytes",
                "maximum_queue_depth",
                "maximum_stream_bytes",
                "schema_id",
                "schema_version",
            },
            "resource limits",
        )
        _require_schema(row, cls.schema_id, "resource limits")
        restored = cls(
            maximum_concurrent_runs=_integer(row, "maximum_concurrent_runs"),
            maximum_queue_depth=_integer(row, "maximum_queue_depth"),
            maximum_memory_bytes_per_run=_integer(
                row,
                "maximum_memory_bytes_per_run",
            ),
            maximum_disk_bytes_per_run=_integer(
                row,
                "maximum_disk_bytes_per_run",
            ),
            maximum_elapsed_seconds_per_run=_integer(
                row,
                "maximum_elapsed_seconds_per_run",
            ),
            maximum_message_bytes=_integer(row, "maximum_message_bytes"),
            maximum_stream_bytes=_integer(row, "maximum_stream_bytes"),
        )
        _require_round_trip(restored, row, "resource limits")
        return restored


@dataclass(frozen=True, slots=True)
class WorkerResourceAdvertisementV1:
    worker_id: str
    worker_compatibility_sha256: str
    resource_classes: tuple[str, ...]
    limits: ResourceLimitsV1
    advertisement_nonce: str

    schema_id: ClassVar[str] = WORKER_RESOURCE_ADVERTISEMENT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_RESOURCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.worker_id, "resource advertisement worker ID")
        _sha256(
            self.worker_compatibility_sha256,
            "resource advertisement compatibility digest",
        )
        _canonical_identifiers(
            self.resource_classes,
            "resource advertisement classes",
            maximum=MAX_RESOURCE_CLASSES_V1,
        )
        if type(self.limits) is not ResourceLimitsV1:
            raise TypeError("resource advertisement limits must be ResourceLimitsV1")
        _sha256(self.advertisement_nonce, "resource advertisement nonce")

    @property
    def advertisement_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.identity_dict())).hexdigest()

    def identity_dict(self) -> dict[str, object]:
        return {
            "advertisement_nonce": self.advertisement_nonce,
            "limits": self.limits.as_dict(),
            "resource_classes": list(self.resource_classes),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "worker_compatibility_sha256": self.worker_compatibility_sha256,
            "worker_id": self.worker_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_dict(),
            "advertisement_sha256": self.advertisement_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> WorkerResourceAdvertisementV1:
        row = _exact_object(
            value,
            {
                "advertisement_nonce",
                "advertisement_sha256",
                "limits",
                "resource_classes",
                "schema_id",
                "schema_version",
                "worker_compatibility_sha256",
                "worker_id",
            },
            "worker resource advertisement",
        )
        _require_schema(row, cls.schema_id, "worker resource advertisement")
        declared = _sha256(
            row["advertisement_sha256"],
            "declared resource advertisement digest",
        )
        restored = cls(
            worker_id=_text(row, "worker_id"),
            worker_compatibility_sha256=_text(
                row,
                "worker_compatibility_sha256",
            ),
            resource_classes=_text_tuple(row["resource_classes"], "resource classes"),
            limits=ResourceLimitsV1.from_dict(row["limits"]),
            advertisement_nonce=_text(row, "advertisement_nonce"),
        )
        if declared != restored.advertisement_sha256:
            raise ValueError("resource advertisement digest differs from content")
        _require_round_trip(restored, row, "worker resource advertisement")
        return restored


@dataclass(frozen=True, slots=True)
class ResourceClaimV1:
    experiment_id: str
    work_request_id: str
    resource_class: str
    memory_bytes: int
    disk_bytes: int
    elapsed_seconds: int

    schema_id: ClassVar[str] = RESOURCE_CLAIM_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_RESOURCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.experiment_id, "resource claim experiment ID")
        _sha256(self.work_request_id, "resource claim work-request ID")
        _identifier(self.resource_class, "resource claim class")
        _bounded_integer(
            self.memory_bytes,
            "resource claim memory bytes",
            1,
            MAX_MEMORY_BYTES_PER_RUN_V1,
        )
        _bounded_integer(
            self.disk_bytes,
            "resource claim disk bytes",
            1,
            MAX_DISK_BYTES_PER_RUN_V1,
        )
        _bounded_integer(
            self.elapsed_seconds,
            "resource claim elapsed seconds",
            1,
            MAX_ELAPSED_SECONDS_PER_RUN_V1,
        )

    @property
    def claim_id(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.identity_dict())).hexdigest()

    def identity_dict(self) -> dict[str, object]:
        return {
            "disk_bytes": self.disk_bytes,
            "elapsed_seconds": self.elapsed_seconds,
            "experiment_id": self.experiment_id,
            "memory_bytes": self.memory_bytes,
            "resource_class": self.resource_class,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "work_request_id": self.work_request_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "claim_id": self.claim_id}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> ResourceClaimV1:
        row = _exact_object(
            value,
            {
                "claim_id",
                "disk_bytes",
                "elapsed_seconds",
                "experiment_id",
                "memory_bytes",
                "resource_class",
                "schema_id",
                "schema_version",
                "work_request_id",
            },
            "resource claim",
        )
        _require_schema(row, cls.schema_id, "resource claim")
        declared = _sha256(row["claim_id"], "declared resource claim ID")
        restored = cls(
            experiment_id=_text(row, "experiment_id"),
            work_request_id=_text(row, "work_request_id"),
            resource_class=_text(row, "resource_class"),
            memory_bytes=_integer(row, "memory_bytes"),
            disk_bytes=_integer(row, "disk_bytes"),
            elapsed_seconds=_integer(row, "elapsed_seconds"),
        )
        if declared != restored.claim_id:
            raise ValueError("resource claim ID differs from content")
        _require_round_trip(restored, row, "resource claim")
        return restored


@dataclass(frozen=True, slots=True)
class ExperimentCancellationV1:
    experiment_id: str
    cancellation_id: str
    reason_code: str
    sequence: int

    schema_id: ClassVar[str] = RESOURCE_CANCELLATION_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_RESOURCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.experiment_id, "cancelled experiment ID")
        _sha256(self.cancellation_id, "experiment cancellation ID")
        _identifier(self.reason_code, "experiment cancellation reason")
        _bounded_integer(self.sequence, "experiment cancellation sequence", 1, 1 << 63)

    def as_dict(self) -> dict[str, object]:
        return {
            "cancellation_id": self.cancellation_id,
            "experiment_id": self.experiment_id,
            "reason_code": self.reason_code,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> ExperimentCancellationV1:
        row = _exact_object(
            value,
            {
                "cancellation_id",
                "experiment_id",
                "reason_code",
                "schema_id",
                "schema_version",
                "sequence",
            },
            "experiment cancellation",
        )
        _require_schema(row, cls.schema_id, "experiment cancellation")
        restored = cls(
            experiment_id=_text(row, "experiment_id"),
            cancellation_id=_text(row, "cancellation_id"),
            reason_code=_text(row, "reason_code"),
            sequence=_integer(row, "sequence"),
        )
        _require_round_trip(restored, row, "experiment cancellation")
        return restored


@dataclass(frozen=True, slots=True)
class ResourceAdmissionDecisionV1:
    claim_id: str
    status: ResourceAdmissionStatusV1
    code: ResourceDecisionCodeV1
    decision_sequence: int
    active_run_count: int
    queued_run_count: int
    cancellation_id: str | None = None

    schema_id: ClassVar[str] = RESOURCE_DECISION_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_RESOURCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.claim_id, "resource decision claim ID")
        if type(self.status) is not ResourceAdmissionStatusV1:
            raise TypeError("resource admission status is invalid")
        if type(self.code) is not ResourceDecisionCodeV1:
            raise TypeError("resource decision code is invalid")
        _bounded_integer(self.decision_sequence, "resource decision sequence", 1, 1 << 63)
        _bounded_integer(
            self.active_run_count,
            "resource decision active count",
            0,
            MAX_CONCURRENT_RUNS_V1,
        )
        _bounded_integer(
            self.queued_run_count,
            "resource decision queue count",
            0,
            MAX_QUEUE_DEPTH_V1,
        )
        if self.cancellation_id is not None:
            _sha256(self.cancellation_id, "resource decision cancellation ID")
        if (
            self.status is ResourceAdmissionStatusV1.CANCELLED
        ) != (self.cancellation_id is not None):
            raise ValueError("cancelled resource decision requires cancellation identity")
        allowed_codes = {
            ResourceAdmissionStatusV1.ADMITTED: {
                ResourceDecisionCodeV1.CAPACITY_AVAILABLE,
            },
            ResourceAdmissionStatusV1.QUEUED: {
                ResourceDecisionCodeV1.QUEUE_BACKPRESSURE,
            },
            ResourceAdmissionStatusV1.REFUSED: {
                ResourceDecisionCodeV1.QUEUE_FULL,
                ResourceDecisionCodeV1.RESOURCE_CLASS_UNAVAILABLE,
                ResourceDecisionCodeV1.MEMORY_LIMIT_EXCEEDED,
                ResourceDecisionCodeV1.DISK_LIMIT_EXCEEDED,
                ResourceDecisionCodeV1.ELAPSED_LIMIT_EXCEEDED,
            },
            ResourceAdmissionStatusV1.CANCELLED: {
                ResourceDecisionCodeV1.EXPERIMENT_CANCELLED,
            },
            ResourceAdmissionStatusV1.RELEASED: {
                ResourceDecisionCodeV1.RUN_RELEASED,
            },
            ResourceAdmissionStatusV1.ABORTED: {
                ResourceDecisionCodeV1.MEMORY_LIMIT_EXCEEDED,
                ResourceDecisionCodeV1.DISK_LIMIT_EXCEEDED,
                ResourceDecisionCodeV1.ELAPSED_LIMIT_EXCEEDED,
            },
        }
        if self.code not in allowed_codes[self.status]:
            raise ValueError("resource decision status and reason code disagree")

    @property
    def decision_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "active_run_count": self.active_run_count,
            "cancellation_id": self.cancellation_id,
            "claim_id": self.claim_id,
            "code": self.code.value,
            "decision_sequence": self.decision_sequence,
            "queued_run_count": self.queued_run_count,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> ResourceAdmissionDecisionV1:
        row = _exact_object(
            value,
            {
                "active_run_count",
                "cancellation_id",
                "claim_id",
                "code",
                "decision_sequence",
                "queued_run_count",
                "schema_id",
                "schema_version",
                "status",
            },
            "resource decision",
        )
        _require_schema(row, cls.schema_id, "resource decision")
        cancellation = row["cancellation_id"]
        if cancellation is not None and type(cancellation) is not str:
            raise TypeError("resource decision cancellation ID must be text or null")
        restored = cls(
            claim_id=_text(row, "claim_id"),
            status=ResourceAdmissionStatusV1(_text(row, "status")),
            code=ResourceDecisionCodeV1(_text(row, "code")),
            decision_sequence=_integer(row, "decision_sequence"),
            active_run_count=_integer(row, "active_run_count"),
            queued_run_count=_integer(row, "queued_run_count"),
            cancellation_id=cancellation,
        )
        _require_round_trip(restored, row, "resource decision")
        return restored


class ResourceControllerV1:
    """Thread-safe operational admission, backpressure, cancellation, and budgets."""

    __slots__ = (
        "_active",
        "_cancelled",
        "_classes",
        "_limits",
        "_lock",
        "_queue",
        "_sequence",
    )

    def __init__(
        self,
        *,
        limits: ResourceLimitsV1,
        resource_classes: tuple[str, ...],
    ) -> None:
        if type(limits) is not ResourceLimitsV1:
            raise TypeError("resource controller requires ResourceLimitsV1")
        _canonical_identifiers(
            resource_classes,
            "resource controller classes",
            maximum=MAX_RESOURCE_CLASSES_V1,
        )
        self._limits = limits
        self._classes = frozenset(resource_classes)
        self._active: dict[str, ResourceClaimV1] = {}
        self._queue: list[ResourceClaimV1] = []
        self._cancelled: dict[str, ExperimentCancellationV1] = {}
        self._sequence = 0
        self._lock = threading.Lock()

    @property
    def limits(self) -> ResourceLimitsV1:
        return self._limits

    def admit(self, claim: ResourceClaimV1) -> ResourceAdmissionDecisionV1:
        if type(claim) is not ResourceClaimV1:
            raise TypeError("resource admission requires ResourceClaimV1")
        with self._lock:
            if claim.claim_id in self._active or any(
                item.claim_id == claim.claim_id for item in self._queue
            ):
                raise ValueError("resource claim is already active or queued")
            cancellation = self._cancelled.get(claim.experiment_id)
            if cancellation is not None:
                return self._decision(
                    claim,
                    ResourceAdmissionStatusV1.CANCELLED,
                    ResourceDecisionCodeV1.EXPERIMENT_CANCELLED,
                    cancellation_id=cancellation.cancellation_id,
                )
            refusal = self._claim_refusal(claim)
            if refusal is not None:
                return self._decision(
                    claim,
                    ResourceAdmissionStatusV1.REFUSED,
                    refusal,
                )
            if len(self._active) < self._limits.maximum_concurrent_runs:
                self._active[claim.claim_id] = claim
                return self._decision(
                    claim,
                    ResourceAdmissionStatusV1.ADMITTED,
                    ResourceDecisionCodeV1.CAPACITY_AVAILABLE,
                )
            if len(self._queue) >= self._limits.maximum_queue_depth:
                return self._decision(
                    claim,
                    ResourceAdmissionStatusV1.REFUSED,
                    ResourceDecisionCodeV1.QUEUE_FULL,
                )
            self._queue.append(claim)
            return self._decision(
                claim,
                ResourceAdmissionStatusV1.QUEUED,
                ResourceDecisionCodeV1.QUEUE_BACKPRESSURE,
            )

    def release(self, claim_id: str) -> tuple[ResourceAdmissionDecisionV1, ...]:
        digest = _sha256(claim_id, "released resource claim ID")
        with self._lock:
            claim = self._active.pop(digest, None)
            if claim is None:
                raise KeyError("resource claim is not active")
            decisions = [
                self._decision(
                    claim,
                    ResourceAdmissionStatusV1.RELEASED,
                    ResourceDecisionCodeV1.RUN_RELEASED,
                )
            ]
            decisions.extend(self._promote_waiting())
            return tuple(decisions)

    def observe_usage(
        self,
        claim_id: str,
        *,
        memory_bytes: int,
        disk_bytes: int,
        elapsed_seconds: int,
    ) -> tuple[ResourceAdmissionDecisionV1, ...]:
        digest = _sha256(claim_id, "observed resource claim ID")
        for value, label in (
            (memory_bytes, "observed memory bytes"),
            (disk_bytes, "observed disk bytes"),
            (elapsed_seconds, "observed elapsed seconds"),
        ):
            _bounded_integer(value, label, 0, 1 << 63)
        with self._lock:
            claim = self._active.get(digest)
            if claim is None:
                raise KeyError("resource claim is not active")
            code = None
            if memory_bytes > claim.memory_bytes:
                code = ResourceDecisionCodeV1.MEMORY_LIMIT_EXCEEDED
            elif disk_bytes > claim.disk_bytes:
                code = ResourceDecisionCodeV1.DISK_LIMIT_EXCEEDED
            elif elapsed_seconds > claim.elapsed_seconds:
                code = ResourceDecisionCodeV1.ELAPSED_LIMIT_EXCEEDED
            if code is None:
                return ()
            self._active.pop(digest)
            decisions = [
                self._decision(
                    claim,
                    ResourceAdmissionStatusV1.ABORTED,
                    code,
                )
            ]
            decisions.extend(self._promote_waiting())
            return tuple(decisions)

    def cancel_experiment(
        self,
        cancellation: ExperimentCancellationV1,
    ) -> tuple[ResourceAdmissionDecisionV1, ...]:
        if type(cancellation) is not ExperimentCancellationV1:
            raise TypeError("resource cancellation requires ExperimentCancellationV1")
        with self._lock:
            prior = self._cancelled.get(cancellation.experiment_id)
            if prior is not None:
                if prior == cancellation:
                    return ()
                raise ValueError("experiment already has a different cancellation")
            self._cancelled[cancellation.experiment_id] = cancellation
            affected = [
                item
                for item in self._active.values()
                if item.experiment_id == cancellation.experiment_id
            ]
            affected.extend(
                item
                for item in self._queue
                if item.experiment_id == cancellation.experiment_id
            )
            for item in affected:
                self._active.pop(item.claim_id, None)
            affected_ids = {item.claim_id for item in affected}
            self._queue = [
                item for item in self._queue if item.claim_id not in affected_ids
            ]
            decisions = [
                self._decision(
                    item,
                    ResourceAdmissionStatusV1.CANCELLED,
                    ResourceDecisionCodeV1.EXPERIMENT_CANCELLED,
                    cancellation_id=cancellation.cancellation_id,
                )
                for item in sorted(affected, key=lambda candidate: candidate.claim_id)
            ]
            decisions.extend(self._promote_waiting())
            return tuple(decisions)

    def _claim_refusal(self, claim: ResourceClaimV1) -> ResourceDecisionCodeV1 | None:
        if claim.resource_class not in self._classes:
            return ResourceDecisionCodeV1.RESOURCE_CLASS_UNAVAILABLE
        if claim.memory_bytes > self._limits.maximum_memory_bytes_per_run:
            return ResourceDecisionCodeV1.MEMORY_LIMIT_EXCEEDED
        if claim.disk_bytes > self._limits.maximum_disk_bytes_per_run:
            return ResourceDecisionCodeV1.DISK_LIMIT_EXCEEDED
        if claim.elapsed_seconds > self._limits.maximum_elapsed_seconds_per_run:
            return ResourceDecisionCodeV1.ELAPSED_LIMIT_EXCEEDED
        return None

    def _decision(
        self,
        claim: ResourceClaimV1,
        status: ResourceAdmissionStatusV1,
        code: ResourceDecisionCodeV1,
        *,
        cancellation_id: str | None = None,
    ) -> ResourceAdmissionDecisionV1:
        self._sequence += 1
        return ResourceAdmissionDecisionV1(
            claim_id=claim.claim_id,
            status=status,
            code=code,
            decision_sequence=self._sequence,
            active_run_count=len(self._active),
            queued_run_count=len(self._queue),
            cancellation_id=cancellation_id,
        )

    def _promote_waiting(self) -> list[ResourceAdmissionDecisionV1]:
        decisions: list[ResourceAdmissionDecisionV1] = []
        while self._queue and len(self._active) < self._limits.maximum_concurrent_runs:
            promoted = self._queue.pop(0)
            cancellation = self._cancelled.get(promoted.experiment_id)
            if cancellation is not None:
                decisions.append(
                    self._decision(
                        promoted,
                        ResourceAdmissionStatusV1.CANCELLED,
                        ResourceDecisionCodeV1.EXPERIMENT_CANCELLED,
                        cancellation_id=cancellation.cancellation_id,
                    )
                )
                continue
            self._active[promoted.claim_id] = promoted
            decisions.append(
                self._decision(
                    promoted,
                    ResourceAdmissionStatusV1.ADMITTED,
                    ResourceDecisionCodeV1.CAPACITY_AVAILABLE,
                )
            )
        return decisions


def record_from_canonical_bytes(record_type, raw: bytes):
    """Decode one bounded resource record through its exact ``from_dict`` boundary."""

    if record_type not in {
        ResourceLimitsV1,
        WorkerResourceAdvertisementV1,
        ResourceClaimV1,
        ExperimentCancellationV1,
        ResourceAdmissionDecisionV1,
    }:
        raise TypeError("unsupported resource record type")
    if type(raw) is not bytes or not raw or len(raw) > MAX_RESOURCE_RECORD_BYTES_V1:
        raise ValueError("resource record bytes are empty or exceed the message limit")
    value = load_canonical_json_bytes(raw, "orchestration resource record")
    restored = record_type.from_dict(value)
    if restored.canonical_bytes() != raw:
        raise ValueError("resource record bytes are not canonical")
    return restored


def _bounded_integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer in [{minimum}, {maximum}]")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be one canonical identifier")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _canonical_identifiers(values: object, label: str, *, maximum: int) -> None:
    if type(values) is not tuple or not values or len(values) > maximum:
        raise ValueError(f"{label} must be a nonempty bounded tuple")
    for value in values:
        _identifier(value, label)
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be sorted and unique")


def _exact_object(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise TypeError(f"serialized {label} must be one exact object")
    actual = set(value)
    if actual != fields:
        raise ValueError(
            f"serialized {label} fields differ: "
            f"missing={sorted(fields - actual)} unknown={sorted(actual - fields)}"
        )
    return value


def _require_schema(row: dict[str, object], schema_id: str, label: str) -> None:
    if (
        row["schema_id"] != schema_id
        or type(row["schema_version"]) is not int
        or row["schema_version"] != ORCHESTRATION_RESOURCE_SCHEMA_VERSION
    ):
        raise ValueError(f"serialized {label} schema differs from V1")


def _text(row: dict[str, object], key: str) -> str:
    value = row[key]
    if type(value) is not str:
        raise TypeError(f"serialized {key} must be exact text")
    return value


def _integer(row: dict[str, object], key: str) -> int:
    value = row[key]
    if type(value) is not int:
        raise TypeError(f"serialized {key} must be an exact integer")
    return value


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise TypeError(f"serialized {label} must be an array of text")
    return tuple(value)


def _require_round_trip(record: object, row: dict[str, object], label: str) -> None:
    as_dict = getattr(record, "as_dict", None)
    if not callable(as_dict) or as_dict() != row:
        raise ValueError(f"serialized {label} did not round-trip exactly")


__all__ = [
    "MAX_CONCURRENT_RUNS_V1",
    "MAX_DISK_BYTES_PER_RUN_V1",
    "MAX_ELAPSED_SECONDS_PER_RUN_V1",
    "MAX_MEMORY_BYTES_PER_RUN_V1",
    "MAX_MESSAGE_BYTES_V1",
    "MAX_QUEUE_DEPTH_V1",
    "MAX_RESOURCE_RECORD_BYTES_V1",
    "MAX_RESOURCE_CLASSES_V1",
    "MAX_STREAM_BYTES_V1",
    "ORCHESTRATION_RESOURCE_SCHEMA_VERSION",
    "RESOURCE_CANCELLATION_SCHEMA_ID",
    "RESOURCE_CLAIM_SCHEMA_ID",
    "RESOURCE_DECISION_SCHEMA_ID",
    "RESOURCE_LIMITS_SCHEMA_ID",
    "WORKER_RESOURCE_ADVERTISEMENT_SCHEMA_ID",
    "ExperimentCancellationV1",
    "ResourceAdmissionDecisionV1",
    "ResourceAdmissionStatusV1",
    "ResourceClaimV1",
    "ResourceControllerV1",
    "ResourceDecisionCodeV1",
    "ResourceLimitsV1",
    "WorkerResourceAdvertisementV1",
    "record_from_canonical_bytes",
]
