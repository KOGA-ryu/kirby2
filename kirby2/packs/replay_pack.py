"""Replay-pack adapter preserving the owning run and artifact identities."""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import ClassVar

from kirby2 import __version__
from kirby2.research.models import ArtifactReference, ArtifactType, RunManifest

from .formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    require_data_identifier,
    require_nfc_text,
    require_sha256,
)
from .models import PackTypeV1
from .types import (
    DomainPackAdapterContractV1,
    DomainPackIndexV1,
    DomainPackRefusalCodeV1,
    DomainPackRefused,
    PackArtifactRoleV1,
    PackArtifactStorageModeV1,
    validate_adapter_inventory,
)


REPLAY_PACK_ADAPTER_ID_V1 = "KIRBY2_REPLAY_PACK_ADAPTER_V1"
REPLAY_COMPATIBILITY_SCHEMA_ID_V1 = "KIRBY2_REPLAY_PACK_COMPATIBILITY_V1"
REPLAY_RESULT_BINDING_SCHEMA_ID_V1 = "KIRBY2_REPLAY_RESULT_BINDING_V1"
REPLAY_RUN_MANIFEST_SCHEMA_ID_V1 = "KIRBY2_RUN_MANIFEST_V2"
REPLAY_ENGINE_COMPONENT_ID_V1 = "KIRBY2_ENGINE_V1"
REPLAY_RENDERER_COMPONENT_ID_V1 = "KIRBY2_REVIEW_REPLAY_STUDIO_V1"


def _roles(*items: PackArtifactRoleV1) -> tuple[PackArtifactRoleV1, ...]:
    return tuple(sorted(items, key=lambda item: item.value))


_REGISTERED_REPLAY_ROLES = frozenset(
    {
        PackArtifactRoleV1.REPLAY_CHECKPOINT,
        PackArtifactRoleV1.REPLAY_EVENT_ARTIFACT,
        PackArtifactRoleV1.REPLAY_RESULT_ARTIFACT,
        PackArtifactRoleV1.REPLAY_REGISTERED_ARTIFACT,
    }
)


