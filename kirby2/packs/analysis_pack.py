"""Canonical report-data and annotation-only analysis pack adapter."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from .formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
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


ANALYSIS_PACK_ADAPTER_ID_V1 = "KIRBY2_ANALYSIS_PACK_ADAPTER_V1"
ANALYSIS_PROVENANCE_SCHEMA_ID_V1 = "KIRBY2_ANALYSIS_PACK_PROVENANCE_V1"
ANALYSIS_RENDERING_POLICY_V1 = "DATA_ONLY_INSTALLED_RENDERER_REQUIRED"

_RUN_ID = re.compile(r"run-[0-9a-f]{24}\Z")
_FORBIDDEN_CODE_KEYS = frozenset(
    {
        "css",
        "html",
        "javascript",
        "module_code",
        "renderer_code",
        "script",
        "script_body",
        "source_code",
    }
)
_FORBIDDEN_CODE_PREFIXES = (
    "<!doctype html",
    "<html",
    "<script",
    "javascript:",
)


def _roles(*items: PackArtifactRoleV1) -> tuple[PackArtifactRoleV1, ...]:
    return tuple(sorted(items, key=lambda item: item.value))


ANALYSIS_PACK_ADAPTER_V1 = DomainPackAdapterContractV1(
    pack_type=PackTypeV1.ANALYSIS,
    adapter_id=ANALYSIS_PACK_ADAPTER_ID_V1,
    adapter_version=1,
    compiler_component_id="KIRBY2_ANALYSIS_PACK_COMPILER_V1",
    compiler_version="0.1.0",
    required_roles=_roles(
        PackArtifactRoleV1.ANALYSIS_REPORT_DATA,
        PackArtifactRoleV1.ANALYSIS_PROVENANCE,
    ),
    allowed_roles=_roles(
        PackArtifactRoleV1.ANALYSIS_REPORT_DATA,
        PackArtifactRoleV1.ANALYSIS_ANNOTATIONS,
        PackArtifactRoleV1.ANALYSIS_PROVENANCE,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    multiple_roles=_roles(
        PackArtifactRoleV1.ANALYSIS_REPORT_DATA,
        PackArtifactRoleV1.ANALYSIS_ANNOTATIONS,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    primary_roles=_roles(PackArtifactRoleV1.ANALYSIS_REPORT_DATA),
    supports_replay_equivalence=False,
    supports_execution=False,
)


@dataclass(frozen=True, slots=True)
class AnalysisProvenanceRecordV1:
    source_run_id: str | None
    source_run_manifest_sha256: str | None
    source_artifact_sha256s: tuple[str, ...]
    rendering_policy: str = ANALYSIS_RENDERING_POLICY_V1

    schema_id: ClassVar[str] = ANALYSIS_PROVENANCE_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        if (self.source_run_id is None) != (
            self.source_run_manifest_sha256 is None
        ):
            raise ValueError("analysis run ID and manifest digest must travel together")
        if self.source_run_id is not None and not _RUN_ID.fullmatch(self.source_run_id):
            raise ValueError("analysis source run ID is invalid")
        if self.source_run_manifest_sha256 is not None:
            require_sha256(
                self.source_run_manifest_sha256,
                "analysis source run-manifest digest",
            )
        if type(self.source_artifact_sha256s) is not tuple or not self.source_artifact_sha256s:
            raise ValueError("analysis provenance requires source artifact digests")
        for digest in self.source_artifact_sha256s:
            require_sha256(digest, "analysis source artifact digest")
        if self.source_artifact_sha256s != tuple(sorted(self.source_artifact_sha256s)):
            raise ValueError("analysis source artifact digests must be canonically ordered")
        if self.rendering_policy != ANALYSIS_RENDERING_POLICY_V1:
            raise ValueError("analysis rendering policy differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "rendering_policy": self.rendering_policy,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_artifact_sha256s": list(self.source_artifact_sha256s),
            "source_run_id": self.source_run_id,
            "source_run_manifest_sha256": self.source_run_manifest_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> AnalysisProvenanceRecordV1:
        value = load_canonical_json_bytes(raw, "analysis provenance")
        expected = {
            "rendering_policy",
            "schema_id",
            "schema_version",
            "source_artifact_sha256s",
            "source_run_id",
            "source_run_manifest_sha256",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("analysis provenance fields differ")
        if value["schema_id"] != cls.schema_id or value["schema_version"] != 1:
            raise ValueError("analysis provenance schema differs")
        digests = value["source_artifact_sha256s"]
        if type(digests) is not list or any(type(item) is not str for item in digests):
            raise TypeError("analysis source artifact digests must be an array of text")
        source_run_id = value["source_run_id"]
        source_manifest = value["source_run_manifest_sha256"]
        if source_run_id is not None and type(source_run_id) is not str:
            raise TypeError("analysis source run ID must be text or null")
        if source_manifest is not None and type(source_manifest) is not str:
            raise TypeError("analysis source manifest digest must be text or null")
        rendering_policy = value["rendering_policy"]
        if type(rendering_policy) is not str:
            raise TypeError("analysis rendering policy must be text")
        restored = cls(
            source_run_id=source_run_id,
            source_run_manifest_sha256=source_manifest,
            source_artifact_sha256s=tuple(digests),
            rendering_policy=rendering_policy,
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("analysis provenance changed during restoration")
        return restored


def validate_analysis_pack(
    index: DomainPackIndexV1,
    original_bytes: Mapping[str, bytes],
) -> None:
    """Refuse executable renderer material and bind every data artifact exactly."""

    validate_adapter_inventory(
        ANALYSIS_PACK_ADAPTER_V1,
        index.pack_type,
        index.primary_artifact_id,
        index.artifacts,
    )
    provenance_row = index.artifact(PackArtifactRoleV1.ANALYSIS_PROVENANCE)
    if (
        provenance_row.storage_mode is not PackArtifactStorageModeV1.DIRECT
        or provenance_row.original_media_type != "application/json"
        or provenance_row.original_schema_id != ANALYSIS_PROVENANCE_SCHEMA_ID_V1
        or provenance_row.original_schema_version != 1
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
            "analysis provenance must be direct canonical JSON",
        )
    try:
        provenance = AnalysisProvenanceRecordV1.from_canonical_bytes(
            _artifact_bytes(original_bytes, provenance_row.artifact_id)
        )
    except (TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
            "analysis provenance failed its exact canonical schema",
        ) from error
    if provenance_row.logical_identity_sha256 != hashlib.sha256(
        _artifact_bytes(original_bytes, provenance_row.artifact_id)
    ).hexdigest():
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
            "analysis provenance logical identity differs from canonical bytes",
        )

    data_rows = tuple(
        item
        for item in index.artifacts
        if item.role
        in {
            PackArtifactRoleV1.ANALYSIS_REPORT_DATA,
            PackArtifactRoleV1.ANALYSIS_ANNOTATIONS,
        }
    )
    actual_digests: list[str] = []
    for row in data_rows:
        if (
            row.storage_mode is not PackArtifactStorageModeV1.DIRECT
            or row.original_media_type
            not in {"application/json", "application/vnd.kirby2.report+json"}
        ):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.RENDERER_INJECTION_REFUSED,
                "analysis artifacts must be direct canonical JSON/report data",
            )
        raw = _artifact_bytes(original_bytes, row.artifact_id)
        try:
            value = load_canonical_json_bytes(raw, "analysis data artifact")
        except (TypeError, ValueError) as error:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
                f"analysis data is not canonical JSON: {row.artifact_id}",
            ) from error
        if _contains_renderer_code(value):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.RENDERER_INJECTION_REFUSED,
                f"analysis data contains HTML/JavaScript renderer code: {row.artifact_id}",
            )
        digest = hashlib.sha256(raw).hexdigest()
        if row.logical_identity_sha256 != digest:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
                f"analysis logical identity differs from canonical data: {row.artifact_id}",
            )
        actual_digests.append(digest)
    if tuple(sorted(actual_digests)) != provenance.source_artifact_sha256s:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
            "analysis provenance does not bind the complete report/annotation inventory",
        )


def _contains_renderer_code(value: object) -> bool:
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                return True
            if key.casefold() in _FORBIDDEN_CODE_KEYS:
                return True
            if _contains_renderer_code(child):
                return True
        return False
    if type(value) is list:
        return any(_contains_renderer_code(item) for item in value)
    if type(value) is str:
        normalized = value.lstrip().casefold()
        return normalized.startswith(_FORBIDDEN_CODE_PREFIXES)
    return False


def _artifact_bytes(values: Mapping[str, bytes], artifact_id: str) -> bytes:
    raw = values.get(artifact_id)
    if type(raw) is not bytes:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            f"analysis artifact bytes are absent: {artifact_id}",
        )
    return raw


__all__ = [
    "ANALYSIS_PACK_ADAPTER_ID_V1",
    "ANALYSIS_PACK_ADAPTER_V1",
    "ANALYSIS_PROVENANCE_SCHEMA_ID_V1",
    "ANALYSIS_RENDERING_POLICY_V1",
    "AnalysisProvenanceRecordV1",
    "validate_analysis_pack",
]
