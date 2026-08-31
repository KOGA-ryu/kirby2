"""Immutable data-only coordinator/worker protocol for WO38-B.

The same closed records cross the in-process and multiprocessing boundaries.  A
request contains scientific work and required audit identities only: worker,
attempt, lease, retry, heartbeat, and clock state belong to later coordinator
cards.  No protocol field accepts a command, Python source, module/import target,
filesystem path, pickle, or other dynamic-execution payload.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, TypeVar

from kirby2.immutable import freeze_json, thaw_json
from kirby2.orchestration.models import DigestReferenceV1, LogicalWorkUnit


ORCHESTRATION_PROTOCOL_SCHEMA_VERSION = 1
PROTOCOL_DIAGNOSTIC_SCHEMA_ID = "KIRBY2_PROTOCOL_DIAGNOSTIC_V1"
WORKER_COMPATIBILITY_SCHEMA_ID = "KIRBY2_WORKER_COMPATIBILITY_V1"
WORK_REQUEST_SCHEMA_ID = "KIRBY2_WORK_REQUEST_V1"
INLINE_ARTIFACT_SCHEMA_ID = "KIRBY2_INLINE_ARTIFACT_V1"
RUNTIME_AUDIT_RESULT_SCHEMA_ID = "KIRBY2_RUNTIME_AUDIT_RESULT_V1"
WORKER_RESULT_MANIFEST_SCHEMA_ID = "KIRBY2_WORKER_RESULT_MANIFEST_V1"
WORKER_RESULT_SCHEMA_ID = "KIRBY2_WORKER_RESULT_V1"
LAN_PROTOCOL_ENVELOPE_SCHEMA_ID = "KIRBY2_LAN_PROTOCOL_ENVELOPE_V1"

MAX_INLINE_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_DIAGNOSTIC_DETAILS_BYTES = 1024 * 1024
MAX_PROTOCOL_JSON_DEPTH = 64
MAX_PROTOCOL_JSON_ITEMS = 100_000
MAX_LAN_ENVELOPE_BYTES_V1 = 64 * 1024 * 1024
MAX_LAN_PAYLOAD_BYTES_V1 = 47 * 1024 * 1024
MAX_LAN_SESSION_SEQUENCE_V1 = (1 << 63) - 1

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DIAGNOSTIC_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_KEY_TEXT = re.compile(r"[^a-z0-9]+")
_EnumT = TypeVar("_EnumT", bound=Enum)

# These tokens are allowed only as inert identity metadata.  For example,
# ``source_version`` and ``module_sha256`` are data; ``source``, ``module_path``,
# ``run_command``, and ``pickle_payload`` are executable payload surfaces.
_EXECUTABLE_SURFACE_TOKENS = frozenset(
    {"command", "import", "module", "path", "pickle", "source"}
)
_INERT_IDENTITY_SUFFIXES = frozenset(
    {
        "digest",
        "digests",
        "format",
        "formats",
        "id",
        "ids",
        "identity",
        "identities",
        "sha256",
        "version",
        "versions",
    }
)
_WALL_CLOCK_KEYS = frozenset(
    {
        "wall_clock",
        "wall_clock_time",
        "wall_clock_utc",
        "wallclock",
        "wallclocktime",
        "wallclockutc",
    }
)


class InlineArtifactMediaTypeV1(str, Enum):
    """Closed canonical data encodings supported by the local V1 protocol."""

    CANONICAL_JSON = "application/json"
    CANONICAL_JSONL = "application/x-ndjson"


class RuntimeAuditStatusV1(str, Enum):
    """Closed terminal state of one required runtime audit."""

    PASSED = "PASSED"
    FAILED = "FAILED"


class WorkerResultStatusV1(str, Enum):
    """Closed terminal worker response states."""

    SUCCEEDED = "SUCCEEDED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    COMPATIBILITY_REFUSED = "COMPATIBILITY_REFUSED"
    WORK_KIND_REFUSED = "WORK_KIND_REFUSED"
    RUNTIME_AUDIT_FAILED = "RUNTIME_AUDIT_FAILED"


class LanMessageKindV1(str, Enum):
    """Closed data record kinds accepted by the authenticated LAN transport."""

    SESSION_HELLO = "SESSION_HELLO"
    WORKER_COMPATIBILITY = "WORKER_COMPATIBILITY"
    RESOURCE_ADVERTISEMENT = "RESOURCE_ADVERTISEMENT"
    RESOURCE_CLAIM = "RESOURCE_CLAIM"
    RESOURCE_DECISION = "RESOURCE_DECISION"
    LEASE_POLICY = "LEASE_POLICY"
    LEASE_GRANT = "LEASE_GRANT"
    LEASE_HEARTBEAT = "LEASE_HEARTBEAT"
    WORK_REQUEST = "WORK_REQUEST"
    WORK_RESULT = "WORK_RESULT"
    EXPERIMENT_CANCELLATION = "EXPERIMENT_CANCELLATION"
    ARTIFACT_ACCESS_SCOPE = "ARTIFACT_ACCESS_SCOPE"
    CONTENT_REQUEST = "CONTENT_REQUEST"
    SESSION_CLOSE = "SESSION_CLOSE"


@dataclass(frozen=True, slots=True)
class ProtocolDiagnosticV1:
    """Bounded structured diagnostic; never an exception or executable object."""

    code: str
    summary: str
    details: Mapping[str, object]

    schema_id: ClassVar[str] = PROTOCOL_DIAGNOSTIC_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_PROTOCOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.code) is not str or _DIAGNOSTIC_CODE.fullmatch(self.code) is None:
            raise ValueError("protocol diagnostic code must be uppercase canonical text")
        _bounded_text(self.summary, "protocol diagnostic summary", maximum_bytes=4096)
        frozen = _freeze_protocol_object(
            self.details,
            "protocol diagnostic details",
            maximum_bytes=MAX_DIAGNOSTIC_DETAILS_BYTES,
        )
        object.__setattr__(self, "details", frozen)

    def identity_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "details": _detached_object(self.details),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "summary": self.summary,
        }

    @property
    def diagnostic_sha256(self) -> str:
        return _canonical_sha256(self.identity_dict())

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.code, self.diagnostic_sha256)

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "diagnostic_sha256": self.diagnostic_sha256}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> ProtocolDiagnosticV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "code",
                    "details",
                    "diagnostic_sha256",
                    "schema_id",
                    "schema_version",
                    "summary",
                }
            ),
            "protocol diagnostic",
        )
        _require_schema(payload, cls.schema_id, "protocol diagnostic")
        declared = _require_sha256(
            payload["diagnostic_sha256"],
            "declared protocol diagnostic digest",
        )
        restored = cls(
            code=_exact_text(payload, "code"),
            summary=_exact_text(payload, "summary"),
            details=_exact_mapping(payload["details"], "protocol diagnostic details"),
        )
        if not hmac.compare_digest(declared, restored.diagnostic_sha256):
            raise ValueError("protocol diagnostic digest differs from exact content")
        _require_exact_round_trip(restored, payload, "protocol diagnostic")
        return restored


@dataclass(frozen=True, slots=True)
class WorkerCompatibilityV1:
    """Exact compatibility identity mirrored from ``LogicalWorkUnit``.

    ``engine_identity`` binds the implementation/source digest and
    ``runtime_identity`` binds the Python/runtime digest.  No display version is
    accepted as a compatibility substitute.
    """

    engine_identity: DigestReferenceV1
    runtime_identity: DigestReferenceV1
    dependency_identity: DigestReferenceV1
    compiler_identity: DigestReferenceV1
    schemas: tuple[DigestReferenceV1, ...]
    capabilities: tuple[DigestReferenceV1, ...]

    schema_id: ClassVar[str] = WORKER_COMPATIBILITY_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_PROTOCOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value, label in (
            (self.engine_identity, "worker engine/source identity"),
            (self.runtime_identity, "worker Python/runtime identity"),
            (self.dependency_identity, "worker dependency identity"),
            (self.compiler_identity, "worker compiler identity"),
        ):
            if type(value) is not DigestReferenceV1:
                raise TypeError(f"{label} must be DigestReferenceV1")
        _canonical_references(
            self.schemas,
            "worker schema identities",
            require_nonempty=True,
        )
        _canonical_references(
            self.capabilities,
            "worker capability identities",
            require_nonempty=True,
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "capabilities": [item.as_dict() for item in self.capabilities],
            "compiler_identity": self.compiler_identity.as_dict(),
            "dependency_identity": self.dependency_identity.as_dict(),
            "engine_identity": self.engine_identity.as_dict(),
            "runtime_identity": self.runtime_identity.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "schemas": [item.as_dict() for item in self.schemas],
        }

    @property
    def compatibility_sha256(self) -> str:
        return _canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_dict(),
            "compatibility_sha256": self.compatibility_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    def matches_logical_work_unit(self, logical_work_unit: LogicalWorkUnit) -> bool:
        if type(logical_work_unit) is not LogicalWorkUnit:
            raise TypeError("compatibility comparison requires LogicalWorkUnit")
        return self == type(self).from_logical_work_unit(logical_work_unit)

    @classmethod
    def from_logical_work_unit(
        cls,
        logical_work_unit: LogicalWorkUnit,
    ) -> WorkerCompatibilityV1:
        if type(logical_work_unit) is not LogicalWorkUnit:
            raise TypeError("compatibility construction requires LogicalWorkUnit")
        return cls(
            engine_identity=logical_work_unit.engine_identity,
            runtime_identity=logical_work_unit.runtime_identity,
            dependency_identity=logical_work_unit.dependency_identity,
            compiler_identity=logical_work_unit.compiler_identity,
            schemas=logical_work_unit.schemas,
            capabilities=logical_work_unit.capabilities,
        )

    @classmethod
    def from_dict(cls, value: object) -> WorkerCompatibilityV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "capabilities",
                    "compatibility_sha256",
                    "compiler_identity",
                    "dependency_identity",
                    "engine_identity",
                    "runtime_identity",
                    "schema_id",
                    "schema_version",
                    "schemas",
                }
            ),
            "worker compatibility",
        )
        _require_schema(payload, cls.schema_id, "worker compatibility")
        declared = _require_sha256(
            payload["compatibility_sha256"],
            "declared worker compatibility digest",
        )
        restored = cls(
            engine_identity=DigestReferenceV1.from_dict(payload["engine_identity"]),
            runtime_identity=DigestReferenceV1.from_dict(payload["runtime_identity"]),
            dependency_identity=DigestReferenceV1.from_dict(
                payload["dependency_identity"]
            ),
            compiler_identity=DigestReferenceV1.from_dict(
                payload["compiler_identity"]
            ),
            schemas=_references_from_dict(payload["schemas"], "worker schemas"),
            capabilities=_references_from_dict(
                payload["capabilities"],
                "worker capabilities",
            ),
        )
        if not hmac.compare_digest(declared, restored.compatibility_sha256):
            raise ValueError(
                "declared worker compatibility digest differs from exact identities"
            )
        _require_exact_round_trip(restored, payload, "worker compatibility")
        return restored


@dataclass(frozen=True, slots=True)
class WorkRequestV1:
    """Content-derived coordinator request with no operational lease state."""

    logical_work_unit: LogicalWorkUnit
    required_runtime_audits: tuple[DigestReferenceV1, ...]

    schema_id: ClassVar[str] = WORK_REQUEST_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_PROTOCOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.logical_work_unit) is not LogicalWorkUnit:
            raise TypeError("work request logical work must be LogicalWorkUnit")
        _canonical_references(
            self.required_runtime_audits,
            "required runtime audit identities",
            require_nonempty=True,
        )
        _reject_executable_payload_surfaces(
            self.logical_work_unit.configuration,
            "logical work configuration",
        )
        _reject_wall_clock_surfaces(
            self.logical_work_unit.configuration,
            "logical work configuration",
        )

    @property
    def required_compatibility(self) -> WorkerCompatibilityV1:
        return WorkerCompatibilityV1.from_logical_work_unit(self.logical_work_unit)

    def identity_dict(self) -> dict[str, object]:
        return {
            "logical_work_unit": self.logical_work_unit.as_dict(),
            "required_runtime_audits": [
                item.as_dict() for item in self.required_runtime_audits
            ],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @property
    def work_request_id(self) -> str:
        return _canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "work_request_id": self.work_request_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> WorkRequestV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "logical_work_unit",
                    "required_runtime_audits",
                    "schema_id",
                    "schema_version",
                    "work_request_id",
                }
            ),
            "work request",
        )
        _require_schema(payload, cls.schema_id, "work request")
        declared = _require_sha256(
            payload["work_request_id"],
            "declared work request ID",
        )
        restored = cls(
            logical_work_unit=LogicalWorkUnit.from_dict(
                payload["logical_work_unit"]
            ),
            required_runtime_audits=_references_from_dict(
                payload["required_runtime_audits"],
                "required runtime audits",
            ),
        )
        if not hmac.compare_digest(declared, restored.work_request_id):
            raise ValueError("declared work request ID differs from exact content")
        _require_exact_round_trip(restored, payload, "work request")
        return restored


@dataclass(frozen=True, slots=True)
class InlineArtifactV1:
    """One bounded canonical data artifact carried inline by a local worker."""

    artifact_id: str
    media_type: InlineArtifactMediaTypeV1
    payload_bytes: bytes

    schema_id: ClassVar[str] = INLINE_ARTIFACT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_PROTOCOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.artifact_id) is not str
            or _ARTIFACT_ID.fullmatch(self.artifact_id) is None
            or self.artifact_id in {".", ".."}
        ):
            raise ValueError(
                "inline artifact ID must be one non-path canonical data name"
            )
        if type(self.media_type) is not InlineArtifactMediaTypeV1:
            raise TypeError("inline artifact media type must be V1")
        raw = _require_exact_bytes(self.payload_bytes, "inline artifact payload")
        if not raw or len(raw) > MAX_INLINE_ARTIFACT_BYTES:
            raise ValueError("inline artifact must be nonempty and within its limit")
        if self.media_type is InlineArtifactMediaTypeV1.CANONICAL_JSON:
            value = _load_canonical_json_object(
                raw,
                "inline JSON artifact",
                maximum_bytes=MAX_INLINE_ARTIFACT_BYTES,
                reject_executable_surfaces=False,
            )
            _reject_wall_clock_surfaces(value, "inline JSON artifact")
        elif self.media_type is InlineArtifactMediaTypeV1.CANONICAL_JSONL:
            _load_canonical_json_lines(raw, "inline JSONL artifact")
        else:
            raise RuntimeError("inline artifact media type is not exhaustively handled")

    @property
    def byte_count(self) -> int:
        return len(self.payload_bytes)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload_bytes).hexdigest()

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.artifact_id, self.sha256)

    @property
    def digest_reference(self) -> DigestReferenceV1:
        return DigestReferenceV1(name=self.artifact_id, sha256=self.sha256)

    def descriptor_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "byte_count": self.byte_count,
            "media_type": self.media_type.value,
            "sha256": self.sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.descriptor_dict(),
            "payload_base64": base64.b64encode(self.payload_bytes).decode("ascii"),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_object(
        cls,
        artifact_id: str,
        value: Mapping[str, object],
    ) -> InlineArtifactV1:
        if not isinstance(value, Mapping):
            raise TypeError("inline JSON artifact source must be an object")
        detached = thaw_json(freeze_json(value))
        if type(detached) is not dict:
            raise RuntimeError("inline JSON artifact lost its object shape")
        _reject_wall_clock_surfaces(detached, "inline JSON artifact")
        return cls(
            artifact_id=artifact_id,
            media_type=InlineArtifactMediaTypeV1.CANONICAL_JSON,
            payload_bytes=_canonical_json_bytes(detached),
        )

    @classmethod
    def from_json_lines(
        cls,
        artifact_id: str,
        values: Iterable[Mapping[str, object]],
    ) -> InlineArtifactV1:
        if isinstance(values, (str, bytes, bytearray, Mapping)):
            raise TypeError("inline JSONL source must be an iterable of objects")
        rows: list[bytes] = []
        for index, value in enumerate(values):
            if not isinstance(value, Mapping):
                raise TypeError(f"inline JSONL row {index} must be an object")
            detached = thaw_json(freeze_json(value))
            if type(detached) is not dict:
                raise RuntimeError("inline JSONL row lost its object shape")
            _reject_wall_clock_surfaces(detached, f"inline JSONL row {index}")
            rows.append(_canonical_json_bytes(detached))
        if not rows:
            raise ValueError("inline JSONL artifact must contain at least one row")
        return cls(
            artifact_id=artifact_id,
            media_type=InlineArtifactMediaTypeV1.CANONICAL_JSONL,
            payload_bytes=b"\n".join(rows) + b"\n",
        )

    @classmethod
    def from_dict(cls, value: object) -> InlineArtifactV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "artifact_id",
                    "byte_count",
                    "media_type",
                    "payload_base64",
                    "schema_id",
                    "schema_version",
                    "sha256",
                }
            ),
            "inline artifact",
        )
        _require_schema(payload, cls.schema_id, "inline artifact")
        declared_count = _exact_integer(payload, "byte_count")
        if declared_count <= 0 or declared_count > MAX_INLINE_ARTIFACT_BYTES:
            raise ValueError("declared inline artifact byte count is outside limits")
        declared_digest = _require_sha256(
            payload["sha256"],
            "declared inline artifact digest",
        )
        raw = _strict_base64(
            _exact_text(payload, "payload_base64"),
            "inline artifact payload",
            maximum_bytes=MAX_INLINE_ARTIFACT_BYTES,
        )
        restored = cls(
            artifact_id=_exact_text(payload, "artifact_id"),
            media_type=_enum_value(
                InlineArtifactMediaTypeV1,
                payload["media_type"],
                "inline artifact media type",
            ),
            payload_bytes=raw,
        )
        if declared_count != restored.byte_count:
            raise ValueError("declared inline artifact byte count differs from bytes")
        if not hmac.compare_digest(declared_digest, restored.sha256):
            raise ValueError("declared inline artifact digest differs from bytes")
        _require_exact_round_trip(restored, payload, "inline artifact")
        return restored


@dataclass(frozen=True, slots=True)
class RuntimeAuditResultV1:
    """Typed result and digest evidence for one coordinator-required audit."""

    audit_identity: DigestReferenceV1
    status: RuntimeAuditStatusV1
    evidence_digests: tuple[DigestReferenceV1, ...]
    diagnostics: tuple[ProtocolDiagnosticV1, ...]

    schema_id: ClassVar[str] = RUNTIME_AUDIT_RESULT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_PROTOCOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.audit_identity) is not DigestReferenceV1:
            raise TypeError("runtime audit identity must be DigestReferenceV1")
        if type(self.status) is not RuntimeAuditStatusV1:
            raise TypeError("runtime audit status must be RuntimeAuditStatusV1")
        _canonical_references(
            self.evidence_digests,
            "runtime audit evidence digests",
            require_nonempty=True,
        )
        _canonical_diagnostics(
            self.diagnostics,
            "runtime audit diagnostics",
            require_nonempty=self.status is RuntimeAuditStatusV1.FAILED,
        )
        if self.status is RuntimeAuditStatusV1.PASSED and self.diagnostics:
            raise ValueError("passed runtime audit cannot carry diagnostics")

    def identity_dict(self) -> dict[str, object]:
        """Scientific audit result; explanatory diagnostics are excluded."""

        return {
            "audit_identity": self.audit_identity.as_dict(),
            "evidence_digests": [
                item.as_dict() for item in self.evidence_digests
            ],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
        }

    @property
    def runtime_audit_result_sha256(self) -> str:
        return _canonical_sha256(self.identity_dict())

    @property
    def result_reference(self) -> DigestReferenceV1:
        return DigestReferenceV1(
            name=self.audit_identity.name,
            sha256=self.runtime_audit_result_sha256,
        )

    def record_dict(self) -> dict[str, object]:
        return {
            **self.identity_dict(),
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "runtime_audit_result_sha256": self.runtime_audit_result_sha256,
        }

    @property
    def record_sha256(self) -> str:
        return _canonical_sha256(self.record_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.record_dict(), "record_sha256": self.record_sha256}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> RuntimeAuditResultV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "audit_identity",
                    "diagnostics",
                    "evidence_digests",
                    "record_sha256",
                    "runtime_audit_result_sha256",
                    "schema_id",
                    "schema_version",
                    "status",
                }
            ),
            "runtime audit result",
        )
        _require_schema(payload, cls.schema_id, "runtime audit result")
        declared_result = _require_sha256(
            payload["runtime_audit_result_sha256"],
            "declared runtime audit result digest",
        )
        declared_record = _require_sha256(
            payload["record_sha256"],
            "declared runtime audit record digest",
        )
        restored = cls(
            audit_identity=DigestReferenceV1.from_dict(payload["audit_identity"]),
            status=_enum_value(
                RuntimeAuditStatusV1,
                payload["status"],
                "runtime audit status",
            ),
            evidence_digests=_references_from_dict(
                payload["evidence_digests"],
                "runtime audit evidence digests",
            ),
            diagnostics=_diagnostics_from_dict(
                payload["diagnostics"],
                "runtime audit diagnostics",
            ),
        )
        if not hmac.compare_digest(
            declared_result,
            restored.runtime_audit_result_sha256,
        ):
            raise ValueError("runtime audit result digest differs from exact evidence")
        if not hmac.compare_digest(declared_record, restored.record_sha256):
            raise ValueError("runtime audit record digest differs from exact record")
        _require_exact_round_trip(restored, payload, "runtime audit result")
        return restored


@dataclass(frozen=True, slots=True)
class WorkerResultManifestV1:
    """Canonical success manifest independent of workers, attempts, and clocks."""

    work_request_id: str
    logical_work_unit_id: str
    worker_compatibility_sha256: str
    artifacts: tuple[DigestReferenceV1, ...]
    runtime_audit_results: tuple[DigestReferenceV1, ...]

    schema_id: ClassVar[str] = WORKER_RESULT_MANIFEST_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_PROTOCOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_sha256(self.work_request_id, "result-manifest work request ID")
        _require_sha256(self.logical_work_unit_id, "result-manifest logical work ID")
        _require_sha256(
            self.worker_compatibility_sha256,
            "result-manifest compatibility digest",
        )
        _canonical_references(
            self.artifacts,
            "result-manifest artifacts",
            require_nonempty=True,
        )
        _canonical_references(
            self.runtime_audit_results,
            "result-manifest runtime audits",
            require_nonempty=True,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifacts": [item.as_dict() for item in self.artifacts],
            "logical_work_unit_id": self.logical_work_unit_id,
            "runtime_audit_results": [
                item.as_dict() for item in self.runtime_audit_results
            ],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "work_request_id": self.work_request_id,
            "worker_compatibility_sha256": self.worker_compatibility_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def for_success(
        cls,
        *,
        request: WorkRequestV1,
        worker_compatibility: WorkerCompatibilityV1,
        artifacts: tuple[InlineArtifactV1, ...],
        runtime_audit_results: tuple[RuntimeAuditResultV1, ...],
    ) -> WorkerResultManifestV1:
        if type(request) is not WorkRequestV1:
            raise TypeError("result manifest construction requires WorkRequestV1")
        if type(worker_compatibility) is not WorkerCompatibilityV1:
            raise TypeError(
                "result manifest construction requires WorkerCompatibilityV1"
            )
        _canonical_artifacts(artifacts, require_nonempty=True)
        _canonical_audit_results(runtime_audit_results, require_nonempty=True)
        return cls(
            work_request_id=request.work_request_id,
            logical_work_unit_id=request.logical_work_unit.logical_work_unit_id,
            worker_compatibility_sha256=worker_compatibility.compatibility_sha256,
            artifacts=tuple(item.digest_reference for item in artifacts),
            runtime_audit_results=tuple(
                item.result_reference for item in runtime_audit_results
            ),
        )

    @classmethod
    def from_dict(cls, value: object) -> WorkerResultManifestV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "artifacts",
                    "logical_work_unit_id",
                    "runtime_audit_results",
                    "schema_id",
                    "schema_version",
                    "work_request_id",
                    "worker_compatibility_sha256",
                }
            ),
            "worker result manifest",
        )
        _require_schema(payload, cls.schema_id, "worker result manifest")
        restored = cls(
            work_request_id=_exact_text(payload, "work_request_id"),
            logical_work_unit_id=_exact_text(payload, "logical_work_unit_id"),
            worker_compatibility_sha256=_exact_text(
                payload,
                "worker_compatibility_sha256",
            ),
            artifacts=_references_from_dict(
                payload["artifacts"],
                "result-manifest artifacts",
            ),
            runtime_audit_results=_references_from_dict(
                payload["runtime_audit_results"],
                "result-manifest runtime audits",
            ),
        )
        _require_exact_round_trip(restored, payload, "worker result manifest")
        return restored

    @classmethod
    def from_bytes(cls, raw: object) -> WorkerResultManifestV1:
        payload = _load_canonical_json_object(
            raw,
            "worker result manifest bytes",
            maximum_bytes=MAX_DIAGNOSTIC_DETAILS_BYTES,
        )
        restored = cls.from_dict(payload)
        if restored.canonical_bytes() != raw:
            raise ValueError("worker result manifest bytes are not canonical")
        return restored


@dataclass(frozen=True, slots=True)
class WorkerResultV1:
    """One immutable data-only worker response.

    Only ``SUCCEEDED`` carries a manifest and inline artifacts.  Refusal and
    failure states carry structured diagnostics, never partially registerable
    result bytes.
    """

    request: WorkRequestV1
    worker_compatibility: WorkerCompatibilityV1
    status: WorkerResultStatusV1
    manifest: WorkerResultManifestV1 | None
    artifacts: tuple[InlineArtifactV1, ...]
    runtime_audit_results: tuple[RuntimeAuditResultV1, ...]
    diagnostics: tuple[ProtocolDiagnosticV1, ...]

    schema_id: ClassVar[str] = WORKER_RESULT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_PROTOCOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.request) is not WorkRequestV1:
            raise TypeError("worker result request must be WorkRequestV1")
        if type(self.worker_compatibility) is not WorkerCompatibilityV1:
            raise TypeError(
                "worker result compatibility must be WorkerCompatibilityV1"
            )
        if type(self.status) is not WorkerResultStatusV1:
            raise TypeError("worker result status must be WorkerResultStatusV1")
        _canonical_artifacts(
            self.artifacts,
            require_nonempty=self.status is WorkerResultStatusV1.SUCCEEDED,
        )
        _canonical_audit_results(
            self.runtime_audit_results,
            require_nonempty=self.status
            in {
                WorkerResultStatusV1.SUCCEEDED,
                WorkerResultStatusV1.RUNTIME_AUDIT_FAILED,
            },
        )
        _canonical_diagnostics(
            self.diagnostics,
            "worker result diagnostics",
            require_nonempty=self.status is not WorkerResultStatusV1.SUCCEEDED,
        )
        if self.status is WorkerResultStatusV1.SUCCEEDED and self.diagnostics:
            raise ValueError("successful worker result cannot carry diagnostics")

        compatibility_matches = self.worker_compatibility.matches_logical_work_unit(
            self.request.logical_work_unit
        )
        if self.status is WorkerResultStatusV1.COMPATIBILITY_REFUSED:
            if compatibility_matches:
                raise ValueError(
                    "compatibility refusal requires a differing exact identity"
                )
        elif not compatibility_matches:
            raise ValueError(
                "non-compatibility-refusal result requires the exact environment"
            )

        if self.status is WorkerResultStatusV1.SUCCEEDED:
            if type(self.manifest) is not WorkerResultManifestV1:
                raise TypeError("successful worker result requires a typed manifest")
            self._require_complete_runtime_audits(require_all_passed=True)
            expected = WorkerResultManifestV1.for_success(
                request=self.request,
                worker_compatibility=self.worker_compatibility,
                artifacts=self.artifacts,
                runtime_audit_results=self.runtime_audit_results,
            )
            if self.manifest != expected:
                raise ValueError("worker success manifest differs from returned content")
            return

        if self.manifest is not None or self.artifacts:
            raise ValueError("worker refusal/failure cannot carry result artifacts")
        if self.status is WorkerResultStatusV1.RUNTIME_AUDIT_FAILED:
            self._require_complete_runtime_audits(require_all_passed=False)
            if all(
                item.status is RuntimeAuditStatusV1.PASSED
                for item in self.runtime_audit_results
            ):
                raise ValueError("audit-failed result requires at least one failed audit")
        elif self.runtime_audit_results:
            raise ValueError("pre-audit refusal/failure cannot claim runtime audit results")

    def _require_complete_runtime_audits(self, *, require_all_passed: bool) -> None:
        required = self.request.required_runtime_audits
        observed = tuple(item.audit_identity for item in self.runtime_audit_results)
        if observed != required:
            raise ValueError("worker runtime audit results differ from required audits")
        if require_all_passed and any(
            item.status is not RuntimeAuditStatusV1.PASSED
            for item in self.runtime_audit_results
        ):
            raise ValueError("successful worker result requires every audit to pass")

    @property
    def manifest_bytes(self) -> bytes | None:
        if self.manifest is None:
            return None
        return self.manifest.canonical_bytes()

    @property
    def scientific_result_sha256(self) -> str | None:
        """Successful result identity, free of diagnostics and operational clocks."""

        if self.manifest is None:
            return None
        return self.manifest.manifest_sha256

    def record_dict(self) -> dict[str, object]:
        return {
            "artifacts": [item.as_dict() for item in self.artifacts],
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "manifest": None if self.manifest is None else self.manifest.as_dict(),
            "request": self.request.as_dict(),
            "runtime_audit_results": [
                item.as_dict() for item in self.runtime_audit_results
            ],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "scientific_result_sha256": self.scientific_result_sha256,
            "status": self.status.value,
            "worker_compatibility": self.worker_compatibility.as_dict(),
        }

    @property
    def record_sha256(self) -> str:
        return _canonical_sha256(self.record_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.record_dict(), "record_sha256": self.record_sha256}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> WorkerResultV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "artifacts",
                    "diagnostics",
                    "manifest",
                    "record_sha256",
                    "request",
                    "runtime_audit_results",
                    "schema_id",
                    "schema_version",
                    "scientific_result_sha256",
                    "status",
                    "worker_compatibility",
                }
            ),
            "worker result",
        )
        _require_schema(payload, cls.schema_id, "worker result")
        declared_record = _require_sha256(
            payload["record_sha256"],
            "declared worker result record digest",
        )
        declared_scientific = _optional_sha256(
            payload["scientific_result_sha256"],
            "declared scientific result digest",
        )
        manifest_payload = payload["manifest"]
        restored = cls(
            request=WorkRequestV1.from_dict(payload["request"]),
            worker_compatibility=WorkerCompatibilityV1.from_dict(
                payload["worker_compatibility"]
            ),
            status=_enum_value(
                WorkerResultStatusV1,
                payload["status"],
                "worker result status",
            ),
            manifest=(
                None
                if manifest_payload is None
                else WorkerResultManifestV1.from_dict(manifest_payload)
            ),
            artifacts=tuple(
                InlineArtifactV1.from_dict(item)
                for item in _exact_array(payload["artifacts"], "worker artifacts")
            ),
            runtime_audit_results=tuple(
                RuntimeAuditResultV1.from_dict(item)
                for item in _exact_array(
                    payload["runtime_audit_results"],
                    "worker runtime audit results",
                )
            ),
            diagnostics=_diagnostics_from_dict(
                payload["diagnostics"],
                "worker result diagnostics",
            ),
        )
        if declared_scientific != restored.scientific_result_sha256:
            raise ValueError("declared scientific result digest differs from manifest")
        if not hmac.compare_digest(declared_record, restored.record_sha256):
            raise ValueError("declared worker result digest differs from exact record")
        _require_exact_round_trip(restored, payload, "worker result")
        return restored


@dataclass(frozen=True, slots=True)
class LanProtocolEnvelopeV1:
    """Canonical authenticated-LAN message without executable dispatch data.

    The bootstrap hello is the sole message without a session identity and uses
    sequence zero.  Every later message carries the authenticated session digest,
    a strictly positive sequence, and a unique correlation nonce.  TLS supplies
    confidentiality and authentication; these fields provide protocol binding and
    replay detection rather than a second, invented cryptographic construction.
    """

    message_kind: LanMessageKindV1
    session_id: str | None
    sequence: int
    nonce: str
    payload_bytes: bytes

    schema_id: ClassVar[str] = LAN_PROTOCOL_ENVELOPE_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_PROTOCOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.message_kind) is not LanMessageKindV1:
            raise TypeError("LAN message kind must be LanMessageKindV1")
        if type(self.sequence) is not int:
            raise TypeError("LAN message sequence must be an exact integer")
        bootstrap = self.message_kind is LanMessageKindV1.SESSION_HELLO
        if bootstrap:
            if self.session_id is not None or self.sequence != 0:
                raise ValueError("LAN session hello must use null session and sequence zero")
        else:
            _require_sha256(self.session_id, "LAN envelope session ID")
            if not 1 <= self.sequence <= MAX_LAN_SESSION_SEQUENCE_V1:
                raise ValueError("LAN message sequence is outside the V1 range")
        _require_sha256(self.nonce, "LAN envelope nonce")
        raw = _require_exact_bytes(self.payload_bytes, "LAN envelope payload")
        if not raw or len(raw) > MAX_LAN_PAYLOAD_BYTES_V1:
            raise ValueError("LAN envelope payload is empty or exceeds its V1 limit")
        _load_canonical_json_object(
            raw,
            "LAN envelope payload",
            maximum_bytes=MAX_LAN_PAYLOAD_BYTES_V1,
        )

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.payload_bytes).hexdigest()

    def identity_dict(self) -> dict[str, object]:
        return {
            "message_kind": self.message_kind.value,
            "nonce": self.nonce,
            "payload_base64": base64.b64encode(self.payload_bytes).decode("ascii"),
            "payload_sha256": self.payload_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "session_id": self.session_id,
        }

    @property
    def envelope_sha256(self) -> str:
        return _canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "envelope_sha256": self.envelope_sha256}

    def canonical_bytes(self) -> bytes:
        raw = _canonical_json_bytes(self.as_dict())
        if len(raw) > MAX_LAN_ENVELOPE_BYTES_V1:
            raise ValueError("LAN envelope exceeds its V1 encoded byte limit")
        return raw

    @classmethod
    def from_dict(cls, value: object) -> LanProtocolEnvelopeV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "envelope_sha256",
                    "message_kind",
                    "nonce",
                    "payload_base64",
                    "payload_sha256",
                    "schema_id",
                    "schema_version",
                    "sequence",
                    "session_id",
                }
            ),
            "LAN protocol envelope",
        )
        _require_schema(payload, cls.schema_id, "LAN protocol envelope")
        declared_envelope = _require_sha256(
            payload["envelope_sha256"],
            "declared LAN envelope digest",
        )
        declared_payload = _require_sha256(
            payload["payload_sha256"],
            "declared LAN payload digest",
        )
        session_id = payload["session_id"]
        if session_id is not None and type(session_id) is not str:
            raise TypeError("serialized LAN session ID must be exact text or null")
        restored = cls(
            message_kind=_enum_value(
                LanMessageKindV1,
                payload["message_kind"],
                "LAN message kind",
            ),
            session_id=session_id,
            sequence=_exact_integer(payload, "sequence"),
            nonce=_exact_text(payload, "nonce"),
            payload_bytes=_strict_base64(
                _exact_text(payload, "payload_base64"),
                "LAN envelope payload",
                maximum_bytes=MAX_LAN_PAYLOAD_BYTES_V1,
            ),
        )
        if not hmac.compare_digest(declared_payload, restored.payload_sha256):
            raise ValueError("declared LAN payload digest differs from exact bytes")
        if not hmac.compare_digest(declared_envelope, restored.envelope_sha256):
            raise ValueError("declared LAN envelope digest differs from exact content")
        _require_exact_round_trip(restored, payload, "LAN protocol envelope")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: object) -> LanProtocolEnvelopeV1:
        payload = _load_canonical_json_object(
            raw,
            "LAN protocol envelope bytes",
            maximum_bytes=MAX_LAN_ENVELOPE_BYTES_V1,
        )
        restored = cls.from_dict(payload)
        if restored.canonical_bytes() != raw:
            raise ValueError("LAN protocol envelope bytes are not canonical")
        return restored


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        raise ValueError("orchestration protocol record is not canonical JSON") from error


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _optional_sha256(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _require_sha256(value, label)


def _bounded_text(value: object, label: str, *, maximum_bytes: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty text without edge whitespace")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{label} must be NFC-normalized")
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise ValueError(f"{label} contains a forbidden control/code point")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds its canonical byte limit")
    return value


def _require_exact_bytes(value: object, label: str) -> bytes:
    if type(value) is not bytes:
        raise TypeError(f"{label} must be immutable bytes")
    return value


def _exact_object(
    value: object,
    expected: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"serialized {label} must be an exact object")
    if any(type(key) is not str for key in value):
        raise TypeError(f"serialized {label} field names must be exact text")
    actual = frozenset(value)
    if actual != expected:
        raise ValueError(
            f"serialized {label} fields differ: "
            f"missing={sorted(expected - actual)} unknown={sorted(actual - expected)}"
        )
    return value


def _exact_array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"serialized {label} must be an exact array")
    return value


def _exact_mapping(value: object, label: str) -> Mapping[str, object]:
    if type(value) is not dict:
        raise TypeError(f"serialized {label} must be an exact object")
    return value


def _exact_text(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TypeError(f"serialized {key} must be exact text")
    return value


def _exact_integer(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"serialized {key} must be an exact integer")
    return value


def _enum_value(
    enum_type: type[_EnumT],
    value: object,
    label: str,
) -> _EnumT:
    if type(value) is not str:
        raise TypeError(f"serialized {label} must be exact text")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"serialized {label} is unsupported") from error


def _require_schema(
    payload: Mapping[str, object],
    schema_id: str,
    label: str,
) -> None:
    if (
        type(payload["schema_id"]) is not str
        or payload["schema_id"] != schema_id
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != ORCHESTRATION_PROTOCOL_SCHEMA_VERSION
    ):
        raise ValueError(f"serialized {label} schema differs from the V1 contract")


def _canonical_references(
    values: tuple[DigestReferenceV1, ...],
    label: str,
    *,
    require_nonempty: bool,
) -> None:
    if type(values) is not tuple or any(
        type(item) is not DigestReferenceV1 for item in values
    ):
        raise TypeError(f"{label} must be an immutable DigestReferenceV1 tuple")
    if require_nonempty and not values:
        raise ValueError(f"{label} cannot be empty")
    if values != tuple(sorted(values, key=lambda item: item.sort_key)):
        raise ValueError(f"{label} must use canonical name/digest order")
    names = tuple(item.name for item in values)
    if len(names) != len(set(names)):
        raise ValueError(f"{label} cannot contain duplicate names")


def _references_from_dict(
    value: object,
    label: str,
) -> tuple[DigestReferenceV1, ...]:
    return tuple(
        DigestReferenceV1.from_dict(item) for item in _exact_array(value, label)
    )


def _canonical_diagnostics(
    values: tuple[ProtocolDiagnosticV1, ...],
    label: str,
    *,
    require_nonempty: bool,
) -> None:
    if type(values) is not tuple or any(
        type(item) is not ProtocolDiagnosticV1 for item in values
    ):
        raise TypeError(f"{label} must be an immutable ProtocolDiagnosticV1 tuple")
    if require_nonempty and not values:
        raise ValueError(f"{label} cannot be empty")
    if values != tuple(sorted(values, key=lambda item: item.sort_key)):
        raise ValueError(f"{label} must use canonical code/digest order")
    codes = tuple(item.code for item in values)
    if len(codes) != len(set(codes)):
        raise ValueError(f"{label} cannot contain duplicate codes")


def _diagnostics_from_dict(
    value: object,
    label: str,
) -> tuple[ProtocolDiagnosticV1, ...]:
    return tuple(
        ProtocolDiagnosticV1.from_dict(item)
        for item in _exact_array(value, label)
    )


def _canonical_artifacts(
    values: tuple[InlineArtifactV1, ...],
    *,
    require_nonempty: bool,
) -> None:
    if type(values) is not tuple or any(
        type(item) is not InlineArtifactV1 for item in values
    ):
        raise TypeError("worker artifacts must be an immutable InlineArtifactV1 tuple")
    if require_nonempty and not values:
        raise ValueError("worker artifacts cannot be empty")
    if values != tuple(sorted(values, key=lambda item: item.sort_key)):
        raise ValueError("worker artifacts must use canonical artifact-ID order")
    names = tuple(item.artifact_id for item in values)
    if len(names) != len(set(names)):
        raise ValueError("worker artifacts cannot contain duplicate artifact IDs")


def _canonical_audit_results(
    values: tuple[RuntimeAuditResultV1, ...],
    *,
    require_nonempty: bool,
) -> None:
    if type(values) is not tuple or any(
        type(item) is not RuntimeAuditResultV1 for item in values
    ):
        raise TypeError(
            "runtime audit results must be an immutable RuntimeAuditResultV1 tuple"
        )
    if require_nonempty and not values:
        raise ValueError("runtime audit results cannot be empty")
    expected = tuple(sorted(values, key=lambda item: item.audit_identity.sort_key))
    if values != expected:
        raise ValueError("runtime audit results must use canonical audit-ID order")
    names = tuple(item.audit_identity.name for item in values)
    if len(names) != len(set(names)):
        raise ValueError("runtime audit results cannot contain duplicate audit names")


def _normalize_key(value: str) -> str:
    expanded = _CAMEL_CASE_BOUNDARY.sub("_", value)
    return _NON_KEY_TEXT.sub("_", expanded.casefold()).strip("_")


def _reject_executable_payload_surfaces(value: object, label: str) -> None:
    _walk_protocol_json(
        value,
        label,
        reject_executable_surfaces=True,
        reject_wall_clock=False,
    )


def _reject_wall_clock_surfaces(value: object, label: str) -> None:
    _walk_protocol_json(
        value,
        label,
        reject_executable_surfaces=False,
        reject_wall_clock=True,
    )


def _walk_protocol_json(
    value: object,
    label: str,
    *,
    reject_executable_surfaces: bool,
    reject_wall_clock: bool,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    if counter is None:
        counter = [0]
    if depth > MAX_PROTOCOL_JSON_DEPTH:
        raise ValueError(f"{label} exceeds the protocol nesting limit")
    counter[0] += 1
    if counter[0] > MAX_PROTOCOL_JSON_ITEMS:
        raise ValueError(f"{label} exceeds the protocol item limit")
    if value is None or type(value) in {bool, int, str}:
        if type(value) is str:
            if value != unicodedata.normalize("NFC", value):
                raise ValueError(f"{label} text must be NFC-normalized")
            if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
                raise ValueError(f"{label} contains an invalid Unicode code point")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} float must be finite")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str or not key:
                raise TypeError(f"{label} object keys must be nonempty exact text")
            if key != unicodedata.normalize("NFC", key):
                raise ValueError(f"{label} object keys must be NFC-normalized")
            normalized = _normalize_key(key)
            tokens = tuple(part for part in normalized.split("_") if part)
            if reject_executable_surfaces and (
                _EXECUTABLE_SURFACE_TOKENS.intersection(tokens)
                and (not tokens or tokens[-1] not in _INERT_IDENTITY_SUFFIXES)
            ):
                raise ValueError(
                    f"{label} contains forbidden executable payload field {key!r}"
                )
            if reject_wall_clock and (
                normalized in _WALL_CLOCK_KEYS
                or "wallclock" in normalized.replace("_", "")
            ):
                raise ValueError(
                    f"{label} contains operational wall-clock field {key!r}"
                )
            _walk_protocol_json(
                item,
                f"{label}.{key}",
                reject_executable_surfaces=reject_executable_surfaces,
                reject_wall_clock=reject_wall_clock,
                depth=depth + 1,
                counter=counter,
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _walk_protocol_json(
                item,
                f"{label}[{index}]",
                reject_executable_surfaces=reject_executable_surfaces,
                reject_wall_clock=reject_wall_clock,
                depth=depth + 1,
                counter=counter,
            )
        return
    raise TypeError(f"{label} contains unsupported value {type(value).__name__}")


def _freeze_protocol_object(
    value: object,
    label: str,
    *,
    maximum_bytes: int,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    _reject_executable_payload_surfaces(value, label)
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise RuntimeError(f"{label} lost its object shape during freezing")
    if len(_canonical_json_bytes(_detached_object(frozen))) > maximum_bytes:
        raise ValueError(f"{label} exceeds its protocol byte limit")
    return frozen


def _detached_object(value: Mapping[str, object]) -> dict[str, object]:
    detached = thaw_json(value)
    if type(detached) is not dict:
        raise RuntimeError("immutable protocol object lost its mapping shape")
    return detached


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate canonical JSON key {key!r}")
        result[key] = value
    return result


def _load_canonical_json_object(
    raw: object,
    label: str,
    *,
    maximum_bytes: int,
    reject_executable_surfaces: bool = True,
) -> dict[str, object]:
    payload = _require_exact_bytes(raw, label)
    if not payload or len(payload) > maximum_bytes:
        raise ValueError(f"{label} must be nonempty and within its byte limit")
    try:
        decoded = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_object_from_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError(f"{label} must contain one canonical JSON object") from error
    if type(decoded) is not dict:
        raise TypeError(f"{label} must contain one exact JSON object")
    if reject_executable_surfaces:
        _reject_executable_payload_surfaces(decoded, label)
    if _canonical_json_bytes(decoded) != payload:
        raise ValueError(f"{label} are not exact canonical JSON")
    return decoded


def _load_canonical_json_lines(raw: bytes, label: str) -> tuple[dict[str, object], ...]:
    if not raw.endswith(b"\n") or len(raw) > MAX_INLINE_ARTIFACT_BYTES:
        raise ValueError(f"{label} must be bounded and end with one newline")
    lines = raw[:-1].split(b"\n")
    if not lines or any(not line for line in lines):
        raise ValueError(f"{label} cannot contain empty rows")
    rows = tuple(
        _load_canonical_json_object(
            line,
            f"{label} row {index}",
            maximum_bytes=MAX_INLINE_ARTIFACT_BYTES,
            reject_executable_surfaces=False,
        )
        for index, line in enumerate(lines)
    )
    for index, row in enumerate(rows):
        _reject_wall_clock_surfaces(row, f"{label} row {index}")
    return rows


def _strict_base64(encoded: str, label: str, *, maximum_bytes: int) -> bytes:
    maximum_encoded_bytes = 4 * ((maximum_bytes + 2) // 3)
    if len(encoded) > maximum_encoded_bytes:
        raise ValueError(f"serialized {label} exceeds its protocol byte limit")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as error:
        raise ValueError(f"serialized {label} is not strict base64") from error
    if base64.b64encode(raw).decode("ascii") != encoded:
        raise ValueError(f"serialized {label} base64 is not canonical")
    if len(raw) > maximum_bytes:
        raise ValueError(f"serialized {label} exceeds its protocol byte limit")
    return raw


def _require_exact_round_trip(
    record: object,
    payload: Mapping[str, object],
    label: str,
) -> None:
    as_dict = getattr(record, "as_dict", None)
    if not callable(as_dict) or as_dict() != payload:
        raise ValueError(f"serialized {label} did not round-trip exactly")


__all__ = [
    "INLINE_ARTIFACT_SCHEMA_ID",
    "InlineArtifactMediaTypeV1",
    "InlineArtifactV1",
    "LAN_PROTOCOL_ENVELOPE_SCHEMA_ID",
    "LanMessageKindV1",
    "LanProtocolEnvelopeV1",
    "MAX_DIAGNOSTIC_DETAILS_BYTES",
    "MAX_INLINE_ARTIFACT_BYTES",
    "MAX_LAN_ENVELOPE_BYTES_V1",
    "MAX_LAN_PAYLOAD_BYTES_V1",
    "MAX_LAN_SESSION_SEQUENCE_V1",
    "MAX_PROTOCOL_JSON_DEPTH",
    "MAX_PROTOCOL_JSON_ITEMS",
    "ORCHESTRATION_PROTOCOL_SCHEMA_VERSION",
    "PROTOCOL_DIAGNOSTIC_SCHEMA_ID",
    "ProtocolDiagnosticV1",
    "RUNTIME_AUDIT_RESULT_SCHEMA_ID",
    "RuntimeAuditResultV1",
    "RuntimeAuditStatusV1",
    "WORKER_COMPATIBILITY_SCHEMA_ID",
    "WORKER_RESULT_MANIFEST_SCHEMA_ID",
    "WORKER_RESULT_SCHEMA_ID",
    "WORK_REQUEST_SCHEMA_ID",
    "WorkRequestV1",
    "WorkerCompatibilityV1",
    "WorkerResultManifestV1",
    "WorkerResultStatusV1",
    "WorkerResultV1",
]
