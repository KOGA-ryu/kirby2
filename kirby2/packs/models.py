"""Strict immutable contracts for canonical data-only Kirby2 packs.

The models in this module are the sole typed boundary for ``manifest.toml``.
They deliberately contain no archive or installation behavior.  Logical identity is
derived in :mod:`kirby2.packs.identity`; canonical byte encodings and safe content
declarations are owned by :mod:`kirby2.packs.formats`.
"""

from __future__ import annotations

import hmac
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, TypeVar, cast

from .formats import (
    K2PACK_CANONICALIZATION_ID,
    K2PACK_CANONICALIZATION_VERSION,
    require_content_declaration,
    require_data_identifier,
    require_namespace,
    require_nfc_text,
    require_pack_name,
    require_relative_pack_path,
    require_semver,
    require_semver_range,
    require_sha256,
)


PACK_FORMAT_ID = "KIRBY2_DATA_ONLY_PACK_V1"
PACK_FORMAT_VERSION = 1
PACK_MANIFEST_SCHEMA_ID = "KIRBY2_PACK_MANIFEST_V1"
PACK_MANIFEST_SCHEMA_VERSION = 1

_EnumT = TypeVar("_EnumT", bound=Enum)


class PackTypeV1(str, Enum):
    """Closed roadmap inventory of portable pack domains."""

    SCENARIO = "SCENARIO"
    LESSON = "LESSON"
    CURRICULUM = "CURRICULUM"
    STRATEGY = "STRATEGY"
    MARKET_PROFILE = "MARKET_PROFILE"
    HISTORICAL = "HISTORICAL"
    REPLAY = "REPLAY"
    ANALYSIS = "ANALYSIS"
    RESEARCH = "RESEARCH"


class PackContentFormatV1(str, Enum):
    """Allowlisted data encodings understood by ``packs.formats``."""

    TOML = "TOML"
    PARQUET = "PARQUET"
    CANONICAL_JSON = "CANONICAL_JSON"
    CANONICAL_EVENT_STREAM = "CANONICAL_EVENT_STREAM"
    REPORT_DATA = "REPORT_DATA"
    BINARY_EVIDENCE = "BINARY_EVIDENCE"


class PackCompatibilityLevelV1(str, Enum):
    """The four distinct compatibility claims required by WO39."""

    READABLE = "READABLE"
    INSTALLABLE = "INSTALLABLE"
    EXECUTABLE = "EXECUTABLE"
    REPLAY_EQUIVALENT = "REPLAY_EQUIVALENT"


_COMPATIBILITY_ORDER_V1 = (
    PackCompatibilityLevelV1.READABLE,
    PackCompatibilityLevelV1.INSTALLABLE,
    PackCompatibilityLevelV1.EXECUTABLE,
    PackCompatibilityLevelV1.REPLAY_EQUIVALENT,
)

_EXECUTABLE_ENTRYPOINT_TOKENS = frozenset(
    {
        "callable",
        "command",
        "exec",
        "html",
        "javascript",
        "module",
        "python",
        "renderer",
        "script",
        "shell",
    }
)


class PackRedistributionPolicyV1(str, Enum):
    """Declared redistribution policy; this is not a legal determination."""

    ALLOWED = "ALLOWED"
    CONDITIONAL = "CONDITIONAL"
    PROHIBITED = "PROHIBITED"
    UNKNOWN = "UNKNOWN"


class PackContentModeV1(str, Enum):
    """Whether licensed source bytes travel inside the pack."""

    SELF_CONTAINED = "SELF_CONTAINED"
    REFERENCE_ONLY = "REFERENCE_ONLY"


