"""Authorized, redacted, unpacked instructor-evidence export.

An export is five canonical JSON files in one ordinary directory.  It is not an
archive, content-addressed store, pack, installer, account boundary, or network
service.  Construction is allowlist-only and consent-gated; import verifies every
byte before writing beneath an explicit :class:`~kirby2.research.paths.DataPaths`
evidence root.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import ClassVar

from kirby2.research.paths import DataAreaId, DataPaths

from .assignments import AssignmentAttemptManifestV1, AssignmentRevisionV1
from .consent import (
    ConsentDecisionReasonV1,
    ConsentDecisionStatusV1,
    ConsentRecordV1,
    ConsentScopeV1,
    EVIDENCE_EXPORT_DECISION_SCHEMA_ID,
    EVIDENCE_EXPORT_DECISION_SCHEMA_VERSION,
    EvidenceExportClassV1,
    EvidenceExportDecisionV1,
    EvidenceExportPermissionV1,
    decide_evidence_export,
)
from .redaction import (
    PortableEvidenceKindV1,
    RedactionManifestV1,
    RedactionPolicyV1,
    redact_document,
)
from .reviews import ReviewRevisionV1
from .rubrics import RubricScoreSidecarV1
from .statistics import VersionSignatureV1
from .studies import (
    ProtocolDeviationV1,
    StudyAmendmentV1,
    StudyAttemptBindingV1,
    StudyDataExportPolicyV1,
    StudyExecutionLedgerV1,
    StudyRevisionV1,
)


EXPORT_ARTIFACT_REFERENCE_SCHEMA_ID = "KIRBY2_EXPORT_ARTIFACT_REFERENCE_V1"
EXPORT_INVENTORY_SCHEMA_ID = "KIRBY2_EXPORT_INVENTORY_V1"
EXPORT_CONTENT_DIGEST_SCHEMA_ID = "KIRBY2_EXPORT_CONTENT_DIGEST_V1"
EXPORT_LINEAGE_REFERENCE_SCHEMA_ID = "KIRBY2_EXPORT_LINEAGE_REFERENCE_V1"
EXPORT_OMISSION_SCHEMA_ID = "KIRBY2_EXPORT_OMISSION_V1"
EXPORT_CONSENT_DECISION_SCHEMA_ID = EVIDENCE_EXPORT_DECISION_SCHEMA_ID
SELECTED_CAUSAL_TRACE_SCHEMA_ID = "KIRBY2_SELECTED_CAUSAL_TRACE_V1"
EVIDENCE_EXPORT_MANIFEST_SCHEMA_ID = "KIRBY2_EVIDENCE_EXPORT_MANIFEST_V1"
IMPORTED_EXPORT_SCHEMA_ID = "KIRBY2_IMPORTED_EXPORT_V1"
EXPORT_SCHEMA_VERSION = 1

EXPORT_FORMAT_V1 = "UNPACKED_CANONICAL_DIRECTORY_NOT_ARCHIVE_CAS_PACK_OR_INSTALLER_V1"
EXPORT_PROHIBITED_CONTENT_POLICY_V1 = (
    "NO_DIRECT_IDENTITY_IDENTITY_MAPPING_SECRETS_LOCAL_PATHS_OR_"
    "UNAUTHORIZED_HIDDEN_REVEAL_DATA_V1"
)

EVIDENCE_FILENAME = "evidence.json"
REDACTION_MANIFEST_FILENAME = "redaction-manifest.json"
CONSENT_DECISION_FILENAME = "consent-decision.json"
INVENTORY_FILENAME = "inventory.json"
MANIFEST_FILENAME = "manifest.json"
_EXPECTED_FILES = frozenset(
    {
        EVIDENCE_FILENAME,
        REDACTION_MANIFEST_FILENAME,
        CONSENT_DECISION_FILENAME,
        INVENTORY_FILENAME,
        MANIFEST_FILENAME,
    }
)
_PORTABLE_ROOTS = (
    "annotations",
    "assignment",
    "attempt_manifest",
    "limitations",
    "provenance",
    "rubric_scores",
    "selected_causal_traces",
    "software_version",
)
_REQUIRED_ALLOWLIST_PATHS = tuple(f"/{name}" for name in _PORTABLE_ROOTS)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:[\\/]")
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_TRACES = 64
_MAX_REPEATED_RECORDS = 256


class ExportArtifactKindV1(str, Enum):
    PORTABLE_EVIDENCE = "PORTABLE_EVIDENCE"
    REDACTION_MANIFEST = "REDACTION_MANIFEST"
    CONSENT_DECISION = "CONSENT_DECISION"


class ExportLineageKindV1(str, Enum):
    ASSIGNMENT_REVISION = "ASSIGNMENT_REVISION"
    ATTEMPT_MANIFEST = "ATTEMPT_MANIFEST"
    STUDY_REVISION = "STUDY_REVISION"
    STUDY_PROTOCOL_LOCK = "STUDY_PROTOCOL_LOCK"
    STUDY_EXECUTION_LEDGER = "STUDY_EXECUTION_LEDGER"
    RUBRIC_SCORE = "RUBRIC_SCORE"
    REVIEW_REVISION = "REVIEW_REVISION"
    SELECTED_CAUSAL_TRACE = "SELECTED_CAUSAL_TRACE"


class ExportOmissionReasonV1(str, Enum):
    NOT_SELECTED = "NOT_SELECTED"
    NOT_ALLOWLISTED = "NOT_ALLOWLISTED"
    CONSENT_NOT_AUTHORIZED = "CONSENT_NOT_AUTHORIZED"
    FIELD_REDACTED = "FIELD_REDACTED"
    PROHIBITED_DIRECT_IDENTITY = "PROHIBITED_DIRECT_IDENTITY"
    PROHIBITED_IDENTITY_MAPPING = "PROHIBITED_IDENTITY_MAPPING"
    PROHIBITED_SECRET = "PROHIBITED_SECRET"
    PROHIBITED_LOCAL_PATH = "PROHIBITED_LOCAL_PATH"
    PROHIBITED_HIDDEN_REVEAL_DATA = "PROHIBITED_HIDDEN_REVEAL_DATA"


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("export JSON contains a duplicate object key")
        result[key] = value
    return result


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
        raise ValueError("export value is not strict canonical JSON") from error


def _canonical_json_value(raw: bytes, label: str) -> object:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_JSON_BYTES:
        raise ValueError(f"{label} byte length is invalid")
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_pairs_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if _canonical_json_bytes(value) != raw:
        raise ValueError(f"{label} must be exact canonical JSON")
    return value


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    value = _canonical_json_value(raw, label)
    if type(value) is not dict:
        raise TypeError(f"{label} must be one canonical JSON object")
    return value


def _fields(value: object, expected: frozenset[str], label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact object")
    actual = frozenset(value)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an exact array")
    return value


def _text(value: object, label: str, *, maximum: int = 4096) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty text without edge whitespace")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{label} must use canonical NFC text")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds its bounded size")
    if any(character == "\x00" or character in "\r\n" for character in value):
        raise ValueError(f"{label} contains a forbidden control character")
    return value


def _public_text(value: object, label: str, *, maximum: int = 4096) -> str:
    result = _text(value, label, maximum=maximum)
    if re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", result):
        raise ValueError(f"{label} contains a prohibited direct identifier")
    if re.search(
        r"(?:\A(?:/Users/|/home/|/private/|/var/|/tmp/|/opt/|/etc/|~/)|"
        r"\A[A-Za-z]:[\\/]|\Afile://|\A\\\\)",
        result,
    ):
        raise ValueError(f"{label} contains a prohibited local filesystem path")
    if re.search(
        r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9._~+/=-]+|"
        r"\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
        r"\bsk-[A-Za-z0-9_-]{20,}\b)",
        result,
    ):
        raise ValueError(f"{label} contains prohibited secret material")
    return result


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be one canonical identifier")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _utc(value: object, label: str) -> str:
    result = _text(value, label)
    if _UTC.fullmatch(result) is None:
        raise ValueError(f"{label} must be whole-second UTC")
    try:
        datetime.fromisoformat(result[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} is not a valid UTC timestamp") from error
    return result


def _portable_relative_path(value: object, label: str = "export relative path") -> str:
    result = _text(value, label, maximum=512)
    posix = PurePosixPath(result)
    windows = PureWindowsPath(result)
    if (
        "\\" in result
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or result != posix.as_posix()
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise ValueError(f"{label} must be canonical portable relative POSIX")
    return result


class _CanonicalRecordV1:
    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())  # type: ignore[attr-defined]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class SelectedCausalTraceV1(_CanonicalRecordV1):
    """One explicitly selected causal-trace document with exact source bytes."""

    trace_id: str
    trace_sha256: str
    trace_json: str

    schema_id: ClassVar[str] = SELECTED_CAUSAL_TRACE_SCHEMA_ID
    schema_version: ClassVar[int] = EXPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.trace_id, "selected causal-trace ID")
        _sha256(self.trace_sha256, "selected causal-trace digest")
        if type(self.trace_json) is not str:
            raise TypeError("selected causal-trace JSON must be exact text")
        raw = self.trace_json.encode("ascii")
        _canonical_object(raw, "selected causal trace")
        if hashlib.sha256(raw).hexdigest() != self.trace_sha256:
            raise ValueError("selected causal-trace bytes differ from their digest")

    @property
    def source_bytes(self) -> bytes:
        return self.trace_json.encode("ascii")

    @property
    def trace(self) -> dict[str, object]:
        return _canonical_object(self.source_bytes, "selected causal trace")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "trace": self.trace,
            "trace_id": self.trace_id,
            "trace_sha256": self.trace_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> SelectedCausalTraceV1:
        payload = _fields(
            value,
            frozenset({"schema_id", "schema_version", "trace", "trace_id", "trace_sha256"}),
            "selected causal trace",
        )
        if payload["schema_id"] != cls.schema_id or payload["schema_version"] != 1:
            raise ValueError("selected causal-trace schema differs")
        trace_bytes = _canonical_json_bytes(payload["trace"])
        return cls(
            trace_id=_identifier(payload["trace_id"], "selected causal-trace ID"),
            trace_sha256=_sha256(payload["trace_sha256"], "selected causal-trace digest"),
            trace_json=trace_bytes.decode("ascii"),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> SelectedCausalTraceV1:
        restored = cls.from_dict(_canonical_object(raw, "selected causal trace"))
        if restored.canonical_bytes() != raw:
            raise ValueError("selected causal trace changed during restoration")
        return restored


def create_selected_causal_trace(
    trace_id: str,
    source_bytes: bytes,
) -> SelectedCausalTraceV1:
    _canonical_object(source_bytes, "selected causal trace source")
    return SelectedCausalTraceV1(
        trace_id=trace_id,
        trace_sha256=hashlib.sha256(source_bytes).hexdigest(),
        trace_json=source_bytes.decode("ascii"),
    )


@dataclass(frozen=True, slots=True)
class ExportOmissionV1(_CanonicalRecordV1):
    item_kind: str
    item_id: str
    reason: ExportOmissionReasonV1
    detail: str
    source_sha256: str | None = None

    schema_id: ClassVar[str] = EXPORT_OMISSION_SCHEMA_ID
    schema_version: ClassVar[int] = EXPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.item_kind, "export omission item kind")
        _identifier(self.item_id, "export omission item ID")
        if type(self.reason) is not ExportOmissionReasonV1:
            raise TypeError("export omission reason is invalid")
        _public_text(self.detail, "export omission detail")
        if self.source_sha256 is not None:
            _sha256(self.source_sha256, "omitted source digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "item_id": self.item_id,
            "item_kind": self.item_kind,
            "reason": self.reason.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> ExportOmissionV1:
        payload = _fields(
            value,
            frozenset(
                {"detail", "item_id", "item_kind", "reason", "schema_id", "schema_version", "source_sha256"}
            ),
            "export omission",
        )
        if payload["schema_id"] != cls.schema_id or payload["schema_version"] != 1:
            raise ValueError("export omission schema differs")
        return cls(
            item_kind=_identifier(payload["item_kind"], "export omission item kind"),
            item_id=_identifier(payload["item_id"], "export omission item ID"),
            reason=ExportOmissionReasonV1(_text(payload["reason"], "export omission reason")),
            detail=_public_text(payload["detail"], "export omission detail"),
            source_sha256=(
                None
                if payload["source_sha256"] is None
                else _sha256(payload["source_sha256"], "omitted source digest")
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ExportOmissionV1:
        restored = cls.from_dict(_canonical_object(raw, "export omission"))
        if restored.canonical_bytes() != raw:
            raise ValueError("export omission changed during restoration")
        return restored


@dataclass(frozen=True, slots=True)
class ExportConsentDecisionV1(_CanonicalRecordV1):
    """Standalone snapshot of one governed, allowed export decision."""

    pseudonymous_profile_id: str
    consent_id: str
    consent_sha256: str
    required_scope: ConsentScopeV1
    requested_export: EvidenceExportClassV1
    decision_time_utc: str
    allowed: bool
    status: ConsentDecisionStatusV1
    reason: ConsentDecisionReasonV1
    decision_id: str

    schema_id: ClassVar[str] = EXPORT_CONSENT_DECISION_SCHEMA_ID
    schema_version: ClassVar[int] = EVIDENCE_EXPORT_DECISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.pseudonymous_profile_id, "export decision profile ID")
        _identifier(self.consent_id, "export decision consent ID")
        _sha256(self.consent_sha256, "export decision consent digest")
        if type(self.required_scope) is not ConsentScopeV1:
            raise TypeError("export decision scope is invalid")
        if self.requested_export is not EvidenceExportClassV1.PSEUDONYMOUS_REDACTED_EVIDENCE:
            raise ValueError("portable export must request redacted pseudonymous evidence")
        _utc(self.decision_time_utc, "export decision time")
        if type(self.allowed) is not bool or not self.allowed:
            raise PermissionError("portable export requires an allowed consent decision")
        if self.status is not ConsentDecisionStatusV1.AUTHORIZED:
            raise PermissionError("portable export decision must be AUTHORIZED")
        if self.reason is not ConsentDecisionReasonV1.AUTHORIZED_BY_ACTIVE_CONSENT:
            raise PermissionError("portable export requires active-consent authorization")
        _identifier(self.decision_id, "export decision ID")

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "consent_id": self.consent_id,
            "consent_sha256": self.consent_sha256,
            "decision_id": self.decision_id,
            "decision_kind": "EVIDENCE_EXPORT",
            "decision_time_utc": self.decision_time_utc,
            "pseudonymous_profile_id": self.pseudonymous_profile_id,
            "reason": self.reason.value,
            "requested_export": self.requested_export.value,
            "required_scope": self.required_scope.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
        }

    @classmethod
    def from_decision(cls, decision: EvidenceExportDecisionV1) -> ExportConsentDecisionV1:
        if type(decision) is not EvidenceExportDecisionV1:
            raise TypeError("export authorization must be EvidenceExportDecisionV1")
        snapshot = cls(
            pseudonymous_profile_id=decision.pseudonymous_profile_id,
            consent_id=decision.consent_id,
            consent_sha256=decision.consent_sha256,
            required_scope=decision.required_scope,
            requested_export=decision.requested_export,
            decision_time_utc=decision.decision_time_utc,
            allowed=decision.allowed,
            status=decision.status,
            reason=decision.reason,
            decision_id=decision.decision_id,
        )
        if snapshot.as_dict() != decision.as_dict():
            raise ValueError("export decision snapshot differs from governed evaluator")
        return snapshot

    @classmethod
    def from_dict(cls, value: object) -> ExportConsentDecisionV1:
        payload = _fields(
            value,
            frozenset(
                {
                    "allowed",
                    "consent_id",
                    "consent_sha256",
                    "decision_id",
                    "decision_kind",
                    "decision_time_utc",
                    "pseudonymous_profile_id",
                    "reason",
                    "requested_export",
                    "required_scope",
                    "schema_id",
                    "schema_version",
                    "status",
                }
            ),
            "export consent decision",
        )
        if payload["schema_id"] != cls.schema_id or payload["schema_version"] != 1:
            raise ValueError("export consent-decision schema differs")
        if payload["decision_kind"] != "EVIDENCE_EXPORT":
            raise ValueError("export consent-decision kind differs")
        return cls(
            pseudonymous_profile_id=_identifier(
                payload["pseudonymous_profile_id"], "export decision profile ID"
            ),
            consent_id=_identifier(payload["consent_id"], "export decision consent ID"),
            consent_sha256=_sha256(
                payload["consent_sha256"], "export decision consent digest"
            ),
            required_scope=ConsentScopeV1(_text(payload["required_scope"], "export decision scope")),
            requested_export=EvidenceExportClassV1(
                _text(payload["requested_export"], "requested export class")
            ),
            decision_time_utc=_utc(payload["decision_time_utc"], "export decision time"),
            allowed=payload["allowed"],  # type: ignore[arg-type]
            status=ConsentDecisionStatusV1(_text(payload["status"], "export decision status")),
            reason=ConsentDecisionReasonV1(_text(payload["reason"], "export decision reason")),
            decision_id=_identifier(payload["decision_id"], "export decision ID"),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ExportConsentDecisionV1:
        restored = cls.from_dict(_canonical_object(raw, "export consent decision"))
        if restored.canonical_bytes() != raw:
            raise ValueError("export consent decision changed during restoration")
        return restored


@dataclass(frozen=True, slots=True)
class ExportArtifactReferenceV1(_CanonicalRecordV1):
    artifact_kind: ExportArtifactKindV1
    artifact_id: str
    relative_path: str
    sha256_digest: str
    byte_count: int
    media_type: str
    source_id: str
    source_sha256: str

    schema_id: ClassVar[str] = EXPORT_ARTIFACT_REFERENCE_SCHEMA_ID
    schema_version: ClassVar[int] = EXPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.artifact_kind) is not ExportArtifactKindV1:
            raise TypeError("export artifact kind is invalid")
        _identifier(self.artifact_id, "export artifact ID")
        _portable_relative_path(self.relative_path)
        _sha256(self.sha256_digest, "export artifact digest")
        _positive_int(self.byte_count, "export artifact byte count")
        _text(self.media_type, "export artifact media type")
        _identifier(self.source_id, "export artifact source ID")
        _sha256(self.source_sha256, "export artifact source digest")

    @property
    def digest(self) -> str:
        return self.sha256_digest

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind.value,
            "byte_count": self.byte_count,
            "media_type": self.media_type,
            "relative_path": self.relative_path,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sha256": self.sha256_digest,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> ExportArtifactReferenceV1:
        payload = _fields(
            value,
            frozenset(
                {
                    "artifact_id",
                    "artifact_kind",
                    "byte_count",
                    "media_type",
                    "relative_path",
                    "schema_id",
                    "schema_version",
                    "sha256",
                    "source_id",
                    "source_sha256",
                }
            ),
            "export artifact reference",
        )
        if payload["schema_id"] != cls.schema_id or payload["schema_version"] != 1:
            raise ValueError("export artifact-reference schema differs")
        return cls(
            artifact_kind=ExportArtifactKindV1(
                _text(payload["artifact_kind"], "export artifact kind")
            ),
            artifact_id=_identifier(payload["artifact_id"], "export artifact ID"),
            relative_path=_portable_relative_path(payload["relative_path"]),
            sha256_digest=_sha256(payload["sha256"], "export artifact digest"),
            byte_count=_positive_int(payload["byte_count"], "export artifact byte count"),
            media_type=_text(payload["media_type"], "export artifact media type"),
            source_id=_identifier(payload["source_id"], "export artifact source ID"),
            source_sha256=_sha256(payload["source_sha256"], "export artifact source digest"),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ExportArtifactReferenceV1:
        restored = cls.from_dict(_canonical_object(raw, "export artifact reference"))
        if restored.canonical_bytes() != raw:
            raise ValueError("export artifact reference changed during restoration")
        return restored


def _artifact_reference(
    *,
    artifact_kind: ExportArtifactKindV1,
    artifact_id: str,
    relative_path: str,
    payload: bytes,
    source_id: str,
    source_sha256: str,
) -> ExportArtifactReferenceV1:
    return ExportArtifactReferenceV1(
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        relative_path=relative_path,
        sha256_digest=hashlib.sha256(payload).hexdigest(),
        byte_count=len(payload),
        media_type="application/json",
        source_id=source_id,
        source_sha256=source_sha256,
    )


@dataclass(frozen=True, slots=True)
class ExportInventoryV1(_CanonicalRecordV1):
    artifacts: tuple[ExportArtifactReferenceV1, ...]
    inventory_id: str = field(init=False)

    schema_id: ClassVar[str] = EXPORT_INVENTORY_SCHEMA_ID
    schema_version: ClassVar[int] = EXPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.artifacts) is not tuple or any(
            type(item) is not ExportArtifactReferenceV1 for item in self.artifacts
        ):
            raise TypeError("export inventory must be an immutable reference tuple")
        expected = {
            ExportArtifactKindV1.PORTABLE_EVIDENCE,
            ExportArtifactKindV1.REDACTION_MANIFEST,
            ExportArtifactKindV1.CONSENT_DECISION,
        }
        if {item.artifact_kind for item in self.artifacts} != expected or len(self.artifacts) != 3:
            raise ValueError("export inventory must contain the three exact payload artifacts")
        canonical = tuple(sorted(self.artifacts, key=lambda item: item.relative_path))
        if canonical != self.artifacts:
            raise ValueError("export inventory artifacts must use canonical path order")
        paths = tuple(item.relative_path for item in self.artifacts)
        if len(paths) != len(set(paths)):
            raise ValueError("export inventory paths cannot repeat")
        expected_paths = {
            ExportArtifactKindV1.PORTABLE_EVIDENCE: EVIDENCE_FILENAME,
            ExportArtifactKindV1.REDACTION_MANIFEST: REDACTION_MANIFEST_FILENAME,
            ExportArtifactKindV1.CONSENT_DECISION: CONSENT_DECISION_FILENAME,
        }
        if any(
            item.relative_path != expected_paths[item.artifact_kind]
            or item.media_type != "application/json"
            for item in self.artifacts
        ):
            raise ValueError("export inventory paths or media types differ from the format")
        object.__setattr__(
            self,
            "inventory_id",
            "export-inventory-"
            + hashlib.sha256(_canonical_json_bytes(self.identity_dict())).hexdigest(),
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "artifacts": [item.as_dict() for item in self.artifacts],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "inventory_id": self.inventory_id}

    @classmethod
    def from_dict(cls, value: object) -> ExportInventoryV1:
        payload = _fields(
            value,
            frozenset({"artifacts", "inventory_id", "schema_id", "schema_version"}),
            "export inventory",
        )
        if payload["schema_id"] != cls.schema_id or payload["schema_version"] != 1:
            raise ValueError("export inventory schema differs")
        restored = cls(
            artifacts=tuple(
                ExportArtifactReferenceV1.from_dict(item)
                for item in _array(payload["artifacts"], "export inventory artifacts")
            )
        )
        if restored.as_dict() != payload:
            raise ValueError("export inventory differs from canonical content")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ExportInventoryV1:
        restored = cls.from_dict(_canonical_object(raw, "export inventory"))
        if restored.canonical_bytes() != raw:
            raise ValueError("export inventory changed during restoration")
        return restored


@dataclass(frozen=True, slots=True)
class ExportLineageReferenceV1(_CanonicalRecordV1):
    """Exact immutable input identity retained by the portable export."""

    source_kind: ExportLineageKindV1
    source_id: str
    source_sha256: str

    schema_id: ClassVar[str] = EXPORT_LINEAGE_REFERENCE_SCHEMA_ID
    schema_version: ClassVar[int] = EXPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.source_kind) is not ExportLineageKindV1:
            raise TypeError("export lineage source kind is invalid")
        _identifier(self.source_id, "export lineage source ID")
        _sha256(self.source_sha256, "export lineage source digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> ExportLineageReferenceV1:
        payload = _fields(
            value,
            frozenset(
                {"schema_id", "schema_version", "source_id", "source_kind", "source_sha256"}
            ),
            "export lineage reference",
        )
        if payload["schema_id"] != cls.schema_id or payload["schema_version"] != 1:
            raise ValueError("export lineage-reference schema differs")
        return cls(
            source_kind=ExportLineageKindV1(
                _text(payload["source_kind"], "export lineage source kind")
            ),
            source_id=_identifier(payload["source_id"], "export lineage source ID"),
            source_sha256=_sha256(
                payload["source_sha256"], "export lineage source digest"
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ExportLineageReferenceV1:
        restored = cls.from_dict(_canonical_object(raw, "export lineage reference"))
        if restored.canonical_bytes() != raw:
            raise ValueError("export lineage reference changed during restoration")
        return restored


@dataclass(frozen=True, slots=True)
class ExportContentDigestV1(_CanonicalRecordV1):
    """Pre-redaction and retained digest for one required portable root."""

    content_name: str
    source_sha256: str
    retained_sha256: str

    schema_id: ClassVar[str] = EXPORT_CONTENT_DIGEST_SCHEMA_ID
    schema_version: ClassVar[int] = EXPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.content_name not in _PORTABLE_ROOTS:
            raise ValueError("export content name is outside the portable allowlist")
        _sha256(self.source_sha256, "export source-content digest")
        _sha256(self.retained_sha256, "export retained-content digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "content_name": self.content_name,
            "retained_sha256": self.retained_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> ExportContentDigestV1:
        payload = _fields(
            value,
            frozenset(
                {
                    "content_name",
                    "retained_sha256",
                    "schema_id",
                    "schema_version",
                    "source_sha256",
                }
            ),
            "export content digest",
        )
        if payload["schema_id"] != cls.schema_id or payload["schema_version"] != 1:
            raise ValueError("export content-digest schema differs")
        return cls(
            content_name=_text(payload["content_name"], "export content name"),
            source_sha256=_sha256(
                payload["source_sha256"], "export source-content digest"
            ),
            retained_sha256=_sha256(
                payload["retained_sha256"], "export retained-content digest"
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ExportContentDigestV1:
        restored = cls.from_dict(_canonical_object(raw, "export content digest"))
        if restored.canonical_bytes() != raw:
            raise ValueError("export content digest changed during restoration")
        return restored


@dataclass(frozen=True, slots=True)
class EvidenceExportManifestV1(_CanonicalRecordV1):
    """Closed manifest for one ordinary, unpacked evidence directory."""

    exported_at_utc: str
    pseudonymous_profile_id: str
    assignment_id: str
    assignment_sha256: str
    attempt_id: str
    attempt_sha256: str
    study_id: str
    study_revision_number: int
    study_revision_sha256: str
    study_manifest_sha256: str
    protocol_lock_id: str
    protocol_lock_sha256: str
    study_execution_ledger_id: str
    study_execution_ledger_sha256: str
    study_data_export_policy: StudyDataExportPolicyV1
    software_version: str
    compatibility_versions: tuple[VersionSignatureV1, ...]
    lineage_references: tuple[ExportLineageReferenceV1, ...]
    authorized_consent_scopes: tuple[ConsentScopeV1, ...]
    consent_decision_id: str
    consent_decision_sha256: str
    redaction_document_id: str
    redaction_policy_id: str
    redaction_policy_sha256: str
    redaction_policy: RedactionPolicyV1
    redaction_manifest_sha256: str
    inventory_id: str
    inventory_sha256: str
    retained_references: tuple[ExportArtifactReferenceV1, ...]
    content_digests: tuple[ExportContentDigestV1, ...]
    omissions: tuple[ExportOmissionV1, ...]
    limitations: tuple[str, ...]
    export_id: str = field(init=False)

    schema_id: ClassVar[str] = EVIDENCE_EXPORT_MANIFEST_SCHEMA_ID
    schema_version: ClassVar[int] = EXPORT_SCHEMA_VERSION
    format: ClassVar[str] = EXPORT_FORMAT_V1
    prohibited_content_policy: ClassVar[str] = EXPORT_PROHIBITED_CONTENT_POLICY_V1

    def __post_init__(self) -> None:
        _utc(self.exported_at_utc, "evidence export time")
        _identifier(self.pseudonymous_profile_id, "export profile ID")
        _identifier(self.assignment_id, "export assignment ID")
        _sha256(self.assignment_sha256, "export assignment digest")
        _identifier(self.attempt_id, "export attempt ID")
        _sha256(self.attempt_sha256, "export attempt digest")
        _identifier(self.study_id, "export study ID")
        _positive_int(self.study_revision_number, "export study revision number")
        _sha256(self.study_revision_sha256, "export study revision digest")
        _sha256(self.study_manifest_sha256, "export study manifest digest")
        _identifier(self.protocol_lock_id, "export protocol lock ID")
        _sha256(self.protocol_lock_sha256, "export protocol lock digest")
        _identifier(self.study_execution_ledger_id, "export study ledger ID")
        _sha256(
            self.study_execution_ledger_sha256,
            "export study execution-ledger digest",
        )
        if type(self.study_data_export_policy) is not StudyDataExportPolicyV1:
            raise TypeError("export study data-export policy is invalid")
        if (
            self.study_data_export_policy.permission
            is not EvidenceExportPermissionV1.PSEUDONYMOUS_REDACTED_EVIDENCE_ONLY
        ):
            raise PermissionError("study protocol does not authorize portable export")
        _public_text(self.software_version, "export software version", maximum=256)
        if type(self.compatibility_versions) is not tuple or not self.compatibility_versions:
            raise ValueError("export compatibility versions must be a nonempty tuple")
        if any(type(item) is not VersionSignatureV1 for item in self.compatibility_versions):
            raise TypeError("export compatibility versions must use VersionSignatureV1")
        if len(self.compatibility_versions) > _MAX_REPEATED_RECORDS:
            raise ValueError("export compatibility version count exceeds its bound")
        if tuple(sorted(self.compatibility_versions, key=lambda item: item.canonical_bytes())) != self.compatibility_versions:
            raise ValueError("export compatibility versions must use canonical order")
        if len({item.signature_sha256 for item in self.compatibility_versions}) != len(
            self.compatibility_versions
        ):
            raise ValueError("export compatibility versions cannot repeat")
        if type(self.authorized_consent_scopes) is not tuple or any(
            type(item) is not ConsentScopeV1 for item in self.authorized_consent_scopes
        ):
            raise TypeError("authorized export consent scopes must be an immutable tuple")
        if tuple(sorted(set(self.authorized_consent_scopes), key=lambda item: item.value)) != self.authorized_consent_scopes:
            raise ValueError("authorized export consent scopes must be canonical and unique")
        required_export_scopes = {
            ConsentScopeV1.INSTRUCTIONAL_EVIDENCE,
            ConsentScopeV1.INSTRUCTOR_REVIEW,
            ConsentScopeV1.LOCAL_RESEARCH_STUDY,
        }
        if not required_export_scopes.issubset(self.authorized_consent_scopes):
            raise PermissionError("export manifest lacks required multi-purpose consent scopes")

        if type(self.lineage_references) is not tuple or any(
            type(item) is not ExportLineageReferenceV1 for item in self.lineage_references
        ):
            raise TypeError("export lineage references must be an immutable exact tuple")
        if not self.lineage_references or len(self.lineage_references) > _MAX_REPEATED_RECORDS:
            raise ValueError("export lineage reference count is invalid")
        lineage_order = tuple(
            sorted(
                self.lineage_references,
                key=lambda item: (item.source_kind.value, item.source_id, item.source_sha256),
            )
        )
        if lineage_order != self.lineage_references:
            raise ValueError("export lineage references must use canonical order")
        lineage_keys = tuple(
            (item.source_kind, item.source_id) for item in self.lineage_references
        )
        if len(lineage_keys) != len(set(lineage_keys)):
            raise ValueError("export lineage references cannot repeat")
        by_kind = {
            kind: tuple(item for item in self.lineage_references if item.source_kind is kind)
            for kind in ExportLineageKindV1
        }
        if len(by_kind[ExportLineageKindV1.ASSIGNMENT_REVISION]) != 1:
            raise ValueError("export lineage requires one exact assignment revision")
        if len(by_kind[ExportLineageKindV1.ATTEMPT_MANIFEST]) != 1:
            raise ValueError("export lineage requires one exact attempt manifest")
        if len(by_kind[ExportLineageKindV1.STUDY_REVISION]) != 1:
            raise ValueError("export lineage requires one exact study revision")
        if len(by_kind[ExportLineageKindV1.STUDY_PROTOCOL_LOCK]) != 1:
            raise ValueError("export lineage requires one exact study protocol lock")
        if len(by_kind[ExportLineageKindV1.STUDY_EXECUTION_LEDGER]) != 1:
            raise ValueError("export lineage requires one exact study execution ledger")
        if any(
            not by_kind[kind]
            for kind in (
                ExportLineageKindV1.RUBRIC_SCORE,
                ExportLineageKindV1.REVIEW_REVISION,
                ExportLineageKindV1.SELECTED_CAUSAL_TRACE,
            )
        ):
            raise ValueError("export lineage requires scores, reviews, and selected traces")
        assignment_ref = by_kind[ExportLineageKindV1.ASSIGNMENT_REVISION][0]
        attempt_ref = by_kind[ExportLineageKindV1.ATTEMPT_MANIFEST][0]
        if (assignment_ref.source_id, assignment_ref.source_sha256) != (
            self.assignment_id,
            self.assignment_sha256,
        ):
            raise ValueError("export assignment fields differ from exact lineage")
        if (attempt_ref.source_id, attempt_ref.source_sha256) != (
            self.attempt_id,
            self.attempt_sha256,
        ):
            raise ValueError("export attempt fields differ from exact lineage")
        study_ref = by_kind[ExportLineageKindV1.STUDY_REVISION][0]
        lock_ref = by_kind[ExportLineageKindV1.STUDY_PROTOCOL_LOCK][0]
        ledger_ref = by_kind[ExportLineageKindV1.STUDY_EXECUTION_LEDGER][0]
        if (study_ref.source_id, study_ref.source_sha256) != (
            self.study_id,
            self.study_revision_sha256,
        ):
            raise ValueError("export study fields differ from exact lineage")
        if (lock_ref.source_id, lock_ref.source_sha256) != (
            self.protocol_lock_id,
            self.protocol_lock_sha256,
        ):
            raise ValueError("export protocol-lock fields differ from exact lineage")
        if (ledger_ref.source_id, ledger_ref.source_sha256) != (
            self.study_execution_ledger_id,
            self.study_execution_ledger_sha256,
        ):
            raise ValueError("export study-ledger fields differ from exact lineage")

        _identifier(self.consent_decision_id, "export consent decision ID")
        _sha256(self.consent_decision_sha256, "export consent decision digest")
        _identifier(self.redaction_document_id, "redaction document ID")
        _identifier(self.redaction_policy_id, "redaction policy ID")
        _sha256(self.redaction_policy_sha256, "redaction policy digest")
        if type(self.redaction_policy) is not RedactionPolicyV1:
            raise TypeError("export manifest redaction policy is invalid")
        if self.redaction_policy.allowlisted_paths != _REQUIRED_ALLOWLIST_PATHS:
            raise ValueError(
                "portable export redaction policy must name the eight exact required roots"
            )
        if (
            self.redaction_policy.policy_id != self.redaction_policy_id
            or self.redaction_policy.sha256 != self.redaction_policy_sha256
        ):
            raise ValueError("export manifest redaction policy binding differs")
        if (
            self.study_data_export_policy.redaction_policy_sha256
            != self.redaction_policy_sha256
        ):
            raise PermissionError(
                "study protocol does not authorize this exact redaction policy"
            )
        _sha256(self.redaction_manifest_sha256, "redaction manifest digest")
        _identifier(self.inventory_id, "export inventory ID")
        _sha256(self.inventory_sha256, "export inventory digest")

        if type(self.retained_references) is not tuple or any(
            type(item) is not ExportArtifactReferenceV1 for item in self.retained_references
        ):
            raise TypeError("retained export references must be an immutable exact tuple")
        if len(self.retained_references) != 3:
            raise ValueError("manifest must retain the three exact payload references")
        if tuple(sorted(self.retained_references, key=lambda item: item.relative_path)) != self.retained_references:
            raise ValueError("retained export references must use canonical path order")
        if {item.artifact_kind for item in self.retained_references} != set(ExportArtifactKindV1):
            raise ValueError("retained export reference kinds differ")
        evidence_ref = next(
            item
            for item in self.retained_references
            if item.artifact_kind is ExportArtifactKindV1.PORTABLE_EVIDENCE
        )
        if evidence_ref.artifact_id != self.redaction_document_id:
            raise ValueError("retained evidence reference differs from redaction document")

        if type(self.content_digests) is not tuple or any(
            type(item) is not ExportContentDigestV1 for item in self.content_digests
        ):
            raise TypeError("export content digests must be an immutable exact tuple")
        if tuple(item.content_name for item in self.content_digests) != _PORTABLE_ROOTS:
            raise ValueError("export content digests must cover every portable root")

        if type(self.omissions) is not tuple or any(
            type(item) is not ExportOmissionV1 for item in self.omissions
        ):
            raise TypeError("export omissions must be an immutable exact tuple")
        if len(self.omissions) > _MAX_REPEATED_RECORDS:
            raise ValueError("export omission count exceeds its bound")
        if tuple(sorted(self.omissions, key=lambda item: item.canonical_bytes())) != self.omissions:
            raise ValueError("export omissions must use canonical order")
        omission_keys = tuple((item.item_kind, item.item_id) for item in self.omissions)
        if len(omission_keys) != len(set(omission_keys)):
            raise ValueError("export omissions cannot repeat one item")

        if type(self.limitations) is not tuple or not self.limitations:
            raise ValueError("export limitations must be a nonempty immutable tuple")
        for item in self.limitations:
            _public_text(item, "export limitation")
        if tuple(sorted(set(self.limitations))) != self.limitations:
            raise ValueError("export limitations must be unique and canonically ordered")
        object.__setattr__(
            self,
            "export_id",
            "evidence-export-"
            + hashlib.sha256(_canonical_json_bytes(self.identity_dict())).hexdigest(),
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "assignment_id": self.assignment_id,
            "assignment_sha256": self.assignment_sha256,
            "attempt_id": self.attempt_id,
            "attempt_sha256": self.attempt_sha256,
            "authorized_consent_scopes": [
                item.value for item in self.authorized_consent_scopes
            ],
            "compatibility_versions": [item.as_dict() for item in self.compatibility_versions],
            "consent_decision_id": self.consent_decision_id,
            "consent_decision_sha256": self.consent_decision_sha256,
            "content_digests": [item.as_dict() for item in self.content_digests],
            "export_format": self.format,
            "exported_at_utc": self.exported_at_utc,
            "inventory_id": self.inventory_id,
            "inventory_sha256": self.inventory_sha256,
            "limitations": list(self.limitations),
            "lineage_references": [item.as_dict() for item in self.lineage_references],
            "omissions": [item.as_dict() for item in self.omissions],
            "prohibited_content_policy": self.prohibited_content_policy,
            "pseudonymous_profile_id": self.pseudonymous_profile_id,
            "redaction_document_id": self.redaction_document_id,
            "redaction_manifest_sha256": self.redaction_manifest_sha256,
            "redaction_policy_id": self.redaction_policy_id,
            "redaction_policy": self.redaction_policy.as_dict(),
            "redaction_policy_sha256": self.redaction_policy_sha256,
            "retained_references": [item.as_dict() for item in self.retained_references],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "software_version": self.software_version,
            "study_data_export_policy": self.study_data_export_policy.as_dict(),
            "study_execution_ledger_id": self.study_execution_ledger_id,
            "study_execution_ledger_sha256": self.study_execution_ledger_sha256,
            "study_id": self.study_id,
            "study_manifest_sha256": self.study_manifest_sha256,
            "study_revision_number": self.study_revision_number,
            "study_revision_sha256": self.study_revision_sha256,
            "protocol_lock_id": self.protocol_lock_id,
            "protocol_lock_sha256": self.protocol_lock_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "export_id": self.export_id}

    @classmethod
    def from_dict(cls, value: object) -> EvidenceExportManifestV1:
        payload = _fields(
            value,
            frozenset(
                {
                    "assignment_id",
                    "assignment_sha256",
                    "attempt_id",
                    "attempt_sha256",
                    "authorized_consent_scopes",
                    "compatibility_versions",
                    "consent_decision_id",
                    "consent_decision_sha256",
                    "content_digests",
                    "export_format",
                    "export_id",
                    "exported_at_utc",
                    "inventory_id",
                    "inventory_sha256",
                    "limitations",
                    "lineage_references",
                    "omissions",
                    "prohibited_content_policy",
                    "pseudonymous_profile_id",
                    "redaction_document_id",
                    "redaction_manifest_sha256",
                    "redaction_policy_id",
                    "redaction_policy",
                    "redaction_policy_sha256",
                    "retained_references",
                    "schema_id",
                    "schema_version",
                    "software_version",
                    "study_data_export_policy",
                    "study_execution_ledger_id",
                    "study_execution_ledger_sha256",
                    "study_id",
                    "study_manifest_sha256",
                    "study_revision_number",
                    "study_revision_sha256",
                    "protocol_lock_id",
                    "protocol_lock_sha256",
                }
            ),
            "evidence export manifest",
        )
        if payload["schema_id"] != cls.schema_id or payload["schema_version"] != 1:
            raise ValueError("evidence export manifest schema differs")
        if payload["export_format"] != cls.format:
            raise ValueError("evidence export directory format differs")
        if payload["prohibited_content_policy"] != cls.prohibited_content_policy:
            raise ValueError("evidence export prohibited-content policy differs")
        restored = cls(
            exported_at_utc=_utc(payload["exported_at_utc"], "evidence export time"),
            pseudonymous_profile_id=_identifier(
                payload["pseudonymous_profile_id"], "export profile ID"
            ),
            assignment_id=_identifier(payload["assignment_id"], "export assignment ID"),
            assignment_sha256=_sha256(
                payload["assignment_sha256"], "export assignment digest"
            ),
            attempt_id=_identifier(payload["attempt_id"], "export attempt ID"),
            attempt_sha256=_sha256(payload["attempt_sha256"], "export attempt digest"),
            authorized_consent_scopes=tuple(
                ConsentScopeV1(_text(item, "authorized export consent scope"))
                for item in _array(
                    payload["authorized_consent_scopes"],
                    "authorized export consent scopes",
                )
            ),
            study_id=_identifier(payload["study_id"], "export study ID"),
            study_revision_number=_positive_int(
                payload["study_revision_number"], "export study revision number"
            ),
            study_revision_sha256=_sha256(
                payload["study_revision_sha256"], "export study revision digest"
            ),
            study_manifest_sha256=_sha256(
                payload["study_manifest_sha256"], "export study manifest digest"
            ),
            protocol_lock_id=_identifier(
                payload["protocol_lock_id"], "export protocol lock ID"
            ),
            protocol_lock_sha256=_sha256(
                payload["protocol_lock_sha256"], "export protocol lock digest"
            ),
            study_execution_ledger_id=_identifier(
                payload["study_execution_ledger_id"], "export study ledger ID"
            ),
            study_execution_ledger_sha256=_sha256(
                payload["study_execution_ledger_sha256"],
                "export study execution-ledger digest",
            ),
            study_data_export_policy=StudyDataExportPolicyV1.from_dict(
                payload["study_data_export_policy"]
            ),
            software_version=_public_text(
                payload["software_version"], "export software version", maximum=256
            ),
            compatibility_versions=tuple(
                VersionSignatureV1.from_dict(item)
                for item in _array(
                    payload["compatibility_versions"], "export compatibility versions"
                )
            ),
            lineage_references=tuple(
                ExportLineageReferenceV1.from_dict(item)
                for item in _array(payload["lineage_references"], "export lineage references")
            ),
            consent_decision_id=_identifier(
                payload["consent_decision_id"], "export consent decision ID"
            ),
            consent_decision_sha256=_sha256(
                payload["consent_decision_sha256"], "export consent decision digest"
            ),
            redaction_document_id=_identifier(
                payload["redaction_document_id"], "redaction document ID"
            ),
            redaction_policy_id=_identifier(
                payload["redaction_policy_id"], "redaction policy ID"
            ),
            redaction_policy=RedactionPolicyV1.from_dict(payload["redaction_policy"]),
            redaction_policy_sha256=_sha256(
                payload["redaction_policy_sha256"], "redaction policy digest"
            ),
            redaction_manifest_sha256=_sha256(
                payload["redaction_manifest_sha256"], "redaction manifest digest"
            ),
            inventory_id=_identifier(payload["inventory_id"], "export inventory ID"),
            inventory_sha256=_sha256(
                payload["inventory_sha256"], "export inventory digest"
            ),
            retained_references=tuple(
                ExportArtifactReferenceV1.from_dict(item)
                for item in _array(
                    payload["retained_references"], "retained export references"
                )
            ),
            content_digests=tuple(
                ExportContentDigestV1.from_dict(item)
                for item in _array(payload["content_digests"], "export content digests")
            ),
            omissions=tuple(
                ExportOmissionV1.from_dict(item)
                for item in _array(payload["omissions"], "export omissions")
            ),
            limitations=tuple(
                _public_text(item, "export limitation")
                for item in _array(payload["limitations"], "export limitations")
            ),
        )
        if restored.as_dict() != payload:
            raise ValueError("evidence export manifest differs from canonical content")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> EvidenceExportManifestV1:
        restored = cls.from_dict(_canonical_object(raw, "evidence export manifest"))
        if restored.canonical_bytes() != raw:
            raise ValueError("evidence export manifest changed during restoration")
        return restored


@dataclass(frozen=True, slots=True)
class EvidenceExportBundleV1:
    """In-memory exact bytes for the five-file unpacked directory."""

    manifest: EvidenceExportManifestV1
    inventory: ExportInventoryV1
    evidence_bytes: bytes
    redaction_manifest: RedactionManifestV1
    consent_decision: ExportConsentDecisionV1

    def __post_init__(self) -> None:
        if type(self.manifest) is not EvidenceExportManifestV1:
            raise TypeError("export bundle manifest is invalid")
        if type(self.inventory) is not ExportInventoryV1:
            raise TypeError("export bundle inventory is invalid")
        if type(self.evidence_bytes) is not bytes:
            raise TypeError("export evidence payload must be exact bytes")
        evidence = _canonical_object(self.evidence_bytes, "portable evidence")
        if tuple(sorted(evidence)) != _PORTABLE_ROOTS:
            raise ValueError("portable evidence must retain every required content root")
        if type(self.redaction_manifest) is not RedactionManifestV1:
            raise TypeError("export redaction manifest is invalid")
        if self.redaction_manifest.document_kind is not PortableEvidenceKindV1.PORTABLE_EVIDENCE:
            raise ValueError("export redaction manifest has the wrong document kind")
        if type(self.consent_decision) is not ExportConsentDecisionV1:
            raise TypeError("export consent decision is invalid")
        if self.manifest.inventory_id != self.inventory.inventory_id:
            raise ValueError("export manifest inventory ID differs")
        if self.manifest.inventory_sha256 != self.inventory.sha256:
            raise ValueError("export manifest inventory digest differs")
        if self.manifest.retained_references != self.inventory.artifacts:
            raise ValueError("manifest retained references differ from inventory")
        if self.manifest.redaction_document_id != self.redaction_manifest.document_id:
            raise ValueError("manifest redaction document ID differs")
        if self.manifest.redaction_manifest_sha256 != self.redaction_manifest.sha256:
            raise ValueError("manifest redaction-manifest digest differs")
        if (
            self.redaction_manifest.policy_id != self.manifest.redaction_policy_id
            or self.redaction_manifest.policy_sha256
            != self.manifest.redaction_policy_sha256
        ):
            raise ValueError("redaction manifest differs from the retained exact policy")
        if self.redaction_manifest.output_sha256 != hashlib.sha256(self.evidence_bytes).hexdigest():
            raise ValueError("portable evidence differs from its redaction manifest")
        if self.manifest.consent_decision_id != self.consent_decision.decision_id:
            raise ValueError("manifest consent decision ID differs")
        if self.manifest.consent_decision_sha256 != self.consent_decision.sha256:
            raise ValueError("manifest consent decision digest differs")
        if self.manifest.pseudonymous_profile_id != self.consent_decision.pseudonymous_profile_id:
            raise ValueError("manifest profile differs from consent authorization")
        if self.consent_decision.required_scope not in self.manifest.authorized_consent_scopes:
            raise ValueError("consent decision scope is absent from the authorized scope set")
        payload_by_kind = {
            ExportArtifactKindV1.PORTABLE_EVIDENCE: self.evidence_bytes,
            ExportArtifactKindV1.REDACTION_MANIFEST: self.redaction_manifest.canonical_bytes(),
            ExportArtifactKindV1.CONSENT_DECISION: self.consent_decision.canonical_bytes(),
        }
        for reference in self.inventory.artifacts:
            raw = payload_by_kind[reference.artifact_kind]
            if reference.byte_count != len(raw) or reference.sha256_digest != hashlib.sha256(raw).hexdigest():
                raise ValueError("export inventory reference differs from exact payload bytes")
        reference_by_kind = {
            item.artifact_kind: item for item in self.inventory.artifacts
        }
        evidence_reference = reference_by_kind[ExportArtifactKindV1.PORTABLE_EVIDENCE]
        redaction_reference = reference_by_kind[
            ExportArtifactKindV1.REDACTION_MANIFEST
        ]
        consent_reference = reference_by_kind[ExportArtifactKindV1.CONSENT_DECISION]
        if (
            evidence_reference.artifact_id != self.redaction_manifest.document_id
            or evidence_reference.source_id != self.redaction_manifest.document_id
            or evidence_reference.source_sha256 != self.redaction_manifest.source_sha256
        ):
            raise ValueError("portable evidence reference has incorrect source lineage")
        if (
            redaction_reference.artifact_id
            != "redaction-manifest-" + self.redaction_manifest.sha256
            or redaction_reference.source_id != self.redaction_manifest.document_id
            or redaction_reference.source_sha256 != self.redaction_manifest.source_sha256
        ):
            raise ValueError("redaction manifest reference has incorrect source lineage")
        if (
            consent_reference.artifact_id != self.consent_decision.decision_id
            or consent_reference.source_id != self.consent_decision.consent_id
            or consent_reference.source_sha256 != self.consent_decision.consent_sha256
        ):
            raise ValueError("consent decision reference has incorrect source lineage")
        content_by_name = {item.content_name: item for item in self.manifest.content_digests}
        field_by_path = {item.json_path: item for item in self.redaction_manifest.entries}
        for name in _PORTABLE_ROOTS:
            retained = _canonical_json_bytes(evidence[name])
            field = field_by_path.get(f"/{name}")
            if field is None:
                raise ValueError(f"redaction manifest does not bind portable root {name}")
            if content_by_name[name].source_sha256 != field.source_sha256:
                raise ValueError(f"source content digest differs for {name}")
            if (
                content_by_name[name].retained_sha256
                != hashlib.sha256(retained).hexdigest()
                or field.output_sha256 != content_by_name[name].retained_sha256
            ):
                raise ValueError(f"retained content digest differs for {name}")
        sanitized = redact_document(
            self.evidence_bytes,
            document_id=self.manifest.redaction_document_id,
            document_kind=PortableEvidenceKindV1.PORTABLE_EVIDENCE,
            policy=self.manifest.redaction_policy,
        )
        if sanitized.output_bytes != self.evidence_bytes:
            raise ValueError("portable evidence contains content prohibited by its policy")
        if self.total_byte_count > _MAX_TOTAL_BYTES:
            raise ValueError("export directory exceeds its total byte bound")

    @property
    def export_id(self) -> str:
        return self.manifest.export_id

    @property
    def manifest_sha256(self) -> str:
        return self.manifest.sha256

    @property
    def inventory_sha256(self) -> str:
        return self.inventory.sha256

    @property
    def files(self) -> tuple[tuple[str, bytes], ...]:
        return (
            (CONSENT_DECISION_FILENAME, self.consent_decision.canonical_bytes()),
            (EVIDENCE_FILENAME, self.evidence_bytes),
            (INVENTORY_FILENAME, self.inventory.canonical_bytes()),
            (MANIFEST_FILENAME, self.manifest.canonical_bytes()),
            (REDACTION_MANIFEST_FILENAME, self.redaction_manifest.canonical_bytes()),
        )

    @property
    def total_byte_count(self) -> int:
        return sum(len(payload) for _, payload in self.files)


@dataclass(frozen=True, slots=True)
class ImportedExportV1(_CanonicalRecordV1):
    export_id: str
    relative_directory: str
    manifest_sha256: str
    inventory_sha256: str

    schema_id: ClassVar[str] = IMPORTED_EXPORT_SCHEMA_ID
    schema_version: ClassVar[int] = EXPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.export_id, "imported export ID")
        _portable_relative_path(self.relative_directory, "imported export directory")
        if self.relative_directory != f"{DataAreaId.EVIDENCE.value}/{self.export_id}":
            raise ValueError("imported export directory is outside its exact evidence slot")
        _sha256(self.manifest_sha256, "imported export manifest digest")
        _sha256(self.inventory_sha256, "imported export inventory digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "export_id": self.export_id,
            "inventory_sha256": self.inventory_sha256,
            "manifest_sha256": self.manifest_sha256,
            "relative_directory": self.relative_directory,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> ImportedExportV1:
        payload = _fields(
            value,
            frozenset(
                {
                    "export_id",
                    "inventory_sha256",
                    "manifest_sha256",
                    "relative_directory",
                    "schema_id",
                    "schema_version",
                }
            ),
            "imported export",
        )
        if payload["schema_id"] != cls.schema_id or payload["schema_version"] != 1:
            raise ValueError("imported export schema differs")
        return cls(
            export_id=_identifier(payload["export_id"], "imported export ID"),
            relative_directory=_portable_relative_path(
                payload["relative_directory"], "imported export directory"
            ),
            manifest_sha256=_sha256(
                payload["manifest_sha256"], "imported export manifest digest"
            ),
            inventory_sha256=_sha256(
                payload["inventory_sha256"], "imported export inventory digest"
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ImportedExportV1:
        restored = cls.from_dict(_canonical_object(raw, "imported export"))
        if restored.canonical_bytes() != raw:
            raise ValueError("imported export changed during restoration")
        return restored


def _lineage_reference(
    source_kind: ExportLineageKindV1,
    source_id: str,
    source_sha256: str,
) -> ExportLineageReferenceV1:
    return ExportLineageReferenceV1(
        source_kind=source_kind,
        source_id=source_id,
        source_sha256=source_sha256,
    )


def _canonical_lineage(
    references: tuple[ExportLineageReferenceV1, ...],
) -> tuple[ExportLineageReferenceV1, ...]:
    return tuple(
        sorted(
            references,
            key=lambda item: (item.source_kind.value, item.source_id, item.source_sha256),
        )
    )


def _canonical_exact_tuple(
    value: object,
    expected_type: type,
    label: str,
    *,
    allow_empty: bool = False,
    maximum: int = _MAX_REPEATED_RECORDS,
) -> tuple:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    if not allow_empty and not value:
        raise ValueError(f"{label} cannot be empty")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bounded count")
    if any(type(item) is not expected_type for item in value):
        raise TypeError(f"{label} contains an invalid value")
    return value


def build_export_bundle(
    *,
    current_consent: ConsentRecordV1,
    required_scope: ConsentScopeV1,
    decision_time_utc: str,
    assignment: AssignmentRevisionV1,
    attempt_manifest: AssignmentAttemptManifestV1,
    study_revision: StudyRevisionV1,
    study_ledger: StudyExecutionLedgerV1,
    scores: tuple[RubricScoreSidecarV1, ...],
    reviews: tuple[ReviewRevisionV1, ...],
    selected_causal_traces: tuple[SelectedCausalTraceV1, ...],
    compatibility_versions: tuple[VersionSignatureV1, ...],
    software_version: str,
    limitations: tuple[str, ...],
    redaction_policy: RedactionPolicyV1,
    omissions: tuple[ExportOmissionV1, ...] = (),
) -> EvidenceExportBundleV1:
    """Build one consent-authorized, allowlist-only portable evidence directory.

    The returned value is only an immutable collection of exact bytes.  This pure
    builder does not create a directory or register anything under ``DataPaths``.
    """

    if type(current_consent) is not ConsentRecordV1:
        raise TypeError("export consent head must be ConsentRecordV1")
    if type(required_scope) is not ConsentScopeV1:
        raise TypeError("export required scope must be ConsentScopeV1")
    _utc(decision_time_utc, "export decision time")
    if type(assignment) is not AssignmentRevisionV1:
        raise TypeError("export assignment must be AssignmentRevisionV1")
    if type(attempt_manifest) is not AssignmentAttemptManifestV1:
        raise TypeError("export attempt must be AssignmentAttemptManifestV1")
    if type(study_revision) is not StudyRevisionV1:
        raise TypeError("export study revision must be StudyRevisionV1")
    if type(study_ledger) is not StudyExecutionLedgerV1:
        raise TypeError("export study ledger must be StudyExecutionLedgerV1")
    if type(redaction_policy) is not RedactionPolicyV1:
        raise TypeError("export redaction policy must be RedactionPolicyV1")
    if redaction_policy.allowlisted_paths != _REQUIRED_ALLOWLIST_PATHS:
        raise ValueError(
            "export policy must allowlist the eight exact portable evidence roots"
        )
    _public_text(software_version, "export software version", maximum=256)
    if (
        attempt_manifest.assignment_revision.assignment_id != assignment.assignment_id
        or attempt_manifest.assignment_revision.sha256 != assignment.sha256
    ):
        raise ValueError("export attempt does not bind the exact assignment revision")
    complete_export_scopes = frozenset(
        {
            ConsentScopeV1.INSTRUCTIONAL_EVIDENCE,
            ConsentScopeV1.INSTRUCTOR_REVIEW,
            ConsentScopeV1.LOCAL_RESEARCH_STUDY,
        }
    )
    if not complete_export_scopes.issubset(current_consent.scopes):
        missing = sorted(scope.value for scope in complete_export_scopes - set(current_consent.scopes))
        raise PermissionError(
            "evidence export consent lacks required multi-purpose scopes: "
            + ",".join(missing)
        )
    if (
        study_ledger.study_revision.study_id != study_revision.study_id
        or study_ledger.study_revision.sha256 != study_revision.sha256
    ):
        raise ValueError("export ledger does not pin the exact study revision")
    if (
        study_ledger.protocol_lock.study_id != study_revision.study_id
        or study_ledger.protocol_lock.study_revision_sha256 != study_revision.sha256
        or study_ledger.protocol_lock.manifest_sha256 != study_revision.manifest.sha256
    ):
        raise ValueError("export ledger protocol lock differs from the exact study")
    matching_study_attempt = next(
        (
            item
            for item in study_ledger.included_attempts
            if item.assignment_id == assignment.assignment_id
            and item.assignment_sha256 == assignment.sha256
            and item.attempt_id == attempt_manifest.attempt_id
            and item.attempt_sha256 == attempt_manifest.sha256
        ),
        None,
    )
    if matching_study_attempt is None:
        raise PermissionError(
            "export study ledger does not include the exact selected attempt"
        )
    if decision_time_utc < matching_study_attempt.included_at_utc:
        raise PermissionError("export decision predates the selected study inclusion")
    ledger_entry_times = tuple(
        item.included_at_utc
        if type(item) is StudyAttemptBindingV1
        else item.amended_at_utc
        if type(item) is StudyAmendmentV1
        else item.recorded_at_utc
        if type(item) is ProtocolDeviationV1
        else ""
        for item in study_ledger.entries
    )
    if ledger_entry_times and decision_time_utc < max(ledger_entry_times):
        raise PermissionError("export decision predates committed study-ledger entries")
    study_export_policy = study_revision.manifest.data_export_policy
    if (
        study_export_policy.permission
        is not EvidenceExportPermissionV1.PSEUDONYMOUS_REDACTED_EVIDENCE_ONLY
        or study_export_policy.redaction_policy_sha256 != redaction_policy.sha256
    ):
        raise PermissionError(
            "locked study protocol does not authorize this exact redaction policy"
        )
    study_ledger_id = "study-execution-ledger-" + study_ledger.sha256

    decision = decide_evidence_export(
        current_consent,
        required_scope=required_scope,
        requested_export=EvidenceExportClassV1.PSEUDONYMOUS_REDACTED_EVIDENCE,
        decision_time_utc=decision_time_utc,
    )
    if not decision.allowed:
        raise PermissionError(
            "evidence export refused by consent evaluator: " + decision.reason.value
        )
    consent_decision = ExportConsentDecisionV1.from_decision(decision)
    if consent_decision.pseudonymous_profile_id != attempt_manifest.learner_profile_id:
        raise PermissionError("export consent belongs to a different pseudonymous profile")

    score_values = _canonical_exact_tuple(scores, RubricScoreSidecarV1, "export scores")
    score_values = tuple(sorted(score_values, key=lambda item: item.score_id))
    if len({item.score_id for item in score_values}) != len(score_values):
        raise ValueError("export scores cannot repeat")
    for score in score_values:
        if (
            score.assignment_attempt_id != attempt_manifest.attempt_id
            or score.assignment_attempt_sha256 != attempt_manifest.sha256
        ):
            raise ValueError("export score does not bind the exact attempt manifest")

    review_values = _canonical_exact_tuple(reviews, ReviewRevisionV1, "export reviews")
    review_values = tuple(sorted(review_values, key=lambda item: item.review_id))
    if len({item.review_id for item in review_values}) != len(review_values):
        raise ValueError("export reviews cannot repeat")
    for review in review_values:
        if (
            review.attempt_id != attempt_manifest.attempt_id
            or review.sidecar.attempt.attempt_sha256 != attempt_manifest.sha256
        ):
            raise ValueError("export review does not bind the exact attempt manifest")
        if not review.sidecar.completed:
            raise ValueError("export annotations require completed reviews")
        if not review.sidecar.timeline_annotations:
            raise ValueError("each exported review must contain a timeline annotation")

    trace_values = _canonical_exact_tuple(
        selected_causal_traces,
        SelectedCausalTraceV1,
        "selected causal traces",
        maximum=_MAX_TRACES,
    )
    trace_values = tuple(sorted(trace_values, key=lambda item: item.trace_id))
    trace_keys = tuple((item.trace_id, item.trace_sha256) for item in trace_values)
    if len({item.trace_id for item in trace_values}) != len(trace_values):
        raise ValueError("selected causal-trace IDs cannot repeat")
    review_trace_keys = {
        (
            review.sidecar.attempt.causal_trace_id,
            review.sidecar.attempt.causal_trace_sha256,
        )
        for review in review_values
    }
    if not set(trace_keys).issubset(review_trace_keys):
        raise ValueError("selected causal trace lacks an exact exported review binding")

    version_values = _canonical_exact_tuple(
        compatibility_versions,
        VersionSignatureV1,
        "export compatibility versions",
    )
    version_values = tuple(sorted(version_values, key=lambda item: item.canonical_bytes()))
    if len({item.signature_sha256 for item in version_values}) != len(version_values):
        raise ValueError("export compatibility versions cannot repeat")
    if type(limitations) is not tuple or not limitations:
        raise ValueError("export limitations must be a nonempty immutable tuple")
    limitation_values = tuple(
        sorted({_public_text(item, "export limitation") for item in limitations})
    )
    if len(limitation_values) != len(limitations):
        raise ValueError("export limitations cannot repeat")
    omission_values = _canonical_exact_tuple(
        omissions,
        ExportOmissionV1,
        "export omissions",
        allow_empty=True,
    )
    omission_values = tuple(sorted(omission_values, key=lambda item: item.canonical_bytes()))
    if len({(item.item_kind, item.item_id) for item in omission_values}) != len(
        omission_values
    ):
        raise ValueError("export omissions cannot repeat one item")

    lineage = _canonical_lineage(
        (
            _lineage_reference(
                ExportLineageKindV1.ASSIGNMENT_REVISION,
                assignment.assignment_id,
                assignment.sha256,
            ),
            _lineage_reference(
                ExportLineageKindV1.ATTEMPT_MANIFEST,
                attempt_manifest.attempt_id,
                attempt_manifest.sha256,
            ),
            _lineage_reference(
                ExportLineageKindV1.STUDY_REVISION,
                study_revision.study_id,
                study_revision.sha256,
            ),
            _lineage_reference(
                ExportLineageKindV1.STUDY_PROTOCOL_LOCK,
                study_ledger.protocol_lock.protocol_lock_id,
                study_ledger.protocol_lock.sha256,
            ),
            _lineage_reference(
                ExportLineageKindV1.STUDY_EXECUTION_LEDGER,
                study_ledger_id,
                study_ledger.sha256,
            ),
            *(
                _lineage_reference(
                    ExportLineageKindV1.RUBRIC_SCORE,
                    score.score_id,
                    score.sha256,
                )
                for score in score_values
            ),
            *(
                _lineage_reference(
                    ExportLineageKindV1.REVIEW_REVISION,
                    review.review_id,
                    review.sha256,
                )
                for review in review_values
            ),
            *(
                _lineage_reference(
                    ExportLineageKindV1.SELECTED_CAUSAL_TRACE,
                    trace.trace_id,
                    trace.trace_sha256,
                )
                for trace in trace_values
            ),
        )
    )
    provenance = {
        "authorized_consent_scopes": [
            item.value
            for item in sorted(set(current_consent.scopes), key=lambda item: item.value)
        ],
        "compatibility_versions": [item.as_dict() for item in version_values],
        "consent_decision": consent_decision.as_dict(),
        "lineage_references": [item.as_dict() for item in lineage],
        "redaction_policy": redaction_policy.as_dict(),
        "study_execution_ledger_id": study_ledger_id,
        "study_execution_ledger_sha256": study_ledger.sha256,
        "study_inclusion_witness": matching_study_attempt.as_dict(),
        "study_protocol_lock": study_ledger.protocol_lock.as_dict(),
        "study_revision": study_revision.as_dict(),
        "study_revision_sha256": study_revision.sha256,
    }
    source = {
        "annotations": [
            {
                "review_id": review.review_id,
                "review_lineage_id": review.lineage_id,
                "review_revision": review.revision,
                "review_sha256": review.sha256,
                "timeline_annotations": [
                    item.as_dict() for item in review.sidecar.timeline_annotations
                ],
            }
            for review in review_values
        ],
        "assignment": assignment.as_dict(),
        "attempt_manifest": attempt_manifest.as_dict(),
        "limitations": list(limitation_values),
        "provenance": provenance,
        "rubric_scores": [item.as_dict() for item in score_values],
        "selected_causal_traces": [item.as_dict() for item in trace_values],
        "software_version": {
            "compatibility_versions": [item.as_dict() for item in version_values],
            "software_version": software_version,
        },
    }
    source_bytes = _canonical_json_bytes(source)
    document_id = "portable-evidence-" + hashlib.sha256(source_bytes).hexdigest()
    redacted = redact_document(
        source_bytes,
        document_id=document_id,
        document_kind=PortableEvidenceKindV1.PORTABLE_EVIDENCE,
        policy=redaction_policy,
    )
    retained = _canonical_object(redacted.output_bytes, "redacted portable evidence")
    if tuple(sorted(retained)) != _PORTABLE_ROOTS:
        raise ValueError(
            "redaction policy must retain all eight required portable evidence roots"
        )
    sanitized = redact_document(
        redacted.output_bytes,
        document_id=document_id,
        document_kind=PortableEvidenceKindV1.PORTABLE_EVIDENCE,
        policy=redaction_policy,
    )
    if sanitized.output_bytes != redacted.output_bytes:
        raise ValueError("redacted portable evidence is not closed under its policy")

    content_digests = tuple(
        ExportContentDigestV1(
            content_name=name,
            source_sha256=hashlib.sha256(_canonical_json_bytes(source[name])).hexdigest(),
            retained_sha256=hashlib.sha256(
                _canonical_json_bytes(retained[name])
            ).hexdigest(),
        )
        for name in _PORTABLE_ROOTS
    )
    redaction_manifest_bytes = redacted.manifest.canonical_bytes()
    consent_bytes = consent_decision.canonical_bytes()
    references = tuple(
        sorted(
            (
                _artifact_reference(
                    artifact_kind=ExportArtifactKindV1.PORTABLE_EVIDENCE,
                    artifact_id=document_id,
                    relative_path=EVIDENCE_FILENAME,
                    payload=redacted.output_bytes,
                    source_id=document_id,
                    source_sha256=redacted.source_sha256,
                ),
                _artifact_reference(
                    artifact_kind=ExportArtifactKindV1.REDACTION_MANIFEST,
                    artifact_id="redaction-manifest-" + redacted.manifest.sha256,
                    relative_path=REDACTION_MANIFEST_FILENAME,
                    payload=redaction_manifest_bytes,
                    source_id=document_id,
                    source_sha256=redacted.source_sha256,
                ),
                _artifact_reference(
                    artifact_kind=ExportArtifactKindV1.CONSENT_DECISION,
                    artifact_id=consent_decision.decision_id,
                    relative_path=CONSENT_DECISION_FILENAME,
                    payload=consent_bytes,
                    source_id=current_consent.consent_id,
                    source_sha256=current_consent.consent_sha256,
                ),
            ),
            key=lambda item: item.relative_path,
        )
    )
    inventory = ExportInventoryV1(artifacts=references)
    manifest = EvidenceExportManifestV1(
        exported_at_utc=decision_time_utc,
        pseudonymous_profile_id=attempt_manifest.learner_profile_id,
        assignment_id=assignment.assignment_id,
        assignment_sha256=assignment.sha256,
        attempt_id=attempt_manifest.attempt_id,
        attempt_sha256=attempt_manifest.sha256,
        study_id=study_revision.study_id,
        study_revision_number=study_revision.study.revision,
        study_revision_sha256=study_revision.sha256,
        study_manifest_sha256=study_revision.manifest.sha256,
        protocol_lock_id=study_ledger.protocol_lock.protocol_lock_id,
        protocol_lock_sha256=study_ledger.protocol_lock.sha256,
        study_execution_ledger_id=study_ledger_id,
        study_execution_ledger_sha256=study_ledger.sha256,
        study_data_export_policy=study_export_policy,
        software_version=software_version,
        compatibility_versions=version_values,
        lineage_references=lineage,
        authorized_consent_scopes=tuple(
            sorted(set(current_consent.scopes), key=lambda item: item.value)
        ),
        consent_decision_id=consent_decision.decision_id,
        consent_decision_sha256=consent_decision.sha256,
        redaction_document_id=document_id,
        redaction_policy_id=redaction_policy.policy_id,
        redaction_policy_sha256=redaction_policy.sha256,
        redaction_policy=redaction_policy,
        redaction_manifest_sha256=redacted.manifest.sha256,
        inventory_id=inventory.inventory_id,
        inventory_sha256=inventory.sha256,
        retained_references=references,
        content_digests=content_digests,
        omissions=omission_values,
        limitations=limitation_values,
    )
    return EvidenceExportBundleV1(
        manifest=manifest,
        inventory=inventory,
        evidence_bytes=redacted.output_bytes,
        redaction_manifest=redacted.manifest,
        consent_decision=consent_decision,
    )


def _directory_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("evidence export requires no-follow directory descriptors")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _resolved_path(
    value: str | os.PathLike[str],
    label: str,
    *,
    must_exist: bool,
) -> Path:
    try:
        path = Path(value)
    except TypeError as error:
        raise TypeError(f"{label} must be a string or path-like value") from error
    if not path.is_absolute():
        raise ValueError(f"{label} must be explicit and absolute")
    try:
        resolved = path.resolve(strict=must_exist)
    except (FileNotFoundError, OSError, RuntimeError) as error:
        raise ValueError(f"{label} is missing or cannot be resolved safely") from error
    if resolved != path:
        raise ValueError(f"{label} must be supplied in already-resolved form")
    if resolved == Path(resolved.anchor):
        raise ValueError(f"{label} cannot be the filesystem anchor")
    return resolved


def _open_absolute_directory(path: Path, label: str) -> int:
    flags = _directory_flags()
    try:
        current = os.open(Path(path.anchor), flags)
    except OSError as error:
        raise ValueError(f"{label} filesystem anchor is unsafe") from error
    try:
        for component in path.parts[1:]:
            try:
                following = os.open(component, flags, dir_fd=current)
            except OSError as error:
                raise ValueError(f"{label} contains a missing or unsafe directory") from error
            previous = current
            current = following
            os.close(previous)
        metadata = os.fstat(current)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} is not a real directory")
        descriptor = current
        current = -1
        return descriptor
    finally:
        if current >= 0:
            os.close(current)


def _open_directory_at(parent_descriptor: int, name: str, label: str) -> int:
    if type(name) is not str or not name or "/" in name or "\\" in name:
        raise ValueError(f"{label} leaf name is invalid")
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except OSError as error:
        raise ValueError(f"{label} is missing or unsafe") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ValueError(f"{label} is not a real directory")
    return descriptor


def _open_data_area_from_root(
    paths: DataPaths,
    root_descriptor: int,
    area_id: DataAreaId,
) -> int:
    try:
        current = os.dup(root_descriptor)
    except OSError as error:
        raise ValueError("governed data root cannot be pinned") from error
    try:
        for component in PurePosixPath(paths.area_children[area_id]).parts:
            try:
                following = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current,
                )
            except OSError as error:
                raise ValueError(
                    f"governed {area_id.value} area is missing or unsafe"
                ) from error
            previous = current
            current = following
            os.close(previous)
        descriptor = current
        current = -1
        return descriptor
    finally:
        if current >= 0:
            os.close(current)


def _write_exclusive_at(
    directory_descriptor: int,
    filename: str,
    payload: bytes,
) -> None:
    if filename not in _EXPECTED_FILES:
        raise ValueError("export writer refused a file outside the closed inventory")
    if type(payload) is not bytes or not payload or len(payload) > _MAX_JSON_BYTES:
        raise ValueError("export file payload size is invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(filename, flags, 0o600, dir_fd=directory_descriptor)
    except FileExistsError as error:
        raise FileExistsError(f"export file already exists: {filename}") from error
    except OSError as error:
        raise ValueError(f"export file cannot be created safely: {filename}") from error
    complete = False
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise PermissionError("export file must be one owner-only regular file")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write while persisting export file")
            offset += written
        os.fsync(descriptor)
        complete = True
    finally:
        os.close(descriptor)
        if not complete:
            try:
                os.unlink(filename, dir_fd=directory_descriptor)
            except OSError:
                pass


def _read_regular_at(
    directory_descriptor: int,
    filename: str,
    expected_size: int,
) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
    except OSError as error:
        raise ValueError(f"export file is missing or unsafe: {filename}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"export file must be one regular file: {filename}")
        if metadata.st_size != expected_size or expected_size <= 0:
            raise ValueError(f"export file size changed during verification: {filename}")
        chunks: list[bytes] = []
        remaining = expected_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError(f"export file ended early: {filename}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"export file grew during verification: {filename}")
        raw = b"".join(chunks)
        if len(raw) != expected_size:
            raise ValueError(f"export file length differs: {filename}")
        return raw
    finally:
        os.close(descriptor)


def _read_closed_directory(directory_descriptor: int) -> dict[str, bytes]:
    try:
        scan_descriptor = os.open(".", _directory_flags(), dir_fd=directory_descriptor)
    except OSError as error:
        raise ValueError("export directory cannot be pinned for scanning") from error
    try:
        names_list = os.listdir(scan_descriptor)
        metadata_by_name = {
            name: os.stat(name, dir_fd=scan_descriptor, follow_symlinks=False)
            for name in names_list
        }
    except OSError as error:
        raise ValueError("export directory cannot be scanned safely") from error
    finally:
        os.close(scan_descriptor)
    names = set(names_list)
    if names != _EXPECTED_FILES:
        raise ValueError(
            "export directory inventory differs "
            f"missing={sorted(_EXPECTED_FILES - names)} "
            f"extra={sorted(names - _EXPECTED_FILES)}"
        )
    sizes: dict[str, int] = {}
    total = 0
    for name, metadata in metadata_by_name.items():
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise ValueError(f"export entry is not one regular file: {name}")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_JSON_BYTES:
            raise ValueError(f"export entry size is invalid: {name}")
        sizes[name] = metadata.st_size
        total += metadata.st_size
    if total > _MAX_TOTAL_BYTES:
        raise ValueError("export directory exceeds its total byte bound")
    return {
        name: _read_regular_at(directory_descriptor, name, sizes[name])
        for name in sorted(_EXPECTED_FILES)
    }


def _bundle_from_files(files: dict[str, bytes]) -> EvidenceExportBundleV1:
    if set(files) != _EXPECTED_FILES:
        raise ValueError("export byte inventory differs from the closed format")
    manifest = EvidenceExportManifestV1.from_canonical_bytes(files[MANIFEST_FILENAME])
    inventory = ExportInventoryV1.from_canonical_bytes(files[INVENTORY_FILENAME])
    redaction_manifest = RedactionManifestV1.from_canonical_bytes(
        files[REDACTION_MANIFEST_FILENAME]
    )
    consent_decision = ExportConsentDecisionV1.from_canonical_bytes(
        files[CONSENT_DECISION_FILENAME]
    )
    return EvidenceExportBundleV1(
        manifest=manifest,
        inventory=inventory,
        evidence_bytes=files[EVIDENCE_FILENAME],
        redaction_manifest=redaction_manifest,
        consent_decision=consent_decision,
    )


def verify_export_directory(
    directory: str | os.PathLike[str],
) -> EvidenceExportBundleV1:
    """Verify the exact five files without following any directory or file link."""

    path = _resolved_path(directory, "export directory", must_exist=True)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError("export directory is missing or unsafe") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("export directory must be one real directory")
    descriptor = _open_absolute_directory(path, "export directory")
    try:
        files = _read_closed_directory(descriptor)
    finally:
        os.close(descriptor)
    return _bundle_from_files(files)


def _cleanup_created_directory_at(
    parent_descriptor: int,
    name: str,
    directory_descriptor: int,
    written_names: tuple[str, ...],
) -> None:
    pinned = os.fstat(directory_descriptor)
    try:
        for filename in written_names:
            if filename in _EXPECTED_FILES:
                try:
                    os.unlink(filename, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
        os.fsync(directory_descriptor)
    except OSError:
        return
    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(named.st_mode)
            or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
        ):
            return
        os.rmdir(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except OSError:
        pass


def write_export_directory(
    bundle: EvidenceExportBundleV1,
    destination: str | os.PathLike[str],
) -> Path:
    """Exclusively write one bundle to a new, explicit, unpacked directory."""

    if type(bundle) is not EvidenceExportBundleV1:
        raise TypeError("export writer requires EvidenceExportBundleV1")
    # Re-run all byte and cross-record checks before touching the destination.
    bundle = EvidenceExportBundleV1(
        manifest=bundle.manifest,
        inventory=bundle.inventory,
        evidence_bytes=bundle.evidence_bytes,
        redaction_manifest=bundle.redaction_manifest,
        consent_decision=bundle.consent_decision,
    )
    path = _resolved_path(destination, "new export directory", must_exist=False)
    if path.exists() or path.is_symlink():
        raise FileExistsError("export destination already exists")
    parent = path.parent
    if not parent.exists():
        raise ValueError("export destination parent must already exist")
    parent_descriptor = _open_absolute_directory(parent, "export destination parent")
    written: list[str] = []
    directory_descriptor: int | None = None
    created = False
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent_descriptor)
            created = True
            os.fsync(parent_descriptor)
        except FileExistsError as error:
            raise FileExistsError("export destination already exists") from error
        except OSError as error:
            raise ValueError("export destination cannot be created safely") from error
        directory_descriptor = _open_directory_at(
            parent_descriptor, path.name, "new export directory"
        )
        os.fchmod(directory_descriptor, 0o700)
        if stat.S_IMODE(os.fstat(directory_descriptor).st_mode) != 0o700:
            raise PermissionError("export directory must use mode 0700")
        for filename, payload in bundle.files:
            _write_exclusive_at(directory_descriptor, filename, payload)
            written.append(filename)
        os.fsync(directory_descriptor)
    except BaseException:
        if directory_descriptor is not None:
            if created:
                _cleanup_created_directory_at(
                    parent_descriptor,
                    path.name,
                    directory_descriptor,
                    tuple(written),
                )
            os.close(directory_descriptor)
            directory_descriptor = None
        raise
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        os.close(parent_descriptor)
    restored = verify_export_directory(path)
    if restored.files != bundle.files:
        raise ValueError("written export bytes changed before verification")
    return path


def _remove_verified_pinned_directory_at(
    parent_descriptor: int,
    name: str,
    directory_descriptor: int,
    expected_bundle: EvidenceExportBundleV1,
) -> None:
    """Remove only exact expected bytes through one continuously pinned leaf fd."""

    try:
        pinned = os.fstat(directory_descriptor)
        observed = _bundle_from_files(_read_closed_directory(directory_descriptor))
        if observed.files != expected_bundle.files:
            return
        for filename in sorted(_EXPECTED_FILES):
            os.unlink(filename, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
        named = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(named.st_mode)
            or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
        ):
            return
        os.rmdir(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except Exception:
        return


def _remove_verified_directory_at(
    parent_descriptor: int,
    name: str,
    expected_bundle: EvidenceExportBundleV1,
) -> None:
    directory_descriptor: int | None = None
    try:
        directory_descriptor = _open_directory_at(
            parent_descriptor, name, "export staging directory"
        )
        _remove_verified_pinned_directory_at(
            parent_descriptor,
            name,
            directory_descriptor,
            expected_bundle,
        )
    except Exception:
        return
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)


def _remove_verified_staging_directory(
    path: Path,
    expected_bundle: EvidenceExportBundleV1,
) -> None:
    try:
        parent_descriptor = _open_absolute_directory(
            path.parent, "export staging parent"
        )
    except Exception:
        return
    try:
        _remove_verified_directory_at(
            parent_descriptor,
            path.name,
            expected_bundle,
        )
    finally:
        os.close(parent_descriptor)


def _acquire_registration_lock(staging_descriptor: int) -> int:
    lock_name = ".evidence-export-registration.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(lock_name, flags, 0o600, dir_fd=staging_descriptor)
    except OSError as error:
        raise ValueError("evidence export registration lock is unsafe") from error
    complete = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PermissionError(
                "evidence export registration lock must be one owner-only file"
            )
        pinned_identity = (metadata.st_dev, metadata.st_ino)
        os.fchmod(descriptor, 0o600)
        secured = os.fstat(descriptor)
        if (
            not stat.S_ISREG(secured.st_mode)
            or secured.st_nlink != 1
            or (secured.st_dev, secured.st_ino) != pinned_identity
            or stat.S_IMODE(secured.st_mode) != 0o600
        ):
            raise PermissionError(
                "evidence export registration lock must be one owner-only file"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.fsync(staging_descriptor)
        complete = True
        return descriptor
    finally:
        if not complete:
            os.close(descriptor)


def _require_absent_at(directory_descriptor: int, name: str, label: str) -> None:
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError(f"{label} cannot be inspected safely") from error
    raise FileExistsError(f"{label} already exists")


def import_export_directory(
    source_directory: str | os.PathLike[str],
    *,
    paths: DataPaths,
) -> ImportedExportV1:
    """Verify, stage, and atomically register an export under ``DataPaths.evidence``."""

    if type(paths) is not DataPaths:
        raise TypeError("export import requires an explicit DataPaths")
    # This verification intentionally precedes DataPaths.ensure: unverified input
    # cannot cause a governed data root or area to be created.
    source_bundle = verify_export_directory(source_directory)
    paths.ensure((DataAreaId.EVIDENCE, DataAreaId.STAGING))
    paths.validate((DataAreaId.EVIDENCE, DataAreaId.STAGING))

    stage_name = (
        ".evidence-import-"
        + source_bundle.export_id
        + "-"
        + os.urandom(12).hex()
    )
    stage_path = paths.staging / stage_name
    write_export_directory(source_bundle, stage_path)
    paths.validate((DataAreaId.EVIDENCE, DataAreaId.STAGING))
    root_descriptor = _open_absolute_directory(paths.root, "governed data root")
    staging_descriptor: int | None = None
    try:
        staging_descriptor = _open_data_area_from_root(
            paths,
            root_descriptor,
            DataAreaId.STAGING,
        )
        evidence_descriptor = _open_data_area_from_root(
            paths,
            root_descriptor,
            DataAreaId.EVIDENCE,
        )
    except BaseException:
        if staging_descriptor is not None:
            try:
                _remove_verified_directory_at(
                    staging_descriptor,
                    stage_name,
                    source_bundle,
                )
            finally:
                os.close(staging_descriptor)
        else:
            _remove_verified_staging_directory(stage_path, source_bundle)
        raise
    finally:
        os.close(root_descriptor)
    if staging_descriptor is None:  # pragma: no cover - guarded by area open
        raise RuntimeError("governed staging area was not pinned")
    stage_descriptor: int | None = None
    activated_descriptor: int | None = None
    lock_descriptor: int | None = None
    moved = False
    activated = False
    cleanup_name: str | None = stage_name
    registered: EvidenceExportBundleV1 | None = None
    try:
        stage_descriptor = _open_directory_at(
            staging_descriptor,
            stage_name,
            "verified export staging directory",
        )
        staged = _bundle_from_files(_read_closed_directory(stage_descriptor))
        if staged.files != source_bundle.files:
            raise ValueError("staged export differs from verified source bytes")
        pinned_stage = os.fstat(stage_descriptor)
        lock_descriptor = _acquire_registration_lock(staging_descriptor)
        _require_absent_at(
            evidence_descriptor,
            source_bundle.export_id,
            "evidence export registration",
        )
        named_stage = os.stat(
            stage_name,
            dir_fd=staging_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(named_stage.st_mode)
            or (named_stage.st_dev, named_stage.st_ino)
            != (pinned_stage.st_dev, pinned_stage.st_ino)
        ):
            raise ValueError("verified export staging name was rebound")
        try:
            os.rename(
                stage_name,
                source_bundle.export_id,
                src_dir_fd=staging_descriptor,
                dst_dir_fd=evidence_descriptor,
            )
        except OSError as error:
            raise ValueError("verified evidence export cannot be atomically activated") from error
        moved = True
        cleanup_name = None
        activated_descriptor = _open_directory_at(
            evidence_descriptor,
            source_bundle.export_id,
            "activated evidence export",
        )
        activated_metadata = os.fstat(activated_descriptor)
        if (activated_metadata.st_dev, activated_metadata.st_ino) != (
            pinned_stage.st_dev,
            pinned_stage.st_ino,
        ):
            raise ValueError("activated evidence export is not the verified staging inode")
        registered = _bundle_from_files(
            _read_closed_directory(activated_descriptor)
        )
        if registered.files != source_bundle.files:
            raise ValueError("activated evidence export differs from verified source bytes")
        os.fsync(activated_descriptor)
        os.fsync(staging_descriptor)
        os.fsync(evidence_descriptor)
        activated = True
    finally:
        try:
            if moved and not activated:
                quarantine_name = (
                    ".failed-evidence-import-"
                    + source_bundle.export_id
                    + "-"
                    + os.urandom(12).hex()
                )
                try:
                    named_final = os.stat(
                        source_bundle.export_id,
                        dir_fd=evidence_descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISDIR(named_final.st_mode)
                        or (named_final.st_dev, named_final.st_ino)
                        != (pinned_stage.st_dev, pinned_stage.st_ino)
                    ):
                        raise ValueError("failed export registration name was rebound")
                    os.rename(
                        source_bundle.export_id,
                        quarantine_name,
                        src_dir_fd=evidence_descriptor,
                        dst_dir_fd=staging_descriptor,
                    )
                    cleanup_name = quarantine_name
                    os.fsync(staging_descriptor)
                    os.fsync(evidence_descriptor)
                except (OSError, ValueError):
                    if activated_descriptor is not None:
                        try:
                            os.fchmod(activated_descriptor, 0)
                            os.fsync(activated_descriptor)
                        except OSError:
                            pass
            if not activated and cleanup_name is not None:
                if stage_descriptor is not None:
                    _remove_verified_pinned_directory_at(
                        staging_descriptor,
                        cleanup_name,
                        stage_descriptor,
                        source_bundle,
                    )
                else:
                    _remove_verified_directory_at(
                        staging_descriptor,
                        cleanup_name,
                        source_bundle,
                    )
        finally:
            # Closing the flock descriptor releases the advisory lock even after
            # interruption.  Attempt every close independently so one bad fd can
            # never leak the remaining pinned root generation.
            for descriptor in (
                activated_descriptor,
                stage_descriptor,
                lock_descriptor,
                staging_descriptor,
                evidence_descriptor,
            ):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    paths.validate((DataAreaId.EVIDENCE, DataAreaId.STAGING))
    if registered is None:  # pragma: no cover - guarded by activated state
        raise RuntimeError("evidence export activation produced no verified bundle")
    return ImportedExportV1(
        export_id=registered.export_id,
        relative_directory=f"{DataAreaId.EVIDENCE.value}/{registered.export_id}",
        manifest_sha256=registered.manifest_sha256,
        inventory_sha256=registered.inventory_sha256,
    )


def load_selected_causal_trace(raw: bytes) -> SelectedCausalTraceV1:
    return SelectedCausalTraceV1.from_canonical_bytes(raw)


def load_export_omission(raw: bytes) -> ExportOmissionV1:
    return ExportOmissionV1.from_canonical_bytes(raw)


def load_export_consent_decision(raw: bytes) -> ExportConsentDecisionV1:
    return ExportConsentDecisionV1.from_canonical_bytes(raw)


def load_export_artifact_reference(raw: bytes) -> ExportArtifactReferenceV1:
    return ExportArtifactReferenceV1.from_canonical_bytes(raw)


def load_export_inventory(raw: bytes) -> ExportInventoryV1:
    return ExportInventoryV1.from_canonical_bytes(raw)


def load_export_lineage_reference(raw: bytes) -> ExportLineageReferenceV1:
    return ExportLineageReferenceV1.from_canonical_bytes(raw)


def load_export_content_digest(raw: bytes) -> ExportContentDigestV1:
    return ExportContentDigestV1.from_canonical_bytes(raw)


def load_export_manifest(raw: bytes) -> EvidenceExportManifestV1:
    return EvidenceExportManifestV1.from_canonical_bytes(raw)


def load_imported_export(raw: bytes) -> ImportedExportV1:
    return ImportedExportV1.from_canonical_bytes(raw)


__all__ = [
    "CONSENT_DECISION_FILENAME",
    "EVIDENCE_EXPORT_MANIFEST_SCHEMA_ID",
    "EVIDENCE_FILENAME",
    "EXPORT_ARTIFACT_REFERENCE_SCHEMA_ID",
    "EXPORT_CONSENT_DECISION_SCHEMA_ID",
    "EXPORT_CONTENT_DIGEST_SCHEMA_ID",
    "EXPORT_FORMAT_V1",
    "EXPORT_INVENTORY_SCHEMA_ID",
    "EXPORT_LINEAGE_REFERENCE_SCHEMA_ID",
    "EXPORT_OMISSION_SCHEMA_ID",
    "EXPORT_PROHIBITED_CONTENT_POLICY_V1",
    "EXPORT_SCHEMA_VERSION",
    "IMPORTED_EXPORT_SCHEMA_ID",
    "INVENTORY_FILENAME",
    "MANIFEST_FILENAME",
    "REDACTION_MANIFEST_FILENAME",
    "SELECTED_CAUSAL_TRACE_SCHEMA_ID",
    "EvidenceExportBundleV1",
    "EvidenceExportManifestV1",
    "ExportArtifactKindV1",
    "ExportArtifactReferenceV1",
    "ExportConsentDecisionV1",
    "ExportContentDigestV1",
    "ExportInventoryV1",
    "ExportLineageKindV1",
    "ExportLineageReferenceV1",
    "ExportOmissionReasonV1",
    "ExportOmissionV1",
    "ImportedExportV1",
    "SelectedCausalTraceV1",
    "build_export_bundle",
    "create_selected_causal_trace",
    "import_export_directory",
    "load_export_artifact_reference",
    "load_export_consent_decision",
    "load_export_content_digest",
    "load_export_inventory",
    "load_export_lineage_reference",
    "load_export_manifest",
    "load_export_omission",
    "load_imported_export",
    "load_selected_causal_trace",
    "verify_export_directory",
    "write_export_directory",
]
