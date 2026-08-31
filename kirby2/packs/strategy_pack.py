"""Exact strategy adapter retaining legacy, semantic-AST, and lineage identity."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from kirby2.discovery.identity import (
    canonical_strategy_ast_bytes,
    legacy_strategy_source_sha256,
    lineage_payload_sha256,
    strategy_semantic_sha256,
)
from kirby2.strategy.language import parse_strategy_semantic_ast

from .formats import load_canonical_json_bytes
from .models import PackTypeV1
from .types import (
    DomainPackAdapterContractV1,
    DomainPackIndexV1,
    DomainPackRefusalCodeV1,
    DomainPackRefused,
    PackArtifactRoleV1,
    validate_adapter_inventory,
)


STRATEGY_PACK_ADAPTER_ID_V1 = "KIRBY2_STRATEGY_PACK_ADAPTER_V1"


def _roles(*items: PackArtifactRoleV1) -> tuple[PackArtifactRoleV1, ...]:
    return tuple(sorted(items, key=lambda item: item.value))


STRATEGY_PACK_ADAPTER_V1 = DomainPackAdapterContractV1(
    pack_type=PackTypeV1.STRATEGY,
    adapter_id=STRATEGY_PACK_ADAPTER_ID_V1,
    adapter_version=1,
    compiler_component_id="KIRBY2_STRATEGY_PACK_COMPILER_V1",
    compiler_version="0.1.0",
    required_roles=_roles(
        PackArtifactRoleV1.STRATEGY_LEGACY_SOURCE,
        PackArtifactRoleV1.STRATEGY_CANONICAL_AST,
        PackArtifactRoleV1.STRATEGY_EXPERIMENT_LINEAGE,
    ),
    allowed_roles=_roles(
        PackArtifactRoleV1.STRATEGY_LEGACY_SOURCE,
        PackArtifactRoleV1.STRATEGY_CANONICAL_AST,
        PackArtifactRoleV1.STRATEGY_EXPERIMENT_LINEAGE,
        PackArtifactRoleV1.EMBEDDED_RUN,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    multiple_roles=_roles(
        PackArtifactRoleV1.EMBEDDED_RUN,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    primary_roles=_roles(PackArtifactRoleV1.STRATEGY_CANONICAL_AST),
    supports_replay_equivalence=False,
)


def validate_strategy_pack(
    index: DomainPackIndexV1,
    original_bytes: Mapping[str, bytes],
) -> None:
    """Recompute both existing strategy identities and bind experiment lineage."""

    validate_adapter_inventory(
        STRATEGY_PACK_ADAPTER_V1,
        index.pack_type,
        index.primary_artifact_id,
        index.artifacts,
    )
    source_row = index.artifact(PackArtifactRoleV1.STRATEGY_LEGACY_SOURCE)
    ast_row = index.artifact(PackArtifactRoleV1.STRATEGY_CANONICAL_AST)
    lineage_row = index.artifact(PackArtifactRoleV1.STRATEGY_EXPERIMENT_LINEAGE)
    source_raw = _artifact_bytes(original_bytes, source_row.artifact_id)
    ast_raw = _artifact_bytes(original_bytes, ast_row.artifact_id)
    lineage_raw = _artifact_bytes(original_bytes, lineage_row.artifact_id)
    try:
        source = source_raw.decode("utf-8")
        ast = parse_strategy_semantic_ast(source)
    except (UnicodeDecodeError, TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.STRATEGY_IDENTITY_MISMATCH,
            "legacy strategy source failed the owning strategy parser",
        ) from error
    expected_source_identity = legacy_strategy_source_sha256(source)
    expected_ast_identity = strategy_semantic_sha256(ast)
    if (
        source_row.logical_identity_sha256 != expected_source_identity
        or ast_row.logical_identity_sha256 != expected_ast_identity
        or canonical_strategy_ast_bytes(ast) != ast_raw
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.STRATEGY_IDENTITY_MISMATCH,
            "legacy source and canonical AST do not retain their dual identity",
        )
    try:
        lineage = load_canonical_json_bytes(lineage_raw, "strategy experiment lineage")
    except (TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.STRATEGY_IDENTITY_MISMATCH,
            "strategy experiment lineage must be canonical JSON",
        ) from error
    if type(lineage) is not dict or expected_ast_identity not in _all_text_values(lineage):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.STRATEGY_IDENTITY_MISMATCH,
            "strategy experiment lineage does not bind the packed semantic AST",
        )
    if lineage_row.logical_identity_sha256 != lineage_payload_sha256(lineage):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.STRATEGY_IDENTITY_MISMATCH,
            "strategy lineage logical identity differs from its owning domain hash",
        )
    if source_row.original_sha256 != hashlib.sha256(source_raw).hexdigest():
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.STRATEGY_IDENTITY_MISMATCH,
            "legacy strategy exact-byte identity changed",
        )


def _artifact_bytes(values: Mapping[str, bytes], artifact_id: str) -> bytes:
    raw = values.get(artifact_id)
    if type(raw) is not bytes:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            f"strategy artifact bytes are absent: {artifact_id}",
        )
    return raw


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


__all__ = [
    "STRATEGY_PACK_ADAPTER_ID_V1",
    "STRATEGY_PACK_ADAPTER_V1",
    "validate_strategy_pack",
]