REPLAY_PACK_ADAPTER_V1 = DomainPackAdapterContractV1(
    pack_type=PackTypeV1.REPLAY,
    adapter_id=REPLAY_PACK_ADAPTER_ID_V1,
    adapter_version=1,
    compiler_component_id="KIRBY2_REPLAY_PACK_COMPILER_V1",
    compiler_version="0.1.0",
    required_roles=_roles(
        PackArtifactRoleV1.REPLAY_RUN_MANIFEST,
        PackArtifactRoleV1.REPLAY_COMPATIBILITY,
        PackArtifactRoleV1.REPLAY_RESULT_BINDING,
    ),
    allowed_roles=_roles(
        PackArtifactRoleV1.REPLAY_RUN_MANIFEST,
        PackArtifactRoleV1.REPLAY_COMPATIBILITY,
        PackArtifactRoleV1.REPLAY_RESULT_BINDING,
        PackArtifactRoleV1.REPLAY_CHECKPOINT,
        PackArtifactRoleV1.REPLAY_EVENT_ARTIFACT,
        PackArtifactRoleV1.REPLAY_RESULT_ARTIFACT,
        PackArtifactRoleV1.REPLAY_REGISTERED_ARTIFACT,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    multiple_roles=_roles(
        PackArtifactRoleV1.REPLAY_CHECKPOINT,
        PackArtifactRoleV1.REPLAY_EVENT_ARTIFACT,
        PackArtifactRoleV1.REPLAY_RESULT_ARTIFACT,
        PackArtifactRoleV1.REPLAY_REGISTERED_ARTIFACT,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    primary_roles=_roles(PackArtifactRoleV1.REPLAY_RUN_MANIFEST),
    supports_replay_equivalence=True,
)


@dataclass(frozen=True, slots=True)
class ReplayCompatibilityRecordV1:
    run_id: str
    run_manifest_sha256: str
    engine_component_id: str
    engine_version: str
    renderer_component_id: str
    renderer_version: str
    renderer_payload_policy: str = "INSTALLED_RENDERER_REQUIRED_DATA_ONLY_PACK"

    schema_id: ClassVar[str] = REPLAY_COMPATIBILITY_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        _run_id(self.run_id)
        require_sha256(self.run_manifest_sha256, "replay run-manifest digest")
        require_data_identifier(self.engine_component_id, "replay engine component ID")
        require_nfc_text(self.engine_version, "replay engine version", maximum_bytes=128)
        require_data_identifier(self.renderer_component_id, "replay renderer component ID")
        require_nfc_text(self.renderer_version, "replay renderer version", maximum_bytes=128)
        if self.engine_component_id != REPLAY_ENGINE_COMPONENT_ID_V1:
            raise ValueError("replay pack engine component differs")
        if self.renderer_component_id != REPLAY_RENDERER_COMPONENT_ID_V1:
            raise ValueError("replay pack renderer component differs")
        if self.renderer_payload_policy != "INSTALLED_RENDERER_REQUIRED_DATA_ONLY_PACK":
            raise ValueError("replay renderer payload policy differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "engine_component_id": self.engine_component_id,
            "engine_version": self.engine_version,
            "renderer_component_id": self.renderer_component_id,
            "renderer_payload_policy": self.renderer_payload_policy,
            "renderer_version": self.renderer_version,
            "run_id": self.run_id,
            "run_manifest_sha256": self.run_manifest_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ReplayCompatibilityRecordV1:
        payload = _record(
            raw,
            {
                "engine_component_id",
                "engine_version",
                "renderer_component_id",
                "renderer_payload_policy",
                "renderer_version",
                "run_id",
                "run_manifest_sha256",
                "schema_id",
                "schema_version",
            },
            cls.schema_id,
            "replay compatibility record",
        )
        restored = cls(
            run_id=_text(payload, "run_id"),
            run_manifest_sha256=_text(payload, "run_manifest_sha256"),
            engine_component_id=_text(payload, "engine_component_id"),
            engine_version=_text(payload, "engine_version"),
            renderer_component_id=_text(payload, "renderer_component_id"),
            renderer_version=_text(payload, "renderer_version"),
            renderer_payload_policy=_text(payload, "renderer_payload_policy"),
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("replay compatibility record changed during restoration")
        return restored


@dataclass(frozen=True, slots=True)
class ReplayResultBindingV1:
    run_id: str
    configuration_sha256: str
    evidence_sha256: str
    result_sha256: str
    registered_artifact_inventory_sha256: str

    schema_id: ClassVar[str] = REPLAY_RESULT_BINDING_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        _run_id(self.run_id)
        require_sha256(self.configuration_sha256, "replay configuration identity")
        require_sha256(self.evidence_sha256, "replay evidence identity")
        require_sha256(self.result_sha256, "replay result identity")
        require_sha256(
            self.registered_artifact_inventory_sha256,
            "replay registered-artifact inventory identity",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "configuration_sha256": self.configuration_sha256,
            "evidence_sha256": self.evidence_sha256,
            "registered_artifact_inventory_sha256": (
                self.registered_artifact_inventory_sha256
            ),
            "result_sha256": self.result_sha256,
            "run_id": self.run_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_manifest(cls, manifest: RunManifest) -> ReplayResultBindingV1:
        if type(manifest) is not RunManifest:
            raise TypeError("replay result binding requires RunManifest")
        return cls(
            run_id=manifest.run_id,
            configuration_sha256=manifest.configuration_digest,
            evidence_sha256=manifest.evidence_digest,
            result_sha256=manifest.result_digest,
            registered_artifact_inventory_sha256=(
                registered_artifact_inventory_sha256(manifest)
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ReplayResultBindingV1:
        payload = _record(
            raw,
            {
                "configuration_sha256",
                "evidence_sha256",
                "registered_artifact_inventory_sha256",
                "result_sha256",
                "run_id",
                "schema_id",
                "schema_version",
            },
            cls.schema_id,
            "replay result binding",
        )
        restored = cls(
            run_id=_text(payload, "run_id"),
            configuration_sha256=_text(payload, "configuration_sha256"),
            evidence_sha256=_text(payload, "evidence_sha256"),
            result_sha256=_text(payload, "result_sha256"),
            registered_artifact_inventory_sha256=_text(
                payload,
                "registered_artifact_inventory_sha256",
            ),
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("replay result binding changed during restoration")
        return restored


def registered_artifact_inventory_sha256(manifest: RunManifest) -> str:
    if type(manifest) is not RunManifest:
        raise TypeError("registered-artifact inventory requires RunManifest")
    return hashlib.sha256(
        canonical_json_bytes([item.as_dict() for item in manifest.artifacts])
    ).hexdigest()


def replay_role_for_artifact(reference: ArtifactReference) -> PackArtifactRoleV1:
    """Classify an already-registered artifact by semantic type, never filename alone."""

    if type(reference) is not ArtifactReference:
        raise TypeError("replay artifact classification requires ArtifactReference")
    if reference.artifact_type in {
        ArtifactType.FULL_DAY_CHECKPOINT,
        ArtifactType.FULL_DAY_CHECKPOINT_INDEX,
    }:
        return PackArtifactRoleV1.REPLAY_CHECKPOINT
    if reference.artifact_type in {
        ArtifactType.FULL_DAY_OUTER_EVENT_LEDGER,
        ArtifactType.FULL_DAY_SUBSYSTEM_LEDGER,
        ArtifactType.FULL_DAY_QUALIFICATION_LEDGER,
    } or (
        reference.artifact_type is ArtifactType.GENERIC
        and reference.name in {"events", "player_actions"}
    ):
        return PackArtifactRoleV1.REPLAY_EVENT_ARTIFACT
    if reference.artifact_type in {
        ArtifactType.FULL_DAY_SUMMARY,
        ArtifactType.FULL_DAY_QUALIFICATION,
        ArtifactType.FULL_DAY_DIAGNOSTICS,
        ArtifactType.FULL_DAY_WINDOW,
        ArtifactType.FULL_DAY_PROFILE_QUALIFICATION,
        ArtifactType.FULL_DAY_QUALIFICATION_RUN_PROOFS,
        ArtifactType.FULL_DAY_PERFORMANCE_EVIDENCE,
        ArtifactType.STRATEGY_LINEAGE_REPORT,
        ArtifactType.STRATEGY_SCIENTIFIC_OUTCOME,
    }:
        return PackArtifactRoleV1.REPLAY_RESULT_ARTIFACT
    return PackArtifactRoleV1.REPLAY_REGISTERED_ARTIFACT


def replay_registered_schema_id(reference: ArtifactReference) -> str:
    if type(reference) is not ArtifactReference:
        raise TypeError("replay registered schema requires ArtifactReference")
    return f"KIRBY2_REGISTERED_{reference.artifact_type.value}_ARTIFACT_V1"


def validate_replay_pack(
    index: DomainPackIndexV1,
    original_bytes: Mapping[str, bytes],
) -> None:
    """Reconstruct the run manifest and bind every registered artifact exactly."""

    validate_adapter_inventory(
        REPLAY_PACK_ADAPTER_V1,
        index.pack_type,
        index.primary_artifact_id,
        index.artifacts,
    )
    manifest_row = index.artifact(PackArtifactRoleV1.REPLAY_RUN_MANIFEST)
    manifest_raw = _artifact_bytes(original_bytes, manifest_row.artifact_id)
    if (
        manifest_row.storage_mode is not PackArtifactStorageModeV1.DIRECT
        or manifest_row.original_media_type != "application/toml"
        or manifest_row.original_schema_id != REPLAY_RUN_MANIFEST_SCHEMA_ID_V1
        or manifest_row.original_schema_version != 2
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.REPLAY_IDENTITY_MISMATCH,
            "replay run manifest must be direct canonical TOML",
        )
    try:
        payload = tomllib.loads(manifest_raw.decode("utf-8"))
        manifest = RunManifest.from_dict(payload)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.REPLAY_IDENTITY_MISMATCH,
            "replay run manifest failed its owning parser",
        ) from error
    if manifest.to_toml().encode("utf-8") != manifest_raw:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.REPLAY_IDENTITY_MISMATCH,
            "replay run manifest is not exact canonical TOML",
        )
    if manifest_row.logical_identity_sha256 != hashlib.sha256(
        manifest.run_id.encode("ascii")
    ).hexdigest():
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.REPLAY_IDENTITY_MISMATCH,
            "pack-local identity does not retain the authoritative run ID",
        )

    compatibility_row = index.artifact(PackArtifactRoleV1.REPLAY_COMPATIBILITY)
    result_row = index.artifact(PackArtifactRoleV1.REPLAY_RESULT_BINDING)
    if any(
        row.storage_mode is not PackArtifactStorageModeV1.DIRECT
        or row.original_media_type != "application/json"
        or row.original_schema_version != 1
        or row.original_schema_id
        != (
            REPLAY_COMPATIBILITY_SCHEMA_ID_V1
            if row.role is PackArtifactRoleV1.REPLAY_COMPATIBILITY
            else REPLAY_RESULT_BINDING_SCHEMA_ID_V1
        )
        for row in (compatibility_row, result_row)
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.REPLAY_COMPATIBILITY_MISMATCH,
            "replay compatibility and result bindings must be direct canonical JSON",
        )
    try:
        compatibility = ReplayCompatibilityRecordV1.from_canonical_bytes(
            _artifact_bytes(original_bytes, compatibility_row.artifact_id)
        )
        result = ReplayResultBindingV1.from_canonical_bytes(
            _artifact_bytes(original_bytes, result_row.artifact_id)
        )
    except (TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.REPLAY_COMPATIBILITY_MISMATCH,
            "replay compatibility or result binding failed its exact schema",
        ) from error
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    if (
        compatibility.run_id != manifest.run_id
        or compatibility.run_manifest_sha256 != manifest_sha256
        or compatibility.engine_version != manifest.software_version
        or compatibility.renderer_version != __version__
        or result != ReplayResultBindingV1.from_manifest(manifest)
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.REPLAY_COMPATIBILITY_MISMATCH,
            "replay engine, renderer, result, or run binding differs",
        )
    for row in (compatibility_row, result_row):
        if row.logical_identity_sha256 != hashlib.sha256(
            _artifact_bytes(original_bytes, row.artifact_id)
        ).hexdigest():
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.REPLAY_IDENTITY_MISMATCH,
                f"replay metadata logical identity differs: {row.artifact_id}",
            )

    registered_rows = tuple(
        item for item in index.artifacts if item.role in _REGISTERED_REPLAY_ROLES
    )
    if not registered_rows or len(registered_rows) != len(manifest.artifacts):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.REPLAY_IDENTITY_MISMATCH,
            "replay pack does not retain the complete registered artifact inventory",
        )
    references_by_path = {item.relative_path: item for item in manifest.artifacts}
    if len(references_by_path) != len(manifest.artifacts):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.REPLAY_IDENTITY_MISMATCH,
            "run manifest contains duplicate artifact paths",
        )
    seen: set[str] = set()
    for row in registered_rows:
        reference = references_by_path.get(row.original_path)
        if (
            row.original_media_type.casefold()
            in {
                "application/javascript",
                "application/x-javascript",
                "text/css",
                "text/html",
                "text/javascript",
            }
            or PurePosixPath(row.original_path).suffix.casefold()
            in {".css", ".htm", ".html", ".js", ".mjs"}
        ):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.RENDERER_INJECTION_REFUSED,
                "replay packs require the installed compatible renderer and cannot carry renderer code",
            )
        if (
            reference is None
            or row.original_path in seen
            or row.role is not replay_role_for_artifact(reference)
            or row.original_sha256 != reference.sha256
            or row.logical_identity_sha256 != reference.sha256
            or row.original_schema_version != reference.schema_version
            or row.original_schema_id != replay_registered_schema_id(reference)
            or row.original_media_type != reference.media_type
        ):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.REPLAY_IDENTITY_MISMATCH,
                f"replay registered artifact differs from its run manifest: {row.artifact_id}",
            )
        raw = _artifact_bytes(original_bytes, row.artifact_id)
        if hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.REPLAY_IDENTITY_MISMATCH,
                f"replay registered artifact bytes differ: {row.artifact_id}",
            )
        seen.add(row.original_path)
    if seen != set(references_by_path):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.REPLAY_IDENTITY_MISMATCH,
            "replay pack omitted a registered run artifact",
        )


