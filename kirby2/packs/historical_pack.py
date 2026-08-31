"""License-aware historical evidence pack adapter.

Historical packs make the source boundary visible.  A self-contained pack carries
the exact declared source bytes; a reference-only pack carries canonical source
references and never smuggles the licensed source through a generic envelope.
Derived evidence may travel in either mode, but it remains separately identified.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from kirby2.historical.models import HistoricalDataMode

from .formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    require_data_identifier,
    require_nfc_text,
    require_sha256,
)
from .models import PackContentModeV1, PackLicenseV1, PackTypeV1
from .types import (
    DomainPackAdapterContractV1,
    DomainPackIndexV1,
    DomainPackRefusalCodeV1,
    DomainPackRefused,
    PackArtifactRoleV1,
    PackArtifactStorageModeV1,
    validate_adapter_inventory,
)


HISTORICAL_PACK_ADAPTER_ID_V1 = "KIRBY2_HISTORICAL_PACK_ADAPTER_V1"
HISTORICAL_CAPABILITY_SCHEMA_ID_V1 = "KIRBY2_HISTORICAL_PACK_CAPABILITIES_V1"
HISTORICAL_PROVENANCE_SCHEMA_ID_V1 = "KIRBY2_HISTORICAL_PACK_PROVENANCE_V1"
HISTORICAL_SOURCE_LICENSE_SCHEMA_ID_V1 = "KIRBY2_HISTORICAL_SOURCE_LICENSE_V1"
HISTORICAL_SOURCE_REFERENCE_SCHEMA_ID_V1 = "KIRBY2_HISTORICAL_SOURCE_REFERENCE_V1"

_SAFE_SOURCE_URI = re.compile(r"(?:https?|s3|gs|doi|urn):[^\s]+\Z", re.IGNORECASE)


def _roles(*items: PackArtifactRoleV1) -> tuple[PackArtifactRoleV1, ...]:
    return tuple(sorted(items, key=lambda item: item.value))


HISTORICAL_PACK_ADAPTER_V1 = DomainPackAdapterContractV1(
    pack_type=PackTypeV1.HISTORICAL,
    adapter_id=HISTORICAL_PACK_ADAPTER_ID_V1,
    adapter_version=1,
    compiler_component_id="KIRBY2_HISTORICAL_PACK_COMPILER_V1",
    compiler_version="0.1.0",
    required_roles=_roles(
        PackArtifactRoleV1.HISTORICAL_CAPABILITIES,
        PackArtifactRoleV1.HISTORICAL_PROVENANCE,
        PackArtifactRoleV1.HISTORICAL_SOURCE_LICENSE,
    ),
    allowed_roles=_roles(
        PackArtifactRoleV1.HISTORICAL_CAPABILITIES,
        PackArtifactRoleV1.HISTORICAL_PROVENANCE,
        PackArtifactRoleV1.HISTORICAL_SOURCE_LICENSE,
        PackArtifactRoleV1.HISTORICAL_SOURCE_CONTENT,
        PackArtifactRoleV1.HISTORICAL_SOURCE_REFERENCE,
        PackArtifactRoleV1.HISTORICAL_DERIVED_EVIDENCE,
        PackArtifactRoleV1.EMBEDDED_RUN,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    multiple_roles=_roles(
        PackArtifactRoleV1.HISTORICAL_SOURCE_CONTENT,
        PackArtifactRoleV1.HISTORICAL_SOURCE_REFERENCE,
        PackArtifactRoleV1.HISTORICAL_DERIVED_EVIDENCE,
        PackArtifactRoleV1.EMBEDDED_RUN,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    primary_roles=_roles(PackArtifactRoleV1.HISTORICAL_PROVENANCE),
    supports_replay_equivalence=False,
    supports_execution=False,
    allowed_content_modes=(
        PackContentModeV1.REFERENCE_ONLY,
        PackContentModeV1.SELF_CONTAINED,
    ),
)


@dataclass(frozen=True, slots=True)
class HistoricalCapabilityRecordV1:
    dataset_id: str
    historical_mode: HistoricalDataMode
    capability_labels: tuple[str, ...]
    content_mode: PackContentModeV1
    source_payload_included: bool
    source_count: int

    schema_id: ClassVar[str] = HISTORICAL_CAPABILITY_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        require_data_identifier(self.dataset_id, "historical dataset ID")
        if type(self.historical_mode) is not HistoricalDataMode:
            raise TypeError("historical capability mode is invalid")
        if type(self.capability_labels) is not tuple or not self.capability_labels:
            raise ValueError("historical capability labels must be a nonempty tuple")
        for label in self.capability_labels:
            require_data_identifier(label, "historical capability label")
        if self.capability_labels != tuple(sorted(set(self.capability_labels))):
            raise ValueError("historical capability labels must be canonical")
        if type(self.content_mode) is not PackContentModeV1:
            raise TypeError("historical capability content mode is invalid")
        if type(self.source_payload_included) is not bool:
            raise TypeError("historical source-payload claim must be boolean")
        if type(self.source_count) is not int or self.source_count <= 0:
            raise ValueError("historical source count must be positive")
        expected_included = self.content_mode is PackContentModeV1.SELF_CONTAINED
        if self.source_payload_included is not expected_included:
            raise ValueError("historical source-payload claim differs from content mode")

    def as_dict(self) -> dict[str, object]:
        return {
            "capability_labels": list(self.capability_labels),
            "content_mode": self.content_mode.value,
            "dataset_id": self.dataset_id,
            "historical_mode": self.historical_mode.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_count": self.source_count,
            "source_payload_included": self.source_payload_included,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> HistoricalCapabilityRecordV1:
        payload = _exact_record(
            raw,
            {
                "capability_labels",
                "content_mode",
                "dataset_id",
                "historical_mode",
                "schema_id",
                "schema_version",
                "source_count",
                "source_payload_included",
            },
            cls.schema_id,
            "historical capability record",
        )
        labels = payload["capability_labels"]
        if type(labels) is not list or any(type(item) is not str for item in labels):
            raise TypeError("historical capability labels must be an array of text")
        restored = cls(
            dataset_id=_text(payload, "dataset_id"),
            historical_mode=HistoricalDataMode(_text(payload, "historical_mode")),
            capability_labels=tuple(labels),
            content_mode=PackContentModeV1(_text(payload, "content_mode")),
            source_payload_included=_bool(payload, "source_payload_included"),
            source_count=_int(payload, "source_count"),
        )
        _require_round_trip(restored.canonical_bytes(), raw, "historical capability")
        return restored


@dataclass(frozen=True, slots=True)
class HistoricalSourceProvenanceV1:
    source_id: str
    source_uri: str
    source_sha256: str
    media_type: str
    license_id: str

    def __post_init__(self) -> None:
        require_data_identifier(self.source_id, "historical source ID")
        require_nfc_text(self.source_uri, "historical source URI", maximum_bytes=2048)
        if not _SAFE_SOURCE_URI.fullmatch(self.source_uri):
            raise ValueError("historical source URI must be an external URI")
        require_sha256(self.source_sha256, "historical source digest")
        require_nfc_text(self.media_type, "historical source media type", maximum_bytes=128)
        require_data_identifier(self.license_id, "historical source license ID")

    def as_dict(self) -> dict[str, object]:
        return {
            "license_id": self.license_id,
            "media_type": self.media_type,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "source_uri": self.source_uri,
        }

    @classmethod
    def from_dict(cls, value: object) -> HistoricalSourceProvenanceV1:
        payload = _exact_object(
            value,
            {"license_id", "media_type", "source_id", "source_sha256", "source_uri"},
            "historical source provenance",
        )
        return cls(
            source_id=_text(payload, "source_id"),
            source_uri=_text(payload, "source_uri"),
            source_sha256=_text(payload, "source_sha256"),
            media_type=_text(payload, "media_type"),
            license_id=_text(payload, "license_id"),
        )


@dataclass(frozen=True, slots=True)
class HistoricalProvenanceRecordV1:
    dataset_id: str
    sources: tuple[HistoricalSourceProvenanceV1, ...]

    schema_id: ClassVar[str] = HISTORICAL_PROVENANCE_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        require_data_identifier(self.dataset_id, "historical provenance dataset ID")
        if type(self.sources) is not tuple or not self.sources or any(
            type(item) is not HistoricalSourceProvenanceV1 for item in self.sources
        ):
            raise TypeError("historical provenance sources must be a nonempty typed tuple")
        if self.sources != tuple(sorted(self.sources, key=lambda item: item.source_id)):
            raise ValueError("historical provenance sources must use canonical order")
        if len({item.source_id for item in self.sources}) != len(self.sources):
            raise ValueError("historical provenance source IDs must be unique")

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sources": [item.as_dict() for item in self.sources],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> HistoricalProvenanceRecordV1:
        payload = _exact_record(
            raw,
            {"dataset_id", "schema_id", "schema_version", "sources"},
            cls.schema_id,
            "historical provenance record",
        )
        sources = payload["sources"]
        if type(sources) is not list:
            raise TypeError("historical provenance sources must be an array")
        restored = cls(
            dataset_id=_text(payload, "dataset_id"),
            sources=tuple(HistoricalSourceProvenanceV1.from_dict(item) for item in sources),
        )
        _require_round_trip(restored.canonical_bytes(), raw, "historical provenance")
        return restored


@dataclass(frozen=True, slots=True)
class HistoricalSourceLicenseRecordV1:
    dataset_id: str
    license: PackLicenseV1

    schema_id: ClassVar[str] = HISTORICAL_SOURCE_LICENSE_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        require_data_identifier(self.dataset_id, "historical licensed dataset ID")
        if type(self.license) is not PackLicenseV1:
            raise TypeError("historical source license must use PackLicenseV1")

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "license": self.license.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> HistoricalSourceLicenseRecordV1:
        payload = _exact_record(
            raw,
            {"dataset_id", "license", "schema_id", "schema_version"},
            cls.schema_id,
            "historical source license record",
        )
        restored = cls(
            dataset_id=_text(payload, "dataset_id"),
            license=PackLicenseV1.from_dict(payload["license"]),
        )
        _require_round_trip(restored.canonical_bytes(), raw, "historical source license")
        return restored


@dataclass(frozen=True, slots=True)
class HistoricalSourceReferenceV1:
    source_id: str
    source_uri: str
    source_sha256: str
    byte_count: int
    media_type: str

    schema_id: ClassVar[str] = HISTORICAL_SOURCE_REFERENCE_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        require_data_identifier(self.source_id, "historical referenced source ID")
        require_nfc_text(self.source_uri, "historical referenced source URI", maximum_bytes=2048)
        if not _SAFE_SOURCE_URI.fullmatch(self.source_uri):
            raise ValueError("reference-only historical source must use an external URI")
        require_sha256(self.source_sha256, "historical referenced source digest")
        if type(self.byte_count) is not int or self.byte_count <= 0:
            raise ValueError("historical referenced source byte count must be positive")
        require_nfc_text(self.media_type, "historical referenced media type", maximum_bytes=128)

    def as_dict(self) -> dict[str, object]:
        return {
            "byte_count": self.byte_count,
            "media_type": self.media_type,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "source_uri": self.source_uri,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> HistoricalSourceReferenceV1:
        payload = _exact_record(
            raw,
            {
                "byte_count",
                "media_type",
                "schema_id",
                "schema_version",
                "source_id",
                "source_sha256",
                "source_uri",
            },
            cls.schema_id,
            "historical source reference",
        )
        restored = cls(
            source_id=_text(payload, "source_id"),
            source_uri=_text(payload, "source_uri"),
            source_sha256=_text(payload, "source_sha256"),
            byte_count=_int(payload, "byte_count"),
            media_type=_text(payload, "media_type"),
        )
        _require_round_trip(restored.canonical_bytes(), raw, "historical source reference")
        return restored


def validate_historical_pack(
    index: DomainPackIndexV1,
    original_bytes: Mapping[str, bytes],
    *,
    license: PackLicenseV1,
) -> None:
    """Verify source inclusion/reference mode and exact license/provenance binding."""

    validate_adapter_inventory(
        HISTORICAL_PACK_ADAPTER_V1,
        index.pack_type,
        index.primary_artifact_id,
        index.artifacts,
    )
    metadata_rows = tuple(
        index.artifact(role)
        for role in (
            PackArtifactRoleV1.HISTORICAL_CAPABILITIES,
            PackArtifactRoleV1.HISTORICAL_PROVENANCE,
            PackArtifactRoleV1.HISTORICAL_SOURCE_LICENSE,
        )
    )
    metadata_schemas = {
        PackArtifactRoleV1.HISTORICAL_CAPABILITIES: (
            HISTORICAL_CAPABILITY_SCHEMA_ID_V1
        ),
        PackArtifactRoleV1.HISTORICAL_PROVENANCE: (
            HISTORICAL_PROVENANCE_SCHEMA_ID_V1
        ),
        PackArtifactRoleV1.HISTORICAL_SOURCE_LICENSE: (
            HISTORICAL_SOURCE_LICENSE_SCHEMA_ID_V1
        ),
    }
    for row in metadata_rows:
        raw = _artifact_bytes(original_bytes, row.artifact_id)
        if (
            row.storage_mode is not PackArtifactStorageModeV1.DIRECT
            or row.original_media_type != "application/json"
            or row.original_schema_id != metadata_schemas[row.role]
            or row.original_schema_version != 1
            or row.logical_identity_sha256 != hashlib.sha256(raw).hexdigest()
        ):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
                "historical capability, provenance, and license records must be direct canonical JSON",
            )
    try:
        capabilities = HistoricalCapabilityRecordV1.from_canonical_bytes(
            _artifact_bytes(
                original_bytes,
                index.artifact(PackArtifactRoleV1.HISTORICAL_CAPABILITIES).artifact_id,
            )
        )
        provenance = HistoricalProvenanceRecordV1.from_canonical_bytes(
            _artifact_bytes(
                original_bytes,
                index.artifact(PackArtifactRoleV1.HISTORICAL_PROVENANCE).artifact_id,
            )
        )
        license_record = HistoricalSourceLicenseRecordV1.from_canonical_bytes(
            _artifact_bytes(
                original_bytes,
                index.artifact(PackArtifactRoleV1.HISTORICAL_SOURCE_LICENSE).artifact_id,
            )
        )
    except (TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
            "historical metadata failed its exact canonical contract",
        ) from error
    if type(license) is not PackLicenseV1 or license_record.license != license:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.HISTORICAL_LICENSE_MISMATCH,
            "historical source license record differs from the pack declaration",
        )
    if capabilities.content_mode is not license.content_mode:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.HISTORICAL_LICENSE_MISMATCH,
            "historical capability content mode differs from the source license",
        )
    if len({capabilities.dataset_id, provenance.dataset_id, license_record.dataset_id}) != 1:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
            "historical capability, provenance, and license records name different datasets",
        )

    content_rows = index.artifacts_for(PackArtifactRoleV1.HISTORICAL_SOURCE_CONTENT)
    reference_rows = index.artifacts_for(PackArtifactRoleV1.HISTORICAL_SOURCE_REFERENCE)
    expected_sources = {item.source_id: item for item in provenance.sources}
    if any(item.license_id != license.license_id for item in provenance.sources):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.HISTORICAL_LICENSE_MISMATCH,
            "historical provenance source license differs from the declared license",
        )
    if capabilities.source_count != len(expected_sources):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            "historical capability source count differs from provenance",
        )
    if license.content_mode is PackContentModeV1.SELF_CONTAINED:
        if reference_rows or not content_rows or len(content_rows) != len(expected_sources):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.HISTORICAL_REFERENCE_VIOLATION,
                "SELF_CONTAINED historical packs require every source payload and no references",
            )
        for row in content_rows:
            source = expected_sources.get(row.artifact_id)
            if (
                source is None
                or source.source_sha256 != row.original_sha256
                or source.media_type != row.original_media_type
                or row.logical_identity_sha256 != row.original_sha256
            ):
                raise DomainPackRefused(
                    DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
                    f"historical source bytes differ from provenance: {row.artifact_id}",
                )
    else:
        if content_rows or not reference_rows or len(reference_rows) != len(expected_sources):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.HISTORICAL_REFERENCE_VIOLATION,
                "REFERENCE_ONLY historical packs require references and prohibit source payloads",
            )
        for row in reference_rows:
            if (
                row.storage_mode is not PackArtifactStorageModeV1.DIRECT
                or row.original_media_type != "application/json"
                or row.original_schema_id
                != HISTORICAL_SOURCE_REFERENCE_SCHEMA_ID_V1
                or row.original_schema_version != 1
            ):
                raise DomainPackRefused(
                    DomainPackRefusalCodeV1.HISTORICAL_REFERENCE_VIOLATION,
                    "historical source references must be direct canonical JSON",
                )
            try:
                reference = HistoricalSourceReferenceV1.from_canonical_bytes(
                    _artifact_bytes(original_bytes, row.artifact_id)
                )
            except (TypeError, ValueError) as error:
                raise DomainPackRefused(
                    DomainPackRefusalCodeV1.HISTORICAL_REFERENCE_VIOLATION,
                    "historical source reference failed its exact schema",
                ) from error
            source = expected_sources.get(reference.source_id)
            if (
                source is None
                or row.artifact_id != reference.source_id
                or source.source_uri != reference.source_uri
                or source.source_sha256 != reference.source_sha256
                or source.media_type != reference.media_type
                or row.logical_identity_sha256
                != hashlib.sha256(reference.canonical_bytes()).hexdigest()
            ):
                raise DomainPackRefused(
                    DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
                    f"historical source reference differs from provenance: {row.artifact_id}",
                )

    for row in index.artifacts_for(PackArtifactRoleV1.HISTORICAL_DERIVED_EVIDENCE):
        raw = _artifact_bytes(original_bytes, row.artifact_id)
        if (
            row.storage_mode is not PackArtifactStorageModeV1.DIRECT
            or row.original_media_type != "application/json"
        ):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
                "historical derived evidence must be direct canonical JSON",
            )
        try:
            load_canonical_json_bytes(raw, "historical derived evidence")
        except (TypeError, ValueError) as error:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
                "historical derived evidence is not canonical JSON",
            ) from error
        if row.logical_identity_sha256 != hashlib.sha256(raw).hexdigest():
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
                "historical derived evidence identity differs from canonical bytes",
            )


def _artifact_bytes(values: Mapping[str, bytes], artifact_id: str) -> bytes:
    raw = values.get(artifact_id)
    if type(raw) is not bytes:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            f"historical artifact bytes are absent: {artifact_id}",
        )
    return raw


def _exact_record(
    raw: bytes,
    fields: set[str],
    schema_id: str,
    label: str,
) -> dict[str, object]:
    payload = _exact_object(load_canonical_json_bytes(raw, label), fields, label)
    if payload["schema_id"] != schema_id or payload["schema_version"] != 1:
        raise ValueError(f"{label} schema differs")
    return payload


def _exact_object(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} fields differ")
    return value


def _text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TypeError(f"{key} must be text")
    return value


def _int(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value


def _bool(payload: dict[str, object], key: str) -> bool:
    value = payload[key]
    if type(value) is not bool:
        raise TypeError(f"{key} must be a boolean")
    return value


def _require_round_trip(actual: bytes, expected: bytes, label: str) -> None:
    if actual != expected:
        raise ValueError(f"{label} changed during canonical reconstruction")


__all__ = [
    "HISTORICAL_CAPABILITY_SCHEMA_ID_V1",
    "HISTORICAL_PACK_ADAPTER_ID_V1",
    "HISTORICAL_PACK_ADAPTER_V1",
    "HISTORICAL_PROVENANCE_SCHEMA_ID_V1",
    "HISTORICAL_SOURCE_LICENSE_SCHEMA_ID_V1",
    "HISTORICAL_SOURCE_REFERENCE_SCHEMA_ID_V1",
    "HistoricalCapabilityRecordV1",
    "HistoricalProvenanceRecordV1",
    "HistoricalSourceLicenseRecordV1",
    "HistoricalSourceProvenanceV1",
    "HistoricalSourceReferenceV1",
    "validate_historical_pack",
]