@dataclass(frozen=True, slots=True)
class PackCreatorV1:
    """Canonical creator metadata and its non-authenticating identity key."""

    display_name: str
    identity_uri: str

    def __post_init__(self) -> None:
        require_nfc_text(self.display_name, "pack creator display name", maximum_bytes=256)
        require_nfc_text(self.identity_uri, "pack creator identity URI", maximum_bytes=2048)
        if self.identity_uri.startswith(("file:", "/", "~", "\\")):
            raise ValueError("pack creator identity URI cannot be a local path")

    def metadata_dict(self) -> dict[str, object]:
        """Return the exact self-reference-free creator identity projection."""

        return {
            "display_name": self.display_name,
            "identity_uri": self.identity_uri,
        }

    @property
    def creator_id(self) -> str:
        from .identity import derive_creator_id

        return derive_creator_id(self)

    def as_dict(self) -> dict[str, object]:
        return {**self.metadata_dict(), "creator_id": self.creator_id}

    @classmethod
    def from_dict(cls, value: object) -> PackCreatorV1:
        payload = _exact_object(
            value,
            {"creator_id", "display_name", "identity_uri"},
            "pack creator",
        )
        declared_creator_id = require_sha256(
            payload["creator_id"],
            "declared pack creator ID",
        )
        restored = cls(
            display_name=_exact_text(payload, "display_name"),
            identity_uri=_exact_text(payload, "identity_uri"),
        )
        if not hmac.compare_digest(declared_creator_id, restored.creator_id):
            raise ValueError("declared creator ID differs from canonical creator metadata")
        if restored.as_dict() != payload:
            raise ValueError("pack creator did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PackRegistryKeyV1:
    """The immutable creator-qualified registry key for one pack version."""

    creator_id: str
    namespace: str
    name: str
    version: str

    def __post_init__(self) -> None:
        require_sha256(self.creator_id, "pack registry creator ID")
        require_namespace(self.namespace)
        require_pack_name(self.name)
        require_semver(self.version, "pack registry version")

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return (self.creator_id, self.namespace, self.name, self.version)

    def as_dict(self) -> dict[str, object]:
        return {
            "creator_id": self.creator_id,
            "name": self.name,
            "namespace": self.namespace,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: object) -> PackRegistryKeyV1:
        payload = _exact_object(
            value,
            {"creator_id", "name", "namespace", "version"},
            "pack registry key",
        )
        restored = cls(
            creator_id=_exact_text(payload, "creator_id"),
            namespace=_exact_text(payload, "namespace"),
            name=_exact_text(payload, "name"),
            version=_exact_text(payload, "version"),
        )
        if restored.as_dict() != payload:
            raise ValueError("pack registry key did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PackDependencyV1:
    """One local-only, creator-qualified dependency with an exact logical digest."""

    creator_id: str
    namespace: str
    name: str
    version_constraint: str
    expected_pack_id: str

    def __post_init__(self) -> None:
        require_sha256(self.creator_id, "pack dependency creator ID")
        require_namespace(self.namespace, "pack dependency namespace")
        require_pack_name(self.name, "pack dependency name")
        require_semver_range(
            self.version_constraint,
            "pack dependency version constraint",
        )
        require_sha256(self.expected_pack_id, "pack dependency expected pack ID")

    @property
    def target_key(self) -> tuple[str, str, str]:
        return (self.creator_id, self.namespace, self.name)

    @property
    def sort_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.creator_id,
            self.namespace,
            self.name,
            self.version_constraint,
            self.expected_pack_id,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "creator_id": self.creator_id,
            "expected_pack_id": self.expected_pack_id,
            "name": self.name,
            "namespace": self.namespace,
            "version_constraint": self.version_constraint,
        }

    @classmethod
    def from_dict(cls, value: object) -> PackDependencyV1:
        payload = _exact_object(
            value,
            {
                "creator_id",
                "expected_pack_id",
                "name",
                "namespace",
                "version_constraint",
            },
            "pack dependency",
        )
        restored = cls(
            creator_id=_exact_text(payload, "creator_id"),
            namespace=_exact_text(payload, "namespace"),
            name=_exact_text(payload, "name"),
            version_constraint=_exact_text(payload, "version_constraint"),
            expected_pack_id=_exact_text(payload, "expected_pack_id"),
        )
        if restored.as_dict() != payload:
            raise ValueError("pack dependency did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PackVersionRequirementV1:
    """Canonical SemVer requirement for an engine or compiler component."""

    component_id: str
    version_constraint: str

    def __post_init__(self) -> None:
        require_data_identifier(self.component_id, "pack compatibility component ID")
        require_semver_range(
            self.version_constraint,
            "pack compatibility version constraint",
        )

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.component_id, self.version_constraint)

    def as_dict(self) -> dict[str, object]:
        return {
            "component_id": self.component_id,
            "version_constraint": self.version_constraint,
        }

    @classmethod
    def from_dict(cls, value: object) -> PackVersionRequirementV1:
        payload = _exact_object(
            value,
            {"component_id", "version_constraint"},
            "pack version requirement",
        )
        restored = cls(
            component_id=_exact_text(payload, "component_id"),
            version_constraint=_exact_text(payload, "version_constraint"),
        )
        if restored.as_dict() != payload:
            raise ValueError("pack version requirement did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PackSchemaRequirementV1:
    """Exact supported versions for one named data schema."""

    schema_id: str
    supported_versions: tuple[int, ...]

    def __post_init__(self) -> None:
        require_data_identifier(self.schema_id, "pack compatibility schema ID")
        if type(self.supported_versions) is not tuple or not self.supported_versions:
            raise ValueError("pack supported schema versions must be a nonempty tuple")
        if any(type(item) is not int or item <= 0 for item in self.supported_versions):
            raise ValueError("pack supported schema versions must be positive integers")
        if self.supported_versions != tuple(sorted(set(self.supported_versions))):
            raise ValueError("pack supported schema versions must be sorted and unique")

    @property
    def sort_key(self) -> tuple[str, tuple[int, ...]]:
        return (self.schema_id, self.supported_versions)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "supported_versions": list(self.supported_versions),
        }

    @classmethod
    def from_dict(cls, value: object) -> PackSchemaRequirementV1:
        payload = _exact_object(
            value,
            {"schema_id", "supported_versions"},
            "pack schema requirement",
        )
        versions = _exact_array(payload["supported_versions"], "supported schema versions")
        if any(type(item) is not int for item in versions):
            raise TypeError("supported schema versions must be an array of integers")
        restored = cls(
            schema_id=_exact_text(payload, "schema_id"),
            supported_versions=tuple(versions),
        )
        if restored.as_dict() != payload:
            raise ValueError("pack schema requirement did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PackCompatibilityV1:
    """One row of the fixed compatibility matrix."""

    level: PackCompatibilityLevelV1
    supported: bool
    engine: PackVersionRequirementV1 | None = None
    compilers: tuple[PackVersionRequirementV1, ...] = ()
    schemas: tuple[PackSchemaRequirementV1, ...] = ()

    def __post_init__(self) -> None:
        if type(self.level) is not PackCompatibilityLevelV1:
            raise TypeError("pack compatibility level is invalid")
        if type(self.supported) is not bool:
            raise TypeError("pack compatibility support flag must be an exact boolean")
        if not self.supported:
            if self.engine is not None or self.compilers != () or self.schemas != ():
                raise ValueError(
                    "unsupported compatibility rows cannot carry requirements"
                )
            return
        if type(self.engine) is not PackVersionRequirementV1:
            raise TypeError("pack engine compatibility requirement is invalid")
        if type(self.compilers) is not tuple or any(
            type(item) is not PackVersionRequirementV1 for item in self.compilers
        ):
            raise TypeError("pack compiler requirements must be an immutable typed tuple")
        if tuple(sorted(self.compilers, key=lambda item: item.sort_key)) != self.compilers:
            raise ValueError("pack compiler requirements must use canonical order")
        compiler_ids = tuple(item.component_id for item in self.compilers)
        if len(compiler_ids) != len(set(compiler_ids)):
            raise ValueError("pack compiler requirement IDs must be unique")
        if type(self.schemas) is not tuple or not self.schemas or any(
            type(item) is not PackSchemaRequirementV1 for item in self.schemas
        ):
            raise TypeError("pack schema requirements must be a nonempty typed tuple")
        if tuple(sorted(self.schemas, key=lambda item: item.sort_key)) != self.schemas:
            raise ValueError("pack schema requirements must use canonical order")
        schema_ids = tuple(item.schema_id for item in self.schemas)
        if len(schema_ids) != len(set(schema_ids)):
            raise ValueError("pack schema requirement IDs must be unique")

    def supports_schema(self, schema_id: str, schema_version: int) -> bool:
        return self.supported and any(
            item.schema_id == schema_id and schema_version in item.supported_versions
            for item in self.schemas
        )

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "level": self.level.value,
            "supported": self.supported,
        }
        if not self.supported:
            return result
        engine = cast(PackVersionRequirementV1, self.engine)
        result.update(
            {
                "compilers": [item.as_dict() for item in self.compilers],
                "engine": engine.as_dict(),
                "schemas": [item.as_dict() for item in self.schemas],
            }
        )
        return result

    @classmethod
    def from_dict(cls, value: object) -> PackCompatibilityV1:
        if type(value) is not dict:
            raise TypeError("serialized pack compatibility must be an exact object")
        raw_supported = value.get("supported")
        if type(raw_supported) is not bool:
            raise TypeError(
                "serialized pack compatibility supported must be an exact boolean"
            )
        expected_fields = (
            {"compilers", "engine", "level", "schemas", "supported"}
            if raw_supported
            else {"level", "supported"}
        )
        payload = _exact_object(value, expected_fields, "pack compatibility")
        level = _enum_value(
            PackCompatibilityLevelV1,
            payload["level"],
            "pack compatibility level",
        )
        if not raw_supported:
            restored = cls(level=level, supported=False)
            if restored.as_dict() != payload:
                raise ValueError("pack compatibility did not round-trip exactly")
            return restored

        raw_compilers = _exact_object_array(
            payload["compilers"],
            "pack compiler requirements",
        )
        raw_schemas = _exact_object_array(
            payload["schemas"],
            "pack schema requirements",
        )
        restored = cls(
            level=level,
            supported=True,
            engine=PackVersionRequirementV1.from_dict(payload["engine"]),
            compilers=tuple(
                PackVersionRequirementV1.from_dict(item) for item in raw_compilers
            ),
            schemas=tuple(PackSchemaRequirementV1.from_dict(item) for item in raw_schemas),
        )
        if restored.as_dict() != payload:
            raise ValueError("pack compatibility did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PackProvenanceV1:
    """One exact source identity retained by a portable pack."""

    source_kind: str
    source_id: str
    source_sha256: str

    def __post_init__(self) -> None:
        require_data_identifier(self.source_kind, "pack provenance source kind")
        require_data_identifier(self.source_id, "pack provenance source ID")
        require_sha256(self.source_sha256, "pack provenance source digest")

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.source_kind, self.source_id, self.source_sha256)

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> PackProvenanceV1:
        payload = _exact_object(
            value,
            {"source_id", "source_kind", "source_sha256"},
            "pack provenance",
        )
        restored = cls(
            source_kind=_exact_text(payload, "source_kind"),
            source_id=_exact_text(payload, "source_id"),
            source_sha256=_exact_text(payload, "source_sha256"),
        )
        if restored.as_dict() != payload:
            raise ValueError("pack provenance did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PackLicenseV1:
    """Explicit source-license and redistribution declaration."""

    license_id: str
    license_name: str
    license_uri: str
    redistribution_policy: PackRedistributionPolicyV1
    content_mode: PackContentModeV1

    def __post_init__(self) -> None:
        require_data_identifier(self.license_id, "pack license ID")
        require_nfc_text(self.license_name, "pack license name", maximum_bytes=512)
        require_nfc_text(self.license_uri, "pack license URI", maximum_bytes=2048)
        if self.license_uri.startswith(("file:", "/", "~", "\\")):
            raise ValueError("pack license URI cannot be a local path")
        if type(self.redistribution_policy) is not PackRedistributionPolicyV1:
            raise TypeError("pack redistribution policy is invalid")
        if type(self.content_mode) is not PackContentModeV1:
            raise TypeError("pack content mode is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "content_mode": self.content_mode.value,
            "license_id": self.license_id,
            "license_name": self.license_name,
            "license_uri": self.license_uri,
            "redistribution_policy": self.redistribution_policy.value,
        }

    @classmethod
    def from_dict(cls, value: object) -> PackLicenseV1:
        payload = _exact_object(
            value,
            {
                "content_mode",
                "license_id",
                "license_name",
                "license_uri",
                "redistribution_policy",
            },
            "pack license",
        )
        restored = cls(
            license_id=_exact_text(payload, "license_id"),
            license_name=_exact_text(payload, "license_name"),
            license_uri=_exact_text(payload, "license_uri"),
            redistribution_policy=_enum_value(
                PackRedistributionPolicyV1,
                payload["redistribution_policy"],
                "pack redistribution policy",
            ),
            content_mode=_enum_value(
                PackContentModeV1,
                payload["content_mode"],
                "pack content mode",
            ),
        )
        if restored.as_dict() != payload:
            raise ValueError("pack license did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PackFileV1:
    """One complete, explicitly typed payload inventory row."""

    path: str
    byte_count: int
    sha256: str
    content_format: PackContentFormatV1
    media_type: str
    schema_id: str
    schema_version: int

    def __post_init__(self) -> None:
        if type(self.content_format) is not PackContentFormatV1:
            raise TypeError("pack content format is invalid")
        canonical = require_content_declaration(
            path=self.path,
            content_format=self.content_format.value,
            media_type=self.media_type,
            schema_id=self.schema_id,
        )
        if canonical != (
            self.path,
            self.content_format.value,
            self.media_type,
            self.schema_id,
        ):
            raise ValueError("pack content declaration changed during validation")
        if type(self.byte_count) is not int or self.byte_count <= 0:
            raise ValueError("pack payload byte count must be positive")
        require_sha256(self.sha256, "pack payload digest")
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValueError("pack payload schema version must be positive")

    @property
    def sort_key(self) -> bytes:
        return self.path.encode("utf-8")

    def as_dict(self) -> dict[str, object]:
        return {
            "byte_count": self.byte_count,
            "content_format": self.content_format.value,
            "media_type": self.media_type,
            "path": self.path,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> PackFileV1:
        payload = _exact_object(
            value,
            {
                "byte_count",
                "content_format",
                "media_type",
                "path",
                "schema_id",
                "schema_version",
                "sha256",
            },
            "pack file inventory row",
        )
        restored = cls(
            path=_exact_text(payload, "path"),
            byte_count=_exact_integer(payload, "byte_count"),
            sha256=_exact_text(payload, "sha256"),
            content_format=_enum_value(
                PackContentFormatV1,
                payload["content_format"],
                "pack content format",
            ),
            media_type=_exact_text(payload, "media_type"),
            schema_id=_exact_text(payload, "schema_id"),
            schema_version=_exact_integer(payload, "schema_version"),
        )
        if restored.as_dict() != payload:
            raise ValueError("pack file inventory row did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PackEntrypointV1:
    """A data identifier bound to one declared inventory path, never executable code."""

    entrypoint_id: str
    data_id: str
    path: str

    def __post_init__(self) -> None:
        _require_data_only_identifier(self.entrypoint_id, "pack entrypoint ID")
        _require_data_only_identifier(self.data_id, "pack entrypoint data ID")
        require_relative_pack_path(self.path, "pack entrypoint path")

    @property
    def sort_key(self) -> tuple[str, str, bytes]:
        return (self.entrypoint_id, self.data_id, self.path.encode("utf-8"))

    def as_dict(self) -> dict[str, object]:
        return {
            "data_id": self.data_id,
            "entrypoint_id": self.entrypoint_id,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, value: object) -> PackEntrypointV1:
        payload = _exact_object(
            value,
            {"data_id", "entrypoint_id", "path"},
            "pack entrypoint",
        )
        restored = cls(
            entrypoint_id=_exact_text(payload, "entrypoint_id"),
            data_id=_exact_text(payload, "data_id"),
            path=_exact_text(payload, "path"),
        )
        if restored.as_dict() != payload:
            raise ValueError("pack entrypoint did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PackManifestV1:
    """Self-verifying canonical manifest for one logical data-only pack."""

    namespace: str
    name: str
    title: str
    version: str
    creator: PackCreatorV1
    pack_type: PackTypeV1
    compatibility: tuple[PackCompatibilityV1, ...]
    dependencies: tuple[PackDependencyV1, ...]
    provenance: tuple[PackProvenanceV1, ...]
    license: PackLicenseV1
    capability_labels: tuple[str, ...]
    inventory: tuple[PackFileV1, ...]
    entrypoints: tuple[PackEntrypointV1, ...]

    pack_format_id: ClassVar[str] = PACK_FORMAT_ID
    pack_format_version: ClassVar[int] = PACK_FORMAT_VERSION
    schema_id: ClassVar[str] = PACK_MANIFEST_SCHEMA_ID
    schema_version: ClassVar[int] = PACK_MANIFEST_SCHEMA_VERSION
    canonicalization_id: ClassVar[str] = K2PACK_CANONICALIZATION_ID
    canonicalization_version: ClassVar[int] = K2PACK_CANONICALIZATION_VERSION

    def __post_init__(self) -> None:
        require_namespace(self.namespace)
        require_pack_name(self.name)
        require_nfc_text(self.title, "pack title", maximum_bytes=512)
        require_semver(self.version, "pack version")
        if type(self.creator) is not PackCreatorV1:
            raise TypeError("pack creator is invalid")
        if type(self.pack_type) is not PackTypeV1:
            raise TypeError("pack type is invalid")
        if type(self.license) is not PackLicenseV1:
            raise TypeError("pack license is invalid")

        self._validate_compatibility()
        self._validate_dependencies()
        self._validate_provenance()
        self._validate_capabilities()
        self._validate_inventory()
        self._validate_entrypoints()

    def _validate_compatibility(self) -> None:
        if type(self.compatibility) is not tuple or any(
            type(item) is not PackCompatibilityV1 for item in self.compatibility
        ):
            raise TypeError("pack compatibility must be an immutable typed tuple")
        if tuple(item.level for item in self.compatibility) != _COMPATIBILITY_ORDER_V1:
            raise ValueError(
                "pack compatibility must contain the fixed four-row level order"
            )
        support = tuple(item.supported for item in self.compatibility)
        if support[0] is not True:
            raise ValueError("readable compatibility must be supported")
        if any(not support[index] and support[index + 1] for index in range(3)):
            raise ValueError("pack compatibility support must be monotonic")

    def _validate_dependencies(self) -> None:
        if type(self.dependencies) is not tuple or any(
            type(item) is not PackDependencyV1 for item in self.dependencies
        ):
            raise TypeError("pack dependencies must be an immutable typed tuple")
        if tuple(sorted(self.dependencies, key=lambda item: item.sort_key)) != self.dependencies:
            raise ValueError("pack dependencies must use canonical order")
        targets = tuple(item.target_key for item in self.dependencies)
        if len(targets) != len(set(targets)):
            raise ValueError("pack dependencies must have unique creator-qualified targets")

    def _validate_provenance(self) -> None:
        if type(self.provenance) is not tuple or not self.provenance or any(
            type(item) is not PackProvenanceV1 for item in self.provenance
        ):
            raise TypeError("pack provenance must be a nonempty immutable typed tuple")
        if tuple(sorted(self.provenance, key=lambda item: item.sort_key)) != self.provenance:
            raise ValueError("pack provenance must use canonical order")
        source_keys = tuple(
            (item.source_kind, item.source_id) for item in self.provenance
        )
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("pack provenance source identities must be unique")

    def _validate_capabilities(self) -> None:
        if type(self.capability_labels) is not tuple:
            raise TypeError("pack capability labels must be an immutable tuple")
        for item in self.capability_labels:
            require_data_identifier(item, "pack capability label")
        if self.capability_labels != tuple(sorted(set(self.capability_labels))):
            raise ValueError("pack capability labels must be sorted and unique")

    def _validate_inventory(self) -> None:
        if type(self.inventory) is not tuple or not self.inventory or any(
            type(item) is not PackFileV1 for item in self.inventory
        ):
            raise TypeError("pack inventory must be a nonempty immutable typed tuple")
        if tuple(sorted(self.inventory, key=lambda item: item.sort_key)) != self.inventory:
            raise ValueError("pack inventory must use canonical UTF-8 path order")
        paths = tuple(item.path for item in self.inventory)
        if len(paths) != len(set(paths)):
            raise ValueError("pack inventory paths must be unique")
        path_set = set(paths)
        for path in paths:
            parts = path.split("/")
            if any("/".join(parts[:depth]) in path_set for depth in range(1, len(parts))):
                raise ValueError("pack inventory has a file-versus-directory path collision")
        portable_keys = tuple(
            unicodedata.normalize("NFC", item.path).casefold() for item in self.inventory
        )
        if len(portable_keys) != len(set(portable_keys)):
            raise ValueError("pack inventory contains a Unicode or case-fold path collision")
        portable_key_set = set(portable_keys)
        for key in portable_keys:
            parts = key.split("/")
            if any(
                "/".join(parts[:depth]) in portable_key_set
                for depth in range(1, len(parts))
            ):
                raise ValueError(
                    "pack inventory has a portable file-versus-directory collision"
                )
        readable = self.compatibility[0]
        for item in self.inventory:
            if not readable.supports_schema(item.schema_id, item.schema_version):
                raise ValueError(
                    f"pack inventory schema is absent from compatibility: {item.path!r}"
                )

    def _validate_entrypoints(self) -> None:
        if type(self.entrypoints) is not tuple or not self.entrypoints or any(
            type(item) is not PackEntrypointV1 for item in self.entrypoints
        ):
            raise TypeError("pack entrypoints must be a nonempty immutable typed tuple")
        if tuple(sorted(self.entrypoints, key=lambda item: item.sort_key)) != self.entrypoints:
            raise ValueError("pack entrypoints must use canonical order")
        entrypoint_ids = tuple(item.entrypoint_id for item in self.entrypoints)
        if len(entrypoint_ids) != len(set(entrypoint_ids)):
            raise ValueError("pack entrypoint IDs must be unique")
        data_ids = tuple(item.data_id for item in self.entrypoints)
        if len(data_ids) != len(set(data_ids)):
            raise ValueError("pack entrypoint data IDs must be unique")
        inventory_paths = {item.path for item in self.inventory}
        if any(item.path not in inventory_paths for item in self.entrypoints):
            raise ValueError("pack entrypoint references an undeclared inventory path")

    @property
    def creator_id(self) -> str:
        return self.creator.creator_id

    @property
    def registry_key(self) -> PackRegistryKeyV1:
        return PackRegistryKeyV1(
            creator_id=self.creator_id,
            namespace=self.namespace,
            name=self.name,
            version=self.version,
        )

    @property
    def pack_id(self) -> str:
        from .identity import derive_pack_id

        return derive_pack_id(self)

    def identity_dict(self) -> dict[str, object]:
        """Return manifest identity without ``pack_id`` or the separately framed inventory."""

        return {
            "canonicalization_id": self.canonicalization_id,
            "canonicalization_version": self.canonicalization_version,
            "capability_labels": list(self.capability_labels),
            "compatibility": [item.as_dict() for item in self.compatibility],
            "creator": self.creator.as_dict(),
            "dependencies": [item.as_dict() for item in self.dependencies],
            "entrypoints": [item.as_dict() for item in self.entrypoints],
            "license": self.license.as_dict(),
            "name": self.name,
            "namespace": self.namespace,
            "pack_format_id": self.pack_format_id,
            "pack_format_version": self.pack_format_version,
            "pack_type": self.pack_type.value,
            "provenance": [item.as_dict() for item in self.provenance],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "title": self.title,
            "version": self.version,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_dict(),
            "inventory": [item.as_dict() for item in self.inventory],
            "pack_id": self.pack_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> PackManifestV1:
        payload = _exact_object(
            value,
            {
                "canonicalization_id",
                "canonicalization_version",
                "capability_labels",
                "compatibility",
                "creator",
                "dependencies",
                "entrypoints",
                "inventory",
                "license",
                "name",
                "namespace",
                "pack_format_id",
                "pack_format_version",
                "pack_id",
                "pack_type",
                "provenance",
                "schema_id",
                "schema_version",
                "title",
                "version",
            },
            "pack manifest",
        )
        _require_manifest_constant(
            payload,
            "pack_format_id",
            cls.pack_format_id,
        )
        _require_manifest_constant(
            payload,
            "pack_format_version",
            cls.pack_format_version,
        )
        _require_manifest_constant(payload, "schema_id", cls.schema_id)
        _require_manifest_constant(payload, "schema_version", cls.schema_version)
        _require_manifest_constant(
            payload,
            "canonicalization_id",
            cls.canonicalization_id,
        )
        _require_manifest_constant(
            payload,
            "canonicalization_version",
            cls.canonicalization_version,
        )
        declared_pack_id = require_sha256(payload["pack_id"], "declared pack ID")

        raw_dependencies = _exact_object_array(
            payload["dependencies"],
            "pack dependencies",
        )
        raw_compatibility = _exact_object_array(
            payload["compatibility"],
            "pack compatibility matrix",
        )
        raw_provenance = _exact_object_array(
            payload["provenance"],
            "pack provenance",
        )
        raw_inventory = _exact_object_array(payload["inventory"], "pack inventory")
        raw_entrypoints = _exact_object_array(
            payload["entrypoints"],
            "pack entrypoints",
        )
        raw_capabilities = _exact_array(
            payload["capability_labels"],
            "pack capability labels",
        )
        if any(type(item) is not str for item in raw_capabilities):
            raise TypeError("pack capability labels must be an array of strings")

        restored = cls(
            namespace=_exact_text(payload, "namespace"),
            name=_exact_text(payload, "name"),
            title=_exact_text(payload, "title"),
            version=_exact_text(payload, "version"),
            creator=PackCreatorV1.from_dict(payload["creator"]),
            pack_type=_enum_value(PackTypeV1, payload["pack_type"], "pack type"),
            compatibility=tuple(
                PackCompatibilityV1.from_dict(item) for item in raw_compatibility
            ),
            dependencies=tuple(
                PackDependencyV1.from_dict(item) for item in raw_dependencies
            ),
            provenance=tuple(PackProvenanceV1.from_dict(item) for item in raw_provenance),
            license=PackLicenseV1.from_dict(payload["license"]),
            capability_labels=tuple(raw_capabilities),
            inventory=tuple(PackFileV1.from_dict(item) for item in raw_inventory),
            entrypoints=tuple(
                PackEntrypointV1.from_dict(item) for item in raw_entrypoints
            ),
        )
        if not hmac.compare_digest(declared_pack_id, restored.pack_id):
            raise ValueError("declared pack ID differs from canonical pack identity")
        if restored.as_dict() != payload:
            raise ValueError("pack manifest did not round-trip exactly")
        return restored


def _require_data_only_identifier(value: object, label: str) -> str:
    result = require_data_identifier(value, label)
    if "://" in result:
        raise ValueError(f"{label} cannot contain a URI scheme")
    tokens = frozenset(
        token for token in re.split(r"[._:/-]+", result.casefold()) if token
    )
    forbidden = sorted(tokens & _EXECUTABLE_ENTRYPOINT_TOKENS)
    if forbidden:
        raise ValueError(
            f"{label} contains forbidden executable token(s): {forbidden}"
        )
    return result


def _exact_object(
    value: object,
    expected: set[str] | frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"serialized {label} must be an exact object")
    if any(type(key) is not str for key in value):
        raise TypeError(f"serialized {label} field names must be exact text")
    actual = set(value)
    missing = sorted(set(expected) - actual)
    unknown = sorted(actual - set(expected))
    if missing or unknown:
        raise ValueError(
            f"serialized {label} fields are not exact: "
            f"missing={missing} unknown={unknown}"
        )
    return value


def _exact_array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"serialized {label} must be an exact array")
    return value


def _exact_object_array(value: object, label: str) -> list[dict[str, object]]:
    rows = _exact_array(value, label)
    if any(type(item) is not dict for item in rows):
        raise TypeError(f"serialized {label} must contain exact objects")
    return rows  # type: ignore[return-value]


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


def _enum_value(enum_type: type[_EnumT], value: object, label: str) -> _EnumT:
    if type(value) is not str:
        raise TypeError(f"serialized {label} must be exact text")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"serialized {label} is unsupported") from error


def _require_manifest_constant(
    payload: dict[str, object],
    key: str,
    expected: str | int,
) -> None:
    value = payload[key]
    if type(value) is not type(expected) or value != expected:
        raise ValueError(f"pack manifest {key} differs from the supported constant")


__all__ = [
    "PACK_FORMAT_ID",
    "PACK_FORMAT_VERSION",
    "PACK_MANIFEST_SCHEMA_ID",
    "PACK_MANIFEST_SCHEMA_VERSION",
    "PackCompatibilityLevelV1",
    "PackCompatibilityV1",
    "PackContentFormatV1",
    "PackContentModeV1",
    "PackCreatorV1",
    "PackDependencyV1",
    "PackEntrypointV1",
    "PackFileV1",
    "PackLicenseV1",
    "PackManifestV1",
    "PackProvenanceV1",
    "PackRedistributionPolicyV1",
    "PackRegistryKeyV1",
    "PackSchemaRequirementV1",
    "PackTypeV1",
    "PackVersionRequirementV1",
]
