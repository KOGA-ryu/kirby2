"""Immutable content-addressed transfer contracts for WO38-C.

This module defines data only.  It carries logical content references, exact pack
transport bytes, and coordinator-verified result identities without serializing a
filesystem path, executable selector, timestamp, lease, attempt, or worker-routing
field.  Archive parsing, license decisions, staging, activation, and persistence are
performed by the existing pack boundary and the WO38-C content store.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, TypeVar

from kirby2.orchestration.models import DigestReferenceV1
from kirby2.orchestration.protocol import InlineArtifactMediaTypeV1
from kirby2.packs.archive import preflight_pack_archive_bytes
from kirby2.packs.formats import (
    K2PACK_MANIFEST_PATH,
    K2PACK_ZIP_COMPRESSLEVEL,
    K2PACK_ZIP_COMPRESSION,
    canonical_json_bytes,
    canonical_manifest_bytes,
    load_canonical_json_bytes,
    normalized_archive_paths,
    normalized_zip_info,
)
from kirby2.packs.identity import verify_pack_payload_identity
from kirby2.packs.models import PackManifestV1
from kirby2.packs.validation import (
    DEFAULT_PACK_VALIDATION_LIMITS_V1,
    PackValidationLimitsV1,
    require_validation_limits,
)


ARTIFACT_EXCHANGE_SCHEMA_VERSION = 1
CONTENT_REQUEST_SCHEMA_ID = "KIRBY2_CONTENT_REQUEST_V1"
PACK_TRANSFER_DESCRIPTOR_SCHEMA_ID = "KIRBY2_PACK_TRANSFER_DESCRIPTOR_V1"
PACK_TRANSFER_BUNDLE_SCHEMA_ID = "KIRBY2_PACK_TRANSFER_BUNDLE_V1"
RESULT_ARTIFACT_DESCRIPTOR_SCHEMA_ID = "KIRBY2_RESULT_ARTIFACT_DESCRIPTOR_V1"
RESULT_BUNDLE_MANIFEST_SCHEMA_ID = "KIRBY2_RESULT_BUNDLE_MANIFEST_V1"

PACK_REDISTRIBUTION_DECISION_REFERENCE_PREFIX_V1 = "pack-redistribution:"
MAX_CONTENT_REQUEST_REFERENCES_V1 = 4096
MAX_PACK_TRANSFER_ARCHIVE_BYTES_V1 = 256 * 1024 * 1024
MAX_RESULT_ARTIFACT_DESCRIPTORS_V1 = 4096
MAX_RUNTIME_AUDIT_REFERENCES_V1 = 4096
MAX_RESULT_ARTIFACT_BYTES_V1 = 512 * 1024 * 1024
MAX_ARTIFACT_METADATA_BYTES_V1 = 4 * 1024 * 1024
MAX_PACK_TRANSFER_BUNDLE_CANONICAL_BYTES_V1 = (
    4 * ((MAX_PACK_TRANSFER_ARCHIVE_BYTES_V1 + 2) // 3)
    + MAX_ARTIFACT_METADATA_BYTES_V1
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NON_PATH_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_EnumT = TypeVar("_EnumT", bound=Enum)


class TransferRefusalCodeV1(str, Enum):
    """Closed machine-stable reasons that content exchange can fail closed."""

    CONTENT_NOT_FOUND = "CONTENT_NOT_FOUND"
    CONTENT_NOT_REQUESTED = "CONTENT_NOT_REQUESTED"
    CONTENT_DIGEST_MISMATCH = "CONTENT_DIGEST_MISMATCH"
    PACK_ID_MISMATCH = "PACK_ID_MISMATCH"
    TRANSPORT_DIGEST_MISMATCH = "TRANSPORT_DIGEST_MISMATCH"
    BYTE_COUNT_MISMATCH = "BYTE_COUNT_MISMATCH"
    MANIFEST_DIGEST_MISMATCH = "MANIFEST_DIGEST_MISMATCH"
    INVENTORY_DIGEST_MISMATCH = "INVENTORY_DIGEST_MISMATCH"
    VALIDATION_POLICY_MISMATCH = "VALIDATION_POLICY_MISMATCH"
    PACK_VALIDATION_REFUSED = "PACK_VALIDATION_REFUSED"
    REDISTRIBUTION_REFUSED = "REDISTRIBUTION_REFUSED"
    TRANSFER_TOO_LARGE = "TRANSFER_TOO_LARGE"
    ARTIFACT_IDENTITY_MISMATCH = "ARTIFACT_IDENTITY_MISMATCH"
    RESULT_MANIFEST_MISMATCH = "RESULT_MANIFEST_MISMATCH"
    COORDINATOR_VERIFICATION_MISMATCH = "COORDINATOR_VERIFICATION_MISMATCH"


class ArtifactTransferRefused(ValueError):
    """Typed fail-closed transfer refusal; callers branch only on ``code``."""

    def __init__(self, code: TransferRefusalCodeV1, message: str) -> None:
        if type(code) is not TransferRefusalCodeV1:
            raise TypeError("artifact transfer refusal requires TransferRefusalCodeV1")
        _bounded_text(message, "artifact transfer refusal message", maximum_bytes=4096)
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


@dataclass(frozen=True, slots=True)
class ContentRequestV1:
    """One canonical set of requested content digests, never ambient paths."""

    content_references: tuple[DigestReferenceV1, ...]

    schema_id: ClassVar[str] = CONTENT_REQUEST_SCHEMA_ID
    schema_version: ClassVar[int] = ARTIFACT_EXCHANGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _canonical_references(
            self.content_references,
            "content request references",
            maximum_count=MAX_CONTENT_REQUEST_REFERENCES_V1,
            unique_digests=True,
            require_non_path_names=True,
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "content_references": [
                item.as_dict() for item in self.content_references
            ],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @property
    def content_request_id(self) -> str:
        return _canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_dict(),
            "content_request_id": self.content_request_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> ContentRequestV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "content_references",
                    "content_request_id",
                    "schema_id",
                    "schema_version",
                }
            ),
            "content request",
        )
        _require_schema(payload, cls.schema_id, "content request")
        declared = _require_sha256(
            payload["content_request_id"],
            "declared content request ID",
        )
        restored = cls(
            content_references=_references_from_dict(
                payload["content_references"],
                "content request references",
            )
        )
        if not hmac.compare_digest(declared, restored.content_request_id):
            raise ValueError("declared content request ID differs from exact references")
        _require_exact_round_trip(restored, payload, "content request")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: object) -> ContentRequestV1:
        payload = _load_canonical_record(
            raw,
            "content request",
            maximum_bytes=MAX_ARTIFACT_METADATA_BYTES_V1,
        )
        restored = cls.from_dict(payload)
        _require_exact_bytes_round_trip(restored, raw, "content request")
        return restored


@dataclass(frozen=True, slots=True)
class PackTransferDescriptorV1:
    """Exact logical, transport, validation, and redistribution decision identity."""

    pack_id: str
    transport_sha256: str
    byte_count: int
    manifest_sha256: str
    inventory_sha256: str
    validation_policy_id: str
    redistribution_decision_identity: DigestReferenceV1

    schema_id: ClassVar[str] = PACK_TRANSFER_DESCRIPTOR_SCHEMA_ID
    schema_version: ClassVar[int] = ARTIFACT_EXCHANGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_sha256(self.pack_id, "transfer logical pack ID")
        _require_sha256(self.transport_sha256, "transfer transport digest")
        _require_sha256(self.manifest_sha256, "transfer manifest digest")
        _require_sha256(self.inventory_sha256, "transfer inventory digest")
        _require_sha256(self.validation_policy_id, "transfer validation-policy ID")
        _require_positive_count(
            self.byte_count,
            "transfer archive byte count",
            maximum=MAX_PACK_TRANSFER_ARCHIVE_BYTES_V1,
        )
        if type(self.redistribution_decision_identity) is not DigestReferenceV1:
            raise TypeError(
                "transfer redistribution decision must be DigestReferenceV1"
            )
        if (
            self.redistribution_decision_identity.name
            != f"{PACK_REDISTRIBUTION_DECISION_REFERENCE_PREFIX_V1}{self.pack_id}"
        ):
            raise ValueError(
                "transfer redistribution decision uses an unsupported identity name"
            )

    def identity_dict(self) -> dict[str, object]:
        return {
            "byte_count": self.byte_count,
            "inventory_sha256": self.inventory_sha256,
            "manifest_sha256": self.manifest_sha256,
            "pack_id": self.pack_id,
            "redistribution_decision_identity": (
                self.redistribution_decision_identity.as_dict()
            ),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "transport_sha256": self.transport_sha256,
            "validation_policy_id": self.validation_policy_id,
        }

    @property
    def descriptor_sha256(self) -> str:
        return _canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_dict(),
            "descriptor_sha256": self.descriptor_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> PackTransferDescriptorV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "byte_count",
                    "descriptor_sha256",
                    "inventory_sha256",
                    "manifest_sha256",
                    "pack_id",
                    "redistribution_decision_identity",
                    "schema_id",
                    "schema_version",
                    "transport_sha256",
                    "validation_policy_id",
                }
            ),
            "pack transfer descriptor",
        )
        _require_schema(payload, cls.schema_id, "pack transfer descriptor")
        declared = _require_sha256(
            payload["descriptor_sha256"],
            "declared pack transfer descriptor digest",
        )
        restored = cls(
            pack_id=_exact_text(payload, "pack_id"),
            transport_sha256=_exact_text(payload, "transport_sha256"),
            byte_count=_exact_integer(payload, "byte_count"),
            manifest_sha256=_exact_text(payload, "manifest_sha256"),
            inventory_sha256=_exact_text(payload, "inventory_sha256"),
            validation_policy_id=_exact_text(payload, "validation_policy_id"),
            redistribution_decision_identity=DigestReferenceV1.from_dict(
                payload["redistribution_decision_identity"]
            ),
        )
        if not hmac.compare_digest(declared, restored.descriptor_sha256):
            raise ValueError(
                "declared pack transfer descriptor digest differs from exact content"
            )
        _require_exact_round_trip(restored, payload, "pack transfer descriptor")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: object) -> PackTransferDescriptorV1:
        payload = _load_canonical_record(
            raw,
            "pack transfer descriptor",
            maximum_bytes=MAX_ARTIFACT_METADATA_BYTES_V1,
        )
        restored = cls.from_dict(payload)
        _require_exact_bytes_round_trip(restored, raw, "pack transfer descriptor")
        return restored


@dataclass(frozen=True, slots=True)
class PackTransferBundleV1:
    """One bounded exact archive paired with its path-free transfer descriptor."""

    descriptor: PackTransferDescriptorV1
    archive_bytes: bytes

    schema_id: ClassVar[str] = PACK_TRANSFER_BUNDLE_SCHEMA_ID
    schema_version: ClassVar[int] = ARTIFACT_EXCHANGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.descriptor) is not PackTransferDescriptorV1:
            raise TypeError("pack transfer bundle requires PackTransferDescriptorV1")
        raw = _require_exact_bytes(self.archive_bytes, "pack transfer archive bytes")
        if not raw or len(raw) > MAX_PACK_TRANSFER_ARCHIVE_BYTES_V1:
            raise ValueError("pack transfer archive bytes are empty or exceed the limit")
        if len(raw) != self.descriptor.byte_count:
            raise ValueError("pack transfer archive byte count differs from descriptor")
        actual_transport = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(
            actual_transport,
            self.descriptor.transport_sha256,
        ):
            raise ValueError("pack transfer archive digest differs from descriptor")

    @property
    def pack_id(self) -> str:
        return self.descriptor.pack_id

    @property
    def byte_count(self) -> int:
        return len(self.archive_bytes)

    @property
    def transport_sha256(self) -> str:
        return hashlib.sha256(self.archive_bytes).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "archive_base64": base64.b64encode(self.archive_bytes).decode("ascii"),
            "descriptor": self.descriptor.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def bundle_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> PackTransferBundleV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "archive_base64",
                    "descriptor",
                    "schema_id",
                    "schema_version",
                }
            ),
            "pack transfer bundle",
        )
        _require_schema(payload, cls.schema_id, "pack transfer bundle")
        descriptor = PackTransferDescriptorV1.from_dict(payload["descriptor"])
        raw = _strict_base64(
            _exact_text(payload, "archive_base64"),
            "pack transfer archive",
            maximum_bytes=MAX_PACK_TRANSFER_ARCHIVE_BYTES_V1,
        )
        restored = cls(descriptor=descriptor, archive_bytes=raw)
        _require_exact_round_trip(restored, payload, "pack transfer bundle")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: object) -> PackTransferBundleV1:
        payload = _load_canonical_record(
            raw,
            "pack transfer bundle",
            maximum_bytes=MAX_PACK_TRANSFER_BUNDLE_CANONICAL_BYTES_V1,
        )
        restored = cls.from_dict(payload)
        _require_exact_bytes_round_trip(restored, raw, "pack transfer bundle")
        return restored


@dataclass(frozen=True, slots=True)
class ResultArtifactDescriptorV1:
    """Content identity and schema contract for one verified result artifact."""

    artifact_id: str
    media_type: InlineArtifactMediaTypeV1
    schema_identity: DigestReferenceV1
    byte_count: int
    sha256: str

    schema_id: ClassVar[str] = RESULT_ARTIFACT_DESCRIPTOR_SCHEMA_ID
    schema_version: ClassVar[int] = ARTIFACT_EXCHANGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_artifact_id(self.artifact_id)
        if type(self.media_type) is not InlineArtifactMediaTypeV1:
            raise TypeError("result artifact media type must be V1")
        if type(self.schema_identity) is not DigestReferenceV1:
            raise TypeError("result artifact schema identity must be DigestReferenceV1")
        _require_non_path_reference(
            self.schema_identity,
            "result artifact schema identity",
        )
        _require_positive_count(
            self.byte_count,
            "result artifact byte count",
            maximum=MAX_RESULT_ARTIFACT_BYTES_V1,
        )
        _require_sha256(self.sha256, "result artifact digest")

    def identity_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "byte_count": self.byte_count,
            "media_type": self.media_type.value,
            "schema_id": self.schema_id,
            "schema_identity": self.schema_identity.as_dict(),
            "schema_version": self.schema_version,
            "sha256": self.sha256,
        }

    @property
    def descriptor_sha256(self) -> str:
        return _canonical_sha256(self.identity_dict())

    @property
    def digest_reference(self) -> DigestReferenceV1:
        return DigestReferenceV1(name=self.artifact_id, sha256=self.sha256)

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.artifact_id, self.sha256)

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_dict(),
            "descriptor_sha256": self.descriptor_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> ResultArtifactDescriptorV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "artifact_id",
                    "byte_count",
                    "descriptor_sha256",
                    "media_type",
                    "schema_id",
                    "schema_identity",
                    "schema_version",
                    "sha256",
                }
            ),
            "result artifact descriptor",
        )
        _require_schema(payload, cls.schema_id, "result artifact descriptor")
        declared = _require_sha256(
            payload["descriptor_sha256"],
            "declared result artifact descriptor digest",
        )
        restored = cls(
            artifact_id=_exact_text(payload, "artifact_id"),
            media_type=_enum_value(
                InlineArtifactMediaTypeV1,
                payload["media_type"],
                "result artifact media type",
            ),
            schema_identity=DigestReferenceV1.from_dict(payload["schema_identity"]),
            byte_count=_exact_integer(payload, "byte_count"),
            sha256=_exact_text(payload, "sha256"),
        )
        if not hmac.compare_digest(declared, restored.descriptor_sha256):
            raise ValueError(
                "declared result artifact descriptor digest differs from exact content"
            )
        _require_exact_round_trip(restored, payload, "result artifact descriptor")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: object) -> ResultArtifactDescriptorV1:
        payload = _load_canonical_record(
            raw,
            "result artifact descriptor",
            maximum_bytes=MAX_ARTIFACT_METADATA_BYTES_V1,
        )
        restored = cls.from_dict(payload)
        _require_exact_bytes_round_trip(restored, raw, "result artifact descriptor")
        return restored


@dataclass(frozen=True, slots=True)
class ResultBundleManifestV1:
    """Coordinator-produced post-verification registration manifest.

    This is never a worker-emitted or worker-self-reported record.  The coordinator
    constructs it only after independently verifying exact artifact bytes, worker
    compatibility, runtime-audit results, and the verification record named by
    ``coordinator_verification_sha256``.
    """

    work_request_id: str
    logical_work_unit_id: str
    worker_compatibility_sha256: str
    coordinator_verification_sha256: str
    artifacts: tuple[ResultArtifactDescriptorV1, ...]
    runtime_audit_results: tuple[DigestReferenceV1, ...]

    schema_id: ClassVar[str] = RESULT_BUNDLE_MANIFEST_SCHEMA_ID
    schema_version: ClassVar[int] = ARTIFACT_EXCHANGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_sha256(self.work_request_id, "result bundle work request ID")
        _require_sha256(
            self.logical_work_unit_id,
            "result bundle logical work unit ID",
        )
        _require_sha256(
            self.worker_compatibility_sha256,
            "result bundle worker compatibility digest",
        )
        _require_sha256(
            self.coordinator_verification_sha256,
            "result bundle coordinator verification digest",
        )
        _canonical_artifact_descriptors(self.artifacts)
        _canonical_references(
            self.runtime_audit_results,
            "result bundle runtime audit references",
            maximum_count=MAX_RUNTIME_AUDIT_REFERENCES_V1,
            unique_digests=False,
            require_non_path_names=True,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifacts": [item.as_dict() for item in self.artifacts],
            "coordinator_verification_sha256": (
                self.coordinator_verification_sha256
            ),
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
        return canonical_json_bytes(self.as_dict())

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> ResultBundleManifestV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "artifacts",
                    "coordinator_verification_sha256",
                    "logical_work_unit_id",
                    "runtime_audit_results",
                    "schema_id",
                    "schema_version",
                    "work_request_id",
                    "worker_compatibility_sha256",
                }
            ),
            "result bundle manifest",
        )
        _require_schema(payload, cls.schema_id, "result bundle manifest")
        restored = cls(
            work_request_id=_exact_text(payload, "work_request_id"),
            logical_work_unit_id=_exact_text(payload, "logical_work_unit_id"),
            worker_compatibility_sha256=_exact_text(
                payload,
                "worker_compatibility_sha256",
            ),
            coordinator_verification_sha256=_exact_text(
                payload,
                "coordinator_verification_sha256",
            ),
            artifacts=tuple(
                ResultArtifactDescriptorV1.from_dict(item)
                for item in _exact_array(
                    payload["artifacts"],
                    "result bundle artifacts",
                )
            ),
            runtime_audit_results=_references_from_dict(
                payload["runtime_audit_results"],
                "result bundle runtime audit references",
            ),
        )
        _require_exact_round_trip(restored, payload, "result bundle manifest")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: object) -> ResultBundleManifestV1:
        payload = _load_canonical_record(
            raw,
            "result bundle manifest",
            maximum_bytes=MAX_ARTIFACT_METADATA_BYTES_V1,
        )
        restored = cls.from_dict(payload)
        _require_exact_bytes_round_trip(restored, raw, "result bundle manifest")
        return restored


def build_normalized_pack_archive(
    manifest: PackManifestV1,
    payloads: Mapping[str, bytes],
    redistribution_decision_identity: DigestReferenceV1,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
) -> PackTransferBundleV1:
    """Build and fully preflight one deterministic in-memory ``.k2pack`` transport.

    Mapping keys are manifest-declared archive member identifiers, not ambient
    filesystem locations.  No path is retained in the returned transfer contract.
    """

    if type(manifest) is not PackManifestV1:
        raise TypeError("normalized pack construction requires PackManifestV1")
    require_validation_limits(limits)
    _require_redistribution_decision_identity(
        manifest.pack_id,
        redistribution_decision_identity,
    )
    if not isinstance(payloads, Mapping):
        raise TypeError("normalized pack payloads must be a mapping")
    snapshot = dict(payloads)
    if any(type(path) is not str or type(raw) is not bytes for path, raw in snapshot.items()):
        raise TypeError("normalized pack payloads require exact text keys and bytes")

    verified_payload = verify_pack_payload_identity(manifest, snapshot)
    manifest_bytes = canonical_manifest_bytes(manifest)
    expanded_count = len(manifest_bytes) + verified_payload.total_byte_count
    if expanded_count > limits.maximum_total_expanded_bytes:
        raise ArtifactTransferRefused(
            TransferRefusalCodeV1.TRANSFER_TOO_LARGE,
            "pack manifest and payload bytes exceed the validation policy",
        )

    member_bytes = {K2PACK_MANIFEST_PATH: manifest_bytes, **snapshot}
    member_order = normalized_archive_paths(tuple(member_bytes))
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=K2PACK_ZIP_COMPRESSION,
        compresslevel=K2PACK_ZIP_COMPRESSLEVEL,
        allowZip64=False,
        strict_timestamps=True,
    ) as archive:
        for member_id in member_order:
            info = normalized_zip_info(member_id)
            archive.writestr(
                info,
                member_bytes[member_id],
                compress_type=K2PACK_ZIP_COMPRESSION,
                compresslevel=K2PACK_ZIP_COMPRESSLEVEL,
            )
    archive_bytes = buffer.getvalue()
    if len(archive_bytes) > MAX_PACK_TRANSFER_ARCHIVE_BYTES_V1:
        raise ArtifactTransferRefused(
            TransferRefusalCodeV1.TRANSFER_TOO_LARGE,
            "normalized pack archive exceeds the transfer hard limit",
        )

    transport_digest = hashlib.sha256(archive_bytes).hexdigest()
    preflight = preflight_pack_archive_bytes(
        archive_bytes,
        limits=limits,
        expected_pack_id=manifest.pack_id,
        expected_transport_sha256=transport_digest,
    )
    if (
        preflight.manifest != manifest
        or preflight.pack_id != verified_payload.pack_id
        or preflight.inventory_sha256 != verified_payload.inventory_sha256
        or preflight.archive_byte_count != len(archive_bytes)
        or preflight.transport_sha256 != transport_digest
    ):
        raise ArtifactTransferRefused(
            TransferRefusalCodeV1.PACK_VALIDATION_REFUSED,
            "post-build pack preflight differs from exact construction inputs",
        )

    descriptor = PackTransferDescriptorV1(
        pack_id=preflight.pack_id,
        transport_sha256=preflight.transport_sha256,
        byte_count=preflight.archive_byte_count,
        manifest_sha256=preflight.manifest_sha256,
        inventory_sha256=preflight.inventory_sha256,
        validation_policy_id=preflight.validation_policy_id,
        redistribution_decision_identity=redistribution_decision_identity,
    )
    return PackTransferBundleV1(
        descriptor=descriptor,
        archive_bytes=archive_bytes,
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _require_positive_count(value: object, label: str, *, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer in [1, {maximum}]")
    return value


def _bounded_text(value: object, label: str, *, maximum_bytes: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty text without edge whitespace")
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
        raise TypeError(f"{label} must be immutable exact bytes")
    return value


def _require_artifact_id(value: object) -> str:
    if (
        type(value) is not str
        or _ARTIFACT_ID.fullmatch(value) is None
        or value in {".", ".."}
    ):
        raise ValueError("result artifact ID must be one non-path canonical name")
    return value


def _require_redistribution_decision_identity(
    pack_id: str,
    value: object,
) -> DigestReferenceV1:
    _require_sha256(pack_id, "redistribution decision pack ID")
    if type(value) is not DigestReferenceV1:
        raise TypeError("redistribution decision identity must be DigestReferenceV1")
    expected_name = f"{PACK_REDISTRIBUTION_DECISION_REFERENCE_PREFIX_V1}{pack_id}"
    if value.name != expected_name:
        raise ValueError("redistribution decision identity names a different pack")
    return value


def _require_non_path_reference(value: DigestReferenceV1, label: str) -> None:
    if type(value) is not DigestReferenceV1:
        raise TypeError(f"{label} must be DigestReferenceV1")
    name = value.name
    if (
        _NON_PATH_IDENTIFIER.fullmatch(name) is None
        or name in {".", ".."}
        or name.casefold().startswith("file:")
        or (len(name) >= 2 and name[0].isalpha() and name[1] == ":")
    ):
        raise ValueError(f"{label} name must be a canonical non-path identifier")


def _canonical_references(
    values: tuple[DigestReferenceV1, ...],
    label: str,
    *,
    maximum_count: int,
    unique_digests: bool,
    require_non_path_names: bool,
) -> None:
    if type(values) is not tuple or not values or any(
        type(item) is not DigestReferenceV1 for item in values
    ):
        raise TypeError(f"{label} must be a nonempty immutable reference tuple")
    if len(values) > maximum_count:
        raise ValueError(f"{label} exceeds its count limit")
    if values != tuple(sorted(values, key=lambda item: item.sort_key)):
        raise ValueError(f"{label} must use canonical name/digest order")
    names = tuple(item.name for item in values)
    if len(names) != len(set(names)):
        raise ValueError(f"{label} cannot contain duplicate names")
    if unique_digests:
        digests = tuple(item.sha256 for item in values)
        if len(digests) != len(set(digests)):
            raise ValueError(f"{label} cannot request the same digest more than once")
    if require_non_path_names:
        for item in values:
            _require_non_path_reference(item, label)


def _canonical_artifact_descriptors(
    values: tuple[ResultArtifactDescriptorV1, ...],
) -> None:
    if type(values) is not tuple or not values or any(
        type(item) is not ResultArtifactDescriptorV1 for item in values
    ):
        raise TypeError(
            "result bundle artifacts must be a nonempty immutable descriptor tuple"
        )
    if len(values) > MAX_RESULT_ARTIFACT_DESCRIPTORS_V1:
        raise ValueError("result bundle artifact count exceeds its limit")
    if values != tuple(sorted(values, key=lambda item: item.sort_key)):
        raise ValueError("result bundle artifacts must use canonical artifact-ID order")
    names = tuple(item.artifact_id for item in values)
    if len(names) != len(set(names)):
        raise ValueError("result bundle artifacts cannot contain duplicate IDs")
    digests = tuple(item.sha256 for item in values)
    if len(digests) != len(set(digests)):
        raise ValueError(
            "result bundle artifacts cannot bind one digest to multiple IDs"
        )


def _references_from_dict(value: object, label: str) -> tuple[DigestReferenceV1, ...]:
    return tuple(
        DigestReferenceV1.from_dict(item)
        for item in _exact_array(value, label)
    )


def _exact_object(
    value: object,
    expected: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"serialized {label} must be one exact object")
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
        raise TypeError(f"serialized {label} must be one exact array")
    return value


def _exact_text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TypeError(f"serialized {key} must be exact text")
    return value


def _exact_integer(payload: dict[str, object], key: str) -> int:
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
    payload: dict[str, object],
    schema_id: str,
    label: str,
) -> None:
    if (
        type(payload["schema_id"]) is not str
        or payload["schema_id"] != schema_id
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != ARTIFACT_EXCHANGE_SCHEMA_VERSION
    ):
        raise ValueError(f"serialized {label} schema differs from the V1 contract")


def _strict_base64(encoded: str, label: str, *, maximum_bytes: int) -> bytes:
    maximum_encoded = 4 * ((maximum_bytes + 2) // 3)
    if len(encoded) > maximum_encoded:
        raise ValueError(f"serialized {label} exceeds its byte limit")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as error:
        raise ValueError(f"serialized {label} is not strict base64") from error
    if not raw or len(raw) > maximum_bytes:
        raise ValueError(f"serialized {label} is empty or exceeds its byte limit")
    if base64.b64encode(raw).decode("ascii") != encoded:
        raise ValueError(f"serialized {label} base64 is not canonical")
    return raw


def _load_canonical_record(
    raw: object,
    label: str,
    *,
    maximum_bytes: int,
) -> object:
    payload = _require_exact_bytes(raw, f"serialized {label}")
    if not payload or len(payload) > maximum_bytes:
        raise ValueError(f"serialized {label} is empty or exceeds its byte limit")
    return load_canonical_json_bytes(payload, label)


def _require_exact_round_trip(
    record: object,
    payload: dict[str, object],
    label: str,
) -> None:
    as_dict = getattr(record, "as_dict", None)
    if not callable(as_dict) or as_dict() != payload:
        raise ValueError(f"serialized {label} did not round-trip exactly")


def _require_exact_bytes_round_trip(
    record: object,
    raw: object,
    label: str,
) -> None:
    canonical = getattr(record, "canonical_bytes", None)
    if not callable(canonical) or canonical() != raw:
        raise ValueError(f"serialized {label} bytes did not round-trip exactly")


__all__ = [
    "ARTIFACT_EXCHANGE_SCHEMA_VERSION",
    "CONTENT_REQUEST_SCHEMA_ID",
    "MAX_ARTIFACT_METADATA_BYTES_V1",
    "MAX_CONTENT_REQUEST_REFERENCES_V1",
    "MAX_PACK_TRANSFER_ARCHIVE_BYTES_V1",
    "MAX_PACK_TRANSFER_BUNDLE_CANONICAL_BYTES_V1",
    "MAX_RESULT_ARTIFACT_BYTES_V1",
    "MAX_RESULT_ARTIFACT_DESCRIPTORS_V1",
    "MAX_RUNTIME_AUDIT_REFERENCES_V1",
    "PACK_REDISTRIBUTION_DECISION_REFERENCE_PREFIX_V1",
    "PACK_TRANSFER_BUNDLE_SCHEMA_ID",
    "PACK_TRANSFER_DESCRIPTOR_SCHEMA_ID",
    "RESULT_ARTIFACT_DESCRIPTOR_SCHEMA_ID",
    "RESULT_BUNDLE_MANIFEST_SCHEMA_ID",
    "ArtifactTransferRefused",
    "ContentRequestV1",
    "PackTransferBundleV1",
    "PackTransferDescriptorV1",
    "ResultArtifactDescriptorV1",
    "ResultBundleManifestV1",
    "TransferRefusalCodeV1",
    "build_normalized_pack_archive",
]
