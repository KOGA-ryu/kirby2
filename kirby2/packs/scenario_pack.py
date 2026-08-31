"""Exact scenario adapter for portable data-only packs."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

from kirby2.scenario_lang.commands import inspect_scenario_source
from kirby2.scenario_lang.models import CompiledScenarioArtifactV1

from .formats import canonical_json_bytes, load_canonical_json_bytes
from .models import (
    PackContentFormatV1,
    PackContentModeV1,
    PackCreatorV1,
    PackLicenseV1,
    PackRedistributionPolicyV1,
    PackTypeV1,
)
from .types import (
    DomainPackAdapterContractV1,
    DomainPackIndexV1,
    DomainPackRefusalCodeV1,
    DomainPackRefused,
    PackArtifactRoleV1,
    PackArtifactStorageModeV1,
    PackBuildSpecificationV1,
    PackSourceArtifactV1,
    validate_adapter_inventory,
)


SCENARIO_PACK_ADAPTER_ID_V1 = "KIRBY2_SCENARIO_PACK_ADAPTER_V1"
SCENARIO_PACK_CAPABILITIES_SCHEMA_ID_V1 = "KIRBY2_SCENARIO_PACK_CAPABILITIES_V1"


def _roles(*items: PackArtifactRoleV1) -> tuple[PackArtifactRoleV1, ...]:
    return tuple(sorted(items, key=lambda item: item.value))


SCENARIO_PACK_ADAPTER_V1 = DomainPackAdapterContractV1(
    pack_type=PackTypeV1.SCENARIO,
    adapter_id=SCENARIO_PACK_ADAPTER_ID_V1,
    adapter_version=1,
    compiler_component_id="KIRBY2_SCENARIO_PACK_COMPILER_V1",
    compiler_version="0.1.0",
    required_roles=_roles(
        PackArtifactRoleV1.SCENARIO_SOURCE,
        PackArtifactRoleV1.SCENARIO_COMPILED,
        PackArtifactRoleV1.SCENARIO_VALIDATION,
        PackArtifactRoleV1.SCENARIO_CAPABILITIES,
    ),
    allowed_roles=_roles(
        PackArtifactRoleV1.SCENARIO_SOURCE,
        PackArtifactRoleV1.SCENARIO_COMPILED,
        PackArtifactRoleV1.SCENARIO_VALIDATION,
        PackArtifactRoleV1.SCENARIO_CAPABILITIES,
        PackArtifactRoleV1.EMBEDDED_RUN,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    multiple_roles=_roles(
        PackArtifactRoleV1.SCENARIO_SOURCE,
        PackArtifactRoleV1.EMBEDDED_RUN,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    primary_roles=_roles(PackArtifactRoleV1.SCENARIO_COMPILED),
    supports_replay_equivalence=True,
)


def validate_scenario_pack(
    index: DomainPackIndexV1,
    original_bytes: Mapping[str, bytes],
) -> None:
    """Rebind compiled, validation, source, and capability identities exactly."""

    validate_adapter_inventory(
        SCENARIO_PACK_ADAPTER_V1,
        index.pack_type,
        index.primary_artifact_id,
        index.artifacts,
    )
    compiled_row = index.artifact(PackArtifactRoleV1.SCENARIO_COMPILED)
    validation_row = index.artifact(PackArtifactRoleV1.SCENARIO_VALIDATION)
    capabilities_row = index.artifact(PackArtifactRoleV1.SCENARIO_CAPABILITIES)
    try:
        compiled = CompiledScenarioArtifactV1.from_bytes(
            _artifact_bytes(original_bytes, compiled_row.artifact_id)
        )
    except (TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SCENARIO_IDENTITY_MISMATCH,
            "compiled scenario artifact failed its owning parser",
        ) from error
    if not compiled.execution_eligible or compiled.validation_report is None:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SCENARIO_IDENTITY_MISMATCH,
            "scenario pack requires an execution-eligible validated artifact",
        )
    report = compiled.validation_report
    report_bytes = _artifact_bytes(original_bytes, validation_row.artifact_id)
    if report.canonical_bytes() != report_bytes:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SCENARIO_IDENTITY_MISMATCH,
            "separate validation artifact differs from the compiled scenario binding",
        )
    if (
        compiled_row.logical_identity_sha256 != compiled.compiled_artifact_digest
        or validation_row.logical_identity_sha256 != report.validation_report_digest
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SCENARIO_IDENTITY_MISMATCH,
            "scenario logical identities differ from their owning contracts",
        )
    source_rows = index.artifacts_for(PackArtifactRoleV1.SCENARIO_SOURCE)
    if not any(
        row.logical_identity_sha256 == compiled.source_bundle_digest
        for row in source_rows
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SCENARIO_IDENTITY_MISMATCH,
            "scenario sources do not retain the compiled source-bundle identity",
        )

    raw_capabilities = _artifact_bytes(original_bytes, capabilities_row.artifact_id)
    payload = load_canonical_json_bytes(raw_capabilities, "scenario pack capabilities")
    if type(payload) is not dict or set(payload) != {
        "capability_decisions",
        "capability_digest",
        "run_identity_digest",
        "schema_id",
        "schema_version",
        "source_bundle_digest",
    }:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SCENARIO_IDENTITY_MISMATCH,
            "scenario capability artifact has the wrong exact schema",
        )
    expected_capabilities = _scenario_capability_payload(compiled)
    if payload != expected_capabilities:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SCENARIO_IDENTITY_MISMATCH,
            "scenario capability artifact differs from compiled capability decisions",
        )
    capability_identity = hashlib.sha256(raw_capabilities).hexdigest()
    if capabilities_row.logical_identity_sha256 != capability_identity:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SCENARIO_IDENTITY_MISMATCH,
            "scenario capability identity differs from its canonical bytes",
        )


def build_scenario_demo_inputs(
    source_path: Path,
) -> tuple[PackBuildSpecificationV1, dict[str, bytes]]:
    """Compile one canonical source into the exact four-part scenario pack input."""

    result = inspect_scenario_source(Path(source_path))
    if not result.passed or result.artifact is None or result.resolved is None:
        detail = (
            result.diagnostics[0].code
            if result.diagnostics
            else "SCENARIO_SOURCE_NOT_EXECUTION_ELIGIBLE"
        )
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SCENARIO_IDENTITY_MISMATCH,
            f"scenario source did not pass compile/validation: {detail}",
        )
    artifact = result.artifact
    report = artifact.validation_report
    if report is None:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.SCENARIO_IDENTITY_MISMATCH,
            "validated scenario lost its validation report",
        )

    source_root = result.source_path.parent
    source_entries: list[PackSourceArtifactV1] = []
    payloads: dict[str, bytes] = {}
    for ordinal, document in enumerate(result.resolved.import_bundle.documents):
        if document.pack_namespace is not None:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.SOURCE_PATH_UNSAFE,
                "single-source demo cannot capture an activated external pack import",
            )
        candidate = (source_root / document.logical_path).resolve(strict=True)
        try:
            candidate.relative_to(source_root.resolve(strict=True))
        except ValueError as error:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.SOURCE_PATH_UNSAFE,
                "scenario import escaped its source root during pack capture",
            ) from error
        raw = candidate.read_bytes()
        if hashlib.sha256(raw).hexdigest() != document.raw_sha256:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.SOURCE_CHANGED,
                "scenario source bytes changed after compiler resolution",
            )
        source_id = f"scenario-source-{ordinal:04d}"
        payloads[source_id] = raw
        source_entries.append(
            PackSourceArtifactV1(
                artifact_id=source_id,
                role=PackArtifactRoleV1.SCENARIO_SOURCE,
                source_path=document.logical_path,
                original_schema_id="KIRBY2_SCENARIO_SOURCE_V1",
                original_schema_version=1,
                original_media_type="application/toml",
                storage_mode=PackArtifactStorageModeV1.DIRECT,
                logical_identity_kind=(
                    "SCENARIO_SOURCE_BUNDLE_SHA256_V1"
                    if ordinal == 0
                    else "SCENARIO_SOURCE_DOCUMENT_SHA256_V1"
                ),
                logical_identity_sha256=(
                    artifact.source_bundle_digest if ordinal == 0 else document.raw_sha256
                ),
                direct_content_format=PackContentFormatV1.TOML,
            )
        )

    compiled_id = "scenario-compiled"
    validation_id = "scenario-validation"
    capabilities_id = "scenario-capabilities"
    compiled_bytes = artifact.canonical_bytes()
    validation_bytes = report.canonical_bytes()
    capability_bytes = canonical_json_bytes(_scenario_capability_payload(artifact))
    payloads.update(
        {
            compiled_id: compiled_bytes,
            validation_id: validation_bytes,
            capabilities_id: capability_bytes,
        }
    )
    source_entries.extend(
        (
            PackSourceArtifactV1(
                artifact_id=compiled_id,
                role=PackArtifactRoleV1.SCENARIO_COMPILED,
                source_path="generated/compiled-scenario.json",
                original_schema_id="KIRBY2_COMPILED_SCENARIO_ARTIFACT_V1",
                original_schema_version=1,
                original_media_type="application/json",
                storage_mode=PackArtifactStorageModeV1.DIRECT,
                logical_identity_kind="SCENARIO_COMPILED_ARTIFACT_SHA256_V1",
                logical_identity_sha256=artifact.compiled_artifact_digest,
                direct_content_format=PackContentFormatV1.CANONICAL_JSON,
            ),
            PackSourceArtifactV1(
                artifact_id=validation_id,
                role=PackArtifactRoleV1.SCENARIO_VALIDATION,
                source_path="generated/scenario-validation.json",
                original_schema_id="KIRBY2_SCENARIO_VALIDATION_REPORT_V1",
                original_schema_version=1,
                original_media_type="application/json",
                storage_mode=PackArtifactStorageModeV1.DIRECT,
                logical_identity_kind="SCENARIO_VALIDATION_REPORT_SHA256_V1",
                logical_identity_sha256=report.validation_report_digest,
                direct_content_format=PackContentFormatV1.CANONICAL_JSON,
            ),
            PackSourceArtifactV1(
                artifact_id=capabilities_id,
                role=PackArtifactRoleV1.SCENARIO_CAPABILITIES,
                source_path="generated/scenario-capabilities.json",
                original_schema_id=SCENARIO_PACK_CAPABILITIES_SCHEMA_ID_V1,
                original_schema_version=1,
                original_media_type="application/json",
                storage_mode=PackArtifactStorageModeV1.DIRECT,
                logical_identity_kind="SCENARIO_CAPABILITY_BINDING_SHA256_V1",
                logical_identity_sha256=hashlib.sha256(capability_bytes).hexdigest(),
                direct_content_format=PackContentFormatV1.CANONICAL_JSON,
            ),
        )
    )
    metadata = result.resolved.root_source.metadata
    pack_name = _pack_name(metadata.scenario_id)
    capabilities = tuple(
        sorted(
            {
                decision.capability_id
                for decision in report.capability_decisions
                if not decision.blocks_execution
            }
        )
    )
    specification = PackBuildSpecificationV1(
        namespace="kirby2.examples",
        name=pack_name,
        title=metadata.title,
        version=f"{metadata.scenario_version}.0.0",
        creator=PackCreatorV1(
            display_name="Kirby2 Project",
            identity_uri="https://kirby2.local/project",
        ),
        pack_type=PackTypeV1.SCENARIO,
        primary_artifact_id=compiled_id,
        dependencies=(),
        license=PackLicenseV1(
            license_id="KIRBY2-PROJECT",
            license_name="Kirby2 project data license",
            license_uri="https://kirby2.local/license",
            redistribution_policy=PackRedistributionPolicyV1.ALLOWED,
            content_mode=PackContentModeV1.SELF_CONTAINED,
        ),
        capability_labels=capabilities,
        artifacts=tuple(sorted(source_entries, key=lambda item: item.artifact_id)),
    )
    return specification, payloads


def _scenario_capability_payload(
    artifact: CompiledScenarioArtifactV1,
) -> dict[str, object]:
    report = artifact.validation_report
    if report is None:
        raise ValueError("scenario capability payload requires a validation report")
    return {
        "capability_decisions": [
            item.as_dict() for item in report.capability_decisions
        ],
        "capability_digest": artifact.plan_envelope.capability_digest,
        "run_identity_digest": artifact.run_identity_digest,
        "schema_id": SCENARIO_PACK_CAPABILITIES_SCHEMA_ID_V1,
        "schema_version": 1,
        "source_bundle_digest": artifact.source_bundle_digest,
    }


def _artifact_bytes(values: Mapping[str, bytes], artifact_id: str) -> bytes:
    raw = values.get(artifact_id)
    if type(raw) is not bytes:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            f"artifact bytes are absent: {artifact_id}",
        )
    return raw


def _pack_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", value.casefold()).strip("-")
    if not name or not name[0].isalpha():
        name = f"scenario-{name or 'example'}"
    return name[:128].rstrip("-")


__all__ = [
    "SCENARIO_PACK_ADAPTER_ID_V1",
    "SCENARIO_PACK_ADAPTER_V1",
    "SCENARIO_PACK_CAPABILITIES_SCHEMA_ID_V1",
    "build_scenario_demo_inputs",
    "validate_scenario_pack",
]
