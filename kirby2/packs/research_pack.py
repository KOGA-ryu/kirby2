"""Consent- and redaction-bound research evidence pack adapter.

The pack layer does not invent a privacy assertion.  It accepts only the governed
portable evidence bundle produced by ``kirby2.instructor.export`` and reconstructs
that owning contract before accepting the archive.  No private/direct-identity
export mode is implemented by this adapter.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from kirby2.instructor.export import (
    EVIDENCE_EXPORT_MANIFEST_SCHEMA_ID,
    EXPORT_CONSENT_DECISION_SCHEMA_ID,
    EXPORT_INVENTORY_SCHEMA_ID,
    EvidenceExportBundleV1,
    EvidenceExportManifestV1,
    ExportConsentDecisionV1,
    ExportInventoryV1,
)
from kirby2.instructor.redaction import REDACTION_MANIFEST_SCHEMA_ID, RedactionManifestV1

from .formats import load_canonical_json_bytes
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


RESEARCH_PACK_ADAPTER_ID_V1 = "KIRBY2_RESEARCH_PACK_ADAPTER_V1"
RESEARCH_REDACTED_EVIDENCE_SCHEMA_ID_V1 = (
    "KIRBY2_RESEARCH_REDACTED_EVIDENCE_V1"
)


def _roles(*items: PackArtifactRoleV1) -> tuple[PackArtifactRoleV1, ...]:
    return tuple(sorted(items, key=lambda item: item.value))


RESEARCH_PACK_ADAPTER_V1 = DomainPackAdapterContractV1(
    pack_type=PackTypeV1.RESEARCH,
    adapter_id=RESEARCH_PACK_ADAPTER_ID_V1,
    adapter_version=1,
    compiler_component_id="KIRBY2_RESEARCH_PACK_COMPILER_V1",
    compiler_version="0.1.0",
    required_roles=_roles(
        PackArtifactRoleV1.RESEARCH_EXPORT_MANIFEST,
        PackArtifactRoleV1.RESEARCH_EXPORT_INVENTORY,
        PackArtifactRoleV1.RESEARCH_REDACTED_EVIDENCE,
        PackArtifactRoleV1.RESEARCH_REDACTION_MANIFEST,
        PackArtifactRoleV1.RESEARCH_CONSENT_DECISION,
    ),
    allowed_roles=_roles(
        PackArtifactRoleV1.RESEARCH_EXPORT_MANIFEST,
        PackArtifactRoleV1.RESEARCH_EXPORT_INVENTORY,
        PackArtifactRoleV1.RESEARCH_REDACTED_EVIDENCE,
        PackArtifactRoleV1.RESEARCH_REDACTION_MANIFEST,
        PackArtifactRoleV1.RESEARCH_CONSENT_DECISION,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    multiple_roles=_roles(PackArtifactRoleV1.EMBEDDED_AUDIT),
    primary_roles=_roles(PackArtifactRoleV1.RESEARCH_EXPORT_MANIFEST),
    supports_replay_equivalence=False,
    supports_execution=False,
)


def validate_research_pack(
    index: DomainPackIndexV1,
    original_bytes: Mapping[str, bytes],
) -> None:
    """Rebuild the governed portable export and reject any weaker privacy claim."""

    validate_adapter_inventory(
        RESEARCH_PACK_ADAPTER_V1,
        index.pack_type,
        index.primary_artifact_id,
        index.artifacts,
    )
    rows = {
        role: index.artifact(role)
        for role in (
            PackArtifactRoleV1.RESEARCH_EXPORT_MANIFEST,
            PackArtifactRoleV1.RESEARCH_EXPORT_INVENTORY,
            PackArtifactRoleV1.RESEARCH_REDACTED_EVIDENCE,
            PackArtifactRoleV1.RESEARCH_REDACTION_MANIFEST,
            PackArtifactRoleV1.RESEARCH_CONSENT_DECISION,
        )
    }
    expected_schemas = {
        PackArtifactRoleV1.RESEARCH_EXPORT_MANIFEST: (
            EVIDENCE_EXPORT_MANIFEST_SCHEMA_ID
        ),
        PackArtifactRoleV1.RESEARCH_EXPORT_INVENTORY: EXPORT_INVENTORY_SCHEMA_ID,
        PackArtifactRoleV1.RESEARCH_REDACTED_EVIDENCE: (
            RESEARCH_REDACTED_EVIDENCE_SCHEMA_ID_V1
        ),
        PackArtifactRoleV1.RESEARCH_REDACTION_MANIFEST: REDACTION_MANIFEST_SCHEMA_ID,
        PackArtifactRoleV1.RESEARCH_CONSENT_DECISION: (
            EXPORT_CONSENT_DECISION_SCHEMA_ID
        ),
    }
    for role, row in rows.items():
        if (
            row.storage_mode is not PackArtifactStorageModeV1.DIRECT
            or row.original_media_type != "application/json"
            or row.original_schema_id != expected_schemas[role]
            or row.original_schema_version != 1
        ):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.RESEARCH_REDACTION_VIOLATION,
                f"research privacy artifact must be direct canonical JSON: {role.value}",
            )
    try:
        consent = ExportConsentDecisionV1.from_canonical_bytes(
            _artifact_bytes(
                original_bytes,
                rows[PackArtifactRoleV1.RESEARCH_CONSENT_DECISION].artifact_id,
            )
        )
    except (PermissionError, TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.RESEARCH_CONSENT_REQUIRED,
            "research pack requires an allowed governed pseudonymous export decision",
        ) from error
    try:
        manifest = EvidenceExportManifestV1.from_canonical_bytes(
            _artifact_bytes(
                original_bytes,
                rows[PackArtifactRoleV1.RESEARCH_EXPORT_MANIFEST].artifact_id,
            )
        )
        inventory = ExportInventoryV1.from_canonical_bytes(
            _artifact_bytes(
                original_bytes,
                rows[PackArtifactRoleV1.RESEARCH_EXPORT_INVENTORY].artifact_id,
            )
        )
        evidence = _artifact_bytes(
            original_bytes,
            rows[PackArtifactRoleV1.RESEARCH_REDACTED_EVIDENCE].artifact_id,
        )
        load_canonical_json_bytes(evidence, "research redacted evidence")
        redaction = RedactionManifestV1.from_canonical_bytes(
            _artifact_bytes(
                original_bytes,
                rows[PackArtifactRoleV1.RESEARCH_REDACTION_MANIFEST].artifact_id,
            )
        )
        bundle = EvidenceExportBundleV1(
            manifest=manifest,
            inventory=inventory,
            evidence_bytes=evidence,
            redaction_manifest=redaction,
            consent_decision=consent,
        )
    except PermissionError as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.RESEARCH_CONSENT_REQUIRED,
            "research pack export is not authorized by the retained consent contract",
        ) from error
    except (TypeError, ValueError) as error:
        code = (
            DomainPackRefusalCodeV1.DIRECT_IDENTITY_REFUSED
            if any(
                token in str(error).casefold()
                for token in ("direct ident", "identity mapping", "email")
            )
            else DomainPackRefusalCodeV1.RESEARCH_REDACTION_VIOLATION
        )
        raise DomainPackRefused(
            code,
            "research evidence failed its field-redaction and portable-export contract",
        ) from error

    expected_identities = {
        PackArtifactRoleV1.RESEARCH_EXPORT_MANIFEST: manifest.sha256,
        PackArtifactRoleV1.RESEARCH_EXPORT_INVENTORY: inventory.sha256,
        PackArtifactRoleV1.RESEARCH_REDACTED_EVIDENCE: hashlib.sha256(evidence).hexdigest(),
        PackArtifactRoleV1.RESEARCH_REDACTION_MANIFEST: redaction.sha256,
        PackArtifactRoleV1.RESEARCH_CONSENT_DECISION: consent.sha256,
    }
    for role, expected in expected_identities.items():
        row = rows[role]
        raw = _artifact_bytes(original_bytes, row.artifact_id)
        if (
            row.logical_identity_sha256 != expected
            or row.original_sha256 != hashlib.sha256(raw).hexdigest()
        ):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
                f"research owning identity differs from the pack row: {role.value}",
            )
    if bundle.export_id != manifest.export_id:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.RESEARCH_REDACTION_VIOLATION,
            "research export identity changed during bundle reconstruction",
        )


def _artifact_bytes(values: Mapping[str, bytes], artifact_id: str) -> bytes:
    raw = values.get(artifact_id)
    if type(raw) is not bytes:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            f"research artifact bytes are absent: {artifact_id}",
        )
    return raw


__all__ = [
    "RESEARCH_PACK_ADAPTER_ID_V1",
    "RESEARCH_PACK_ADAPTER_V1",
    "RESEARCH_REDACTED_EVIDENCE_SCHEMA_ID_V1",
    "validate_research_pack",
]
