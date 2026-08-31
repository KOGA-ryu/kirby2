"""Durable orchestration recovery, idempotence, and attempt-state reduction.

Scientific work and result identities come from the WO38-A/B contracts.  This
module adds only operational state: attempt numbers, restart observations, command
events, and immutable result references.  A retry can therefore change how work is
performed without changing what work means.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import ClassVar

from kirby2.immutable import freeze_json, thaw_json
from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes
from kirby2.research.paths import DataAreaId, DataPaths

from .aggregation import ExperimentAggregateV1, aggregate_registered_results
from .content_store import (
    ContentStoreRefusalCodeV1,
    ContentStoreRefused,
    OrchestrationContentStoreV1,
    RegisteredResultBundleV1,
    ResultAttemptStageV1,
)
from .coordinator import (
    OrchestrationCoordinatorV1,
    VerifiedWorkResultV1,
    build_verified_result_manifest,
)
from .leases import derive_attempt_id
from .local import ExecutionBackendV1
from .models import ExperimentWorkPlanV1
from .protocol import WorkRequestV1
from .worker import complete_run_runtime_audit_identities


ORCHESTRATION_RECOVERY_SCHEMA_VERSION = 1
RECOVERY_OPERATIONAL_EVENT_SCHEMA_ID = "KIRBY2_RECOVERY_OPERATIONAL_EVENT_V1"
RECOVERY_WORK_RECORD_SCHEMA_ID = "KIRBY2_RECOVERY_WORK_RECORD_V1"
RECOVERY_CHECKPOINT_SCHEMA_ID = "KIRBY2_RECOVERY_CHECKPOINT_V1"

MAX_RECOVERY_CHECKPOINT_BYTES_V1 = 64 * 1024 * 1024
MAX_RECOVERY_RECORDS_V1 = 1_000_000
MAX_RECOVERY_EVENTS_V1 = 4_000_000
MAX_RECOVERY_EVENT_DETAILS_BYTES_V1 = 16 * 1024
MAX_ATTEMPT_NUMBER_V1 = (1 << 31) - 1

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z")
_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_BACKEND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RECOVERY_DIRECTORY = "orchestration-recovery-v1"
_LOCK_FILE = ".recovery.lock"
_TEMP_PREFIX = ".recovery-tmp-"


class RecoveryRefused(RuntimeError):
    """Stable recovery refusal whose code can be handled without message parsing."""

    def __init__(self, code: str, detail: str) -> None:
        if type(code) is not str or _REASON_CODE.fullmatch(code) is None:
            raise ValueError("recovery refusal code must be canonical uppercase text")
        if type(detail) is not str or not detail or len(detail.encode("utf-8")) > 4096:
            raise ValueError("recovery refusal detail must be bounded nonempty text")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class RecoveryExperimentStatusV1(str, Enum):
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    QUARANTINED = "QUARANTINED"


class RecoveryWorkStateV1(str, Enum):
    QUEUED = "QUEUED"
    IN_FLIGHT = "IN_FLIGHT"
    PENDING_REGISTRATION = "PENDING_REGISTRATION"
    REGISTERED = "REGISTERED"
    CANCELLED = "CANCELLED"
    QUARANTINED = "QUARANTINED"


class RecoveryEventKindV1(str, Enum):
    SUBMITTED = "SUBMITTED"
    RESUMED = "RESUMED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    ATTEMPT_REISSUED = "ATTEMPT_REISSUED"
    ATTEMPT_STARTED = "ATTEMPT_STARTED"
    ATTEMPT_FAILED = "ATTEMPT_FAILED"
    RESULT_ACCEPTED = "RESULT_ACCEPTED"
    RESULT_REGISTERED = "RESULT_REGISTERED"
    LATE_RESULT_IDEMPOTENT = "LATE_RESULT_IDEMPOTENT"
    DETERMINISM_FAILURE = "DETERMINISM_FAILURE"
    STAGING_DISCARDED = "STAGING_DISCARDED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class RecoveryCompletionOrderV1(str, Enum):
    """Operational acceptance order used to demonstrate order independence."""

    CANONICAL = "CANONICAL"
    REVERSE = "REVERSE"


@dataclass(frozen=True, slots=True)
class RecoveryOperationalEventV1:
    sequence: int
    kind: RecoveryEventKindV1
    recorded_at_utc: str
    event_nonce: str
    logical_work_unit_id: str | None = None
    attempt_number: int | None = None
    attempt_id: str | None = None
    details: Mapping[str, object] | None = None

    schema_id: ClassVar[str] = RECOVERY_OPERATIONAL_EVENT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_RECOVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _bounded_integer(self.sequence, "recovery event sequence", 1, MAX_RECOVERY_EVENTS_V1)
        if type(self.kind) is not RecoveryEventKindV1:
            raise TypeError("recovery event kind is invalid")
        _utc(self.recorded_at_utc, "recovery event time")
        _sha256(self.event_nonce, "recovery event nonce")
        if self.logical_work_unit_id is not None:
            _sha256(self.logical_work_unit_id, "recovery event logical-work ID")
        if self.attempt_number is not None:
            _bounded_integer(
                self.attempt_number,
                "recovery event attempt number",
                1,
                MAX_ATTEMPT_NUMBER_V1,
            )
        if self.attempt_id is not None:
            _sha256(self.attempt_id, "recovery event attempt ID")
        if (self.attempt_number is None) != (self.attempt_id is None):
            raise ValueError("recovery event attempt number and ID must appear together")
        if self.attempt_number is not None:
            if self.logical_work_unit_id is None:
                raise ValueError("attempt event requires a logical-work identity")
            expected = derive_attempt_id(self.logical_work_unit_id, self.attempt_number)
            if not hmac.compare_digest(expected, self.attempt_id or ""):
                raise ValueError("recovery event attempt ID differs from its number")
        raw_details: Mapping[str, object] = {} if self.details is None else self.details
        if not isinstance(raw_details, Mapping):
            raise TypeError("recovery event details must be an object")
        frozen = freeze_json(dict(raw_details))
        if not isinstance(frozen, Mapping):
            raise TypeError("recovery event details did not freeze as an object")
        if len(canonical_json_bytes(thaw_json(frozen))) > MAX_RECOVERY_EVENT_DETAILS_BYTES_V1:
            raise ValueError("recovery event details exceed their byte limit")
        object.__setattr__(self, "details", frozen)

    @property
    def event_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.identity_dict())).hexdigest()

    def identity_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "details": thaw_json(self.details),
            "event_nonce": self.event_nonce,
            "kind": self.kind.value,
            "logical_work_unit_id": self.logical_work_unit_id,
            "recorded_at_utc": self.recorded_at_utc,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "event_sha256": self.event_sha256}

    @classmethod
    def from_dict(cls, value: object) -> RecoveryOperationalEventV1:
        row = _exact(
            value,
            {
                "attempt_id",
                "attempt_number",
                "details",
                "event_nonce",
                "event_sha256",
                "kind",
                "logical_work_unit_id",
                "recorded_at_utc",
                "schema_id",
                "schema_version",
                "sequence",
            },
            "recovery operational event",
        )
        _schema(row, cls.schema_id, "recovery operational event")
        declared = _sha256(row["event_sha256"], "declared recovery event digest")
        details = row["details"]
        if type(details) is not dict:
            raise TypeError("recovery event details must be an exact object")
        restored = cls(
            sequence=_integer(row, "sequence"),
            kind=RecoveryEventKindV1(_text(row, "kind")),
            recorded_at_utc=_text(row, "recorded_at_utc"),
            event_nonce=_text(row, "event_nonce"),
            logical_work_unit_id=_optional_text(row, "logical_work_unit_id"),
            attempt_number=_optional_integer(row, "attempt_number"),
            attempt_id=_optional_text(row, "attempt_id"),
            details=details,
        )
        if not hmac.compare_digest(declared, restored.event_sha256):
            raise ValueError("recovery event digest differs from canonical content")
        _round_trip(restored, row, "recovery operational event")
        return restored


@dataclass(frozen=True, slots=True)
class RecoveryWorkRecordV1:
    """Latest operational reduction for one immutable logical work unit."""

    work_request_id: str
    logical_work_unit_id: str
    state: RecoveryWorkStateV1
    attempt_number: int = 0
    last_attempt_id: str | None = None
    selected_result_sha256: str | None = None
    selected_manifest_sha256: str | None = None
    successful_result_sha256s: tuple[str, ...] = ()
    registered_manifest_sha256s: tuple[str, ...] = ()
    last_failure_code: str | None = None

    schema_id: ClassVar[str] = RECOVERY_WORK_RECORD_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_RECOVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.work_request_id, "recovery work-request ID")
        _sha256(self.logical_work_unit_id, "recovery logical-work ID")
        if type(self.state) is not RecoveryWorkStateV1:
            raise TypeError("recovery work state is invalid")
        _bounded_integer(
            self.attempt_number,
            "recovery attempt number",
            0,
            MAX_ATTEMPT_NUMBER_V1,
        )
        if self.last_attempt_id is not None:
            _sha256(self.last_attempt_id, "recovery last-attempt ID")
        if (self.attempt_number == 0) != (self.last_attempt_id is None):
            raise ValueError("recovery attempt number and last-attempt ID disagree")
        if self.last_attempt_id is not None:
            expected = derive_attempt_id(self.logical_work_unit_id, self.attempt_number)
            if not hmac.compare_digest(expected, self.last_attempt_id):
                raise ValueError("recovery last-attempt ID differs from its number")
        for value, label in (
            (self.selected_result_sha256, "selected recovery result"),
            (self.selected_manifest_sha256, "selected recovery manifest"),
        ):
            if value is not None:
                _sha256(value, label)
        _canonical_digests(
            self.successful_result_sha256s,
            "successful recovery results",
        )
        _canonical_digests(
            self.registered_manifest_sha256s,
            "registered recovery manifests",
        )
        if self.last_failure_code is not None:
            _reason(self.last_failure_code, "recovery failure code")
        self._validate_state_shape()

    def _validate_state_shape(self) -> None:
        selected = (
            self.selected_result_sha256 is not None
            and self.selected_manifest_sha256 is not None
        )
        if (self.selected_result_sha256 is None) != (self.selected_manifest_sha256 is None):
            raise ValueError("selected result and manifest must appear together")
        if self.state is RecoveryWorkStateV1.QUEUED:
            if selected or self.successful_result_sha256s or self.registered_manifest_sha256s:
                raise ValueError("queued work cannot carry successful result state")
            return
        if self.state is RecoveryWorkStateV1.IN_FLIGHT:
            if self.attempt_number == 0 or self.last_failure_code is not None:
                raise ValueError("in-flight work has an invalid operational shape")
            has_pending_candidate = (
                selected
                and self.successful_result_sha256s
                == (self.selected_result_sha256,)
                and not self.registered_manifest_sha256s
            )
            if not has_pending_candidate and (
                selected
                or self.successful_result_sha256s
                or self.registered_manifest_sha256s
            ):
                raise ValueError("in-flight work has an invalid pending-result shape")
            return
        if self.state is RecoveryWorkStateV1.PENDING_REGISTRATION:
            if (
                self.attempt_number == 0
                or not selected
                or self.successful_result_sha256s
                != (self.selected_result_sha256,)
                or self.registered_manifest_sha256s
            ):
                raise ValueError("pending registration has an invalid write-ahead shape")
            return
        if self.state is RecoveryWorkStateV1.REGISTERED:
            if (
                self.attempt_number == 0
                or not selected
                or self.last_failure_code is not None
                or self.successful_result_sha256s != (self.selected_result_sha256,)
                or self.registered_manifest_sha256s != (self.selected_manifest_sha256,)
            ):
                raise ValueError("registered work has an invalid selected-result shape")
            return
        if self.state is RecoveryWorkStateV1.CANCELLED:
            if (
                selected
                or self.successful_result_sha256s
                or self.registered_manifest_sha256s
                or self.last_failure_code != "OPERATOR_CANCELLED"
            ):
                raise ValueError("cancelled work has an invalid operational shape")
            return
        if self.state is RecoveryWorkStateV1.QUARANTINED:
            if (
                selected
                or len(self.successful_result_sha256s) < 2
                or self.last_failure_code != "DETERMINISM_FAILURE"
            ):
                raise ValueError("quarantined work has an invalid conflict shape")
            return
        raise RuntimeError("recovery work state is not exhaustively handled")

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_number": self.attempt_number,
            "last_attempt_id": self.last_attempt_id,
            "last_failure_code": self.last_failure_code,
            "logical_work_unit_id": self.logical_work_unit_id,
            "registered_manifest_sha256s": list(self.registered_manifest_sha256s),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "selected_manifest_sha256": self.selected_manifest_sha256,
            "selected_result_sha256": self.selected_result_sha256,
            "state": self.state.value,
            "successful_result_sha256s": list(self.successful_result_sha256s),
            "work_request_id": self.work_request_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> RecoveryWorkRecordV1:
        row = _exact(
            value,
            {
                "attempt_number",
                "last_attempt_id",
                "last_failure_code",
                "logical_work_unit_id",
                "registered_manifest_sha256s",
                "schema_id",
                "schema_version",
                "selected_manifest_sha256",
                "selected_result_sha256",
                "state",
                "successful_result_sha256s",
                "work_request_id",
            },
            "recovery work record",
        )
        _schema(row, cls.schema_id, "recovery work record")
        restored = cls(
            work_request_id=_text(row, "work_request_id"),
            logical_work_unit_id=_text(row, "logical_work_unit_id"),
            state=RecoveryWorkStateV1(_text(row, "state")),
            attempt_number=_integer(row, "attempt_number"),
            last_attempt_id=_optional_text(row, "last_attempt_id"),
            selected_result_sha256=_optional_text(row, "selected_result_sha256"),
            selected_manifest_sha256=_optional_text(row, "selected_manifest_sha256"),
            successful_result_sha256s=_digest_array(
                row["successful_result_sha256s"],
                "successful recovery results",
            ),
            registered_manifest_sha256s=_digest_array(
                row["registered_manifest_sha256s"],
                "registered recovery manifests",
            ),
            last_failure_code=_optional_text(row, "last_failure_code"),
        )
        _round_trip(restored, row, "recovery work record")
        return restored


@dataclass(frozen=True, slots=True)
class RecoveryCheckpointV1:
    """Atomic current recovery state with a hash link to its predecessor."""

    plan: ExperimentWorkPlanV1
    revision: int
    previous_checkpoint_sha256: str | None
    status: RecoveryExperimentStatusV1
    records: tuple[RecoveryWorkRecordV1, ...]
    events: tuple[RecoveryOperationalEventV1, ...]
    aggregate: ExperimentAggregateV1 | None = None
    cancellation_reason_code: str | None = None

    schema_id: ClassVar[str] = RECOVERY_CHECKPOINT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_RECOVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.plan) is not ExperimentWorkPlanV1:
            raise TypeError("recovery checkpoint requires ExperimentWorkPlanV1")
        _bounded_integer(self.revision, "recovery checkpoint revision", 1, (1 << 63) - 1)
        if self.previous_checkpoint_sha256 is not None:
            _sha256(
                self.previous_checkpoint_sha256,
                "previous recovery checkpoint digest",
            )
        if (self.revision == 1) != (self.previous_checkpoint_sha256 is None):
            raise ValueError("recovery checkpoint revision and predecessor disagree")
        if type(self.status) is not RecoveryExperimentStatusV1:
            raise TypeError("recovery experiment status is invalid")
        if (
            type(self.records) is not tuple
            or not self.records
            or len(self.records) > MAX_RECOVERY_RECORDS_V1
            or any(type(item) is not RecoveryWorkRecordV1 for item in self.records)
        ):
            raise ValueError("recovery checkpoint requires bounded typed records")
        logical_ids = tuple(item.logical_work_unit_id for item in self.records)
        expected_ids = tuple(item.logical_work_unit_id for item in self.plan.logical_units)
        if logical_ids != expected_ids:
            raise ValueError("recovery records do not exactly cover the canonical plan")
        required_audits = complete_run_runtime_audit_identities()
        expected_requests = tuple(
            WorkRequestV1(
                logical_work_unit=unit,
                required_runtime_audits=required_audits,
            ).work_request_id
            for unit in self.plan.logical_units
        )
        if tuple(item.work_request_id for item in self.records) != expected_requests:
            raise ValueError("recovery records differ from canonical work requests")
        if (
            type(self.events) is not tuple
            or not self.events
            or len(self.events) > MAX_RECOVERY_EVENTS_V1
            or any(type(item) is not RecoveryOperationalEventV1 for item in self.events)
        ):
            raise ValueError("recovery checkpoint requires bounded typed events")
        if tuple(item.sequence for item in self.events) != tuple(range(1, len(self.events) + 1)):
            raise ValueError("recovery events must use contiguous canonical sequence")
        if self.events[0].kind is not RecoveryEventKindV1.SUBMITTED:
            raise ValueError("recovery history must begin with submission")
        if self.aggregate is not None:
            if type(self.aggregate) is not ExperimentAggregateV1:
                raise TypeError("recovery aggregate is invalid")
            if self.aggregate.plan_id != self.plan.plan_id:
                raise ValueError("recovery aggregate belongs to another plan")
        if self.cancellation_reason_code is not None:
            _reason(self.cancellation_reason_code, "recovery cancellation reason")
        self._validate_status_shape()

    def _validate_status_shape(self) -> None:
        states = tuple(item.state for item in self.records)
        if self.status is RecoveryExperimentStatusV1.SUBMITTED:
            if any(item is not RecoveryWorkStateV1.QUEUED for item in states):
                raise ValueError("submitted recovery checkpoint must be entirely queued")
            if self.aggregate is not None or self.cancellation_reason_code is not None:
                raise ValueError("submitted checkpoint cannot be terminal")
            return
        if self.status is RecoveryExperimentStatusV1.RUNNING:
            if any(item in {RecoveryWorkStateV1.CANCELLED, RecoveryWorkStateV1.QUARANTINED} for item in states):
                raise ValueError("running checkpoint cannot contain terminal refusal state")
            if self.aggregate is not None or self.cancellation_reason_code is not None:
                raise ValueError("running checkpoint cannot carry terminal metadata")
            return
        if self.status is RecoveryExperimentStatusV1.COMPLETED:
            if any(item is not RecoveryWorkStateV1.REGISTERED for item in states):
                raise ValueError("completed checkpoint requires every result registered")
            if self.aggregate is None or self.cancellation_reason_code is not None:
                raise ValueError("completed checkpoint requires exactly one aggregate")
            manifests = tuple(item.selected_manifest_sha256 for item in self.records)
            aggregate_manifests = tuple(item.manifest_sha256 for item in self.aggregate.members)
            if manifests != aggregate_manifests:
                raise ValueError("completed aggregate differs from selected manifests")
            return
        if self.status is RecoveryExperimentStatusV1.CANCELLED:
            if not any(item is RecoveryWorkStateV1.CANCELLED for item in states):
                raise ValueError("cancelled checkpoint contains no cancelled work")
            if any(
                item
                in {
                    RecoveryWorkStateV1.QUEUED,
                    RecoveryWorkStateV1.IN_FLIGHT,
                    RecoveryWorkStateV1.PENDING_REGISTRATION,
                    RecoveryWorkStateV1.QUARANTINED,
                }
                for item in states
            ):
                raise ValueError("cancelled checkpoint retains active or quarantined work")
            if self.aggregate is not None or self.cancellation_reason_code is None:
                raise ValueError("cancelled checkpoint terminal metadata is incomplete")
            return
        if self.status is RecoveryExperimentStatusV1.QUARANTINED:
            if not any(item is RecoveryWorkStateV1.QUARANTINED for item in states):
                raise ValueError("quarantined checkpoint contains no conflict")
            if self.aggregate is not None or self.cancellation_reason_code is not None:
                raise ValueError("quarantined checkpoint cannot select an aggregate")
            return
        raise RuntimeError("recovery experiment status is not exhaustively handled")

    @property
    def plan_id(self) -> str:
        return self.plan.plan_id

    def identity_dict(self) -> dict[str, object]:
        return {
            "aggregate": None if self.aggregate is None else self.aggregate.as_dict(),
            "cancellation_reason_code": self.cancellation_reason_code,
            "events": [item.as_dict() for item in self.events],
            "plan": self.plan.as_dict(),
            "previous_checkpoint_sha256": self.previous_checkpoint_sha256,
            "records": [item.as_dict() for item in self.records],
            "revision": self.revision,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
        }

    @property
    def checkpoint_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.identity_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checkpoint_sha256": self.checkpoint_sha256}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> RecoveryCheckpointV1:
        row = _exact(
            value,
            {
                "aggregate",
                "cancellation_reason_code",
                "checkpoint_sha256",
                "events",
                "plan",
                "previous_checkpoint_sha256",
                "records",
                "revision",
                "schema_id",
                "schema_version",
                "status",
            },
            "recovery checkpoint",
        )
        _schema(row, cls.schema_id, "recovery checkpoint")
        declared = _sha256(row["checkpoint_sha256"], "declared recovery checkpoint digest")
        raw_records = row["records"]
        raw_events = row["events"]
        if type(raw_records) is not list or type(raw_events) is not list:
            raise TypeError("recovery records and events must be arrays")
        raw_aggregate = row["aggregate"]
        restored = cls(
            plan=ExperimentWorkPlanV1.from_dict(row["plan"]),
            revision=_integer(row, "revision"),
            previous_checkpoint_sha256=_optional_text(
                row,
                "previous_checkpoint_sha256",
            ),
            status=RecoveryExperimentStatusV1(_text(row, "status")),
            records=tuple(RecoveryWorkRecordV1.from_dict(item) for item in raw_records),
            events=tuple(RecoveryOperationalEventV1.from_dict(item) for item in raw_events),
            aggregate=(
                None
                if raw_aggregate is None
                else ExperimentAggregateV1.from_dict(raw_aggregate)
            ),
            cancellation_reason_code=_optional_text(row, "cancellation_reason_code"),
        )
        if not hmac.compare_digest(declared, restored.checkpoint_sha256):
            raise ValueError("recovery checkpoint digest differs from canonical content")
        _round_trip(restored, row, "recovery checkpoint")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> RecoveryCheckpointV1:
        if type(raw) is not bytes or not raw or len(raw) > MAX_RECOVERY_CHECKPOINT_BYTES_V1:
            raise ValueError("recovery checkpoint bytes exceed their V1 limit")
        restored = cls.from_dict(load_canonical_json_bytes(raw, "recovery checkpoint"))
        if restored.canonical_bytes() != raw:
            raise ValueError("recovery checkpoint bytes are not canonical")
        return restored


class RecoveryCheckpointStoreV1:
    """Descriptor-relative atomic storage for one current checkpoint per plan."""

    __slots__ = ("_paths",)

    def __init__(self, paths: DataPaths) -> None:
        if type(paths) is not DataPaths:
            raise TypeError("recovery checkpoint store requires DataPaths")
        paths.validate()
        self._paths = paths

    def load(self, plan_id: str) -> RecoveryCheckpointV1 | None:
        digest = _sha256(plan_id, "recovery checkpoint plan ID")
        self._paths.validate(DataAreaId.CHECKPOINTS)
        try:
            root_fd = _open_directory(self._paths.checkpoints)
        except FileNotFoundError:
            return None
        try:
            try:
                state_fd = _open_directory_at(root_fd, _RECOVERY_DIRECTORY)
            except FileNotFoundError:
                return None
            try:
                restored = self._load_at(state_fd, digest)
            finally:
                os.close(state_fd)
        finally:
            os.close(root_fd)
        return restored

    def save(self, checkpoint: RecoveryCheckpointV1) -> str:
        if type(checkpoint) is not RecoveryCheckpointV1:
            raise TypeError("recovery checkpoint save requires typed state")
        raw = checkpoint.canonical_bytes()
        if len(raw) > MAX_RECOVERY_CHECKPOINT_BYTES_V1:
            raise ValueError("recovery checkpoint exceeds its V1 byte limit")
        self._paths.ensure(DataAreaId.CHECKPOINTS)
        self._paths.validate(DataAreaId.CHECKPOINTS)
        root_fd = _open_directory(self._paths.checkpoints)
        try:
            state_fd = _ensure_directory_at(root_fd, _RECOVERY_DIRECTORY)
            try:
                with _RecoveryLock(state_fd):
                    prior = self._load_at(state_fd, checkpoint.plan_id)
                    if prior is None:
                        if checkpoint.revision != 1:
                            raise ValueError("first recovery checkpoint must be revision one")
                    elif (
                        checkpoint.revision != prior.revision + 1
                        or checkpoint.previous_checkpoint_sha256 != prior.checkpoint_sha256
                    ):
                        raise ValueError("recovery checkpoint does not extend stored state")
                    temporary = _write_temporary(state_fd, raw)
                    try:
                        os.replace(
                            temporary,
                            f"{checkpoint.plan_id}.json",
                            src_dir_fd=state_fd,
                            dst_dir_fd=state_fd,
                        )
                        temporary = ""
                        os.fsync(state_fd)
                    finally:
                        if temporary:
                            try:
                                os.unlink(temporary, dir_fd=state_fd)
                            except FileNotFoundError:
                                pass
            finally:
                os.close(state_fd)
        finally:
            os.close(root_fd)
        return checkpoint.checkpoint_sha256

    @staticmethod
    def _load_at(state_fd: int, plan_id: str) -> RecoveryCheckpointV1 | None:
        try:
            descriptor = _open_regular_at(state_fd, f"{plan_id}.json")
        except FileNotFoundError:
            return None
        try:
            raw = _read_bounded(descriptor, MAX_RECOVERY_CHECKPOINT_BYTES_V1)
        finally:
            os.close(descriptor)
        checkpoint = RecoveryCheckpointV1.from_canonical_bytes(raw)
        if checkpoint.plan_id != plan_id:
            raise ValueError("recovery checkpoint file belongs to another plan")
        return checkpoint


class RecoveryCoordinatorV1:
    """Submit, restart, retry, register, quarantine, and aggregate experiments."""

    __slots__ = ("_content", "_coordinator", "_store")

    def __init__(self, paths: DataPaths) -> None:
        if type(paths) is not DataPaths:
            raise TypeError("recovery coordinator requires DataPaths")
        self._store = RecoveryCheckpointStoreV1(paths)
        self._content = OrchestrationContentStoreV1(paths=paths)
        self._coordinator = OrchestrationCoordinatorV1()

    def submit(
        self,
        plan: ExperimentWorkPlanV1,
        *,
        recorded_at_utc: str | None = None,
    ) -> RecoveryCheckpointV1:
        if type(plan) is not ExperimentWorkPlanV1:
            raise TypeError("recovery submission requires ExperimentWorkPlanV1")
        if self._store.load(plan.plan_id) is not None:
            raise RecoveryRefused("PLAN_ALREADY_SUBMITTED", "plan already has recovery state")
        required_audits = complete_run_runtime_audit_identities()
        records = tuple(
            RecoveryWorkRecordV1(
                work_request_id=WorkRequestV1(
                    logical_work_unit=unit,
                    required_runtime_audits=required_audits,
                ).work_request_id,
                logical_work_unit_id=unit.logical_work_unit_id,
                state=RecoveryWorkStateV1.QUEUED,
            )
            for unit in plan.logical_units
        )
        event = _event(
            (),
            RecoveryEventKindV1.SUBMITTED,
            recorded_at_utc=recorded_at_utc,
            details={"logical_work_unit_count": len(records)},
        )
        checkpoint = RecoveryCheckpointV1(
            plan=plan,
            revision=1,
            previous_checkpoint_sha256=None,
            status=RecoveryExperimentStatusV1.SUBMITTED,
            records=records,
            events=(event,),
        )
        self._store.save(checkpoint)
        return checkpoint

    def status(self, plan_id: str) -> RecoveryCheckpointV1:
        checkpoint = self._store.load(plan_id)
        if checkpoint is None:
            raise RecoveryRefused("PLAN_NOT_FOUND", "plan has no recovery checkpoint")
        return checkpoint

    def cancel(
        self,
        plan_id: str,
        *,
        reason_code: str = "OPERATOR_CANCELLED",
        recorded_at_utc: str | None = None,
    ) -> RecoveryCheckpointV1:
        selected_reason = _reason(reason_code, "cancellation reason")
        checkpoint = self.status(plan_id)
        if checkpoint.status is RecoveryExperimentStatusV1.COMPLETED:
            raise RecoveryRefused("PLAN_ALREADY_COMPLETED", "completed work cannot be cancelled")
        if checkpoint.status is RecoveryExperimentStatusV1.QUARANTINED:
            raise RecoveryRefused("PLAN_QUARANTINED", "quarantined work cannot be cancelled")
        if checkpoint.status is RecoveryExperimentStatusV1.CANCELLED:
            return checkpoint
        records = tuple(
            item
            if item.state is RecoveryWorkStateV1.REGISTERED
            else replace(
                item,
                state=RecoveryWorkStateV1.CANCELLED,
                selected_result_sha256=None,
                selected_manifest_sha256=None,
                successful_result_sha256s=(),
                registered_manifest_sha256s=(),
                last_failure_code="OPERATOR_CANCELLED",
            )
            for item in checkpoint.records
        )
        events = _append_event(
            checkpoint.events,
            RecoveryEventKindV1.CANCELLED,
            recorded_at_utc=recorded_at_utc,
            details={"reason_code": selected_reason},
        )
        updated = _advance(
            checkpoint,
            status=RecoveryExperimentStatusV1.CANCELLED,
            records=records,
            events=events,
            aggregate=None,
            cancellation_reason_code=selected_reason,
        )
        self._store.save(updated)
        return updated

    def resume(
        self,
        plan_id: str,
        backend: ExecutionBackendV1,
        *,
        completion_order: RecoveryCompletionOrderV1 = RecoveryCompletionOrderV1.CANONICAL,
        recorded_at_utc: str | None = None,
    ) -> RecoveryCheckpointV1:
        if type(completion_order) is not RecoveryCompletionOrderV1:
            raise TypeError("recovery completion order is invalid")
        backend_id = getattr(backend, "backend_id", None)
        if type(backend_id) is not str or _BACKEND_ID.fullmatch(backend_id) is None:
            raise TypeError("recovery backend must expose one canonical backend ID")
        checkpoint = self.status(plan_id)
        if checkpoint.status is RecoveryExperimentStatusV1.COMPLETED:
            return checkpoint
        if checkpoint.status is RecoveryExperimentStatusV1.CANCELLED:
            raise RecoveryRefused("PLAN_CANCELLED", "cancelled work cannot be resumed")
        if checkpoint.status is RecoveryExperimentStatusV1.QUARANTINED:
            raise RecoveryRefused("PLAN_QUARANTINED", "quarantined work cannot be resumed")

        checkpoint = self._reconcile_pending_registrations(
            checkpoint,
            recorded_at_utc=recorded_at_utc,
        )
        if all(
            item.state is RecoveryWorkStateV1.REGISTERED
            for item in checkpoint.records
        ):
            return self._complete_registered(
                checkpoint,
                recorded_at_utc=recorded_at_utc,
            )
        checkpoint = self._expire_in_flight(checkpoint, recorded_at_utc=recorded_at_utc)
        events = _append_event(
            checkpoint.events,
            RecoveryEventKindV1.RESUMED,
            recorded_at_utc=recorded_at_utc,
            details={"backend_id": backend_id},
        )
        records: list[RecoveryWorkRecordV1] = []
        active_ids: list[str] = []
        for item in checkpoint.records:
            if item.state not in {
                RecoveryWorkStateV1.QUEUED,
                RecoveryWorkStateV1.PENDING_REGISTRATION,
            }:
                records.append(item)
                continue
            next_attempt = item.attempt_number + 1
            if next_attempt > MAX_ATTEMPT_NUMBER_V1:
                raise RecoveryRefused("ATTEMPT_LIMIT_REACHED", "work exhausted attempt numbers")
            attempt_id = derive_attempt_id(item.logical_work_unit_id, next_attempt)
            kind = (
                RecoveryEventKindV1.ATTEMPT_STARTED
                if item.attempt_number == 0
                else RecoveryEventKindV1.ATTEMPT_REISSUED
            )
            events = _append_event(
                events,
                kind,
                recorded_at_utc=recorded_at_utc,
                logical_work_unit_id=item.logical_work_unit_id,
                attempt_number=next_attempt,
                attempt_id=attempt_id,
                details={"backend_id": backend_id},
            )
            records.append(
                replace(
                    item,
                    state=RecoveryWorkStateV1.IN_FLIGHT,
                    attempt_number=next_attempt,
                    last_attempt_id=attempt_id,
                    last_failure_code=None,
                )
            )
            active_ids.append(item.logical_work_unit_id)
        started = _advance(
            checkpoint,
            status=RecoveryExperimentStatusV1.RUNNING,
            records=tuple(records),
            events=events,
            aggregate=None,
            cancellation_reason_code=None,
        )
        self._store.save(started)
        if not active_ids:
            return self._complete_registered(started, recorded_at_utc=recorded_at_utc)

        active_set = frozenset(active_ids)
        active_units = tuple(
            unit for unit in started.plan.logical_units if unit.logical_work_unit_id in active_set
        )
        subplan = ExperimentWorkPlanV1(
            master_seed_identity=started.plan.master_seed_identity,
            experiment_identity=started.plan.experiment_identity,
            logical_units=active_units,
        )
        try:
            run = self._coordinator.execute(subplan, backend)
        except Exception as error:
            failed = self._record_backend_failure(
                started,
                backend_id=backend_id,
                error_type=type(error).__name__,
                recorded_at_utc=recorded_at_utc,
            )
            self._store.save(failed)
            raise

        returned = run.verified_results
        if completion_order is RecoveryCompletionOrderV1.REVERSE:
            returned = tuple(reversed(returned))
        current = started
        for result in returned:
            record = _record_by_logical_id(current, result.logical_work_unit_id)
            current = self._accept_success(
                current,
                result,
                attempt_number=record.attempt_number,
                recorded_at_utc=recorded_at_utc,
            )
            self._store.save(current)
            if current.status is RecoveryExperimentStatusV1.QUARANTINED:
                return current
        return self._complete_registered(current, recorded_at_utc=recorded_at_utc)

    def record_success(
        self,
        plan_id: str,
        result: VerifiedWorkResultV1,
        *,
        attempt_number: int,
        recorded_at_utc: str | None = None,
    ) -> RecoveryCheckpointV1:
        """Accept an on-time or late verified result through one idempotent reducer."""

        checkpoint = self.status(plan_id)
        if checkpoint.status is RecoveryExperimentStatusV1.CANCELLED:
            raise RecoveryRefused("PLAN_CANCELLED", "cancelled work refuses late results")
        checkpoint = self._reconcile_pending_registrations(
            checkpoint,
            recorded_at_utc=recorded_at_utc,
        )
        updated = self._accept_success(
            checkpoint,
            result,
            attempt_number=attempt_number,
            recorded_at_utc=recorded_at_utc,
        )
        self._store.save(updated)
        if (
            updated.status is not RecoveryExperimentStatusV1.QUARANTINED
            and all(item.state is RecoveryWorkStateV1.REGISTERED for item in updated.records)
            and updated.status is not RecoveryExperimentStatusV1.COMPLETED
        ):
            return self._complete_registered(updated, recorded_at_utc=recorded_at_utc)
        return updated

    def cleanup_unregistered_attempt(
        self,
        attempt: ResultAttemptStageV1,
        *,
        plan_id: str,
        recorded_at_utc: str | None = None,
    ) -> RecoveryCheckpointV1:
        """Discard one exact private staging capability, never a registered digest."""

        if type(attempt) is not ResultAttemptStageV1:
            raise TypeError("recovery cleanup requires an exact attempt capability")
        checkpoint = self.status(plan_id)
        self._content.discard_result_attempt(attempt)
        events = _append_event(
            checkpoint.events,
            RecoveryEventKindV1.STAGING_DISCARDED,
            recorded_at_utc=recorded_at_utc,
            logical_work_unit_id=attempt.logical_work_unit_id,
            details={"stage_key_sha256": attempt.stage_key_sha256},
        )
        updated = _advance(checkpoint, events=events)
        self._store.save(updated)
        return updated

    def _reconcile_pending_registrations(
        self,
        checkpoint: RecoveryCheckpointV1,
        *,
        recorded_at_utc: str | None,
    ) -> RecoveryCheckpointV1:
        """Finalize a manifest published before its checkpoint acknowledgement."""

        records: list[RecoveryWorkRecordV1] = []
        events = checkpoint.events
        changed = False
        for item in checkpoint.records:
            if item.state is not RecoveryWorkStateV1.PENDING_REGISTRATION:
                records.append(item)
                continue
            manifest_digest = item.selected_manifest_sha256
            result_digest = item.selected_result_sha256
            if manifest_digest is None or result_digest is None:
                raise RuntimeError("pending registration lost its write-ahead identity")
            try:
                manifest = self._content.read_result_manifest(manifest_digest)
            except ContentStoreRefused as error:
                if error.refusal.code not in {
                    ContentStoreRefusalCodeV1.DATA_PATHS_UNSAFE,
                    ContentStoreRefusalCodeV1.REGISTERED_MANIFEST_INVALID,
                }:
                    raise
                records.append(item)
                continue
            if (
                manifest.manifest_sha256 != manifest_digest
                or manifest.logical_work_unit_id != item.logical_work_unit_id
                or manifest.work_request_id != item.work_request_id
                or manifest.coordinator_verification_sha256 != result_digest
            ):
                raise RecoveryRefused(
                    "PENDING_REGISTRATION_CONFLICT",
                    "published manifest differs from its write-ahead result",
                )
            changed = True
            registered = replace(
                item,
                state=RecoveryWorkStateV1.REGISTERED,
                registered_manifest_sha256s=(manifest_digest,),
                last_failure_code=None,
            )
            records.append(registered)
            events = _append_event(
                events,
                RecoveryEventKindV1.RESULT_REGISTERED,
                recorded_at_utc=recorded_at_utc,
                logical_work_unit_id=item.logical_work_unit_id,
                attempt_number=item.attempt_number,
                attempt_id=item.last_attempt_id,
                details={
                    "manifest_sha256": manifest_digest,
                    "restart_reconciled": True,
                },
            )
        if not changed:
            return checkpoint
        updated = _advance(
            checkpoint,
            status=checkpoint.status,
            records=tuple(records),
            events=events,
        )
        self._store.save(updated)
        return updated

    def _expire_in_flight(
        self,
        checkpoint: RecoveryCheckpointV1,
        *,
        recorded_at_utc: str | None,
    ) -> RecoveryCheckpointV1:
        records: list[RecoveryWorkRecordV1] = []
        events = checkpoint.events
        changed = False
        for item in checkpoint.records:
            if item.state is not RecoveryWorkStateV1.IN_FLIGHT:
                records.append(item)
                continue
            changed = True
            events = _append_event(
                events,
                RecoveryEventKindV1.LEASE_EXPIRED,
                recorded_at_utc=recorded_at_utc,
                logical_work_unit_id=item.logical_work_unit_id,
                attempt_number=item.attempt_number,
                attempt_id=item.last_attempt_id,
                details={},
            )
            records.append(
                replace(
                    item,
                    state=(
                        RecoveryWorkStateV1.PENDING_REGISTRATION
                        if item.successful_result_sha256s
                        else RecoveryWorkStateV1.QUEUED
                    ),
                    last_failure_code="LEASE_EXPIRED",
                )
            )
        if not changed:
            return checkpoint
        updated = _advance(
            checkpoint,
            status=RecoveryExperimentStatusV1.RUNNING,
            records=tuple(records),
            events=events,
        )
        self._store.save(updated)
        return updated

    def _record_backend_failure(
        self,
        checkpoint: RecoveryCheckpointV1,
        *,
        backend_id: str,
        error_type: str,
        recorded_at_utc: str | None,
    ) -> RecoveryCheckpointV1:
        safe_error_type = (
            error_type
            if type(error_type) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", error_type)
            else "BackendError"
        )
        records: list[RecoveryWorkRecordV1] = []
        events = checkpoint.events
        for item in checkpoint.records:
            if item.state is not RecoveryWorkStateV1.IN_FLIGHT:
                records.append(item)
                continue
            events = _append_event(
                events,
                RecoveryEventKindV1.ATTEMPT_FAILED,
                recorded_at_utc=recorded_at_utc,
                logical_work_unit_id=item.logical_work_unit_id,
                attempt_number=item.attempt_number,
                attempt_id=item.last_attempt_id,
                details={"backend_id": backend_id, "error_type": safe_error_type},
            )
            records.append(
                replace(
                    item,
                    state=(
                        RecoveryWorkStateV1.PENDING_REGISTRATION
                        if item.successful_result_sha256s
                        else RecoveryWorkStateV1.QUEUED
                    ),
                    last_failure_code="BACKEND_EXECUTION_FAILED",
                )
            )
        return _advance(checkpoint, records=tuple(records), events=events)

    def _accept_success(
        self,
        checkpoint: RecoveryCheckpointV1,
        result: VerifiedWorkResultV1,
        *,
        attempt_number: int,
        recorded_at_utc: str | None,
    ) -> RecoveryCheckpointV1:
        if type(result) is not VerifiedWorkResultV1:
            raise TypeError("recovery success requires VerifiedWorkResultV1")
        record = _record_by_logical_id(checkpoint, result.logical_work_unit_id)
        _bounded_integer(
            attempt_number,
            "returned recovery attempt number",
            1,
            record.attempt_number,
        )
        attempt_id = derive_attempt_id(record.logical_work_unit_id, attempt_number)
        if result.work_request_id != record.work_request_id:
            raise RecoveryRefused("FOREIGN_RESULT", "verified result names another request")
        digest = result.scientific_result_sha256
        if digest in record.successful_result_sha256s:
            logical_unit = _unit_by_logical_id(
                checkpoint.plan,
                record.logical_work_unit_id,
            )
            expected_manifest = build_verified_result_manifest(
                logical_unit,
                result,
            )
            if (
                record.selected_result_sha256 == digest
                and record.selected_manifest_sha256
                != expected_manifest.manifest_sha256
            ):
                raise RecoveryRefused(
                    "IDEMPOTENCE_BINDING_FAILURE",
                    "identical result digest produced a different manifest identity",
                )
            events = _append_event(
                checkpoint.events,
                RecoveryEventKindV1.LATE_RESULT_IDEMPOTENT,
                recorded_at_utc=recorded_at_utc,
                logical_work_unit_id=record.logical_work_unit_id,
                attempt_number=attempt_number,
                attempt_id=attempt_id,
                details={"scientific_result_sha256": digest},
            )
            if expected_manifest.manifest_sha256 in record.registered_manifest_sha256s:
                return _advance(checkpoint, events=events)
            if record.state not in {
                RecoveryWorkStateV1.IN_FLIGHT,
                RecoveryWorkStateV1.PENDING_REGISTRATION,
                RecoveryWorkStateV1.QUARANTINED,
            }:
                raise RecoveryRefused(
                    "IDEMPOTENCE_BINDING_FAILURE",
                    "known result has no registered or recoverable manifest",
                )
            registration = self._register_idempotently(
                logical_unit=logical_unit,
                result=result,
                attempt_id=attempt_id,
            )
            manifests = tuple(
                sorted(
                    set(
                        (
                            *record.registered_manifest_sha256s,
                            registration.manifest_sha256,
                        )
                    )
                )
            )
            recovered_record = (
                replace(
                    record,
                    registered_manifest_sha256s=manifests,
                )
                if record.state is RecoveryWorkStateV1.QUARANTINED
                else replace(
                    record,
                    state=RecoveryWorkStateV1.REGISTERED,
                    selected_result_sha256=digest,
                    selected_manifest_sha256=registration.manifest_sha256,
                    registered_manifest_sha256s=(registration.manifest_sha256,),
                    last_failure_code=None,
                )
            )
            events = _append_event(
                events,
                RecoveryEventKindV1.RESULT_REGISTERED,
                recorded_at_utc=recorded_at_utc,
                logical_work_unit_id=record.logical_work_unit_id,
                attempt_number=attempt_number,
                attempt_id=attempt_id,
                details={
                    "manifest_sha256": registration.manifest_sha256,
                    "restart_recovered": True,
                },
            )
            return _advance(
                checkpoint,
                records=_replace_record(checkpoint.records, recovered_record),
                events=events,
            )

        if record.successful_result_sha256s:
            conflicts = tuple(sorted((*record.successful_result_sha256s, digest)))
            logical_unit = _unit_by_logical_id(
                checkpoint.plan,
                record.logical_work_unit_id,
            )
            conflict_manifest = build_verified_result_manifest(logical_unit, result)
            quarantine_write_ahead = replace(
                record,
                state=RecoveryWorkStateV1.QUARANTINED,
                selected_result_sha256=None,
                selected_manifest_sha256=None,
                successful_result_sha256s=conflicts,
                last_failure_code="DETERMINISM_FAILURE",
            )
            events = _append_event(
                checkpoint.events,
                RecoveryEventKindV1.DETERMINISM_FAILURE,
                recorded_at_utc=recorded_at_utc,
                logical_work_unit_id=record.logical_work_unit_id,
                attempt_number=attempt_number,
                attempt_id=attempt_id,
                details={
                    "conflicting_manifest_sha256": conflict_manifest.manifest_sha256,
                    "successful_result_sha256s": list(conflicts),
                },
            )
            quarantined_checkpoint = _advance(
                checkpoint,
                status=RecoveryExperimentStatusV1.QUARANTINED,
                records=_replace_record(
                    checkpoint.records,
                    quarantine_write_ahead,
                ),
                events=events,
                aggregate=None,
            )
            self._store.save(quarantined_checkpoint)
            conflict_registration = self._register_idempotently(
                logical_unit=logical_unit,
                result=result,
                attempt_id=attempt_id,
            )
            evidence_manifests = tuple(
                sorted(
                    (
                        *record.registered_manifest_sha256s,
                        conflict_registration.manifest_sha256,
                    )
                )
            )
            quarantined = replace(
                quarantine_write_ahead,
                registered_manifest_sha256s=evidence_manifests,
            )
            events = _append_event(
                quarantined_checkpoint.events,
                RecoveryEventKindV1.RESULT_REGISTERED,
                recorded_at_utc=recorded_at_utc,
                logical_work_unit_id=record.logical_work_unit_id,
                attempt_number=attempt_number,
                attempt_id=attempt_id,
                details={
                    "manifest_sha256": conflict_registration.manifest_sha256,
                    "quarantine_evidence": True,
                },
            )
            return _advance(
                quarantined_checkpoint,
                status=RecoveryExperimentStatusV1.QUARANTINED,
                records=_replace_record(
                    quarantined_checkpoint.records,
                    quarantined,
                ),
                events=events,
                aggregate=None,
            )

        if record.state not in {RecoveryWorkStateV1.QUEUED, RecoveryWorkStateV1.IN_FLIGHT}:
            raise RecoveryRefused("RESULT_STATE_INVALID", "work cannot accept a first success")
        logical_unit = _unit_by_logical_id(checkpoint.plan, record.logical_work_unit_id)
        expected_manifest = build_verified_result_manifest(logical_unit, result)
        pending_record = replace(
            record,
            state=RecoveryWorkStateV1.PENDING_REGISTRATION,
            selected_result_sha256=digest,
            selected_manifest_sha256=expected_manifest.manifest_sha256,
            successful_result_sha256s=(digest,),
            registered_manifest_sha256s=(),
            last_failure_code=None,
        )
        events = _append_event(
            checkpoint.events,
            RecoveryEventKindV1.RESULT_ACCEPTED,
            recorded_at_utc=recorded_at_utc,
            logical_work_unit_id=record.logical_work_unit_id,
            attempt_number=attempt_number,
            attempt_id=attempt_id,
            details={
                "expected_manifest_sha256": expected_manifest.manifest_sha256,
                "scientific_result_sha256": digest,
            },
        )
        pending = _advance(
            checkpoint,
            status=RecoveryExperimentStatusV1.RUNNING,
            records=_replace_record(checkpoint.records, pending_record),
            events=events,
            aggregate=None,
        )
        self._store.save(pending)
        registration = self._register_idempotently(
            logical_unit=logical_unit,
            result=result,
            attempt_id=attempt_id,
        )
        registered = replace(
            pending_record,
            state=RecoveryWorkStateV1.REGISTERED,
            selected_manifest_sha256=registration.manifest_sha256,
            registered_manifest_sha256s=(registration.manifest_sha256,),
            last_failure_code=None,
        )
        events = _append_event(
            pending.events,
            RecoveryEventKindV1.RESULT_REGISTERED,
            recorded_at_utc=recorded_at_utc,
            logical_work_unit_id=record.logical_work_unit_id,
            attempt_number=attempt_number,
            attempt_id=attempt_id,
            details={"manifest_sha256": registration.manifest_sha256},
        )
        return _advance(
            pending,
            status=RecoveryExperimentStatusV1.RUNNING,
            records=_replace_record(pending.records, registered),
            events=events,
            aggregate=None,
        )

    def _register_idempotently(
        self,
        *,
        logical_unit,
        result: VerifiedWorkResultV1,
        attempt_id: str,
    ) -> RegisteredResultBundleV1:
        manifest = build_verified_result_manifest(logical_unit, result)
        attempt = self._content.begin_result_attempt(
            attempt_id=attempt_id,
            work_request_id=result.work_request_id,
            logical_work_unit_id=result.logical_work_unit_id,
        )
        artifacts = {item.artifact_id: item for item in result.artifacts}
        try:
            for descriptor in manifest.artifacts:
                self._content.stage_result_artifact(
                    attempt,
                    descriptor,
                    artifacts[descriptor.artifact_id].payload_bytes,
                )
            return self._content.register_result_bundle(
                attempt,
                manifest,
                logical_work_unit=logical_unit,
                coordinator_verification=result,
            )
        except ContentStoreRefused as error:
            self._discard_failed_stage(attempt)
            if (
                error.refusal.code
                is not ContentStoreRefusalCodeV1.REGISTERED_CONTENT_IMMUTABLE
            ):
                raise
            existing = self._content.read_result_manifest(manifest.manifest_sha256)
            if existing != manifest:
                raise RecoveryRefused(
                    "REGISTERED_RESULT_CONFLICT",
                    "immutable manifest digest did not restore the expected manifest",
                ) from error
            return RegisteredResultBundleV1(
                manifest=existing,
                manifest_sha256=manifest.manifest_sha256,
                artifact_count=len(existing.artifacts),
            )
        except Exception:
            self._discard_failed_stage(attempt)
            raise

    def _discard_failed_stage(self, attempt: ResultAttemptStageV1) -> None:
        try:
            self._content.discard_result_attempt(attempt)
        except ContentStoreRefused as cleanup_error:
            if (
                cleanup_error.refusal.code
                is ContentStoreRefusalCodeV1.ATTEMPT_NOT_FOUND
            ):
                return
            raise RecoveryRefused(
                "STAGING_CLEANUP_FAILED",
                "failed registration left an unconfirmed private attempt stage",
            ) from cleanup_error

    def _complete_registered(
        self,
        checkpoint: RecoveryCheckpointV1,
        *,
        recorded_at_utc: str | None,
    ) -> RecoveryCheckpointV1:
        if checkpoint.status is RecoveryExperimentStatusV1.COMPLETED:
            return checkpoint
        if not all(item.state is RecoveryWorkStateV1.REGISTERED for item in checkpoint.records):
            return checkpoint
        manifest_map = {
            item.logical_work_unit_id: item.selected_manifest_sha256
            for item in checkpoint.records
            if item.selected_manifest_sha256 is not None
        }
        aggregate = aggregate_registered_results(
            checkpoint.plan,
            manifest_map,
            self._content,
        )
        events = _append_event(
            checkpoint.events,
            RecoveryEventKindV1.COMPLETED,
            recorded_at_utc=recorded_at_utc,
            details={"aggregate_sha256": aggregate.aggregate_sha256},
        )
        completed = _advance(
            checkpoint,
            status=RecoveryExperimentStatusV1.COMPLETED,
            events=events,
            aggregate=aggregate,
        )
        self._store.save(completed)
        return completed


def _record_by_logical_id(
    checkpoint: RecoveryCheckpointV1,
    logical_work_unit_id: str,
) -> RecoveryWorkRecordV1:
    digest = _sha256(logical_work_unit_id, "recovery result logical-work ID")
    matches = tuple(item for item in checkpoint.records if item.logical_work_unit_id == digest)
    if len(matches) != 1:
        raise RecoveryRefused("FOREIGN_RESULT", "result names no unique planned work unit")
    return matches[0]


def _unit_by_logical_id(plan: ExperimentWorkPlanV1, logical_work_unit_id: str):
    matches = tuple(
        item for item in plan.logical_units if item.logical_work_unit_id == logical_work_unit_id
    )
    if len(matches) != 1:
        raise RecoveryRefused("FOREIGN_RESULT", "logical work is absent from the plan")
    return matches[0]


def _replace_record(
    records: tuple[RecoveryWorkRecordV1, ...],
    replacement: RecoveryWorkRecordV1,
) -> tuple[RecoveryWorkRecordV1, ...]:
    replaced = tuple(
        replacement if item.logical_work_unit_id == replacement.logical_work_unit_id else item
        for item in records
    )
    if sum(item.logical_work_unit_id == replacement.logical_work_unit_id for item in records) != 1:
        raise RuntimeError("recovery record replacement did not name exactly one unit")
    return replaced


def _advance(
    checkpoint: RecoveryCheckpointV1,
    *,
    status: RecoveryExperimentStatusV1 | None = None,
    records: tuple[RecoveryWorkRecordV1, ...] | None = None,
    events: tuple[RecoveryOperationalEventV1, ...] | None = None,
    aggregate: ExperimentAggregateV1 | None | object = ...,
    cancellation_reason_code: str | None | object = ...,
) -> RecoveryCheckpointV1:
    selected_aggregate = checkpoint.aggregate if aggregate is ... else aggregate
    selected_cancellation = (
        checkpoint.cancellation_reason_code
        if cancellation_reason_code is ...
        else cancellation_reason_code
    )
    return RecoveryCheckpointV1(
        plan=checkpoint.plan,
        revision=checkpoint.revision + 1,
        previous_checkpoint_sha256=checkpoint.checkpoint_sha256,
        status=checkpoint.status if status is None else status,
        records=checkpoint.records if records is None else records,
        events=checkpoint.events if events is None else events,
        aggregate=selected_aggregate,
        cancellation_reason_code=selected_cancellation,
    )


def _event(
    prior: tuple[RecoveryOperationalEventV1, ...],
    kind: RecoveryEventKindV1,
    *,
    recorded_at_utc: str | None,
    logical_work_unit_id: str | None = None,
    attempt_number: int | None = None,
    attempt_id: str | None = None,
    details: Mapping[str, object] | None = None,
) -> RecoveryOperationalEventV1:
    return RecoveryOperationalEventV1(
        sequence=len(prior) + 1,
        kind=kind,
        recorded_at_utc=_now_utc() if recorded_at_utc is None else recorded_at_utc,
        event_nonce=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
        logical_work_unit_id=logical_work_unit_id,
        attempt_number=attempt_number,
        attempt_id=attempt_id,
        details={} if details is None else details,
    )


def _append_event(
    prior: tuple[RecoveryOperationalEventV1, ...],
    kind: RecoveryEventKindV1,
    **kwargs,
) -> tuple[RecoveryOperationalEventV1, ...]:
    return (*prior, _event(prior, kind, **kwargs))


def _now_utc() -> str:
    value = datetime.now(timezone.utc)
    if value.microsecond:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(value: object, label: str) -> datetime:
    if type(value) is not str or _UTC.fullmatch(value) is None:
        raise ValueError(f"{label} must be canonical UTC text")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    expected = (
        parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if parsed.microsecond
        else parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    if expected != value:
        raise ValueError(f"{label} is not canonically formatted")
    return parsed


class _RecoveryLock:
    __slots__ = ("_descriptor", "_directory")

    def __init__(self, directory: int) -> None:
        self._directory = directory
        self._descriptor = -1

    def __enter__(self) -> _RecoveryLock:
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(_LOCK_FILE, flags, 0o600, dir_fd=self._directory)
        _require_private_regular(os.fstat(descriptor), "recovery lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._descriptor >= 0:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._descriptor = -1


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    _require_directory(os.fstat(descriptor), "recovery checkpoint area", private=False)
    return descriptor


def _open_directory_at(parent: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent)
    _require_directory(os.fstat(descriptor), "recovery state directory", private=True)
    return descriptor


def _ensure_directory_at(parent: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError:
        pass
    return _open_directory_at(parent, name)


def _open_regular_at(parent: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent)
    _require_private_regular(os.fstat(descriptor), "recovery checkpoint")
    return descriptor


def _write_temporary(parent: int, raw: bytes) -> str:
    name = f"{_TEMP_PREFIX}{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o600, dir_fd=parent)
    try:
        _require_private_regular(os.fstat(descriptor), "temporary recovery checkpoint")
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("recovery checkpoint write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass
        raise
    os.close(descriptor)
    return name


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if not raw or len(raw) > maximum:
        raise ValueError("recovery checkpoint is empty or exceeds its byte limit")
    return raw


def _require_directory(metadata: os.stat_result, label: str, *, private: bool) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} is not a directory")
    if metadata.st_uid != os.getuid():
        raise PermissionError(f"{label} is not owned by the current user")
    if private and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError(f"{label} must not grant group or other permissions")


def _require_private_regular(metadata: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError(f"{label} must be one non-hardlinked regular file")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError(f"{label} must be private and user-owned")


def _exact(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact object")
    if set(value) != fields:
        raise ValueError(f"{label} fields differ from the V1 schema")
    return value


def _schema(row: dict[str, object], schema_id: str, label: str) -> None:
    if (
        type(row["schema_id"]) is not str
        or row["schema_id"] != schema_id
        or type(row["schema_version"]) is not int
        or row["schema_version"] != 1
    ):
        raise ValueError(f"{label} schema is not supported")


def _text(row: dict[str, object], key: str) -> str:
    value = row[key]
    if type(value) is not str or not value:
        raise TypeError(f"{key} must be nonempty exact text")
    return value


def _optional_text(row: dict[str, object], key: str) -> str | None:
    value = row[key]
    if value is None:
        return None
    if type(value) is not str or not value:
        raise TypeError(f"{key} must be nonempty exact text or null")
    return value


def _integer(row: dict[str, object], key: str) -> int:
    value = row[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an exact integer")
    return value


def _optional_integer(row: dict[str, object], key: str) -> int | None:
    value = row[key]
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError(f"{key} must be an exact integer or null")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _reason(value: object, label: str) -> str:
    if type(value) is not str or _REASON_CODE.fullmatch(value) is None:
        raise ValueError(f"{label} must be canonical uppercase text")
    return value


def _bounded_integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} is outside its V1 bound")
    return value


def _canonical_digests(values: tuple[str, ...], label: str) -> None:
    if type(values) is not tuple or values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be one canonical unique tuple")
    for value in values:
        _sha256(value, label)


def _digest_array(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an array")
    restored = tuple(value)
    _canonical_digests(restored, label)
    return restored


def _round_trip(value: object, row: dict[str, object], label: str) -> None:
    if getattr(value, "as_dict")() != row:
        raise ValueError(f"serialized {label} did not round-trip exactly")


__all__ = [
    "MAX_RECOVERY_CHECKPOINT_BYTES_V1",
    "ORCHESTRATION_RECOVERY_SCHEMA_VERSION",
    "RECOVERY_CHECKPOINT_SCHEMA_ID",
    "RECOVERY_OPERATIONAL_EVENT_SCHEMA_ID",
    "RECOVERY_WORK_RECORD_SCHEMA_ID",
    "RecoveryCheckpointStoreV1",
    "RecoveryCheckpointV1",
    "RecoveryCompletionOrderV1",
    "RecoveryCoordinatorV1",
    "RecoveryEventKindV1",
    "RecoveryExperimentStatusV1",
    "RecoveryOperationalEventV1",
    "RecoveryRefused",
    "RecoveryWorkRecordV1",
    "RecoveryWorkStateV1",
]
