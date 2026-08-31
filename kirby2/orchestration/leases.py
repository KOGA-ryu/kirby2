"""Operational leases, heartbeats, and crash-safe coordinator state for WO38-D.

Lease clocks and restart state never enter logical work or scientific result identity.
They determine only whether an operational attempt may continue, be reissued, or be
quarantined.  The coordinator snapshot distinguishes every pre-registration state and
is atomically replaced beneath the governed checkpoints area.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import ClassVar

from kirby2.orchestration.models import (
    ORCHESTRATION_MODEL_SCHEMA_VERSION,
    WORK_ATTEMPT_IDENTITY_SCHEMA_ID,
)
from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes
from kirby2.research.paths import DataAreaId, DataPaths


ORCHESTRATION_LEASE_SCHEMA_VERSION = 1
LEASE_POLICY_SCHEMA_ID = "KIRBY2_ORCHESTRATION_LEASE_POLICY_V1"
LEASE_GRANT_SCHEMA_ID = "KIRBY2_ORCHESTRATION_LEASE_GRANT_V1"
LEASE_HEARTBEAT_SCHEMA_ID = "KIRBY2_ORCHESTRATION_LEASE_HEARTBEAT_V1"
COORDINATOR_WORK_STATE_SCHEMA_ID = "KIRBY2_COORDINATOR_WORK_STATE_V1"
COORDINATOR_STATE_SNAPSHOT_SCHEMA_ID = "KIRBY2_COORDINATOR_STATE_SNAPSHOT_V1"

MAX_LEASE_SECONDS_V1 = 24 * 60 * 60
MAX_HEARTBEAT_INTERVAL_SECONDS_V1 = 60 * 60
MAX_MISSED_HEARTBEATS_V1 = 1024
MAX_COORDINATOR_STATE_RECORDS_V1 = 1_000_000
MAX_COORDINATOR_SNAPSHOT_BYTES_V1 = 64 * 1024 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z")
_STATE_DIRECTORY = "orchestration-coordinator-v1"
_LOCK_FILE = ".coordinator-state.lock"
_TEMP_PREFIX = ".coordinator-state-tmp-"


class LeaseRefusalCodeV1(str, Enum):
    UNKNOWN_LEASE = "UNKNOWN_LEASE"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    HEARTBEAT_REPLAYED = "HEARTBEAT_REPLAYED"
    HEARTBEAT_SEQUENCE_GAP = "HEARTBEAT_SEQUENCE_GAP"
    LEASE_BINDING_MISMATCH = "LEASE_BINDING_MISMATCH"


class LeaseRefused(RuntimeError):
    def __init__(self, code: LeaseRefusalCodeV1, detail: str) -> None:
        if type(code) is not LeaseRefusalCodeV1:
            raise TypeError("lease refusal code is invalid")
        if type(detail) is not str or not detail or len(detail.encode("utf-8")) > 2048:
            raise ValueError("lease refusal detail must be bounded text")
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


class CoordinatorWorkStateV1(str, Enum):
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    COMPLETED_UNVERIFIED = "COMPLETED_UNVERIFIED"
    REGISTERED = "REGISTERED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class LeasePolicyV1:
    lease_seconds: int
    heartbeat_interval_seconds: int
    maximum_missed_heartbeats: int

    schema_id: ClassVar[str] = LEASE_POLICY_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_LEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _bounded_int(self.lease_seconds, "lease seconds", 1, MAX_LEASE_SECONDS_V1)
        _bounded_int(
            self.heartbeat_interval_seconds,
            "heartbeat interval seconds",
            1,
            MAX_HEARTBEAT_INTERVAL_SECONDS_V1,
        )
        _bounded_int(
            self.maximum_missed_heartbeats,
            "maximum missed heartbeats",
            1,
            MAX_MISSED_HEARTBEATS_V1,
        )
        if (
            self.heartbeat_interval_seconds * self.maximum_missed_heartbeats
            > self.lease_seconds
        ):
            raise ValueError("heartbeat miss window cannot exceed the lease duration")

    def as_dict(self) -> dict[str, object]:
        return {
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "lease_seconds": self.lease_seconds,
            "maximum_missed_heartbeats": self.maximum_missed_heartbeats,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> LeasePolicyV1:
        row = _exact(
            value,
            {
                "heartbeat_interval_seconds",
                "lease_seconds",
                "maximum_missed_heartbeats",
                "schema_id",
                "schema_version",
            },
            "lease policy",
        )
        _schema(row, cls.schema_id, "lease policy")
        restored = cls(
            lease_seconds=_integer(row, "lease_seconds"),
            heartbeat_interval_seconds=_integer(
                row,
                "heartbeat_interval_seconds",
            ),
            maximum_missed_heartbeats=_integer(
                row,
                "maximum_missed_heartbeats",
            ),
        )
        _round_trip(restored, row, "lease policy")
        return restored


@dataclass(frozen=True, slots=True)
class LeaseGrantV1:
    plan_id: str
    work_request_id: str
    logical_work_unit_id: str
    attempt_number: int
    attempt_id: str
    worker_id: str
    session_id: str
    lease_sequence: int
    issued_at_utc: str
    expires_at_utc: str
    grant_nonce: str

    schema_id: ClassVar[str] = LEASE_GRANT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_LEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.plan_id, "lease plan ID")
        _sha256(self.work_request_id, "lease work-request ID")
        _sha256(self.logical_work_unit_id, "lease logical-work ID")
        _bounded_int(self.attempt_number, "lease attempt number", 1, (1 << 31) - 1)
        expected_attempt = derive_attempt_id(
            self.logical_work_unit_id,
            self.attempt_number,
        )
        if not hmac.compare_digest(self.attempt_id, expected_attempt):
            raise ValueError("lease attempt ID differs from logical work and number")
        _identifier(self.worker_id, "lease worker ID")
        _sha256(self.session_id, "lease session ID")
        _bounded_int(self.lease_sequence, "lease sequence", 1, (1 << 63) - 1)
        issued = _utc(self.issued_at_utc, "lease issue time")
        expires = _utc(self.expires_at_utc, "lease expiry time")
        if issued >= expires:
            raise ValueError("lease expiry must follow issue time")
        if (expires - issued).total_seconds() > MAX_LEASE_SECONDS_V1:
            raise ValueError("lease duration exceeds the V1 hard limit")
        _sha256(self.grant_nonce, "lease grant nonce")

    @property
    def lease_id(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.identity_dict())).hexdigest()

    def identity_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "expires_at_utc": self.expires_at_utc,
            "grant_nonce": self.grant_nonce,
            "issued_at_utc": self.issued_at_utc,
            "lease_sequence": self.lease_sequence,
            "logical_work_unit_id": self.logical_work_unit_id,
            "plan_id": self.plan_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "work_request_id": self.work_request_id,
            "worker_id": self.worker_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "lease_id": self.lease_id}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> LeaseGrantV1:
        row = _exact(
            value,
            {
                "attempt_id",
                "attempt_number",
                "expires_at_utc",
                "grant_nonce",
                "issued_at_utc",
                "lease_id",
                "lease_sequence",
                "logical_work_unit_id",
                "plan_id",
                "schema_id",
                "schema_version",
                "session_id",
                "work_request_id",
                "worker_id",
            },
            "lease grant",
        )
        _schema(row, cls.schema_id, "lease grant")
        declared = _sha256(row["lease_id"], "declared lease ID")
        restored = cls(
            plan_id=_text(row, "plan_id"),
            work_request_id=_text(row, "work_request_id"),
            logical_work_unit_id=_text(row, "logical_work_unit_id"),
            attempt_number=_integer(row, "attempt_number"),
            attempt_id=_text(row, "attempt_id"),
            worker_id=_text(row, "worker_id"),
            session_id=_text(row, "session_id"),
            lease_sequence=_integer(row, "lease_sequence"),
            issued_at_utc=_text(row, "issued_at_utc"),
            expires_at_utc=_text(row, "expires_at_utc"),
            grant_nonce=_text(row, "grant_nonce"),
        )
        if not hmac.compare_digest(declared, restored.lease_id):
            raise ValueError("declared lease ID differs from exact lease content")
        _round_trip(restored, row, "lease grant")
        return restored


@dataclass(frozen=True, slots=True)
class LeaseHeartbeatV1:
    lease_id: str
    attempt_id: str
    worker_id: str
    session_id: str
    heartbeat_sequence: int
    sent_at_utc: str
    heartbeat_nonce: str

    schema_id: ClassVar[str] = LEASE_HEARTBEAT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_LEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.lease_id, "heartbeat lease ID")
        _sha256(self.attempt_id, "heartbeat attempt ID")
        _identifier(self.worker_id, "heartbeat worker ID")
        _sha256(self.session_id, "heartbeat session ID")
        _bounded_int(
            self.heartbeat_sequence,
            "heartbeat sequence",
            1,
            (1 << 63) - 1,
        )
        _utc(self.sent_at_utc, "heartbeat send time")
        _sha256(self.heartbeat_nonce, "heartbeat nonce")

    @property
    def heartbeat_id(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "heartbeat_nonce": self.heartbeat_nonce,
            "heartbeat_sequence": self.heartbeat_sequence,
            "lease_id": self.lease_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sent_at_utc": self.sent_at_utc,
            "session_id": self.session_id,
            "worker_id": self.worker_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> LeaseHeartbeatV1:
        row = _exact(
            value,
            {
                "attempt_id",
                "heartbeat_nonce",
                "heartbeat_sequence",
                "lease_id",
                "schema_id",
                "schema_version",
                "sent_at_utc",
                "session_id",
                "worker_id",
            },
            "lease heartbeat",
        )
        _schema(row, cls.schema_id, "lease heartbeat")
        restored = cls(
            lease_id=_text(row, "lease_id"),
            attempt_id=_text(row, "attempt_id"),
            worker_id=_text(row, "worker_id"),
            session_id=_text(row, "session_id"),
            heartbeat_sequence=_integer(row, "heartbeat_sequence"),
            sent_at_utc=_text(row, "sent_at_utc"),
            heartbeat_nonce=_text(row, "heartbeat_nonce"),
        )
        _round_trip(restored, row, "lease heartbeat")
        return restored


@dataclass(frozen=True, slots=True)
class CoordinatorWorkStateRecordV1:
    work_request_id: str
    logical_work_unit_id: str
    state: CoordinatorWorkStateV1
    attempt_id: str | None = None
    worker_id: str | None = None
    lease_id: str | None = None
    returned_result_sha256: str | None = None
    registered_manifest_sha256: str | None = None
    failure_code: str | None = None

    schema_id: ClassVar[str] = COORDINATOR_WORK_STATE_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_LEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.work_request_id, "coordinator state work-request ID")
        _sha256(self.logical_work_unit_id, "coordinator state logical-work ID")
        if type(self.state) is not CoordinatorWorkStateV1:
            raise TypeError("coordinator work state is invalid")
        for value, label in (
            (self.attempt_id, "coordinator state attempt ID"),
            (self.lease_id, "coordinator state lease ID"),
            (self.returned_result_sha256, "coordinator state returned result"),
            (self.registered_manifest_sha256, "coordinator state manifest"),
        ):
            if value is not None:
                _sha256(value, label)
        if self.worker_id is not None:
            _identifier(self.worker_id, "coordinator state worker ID")
        if self.failure_code is not None:
            _identifier(self.failure_code, "coordinator state failure code")
        self._validate_state_shape()

    def _validate_state_shape(self) -> None:
        leased = self.attempt_id is not None and self.worker_id is not None and self.lease_id is not None
        returned = self.returned_result_sha256 is not None
        registered = self.registered_manifest_sha256 is not None
        failed = self.failure_code is not None
        if self.state is CoordinatorWorkStateV1.QUEUED:
            if leased or returned or registered or failed:
                raise ValueError("queued work cannot carry attempt/result/failure state")
            return
        if self.state is CoordinatorWorkStateV1.LEASED:
            if not leased or returned or registered or failed:
                raise ValueError("leased work state is incomplete")
            return
        if self.state is CoordinatorWorkStateV1.COMPLETED_UNVERIFIED:
            if not leased or not returned or registered or failed:
                raise ValueError("completed-unverified work state is incomplete")
            return
        if self.state is CoordinatorWorkStateV1.REGISTERED:
            if not leased or not returned or not registered or failed:
                raise ValueError("registered work state is incomplete")
            return
        if self.state in {
            CoordinatorWorkStateV1.FAILED,
            CoordinatorWorkStateV1.CANCELLED,
        }:
            if returned or registered or not failed:
                raise ValueError("failed/cancelled work state has invalid result fields")
            if any(value is not None for value in (self.attempt_id, self.worker_id, self.lease_id)) and not leased:
                raise ValueError("partial failed/cancelled attempt binding is invalid")
            return
        if self.state is CoordinatorWorkStateV1.QUARANTINED:
            if not leased or not returned or registered or not failed:
                raise ValueError("quarantined work state is incomplete")
            return
        raise RuntimeError("coordinator work state is not exhaustively handled")

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.work_request_id, self.logical_work_unit_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "failure_code": self.failure_code,
            "lease_id": self.lease_id,
            "logical_work_unit_id": self.logical_work_unit_id,
            "registered_manifest_sha256": self.registered_manifest_sha256,
            "returned_result_sha256": self.returned_result_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "state": self.state.value,
            "work_request_id": self.work_request_id,
            "worker_id": self.worker_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> CoordinatorWorkStateRecordV1:
        row = _exact(
            value,
            {
                "attempt_id",
                "failure_code",
                "lease_id",
                "logical_work_unit_id",
                "registered_manifest_sha256",
                "returned_result_sha256",
                "schema_id",
                "schema_version",
                "state",
                "work_request_id",
                "worker_id",
            },
            "coordinator work state",
        )
        _schema(row, cls.schema_id, "coordinator work state")
        restored = cls(
            work_request_id=_text(row, "work_request_id"),
            logical_work_unit_id=_text(row, "logical_work_unit_id"),
            state=CoordinatorWorkStateV1(_text(row, "state")),
            attempt_id=_optional_text(row, "attempt_id"),
            worker_id=_optional_text(row, "worker_id"),
            lease_id=_optional_text(row, "lease_id"),
            returned_result_sha256=_optional_text(row, "returned_result_sha256"),
            registered_manifest_sha256=_optional_text(
                row,
                "registered_manifest_sha256",
            ),
            failure_code=_optional_text(row, "failure_code"),
        )
        _round_trip(restored, row, "coordinator work state")
        return restored


@dataclass(frozen=True, slots=True)
class CoordinatorStateSnapshotV1:
    plan_id: str
    revision: int
    previous_snapshot_sha256: str | None
    records: tuple[CoordinatorWorkStateRecordV1, ...]
    cancellation_sha256s: tuple[str, ...] = ()

    schema_id: ClassVar[str] = COORDINATOR_STATE_SNAPSHOT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_LEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.plan_id, "coordinator snapshot plan ID")
        _bounded_int(self.revision, "coordinator snapshot revision", 1, (1 << 63) - 1)
        if self.previous_snapshot_sha256 is not None:
            _sha256(
                self.previous_snapshot_sha256,
                "previous coordinator snapshot digest",
            )
        if (self.revision == 1) != (self.previous_snapshot_sha256 is None):
            raise ValueError("coordinator snapshot predecessor and revision disagree")
        if (
            type(self.records) is not tuple
            or not self.records
            or len(self.records) > MAX_COORDINATOR_STATE_RECORDS_V1
        ):
            raise ValueError("coordinator snapshot records require a nonempty bounded tuple")
        if any(type(item) is not CoordinatorWorkStateRecordV1 for item in self.records):
            raise TypeError("coordinator snapshot records must be typed")
        if self.records != tuple(sorted(self.records, key=lambda item: item.sort_key)):
            raise ValueError("coordinator snapshot records must use canonical order")
        work_ids = tuple(item.work_request_id for item in self.records)
        logical_ids = tuple(item.logical_work_unit_id for item in self.records)
        if len(work_ids) != len(set(work_ids)) or len(logical_ids) != len(set(logical_ids)):
            raise ValueError("coordinator snapshot cannot duplicate work identities")
        _canonical_digests(
            self.cancellation_sha256s,
            "coordinator snapshot cancellations",
        )

    @property
    def snapshot_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "cancellation_sha256s": list(self.cancellation_sha256s),
            "plan_id": self.plan_id,
            "previous_snapshot_sha256": self.previous_snapshot_sha256,
            "records": [item.as_dict() for item in self.records],
            "revision": self.revision,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> CoordinatorStateSnapshotV1:
        row = _exact(
            value,
            {
                "cancellation_sha256s",
                "plan_id",
                "previous_snapshot_sha256",
                "records",
                "revision",
                "schema_id",
                "schema_version",
            },
            "coordinator state snapshot",
        )
        _schema(row, cls.schema_id, "coordinator state snapshot")
        raw_records = row["records"]
        if type(raw_records) is not list:
            raise TypeError("coordinator snapshot records must be an array")
        restored = cls(
            plan_id=_text(row, "plan_id"),
            revision=_integer(row, "revision"),
            previous_snapshot_sha256=_optional_text(
                row,
                "previous_snapshot_sha256",
            ),
            records=tuple(
                CoordinatorWorkStateRecordV1.from_dict(item) for item in raw_records
            ),
            cancellation_sha256s=_digest_tuple(
                row["cancellation_sha256s"],
                "coordinator snapshot cancellations",
            ),
        )
        _round_trip(restored, row, "coordinator state snapshot")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> CoordinatorStateSnapshotV1:
        if type(raw) is not bytes or not raw or len(raw) > MAX_COORDINATOR_SNAPSHOT_BYTES_V1:
            raise ValueError("coordinator snapshot bytes exceed the bounded limit")
        restored = cls.from_dict(
            load_canonical_json_bytes(raw, "coordinator state snapshot")
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("coordinator snapshot bytes are not canonical")
        return restored


@dataclass(slots=True)
class _LiveLease:
    grant: LeaseGrantV1
    deadline_ns: int
    hard_deadline_ns: int
    heartbeat_sequence: int = 0
    heartbeat_nonces: set[str] | None = None

    def __post_init__(self) -> None:
        if self.heartbeat_nonces is None:
            self.heartbeat_nonces = set()


class LeaseBookV1:
    """Coordinator-authoritative monotonic lease and heartbeat enforcement."""

    __slots__ = ("_leases", "_lock", "_policy", "_sequence")

    def __init__(self, policy: LeasePolicyV1) -> None:
        if type(policy) is not LeasePolicyV1:
            raise TypeError("lease book requires LeasePolicyV1")
        self._policy = policy
        self._leases: dict[str, _LiveLease] = {}
        self._sequence = 0
        self._lock = threading.Lock()

    @property
    def policy(self) -> LeasePolicyV1:
        return self._policy

    def grant(
        self,
        *,
        plan_id: str,
        work_request_id: str,
        logical_work_unit_id: str,
        attempt_number: int,
        worker_id: str,
        session_id: str,
        issued_at_utc: str,
    ) -> LeaseGrantV1:
        issued = _utc(issued_at_utc, "lease issue time")
        with self._lock:
            self._sequence += 1
            grant = LeaseGrantV1(
                plan_id=plan_id,
                work_request_id=work_request_id,
                logical_work_unit_id=logical_work_unit_id,
                attempt_number=attempt_number,
                attempt_id=derive_attempt_id(logical_work_unit_id, attempt_number),
                worker_id=worker_id,
                session_id=session_id,
                lease_sequence=self._sequence,
                issued_at_utc=issued_at_utc,
                expires_at_utc=_format_utc(
                    issued + timedelta(seconds=self._policy.lease_seconds)
                ),
                grant_nonce=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            )
            if grant.lease_id in self._leases:
                raise RuntimeError("new lease identity collided with an active lease")
            now_ns = time.monotonic_ns()
            hard_deadline_ns = now_ns + self._policy.lease_seconds * 1_000_000_000
            self._leases[grant.lease_id] = _LiveLease(
                grant=grant,
                deadline_ns=min(
                    hard_deadline_ns,
                    now_ns
                    + self._policy.heartbeat_interval_seconds
                    * self._policy.maximum_missed_heartbeats
                    * 1_000_000_000,
                ),
                hard_deadline_ns=hard_deadline_ns,
            )
            return grant

    def heartbeat(self, heartbeat: LeaseHeartbeatV1) -> LeaseGrantV1:
        if type(heartbeat) is not LeaseHeartbeatV1:
            raise TypeError("lease heartbeat requires LeaseHeartbeatV1")
        with self._lock:
            live = self._leases.get(heartbeat.lease_id)
            if live is None:
                raise LeaseRefused(
                    LeaseRefusalCodeV1.UNKNOWN_LEASE,
                    "heartbeat names no active lease",
                )
            if time.monotonic_ns() >= live.deadline_ns:
                self._leases.pop(heartbeat.lease_id, None)
                raise LeaseRefused(
                    LeaseRefusalCodeV1.LEASE_EXPIRED,
                    "heartbeat arrived after coordinator lease expiry",
                )
            grant = live.grant
            if (
                heartbeat.attempt_id != grant.attempt_id
                or heartbeat.worker_id != grant.worker_id
                or heartbeat.session_id != grant.session_id
            ):
                raise LeaseRefused(
                    LeaseRefusalCodeV1.LEASE_BINDING_MISMATCH,
                    "heartbeat differs from its attempt, worker, or TLS session",
                )
            sent = _utc(heartbeat.sent_at_utc, "heartbeat send time")
            if not (
                _utc(grant.issued_at_utc, "lease issue time")
                <= sent
                <= _utc(grant.expires_at_utc, "lease expiry time")
            ):
                raise LeaseRefused(
                    LeaseRefusalCodeV1.LEASE_BINDING_MISMATCH,
                    "heartbeat time falls outside its granted lease",
                )
            nonces = live.heartbeat_nonces
            if nonces is None:
                raise RuntimeError("live lease lost its heartbeat nonce set")
            if heartbeat.heartbeat_nonce in nonces:
                raise LeaseRefused(
                    LeaseRefusalCodeV1.HEARTBEAT_REPLAYED,
                    "heartbeat nonce was already accepted",
                )
            expected = live.heartbeat_sequence + 1
            if heartbeat.heartbeat_sequence != expected:
                raise LeaseRefused(
                    LeaseRefusalCodeV1.HEARTBEAT_SEQUENCE_GAP,
                    "heartbeat sequence is not the next exact value",
                )
            nonces.add(heartbeat.heartbeat_nonce)
            live.heartbeat_sequence = heartbeat.heartbeat_sequence
            live.deadline_ns = min(
                live.hard_deadline_ns,
                time.monotonic_ns()
                + self._policy.heartbeat_interval_seconds
                * self._policy.maximum_missed_heartbeats
                * 1_000_000_000,
            )
            return grant

    def complete(self, lease_id: str) -> LeaseGrantV1:
        digest = _sha256(lease_id, "completed lease ID")
        with self._lock:
            live = self._leases.pop(digest, None)
            if live is None:
                raise LeaseRefused(
                    LeaseRefusalCodeV1.UNKNOWN_LEASE,
                    "completion names no active lease",
                )
            if time.monotonic_ns() >= live.deadline_ns:
                raise LeaseRefused(
                    LeaseRefusalCodeV1.LEASE_EXPIRED,
                    "completion arrived after coordinator lease expiry",
                )
            return live.grant

    def expire(self) -> tuple[LeaseGrantV1, ...]:
        now = time.monotonic_ns()
        with self._lock:
            expired = tuple(
                sorted(
                    (
                        live.grant
                        for live in self._leases.values()
                        if now >= live.deadline_ns
                    ),
                    key=lambda item: item.lease_id,
                )
            )
            for grant in expired:
                self._leases.pop(grant.lease_id, None)
            return expired


class CoordinatorStateStoreV1:
    """Atomic, chained current-state snapshot under governed checkpoints."""

    __slots__ = ("_paths",)

    def __init__(self, paths: DataPaths) -> None:
        if type(paths) is not DataPaths:
            raise TypeError("coordinator state store requires DataPaths")
        paths.validate()
        self._paths = paths

    def load(self, plan_id: str) -> CoordinatorStateSnapshotV1 | None:
        digest = _sha256(plan_id, "coordinator state plan ID")
        self._paths.validate(DataAreaId.CHECKPOINTS)
        area = self._paths.checkpoints
        if not area.exists():
            return None
        root_fd = _open_directory(area)
        try:
            try:
                state_fd = _open_directory_at(root_fd, _STATE_DIRECTORY)
            except FileNotFoundError:
                return None
            try:
                try:
                    descriptor = _open_regular_at(state_fd, f"{digest}.json")
                except FileNotFoundError:
                    return None
                try:
                    raw = _read_bounded(descriptor, MAX_COORDINATOR_SNAPSHOT_BYTES_V1)
                finally:
                    os.close(descriptor)
            finally:
                os.close(state_fd)
        finally:
            os.close(root_fd)
        snapshot = CoordinatorStateSnapshotV1.from_canonical_bytes(raw)
        if snapshot.plan_id != digest:
            raise ValueError("coordinator snapshot file is bound to another plan")
        return snapshot

    def save(self, snapshot: CoordinatorStateSnapshotV1) -> str:
        if type(snapshot) is not CoordinatorStateSnapshotV1:
            raise TypeError("coordinator state save requires typed snapshot")
        raw = snapshot.canonical_bytes()
        if len(raw) > MAX_COORDINATOR_SNAPSHOT_BYTES_V1:
            raise ValueError("coordinator snapshot exceeds its byte limit")
        self._paths.ensure(DataAreaId.CHECKPOINTS)
        self._paths.validate(DataAreaId.CHECKPOINTS)
        root_fd = _open_directory(self._paths.checkpoints)
        try:
            state_fd = _ensure_directory_at(root_fd, _STATE_DIRECTORY)
            try:
                with _StateLock(state_fd):
                    prior = self._load_at(state_fd, snapshot.plan_id)
                    if prior is None:
                        if snapshot.revision != 1:
                            raise ValueError("first coordinator snapshot must be revision one")
                    elif prior.plan_id != snapshot.plan_id:
                        raise ValueError("stored coordinator snapshot belongs to another plan")
                    elif (
                        snapshot.revision != prior.revision + 1
                        or snapshot.previous_snapshot_sha256 != prior.snapshot_sha256
                    ):
                        raise ValueError(
                            "coordinator snapshot does not extend the stored revision"
                        )
                    temporary = _write_temp(state_fd, raw)
                    try:
                        os.replace(
                            temporary,
                            f"{snapshot.plan_id}.json",
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
        return snapshot.snapshot_sha256

    @staticmethod
    def _load_at(state_fd: int, plan_id: str) -> CoordinatorStateSnapshotV1 | None:
        try:
            descriptor = _open_regular_at(state_fd, f"{plan_id}.json")
        except FileNotFoundError:
            return None
        try:
            raw = _read_bounded(descriptor, MAX_COORDINATOR_SNAPSHOT_BYTES_V1)
        finally:
            os.close(descriptor)
        return CoordinatorStateSnapshotV1.from_canonical_bytes(raw)


def derive_attempt_id(logical_work_unit_id: str, attempt_number: int) -> str:
    logical = _sha256(logical_work_unit_id, "attempt logical-work ID")
    number = _bounded_int(attempt_number, "attempt number", 1, (1 << 31) - 1)
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "attempt_number": number,
                "logical_work_unit_id": logical,
                "schema_id": WORK_ATTEMPT_IDENTITY_SCHEMA_ID,
                "schema_version": ORCHESTRATION_MODEL_SCHEMA_VERSION,
            }
        )
    ).hexdigest()


def _format_utc(value: datetime) -> str:
    canonical = value.astimezone(timezone.utc)
    if canonical.microsecond:
        return canonical.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return canonical.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(value: object, label: str) -> datetime:
    if type(value) is not str or _UTC.fullmatch(value) is None:
        raise ValueError(f"{label} must be canonical UTC text")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if _format_utc(parsed) != value:
        raise ValueError(f"{label} is not canonically formatted")
    return parsed


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
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


def _canonical_digests(values: object, label: str) -> None:
    if type(values) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    for value in values:
        _sha256(value, label)
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be sorted and unique")


def _exact(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise TypeError(f"serialized {label} must be one exact object")
    actual = set(value)
    if actual != fields:
        raise ValueError(
            f"serialized {label} fields differ: "
            f"missing={sorted(fields - actual)} unknown={sorted(actual - fields)}"
        )
    return value


def _schema(row: dict[str, object], schema_id: str, label: str) -> None:
    if (
        row["schema_id"] != schema_id
        or type(row["schema_version"]) is not int
        or row["schema_version"] != ORCHESTRATION_LEASE_SCHEMA_VERSION
    ):
        raise ValueError(f"serialized {label} schema differs from V1")


def _text(row: dict[str, object], key: str) -> str:
    value = row[key]
    if type(value) is not str:
        raise TypeError(f"serialized {key} must be exact text")
    return value


def _optional_text(row: dict[str, object], key: str) -> str | None:
    value = row[key]
    if value is not None and type(value) is not str:
        raise TypeError(f"serialized {key} must be text or null")
    return value


def _integer(row: dict[str, object], key: str) -> int:
    value = row[key]
    if type(value) is not int:
        raise TypeError(f"serialized {key} must be an exact integer")
    return value


def _digest_tuple(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise TypeError(f"serialized {label} must be an array of digests")
    return tuple(value)


def _round_trip(record: object, row: dict[str, object], label: str) -> None:
    as_dict = getattr(record, "as_dict", None)
    if not callable(as_dict) or as_dict() != row:
        raise ValueError(f"serialized {label} did not round-trip exactly")


def _directory_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("coordinator state requires no-follow directory handles")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory(path: Path) -> int:
    descriptor = os.open(path, _directory_flags())
    try:
        _safe_directory(os.fstat(descriptor))
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_directory_at(parent: int, name: str) -> int:
    _component(name)
    descriptor = os.open(name, _directory_flags(), dir_fd=parent)
    try:
        _safe_directory(os.fstat(descriptor))
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _ensure_directory_at(parent: int, name: str) -> int:
    _component(name)
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError:
        pass
    return _open_directory_at(parent, name)


def _open_regular_at(parent: int, name: str) -> int:
    _component(name)
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=parent)
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
        or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
    ):
        os.close(descriptor)
        raise PermissionError("coordinator state file is unsafe")
    return descriptor


def _safe_directory(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_mode & 0o022
        or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
    ):
        raise PermissionError("coordinator state directory is unsafe")


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    before = os.fstat(descriptor)
    if before.st_size <= 0 or before.st_size > maximum:
        raise ValueError("coordinator state file is empty or exceeds its limit")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, maximum - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise ValueError("coordinator state read exceeded its limit")
    after = os.fstat(descriptor)
    if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_dev,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RuntimeError("coordinator state changed during read")
    return b"".join(chunks)


def _write_temp(parent: int, raw: bytes) -> str:
    for _ in range(32):
        name = _TEMP_PREFIX + secrets.token_hex(16)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=parent)
        except FileExistsError:
            continue
        try:
            view = memoryview(raw)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset : offset + 1024 * 1024])
                if written <= 0:
                    raise OSError("coordinator state write made no progress")
                offset += written
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            os.unlink(name, dir_fd=parent)
            raise
        else:
            os.close(descriptor)
            return name
    raise RuntimeError("coordinator state could not allocate a temporary file")


def _component(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("coordinator state path component is invalid")
    return value


class _StateLock:
    def __init__(self, parent: int) -> None:
        self._parent = parent
        self._descriptor: int | None = None

    def __enter__(self) -> _StateLock:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(_LOCK_FILE, flags, 0o600, dir_fd=self._parent)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o022
            or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
        ):
            os.close(descriptor)
            raise PermissionError("coordinator state lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


__all__ = [
    "COORDINATOR_STATE_SNAPSHOT_SCHEMA_ID",
    "COORDINATOR_WORK_STATE_SCHEMA_ID",
    "LEASE_GRANT_SCHEMA_ID",
    "LEASE_HEARTBEAT_SCHEMA_ID",
    "LEASE_POLICY_SCHEMA_ID",
    "MAX_COORDINATOR_SNAPSHOT_BYTES_V1",
    "MAX_COORDINATOR_STATE_RECORDS_V1",
    "MAX_HEARTBEAT_INTERVAL_SECONDS_V1",
    "MAX_LEASE_SECONDS_V1",
    "MAX_MISSED_HEARTBEATS_V1",
    "ORCHESTRATION_LEASE_SCHEMA_VERSION",
    "CoordinatorStateSnapshotV1",
    "CoordinatorStateStoreV1",
    "CoordinatorWorkStateRecordV1",
    "CoordinatorWorkStateV1",
    "LeaseBookV1",
    "LeaseGrantV1",
    "LeaseHeartbeatV1",
    "LeasePolicyV1",
    "LeaseRefusalCodeV1",
    "LeaseRefused",
    "derive_attempt_id",
]
