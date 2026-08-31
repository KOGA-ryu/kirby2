"""Deterministic builders and verifiers for WO39-D domain packs.

All source filesystem access is confined to one explicit source directory.  Archive
construction is in-memory, normalized, fully preflighted, and then passed through the
owning domain adapter before a caller may persist or install it.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import re
import stat
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from kirby2 import __version__

from .analysis_pack import (
    ANALYSIS_PACK_ADAPTER_V1,
    ANALYSIS_PROVENANCE_SCHEMA_ID_V1,
    AnalysisProvenanceRecordV1,
    validate_analysis_pack,
)
from .archive import PackArchivePreflightV1, preflight_pack_archive_bytes
from .curriculum_pack import (
    CURRICULUM_PACK_ADAPTER_V1,
    validate_curriculum_pack,
)
from .dependencies import PackRuntimeEnvironmentV1, semver_satisfies
from .formats import (
    K2PACK_MANIFEST_PATH,
    K2PACK_ZIP_COMPRESSLEVEL,
    K2PACK_ZIP_COMPRESSION,
    canonical_manifest_bytes,
    inspect_payload_format_claim,
    load_canonical_json_bytes,
    normalized_archive_paths,
    normalized_zip_info,
    require_relative_pack_path,
)
from .identity import verify_pack_payload_identity
from .historical_pack import HISTORICAL_PACK_ADAPTER_V1, validate_historical_pack
from .lesson_pack import LESSON_PACK_ADAPTER_V1, validate_lesson_pack
from .models import (
    PackCompatibilityLevelV1,
    PackCompatibilityV1,
    PackContentFormatV1,
    PackContentModeV1,
    PackCreatorV1,
    PackEntrypointV1,
    PackFileV1,
    PackLicenseV1,
    PackManifestV1,
    PackProvenanceV1,
    PackRedistributionPolicyV1,
    PackSchemaRequirementV1,
    PackTypeV1,
    PackVersionRequirementV1,
)
from .profile_pack import PROFILE_PACK_ADAPTER_V1, validate_profile_pack
from .replay_pack import (
    REPLAY_COMPATIBILITY_SCHEMA_ID_V1,
    REPLAY_ENGINE_COMPONENT_ID_V1,
    REPLAY_PACK_ADAPTER_V1,
    REPLAY_RENDERER_COMPONENT_ID_V1,
    REPLAY_RESULT_BINDING_SCHEMA_ID_V1,
    REPLAY_RUN_MANIFEST_SCHEMA_ID_V1,
    ReplayCompatibilityRecordV1,
    ReplayResultBindingV1,
    replay_role_for_artifact,
    replay_registered_schema_id,
    validate_replay_pack,
)
from .research_pack import RESEARCH_PACK_ADAPTER_V1, validate_research_pack
from .scenario_pack import SCENARIO_PACK_ADAPTER_V1, validate_scenario_pack
from .strategy_pack import STRATEGY_PACK_ADAPTER_V1, validate_strategy_pack
from .types import (
    DOMAIN_PACK_INDEX_SCHEMA_ID,
    DOMAIN_PACK_INDEX_SCHEMA_VERSION,
    MAX_DOMAIN_TOTAL_BYTES_V1,
    MAX_ORIGINAL_ARTIFACT_BYTES_V1,
    PRESERVED_EXACT_BYTES_SCHEMA_ID,
    PRESERVED_EXACT_BYTES_SCHEMA_VERSION,
    DomainArtifactIdentityV1,
    DomainPackAdapterContractV1,
    DomainPackIndexV1,
    DomainPackRefusalCodeV1,
    DomainPackRefused,
    PackArtifactRoleV1,
    PackArtifactStorageModeV1,
    PackBuildSpecificationV1,
    PackSourceArtifactV1,
    PreservedExactBytesV1,
    validate_adapter_inventory,
)


PACK_SOURCE_FILENAME_V1 = "pack-source.toml"
DOMAIN_PACK_INDEX_PATH_V1 = "domain/index.json"
PACK_BUILD_MAX_SOURCE_DEFINITION_BYTES_V1 = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024


_DIRECT_DECLARATIONS: dict[PackContentFormatV1, tuple[str, str]] = {
    PackContentFormatV1.TOML: (".toml", "application/toml"),
    PackContentFormatV1.PARQUET: (".parquet", "application/vnd.apache.parquet"),
    PackContentFormatV1.CANONICAL_JSON: (".json", "application/json"),
    PackContentFormatV1.CANONICAL_EVENT_STREAM: (".jsonl", "application/x-ndjson"),
    PackContentFormatV1.REPORT_DATA: (
        ".report.json",
        "application/vnd.kirby2.report+json",
    ),
}
_BINARY_DIRECT_SUFFIXES: dict[str, str] = {
    "application/vnd.apache.arrow.file": ".arrow",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
}


@dataclass(frozen=True, slots=True)
class DomainPackBuildV1:
    """One complete normalized archive that already passed domain verification."""

    manifest: PackManifestV1
    index: DomainPackIndexV1
    archive_bytes: bytes
    preflight: PackArchivePreflightV1

    def __post_init__(self) -> None:
        if type(self.manifest) is not PackManifestV1:
            raise TypeError("domain pack build manifest is invalid")
        if type(self.index) is not DomainPackIndexV1:
            raise TypeError("domain pack build index is invalid")
        if type(self.archive_bytes) is not bytes or not self.archive_bytes:
            raise ValueError("domain pack build archive bytes are empty")
        if type(self.preflight) is not PackArchivePreflightV1:
            raise TypeError("domain pack build preflight is invalid")
        if (
            self.manifest != self.preflight.manifest
            or self.manifest.pack_id != self.preflight.pack_id
            or self.manifest.pack_type is not self.index.pack_type
        ):
            raise ValueError("domain pack build contracts disagree")

    @property
    def transport_sha256(self) -> str:
        return self.preflight.transport_sha256


@dataclass(frozen=True, slots=True)
class DomainPackVerificationV1:
    """Complete hostile-preflight plus owning-adapter verification result."""

    preflight: PackArchivePreflightV1
    index: DomainPackIndexV1
    original_artifact_count: int
    original_total_byte_count: int

    def __post_init__(self) -> None:
        if type(self.preflight) is not PackArchivePreflightV1:
            raise TypeError("domain pack verification preflight is invalid")
        if type(self.index) is not DomainPackIndexV1:
            raise TypeError("domain pack verification index is invalid")
        if (
            type(self.original_artifact_count) is not int
            or self.original_artifact_count <= 0
            or self.original_artifact_count != len(self.index.artifacts)
        ):
            raise ValueError("domain pack verification artifact count is inconsistent")
        if (
            type(self.original_total_byte_count) is not int
            or self.original_total_byte_count <= 0
            or self.original_total_byte_count
            != sum(item.original_byte_count for item in self.index.artifacts)
        ):
            raise ValueError("domain pack verification byte count is inconsistent")

    @property
    def manifest(self) -> PackManifestV1:
        return self.preflight.manifest

    @property
    def pack_id(self) -> str:
        return self.preflight.pack_id

    def as_dict(self) -> dict[str, object]:
        return {
            "domain_identity_sha256": self.index.domain_identity_sha256,
            "original_artifact_count": self.original_artifact_count,
            "original_total_byte_count": self.original_total_byte_count,
            "pack_id": self.pack_id,
            "pack_type": self.index.pack_type.value,
            "transport_sha256": self.preflight.transport_sha256,
            "validation_policy_id": self.preflight.validation_policy_id,
        }


def supported_domain_pack_types_v1() -> tuple[PackTypeV1, ...]:
    return tuple(sorted(_adapter_contracts(), key=lambda item: item.value))


def domain_pack_adapter_v1(pack_type: PackTypeV1) -> DomainPackAdapterContractV1:
    if type(pack_type) is not PackTypeV1:
        raise TypeError("domain adapter lookup requires PackTypeV1")
    contract = _adapter_contracts().get(pack_type)
    if contract is None:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.UNSUPPORTED_PACK_TYPE,
            f"WO39-D does not build pack type {pack_type.value}",
        )
    return contract


def build_pack_source_directory(source_directory: Path) -> DomainPackBuildV1:
    """Build one ``pack-source.toml`` directory without following source symlinks."""

    root = _trusted_source_directory(Path(source_directory))
    definition_raw = _read_confined_regular_file(
        root,
        PACK_SOURCE_FILENAME_V1,
        maximum_bytes=PACK_BUILD_MAX_SOURCE_DEFINITION_BYTES_V1,
    )
    try:
        payload = tomllib.loads(definition_raw.decode("utf-8"))
        specification = PackBuildSpecificationV1.from_dict(payload)
    except DomainPackRefused:
        raise
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SOURCE_DEFINITION_INVALID,
            "pack-source.toml failed its exact typed schema",
        ) from error
    raw_artifacts: dict[str, bytes] = {}
    total = 0
    for artifact in specification.artifacts:
        raw = _read_confined_regular_file(
            root,
            artifact.source_path,
            maximum_bytes=MAX_ORIGINAL_ARTIFACT_BYTES_V1,
        )
        total += len(raw)
        if total > MAX_DOMAIN_TOTAL_BYTES_V1:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.SOURCE_TOO_LARGE,
                "pack source artifacts exceed the total V1 byte limit",
            )
        raw_artifacts[artifact.artifact_id] = raw
    return build_domain_pack(
        specification,
        raw_artifacts,
        source_definition_sha256=hashlib.sha256(definition_raw).hexdigest(),
    )


def build_registered_run_pack(
    store_root: Path,
    run_id: str,
) -> DomainPackBuildV1:
    """Export one verified run from its exact registered artifact inventory.

    The run directory is never enumerated.  The canonical manifest is loaded by its
    owning store, verified, and then each declared ``ArtifactReference`` is read by
    exact confined path with no-follow semantics and rechecked against its digest.
    Privacy-sensitive learner and instructor runs are refused here because their
    ordinary run manifests do not contain the governed consent/redaction bundle
    required by ``RESEARCH_PACK_ADAPTER_V1``.
    """

    from kirby2.research.models import RunManifest, RunType
    from kirby2.research.store import RunStore

    root = _trusted_source_directory(Path(store_root))
    store = RunStore(root)
    try:
        verification = store.verify_run(run_id)
        manifest = store.load_manifest(run_id)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.RUN_VERIFICATION_FAILED,
            f"registered run could not be verified: {run_id}",
        ) from error
    if not verification.passed:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.RUN_VERIFICATION_FAILED,
            "registered run verification failed: " + "; ".join(verification.failures),
        )
    if type(manifest) is not RunManifest or manifest.schema_version < 2:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.RUN_EXPORT_UNSUPPORTED,
            "run export requires the typed artifact manifest schema",
        )

    sensitive_types = {
        RunType.LEARNER_UPDATE,
        RunType.INSTRUCTOR_ASSIGNMENT,
        RunType.INSTRUCTOR_ATTEMPT,
        RunType.INSTRUCTOR_RUBRIC,
        RunType.INSTRUCTOR_REVIEW,
    }
    if manifest.run_type in sensitive_types:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.RESEARCH_CONSENT_REQUIRED,
            "privacy-sensitive runs must first become a governed consent/redaction export bundle",
        )

    run_directory = _trusted_source_directory(store.run_directory(run_id))
    manifest_raw = _read_confined_regular_file(
        run_directory,
        "manifest.toml",
        maximum_bytes=PACK_BUILD_MAX_SOURCE_DEFINITION_BYTES_V1,
    )
    if (
        manifest.to_toml().encode("utf-8") != manifest_raw
        or hashlib.sha256(manifest_raw).hexdigest()
        != hashlib.sha256(manifest.to_toml().encode("utf-8")).hexdigest()
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.RUN_ARTIFACT_UNSAFE,
            "verified run manifest changed before export capture",
        )

    registered: list[tuple[object, bytes]] = []
    total = len(manifest_raw)
    for reference in manifest.artifacts:
        raw = _read_confined_regular_file(
            run_directory,
            reference.relative_path,
            maximum_bytes=MAX_ORIGINAL_ARTIFACT_BYTES_V1,
        )
        if hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.RUN_ARTIFACT_UNSAFE,
                f"registered artifact changed before export capture: {reference.name}",
            )
        total += len(raw)
        if total > MAX_DOMAIN_TOTAL_BYTES_V1:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.SOURCE_TOO_LARGE,
                "registered run artifacts exceed the domain-pack byte bound",
            )
        registered.append((reference, raw))

    analysis_types = {
        RunType.LESSON,
        RunType.LESSON_MINING,
        RunType.LESSON_REVIEW,
        RunType.LESSON_BUILD,
    }
    if manifest.run_type in analysis_types:
        specification, originals = _analysis_run_export_inputs(
            manifest,
            manifest_raw,
            tuple(registered),
        )
    else:
        specification, originals = _replay_run_export_inputs(
            manifest,
            manifest_raw,
            tuple(registered),
        )
    return build_domain_pack(specification, originals)


def _replay_run_export_inputs(
    manifest: object,
    manifest_raw: bytes,
    registered: tuple[tuple[object, bytes], ...],
) -> tuple[PackBuildSpecificationV1, dict[str, bytes]]:
    from kirby2.research.models import ArtifactReference, RunManifest

    if type(manifest) is not RunManifest or not registered:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.RUN_EXPORT_UNSUPPORTED,
            "replay export requires a typed run with registered artifacts",
        )
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    compatibility = ReplayCompatibilityRecordV1(
        run_id=manifest.run_id,
        run_manifest_sha256=manifest_sha256,
        engine_component_id=REPLAY_ENGINE_COMPONENT_ID_V1,
        engine_version=manifest.software_version,
        renderer_component_id=REPLAY_RENDERER_COMPONENT_ID_V1,
        renderer_version=__version__,
    )
    result = ReplayResultBindingV1.from_manifest(manifest)
    originals: dict[str, bytes] = {
        "replay-compatibility": compatibility.canonical_bytes(),
        "replay-result-binding": result.canonical_bytes(),
        "run-manifest": manifest_raw,
    }
    declarations: list[PackSourceArtifactV1] = [
        PackSourceArtifactV1(
            artifact_id="replay-compatibility",
            role=PackArtifactRoleV1.REPLAY_COMPATIBILITY,
            source_path="generated/replay-compatibility.json",
            original_schema_id=REPLAY_COMPATIBILITY_SCHEMA_ID_V1,
            original_schema_version=1,
            original_media_type="application/json",
            storage_mode=PackArtifactStorageModeV1.DIRECT,
            logical_identity_kind="REPLAY_COMPATIBILITY_SHA256",
            logical_identity_sha256=hashlib.sha256(
                compatibility.canonical_bytes()
            ).hexdigest(),
            direct_content_format=PackContentFormatV1.CANONICAL_JSON,
        ),
        PackSourceArtifactV1(
            artifact_id="replay-result-binding",
            role=PackArtifactRoleV1.REPLAY_RESULT_BINDING,
            source_path="generated/replay-result-binding.json",
            original_schema_id=REPLAY_RESULT_BINDING_SCHEMA_ID_V1,
            original_schema_version=1,
            original_media_type="application/json",
            storage_mode=PackArtifactStorageModeV1.DIRECT,
            logical_identity_kind="REPLAY_RESULT_BINDING_SHA256",
            logical_identity_sha256=hashlib.sha256(
                result.canonical_bytes()
            ).hexdigest(),
            direct_content_format=PackContentFormatV1.CANONICAL_JSON,
        ),
        PackSourceArtifactV1(
            artifact_id="run-manifest",
            role=PackArtifactRoleV1.REPLAY_RUN_MANIFEST,
            source_path="manifest.toml",
            original_schema_id=REPLAY_RUN_MANIFEST_SCHEMA_ID_V1,
            original_schema_version=manifest.schema_version,
            original_media_type="application/toml",
            storage_mode=PackArtifactStorageModeV1.DIRECT,
            logical_identity_kind="RUN_ID",
            logical_identity_sha256=hashlib.sha256(
                manifest.run_id.encode("ascii")
            ).hexdigest(),
            direct_content_format=PackContentFormatV1.TOML,
        ),
    ]
    for ordinal, item in enumerate(registered):
        reference, raw = item
        if type(reference) is not ArtifactReference or type(raw) is not bytes:
            raise TypeError("registered replay inputs must retain typed references")
        artifact_id = _registered_artifact_id(ordinal, reference.sha256)
        storage_mode, content_format = _registered_storage(reference, raw)
        originals[artifact_id] = raw
        declarations.append(
            PackSourceArtifactV1(
                artifact_id=artifact_id,
                role=replay_role_for_artifact(reference),
                source_path=reference.relative_path,
                original_schema_id=replay_registered_schema_id(reference),
                original_schema_version=reference.schema_version,
                original_media_type=reference.media_type,
                storage_mode=storage_mode,
                logical_identity_kind="RUN_ARTIFACT_SHA256",
                logical_identity_sha256=reference.sha256,
                direct_content_format=content_format,
            )
        )
    return (
        PackBuildSpecificationV1(
            namespace="kirby2.local",
            name=f"{manifest.run_id}-replay",
            title=f"Kirby2 verified replay evidence {manifest.run_id}",
            version="1.0.0",
            creator=_local_run_export_creator(),
            pack_type=PackTypeV1.REPLAY,
            primary_artifact_id="run-manifest",
            dependencies=(),
            license=_local_evidence_license(),
            capability_labels=(
                "LOCAL_EVIDENCE",
                "REGISTERED_ARTIFACTS_ONLY",
                "RUN_IDENTITY_PRESERVED",
            ),
            artifacts=tuple(sorted(declarations, key=lambda item: item.artifact_id)),
        ),
        originals,
    )


def _analysis_run_export_inputs(
    manifest: object,
    manifest_raw: bytes,
    registered: tuple[tuple[object, bytes], ...],
) -> tuple[PackBuildSpecificationV1, dict[str, bytes]]:
    from kirby2.research.models import ArtifactReference, ArtifactType, RunManifest

    if type(manifest) is not RunManifest or not registered:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.RUN_EXPORT_UNSUPPORTED,
            "analysis export requires canonical registered report artifacts",
        )
    annotation_types = {
        ArtifactType.FULL_DAY_REVIEW_SELECTION,
        ArtifactType.FULL_DAY_REVIEW_PACKET,
        ArtifactType.FULL_DAY_REVIEWER_SIDECAR,
        ArtifactType.LESSON_REVIEW_SIDECAR,
        ArtifactType.LESSON_TECHNICAL_REVIEW_PACKET,
        ArtifactType.INSTRUCTOR_REVIEW_SIDECAR,
    }
    originals: dict[str, bytes] = {}
    declarations: list[PackSourceArtifactV1] = []
    report_ids: list[str] = []
    data_digests: list[str] = []
    for ordinal, item in enumerate(registered):
        reference, raw = item
        if type(reference) is not ArtifactReference or type(raw) is not bytes:
            raise TypeError("registered analysis inputs must retain typed references")
        if reference.media_type not in {
            "application/json",
            "application/vnd.kirby2.report+json",
        }:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.RUN_EXPORT_UNSUPPORTED,
                f"analysis export refuses non-report artifact: {reference.name}",
            )
        try:
            load_canonical_json_bytes(raw, "registered analysis artifact")
        except (TypeError, ValueError) as error:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.RUN_EXPORT_UNSUPPORTED,
                f"analysis export requires canonical JSON: {reference.name}",
            ) from error
        artifact_id = _registered_artifact_id(ordinal, reference.sha256)
        role = (
            PackArtifactRoleV1.ANALYSIS_ANNOTATIONS
            if reference.artifact_type in annotation_types
            else PackArtifactRoleV1.ANALYSIS_REPORT_DATA
        )
        if role is PackArtifactRoleV1.ANALYSIS_REPORT_DATA:
            report_ids.append(artifact_id)
        originals[artifact_id] = raw
        data_digests.append(reference.sha256)
        declarations.append(
            PackSourceArtifactV1(
                artifact_id=artifact_id,
                role=role,
                source_path=reference.relative_path,
                original_schema_id=replay_registered_schema_id(reference),
                original_schema_version=reference.schema_version,
                original_media_type=reference.media_type,
                storage_mode=PackArtifactStorageModeV1.DIRECT,
                logical_identity_kind="RUN_ARTIFACT_SHA256",
                logical_identity_sha256=reference.sha256,
                direct_content_format=(
                    PackContentFormatV1.REPORT_DATA
                    if reference.media_type == "application/vnd.kirby2.report+json"
                    else PackContentFormatV1.CANONICAL_JSON
                ),
            )
        )
    if not report_ids:
        first = declarations[0]
        declarations[0] = PackSourceArtifactV1(
            artifact_id=first.artifact_id,
            role=PackArtifactRoleV1.ANALYSIS_REPORT_DATA,
            source_path=first.source_path,
            original_schema_id=first.original_schema_id,
            original_schema_version=first.original_schema_version,
            original_media_type=first.original_media_type,
            storage_mode=first.storage_mode,
            logical_identity_kind=first.logical_identity_kind,
            logical_identity_sha256=first.logical_identity_sha256,
            direct_content_format=first.direct_content_format,
        )
        report_ids.append(first.artifact_id)
    provenance = AnalysisProvenanceRecordV1(
        source_run_id=manifest.run_id,
        source_run_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        source_artifact_sha256s=tuple(sorted(data_digests)),
    )
    originals["analysis-provenance"] = provenance.canonical_bytes()
    declarations.append(
        PackSourceArtifactV1(
            artifact_id="analysis-provenance",
            role=PackArtifactRoleV1.ANALYSIS_PROVENANCE,
            source_path="generated/analysis-provenance.json",
            original_schema_id=ANALYSIS_PROVENANCE_SCHEMA_ID_V1,
            original_schema_version=1,
            original_media_type="application/json",
            storage_mode=PackArtifactStorageModeV1.DIRECT,
            logical_identity_kind="ANALYSIS_PROVENANCE_SHA256",
            logical_identity_sha256=hashlib.sha256(
                provenance.canonical_bytes()
            ).hexdigest(),
            direct_content_format=PackContentFormatV1.CANONICAL_JSON,
        )
    )
    return (
        PackBuildSpecificationV1(
            namespace="kirby2.local",
            name=f"{manifest.run_id}-analysis",
            title=f"Kirby2 canonical analysis evidence {manifest.run_id}",
            version="1.0.0",
            creator=_local_run_export_creator(),
            pack_type=PackTypeV1.ANALYSIS,
            primary_artifact_id=report_ids[0],
            dependencies=(),
            license=_local_evidence_license(),
            capability_labels=(
                "CANONICAL_REPORT_DATA_ONLY",
                "LOCAL_EVIDENCE",
                "REGISTERED_ARTIFACTS_ONLY",
            ),
            artifacts=tuple(sorted(declarations, key=lambda item: item.artifact_id)),
        ),
        originals,
    )


def _registered_storage(
    reference: object,
    raw: bytes,
) -> tuple[PackArtifactStorageModeV1, PackContentFormatV1 | None]:
    from kirby2.research.models import ArtifactReference

    if type(reference) is not ArtifactReference or type(raw) is not bytes:
        raise TypeError("registered artifact storage selection requires exact inputs")
    if reference.media_type == "application/toml":
        return PackArtifactStorageModeV1.DIRECT, PackContentFormatV1.TOML
    if reference.media_type == "application/vnd.apache.parquet":
        return PackArtifactStorageModeV1.DIRECT, PackContentFormatV1.PARQUET
    if reference.media_type == "application/x-ndjson":
        return PackArtifactStorageModeV1.DIRECT, PackContentFormatV1.CANONICAL_EVENT_STREAM
    if reference.media_type in {"application/json", "application/vnd.kirby2.report+json"}:
        try:
            load_canonical_json_bytes(raw, "registered JSON artifact")
        except (TypeError, ValueError):
            return PackArtifactStorageModeV1.EXACT_BYTES_ENVELOPE, None
        return (
            PackArtifactStorageModeV1.DIRECT,
            (
                PackContentFormatV1.REPORT_DATA
                if reference.media_type == "application/vnd.kirby2.report+json"
                else PackContentFormatV1.CANONICAL_JSON
            ),
        )
    if reference.media_type in _BINARY_DIRECT_SUFFIXES:
        return PackArtifactStorageModeV1.DIRECT, PackContentFormatV1.BINARY_EVIDENCE
    return PackArtifactStorageModeV1.EXACT_BYTES_ENVELOPE, None


def _registered_artifact_id(ordinal: int, digest: str) -> str:
    return f"registered-{ordinal:04d}-{digest[:16]}"


def _local_run_export_creator() -> PackCreatorV1:
    return PackCreatorV1(
        display_name="Kirby2 local run exporter",
        identity_uri="urn:kirby2:local-run-exporter-v1",
    )


def _local_evidence_license() -> PackLicenseV1:
    return PackLicenseV1(
        license_id="KIRBY2_LOCAL_EVIDENCE_V1",
        license_name="Kirby2 local evidence; inspect source rights before redistribution",
        license_uri="urn:kirby2:license:local-evidence-v1",
        redistribution_policy=PackRedistributionPolicyV1.CONDITIONAL,
        content_mode=PackContentModeV1.SELF_CONTAINED,
    )


def build_domain_pack(
    specification: PackBuildSpecificationV1,
    original_artifacts: Mapping[str, bytes],
    *,
    source_definition_sha256: str | None = None,
) -> DomainPackBuildV1:
    """Build, preflight, and owning-adapter-verify one deterministic archive."""

    if type(specification) is not PackBuildSpecificationV1:
        raise TypeError("domain pack construction requires PackBuildSpecificationV1")
    if not isinstance(original_artifacts, Mapping):
        raise TypeError("domain pack original artifacts must be a mapping")
    originals = dict(original_artifacts)
    if any(type(key) is not str or type(value) is not bytes for key, value in originals.items()):
        raise TypeError("domain pack original artifact mapping requires text keys and bytes")
    expected_ids = tuple(item.artifact_id for item in specification.artifacts)
    if tuple(sorted(originals)) != expected_ids:
        missing = sorted(set(expected_ids) - set(originals))
        extra = sorted(set(originals) - set(expected_ids))
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            f"original artifact mapping differs: missing={missing}, extra={extra}",
        )
    if any(not raw or len(raw) > MAX_ORIGINAL_ARTIFACT_BYTES_V1 for raw in originals.values()):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SOURCE_TOO_LARGE,
            "one original artifact is empty or exceeds the V1 byte limit",
        )
    if sum(map(len, originals.values())) > MAX_DOMAIN_TOTAL_BYTES_V1:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SOURCE_TOO_LARGE,
            "original artifacts exceed the total V1 byte limit",
        )
    if source_definition_sha256 is not None:
        from .formats import require_sha256

        require_sha256(source_definition_sha256, "pack source-definition digest")

    contract = domain_pack_adapter_v1(specification.pack_type)
    _validate_content_license(specification, contract)
    validate_adapter_inventory(
        contract,
        specification.pack_type,
        specification.primary_artifact_id,
        specification.artifacts,
    )

    payloads: dict[str, bytes] = {}
    artifact_rows: list[DomainArtifactIdentityV1] = []
    inventory_rows: list[PackFileV1] = []
    for ordinal, declaration in enumerate(specification.artifacts):
        original = originals[declaration.artifact_id]
        original_sha256 = hashlib.sha256(original).hexdigest()
        logical_identity = (
            original_sha256
            if declaration.logical_identity_sha256 is None
            else declaration.logical_identity_sha256
        )
        payload_path, payload, content_format, media_type, schema_id, schema_version = (
            _materialize_artifact_payload(
                ordinal,
                declaration,
                original,
                original_sha256,
                logical_identity,
            )
        )
        inspect_payload_format_claim(
            payload,
            path=payload_path,
            content_format=content_format.value,
            media_type=media_type,
            schema_id=schema_id,
        )
        payloads[payload_path] = payload
        inventory_rows.append(
            PackFileV1(
                path=payload_path,
                byte_count=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                content_format=content_format,
                media_type=media_type,
                schema_id=schema_id,
                schema_version=schema_version,
            )
        )
        artifact_rows.append(
            DomainArtifactIdentityV1(
                artifact_id=declaration.artifact_id,
                role=declaration.role,
                payload_path=payload_path,
                original_path=declaration.source_path,
                original_schema_id=declaration.original_schema_id,
                original_schema_version=declaration.original_schema_version,
                original_media_type=declaration.original_media_type,
                original_byte_count=len(original),
                original_sha256=original_sha256,
                logical_identity_kind=declaration.logical_identity_kind,
                logical_identity_sha256=logical_identity,
                storage_mode=declaration.storage_mode,
            )
        )

    index = DomainPackIndexV1(
        pack_type=specification.pack_type,
        adapter_id=contract.adapter_id,
        adapter_version=contract.adapter_version,
        primary_artifact_id=specification.primary_artifact_id,
        artifacts=tuple(sorted(artifact_rows, key=lambda item: item.sort_key)),
    )
    _validate_domain(index, originals, license=specification.license)
    index_bytes = index.canonical_bytes()
    payloads[DOMAIN_PACK_INDEX_PATH_V1] = index_bytes
    inventory_rows.append(
        PackFileV1(
            path=DOMAIN_PACK_INDEX_PATH_V1,
            byte_count=len(index_bytes),
            sha256=hashlib.sha256(index_bytes).hexdigest(),
            content_format=PackContentFormatV1.CANONICAL_JSON,
            media_type="application/json",
            schema_id=DOMAIN_PACK_INDEX_SCHEMA_ID,
            schema_version=DOMAIN_PACK_INDEX_SCHEMA_VERSION,
        )
    )
    inventory = tuple(sorted(inventory_rows, key=lambda item: item.sort_key))
    schemas = _schema_requirements(inventory)
    compatibility = _compatibility(contract, schemas)
    provenance = [
        PackProvenanceV1(
            source_kind=item.role.value,
            source_id=item.artifact_id,
            source_sha256=item.original_sha256,
        )
        for item in index.artifacts
    ]
    if source_definition_sha256 is not None:
        provenance.append(
            PackProvenanceV1(
                source_kind="PACK_SOURCE_DEFINITION",
                source_id="pack-source",
                source_sha256=source_definition_sha256,
            )
        )
    manifest = PackManifestV1(
        namespace=specification.namespace,
        name=specification.name,
        title=specification.title,
        version=specification.version,
        creator=specification.creator,
        pack_type=specification.pack_type,
        compatibility=compatibility,
        dependencies=specification.dependencies,
        provenance=tuple(sorted(provenance, key=lambda item: item.sort_key)),
        license=specification.license,
        capability_labels=tuple(
            sorted(
                set(specification.capability_labels)
                | {"DATA_ONLY", contract.adapter_id}
            )
        ),
        inventory=inventory,
        entrypoints=(
            PackEntrypointV1(
                entrypoint_id=f"{specification.pack_type.value.casefold()}-data",
                data_id=f"{specification.pack_type.value.casefold()}-domain-index",
                path=DOMAIN_PACK_INDEX_PATH_V1,
            ),
        ),
    )
    verify_pack_payload_identity(manifest, payloads)
    archive_bytes = _normalized_archive_bytes(manifest, payloads)
    verification = verify_domain_pack_archive_bytes(
        archive_bytes,
        expected_pack_id=manifest.pack_id,
    )
    return DomainPackBuildV1(
        manifest=manifest,
        index=index,
        archive_bytes=archive_bytes,
        preflight=verification.preflight,
    )


def verify_domain_pack_archive_bytes(
    archive_bytes: bytes,
    *,
    expected_pack_id: str | None = None,
) -> DomainPackVerificationV1:
    """Apply full archive preflight, restore original bytes, and run the adapter."""

    preflight = preflight_pack_archive_bytes(
        archive_bytes,
        expected_pack_id=expected_pack_id,
    )
    manifest = preflight.manifest
    contract = domain_pack_adapter_v1(manifest.pack_type)
    inventory_by_path = {item.path: item for item in manifest.inventory}
    index_declaration = inventory_by_path.get(DOMAIN_PACK_INDEX_PATH_V1)
    if (
        index_declaration is None
        or index_declaration.content_format is not PackContentFormatV1.CANONICAL_JSON
        or index_declaration.media_type != "application/json"
        or index_declaration.schema_id != DOMAIN_PACK_INDEX_SCHEMA_ID
        or index_declaration.schema_version != DOMAIN_PACK_INDEX_SCHEMA_VERSION
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.DOMAIN_INDEX_INVALID,
            "pack inventory does not declare the exact WO39-D domain index",
        )
    members = _read_declared_members(archive_bytes, manifest)
    try:
        index = DomainPackIndexV1.from_canonical_bytes(
            members[DOMAIN_PACK_INDEX_PATH_V1]
        )
    except DomainPackRefused:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.DOMAIN_INDEX_INVALID,
            "domain index failed exact canonical reconstruction",
        ) from error
    if (
        index.pack_type is not manifest.pack_type
        or index.adapter_id != contract.adapter_id
        or index.adapter_version != contract.adapter_version
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.PACK_TYPE_MISMATCH,
            "domain index, manifest, and installed adapter disagree",
        )
    _validate_manifest_domain_binding(manifest, index, contract)
    expected_payload_paths = {item.payload_path for item in index.artifacts}
    if expected_payload_paths | {DOMAIN_PACK_INDEX_PATH_V1} != set(inventory_by_path):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            "manifest contains undeclared domain payloads or omits indexed artifacts",
        )

    originals: dict[str, bytes] = {}
    for row in index.artifacts:
        declaration = inventory_by_path.get(row.payload_path)
        if declaration is None:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
                f"indexed payload is absent from manifest: {row.payload_path}",
            )
        payload = members[row.payload_path]
        original = _restore_original_bytes(row, declaration, payload)
        if (
            len(original) != row.original_byte_count
            or not hmac.compare_digest(
                hashlib.sha256(original).hexdigest(),
                row.original_sha256,
            )
        ):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
                f"restored original bytes differ: {row.artifact_id}",
            )
        originals[row.artifact_id] = original
    _validate_domain(index, originals, license=manifest.license)
    return DomainPackVerificationV1(
        preflight=preflight,
        index=index,
        original_artifact_count=len(originals),
        original_total_byte_count=sum(map(len, originals.values())),
    )


def builtin_pack_runtime_environment_v1() -> PackRuntimeEnvironmentV1:
    """Return only the nine exact adapter/schema capabilities compiled into WO39-D."""

    contracts = tuple(_adapter_contracts().values())
    schemas = {
        (DOMAIN_PACK_INDEX_SCHEMA_ID, DOMAIN_PACK_INDEX_SCHEMA_VERSION),
        (PRESERVED_EXACT_BYTES_SCHEMA_ID, PRESERVED_EXACT_BYTES_SCHEMA_VERSION),
    }
    # Direct original schemas are interpreted only after the owning adapter has
    # verified them.  The install command adds those exact verified schema rows to
    # this closed built-in base for the one archive being installed.
    return PackRuntimeEnvironmentV1(
        engine_component_id="KIRBY2_ENGINE_V1",
        engine_version=__version__,
        compiler_versions=tuple(
            sorted(
                (item.compiler_component_id, item.compiler_version)
                for item in contracts
            )
        ),
        schema_versions=tuple(sorted(schemas)),
    )


def runtime_environment_for_verified_pack_v1(
    verification: DomainPackVerificationV1,
) -> PackRuntimeEnvironmentV1:
    """Bind an install environment to schemas just verified by the local adapter."""

    if type(verification) is not DomainPackVerificationV1:
        raise TypeError("verified pack runtime requires DomainPackVerificationV1")
    base = builtin_pack_runtime_environment_v1()
    schemas = {
        (item.schema_id, item.schema_version)
        for item in verification.manifest.inventory
    }
    # One runtime version per schema ID is possible because the manifest's
    # installability row is exact.  Refuse an internally ambiguous archive.
    by_id: dict[str, set[int]] = {}
    for schema_id, version in schemas:
        by_id.setdefault(schema_id, set()).add(version)
    ambiguous = sorted(schema_id for schema_id, versions in by_id.items() if len(versions) != 1)
    if ambiguous:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            f"pack requires multiple local versions for schema(s): {ambiguous}",
        )
    return PackRuntimeEnvironmentV1(
        engine_component_id=base.engine_component_id,
        engine_version=base.engine_version,
        compiler_versions=base.compiler_versions,
        schema_versions=tuple(
            sorted((schema_id, next(iter(versions))) for schema_id, versions in by_id.items())
        ),
    )


def write_new_pack_archive(build: DomainPackBuildV1, output_path: Path) -> Path:
    """Atomically create one new archive without replacing an existing file."""

    if type(build) is not DomainPackBuildV1:
        raise TypeError("pack archive write requires DomainPackBuildV1")
    target = Path(output_path)
    if target.suffix != ".k2pack":
        raise ValueError("pack archive output must use lowercase .k2pack")
    parent = target.parent.resolve(strict=True)
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("pack archive output parent must be a real directory")
    target = parent / target.name
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(build.archive_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        temporary.unlink()
        directory_descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def _adapter_contracts() -> dict[PackTypeV1, DomainPackAdapterContractV1]:
    return {
        PackTypeV1.SCENARIO: SCENARIO_PACK_ADAPTER_V1,
        PackTypeV1.LESSON: LESSON_PACK_ADAPTER_V1,
        PackTypeV1.CURRICULUM: CURRICULUM_PACK_ADAPTER_V1,
        PackTypeV1.STRATEGY: STRATEGY_PACK_ADAPTER_V1,
        PackTypeV1.MARKET_PROFILE: PROFILE_PACK_ADAPTER_V1,
        PackTypeV1.HISTORICAL: HISTORICAL_PACK_ADAPTER_V1,
        PackTypeV1.REPLAY: REPLAY_PACK_ADAPTER_V1,
        PackTypeV1.ANALYSIS: ANALYSIS_PACK_ADAPTER_V1,
        PackTypeV1.RESEARCH: RESEARCH_PACK_ADAPTER_V1,
    }


def _validate_domain(
    index: DomainPackIndexV1,
    originals: Mapping[str, bytes],
    *,
    license: PackLicenseV1,
) -> None:
    _validate_embedded_evidence_identities(index, originals)
    validators = {
        PackTypeV1.SCENARIO: validate_scenario_pack,
        PackTypeV1.LESSON: validate_lesson_pack,
        PackTypeV1.CURRICULUM: validate_curriculum_pack,
        PackTypeV1.STRATEGY: validate_strategy_pack,
        PackTypeV1.MARKET_PROFILE: validate_profile_pack,
        PackTypeV1.REPLAY: validate_replay_pack,
        PackTypeV1.ANALYSIS: validate_analysis_pack,
        PackTypeV1.RESEARCH: validate_research_pack,
    }
    if index.pack_type is PackTypeV1.HISTORICAL:
        validate_historical_pack(index, originals, license=license)
        return
    validator = validators.get(index.pack_type)
    if validator is None:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.UNSUPPORTED_PACK_TYPE,
            f"no domain verifier is installed for {index.pack_type.value}",
        )
    validator(index, originals)


def _validate_embedded_evidence_identities(
    index: DomainPackIndexV1,
    originals: Mapping[str, bytes],
) -> None:
    """Require optional run/audit rows to retain an owning or exact-byte identity."""

    from kirby2.research.models import RunManifest

    for role in (PackArtifactRoleV1.EMBEDDED_RUN, PackArtifactRoleV1.EMBEDDED_AUDIT):
        for row in index.artifacts_for(role):
            raw = originals.get(row.artifact_id)
            if type(raw) is not bytes:
                raise DomainPackRefused(
                    DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
                    f"embedded evidence bytes are absent: {row.artifact_id}",
                )
            if row.logical_identity_sha256 == hashlib.sha256(raw).hexdigest():
                continue
            if role is PackArtifactRoleV1.EMBEDDED_RUN:
                try:
                    payload = tomllib.loads(raw.decode("utf-8"))
                    manifest = RunManifest.from_dict(payload)
                except (UnicodeDecodeError, tomllib.TOMLDecodeError, TypeError, ValueError):
                    manifest = None
                if (
                    manifest is not None
                    and manifest.to_toml().encode("utf-8") == raw
                    and row.logical_identity_sha256
                    == hashlib.sha256(manifest.run_id.encode("ascii")).hexdigest()
                ):
                    continue
            try:
                payload = load_canonical_json_bytes(raw, "embedded evidence")
            except (TypeError, ValueError):
                payload = None
            if payload is not None and row.logical_identity_sha256 in _all_text_values(
                payload
            ):
                continue
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
                f"embedded {role.value} does not retain an authoritative identity",
            )


def _all_text_values(value: object) -> frozenset[str]:
    found: set[str] = set()

    def visit(item: object) -> None:
        if type(item) is str:
            found.add(item)
        elif type(item) is dict:
            for child in item.values():
                visit(child)
        elif type(item) in {list, tuple}:
            for child in item:
                visit(child)

    visit(value)
    return frozenset(found)


def _validate_content_license(
    specification: PackBuildSpecificationV1,
    contract: DomainPackAdapterContractV1,
) -> None:
    license = specification.license
    if license.content_mode not in contract.allowed_content_modes:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.LICENSE_CONTENT_REFUSED,
            f"{contract.adapter_id} does not allow {license.content_mode.value} mode",
        )
    if (
        license.content_mode is PackContentModeV1.SELF_CONTAINED
        and license.redistribution_policy is PackRedistributionPolicyV1.PROHIBITED
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.LICENSE_CONTENT_REFUSED,
            "self-contained pack construction is refused by the declared policy",
        )


def _validate_manifest_domain_binding(
    manifest: PackManifestV1,
    index: DomainPackIndexV1,
    contract: DomainPackAdapterContractV1,
) -> None:
    if (
        manifest.license.content_mode not in contract.allowed_content_modes
        or (
            manifest.license.content_mode is PackContentModeV1.SELF_CONTAINED
            and manifest.license.redistribution_policy
            is PackRedistributionPolicyV1.PROHIBITED
        )
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.LICENSE_CONTENT_REFUSED,
            "domain artifact bytes conflict with the declared license/content mode",
        )
    expected_provenance = {
        (item.role.value, item.artifact_id, item.original_sha256)
        for item in index.artifacts
    }
    actual_provenance = {
        (item.source_kind, item.source_id, item.source_sha256)
        for item in manifest.provenance
    }
    if not expected_provenance <= actual_provenance:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
            "manifest provenance omits or changes an indexed original artifact",
        )
    extras = actual_provenance - expected_provenance
    if len(extras) > 1 or any(
        source_kind != "PACK_SOURCE_DEFINITION" or source_id != "pack-source"
        for source_kind, source_id, _digest in extras
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
            "manifest provenance contains an unbound domain source",
        )
    if not {"DATA_ONLY", contract.adapter_id} <= set(manifest.capability_labels):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            "manifest omits the data-only or exact adapter capability label",
        )
    if (
        len(manifest.entrypoints) != 1
        or manifest.entrypoints[0].path != DOMAIN_PACK_INDEX_PATH_V1
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.DOMAIN_INDEX_INVALID,
            "domain pack entrypoint must select the sole canonical domain index",
        )
    expected_schemas = _schema_requirements(manifest.inventory)
    readable, installable, executable, replay = manifest.compatibility
    if (
        not readable.supported
        or not installable.supported
        or executable.supported is not contract.supports_execution
        or replay.supported is not contract.supports_replay_equivalence
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            "domain pack compatibility support does not match its exact adapter",
        )
    for row in tuple(
        item for item in manifest.compatibility if item.supported
    ):
        if (
            row.engine is None
            or row.engine.component_id != "KIRBY2_ENGINE_V1"
            or not semver_satisfies(__version__, row.engine.version_constraint)
            or row.schemas != expected_schemas
        ):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
                "domain pack engine/schema compatibility is not locally exact",
            )
    if readable.compilers:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            "readable domain compatibility cannot require a compiler",
        )
    for row in (installable, executable, replay):
        if not row.supported:
            continue
        if (
            len(row.compilers) != 1
            or row.compilers[0].component_id != contract.compiler_component_id
            or not semver_satisfies(
                contract.compiler_version,
                row.compilers[0].version_constraint,
            )
        ):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
                "domain pack compiler compatibility differs from its exact adapter",
            )
def _materialize_artifact_payload(
    ordinal: int,
    declaration: object,
    original: bytes,
    original_sha256: str,
    logical_identity: str,
) -> tuple[str, bytes, PackContentFormatV1, str, str, int]:
    from .types import PackSourceArtifactV1

    if type(declaration) is not PackSourceArtifactV1:
        raise TypeError("artifact materialization requires PackSourceArtifactV1")
    slug = _payload_slug(declaration.artifact_id)
    if declaration.storage_mode is PackArtifactStorageModeV1.DIRECT:
        content_format = declaration.direct_content_format
        if type(content_format) is not PackContentFormatV1:
            raise TypeError("direct artifact lost its content format")
        direct = _DIRECT_DECLARATIONS.get(content_format)
        if content_format is PackContentFormatV1.BINARY_EVIDENCE:
            suffix = _BINARY_DIRECT_SUFFIXES.get(declaration.original_media_type)
            direct = (
                None
                if suffix is None
                else (suffix, declaration.original_media_type)
            )
        if direct is None:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
                "direct artifact uses an unsupported format or binary media type",
            )
        suffix, required_media_type = direct
        if declaration.original_media_type != required_media_type:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
                "direct artifact media type differs from its content format",
            )
        return (
            f"data/{ordinal:04d}-{slug}{suffix}",
            original,
            content_format,
            declaration.original_media_type,
            declaration.original_schema_id,
            declaration.original_schema_version,
        )
    envelope = PreservedExactBytesV1(
        artifact_id=declaration.artifact_id,
        original_media_type=declaration.original_media_type,
        original_schema_id=declaration.original_schema_id,
        original_schema_version=declaration.original_schema_version,
        original_sha256=original_sha256,
        logical_identity_kind=declaration.logical_identity_kind,
        logical_identity_sha256=logical_identity,
        exact_bytes=original,
    )
    return (
        f"data/{ordinal:04d}-{slug}.json",
        envelope.canonical_bytes(),
        PackContentFormatV1.CANONICAL_JSON,
        "application/json",
        PRESERVED_EXACT_BYTES_SCHEMA_ID,
        PRESERVED_EXACT_BYTES_SCHEMA_VERSION,
    )


def _restore_original_bytes(
    row: DomainArtifactIdentityV1,
    declaration: PackFileV1,
    payload: bytes,
) -> bytes:
    if row.storage_mode is PackArtifactStorageModeV1.DIRECT:
        if (
            declaration.schema_id != row.original_schema_id
            or declaration.schema_version != row.original_schema_version
            or declaration.media_type != row.original_media_type
        ):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
                f"direct inventory declaration differs: {row.artifact_id}",
            )
        return payload
    if (
        declaration.content_format is not PackContentFormatV1.CANONICAL_JSON
        or declaration.media_type != "application/json"
        or declaration.schema_id != PRESERVED_EXACT_BYTES_SCHEMA_ID
        or declaration.schema_version != PRESERVED_EXACT_BYTES_SCHEMA_VERSION
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
            f"exact-byte envelope declaration differs: {row.artifact_id}",
        )
    try:
        envelope = PreservedExactBytesV1.from_canonical_bytes(payload)
    except (TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
            f"exact-byte envelope failed reconstruction: {row.artifact_id}",
        ) from error
    expected = {
        "artifact_id": row.artifact_id,
        "logical_identity_kind": row.logical_identity_kind,
        "logical_identity_sha256": row.logical_identity_sha256,
        "original_media_type": row.original_media_type,
        "original_schema_id": row.original_schema_id,
        "original_schema_version": row.original_schema_version,
        "original_sha256": row.original_sha256,
    }
    actual = {
        "artifact_id": envelope.artifact_id,
        "logical_identity_kind": envelope.logical_identity_kind,
        "logical_identity_sha256": envelope.logical_identity_sha256,
        "original_media_type": envelope.original_media_type,
        "original_schema_id": envelope.original_schema_id,
        "original_schema_version": envelope.original_schema_version,
        "original_sha256": envelope.original_sha256,
    }
    if actual != expected:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
            f"exact-byte envelope metadata differs from index: {row.artifact_id}",
        )
    return envelope.exact_bytes


def _schema_requirements(
    inventory: tuple[PackFileV1, ...],
) -> tuple[PackSchemaRequirementV1, ...]:
    versions: dict[str, set[int]] = {}
    for item in inventory:
        versions.setdefault(item.schema_id, set()).add(item.schema_version)
    ambiguous = sorted(
        schema_id for schema_id, values in versions.items() if len(values) != 1
    )
    if ambiguous:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            f"one pack cannot require multiple runtime versions for schema(s): {ambiguous}",
        )
    return tuple(
        PackSchemaRequirementV1(
            schema_id=schema_id,
            supported_versions=tuple(sorted(values)),
        )
        for schema_id, values in sorted(versions.items())
    )


def _compatibility(
    contract: DomainPackAdapterContractV1,
    schemas: tuple[PackSchemaRequirementV1, ...],
) -> tuple[PackCompatibilityV1, ...]:
    engine = PackVersionRequirementV1(
        component_id="KIRBY2_ENGINE_V1",
        version_constraint=__version__,
    )
    compiler = PackVersionRequirementV1(
        component_id=contract.compiler_component_id,
        version_constraint=contract.compiler_version,
    )
    return (
        PackCompatibilityV1(
            level=PackCompatibilityLevelV1.READABLE,
            supported=True,
            engine=engine,
            compilers=(),
            schemas=schemas,
        ),
        PackCompatibilityV1(
            level=PackCompatibilityLevelV1.INSTALLABLE,
            supported=True,
            engine=engine,
            compilers=(compiler,),
            schemas=schemas,
        ),
        (
            PackCompatibilityV1(
                level=PackCompatibilityLevelV1.EXECUTABLE,
                supported=True,
                engine=engine,
                compilers=(compiler,),
                schemas=schemas,
            )
            if contract.supports_execution
            else PackCompatibilityV1(
                level=PackCompatibilityLevelV1.EXECUTABLE,
                supported=False,
            )
        ),
        (
            PackCompatibilityV1(
                level=PackCompatibilityLevelV1.REPLAY_EQUIVALENT,
                supported=True,
                engine=engine,
                compilers=(compiler,),
                schemas=schemas,
            )
            if contract.supports_replay_equivalence
            else PackCompatibilityV1(
                level=PackCompatibilityLevelV1.REPLAY_EQUIVALENT,
                supported=False,
            )
        ),
    )


def _normalized_archive_bytes(
    manifest: PackManifestV1,
    payloads: Mapping[str, bytes],
) -> bytes:
    member_bytes = {K2PACK_MANIFEST_PATH: canonical_manifest_bytes(manifest), **dict(payloads)}
    order = normalized_archive_paths(tuple(member_bytes))
    buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=K2PACK_ZIP_COMPRESSION,
            compresslevel=K2PACK_ZIP_COMPRESSLEVEL,
            allowZip64=False,
            strict_timestamps=True,
        ) as archive:
            for path in order:
                archive.writestr(
                    normalized_zip_info(path),
                    member_bytes[path],
                    compress_type=K2PACK_ZIP_COMPRESSION,
                    compresslevel=K2PACK_ZIP_COMPRESSLEVEL,
                )
    except (OSError, RuntimeError, ValueError, zipfile.LargeZipFile) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARCHIVE_BUILD_FAILED,
            "normalized domain archive construction failed",
        ) from error
    return buffer.getvalue()


def _read_declared_members(
    archive_bytes: bytes,
    manifest: PackManifestV1,
) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as archive:
            return {item.path: archive.read(item.path) for item in manifest.inventory}
    except (KeyError, OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            "preflighted domain payloads could not be read by exact declared path",
        ) from error


def _trusted_source_directory(source_directory: Path) -> Path:
    if source_directory.is_symlink():
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SOURCE_PATH_UNSAFE,
            "pack source directory cannot be a symbolic link",
        )
    try:
        root = source_directory.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SOURCE_PATH_UNSAFE,
            "pack source directory does not resolve safely",
        ) from error
    if not root.is_dir():
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SOURCE_PATH_UNSAFE,
            "pack source must be an existing directory",
        )
    return root


def _read_confined_regular_file(
    root: Path,
    relative_path: str,
    *,
    maximum_bytes: int,
) -> bytes:
    try:
        canonical = require_relative_pack_path(relative_path, "pack source path")
    except (TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SOURCE_PATH_UNSAFE,
            "pack source path is not confined canonical POSIX text",
        ) from error
    candidate = root.joinpath(*PurePosixPath(canonical).parts)
    current = root
    try:
        for part in PurePosixPath(canonical).parts[:-1]:
            current = current / part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("unsafe source parent")
        if candidate.is_symlink():
            raise ValueError("unsafe source symlink")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SOURCE_PATH_UNSAFE,
            f"pack source path escapes, is missing, or traverses a link: {canonical}",
        ) from error
    if not hasattr(os, "O_NOFOLLOW"):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SOURCE_PATH_UNSAFE,
            "pack source capture requires no-follow file opens",
        )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            resolved,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("pack source is not one regular non-linked file")
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.SOURCE_TOO_LARGE,
                f"pack source is empty or exceeds its byte limit: {canonical}",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise DomainPackRefused(
                    DomainPackRefusalCodeV1.SOURCE_TOO_LARGE,
                    f"pack source exceeds its byte limit: {canonical}",
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or total != after.st_size:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.SOURCE_CHANGED,
                f"pack source changed while read: {canonical}",
            )
        return b"".join(chunks)
    except DomainPackRefused:
        raise
    except (OSError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SOURCE_PATH_UNSAFE,
            f"pack source could not be captured safely: {canonical}",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _payload_slug(artifact_id: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", artifact_id.casefold()).strip("-")
    return (slug or "artifact")[:96].rstrip("-")


__all__ = [
    "DOMAIN_PACK_INDEX_PATH_V1",
    "PACK_BUILD_MAX_SOURCE_DEFINITION_BYTES_V1",
    "PACK_SOURCE_FILENAME_V1",
    "DomainPackBuildV1",
    "DomainPackVerificationV1",
    "build_domain_pack",
    "build_pack_source_directory",
    "build_registered_run_pack",
    "builtin_pack_runtime_environment_v1",
    "domain_pack_adapter_v1",
    "runtime_environment_for_verified_pack_v1",
    "supported_domain_pack_types_v1",
    "verify_domain_pack_archive_bytes",
    "write_new_pack_archive",
]
