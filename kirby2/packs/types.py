"""Exact domain-artifact contracts for WO39-D portable training packs.

The pack layer is a container, never a replacement identity system.  Every domain
artifact therefore retains both its exact original-byte digest and the logical
identity assigned by its owning subsystem.  ``DomainPackIndexV1`` commits to those
identities without allowing archive paths or storage encodings to become scientific
identity.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from .formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    require_data_identifier,
    require_nfc_text,
    require_relative_pack_path,
    require_sha256,
)
from .models import (
    PackContentFormatV1,
    PackCreatorV1,
    PackDependencyV1,
    PackLicenseV1,
    PackTypeV1,
)


PACK_SOURCE_DEFINITION_SCHEMA_ID = "KIRBY2_PACK_SOURCE_DEFINITION_V1"
PACK_SOURCE_DEFINITION_SCHEMA_VERSION = 1
DOMAIN_PACK_INDEX_SCHEMA_ID = "KIRBY2_DOMAIN_PACK_INDEX_V1"
DOMAIN_PACK_INDEX_SCHEMA_VERSION = 1
PRESERVED_EXACT_BYTES_SCHEMA_ID = "KIRBY2_PRESERVED_EXACT_BYTES_V1"
PRESERVED_EXACT_BYTES_SCHEMA_VERSION = 1

MAX_DOMAIN_ARTIFACT_COUNT_V1 = 4096
MAX_ORIGINAL_ARTIFACT_BYTES_V1 = 64 * 1024 * 1024
MAX_DOMAIN_TOTAL_BYTES_V1 = 256 * 1024 * 1024


class DomainPackRefusalCodeV1(str, Enum):
    """Stable, explicit builder/adapter refusal reasons."""

    UNSUPPORTED_PACK_TYPE = "UNSUPPORTED_PACK_TYPE"
    SOURCE_DEFINITION_INVALID = "SOURCE_DEFINITION_INVALID"
    SOURCE_PATH_UNSAFE = "SOURCE_PATH_UNSAFE"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
    ARTIFACT_INVENTORY_INVALID = "ARTIFACT_INVENTORY_INVALID"
    ARTIFACT_IDENTITY_MISMATCH = "ARTIFACT_IDENTITY_MISMATCH"
    ARTIFACT_FORMAT_INVALID = "ARTIFACT_FORMAT_INVALID"
    MISSING_REQUIRED_ARTIFACT = "MISSING_REQUIRED_ARTIFACT"
    REVEAL_POLICY_VIOLATION = "REVEAL_POLICY_VIOLATION"
    SCENARIO_IDENTITY_MISMATCH = "SCENARIO_IDENTITY_MISMATCH"
    STRATEGY_IDENTITY_MISMATCH = "STRATEGY_IDENTITY_MISMATCH"
    PROFILE_STATUS_INVALID = "PROFILE_STATUS_INVALID"
    LICENSE_CONTENT_REFUSED = "LICENSE_CONTENT_REFUSED"
    ARCHIVE_BUILD_FAILED = "ARCHIVE_BUILD_FAILED"
    DOMAIN_INDEX_INVALID = "DOMAIN_INDEX_INVALID"
    PACK_TYPE_MISMATCH = "PACK_TYPE_MISMATCH"


class DomainPackRefused(ValueError):
    """Typed fail-closed refusal emitted by a domain pack adapter."""

    def __init__(self, code: DomainPackRefusalCodeV1, detail: str) -> None:
        if type(code) is not DomainPackRefusalCodeV1:
            raise TypeError("domain pack refusal requires DomainPackRefusalCodeV1")
        require_nfc_text(detail, "domain pack refusal detail", maximum_bytes=4096)
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


class PackArtifactStorageModeV1(str, Enum):
    """Whether already-canonical bytes travel directly or in a data-only envelope."""

    DIRECT = "DIRECT"
    EXACT_BYTES_ENVELOPE = "EXACT_BYTES_ENVELOPE"


class PackArtifactRoleV1(str, Enum):
    """Closed WO39-D1 artifact vocabulary.

    Runs and audits are deliberately shared optional roles.  Their owning schemas
    and logical identities remain recorded by each artifact row.
    """

    SCENARIO_SOURCE = "SCENARIO_SOURCE"
    SCENARIO_COMPILED = "SCENARIO_COMPILED"
    SCENARIO_VALIDATION = "SCENARIO_VALIDATION"
    SCENARIO_CAPABILITIES = "SCENARIO_CAPABILITIES"

    LESSON_SOURCE = "LESSON_SOURCE"
    LESSON_DETECTOR = "LESSON_DETECTOR"
    LESSON_CAPABILITIES = "LESSON_CAPABILITIES"
    LESSON_OBSERVABLE_POLICY = "LESSON_OBSERVABLE_POLICY"
    LESSON_REVEAL_POLICY = "LESSON_REVEAL_POLICY"
    LESSON_SKILLS = "LESSON_SKILLS"
    LESSON_SCORING = "LESSON_SCORING"
    LESSON_REVIEW_SIDECAR = "LESSON_REVIEW_SIDECAR"

    CURRICULUM_SOURCE = "CURRICULUM_SOURCE"
    CURRICULUM_DETECTOR = "CURRICULUM_DETECTOR"
    CURRICULUM_CAPABILITIES = "CURRICULUM_CAPABILITIES"
    CURRICULUM_OBSERVABLE_POLICY = "CURRICULUM_OBSERVABLE_POLICY"
    CURRICULUM_REVEAL_POLICY = "CURRICULUM_REVEAL_POLICY"
    CURRICULUM_SKILLS = "CURRICULUM_SKILLS"
    CURRICULUM_SCORING = "CURRICULUM_SCORING"
    CURRICULUM_REVIEW_SIDECAR = "CURRICULUM_REVIEW_SIDECAR"

    STRATEGY_LEGACY_SOURCE = "STRATEGY_LEGACY_SOURCE"
    STRATEGY_CANONICAL_AST = "STRATEGY_CANONICAL_AST"
    STRATEGY_EXPERIMENT_LINEAGE = "STRATEGY_EXPERIMENT_LINEAGE"

    MARKET_PROFILE = "MARKET_PROFILE"
    PROFILE_PREREGISTRATION = "PROFILE_PREREGISTRATION"
    PROFILE_REVIEW_STATUS = "PROFILE_REVIEW_STATUS"

    EMBEDDED_RUN = "EMBEDDED_RUN"
    EMBEDDED_AUDIT = "EMBEDDED_AUDIT"


@dataclass(frozen=True, slots=True)
class PackSourceArtifactV1:
    """One confined source entry before its exact bytes are captured."""

    artifact_id: str
    role: PackArtifactRoleV1
    source_path: str
    original_schema_id: str
    original_schema_version: int
    original_media_type: str
    storage_mode: PackArtifactStorageModeV1
    logical_identity_kind: str
    logical_identity_sha256: str | None = None
    direct_content_format: PackContentFormatV1 | None = None

    def __post_init__(self) -> None:
        require_data_identifier(self.artifact_id, "pack source artifact ID")
        if type(self.role) is not PackArtifactRoleV1:
            raise TypeError("pack source artifact role is invalid")
        require_relative_pack_path(self.source_path, "pack source artifact path")
        require_data_identifier(
            self.original_schema_id,
            "pack source artifact schema ID",
        )
        if (
            type(self.original_schema_version) is not int
            or self.original_schema_version <= 0
        ):
            raise ValueError("pack source artifact schema version must be positive")
        require_nfc_text(
            self.original_media_type,
            "pack source artifact media type",
            maximum_bytes=128,
        )
        if type(self.storage_mode) is not PackArtifactStorageModeV1:
            raise TypeError("pack source artifact storage mode is invalid")
        require_data_identifier(
            self.logical_identity_kind,
            "pack source artifact logical identity kind",
        )
        if self.logical_identity_sha256 is not None:
            require_sha256(
                self.logical_identity_sha256,
                "pack source artifact logical identity",
            )
        if self.storage_mode is PackArtifactStorageModeV1.DIRECT:
            if type(self.direct_content_format) is not PackContentFormatV1:
                raise TypeError("direct pack source artifact requires a content format")
        elif self.direct_content_format is not None:
            raise ValueError(
                "exact-byte envelopes cannot declare a direct pack content format"
            )

    @classmethod
    def from_dict(cls, value: object) -> PackSourceArtifactV1:
        payload = _object(value, "pack source artifact")
        required = {
            "artifact_id",
            "logical_identity_kind",
            "original_media_type",
            "original_schema_id",
            "original_schema_version",
            "role",
            "source_path",
            "storage_mode",
        }
        optional = {"content_format", "logical_identity_sha256"}
        _require_fields(payload, required, optional, "pack source artifact")
        try:
            role = PackArtifactRoleV1(_text(payload, "role"))
            storage_mode = PackArtifactStorageModeV1(
                _text(payload, "storage_mode")
            )
        except ValueError as error:
            raise ValueError("pack source artifact uses an unsupported enum") from error
        raw_format = payload.get("content_format")
        content_format: PackContentFormatV1 | None = None
        if raw_format is not None:
            if type(raw_format) is not str:
                raise TypeError("pack source artifact content format must be text")
            try:
                content_format = PackContentFormatV1(raw_format)
            except ValueError as error:
                raise ValueError("pack source artifact content format is unsupported") from error
        raw_identity = payload.get("logical_identity_sha256")
        if raw_identity is not None and type(raw_identity) is not str:
            raise TypeError("pack source artifact logical identity must be text")
        return cls(
            artifact_id=_text(payload, "artifact_id"),
            role=role,
            source_path=_text(payload, "source_path"),
            original_schema_id=_text(payload, "original_schema_id"),
            original_schema_version=_integer(payload, "original_schema_version"),
            original_media_type=_text(payload, "original_media_type"),
            storage_mode=storage_mode,
            logical_identity_kind=_text(payload, "logical_identity_kind"),
            logical_identity_sha256=raw_identity,
            direct_content_format=content_format,
        )


@dataclass(frozen=True, slots=True)
class PackBuildSpecificationV1:
    """Complete typed authoring declaration consumed by the generic builder."""

    namespace: str
    name: str
    title: str
    version: str
    creator: PackCreatorV1
    pack_type: PackTypeV1
    primary_artifact_id: str
    dependencies: tuple[PackDependencyV1, ...]
    license: PackLicenseV1
    capability_labels: tuple[str, ...]
    artifacts: tuple[PackSourceArtifactV1, ...]

    schema_id: ClassVar[str] = PACK_SOURCE_DEFINITION_SCHEMA_ID
    schema_version: ClassVar[int] = PACK_SOURCE_DEFINITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Reuse the strict manifest contracts for the shared fields by importing the
        # small validators rather than defining a second grammar.
        from .formats import require_namespace, require_pack_name, require_semver

        require_namespace(self.namespace)
        require_pack_name(self.name)
        require_nfc_text(self.title, "pack build title", maximum_bytes=512)
        require_semver(self.version, "pack build version")
        if type(self.creator) is not PackCreatorV1:
            raise TypeError("pack build creator is invalid")
        if type(self.pack_type) is not PackTypeV1:
            raise TypeError("pack build type is invalid")
        require_data_identifier(
            self.primary_artifact_id,
            "pack build primary artifact ID",
        )
        if type(self.dependencies) is not tuple or any(
            type(item) is not PackDependencyV1 for item in self.dependencies
        ):
            raise TypeError("pack build dependencies must be a typed tuple")
        if tuple(sorted(self.dependencies, key=lambda item: item.sort_key)) != self.dependencies:
            raise ValueError("pack build dependencies must use canonical order")
        if type(self.license) is not PackLicenseV1:
            raise TypeError("pack build license is invalid")
        if type(self.capability_labels) is not tuple:
            raise TypeError("pack build capability labels must be a tuple")
        for label in self.capability_labels:
            require_data_identifier(label, "pack build capability label")
        if self.capability_labels != tuple(sorted(set(self.capability_labels))):
            raise ValueError("pack build capability labels must be sorted and unique")
        if (
            type(self.artifacts) is not tuple
            or not self.artifacts
            or any(type(item) is not PackSourceArtifactV1 for item in self.artifacts)
        ):
            raise TypeError("pack build artifacts must be a nonempty typed tuple")
        if len(self.artifacts) > MAX_DOMAIN_ARTIFACT_COUNT_V1:
            raise ValueError("pack build artifact count exceeds the V1 bound")
        if tuple(sorted(self.artifacts, key=lambda item: item.artifact_id)) != self.artifacts:
            raise ValueError("pack build artifacts must use canonical artifact-ID order")
        artifact_ids = tuple(item.artifact_id for item in self.artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("pack build artifact IDs must be unique")
        source_paths = tuple(item.source_path for item in self.artifacts)
        if len(source_paths) != len(set(source_paths)):
            raise ValueError("pack build source paths must be unique")
        if self.primary_artifact_id not in set(artifact_ids):
            raise ValueError("pack build primary artifact is absent from its inventory")

    @classmethod
    def from_dict(cls, value: object) -> PackBuildSpecificationV1:
        payload = _object(value, "pack source definition")
        required = {
            "artifacts",
            "capability_labels",
            "creator",
            "license",
            "name",
            "namespace",
            "pack_type",
            "primary_artifact_id",
            "schema_id",
            "schema_version",
            "title",
            "version",
        }
        optional = {"dependencies"}
        _require_fields(payload, required, optional, "pack source definition")
        if payload["schema_id"] != cls.schema_id:
            raise ValueError("pack source definition schema ID is unsupported")
        if payload["schema_version"] != cls.schema_version:
            raise ValueError("pack source definition schema version is unsupported")
        raw_artifacts = _array(payload, "artifacts")
        raw_capabilities = _array(payload, "capability_labels")
        raw_dependencies = payload.get("dependencies", [])
        if type(raw_dependencies) is not list:
            raise TypeError("pack source dependencies must be an array")
        if any(type(item) is not str for item in raw_capabilities):
            raise TypeError("pack source capability labels must be strings")
        try:
            pack_type = PackTypeV1(_text(payload, "pack_type"))
        except ValueError as error:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.UNSUPPORTED_PACK_TYPE,
                "pack source definition requests an unsupported pack type",
            ) from error
        raw_creator = _object(payload["creator"], "pack source creator")
        if set(raw_creator) == {"display_name", "identity_uri"}:
            creator = PackCreatorV1(
                display_name=_text(raw_creator, "display_name"),
                identity_uri=_text(raw_creator, "identity_uri"),
            )
        else:
            creator = PackCreatorV1.from_dict(raw_creator)
        return cls(
            namespace=_text(payload, "namespace"),
            name=_text(payload, "name"),
            title=_text(payload, "title"),
            version=_text(payload, "version"),
            creator=creator,
            pack_type=pack_type,
            primary_artifact_id=_text(payload, "primary_artifact_id"),
            dependencies=tuple(
                sorted(
                    (PackDependencyV1.from_dict(item) for item in raw_dependencies),
                    key=lambda item: item.sort_key,
                )
            ),
            license=PackLicenseV1.from_dict(payload["license"]),
            capability_labels=tuple(sorted(raw_capabilities)),
            artifacts=tuple(
                sorted(
                    (PackSourceArtifactV1.from_dict(item) for item in raw_artifacts),
                    key=lambda item: item.artifact_id,
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class DomainArtifactIdentityV1:
    """One preserved artifact identity bound to a pack payload member."""

    artifact_id: str
    role: PackArtifactRoleV1
    payload_path: str
    original_path: str
    original_schema_id: str
    original_schema_version: int
    original_media_type: str
    original_byte_count: int
    original_sha256: str
    logical_identity_kind: str
    logical_identity_sha256: str
    storage_mode: PackArtifactStorageModeV1

    def __post_init__(self) -> None:
        require_data_identifier(self.artifact_id, "domain artifact ID")
        if type(self.role) is not PackArtifactRoleV1:
            raise TypeError("domain artifact role is invalid")
        require_relative_pack_path(self.payload_path, "domain artifact payload path")
        require_relative_pack_path(self.original_path, "domain artifact original path")
        require_data_identifier(self.original_schema_id, "domain artifact schema ID")
        if type(self.original_schema_version) is not int or self.original_schema_version <= 0:
            raise ValueError("domain artifact schema version must be positive")
        require_nfc_text(
            self.original_media_type,
            "domain artifact original media type",
            maximum_bytes=128,
        )
        if (
            type(self.original_byte_count) is not int
            or self.original_byte_count <= 0
            or self.original_byte_count > MAX_ORIGINAL_ARTIFACT_BYTES_V1
        ):
            raise ValueError("domain artifact byte count is outside the V1 bound")
        require_sha256(self.original_sha256, "domain artifact exact-byte digest")
        require_data_identifier(
            self.logical_identity_kind,
            "domain artifact logical identity kind",
        )
        require_sha256(
            self.logical_identity_sha256,
            "domain artifact logical identity",
        )
        if type(self.storage_mode) is not PackArtifactStorageModeV1:
            raise TypeError("domain artifact storage mode is invalid")

    @property
    def sort_key(self) -> str:
        return self.artifact_id

    def domain_identity_dict(self) -> dict[str, object]:
        """Identity projection independent of pack path and storage encoding."""

        return {
            "artifact_id": self.artifact_id,
            "logical_identity_kind": self.logical_identity_kind,
            "logical_identity_sha256": self.logical_identity_sha256,
            "original_byte_count": self.original_byte_count,
            "original_media_type": self.original_media_type,
            "original_path": self.original_path,
            "original_schema_id": self.original_schema_id,
            "original_schema_version": self.original_schema_version,
            "original_sha256": self.original_sha256,
            "role": self.role.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.domain_identity_dict(),
            "payload_path": self.payload_path,
            "storage_mode": self.storage_mode.value,
        }

    @classmethod
    def from_dict(cls, value: object) -> DomainArtifactIdentityV1:
        payload = _exact_object(
            value,
            {
                "artifact_id",
                "logical_identity_kind",
                "logical_identity_sha256",
                "original_byte_count",
                "original_media_type",
                "original_path",
                "original_schema_id",
                "original_schema_version",
                "original_sha256",
                "payload_path",
                "role",
                "storage_mode",
            },
            "domain artifact identity",
        )
        try:
            role = PackArtifactRoleV1(_text(payload, "role"))
            mode = PackArtifactStorageModeV1(_text(payload, "storage_mode"))
        except ValueError as error:
            raise ValueError("domain artifact identity uses an unsupported enum") from error
        restored = cls(
            artifact_id=_text(payload, "artifact_id"),
            role=role,
            payload_path=_text(payload, "payload_path"),
            original_path=_text(payload, "original_path"),
            original_schema_id=_text(payload, "original_schema_id"),
            original_schema_version=_integer(payload, "original_schema_version"),
            original_media_type=_text(payload, "original_media_type"),
            original_byte_count=_integer(payload, "original_byte_count"),
            original_sha256=_text(payload, "original_sha256"),
            logical_identity_kind=_text(payload, "logical_identity_kind"),
            logical_identity_sha256=_text(payload, "logical_identity_sha256"),
            storage_mode=mode,
        )
        if restored.as_dict() != payload:
            raise ValueError("domain artifact identity did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PreservedExactBytesV1:
    """Canonical JSON transport for exact bytes whose native format is not allowed."""

    artifact_id: str
    original_media_type: str
    original_schema_id: str
    original_schema_version: int
    original_sha256: str
    logical_identity_kind: str
    logical_identity_sha256: str
    exact_bytes: bytes

    schema_id: ClassVar[str] = PRESERVED_EXACT_BYTES_SCHEMA_ID
    schema_version: ClassVar[int] = PRESERVED_EXACT_BYTES_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_data_identifier(self.artifact_id, "preserved artifact ID")
        require_nfc_text(
            self.original_media_type,
            "preserved artifact media type",
            maximum_bytes=128,
        )
        require_data_identifier(self.original_schema_id, "preserved artifact schema ID")
        if type(self.original_schema_version) is not int or self.original_schema_version <= 0:
            raise ValueError("preserved artifact schema version must be positive")
        require_sha256(self.original_sha256, "preserved artifact byte digest")
        require_data_identifier(
            self.logical_identity_kind,
            "preserved artifact logical identity kind",
        )
        require_sha256(
            self.logical_identity_sha256,
            "preserved artifact logical identity",
        )
        if (
            type(self.exact_bytes) is not bytes
            or not self.exact_bytes
            or len(self.exact_bytes) > MAX_ORIGINAL_ARTIFACT_BYTES_V1
        ):
            raise ValueError("preserved artifact exact bytes are outside the V1 bound")
        if not hmac.compare_digest(
            hashlib.sha256(self.exact_bytes).hexdigest(),
            self.original_sha256,
        ):
            raise ValueError("preserved artifact digest differs from its exact bytes")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "exact_bytes_base64": base64.b64encode(self.exact_bytes).decode("ascii"),
            "logical_identity_kind": self.logical_identity_kind,
            "logical_identity_sha256": self.logical_identity_sha256,
            "original_byte_count": len(self.exact_bytes),
            "original_media_type": self.original_media_type,
            "original_schema_id": self.original_schema_id,
            "original_schema_version": self.original_schema_version,
            "original_sha256": self.original_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_canonical_bytes(cls, raw: object) -> PreservedExactBytesV1:
        if type(raw) is not bytes:
            raise TypeError("preserved exact-byte envelope requires bytes")
        payload = _exact_object(
            load_canonical_json_bytes(raw, "preserved exact-byte envelope"),
            {
                "artifact_id",
                "exact_bytes_base64",
                "logical_identity_kind",
                "logical_identity_sha256",
                "original_byte_count",
                "original_media_type",
                "original_schema_id",
                "original_schema_version",
                "original_sha256",
                "schema_id",
                "schema_version",
            },
            "preserved exact-byte envelope",
        )
        if payload["schema_id"] != cls.schema_id or payload["schema_version"] != cls.schema_version:
            raise ValueError("preserved exact-byte envelope schema is unsupported")
        encoded = _text(payload, "exact_bytes_base64")
        try:
            exact = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as error:
            raise ValueError("preserved exact-byte payload is not canonical base64") from error
        if base64.b64encode(exact).decode("ascii") != encoded:
            raise ValueError("preserved exact-byte payload is not canonical base64")
        if len(exact) != _integer(payload, "original_byte_count"):
            raise ValueError("preserved exact-byte count differs from decoded bytes")
        restored = cls(
            artifact_id=_text(payload, "artifact_id"),
            original_media_type=_text(payload, "original_media_type"),
            original_schema_id=_text(payload, "original_schema_id"),
            original_schema_version=_integer(payload, "original_schema_version"),
            original_sha256=_text(payload, "original_sha256"),
            logical_identity_kind=_text(payload, "logical_identity_kind"),
            logical_identity_sha256=_text(payload, "logical_identity_sha256"),
            exact_bytes=exact,
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("preserved exact-byte envelope did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class DomainPackIndexV1:
    """Self-verifying domain index whose identity does not absorb pack mechanics."""

    pack_type: PackTypeV1
    adapter_id: str
    adapter_version: int
    primary_artifact_id: str
    artifacts: tuple[DomainArtifactIdentityV1, ...]

    schema_id: ClassVar[str] = DOMAIN_PACK_INDEX_SCHEMA_ID
    schema_version: ClassVar[int] = DOMAIN_PACK_INDEX_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.pack_type) is not PackTypeV1:
            raise TypeError("domain pack index type is invalid")
        require_data_identifier(self.adapter_id, "domain pack adapter ID")
        if type(self.adapter_version) is not int or self.adapter_version <= 0:
            raise ValueError("domain pack adapter version must be positive")
        require_data_identifier(
            self.primary_artifact_id,
            "domain pack primary artifact ID",
        )
        if (
            type(self.artifacts) is not tuple
            or not self.artifacts
            or any(type(item) is not DomainArtifactIdentityV1 for item in self.artifacts)
        ):
            raise TypeError("domain pack artifacts must be a nonempty typed tuple")
        if len(self.artifacts) > MAX_DOMAIN_ARTIFACT_COUNT_V1:
            raise ValueError("domain pack artifact count exceeds the V1 bound")
        if tuple(sorted(self.artifacts, key=lambda item: item.sort_key)) != self.artifacts:
            raise ValueError("domain pack artifacts must use canonical artifact-ID order")
        ids = tuple(item.artifact_id for item in self.artifacts)
        paths = tuple(item.payload_path for item in self.artifacts)
        if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
            raise ValueError("domain pack artifact IDs and payload paths must be unique")
        if self.primary_artifact_id not in set(ids):
            raise ValueError("domain pack primary artifact is absent")
        if sum(item.original_byte_count for item in self.artifacts) > MAX_DOMAIN_TOTAL_BYTES_V1:
            raise ValueError("domain pack original artifacts exceed the total V1 bound")

    def identity_dict(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "artifacts": [item.domain_identity_dict() for item in self.artifacts],
            "pack_type": self.pack_type.value,
            "primary_artifact_id": self.primary_artifact_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @property
    def domain_identity_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.identity_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "artifacts": [item.as_dict() for item in self.artifacts],
            "domain_identity_sha256": self.domain_identity_sha256,
            "pack_type": self.pack_type.value,
            "primary_artifact_id": self.primary_artifact_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_canonical_bytes(cls, raw: object) -> DomainPackIndexV1:
        if type(raw) is not bytes:
            raise TypeError("domain pack index requires exact bytes")
        payload = _exact_object(
            load_canonical_json_bytes(raw, "domain pack index"),
            {
                "adapter_id",
                "adapter_version",
                "artifacts",
                "domain_identity_sha256",
                "pack_type",
                "primary_artifact_id",
                "schema_id",
                "schema_version",
            },
            "domain pack index",
        )
        if payload["schema_id"] != cls.schema_id or payload["schema_version"] != cls.schema_version:
            raise ValueError("domain pack index schema is unsupported")
        raw_artifacts = _array(payload, "artifacts")
        try:
            pack_type = PackTypeV1(_text(payload, "pack_type"))
        except ValueError as error:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.UNSUPPORTED_PACK_TYPE,
                "domain pack index requests an unsupported pack type",
            ) from error
        restored = cls(
            pack_type=pack_type,
            adapter_id=_text(payload, "adapter_id"),
            adapter_version=_integer(payload, "adapter_version"),
            primary_artifact_id=_text(payload, "primary_artifact_id"),
            artifacts=tuple(
                DomainArtifactIdentityV1.from_dict(item) for item in raw_artifacts
            ),
        )
        declared = require_sha256(
            payload["domain_identity_sha256"],
            "declared domain pack identity",
        )
        if not hmac.compare_digest(declared, restored.domain_identity_sha256):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.DOMAIN_INDEX_INVALID,
                "domain pack identity differs from preserved artifact identities",
            )
        if restored.canonical_bytes() != raw:
            raise ValueError("domain pack index did not round-trip exactly")
        return restored

    def artifact(self, role: PackArtifactRoleV1) -> DomainArtifactIdentityV1:
        selected = tuple(item for item in self.artifacts if item.role is role)
        if len(selected) != 1:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
                f"role {role.value} requires exactly one artifact",
            )
        return selected[0]

    def artifacts_for(self, role: PackArtifactRoleV1) -> tuple[DomainArtifactIdentityV1, ...]:
        return tuple(item for item in self.artifacts if item.role is role)


@dataclass(frozen=True, slots=True)
class DomainPackAdapterContractV1:
    """Data-only declaration consumed by the common adapter validator."""

    pack_type: PackTypeV1
    adapter_id: str
    adapter_version: int
    compiler_component_id: str
    compiler_version: str
    required_roles: tuple[PackArtifactRoleV1, ...]
    allowed_roles: tuple[PackArtifactRoleV1, ...]
    multiple_roles: tuple[PackArtifactRoleV1, ...]
    primary_roles: tuple[PackArtifactRoleV1, ...]
    supports_replay_equivalence: bool

    def __post_init__(self) -> None:
        from .formats import require_semver

        if type(self.pack_type) is not PackTypeV1:
            raise TypeError("domain adapter pack type is invalid")
        require_data_identifier(self.adapter_id, "domain adapter ID")
        if type(self.adapter_version) is not int or self.adapter_version <= 0:
            raise ValueError("domain adapter version must be positive")
        require_data_identifier(
            self.compiler_component_id,
            "domain adapter compiler component ID",
        )
        require_semver(self.compiler_version, "domain adapter compiler version")
        for name in ("required_roles", "allowed_roles", "multiple_roles", "primary_roles"):
            roles = getattr(self, name)
            if type(roles) is not tuple or any(type(item) is not PackArtifactRoleV1 for item in roles):
                raise TypeError(f"domain adapter {name} must be a typed tuple")
            if roles != tuple(sorted(set(roles), key=lambda item: item.value)):
                raise ValueError(f"domain adapter {name} must be sorted and unique")
        allowed = set(self.allowed_roles)
        if not set(self.required_roles) <= allowed:
            raise ValueError("domain adapter required roles must be allowed")
        if not set(self.multiple_roles) <= allowed:
            raise ValueError("domain adapter multiple roles must be allowed")
        if not set(self.primary_roles) <= set(self.required_roles):
            raise ValueError("domain adapter primary roles must be required")
        if type(self.supports_replay_equivalence) is not bool:
            raise TypeError("domain adapter replay support must be a bool")


def validate_adapter_inventory(
    contract: DomainPackAdapterContractV1,
    pack_type: PackTypeV1,
    primary_artifact_id: str,
    artifacts: tuple[PackSourceArtifactV1 | DomainArtifactIdentityV1, ...],
) -> None:
    """Apply exact role/cardinality checks shared by all five adapters."""

    if type(contract) is not DomainPackAdapterContractV1:
        raise TypeError("adapter inventory validation requires a contract")
    if pack_type is not contract.pack_type:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.PACK_TYPE_MISMATCH,
            f"adapter {contract.adapter_id} cannot validate {pack_type.value}",
        )
    if not artifacts:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            "domain pack has no artifacts",
        )
    roles = tuple(item.role for item in artifacts)
    unknown = tuple(sorted(set(roles) - set(contract.allowed_roles), key=lambda item: item.value))
    if unknown:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            f"adapter does not allow role(s): {[item.value for item in unknown]}",
        )
    missing = tuple(role for role in contract.required_roles if role not in roles)
    if missing:
        code = (
            DomainPackRefusalCodeV1.REVEAL_POLICY_VIOLATION
            if any("REVEAL_POLICY" in role.value or "OBSERVABLE_POLICY" in role.value for role in missing)
            else DomainPackRefusalCodeV1.MISSING_REQUIRED_ARTIFACT
        )
        raise DomainPackRefused(
            code,
            f"adapter requires role(s): {[item.value for item in missing]}",
        )
    for role in set(roles) - set(contract.multiple_roles):
        if roles.count(role) != 1:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
                f"role {role.value} must occur exactly once",
            )
    primary = next(
        (item for item in artifacts if item.artifact_id == primary_artifact_id),
        None,
    )
    if primary is None or primary.role not in contract.primary_roles:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            "primary artifact does not use an adapter-approved primary role",
        )


def _object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise TypeError(f"{label} must be an exact string-keyed object")
    return value


def _exact_object(value: object, fields: set[str], label: str) -> dict[str, object]:
    payload = _object(value, label)
    if set(payload) != fields:
        raise ValueError(
            f"{label} fields differ: missing={sorted(fields - set(payload))}, "
            f"extra={sorted(set(payload) - fields)}"
        )
    return payload


def _require_fields(
    payload: dict[str, object],
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    missing = required - set(payload)
    extra = set(payload) - required - optional
    if missing or extra:
        raise ValueError(
            f"{label} fields differ: missing={sorted(missing)}, extra={sorted(extra)}"
        )


def _array(payload: dict[str, object], key: str) -> list[object]:
    value = payload[key]
    if type(value) is not list:
        raise TypeError(f"{key} must be an array")
    return value


def _text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TypeError(f"{key} must be text")
    return value


def _integer(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value


__all__ = [
    "DOMAIN_PACK_INDEX_SCHEMA_ID",
    "DOMAIN_PACK_INDEX_SCHEMA_VERSION",
    "MAX_DOMAIN_ARTIFACT_COUNT_V1",
    "MAX_DOMAIN_TOTAL_BYTES_V1",
    "MAX_ORIGINAL_ARTIFACT_BYTES_V1",
    "PACK_SOURCE_DEFINITION_SCHEMA_ID",
    "PACK_SOURCE_DEFINITION_SCHEMA_VERSION",
    "PRESERVED_EXACT_BYTES_SCHEMA_ID",
    "PRESERVED_EXACT_BYTES_SCHEMA_VERSION",
    "DomainArtifactIdentityV1",
    "DomainPackAdapterContractV1",
    "DomainPackIndexV1",
    "DomainPackRefusalCodeV1",
    "DomainPackRefused",
    "PackArtifactRoleV1",
    "PackArtifactStorageModeV1",
    "PackBuildSpecificationV1",
    "PackSourceArtifactV1",
    "PreservedExactBytesV1",
    "validate_adapter_inventory",
]