def _run_id(value: object) -> str:
    if type(value) is not str or not value.startswith("run-") or len(value) != 28:
        raise ValueError("replay run ID is invalid")
    require_sha256(value[4:] + "0" * 40, "replay run ID body")
    return value


def _artifact_bytes(values: Mapping[str, bytes], artifact_id: str) -> bytes:
    raw = values.get(artifact_id)
    if type(raw) is not bytes:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            f"replay artifact bytes are absent: {artifact_id}",
        )
    return raw


def _record(
    raw: bytes,
    fields: set[str],
    schema_id: str,
    label: str,
) -> dict[str, object]:
    value = load_canonical_json_bytes(raw, label)
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} fields differ")
    if value["schema_id"] != schema_id or value["schema_version"] != 1:
        raise ValueError(f"{label} schema differs")
    return value


def _text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TypeError(f"{key} must be text")
    return value


__all__ = [
    "REPLAY_COMPATIBILITY_SCHEMA_ID_V1",
    "REPLAY_ENGINE_COMPONENT_ID_V1",
    "REPLAY_PACK_ADAPTER_ID_V1",
    "REPLAY_PACK_ADAPTER_V1",
    "REPLAY_RENDERER_COMPONENT_ID_V1",
    "REPLAY_RESULT_BINDING_SCHEMA_ID_V1",
    "REPLAY_RUN_MANIFEST_SCHEMA_ID_V1",
    "ReplayCompatibilityRecordV1",
    "ReplayResultBindingV1",
    "registered_artifact_inventory_sha256",
    "replay_role_for_artifact",
    "replay_registered_schema_id",
    "validate_replay_pack",
]
