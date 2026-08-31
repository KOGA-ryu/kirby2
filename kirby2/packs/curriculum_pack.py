"""Exact curriculum adapter preserving training and review boundaries."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from .formats import load_canonical_json_bytes, load_canonical_toml_bytes
from .models import PackTypeV1
from .types import (
    DomainPackAdapterContractV1,
    DomainPackIndexV1,
    DomainPackRefusalCodeV1,
    DomainPackRefused,
    PackArtifactRoleV1,
    validate_adapter_inventory,
)


CURRICULUM_PACK_ADAPTER_ID_V1 = "KIRBY2_CURRICULUM_PACK_ADAPTER_V1"


def _roles(*items: PackArtifactRoleV1) -> tuple[PackArtifactRoleV1, ...]:
    return tuple(sorted(items, key=lambda item: item.value))


_POLICY_ROLES = _roles(
    PackArtifactRoleV1.CURRICULUM_DETECTOR,
    PackArtifactRoleV1.CURRICULUM_CAPABILITIES,
    PackArtifactRoleV1.CURRICULUM_OBSERVABLE_POLICY,
    PackArtifactRoleV1.CURRICULUM_REVEAL_POLICY,
    PackArtifactRoleV1.CURRICULUM_SKILLS,
    PackArtifactRoleV1.CURRICULUM_SCORING,
    PackArtifactRoleV1.CURRICULUM_REVIEW_SIDECAR,
)

CURRICULUM_PACK_ADAPTER_V1 = DomainPackAdapterContractV1(
    pack_type=PackTypeV1.CURRICULUM,
    adapter_id=CURRICULUM_PACK_ADAPTER_ID_V1,
    adapter_version=1,
    compiler_component_id="KIRBY2_CURRICULUM_PACK_COMPILER_V1",
    compiler_version="0.1.0",
    required_roles=_roles(PackArtifactRoleV1.CURRICULUM_SOURCE, *_POLICY_ROLES),
    allowed_roles=_roles(
        PackArtifactRoleV1.CURRICULUM_SOURCE,
        *_POLICY_ROLES,
        PackArtifactRoleV1.EMBEDDED_RUN,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    multiple_roles=_roles(
        PackArtifactRoleV1.EMBEDDED_RUN,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    primary_roles=_roles(PackArtifactRoleV1.CURRICULUM_SOURCE),
    supports_replay_equivalence=False,
)


def validate_curriculum_pack(
    index: DomainPackIndexV1,
    original_bytes: Mapping[str, bytes],
) -> None:
    """Require exact source/detector/capability/skill/scoring/review identities."""

    validate_adapter_inventory(
        CURRICULUM_PACK_ADAPTER_V1,
        index.pack_type,
        index.primary_artifact_id,
        index.artifacts,
    )
    _validate_source_identity(
        index.artifact(PackArtifactRoleV1.CURRICULUM_SOURCE),
        original_bytes,
        "curriculum source",
    )
    for role in _POLICY_ROLES:
        row = index.artifact(role)
        raw = original_bytes.get(row.artifact_id)
        if type(raw) is not bytes:
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
                f"curriculum artifact bytes are absent: {row.artifact_id}",
            )
        payload = _structured_object(row, raw, f"curriculum {role.value}")
        if (
            row.logical_identity_sha256 != hashlib.sha256(raw).hexdigest()
            and row.logical_identity_sha256 not in _all_text_values(payload)
        ):
            raise DomainPackRefused(
                DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
                f"curriculum role {role.value} has an unbound logical identity",
            )
    observable = index.artifact(PackArtifactRoleV1.CURRICULUM_OBSERVABLE_POLICY)
    reveal = index.artifact(PackArtifactRoleV1.CURRICULUM_REVEAL_POLICY)
    if observable.logical_identity_sha256 == reveal.logical_identity_sha256:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.REVEAL_POLICY_VIOLATION,
            "curriculum observable and reveal policies cannot collapse to one identity",
        )


def _all_text_values(value: object) -> frozenset[str]:
    found: set[str] = set()

    def visit(item: object) -> None:
        if type(item) is str:
            found.add(item)
        elif type(item) is dict:
            for child in item.values():
                visit(child)
        elif type(item) is list:
            for child in item:
                visit(child)

    visit(value)
    return frozenset(found)


def _validate_source_identity(row: object, values: Mapping[str, bytes], label: str) -> None:
    from .types import DomainArtifactIdentityV1

    if type(row) is not DomainArtifactIdentityV1:
        raise TypeError("curriculum source validation requires DomainArtifactIdentityV1")
    raw = values.get(row.artifact_id)
    if type(raw) is not bytes:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            f"{label} bytes are absent",
        )
    if row.logical_identity_sha256 == hashlib.sha256(raw).hexdigest():
        return
    payload = _structured_object(row, raw, label)
    if row.logical_identity_sha256 not in _all_text_values(payload):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
            f"{label} logical identity is not bound to its record",
        )


def _structured_object(row: object, raw: bytes, label: str) -> dict[str, object]:
    from .types import DomainArtifactIdentityV1

    if type(row) is not DomainArtifactIdentityV1:
        raise TypeError("curriculum structured validation requires DomainArtifactIdentityV1")
    try:
        if row.original_media_type == "application/toml":
            payload = load_canonical_toml_bytes(raw, label)
        elif row.original_media_type in {
            "application/json",
            "application/vnd.kirby2.report+json",
        }:
            payload = load_canonical_json_bytes(raw, label)
        else:
            raise ValueError("unsupported structured media type")
    except (TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
            f"{label} must preserve canonical JSON or TOML object data",
        ) from error
    if type(payload) is not dict:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
            f"{label} must preserve an object record",
        )
    return payload


__all__ = [
    "CURRICULUM_PACK_ADAPTER_ID_V1",
    "CURRICULUM_PACK_ADAPTER_V1",
    "validate_curriculum_pack",
]
